"""
dashboard_server.py — Flask сервер with DB-бекендом

Все yesнные читаются/пишутся via db.py. Ниasих файлов кроме ghost_shell.db
(плюс payload_debug.json в профиле for C++ ядра).

Overпуск:
    python dashboard_server.py
    → http://127.0.0.1:5000
"""

import os
import sys
import json
import time
import queue
import logging
import threading
import subprocess
from datetime import datetime
from typing import Optional

# Windows cp1252 stdout quirk — force UTF-8 so the dashboard's own log
# messages (and the stdout of main.py subprocesses we pipe through our
# broadcast logger) don't crash StreamHandler on emoji/Cyrillic. See
# main.py for details.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass

try:
    from flask import Flask, request, jsonify, send_file, Response
    from flask_cors import CORS
except ImportError:
    print("pip install flask flask-cors")
    sys.exit(1)

from db import get_db
from platform_paths import popen_flags_no_console, terminate_process_tree


def _popen_no_console_flags():
    """Cross-platform Popen flags that (a) don't pop a console on Windows,
    (b) put the child in its own process group on Unix."""
    return popen_flags_no_console()


# ──────────────────────────────────────────────────────────────
# RUNNER STATE
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# RUNNER STATE — now a pool of concurrent runs instead of one slot
# ──────────────────────────────────────────────────────────────

class RunnerSlot:
    """
    Represents a single active run — one profile, one main.py subprocess,
    one chromium tree. Each slot is fully isolated from other slots.

    Slots are keyed by run_id in RunnerPool. After the subprocess exits,
    the slot is kept around for a short grace period (so the UI can
    display the final status) then garbage-collected.
    """
    def __init__(self, run_id: int, profile_name: str):
        self.run_id         = run_id
        self.profile_name   = profile_name
        self.thread         = None
        self.process        = None       # subprocess.Popen handle
        self.started_at     = datetime.now().isoformat(timespec="seconds")
        self.finished_at    = None
        self.last_exit_code = None
        self.last_error     = None
        self.is_running     = True
        # Per-slot ring buffer for streaming logs to the dashboard.
        # Sized to survive a burst but not eat memory on 100 runs.
        self.log_queue      = queue.Queue(maxsize=1000)

    def log(self, message: str, level: str = "info"):
        entry = {
            "ts":           datetime.now().strftime("%H:%M:%S"),
            "level":        level,
            "message":      message,
            "run_id":       self.run_id,
            "profile_name": self.profile_name,
        }
        try:
            self.log_queue.put_nowait(entry)
        except queue.Full:
            try:
                self.log_queue.get_nowait()
                self.log_queue.put_nowait(entry)
            except Exception:
                pass
        try:
            get_db().log_add(self.run_id, level, message)
        except Exception:
            pass


class RunnerPool:
    """
    Global registry of concurrent runs. Thread-safe reads/writes.

    Responsibilities:
      - Spawn new slots (one per profile launch)
      - Enforce max_parallel concurrency cap from config
      - Route log lines / stop requests / status queries to the right slot
      - Broadcast a merged log stream to dashboard SSE consumers

    Does NOT own the actual subprocess lifecycle — that lives in the
    per-slot run_thread inside api_run(). The pool is just the bookkeeping.
    """
    def __init__(self):
        self._lock        = threading.RLock()
        self._slots       = {}   # run_id -> RunnerSlot
        # Merged stream for the dashboard — one queue everyone can subscribe to.
        # Per-slot queues still exist for targeted consumers.
        self.broadcast_queue = queue.Queue(maxsize=5000)

    # ── Lifecycle ───────────────────────────────────────────────
    def add(self, slot: RunnerSlot) -> None:
        with self._lock:
            self._slots[slot.run_id] = slot

    def mark_finished(self, run_id: int, exit_code: int = None,
                      error: str = None) -> None:
        with self._lock:
            slot = self._slots.get(run_id)
            if not slot:
                return
            slot.is_running     = False
            slot.finished_at    = datetime.now().isoformat(timespec="seconds")
            slot.last_exit_code = exit_code
            slot.last_error     = error

    def remove(self, run_id: int) -> None:
        with self._lock:
            self._slots.pop(run_id, None)

    # ── Queries ─────────────────────────────────────────────────
    def get(self, run_id: int) -> Optional[RunnerSlot]:
        with self._lock:
            return self._slots.get(run_id)

    def get_by_profile(self, profile_name: str) -> Optional[RunnerSlot]:
        """First slot running this profile, or None. Useful for the UI
        (one profile can realistically only run in one slot at a time —
        two runs of the same profile would share a user-data-dir and
        corrupt each other)."""
        with self._lock:
            for slot in self._slots.values():
                if slot.profile_name == profile_name and slot.is_running:
                    return slot
            return None

    def active_runs(self) -> list[dict]:
        with self._lock:
            return [self._slot_to_dict(s) for s in self._slots.values()
                    if s.is_running]

    def all_slots(self) -> list[dict]:
        with self._lock:
            return [self._slot_to_dict(s) for s in self._slots.values()]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._slots.values() if s.is_running)

    def is_profile_running(self, profile_name: str) -> bool:
        return self.get_by_profile(profile_name) is not None

    # ── Broadcast logging (for the merged /api/logs/live stream) ─
    def broadcast_log(self, entry: dict) -> None:
        try:
            self.broadcast_queue.put_nowait(entry)
        except queue.Full:
            # Drop oldest so newer messages reach subscribers
            try:
                self.broadcast_queue.get_nowait()
                self.broadcast_queue.put_nowait(entry)
            except Exception:
                pass

    # ── Helpers ─────────────────────────────────────────────────
    def _slot_to_dict(self, slot: RunnerSlot) -> dict:
        # Pull the heartbeat_at from the DB so the UI can show how
        # fresh each run's liveness ping is. Useful for spotting
        # wedged browsers in the Runs page before the watchdog
        # kills them.
        hb_age = None
        try:
            row = get_db()._get_conn().execute(
                "SELECT heartbeat_at FROM runs WHERE id = ?",
                (slot.run_id,)
            ).fetchone()
            if row and row["heartbeat_at"]:
                hb = datetime.fromisoformat(row["heartbeat_at"])
                hb_age = int((datetime.now() - hb).total_seconds())
        except Exception:
            pass
        return {
            "run_id":         slot.run_id,
            "profile_name":   slot.profile_name,
            "started_at":     slot.started_at,
            "finished_at":    slot.finished_at,
            "last_exit_code": slot.last_exit_code,
            "last_error":     slot.last_error,
            "is_running":     slot.is_running,
            "heartbeat_age":  hb_age,    # seconds since last heartbeat, or null
        }


# The global pool replaces the old singleton RUNNER. A compatibility
# shim below exposes the old RUNNER.* attributes for code paths that
# haven't been migrated yet (they see "whatever is most recent").
RUNNER_POOL = RunnerPool()


class _LegacyRunnerShim:
    """
    Back-compat for pre-pool code that reads RUNNER.is_running /
    RUNNER.current_run_id / RUNNER.profile_name. Returns values for
    the most recently started slot if any is active, else sensible
    defaults. New code should use RUNNER_POOL directly.

    Writes are accepted and forwarded to RUNNER_POOL for the most
    recent slot, to keep the older run_thread() paths working during
    migration.
    """
    @property
    def is_running(self):
        return RUNNER_POOL.active_count() > 0

    @property
    def current_run_id(self):
        runs = RUNNER_POOL.active_runs()
        return runs[0]["run_id"] if runs else None

    @property
    def profile_name(self):
        runs = RUNNER_POOL.active_runs()
        return runs[0]["profile_name"] if runs else None

    # Fields written by run_thread() — no-ops now (slot owns them)
    @is_running.setter
    def is_running(self, _): pass
    @current_run_id.setter
    def current_run_id(self, _): pass
    @profile_name.setter
    def profile_name(self, _): pass

    # Legacy public attrs — provide stubs so setattr doesn't crash.
    # Kept for endpoints that hang "started_at / finished_at / etc"
    # on the global but no longer mean anything meaningful multi-run.
    started_at     = None
    finished_at    = None
    last_exit_code = None
    last_error     = None
    thread         = None
    process        = None

    def log(self, message: str, level: str = "info"):
        """Legacy global log — broadcast to all subscribers.
        Individual runs should use their own slot.log() now."""
        entry = {
            "ts":       datetime.now().strftime("%H:%M:%S"),
            "level":    level,
            "message":  message,
            "run_id":   None,
            "profile_name": None,
        }
        RUNNER_POOL.broadcast_log(entry)


RUNNER = _LegacyRunnerShim()


def cleanup_stale_runs():
    """
    Resolve inconsistent DB/process state left by previous dashboard
    instances. For each run stuck in 'running' state (finished_at IS NULL):

      * dead process / stale heartbeat → force-kill any descendants and
        mark the run as failed
      * live process with fresh heartbeat → leave alone (dashboard was
        restarted but scheduler or manual run is still healthily going)

    Before this function existed, we only flipped the DB flag — Chrome
    subprocesses from the old dashboard kept running and collided with
    fresh spawns on user-data-dir locks. Now we genuinely clean up.
    """
    try:
        from process_reaper import reap_stale_runs
        db = get_db()
        stats = reap_stale_runs(db, reason_prefix="dashboard-restart")
        if any(stats.values()):
            logging.info(
                f"[startup] Stale-run reap: "
                f"killed={stats['killed']}, marked={stats['marked_finished']}, "
                f"still alive={stats['alive_left_alone']}"
            )
    except Exception as e:
        logging.error(f"[startup] Stale cleanup failed: {e}", exc_info=True)


# ──────────────────────────────────────────────────────────────
# FLASK
# ──────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder="dashboard", static_url_path="")
CORS(app)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.route("/")
def index():
    """Serve the dashboard SPA entry page."""
    html_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "dashboard", "index.html"
    )
    if not os.path.exists(html_path):
        return (
            "<h1>dashboard/index.html not found</h1>"
            "<p>Create the <code>dashboard/</code> folder next to dashboard_server.py</p>",
            404
        )
    return send_file(html_path)


# ──────────────────────────────────────────────────────────────
# API: CONFIG
# ──────────────────────────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_config_get():
    try:
        return jsonify(get_db().config_get_all())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["POST"])
def api_config_set():
    try:
        data = request.get_json(force=True)
        get_db().config_set_all(data)
        return jsonify({"ok": True, "saved_at": datetime.now().isoformat(timespec="seconds")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# API: STATS
# ──────────────────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
def api_stats():
    db = get_db()
    total_summary = db.events_summary(hours=24 * 365)  # all события
    total_comp, unique_domains = db.competitors_count()
    profiles = db.profiles_list()

    # Post-click action counters — rolled up from action_events
    actions_24h = db.action_events_summary(hours=24)
    actions_all = db.action_events_summary(hours=24 * 365)

    # Build info — which Chromium / Chrome versions ship with this binary
    try:
        from device_templates import CHROMIUM_BUILD, CHROMIUM_BUILD_FULL, CHROME_VERSIONS
        stable_chrome = CHROME_VERSIONS[0]["major"] if CHROME_VERSIONS else "?"
        # User-configurable spoof range (major version bounds)
        spoof_min = db.config_get("browser.spoof_chrome_min") or None
        spoof_max = db.config_get("browser.spoof_chrome_max") or None
        build_info = {
            "chromium_build":      CHROMIUM_BUILD,
            "chromium_build_full": CHROMIUM_BUILD_FULL,
            "chrome_spoof":        stable_chrome,     # top of the pool
            "chrome_pool":         [v["major"] for v in CHROME_VERSIONS],
            "chrome_pool_full":    [v["full"]  for v in CHROME_VERSIONS],
            "spoof_min":           spoof_min,
            "spoof_max":           spoof_max,
        }
    except Exception:
        build_info = {}

    return jsonify({
        "total_profiles":    len(profiles),
        "total_searches":    total_summary.get("search_ok", 0),
        "total_captchas":    total_summary.get("captcha", 0),
        "total_blocks":      total_summary.get("blocked", 0),
        "total_competitors": total_comp,
        "unique_domains":    unique_domains,
        "daily":             db.daily_stats(days=14),
        "run_status":        get_run_status_dict(),
        # Post-click action stats (24h and all-time)
        "actions_24h":       actions_24h,
        "actions_total":     actions_all,
        "build_info":        build_info,
    })


# ──────────────────────────────────────────────────────────────
# API: TRAFFIC STATS (aggregated by profile × domain × hour)
# ──────────────────────────────────────────────────────────────

@app.route("/api/traffic/summary", methods=["GET"])
def api_traffic_summary():
    """Global traffic totals + hourly time series.
    Query params:
      ?hours=N         — default 24, max 2160 (90 days)
      ?bucket=hour|day — chart granularity
    """
    hours  = min(int(request.args.get("hours", 24) or 24), 24 * 90)
    bucket = request.args.get("bucket", "hour")
    if bucket not in ("hour", "day"):
        bucket = "hour"
    db = get_db()
    summary = db.traffic_summary(hours=hours)
    summary["timeseries"] = db.traffic_timeseries(hours=hours, bucket=bucket)
    return jsonify(summary)


@app.route("/api/traffic/by-profile", methods=["GET"])
def api_traffic_by_profile():
    """Per-profile traffic totals sorted by bytes desc."""
    hours = min(int(request.args.get("hours", 24) or 24), 24 * 90)
    return jsonify({
        "hours":    hours,
        "profiles": get_db().traffic_by_profile(hours=hours),
    })


@app.route("/api/traffic/by-domain", methods=["GET"])
def api_traffic_by_domain():
    """Top domains by bytes. Optionally filter to one profile."""
    hours        = min(int(request.args.get("hours", 24) or 24), 24 * 90)
    limit        = min(int(request.args.get("limit", 50) or 50), 500)
    profile_name = request.args.get("profile") or None
    return jsonify({
        "hours":   hours,
        "profile": profile_name,
        "domains": get_db().traffic_by_domain(
            hours=hours, limit=limit, profile_name=profile_name
        ),
    })


@app.route("/api/traffic/timeseries", methods=["GET"])
def api_traffic_timeseries():
    """Time-series data for the main traffic chart."""
    hours  = min(int(request.args.get("hours", 24) or 24), 24 * 90)
    bucket = request.args.get("bucket", "hour")
    profile_name = request.args.get("profile") or None
    if bucket not in ("hour", "day"):
        bucket = "hour"
    return jsonify({
        "hours":   hours,
        "bucket":  bucket,
        "profile": profile_name,
        "series":  get_db().traffic_timeseries(
            hours=hours, bucket=bucket, profile_name=profile_name
        ),
    })


# ──────────────────────────────────────────────────────────────
# API: PROFILES
# ──────────────────────────────────────────────────────────────

@app.route("/api/profiles", methods=["GET"])
def api_profiles():
    return jsonify(get_db().profiles_list())


@app.route("/api/profiles/<name>/selfcheck", methods=["GET"])
def api_profile_selfcheck(name: str):
    sc = get_db().selfcheck_latest(name)
    if not sc:
        return jsonify({"error": "Нет selfcheck. Overпусти мониторинг"}), 404
    return jsonify(sc)


@app.route("/api/profiles/<name>/selfcheck/history", methods=["GET"])
def api_profile_selfcheck_history(name: str):
    return jsonify(get_db().selfchecks_history(name, limit=20))


@app.route("/api/profiles/<name>/fingerprint", methods=["GET"])
def api_profile_fingerprint(name: str):
    fp = get_db().fingerprint_current(name)
    if not fp:
        return jsonify({"error": "Нет fingerprint"}), 404
    return jsonify(fp["payload"])


@app.route("/api/profiles/<name>/reset-health", methods=["POST"])
def api_profile_reset_health(name: str):
    """Reset consecutive blocks marker — use after fixing the root cause."""
    try:
        path = os.path.join("profiles", name)
        if not os.path.exists(path):
            return jsonify({"error": f"Profile {name} not found"}), 404
        from session_quality import SessionQualityMonitor
        sqm = SessionQualityMonitor(path)
        sqm.reset_consecutive_blocks()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<name>/clear-session-quality", methods=["POST"])
def api_profile_clear_session_quality(name: str):
    """Delete entire session_quality.json — fresh start."""
    try:
        path = os.path.join("profiles", name)
        if not os.path.exists(path):
            return jsonify({"error": f"Profile {name} not found"}), 404
        from session_quality import SessionQualityMonitor
        sqm = SessionQualityMonitor(path)
        sqm.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<n>/meta", methods=["GET"])
def api_profile_meta_get(name: str):
    """Dashboard-level metadata for a profile: tags, per-profile proxy
    override, rotation config override, notes, group memberships."""
    meta = get_db().profile_meta_get(name)
    return jsonify(meta)


@app.route("/api/profiles/<n>/meta", methods=["POST"])
def api_profile_meta_set(name: str):
    """Partial update of profile metadata. Only keys in the body are
    touched — pass {} to get current state without changes."""
    payload = request.get_json(silent=True) or {}
    allowed = {
        "tags", "proxy_url", "proxy_is_rotating",
        "rotation_api_url", "rotation_provider", "rotation_api_key",
        "notes",
    }
    updates = {k: v for k, v in payload.items() if k in allowed}
    if updates:
        get_db().profile_meta_upsert(name, **updates)
    return jsonify(get_db().profile_meta_get(name))


@app.route("/api/profiles/<n>/tags", methods=["POST"])
def api_profile_set_tags(name: str):
    """Convenience endpoint for the tag editor — replaces the whole
    tag list for this profile."""
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags") or []
    if not isinstance(tags, list):
        return jsonify({"error": "tags must be a list"}), 400
    clean = []
    seen = set()
    for t in tags:
        t = str(t).strip()
        if not t: continue
        key = t.lower()
        if key in seen: continue
        seen.add(key)
        clean.append(t)
    get_db().profile_meta_upsert(name, tags=clean)
    return jsonify({"ok": True, "tags": clean})


# ──────────────────────────────────────────────────────────────
# API: COOKIES / SESSION (per profile)
# ──────────────────────────────────────────────────────────────

@app.route("/api/profiles/<n>/cookies", methods=["GET"])
def api_profile_cookies_list(name: str):
    """Return all stored cookies for this profile as Selenium-shape
    dicts. Read from profiles/<n>/ghostshell_session/cookies.json —
    Chrome's live DB is not touched.
    """
    import cookie_manager
    cookies = cookie_manager.list_cookies(name)
    return jsonify({
        "count":   len(cookies),
        "cookies": cookies,
    })


@app.route("/api/profiles/<n>/cookies/export", methods=["GET"])
def api_profile_cookies_export(name: str):
    """Download cookies as a JSON or Netscape file.
    Query params:
      ?format=json (default) — EditThisCookie-compatible JSON array
      ?format=netscape       — classic cookies.txt (curl/wget)
    """
    import cookie_manager
    cookies = cookie_manager.list_cookies(name)
    fmt = (request.args.get("format") or "json").lower()

    if fmt == "netscape":
        from flask import Response as _Resp
        body = cookie_manager.to_netscape(cookies)
        return _Resp(
            body,
            mimetype="text/plain",
            headers={
                "Content-Disposition":
                    f'attachment; filename="cookies-{name}.txt"',
            },
        )

    # Default JSON
    from flask import Response as _Resp
    body = json.dumps(cookies, ensure_ascii=False, indent=2)
    return _Resp(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="cookies-{name}.json"',
        },
    )


@app.route("/api/profiles/<n>/cookies/import", methods=["POST"])
def api_profile_cookies_import(name: str):
    """Import cookies from a JSON or Netscape blob.

    Body: {blob: "...", mode: "merge" | "replace"}
    - merge   (default): add these cookies to existing ones, duplicates
      keyed by (name, domain, path) overwrite old values
    - replace: discard existing cookies, use imported list only

    Returns {count, added, replaced_total}. If the payload can't be
    parsed at all, returns 400 with the parse error.
    """
    import cookie_manager
    payload = request.get_json(silent=True) or {}
    blob = payload.get("blob") or ""
    mode = (payload.get("mode") or "merge").lower()

    try:
        new_cookies = cookie_manager.parse_import(blob)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    existing = cookie_manager.list_cookies(name) if mode == "merge" else []

    # Merge by (name, domain, path) — identical spec overwrites.
    by_key = {
        (c.get("name"), c.get("domain"), c.get("path", "/")): c
        for c in existing
    }
    added = 0
    for c in new_cookies:
        key = (c.get("name"), c.get("domain"), c.get("path", "/"))
        if key not in by_key:
            added += 1
        by_key[key] = c

    final = list(by_key.values())
    cookie_manager.save_cookies(name, final)
    return jsonify({
        "ok":             True,
        "count":          len(final),
        "added":          added,
        "imported_total": len(new_cookies),
        "mode":           mode,
    })


@app.route("/api/profiles/<n>/cookies/clear", methods=["POST"])
def api_profile_cookies_clear(name: str):
    """Delete all cookies for this profile. Note: if the profile is
    currently running, Chrome's live DB still holds them until shutdown.
    The dashboard shows a warning for active profiles."""
    import cookie_manager
    cookie_manager.clear_cookies(name)
    return jsonify({"ok": True})


@app.route("/api/profiles/<n>/cookies/<path:cookie_name>", methods=["DELETE"])
def api_profile_cookie_delete(name: str, cookie_name: str):
    """Delete a single cookie by its name (across all domains the
    profile has for it). Used by the row-level delete button in the UI."""
    import cookie_manager
    cookies = cookie_manager.list_cookies(name)
    before = len(cookies)
    filtered = [c for c in cookies if c.get("name") != cookie_name]
    cookie_manager.save_cookies(name, filtered)
    return jsonify({"ok": True, "removed": before - len(filtered)})


@app.route("/api/profiles/<n>/storage", methods=["GET"])
def api_profile_storage_list(name: str):
    """Return stored localStorage map (per-origin JSON dict)."""
    import cookie_manager
    return jsonify(cookie_manager.list_storage(name))


# ──────────────────────────────────────────────────────────────
# API: PROFILE GROUPS
# ──────────────────────────────────────────────────────────────

@app.route("/api/groups", methods=["GET"])
def api_groups_list():
    return jsonify(get_db().group_list())


@app.route("/api/groups", methods=["POST"])
def api_groups_create():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    try:
        gid = get_db().group_create(
            name         = name,
            description  = payload.get("description"),
            script       = payload.get("script"),
            max_parallel = payload.get("max_parallel"),
        )
        members = payload.get("members") or []
        if members:
            get_db().group_set_members(gid, members)
        return jsonify({"ok": True, "id": gid,
                        "group": get_db().group_get(gid)})
    except Exception as e:
        return jsonify({"error": str(e)}), 409


@app.route("/api/groups/<int:group_id>", methods=["GET"])
def api_groups_get(group_id: int):
    g = get_db().group_get(group_id)
    if not g:
        return jsonify({"error": "not found"}), 404
    return jsonify(g)


@app.route("/api/groups/<int:group_id>", methods=["POST"])
def api_groups_update(group_id: int):
    payload = request.get_json(silent=True) or {}
    allowed = {"name", "description", "script", "max_parallel"}
    updates = {k: v for k, v in payload.items() if k in allowed}
    if updates:
        get_db().group_update(group_id, **updates)
    if "members" in payload and isinstance(payload["members"], list):
        get_db().group_set_members(group_id, payload["members"])
    return jsonify(get_db().group_get(group_id))


@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
def api_groups_delete(group_id: int):
    get_db().group_delete(group_id)
    return jsonify({"ok": True})


@app.route("/api/groups/<int:group_id>/start", methods=["POST"])
def api_groups_start(group_id: int):
    """
    Launch every member of this group as a separate concurrent run.
    Respects the effective max_parallel cap (group-specific if set,
    otherwise global). Profiles that don't fit end up in `queued`.
    """
    g = get_db().group_get(group_id)
    if not g:
        return jsonify({"error": "group not found"}), 404

    db = get_db()
    max_parallel = int(
        g.get("max_parallel")
        or db.config_get("runner.max_parallel", 4)
        or 4
    )
    room = max(0, max_parallel - RUNNER_POOL.active_count())

    started, queued, errors = [], [], []
    for name in g["members"]:
        if RUNNER_POOL.is_profile_running(name):
            continue
        if len(started) >= room:
            queued.append(name)
            continue
        try:
            result = _spawn_run(name)
            started.append(result)
        except ValueError as e:
            errors.append({"profile_name": name, "error": str(e)})

    return jsonify({
        "ok":           True,
        "started":      started,
        "queued":       queued,
        "errors":       errors,
        "max_parallel": max_parallel,
    })


@app.route("/api/groups/<int:group_id>/stop", methods=["POST"])
def api_groups_stop(group_id: int):
    """Stop every active run belonging to this group's members."""
    g = get_db().group_get(group_id)
    if not g:
        return jsonify({"error": "group not found"}), 404

    stopped = []
    for name in g["members"]:
        slot = RUNNER_POOL.get_by_profile(name)
        if slot:
            try:
                api_runs_stop_specific(slot.run_id)
                stopped.append({"profile_name": name, "run_id": slot.run_id})
            except Exception as e:
                stopped.append({"profile_name": name, "error": str(e)})
    return jsonify({"ok": True, "stopped": stopped, "count": len(stopped)})


# ──────────────────────────────────────────────────────────────
# API: COMPETITORS
# ──────────────────────────────────────────────────────────────

@app.route("/api/competitors", methods=["GET"])
def api_competitors():
    db = get_db()
    total, unique = db.competitors_count()
    by_domain = db.competitors_by_domain()

    # Merge in per-domain action counters (how many times did we click /
    # interact with an ad on this domain — not just how often we saw it)
    actions_by_domain = db.action_events_by_domain()
    for row in by_domain:
        stats = actions_by_domain.get(row["domain"], {})
        row["actions_ran"]     = stats.get("ran", 0)
        row["actions_skipped"] = stats.get("skipped", 0)
        row["actions_errored"] = stats.get("errored", 0)
        row["last_action_at"]  = stats.get("last_action_at")

    return jsonify({
        "total_records":  total,
        "unique_domains": unique,
        "by_domain":      by_domain,
        "recent":         db.competitors_recent(limit=100),
    })


# ──────────────────────────────────────────────────────────────
# API: IPs
# ──────────────────────────────────────────────────────────────

@app.route("/api/ips", methods=["GET"])
def api_ips():
    return jsonify(get_db().ip_stats())


# ──────────────────────────────────────────────────────────────
# API: RUNS
# ──────────────────────────────────────────────────────────────

@app.route("/api/runs", methods=["GET"])
def api_runs():
    return jsonify(get_db().runs_list(limit=50))


def get_run_status_dict():
    """
    Legacy shape — expected by the sidebar's run-status widget.
    Returns state of the single "sidebar-default" run if the sidebar
    Start button was used. When multiple profiles are running, returns
    the most recent one.

    New code should call /api/runs/active for a full list.
    """
    active = RUNNER_POOL.active_runs()
    if not active:
        # Return a sane "idle" shape. Fields are present so the sidebar
        # can render "Finished (code N)" text from the last known slot.
        all_slots = RUNNER_POOL.all_slots()
        last = all_slots[-1] if all_slots else None
        return {
            "is_running":     False,
            "current_run_id": last["run_id"] if last else None,
            "profile_name":   last["profile_name"] if last else None,
            "started_at":     last["started_at"] if last else None,
            "finished_at":    last["finished_at"] if last else None,
            "last_exit_code": last["last_exit_code"] if last else None,
            "last_error":     last["last_error"] if last else None,
            "active_count":   0,
        }
    # Most recently started run (biggest run_id)
    latest = max(active, key=lambda s: s["run_id"])
    return {
        "is_running":     True,
        "current_run_id": latest["run_id"],
        "profile_name":   latest["profile_name"],
        "started_at":     latest["started_at"],
        "finished_at":    latest["finished_at"],
        "last_exit_code": latest["last_exit_code"],
        "last_error":     latest["last_error"],
        "active_count":   len(active),
    }


@app.route("/api/run/status", methods=["GET"])
def api_run_status():
    return jsonify(get_run_status_dict())


@app.route("/api/runs/active", methods=["GET"])
def api_runs_active():
    """
    All currently running slots — one entry per (run_id, profile).
    Used by Profiles page to show Start↔Stop state per row, by the
    sidebar "N running" counter, and by the new Groups page to gauge
    capacity before spawning more.
    """
    return jsonify({
        "runs":    RUNNER_POOL.active_runs(),
        "count":   RUNNER_POOL.active_count(),
    })


def _spawn_run(profile_name: str) -> dict:
    """
    Shared launch path — used by /api/run (legacy default) and
    /api/runs (explicit per-profile). Enforces:
      - one-run-per-profile rule (prevents user-data-dir corruption)
      - global max_parallel cap from config
    Returns {"ok": True, "run_id": N} on success, or raises ValueError
    with an HTTP-friendly message on cap violation.
    """
    if RUNNER_POOL.is_profile_running(profile_name):
        raise ValueError(
            f"Profile {profile_name!r} is already running — one run per "
            f"profile at a time (they'd corrupt each other's user-data-dir)"
        )

    db = get_db()

    # ── Cross-process guard ─────────────────────────────────────
    # RunnerPool only knows about runs spawned by THIS dashboard
    # instance. If dashboard was restarted while a run was alive,
    # or if another tool started main.py directly, the pool is empty
    # but Chrome/main.py might still be running. Check the DB (shared
    # source of truth) and reap any stale entries automatically.
    try:
        from process_reaper import ensure_no_live_run_for_profile
        err = ensure_no_live_run_for_profile(db, profile_name)
        if err:
            raise ValueError(err)
    except ValueError:
        raise
    except Exception as e:
        logging.warning(f"[api_run] pre-spawn guard failed (ignoring): {e}")

    max_parallel = int(db.config_get("runner.max_parallel", 4) or 4)
    if RUNNER_POOL.active_count() >= max_parallel:
        raise ValueError(
            f"Concurrent-run cap reached ({max_parallel}). Stop one "
            f"or raise runner.max_parallel in Settings."
        )

    # Effective proxy for THIS profile — respects per-profile overrides.
    proxy_cfg = db.profile_effective_proxy(profile_name)
    proxy_url = proxy_cfg["url"]

    run_id = db.run_start(profile_name, proxy_url)
    slot = RunnerSlot(run_id=run_id, profile_name=profile_name)
    RUNNER_POOL.add(slot)

    def run_thread():
        slot.log(
            f"Overпуск #{run_id} мониторинга (profile: {profile_name})...",
            "info",
        )
        # Fan out this log into the broadcast stream so the global SSE
        # subscribers still see it.
        RUNNER_POOL.broadcast_log({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": f"▶ #{run_id} {profile_name} — starting",
            "run_id": run_id,
            "profile_name": profile_name,
        })
        try:
            env = os.environ.copy()
            env["GHOST_SHELL_RUN_ID"]       = str(run_id)
            # Tell main.py which profile to run — it used to read
            # browser.profile_name from DB, but with multi-run we
            # need to override per-subprocess.
            env["GHOST_SHELL_PROFILE_NAME"] = profile_name
            # Per-profile proxy override plumbed through env so main.py
            # doesn't need to re-resolve effective config itself.
            if proxy_url:
                env["GHOST_SHELL_PROXY_URL"] = proxy_url

            proc = subprocess.Popen(
                [sys.executable, "-u", "main.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=env,
            )
            slot.process = proc

            # Record the PID in the DB so if this dashboard dies or
            # restarts while the run is active, the next startup can
            # find and kill this process tree via process_reaper.
            try:
                db.run_set_pid(run_id, proc.pid)
            except Exception as e:
                logging.warning(f"[api_run] run_set_pid failed: {e}")

            # Chrome-tree monitor — same logic as before, scoped to this slot.
            chrome_ever_seen = {"value": False}
            monitor_stop     = threading.Event()

            def _chrome_monitor():
                try:
                    import psutil
                    ps_proc = psutil.Process(proc.pid)
                except Exception:
                    return
                no_chrome_seen_at = None
                while not monitor_stop.is_set():
                    if proc.poll() is not None:
                        return
                    try:
                        children = ps_proc.children(recursive=True)
                    except Exception:
                        return
                    chrome_kids = [c for c in children
                                   if c.name().lower() == "chrome.exe"]
                    if chrome_kids:
                        chrome_ever_seen["value"] = True
                        no_chrome_seen_at = None
                    elif chrome_ever_seen["value"]:
                        if no_chrome_seen_at is None:
                            no_chrome_seen_at = time.time()
                        elif time.time() - no_chrome_seen_at > 3:
                            slot.log(
                                "Chrome window closed — stopping monitor",
                                "warning")
                            try:
                                for child in children:
                                    try:  child.terminate()
                                    except Exception: pass
                                proc.terminate()
                            except Exception:
                                pass
                            return
                    time.sleep(1)

            mon_thread = threading.Thread(target=_chrome_monitor, daemon=True)
            mon_thread.start()

            # Route stdout into this slot's log + broadcast
            try:
                for line in proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue
                    lvl = "info"
                    if "ERROR" in line:   lvl = "error"
                    elif "WARN" in line:  lvl = "warning"
                    slot.log(line, lvl)
                    RUNNER_POOL.broadcast_log({
                        "ts":      datetime.now().strftime("%H:%M:%S"),
                        "level":   lvl,
                        "message": line,
                        "run_id":  run_id,
                        "profile_name": profile_name,
                    })
            except Exception as e:
                slot.log(f"stdout reader: {e}", "debug")

            monitor_stop.set()
            proc.wait()
            exit_code = proc.returncode

            total = db.events_summary(hours=24)
            db.run_finish(
                run_id,
                exit_code    = exit_code,
                total_queries= total.get("search_ok", 0) + total.get("search_empty", 0),
                total_ads    = total.get("search_ok", 0),
                captchas     = total.get("captcha", 0),
            )
            slot.log(
                f"Monitor #{run_id} finished (code {exit_code})",
                "info" if exit_code == 0 else "error",
            )
            RUNNER_POOL.mark_finished(run_id, exit_code=exit_code)

        except Exception as e:
            db.run_finish(run_id, exit_code=-1, error=str(e))
            slot.log(f"Error запуска: {e}", "error")
            RUNNER_POOL.mark_finished(run_id, exit_code=-1, error=str(e))
        finally:
            # Keep the slot around for 60s so the UI can read the final
            # status, then GC.
            def _gc():
                time.sleep(60)
                RUNNER_POOL.remove(run_id)
            threading.Thread(target=_gc, daemon=True).start()

    t = threading.Thread(target=run_thread, daemon=True)
    slot.thread = t
    t.start()

    return {"ok": True, "run_id": run_id, "profile_name": profile_name,
            "started_at": slot.started_at}


@app.route("/api/run", methods=["POST"])
def api_run():
    """Legacy endpoint — launches the default profile (or whatever
    profile_name is in the POST body). Kept for the sidebar's "Run
    default profile" button."""
    payload = request.get_json(silent=True) or {}
    profile_name = payload.get("profile_name") or \
                   get_db().config_get("browser.profile_name", "profile_01")
    try:
        result = _spawn_run(profile_name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(result)


@app.route("/api/runs", methods=["POST"])
def api_runs_start():
    """
    Explicit multi-run endpoint — start a run for a named profile.
    Body: {profile_name: "..."}
    Returns 409 if that profile is already running or the concurrency
    cap is hit.
    """
    payload = request.get_json(silent=True) or {}
    profile_name = payload.get("profile_name")
    if not profile_name:
        return jsonify({"error": "profile_name required"}), 400
    try:
        result = _spawn_run(profile_name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify(result)


@app.route("/api/runs/<int:run_id>/stop", methods=["POST"])
def api_runs_stop_specific(run_id: int):
    """Stop one specific run by its run_id. Leaves other active runs alone."""
    slot = RUNNER_POOL.get(run_id)
    if not slot or not slot.is_running or not slot.process:
        return jsonify({"error": "Run not found or not active"}), 409

    try:
        import psutil
        parent_pid = slot.process.pid
        slot.log(
            f"Stop requested — killing process tree of PID {parent_pid}",
            "warning",
        )

        killed = []
        try:
            parent = psutil.Process(parent_pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                    killed.append(f"{child.name()}({child.pid})")
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
            killed.append(f"{parent.name()}({parent.pid})")
        except psutil.NoSuchProcess:
            pass

        try: slot.process.wait(timeout=5)
        except Exception: pass

        try:
            get_db()._get_conn().execute("""
                UPDATE runs
                SET finished_at = ?, exit_code = -99,
                    error = COALESCE(error, 'stopped by user')
                WHERE id = ? AND finished_at IS NULL
            """, (datetime.now().isoformat(timespec="seconds"), run_id))
        except Exception as e:
            logging.error(f"mark-failed on stop: {e}")

        RUNNER_POOL.mark_finished(run_id, exit_code=-99,
                                   error="stopped by user")
        slot.log(
            f"Killed: {', '.join(killed) if killed else '(nothing)'}",
            "warning",
        )
        return jsonify({"ok": True, "killed": killed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/run/stop", methods=["POST"])
def api_run_stop():
    """Legacy stop — stops the *most recent* active run. Kept so the
    old sidebar Stop button still works. Use /api/runs/<id>/stop to
    target a specific run."""
    active = RUNNER_POOL.active_runs()
    if not active:
        return jsonify({"error": "No run in progress"}), 409
    latest = max(active, key=lambda s: s["run_id"])
    return api_runs_stop_specific(latest["run_id"])


@app.route("/api/runs/stop-all", methods=["POST"])
def api_runs_stop_all():
    """Kill every active run. Used by the "Stop all" sidebar button."""
    active = RUNNER_POOL.active_runs()
    results = []
    for run in active:
        try:
            resp = api_runs_stop_specific(run["run_id"])
            results.append({
                "run_id": run["run_id"],
                "profile_name": run["profile_name"],
                "ok": True,
            })
        except Exception as e:
            results.append({
                "run_id": run["run_id"],
                "profile_name": run["profile_name"],
                "ok": False,
                "error": str(e),
            })
    return jsonify({"ok": True, "results": results, "count": len(results)})


@app.route("/api/runs/<int:run_id>/mark-failed", methods=["POST"])
def api_run_mark_failed(run_id: int):
    """Manually mark a stuck run as failed (useful for cleanup)."""
    try:
        db = get_db()
        db._get_conn().execute("""
            UPDATE runs
            SET finished_at = ?, exit_code = -99, error = ?
            WHERE id = ? AND finished_at IS NULL
        """, (
            datetime.now().isoformat(timespec="seconds"),
            "manually marked as failed via dashboard",
            run_id,
        ))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<name>", methods=["DELETE"])
def api_profile_delete(name: str):
    """
    Delete an entire profile — removes the folder AND purges DB rows for it.
    """
    try:
        import shutil
        profile_dir = os.path.join("profiles", name)
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir, ignore_errors=True)

        db = get_db()
        conn = db._get_conn()
        for table in ("events", "selfchecks", "fingerprints"):
            conn.execute(f"DELETE FROM {table} WHERE profile_name = ?", (name,))
        # Runs: don't delete (keep history), but note they belong to a deleted profile
        return jsonify({"ok": True, "deleted": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# API: SCHEDULER
# ──────────────────────────────────────────────────────────────

SCHEDULER_PID_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".scheduler.pid"
)


def _scheduler_pid_alive() -> int:
    """Return PID of running scheduler, or 0.

    Three-state resolution:
      1. No PID file → scheduler never started (or was cleanly stopped).
      2. PID file exists + PID is a live Python running scheduler.py → return PID.
      3. PID file exists but PID is dead OR belongs to another process →
         delete the stale file and return 0.

    Previously this function accepted any live Python as the scheduler,
    which meant a PID recycled by (say) the dashboard itself would report
    "already running" and block manual starts. We now check cmdline.
    """
    if not os.path.exists(SCHEDULER_PID_FILE):
        return 0
    try:
        pid = int(open(SCHEDULER_PID_FILE).read().strip())
    except Exception:
        _clear_scheduler_pid_file("unreadable")
        return 0
    try:
        import psutil
        if psutil.pid_exists(pid):
            p = psutil.Process(pid)
            name = (p.name() or "").lower()
            cmd  = " ".join(p.cmdline() or []).lower()
            # Require BOTH a Python name AND scheduler.py in cmdline.
            # This prevents a recycled PID (e.g. 12345 is now notepad.exe
            # or a child of dashboard_server) from blocking Start.
            if "python" in name and "scheduler.py" in cmd:
                return pid
            # PID is live but NOT ours — stale file from a crashed scheduler
            # whose PID got reused by another program.
            _clear_scheduler_pid_file(
                f"pid={pid} recycled to unrelated process (name={name!r})"
            )
            return 0
    except Exception as e:
        logging.debug(f"[scheduler-pid] psutil check failed: {e}")
    _clear_scheduler_pid_file("pid not alive")
    return 0


def _clear_scheduler_pid_file(reason: str):
    """Remove the stale PID file and log WHY. Makes the next Start work."""
    try:
        os.remove(SCHEDULER_PID_FILE)
        logging.info(f"[scheduler-pid] Cleared stale PID file: {reason}")
    except OSError:
        pass
    # Also clear the heartbeat config so the UI's derived-state logic
    # (which checks heartbeat freshness) doesn't keep showing "alive".
    try:
        get_db().config_set("scheduler.heartbeat_at", None)
    except Exception:
        pass


@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    if _scheduler_pid_alive():
        return jsonify({"error": "Scheduler is already running"}), 409
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "scheduler.py"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_popen_no_console_flags(),
        )
        with open(SCHEDULER_PID_FILE, "w") as f:
            f.write(str(proc.pid))
        return jsonify({"ok": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    pid = _scheduler_pid_alive()
    if not pid:
        return jsonify({"error": "Scheduler is not running"}), 409
    try:
        import psutil
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(timeout=10)
        except psutil.TimeoutExpired:
            p.kill()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    try:
        os.remove(SCHEDULER_PID_FILE)
    except OSError:
        pass
    try:
        get_db().config_set("scheduler.heartbeat_at", None)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/admin/reap-zombies", methods=["POST"])
def api_admin_reap_zombies():
    """Emergency "clean up everything" button. Force-kills any run
    with stale heartbeat, marks their DB rows as finished. Also
    clears stale scheduler PID file.

    The user might hit this from the UI when the dashboard shows
    ghost runs / profile appears locked / "already running" errors
    that they know aren't true. Idempotent — safe to spam."""
    try:
        from process_reaper import reap_stale_runs
        db = get_db()
        stats = reap_stale_runs(db, reason_prefix="manual-reap")
        # Also clear scheduler PID file if the PID isn't ours
        # (forces next Start to work even if detection is confused).
        scheduler_before = _scheduler_pid_alive()
        # Even the "alive" check side-effects a cleanup — call it again
        # to persist the clean state if the result changed.
        scheduler_after  = _scheduler_pid_alive()
        return jsonify({
            "ok":                True,
            "runs_killed":       stats["killed"],
            "runs_marked_dead":  stats["marked_finished"],
            "runs_left_alive":   stats["alive_left_alone"],
            "scheduler_before":  scheduler_before,
            "scheduler_after":   scheduler_after,
        })
    except Exception as e:
        logging.exception("reap-zombies failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduler/status", methods=["GET"])
def api_scheduler_status():
    pid = _scheduler_pid_alive()
    db = get_db()

    today = datetime.now().strftime("%Y-%m-%d")
    row = db._get_conn().execute(
        "SELECT COUNT(*) AS n FROM runs WHERE started_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    runs_today = row["n"] if row else 0

    heartbeat_at = db.config_get("scheduler.heartbeat_at")
    is_alive_heartbeat = False
    heartbeat_age = None
    if heartbeat_at:
        try:
            heartbeat_age = int(
                (datetime.now() - datetime.fromisoformat(heartbeat_at)).total_seconds()
            )
            is_alive_heartbeat = heartbeat_age < 120
        except Exception:
            pass

    # Derive a more informative health state. The UI uses this tag to
    # colour the Scheduler card: green=healthy, amber=stale, red=dead.
    if pid and is_alive_heartbeat:
        health = "ok"
    elif pid and not is_alive_heartbeat:
        # Process alive but no recent heartbeat — scheduler thread wedged.
        health = "stale"
    elif not pid and heartbeat_at and heartbeat_age is not None and heartbeat_age < 300:
        # No PID file but recent heartbeat — scheduler died uncleanly
        # (crashed without clearing DB state). The next status poll will
        # show "stopped" once the 5 min window elapses.
        health = "crashed"
    else:
        health = "stopped"

    return jsonify({
        "is_running":         bool(pid) and is_alive_heartbeat,
        "health":             health,
        "pid":                pid,
        "started_at":         db.config_get("scheduler.started_at"),
        "heartbeat_at":       heartbeat_at,
        "heartbeat_age":      heartbeat_age,
        "next_run_at":        db.config_get("scheduler.next_run_at"),
        "last_run_profile":   db.config_get("scheduler.last_run_profile"),
        "runs_today":         runs_today,
        "target_runs_per_day": db.config_get("scheduler.target_runs_per_day") or 30,
    })


# ──────────────────────────────────────────────────────────────
# API: LOGS
# ──────────────────────────────────────────────────────────────

@app.route("/api/logs/live")
def api_logs_live():
    """SSE live logs stream — merged across all active runs.

    Each entry includes run_id and profile_name so the Logs page can
    tag/filter lines by which run they came from.
    """
    def generate():
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    entry = RUNNER_POOL.broadcast_queue.get(timeout=1)
                    yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    last_heartbeat = time.time()
                except queue.Empty:
                    # Heartbeat every 15s to keep connection alive
                    if time.time() - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = time.time()
                    continue
        except GeneratorExit:
            return

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control":      "no-cache",
        "X-Accel-Buffering":  "no",
        "Connection":         "keep-alive",
    })


@app.route("/api/logs/history", methods=["GET"])
def api_logs_history():
    run_id = request.args.get("run_id", type=int)
    limit  = request.args.get("limit", default=200, type=int)
    return jsonify(get_db().logs_list(run_id=run_id, limit=limit))


# ──────────────────────────────────────────────────────────────
# API: DB TOOLS
# ──────────────────────────────────────────────────────────────

@app.route("/api/db/migrate", methods=["POST"])
def api_db_migrate():
    """Ручной запуск миграции из файлов"""
    try:
        get_db().migrate_from_files(verbose=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/db/info", methods=["GET"])
def api_db_info():
    db = get_db()
    conn = db._get_conn()
    info = {}
    for table in ("runs", "events", "competitors", "ip_history",
                  "fingerprints", "selfchecks", "config_kv", "logs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        info[table] = n
    info["db_path"] = db.path
    return jsonify(info)


# ──────────────────────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# PROFILE CREATION / FINGERPRINT MANAGEMENT
# ──────────────────────────────────────────────────────────────

@app.route("/api/profile-templates", methods=["GET"])
def api_profile_templates():
    """List available device templates for the create-profile UI.

    Returns enriched metadata the dropdown can preview (CPU cores, RAM,
    GPU model short-name, screen resolution, desktop vs laptop).
    """
    try:
        from device_templates import DEVICE_TEMPLATES
        import re

        def _extract_gpu(renderer: str) -> str:
            """Pull a short model from the ANGLE renderer string.
            Example: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 ...)'
                     → 'GeForce RTX 4060'"""
            if not renderer:
                return "Unknown GPU"
            # Strip ANGLE wrapper
            m = re.search(r"ANGLE \([^,]+,\s*([^,]+)", renderer)
            inner = m.group(1).strip() if m else renderer
            # Strip vendor prefix ("NVIDIA ", "Intel(R) ", "AMD ")
            inner = re.sub(r"^(NVIDIA|Intel\(R\)|Intel|AMD)\s+", "", inner)
            # Strip common suffixes ("Direct3D11 vs_5_0 ps_5_0", "Graphics" sometimes)
            inner = re.sub(r"\s*Direct3D.*$", "", inner)
            return inner.strip() or "Unknown GPU"

        out = []
        for t in DEVICE_TEMPLATES:
            cpu    = t.get("cpu")    or {}
            gpu    = t.get("gpu")    or {}
            screen = t.get("screen") or {}
            battery = t.get("battery")
            out.append({
                "name":        t.get("name"),
                "platform":    t.get("platform"),
                "description": t.get("description") or "",
                # Enriched fields for dropdown preview
                "cpu_cores":   cpu.get("concurrency"),
                "ram_gb":      cpu.get("memory"),
                "gpu_model":   _extract_gpu(gpu.get("gl_renderer", "")),
                "gpu_vendor":  gpu.get("webgpu_vendor"),
                "screen_w":    screen.get("width"),
                "screen_h":    screen.get("height"),
                "is_laptop":   battery is not None,
                "weight":      t.get("weight", 1),
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/fingerprint/preview", methods=["POST"])
def api_profile_preview_fingerprint():
    """
    Generate a deterministic fingerprint for (name, template, language)
    without writing anything to disk or DB. Lets the user 'preview' what
    this profile would look like before creating it.
    """
    from device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()
    template = data.get("template") or None
    language = data.get("language") or "uk-UA"
    if not name:
        return jsonify({"error": "name is required"}), 400
    if template == "auto":
        template = None
    try:
        b = DeviceTemplateBuilder(
            profile_name       = name,
            preferred_language = language,
            force_template     = template,
        )
        payload = b.generate_payload_dict()
        # Return a compact summary, not the whole 7KB JSON
        hw   = payload.get("hardware")   or {}
        gfx  = payload.get("graphics")   or {}
        scr  = payload.get("screen")     or {}
        lang = payload.get("languages")  or {}
        tz   = payload.get("timezone")   or {}
        uam  = payload.get("ua_metadata") or {}
        return jsonify({
            "ok":             True,
            "template":       payload.get("template_name"),
            "chrome_version": uam.get("full_version"),
            "platform":       hw.get("platform"),
            "user_agent":     hw.get("user_agent"),
            "cpu_cores":      hw.get("hardware_concurrency"),
            "ram_gb":         hw.get("device_memory"),
            "screen":         f"{scr.get('width')}x{scr.get('height')}",
            "pixel_ratio":    scr.get("pixel_ratio"),
            "language":       lang.get("language"),
            "languages":      lang.get("languages"),
            "timezone":       tz.get("id"),
            "gpu_vendor":     gfx.get("gl_vendor"),
            "gpu_renderer":   gfx.get("gl_renderer"),
            "fonts_count":    len(payload.get("fonts") or []),
            "plugins_count":  len(payload.get("plugins") or []),
            "webgl_exts":     len(gfx.get("webgl_extensions") or []),
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles", methods=["POST"])
def api_profile_create():
    """
    Create a new profile record. Only registers in DB and creates the
    user-data-dir placeholder. Actual fingerprint generation + chrome
    launch happens on first run.
    """
    from device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()
    template = data.get("template") or None
    language = data.get("language") or "uk-UA"
    enrich   = bool(data.get("enrich", True))

    if not name or not re.match(r"^[A-Za-z0-9_\-]+$", name):
        return jsonify({"error": "invalid name (letters, digits, _ and - only)"}), 400

    db = get_db()
    existing = db.profile_get(name) if hasattr(db, "profile_get") else None
    if existing:
        return jsonify({"error": f"profile '{name}' already exists"}), 409

    try:
        # Generate & store fingerprint
        if template == "auto":
            template = None
        builder = DeviceTemplateBuilder(
            profile_name       = name,
            preferred_language = language,
            force_template     = template,
        )
        payload = builder.generate_payload_dict()
        db.fingerprint_save(name, payload)

        # Persist profile metadata
        if hasattr(db, "profile_save"):
            db.profile_save(name, {
                "template_name":      payload.get("template_name"),
                "preferred_language": language,
                "enrich_on_create":   enrich,
                "status":             "ready",
            })

        # Create user data dir on disk
        import os as _os
        prof_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                 "profiles", name)
        _os.makedirs(prof_dir, exist_ok=True)

        return jsonify({"ok": True, "name": name,
                        "template": payload.get("template_name")})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<n>/regenerate-fingerprint", methods=["POST"])
def api_profile_regenerate_fingerprint(n):
    """
    Re-roll the fingerprint for an existing profile. Useful if current
    fingerprint is getting flagged — new seed = new UA / screen / fonts.
    Accepts optional JSON body with seed/template/language overrides.
    """
    from device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    template  = data.get("template") or None
    language  = data.get("language") or None
    new_seed  = data.get("seed_suffix")   # optional: appended to profile name

    db = get_db()
    if hasattr(db, "profile_get") and not db.profile_get(n):
        return jsonify({"error": "profile not found"}), 404

    if not language:
        prof = db.profile_get(n) if hasattr(db, "profile_get") else {}
        language = (prof or {}).get("preferred_language") or "uk-UA"

    if template == "auto":
        template = None

    try:
        # Use a variant of the profile name to get deterministic-but-different
        # fingerprint without losing the ability to re-roll deterministically.
        seed_name = n
        if new_seed:
            seed_name = f"{n}#{new_seed}"
        else:
            # Timestamp-based seed for one-shot "just give me something new"
            import time as _t
            seed_name = f"{n}#{int(_t.time())}"

        builder = DeviceTemplateBuilder(
            profile_name       = seed_name,
            preferred_language = language,
            force_template     = template,
        )
        payload = builder.generate_payload_dict()
        # Save under the ORIGINAL profile name so the new fingerprint
        # becomes the active one.
        payload["profile_name"] = n
        db.fingerprint_save(n, payload)

        # Clear cached health state — old 13/13 no longer applies
        if hasattr(db, "reset_profile_health"):
            db.reset_profile_health(n)

        return jsonify({
            "ok":            True,
            "template":      payload.get("template_name"),
            "chrome_version": (payload.get("ua_metadata") or {}).get("full_version"),
            "seed_used":     seed_name,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/profiles/<n>/clear-history", methods=["POST"])
def api_profile_clear_history_new(n):
    """
    Clear profile history. Scope selects what to clear:
      events | runs | logs | selfchecks | all
    """
    data = request.get_json(silent=True) or {}
    scope = data.get("scope", "events")
    if scope not in ("events", "runs", "logs", "selfchecks", "all"):
        return jsonify({"error": "invalid scope"}), 400

    db = get_db()
    try:
        result = db.clear_profile_history(n, scope=scope)
        return jsonify({"ok": True, "cleared": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/runs/clear", methods=["POST"])
def api_runs_clear():
    """
    Clear all run records. Accepts optional JSON body:
      { "older_than_days": 30 }   — only delete runs older than N days
    """
    data = request.get_json(silent=True) or {}
    older = data.get("older_than_days")
    if older is not None:
        try:
            older = int(older)
        except ValueError:
            return jsonify({"error": "older_than_days must be an integer"}), 400

    db = get_db()
    try:
        count = db.clear_all_runs(older_than_days=older)
        return jsonify({"ok": True, "deleted": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# PROXY DIAGNOSTICS API
# ──────────────────────────────────────────────────────────────

def _get_proxy_url() -> str | None:
    """Fetch proxy URL from DB. Normalizes to include scheme."""
    db = get_db()
    url = (db.config_get("proxy.url")
           or db.config_get("proxy.string")
           or None)
    if url and not url.startswith("http"):
        url = "http://" + url
    return url


def _fetch_exit_info(proxy_url: str, timeout: int = 12) -> dict:
    """Single exit-IP lookup via proxy. Tries ipapi.co → ipwho.is."""
    import requests
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        r = requests.get("https://ipapi.co/json/", proxies=proxies,
                         timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("ip"):
            return {
                "ok":           True,
                "ip":           d.get("ip"),
                "country":      d.get("country_name"),
                "country_code": d.get("country_code"),
                "city":         d.get("city"),
                "region":       d.get("region"),
                "timezone":     d.get("timezone"),
                "org":          d.get("org"),
                "asn":          d.get("asn"),
            }
    except Exception:
        pass
    try:
        r = requests.get("https://ipwho.is/", proxies=proxies, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("success", True) and d.get("ip"):
            return {
                "ok":           True,
                "ip":           d.get("ip"),
                "country":      d.get("country"),
                "country_code": d.get("country_code"),
                "city":         d.get("city"),
                "region":       d.get("region"),
                "timezone":     (d.get("timezone") or {}).get("id"),
                "org":          (d.get("connection") or {}).get("org"),
                "asn":          (d.get("connection") or {}).get("asn"),
            }
    except Exception:
        pass
    return {"ok": False, "error": "all geo services failed"}


@app.route("/api/proxy/current-ip", methods=["GET"])
def api_proxy_current_ip():
    """Fetch current exit IP through configured proxy."""
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400
    return jsonify(_fetch_exit_info(proxy_url))


@app.route("/api/proxy/rotate", methods=["POST"])
def api_proxy_rotate():
    """
    Force a rotation. For providers that rotate per-TCP-connection, we just
    open a fresh connection (which is what fetching exit info does). For
    providers with a rotation API, we call that first.
    """
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400

    db = get_db()
    provider = db.config_get("proxy.rotation_provider") or "none"
    api_url  = db.config_get("proxy.rotation_api_url")
    api_key  = db.config_get("proxy.rotation_api_key")
    method   = db.config_get("proxy.rotation_method") or "GET"

    rotation_called = False
    rotation_error  = None
    rotation_http   = None
    if provider != "none" and api_url:
        import requests
        try:
            headers = _build_rotation_headers(provider, api_key)
            if method.upper() == "POST":
                r = requests.post(api_url, headers=headers, timeout=10)
            else:
                r = requests.get(api_url, headers=headers, timeout=10)
            rotation_http   = r.status_code
            rotation_called = r.ok
            if not r.ok:
                rotation_error = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return jsonify({"ok": False,
                            "error": f"rotation API call failed: {e}"}), 500
        import time as _t
        _t.sleep(2)
    else:
        rotation_error = (
            "Rotation API not configured — provider="
            f"{provider}, url={'set' if api_url else 'missing'}"
        )

    info = _fetch_exit_info(proxy_url)
    info["rotation_called"] = rotation_called
    info["rotation_error"]  = rotation_error
    info["rotation_http"]   = rotation_http
    info["provider"]        = provider
    return jsonify(info)


def _build_rotation_headers(provider: str, api_key: str) -> dict:
    """
    Provider-specific header assembly. Matches the logic in
    rotating_proxy.py so the dashboard test and the runtime rotation
    behave identically.
    """
    headers = {}
    if not api_key:
        return headers
    if provider == "brightdata":
        headers["Authorization"] = f"Bearer {api_key}"
    elif provider == "asocks":
        # asocks auth is the ?apiKey=... query parameter embedded in
        # the URL — no header required, and adding one here can confuse
        # strict API validators.
        pass
    else:
        headers["X-API-Key"] = api_key
    return headers


@app.route("/api/proxy/asocks-port-list", methods=["POST"])
def api_asocks_port_list():
    """
    Fetches the user's port list from asocks using their apiKey. The
    Dashboard calls this while the user is filling in the rotation form
    so they can pick the right portId without having to dig through
    the asocks UI.

    Per https://docs.asocks.com/en/operations/941a4fb52e76050f13a0e886b08d3b6f.html
    the endpoint is GET /v2/proxy/ports?apiKey=<key>&per_page=50.

    IMPORTANT DISTINCTION for users:
      * "Port ID" in the asocks API = internal DB id (6-8 digit integer)
      * TCP port in host:port (e.g. 16720) is NOT the Port ID
    Users confuse these all the time — this endpoint disambiguates.
    """
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "API key is empty"}), 400

    import requests
    try:
        r = requests.get(
            "https://api.asocks.com/v2/proxy/ports",
            params={"apiKey": api_key, "per_page": 50},
            timeout=10,
        )
        if not r.ok:
            return jsonify({
                "ok":    False,
                "http":  r.status_code,
                "error": f"asocks returned HTTP {r.status_code}",
                "body":  r.text[:500],
            }), 200
        data = r.json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Network error: {e}"}), 200

    # asocks returns {success: true, message: {...}}. Inside message, the
    # port list may live under different keys depending on the endpoint
    # version — real shape (as of 2026) is:
    #   message.proxies: [{id, name, proxy: "host:port", login, password,
    #                      countryCode, cityName, refresh_link, ...}, ...]
    # Older versions used message.data (Laravel paginator) and even older
    # ones returned message as a flat array. Handle all three.
    items = None
    if isinstance(data, dict):
        envelope = data.get("message", data)
        if isinstance(envelope, list):
            items = envelope
        elif isinstance(envelope, dict):
            # Current shape: message.proxies
            # Laravel-paginator shape: message.data
            items = (
                envelope.get("proxies")
                or envelope.get("data")
                or envelope.get("items")
                or envelope.get("ports")
                or envelope.get("results")
            )
    else:
        items = data

    if not isinstance(items, list):
        return jsonify({
            "ok":    False,
            "error": "Unexpected response shape from asocks — no port list found",
            "body":  json.dumps(data)[:800] if isinstance(data, (dict, list)) else str(data)[:800],
        }), 200

    # Normalize each port. asocks gives us `proxy: "host:port"` as one string
    # — split it for the UI. Also pass through `refresh_link` so we can skip
    # URL assembly entirely (asocks literally hands us the full rotation URL).
    ports = []
    for p in items:
        if not isinstance(p, dict):
            continue

        # Split "109.236.84.23:16720" into host + port
        host = p.get("host") or p.get("server") or p.get("ip")
        port = p.get("port") or p.get("external_port")
        proxy_str = p.get("proxy")
        if proxy_str and (not host or not port):
            try:
                h, prt = proxy_str.rsplit(":", 1)
                host = host or h
                port = port or int(prt)
            except Exception:
                pass

        # Country: prefer full name, fall back to code
        country = None
        c = p.get("country")
        if isinstance(c, dict):
            country = c.get("name") or c.get("code")
        elif isinstance(c, str):
            country = c
        if not country:
            country = p.get("countryCode") or p.get("country_code")

        ports.append({
            "id":           p.get("id") or p.get("port_id") or p.get("portId"),
            "name":         p.get("name") or p.get("title"),
            "host":         host,
            "port":         port,
            "login":        p.get("login") or p.get("username") or p.get("user"),
            "country":      country,
            "city":         p.get("cityName") or p.get("city"),
            # asocks hands us a pre-signed rotation URL — pass it through
            # so the UI can use it verbatim instead of rebuilding from scratch.
            "refresh_link": p.get("refresh_link"),
            "active":       p.get("status", 1) == 1 if "status" in p else p.get("active", True),
        })
    return jsonify({"ok": True, "ports": ports, "count": len(ports)})


@app.route("/api/proxy/test-rotation-api", methods=["POST"])
def api_proxy_test_rotation_api():
    """
    Ping the configured rotation API URL without actually caring about
    the exit IP. Returns the HTTP status + response snippet so the user
    can verify their asocks/brightdata URL is live.
    """
    db = get_db()
    provider = db.config_get("proxy.rotation_provider") or "none"
    api_url  = db.config_get("proxy.rotation_api_url")
    api_key  = db.config_get("proxy.rotation_api_key")
    method   = (db.config_get("proxy.rotation_method") or "GET").upper()

    if provider == "none" or not api_url:
        return jsonify({
            "ok": False,
            "status": "unconfigured",
            "message": (
                f"Provider is {provider!r} and URL is "
                f"{'set' if api_url else 'empty'}. "
                "Pick a provider and paste the rotation URL first."
            ),
        })

    import requests
    try:
        headers = _build_rotation_headers(provider, api_key)
        kwargs = {"headers": headers, "timeout": 10}
        r = requests.post(api_url, **kwargs) if method == "POST" \
            else requests.get(api_url, **kwargs)
        return jsonify({
            "ok":       r.ok,
            "status":   "ok" if r.ok else "error",
            "http":     r.status_code,
            "provider": provider,
            "method":   method,
            "body":     r.text[:500],
            "message": (
                f"✓ HTTP {r.status_code} — rotation API is working"
                if r.ok else
                f"✗ HTTP {r.status_code} — check URL and credentials"
            ),
        })
    except Exception as e:
        return jsonify({
            "ok":       False,
            "status":   "network-error",
            "provider": provider,
            "message":  f"✗ Network error: {e}",
        })


@app.route("/api/proxy/test-rotation", methods=["POST"])
def api_proxy_test_rotation():
    """Run N rotation tests. Returns exit IPs + country summary."""
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400

    payload = request.get_json(silent=True) or {}
    n = int(payload.get("count", 10))
    n = max(1, min(30, n))

    import time as _t
    results = []
    for _ in range(n):
        results.append(_fetch_exit_info(proxy_url, timeout=15))
        _t.sleep(1.5)

    countries = {}
    unique_ips = set()
    for r in results:
        if r.get("ok"):
            unique_ips.add(r.get("ip"))
            c = r.get("country") or "?"
            countries[c] = countries.get(c, 0) + 1

    return jsonify({
        "ok":         True,
        "results":    results,
        "unique_ips": len(unique_ips),
        "countries":  countries,
        "total":      n,
    })


@app.route("/api/proxy/full-diagnostics", methods=["POST"])
def api_proxy_full_diagnostics():
    """
    Full proxy health report: IP info, geo match, timezone match,
    ASN-based reputation hint.
    """
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return jsonify({"ok": False, "error": "no proxy configured"}), 400

    db = get_db()
    expected_country  = db.config_get("browser.expected_country")  or "Ukraine"
    expected_timezone = db.config_get("browser.expected_timezone") or "Europe/Kyiv"

    info = _fetch_exit_info(proxy_url)
    if not info.get("ok"):
        return jsonify({"ok": False,
                        "error": info.get("error", "IP lookup failed")}), 502

    actual_country = (info.get("country") or "").strip()
    geo_match = True
    if expected_country and actual_country:
        exp_lc = expected_country.strip().lower()
        act_lc = actual_country.lower()
        geo_match = (exp_lc in act_lc or act_lc in exp_lc)

    actual_tz = info.get("timezone")
    tz_aliases = {"Europe/Kiev": "Europe/Kyiv", "Europe/Uzhgorod": "Europe/Kyiv"}
    normalized_actual   = tz_aliases.get(actual_tz, actual_tz)
    normalized_expected = tz_aliases.get(expected_timezone, expected_timezone)
    tz_match = (normalized_actual == normalized_expected)

    org = (info.get("org") or "").lower()
    datacenter_markers = [
        "amazon", "aws", "google cloud", "microsoft", "digitalocean", "ovh",
        "hetzner", "linode", "vultr", "datacamp", "datacenter", "hosting",
    ]
    mobile_markers = ["kyivstar", "lifecell", "vodafone", "mts", "mobile"]
    ip_type = "unknown"
    if any(m in org for m in datacenter_markers):
        ip_type = "datacenter"
    elif any(m in org for m in mobile_markers):
        ip_type = "mobile"
    elif org and ("llc" in org or "ltd" in org):
        ip_type = "residential"

    risk = {"datacenter": "high", "mobile": "low",
            "residential": "low", "unknown": "medium"}.get(ip_type, "medium")

    return jsonify({
        "ok":                True,
        "ip":                info,
        "expected_country":  expected_country,
        "actual_country":    actual_country,
        "geo_match":         geo_match,
        "expected_timezone": expected_timezone,
        "actual_timezone":   actual_tz,
        "tz_match":          tz_match,
        "ip_type":           ip_type,
        "detection_risk":    risk,
    })


# ──────────────────────────────────────────────────────────────
# ACTION PIPELINES API
# ──────────────────────────────────────────────────────────────

@app.route("/api/actions/catalog", methods=["GET"])
def api_actions_catalog():
    """List all supported action types with their params for UI builder."""
    try:
        from action_runner import action_catalog, action_common_params
        return jsonify({
            "types":         action_catalog(),
            "common_params": action_common_params(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/actions/pipelines", methods=["GET"])
def api_actions_pipelines_get():
    """
    Return both pipelines used by main.py:
      - post_ad_actions          (competitor ads)
      - on_target_domain_actions (your own brand's ads, if shown)

    Defensive read: if the value came back as a JSON string (can happen
    after certain legacy import paths), parse it. If parsing fails or
    the value isn't a list, return [] instead of choking the UI with
    `No parameters` ghost-steps.
    """
    db = get_db()

    def _as_list(key):
        raw = db.config_get(key)
        if raw is None:
            return []
        # Sometimes imports stash strings — unwrap one level
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                logging.warning(
                    f"[pipelines] {key} is an unparseable string, resetting"
                )
                return []
        if not isinstance(raw, list):
            logging.warning(
                f"[pipelines] {key} is not a list (type={type(raw).__name__}), resetting"
            )
            return []
        # Filter out malformed steps: missing `type` or not a dict
        clean = []
        for i, step in enumerate(raw):
            if not isinstance(step, dict):
                logging.warning(f"[pipelines] {key}[{i}] skipped (not a dict)")
                continue
            if not step.get("type"):
                logging.warning(f"[pipelines] {key}[{i}] skipped (no type field)")
                continue

            # Auto-migrate legacy `search_all_queries` → `loop` with
            # items_from="queries". Keeps existing configs working after
            # the Scripts refactor (Apr 2026).
            if step.get("type") == "search_all_queries":
                step = {
                    "type":       "loop",
                    "enabled":    step.get("enabled", True),
                    "items_from": "queries",
                    "item_var":   "query",
                    "shuffle":    step.get("shuffle", True),
                    "steps": [
                        {"type": "search_query", "query": "{query}"},
                    ],
                }
                logging.info(
                    f"[pipelines] {key}[{i}] migrated search_all_queries → loop"
                )

            clean.append(step)
        return clean

    return jsonify({
        "main_script":              _as_list("actions.main_script"),
        "post_ad_actions":          _as_list("actions.post_ad_actions"),
        "on_target_domain_actions": _as_list("actions.on_target_domain_actions"),
    })


@app.route("/api/actions/pipelines", methods=["POST"])
def api_actions_pipelines_save():
    """
    Save one, two, or all three pipelines. Body:
      { "main_script":              [...],
        "post_ad_actions":          [...],
        "on_target_domain_actions": [...] }
    Any key is optional; omitted keys are left untouched.
    """
    data = request.get_json(silent=True) or {}
    db = get_db()
    saved = {}

    for key in ("main_script", "post_ad_actions", "on_target_domain_actions"):
        if key in data:
            pipeline = data[key]
            if not isinstance(pipeline, list):
                return jsonify({"error": f"{key} must be a list"}), 400
            # Light validation — every item must have `type`
            for i, step in enumerate(pipeline):
                if not isinstance(step, dict) or "type" not in step:
                    return jsonify({
                        "error": f"{key}[{i}] must be a dict with 'type'"
                    }), 400
            db.config_set(f"actions.{key}", pipeline)
            saved[key] = len(pipeline)

    return jsonify({"ok": True, "saved": saved})


# ──────────────────────────────────────────────────────────────
# API: CONFIGURATION EXPORT / IMPORT
# ──────────────────────────────────────────────────────────────
#
# The export bundles every *configuration* table — stuff the user has
# tweaked via the dashboard — into one JSON file. The bundle EXCLUDES
# history tables (runs, events, logs, competitors, ip_history,
# action_events, selfchecks, fingerprints) since those are per-deployment
# and usually noise when copying a setup between machines.
#
# Format:
#   {
#     "format_version": 1,
#     "exported_at":    "2026-04-22T22:15:00",
#     "app_version":    "ghost-shell-1.0",
#     "config":         { "key": value, ... },            # config_kv
#     "profiles":       [ {...}, ... ],                   # profiles metadata
#     "action_pipelines": {
#         "post_ad_actions":         [ ... ],
#         "on_target_domain_actions": [ ... ],
#     }
#   }

EXPORT_FORMAT_VERSION = 1
# Keys stored in config_kv that are machine-specific and should NOT be
# moved between installations. Proxy credentials, for instance, are
# typically different on each host.
_EXPORT_SKIP_KEYS = {
    "proxy.total_rotations",
    "proxy.last_rotation_at",
    "system.first_run_at",
}


@app.route("/api/export-config", methods=["GET"])
def api_export_config():
    """Download the full dashboard configuration as JSON."""
    db = get_db()
    try:
        from datetime import datetime

        # ── CRITICAL: read FLAT keys directly from SQLite, NOT the
        # nested dict from config_get_all(). The nested form
        # ({"proxy": {"url": ...}, ...}) round-trips incorrectly on
        # import: iterating over the top-level `proxy` key and calling
        # config_set("proxy", {...}) would write the whole object under
        # a flat "proxy" key in config_kv, breaking lookups like
        # config_get("proxy.url").
        rows = db._get_conn().execute(
            "SELECT key, value FROM config_kv"
        ).fetchall()
        flat_config = {}
        for row in rows:
            key = row["key"]
            if key in _EXPORT_SKIP_KEYS:
                continue
            try:
                flat_config[key] = json.loads(row["value"])
            except Exception:
                flat_config[key] = row["value"]

        # Pull out the three action pipelines separately for clarity
        # (they stay duplicated in `config` too — exactly what config_kv has).
        action_pipelines = {
            "main_script":              flat_config.get("actions.main_script", []),
            "post_ad_actions":          flat_config.get("actions.post_ad_actions", []),
            "on_target_domain_actions": flat_config.get("actions.on_target_domain_actions", []),
        }

        # Profiles — just the on-disk config, no per-run data
        profiles = db.profiles_list()
        # profiles_list returns dicts with heavy fields — keep only the
        # deterministic ones. Anything with "last_", "total_", "session_"
        # prefix is runtime state.
        slim_profiles = []
        for p in profiles:
            slim_profiles.append({
                k: v for k, v in p.items()
                if not (k.startswith("last_") or k.startswith("total_")
                        or k.startswith("session_") or k.startswith("recent_"))
            })

        bundle = {
            "format_version":   EXPORT_FORMAT_VERSION,
            "exported_at":      datetime.now().isoformat(timespec="seconds"),
            "app_version":      "ghost-shell-1.0",
            # config is a FLAT dict of dotted keys — one-to-one with
            # SQLite's config_kv rows. Imports read this back as-is.
            "config":           flat_config,
            "profiles":         slim_profiles,
            "action_pipelines": action_pipelines,
        }

        # Content-Disposition makes the browser download it as a file
        from flask import Response
        filename = f"ghost-shell-config-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            json.dumps(bundle, indent=2, ensure_ascii=False),
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    except Exception as e:
        logging.error(f"export-config failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/import-config", methods=["POST"])
def api_import_config():
    """Replace current config with an uploaded bundle.

    Accepts JSON body `{ "bundle": {...}, "mode": "merge"|"replace" }`.
      - merge   (default): union config dicts, existing keys overwritten
                           by bundle values; profiles/pipelines upserted
      - replace: dangerous — wipes existing config_kv before importing

    Handles two `config` formats:
      (a) flat: {"proxy.url": "...", "search.queries": [...]} — new format
      (b) nested: {"proxy": {"url": "..."}, ...} — produced by the buggy
          v1 exporter that round-tripped through config_get_all(). We
          flatten it on the fly so old bundles still import correctly.
    """
    try:
        data = request.get_json(force=True) or {}
        bundle = data.get("bundle") or {}
        mode = data.get("mode", "merge")

        if not isinstance(bundle, dict):
            return jsonify({"error": "bundle must be an object"}), 400
        if bundle.get("format_version") != EXPORT_FORMAT_VERSION:
            return jsonify({
                "error": f"format_version mismatch: expected {EXPORT_FORMAT_VERSION}, "
                         f"got {bundle.get('format_version')!r}. "
                         f"This bundle was made by a different Ghost Shell version."
            }), 400

        db   = get_db()
        conn = db._get_conn()
        stats = {"config_keys": 0, "profiles": 0, "pipelines": 0}

        # Remove legacy "mush" keys before any writes — these are the
        # broken top-level dicts that the old buggy exporter produced
        # (e.g. key="proxy", value='{"url": "..."}'). They shadow the
        # correct dotted keys like "proxy.url" at read time.
        conn.execute("""
            DELETE FROM config_kv
            WHERE key NOT LIKE '%.%'
              AND key NOT IN ('_schema_version', '_last_migration')
        """)

        if mode == "replace":
            # Keep skip-keys (machine-local) but wipe everything else
            conn.execute("""
                DELETE FROM config_kv
                WHERE key NOT IN (%s)
            """ % ",".join("?" * len(_EXPORT_SKIP_KEYS)),
                tuple(_EXPORT_SKIP_KEYS))

        # ── Config keys ──
        cfg_in = bundle.get("config") or {}

        # Detect legacy nested format and flatten it. Heuristic: if any
        # top-level key has NO dot AND its value is a dict, it's nested.
        is_nested = any(
            "." not in k and isinstance(v, dict)
            for k, v in cfg_in.items()
        )
        if is_nested:
            def _flatten(d: dict, prefix: str = "") -> dict:
                flat = {}
                for k, v in d.items():
                    key = f"{prefix}.{k}" if prefix else k
                    if isinstance(v, dict):
                        flat.update(_flatten(v, key))
                    else:
                        flat[key] = v
                return flat
            cfg_in = _flatten(cfg_in)
            logging.info(
                f"[import-config] detected legacy nested bundle — "
                f"flattened to {len(cfg_in)} dotted keys"
            )

        for key, value in cfg_in.items():
            if key in _EXPORT_SKIP_KEYS:
                continue   # never import machine-specific keys
            # Sanity — only accept dotted keys, refuse top-level junk
            # that would re-introduce the mush bug.
            if "." not in key:
                logging.warning(
                    f"[import-config] skipping non-dotted key {key!r}"
                )
                continue
            db.config_set(key, value)
            stats["config_keys"] += 1

        # ── Profiles ── (upsert)
        for p in (bundle.get("profiles") or []):
            if not isinstance(p, dict) or not p.get("name"):
                continue
            try:
                name = p["name"]
                meta = {k: v for k, v in p.items() if k != "name"}
                db.profile_save(name, meta)
                stats["profiles"] += 1
            except Exception as e:
                logging.debug(f"profile_save {p.get('name')}: {e}")

        # ── Action pipelines ──
        ap = bundle.get("action_pipelines") or {}
        for key in ("main_script", "post_ad_actions", "on_target_domain_actions"):
            if key in ap and isinstance(ap[key], list):
                db.config_set(f"actions.{key}", ap[key])
                stats["pipelines"] += 1

        conn.commit()
        return jsonify({"ok": True, "mode": mode, "imported": stats})

    except Exception as e:
        logging.error(f"import-config failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def cleanup_orphan_config_keys():
    """
    One-shot cleanup for a bug in early export/import (format_version=1):
    the buggy exporter round-tripped config through the nested dict,
    and on import it wrote values like {"proxy.url": "..."} as a single
    row with key="proxy" and value='{"url":"..."}'.

    These "mush" keys shadow the correct dotted keys (`proxy.url`,
    `search.queries`, etc.) in config_get_all()'s nested output because
    the nested builder processes them in row-order and the last write
    wins.

    This function removes any top-level (non-dotted) key in config_kv
    that's NOT an internal schema marker. Safe to run on every startup
    — a correctly-seeded DB has zero non-dotted keys.
    """
    try:
        db = get_db()
        conn = db._get_conn()
        rows = conn.execute("""
            SELECT key FROM config_kv
            WHERE key NOT LIKE '%.%'
              AND key NOT IN ('_schema_version', '_last_migration')
        """).fetchall()
        if rows:
            deleted = [r["key"] for r in rows]
            conn.execute("""
                DELETE FROM config_kv
                WHERE key NOT LIKE '%.%'
                  AND key NOT IN ('_schema_version', '_last_migration')
            """)
            conn.commit()
            logging.warning(
                f"[dashboard] cleaned up {len(deleted)} orphan config_kv keys "
                f"(probably from a broken v1 import): {deleted!r}. "
                f"If anything looks unconfigured, re-enter values in the UI."
            )
    except Exception as e:
        logging.debug(f"cleanup_orphan_config_keys failed: {e}")


if __name__ == "__main__":
    # Auto-migration from legacy files
    get_db().migrate_from_files(verbose=True)
    # Clean up stale runs left from previous crashes
    cleanup_stale_runs()
    # One-shot fix for broken v1 import/export (see function docstring)
    cleanup_orphan_config_keys()
    # Traffic-stats retention cleanup — deletes rows older than
    # traffic.retention_days (default 90). Runs once at startup; the
    # background traffic collectors don't need a periodic task beyond
    # this because writes are bounded (~50 rows/hour per profile).
    try:
        db = get_db()
        retention = int(db.config_get("traffic.retention_days") or 90)
        if retention > 0:
            deleted = db.traffic_cleanup(retention_days=retention)
            if deleted > 0:
                print(f"[startup] Cleaned up {deleted} traffic rows older than "
                      f"{retention} days")
    except Exception as e:
        print(f"[startup] traffic cleanup skipped: {e}")

    port = int(os.environ.get("PORT", 5000))
    url  = f"http://127.0.0.1:{port}"
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Ghost Shell Dashboard                                    ║")
    print(f"║  → {url:<55}║")
    print("║  Ctrl+C to stop                                           ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    # Auto-open the dashboard in the user's default browser.
    # Disable via env: GHOST_SHELL_NO_BROWSER=1 (useful for headless hosts).
    if os.environ.get("GHOST_SHELL_NO_BROWSER") != "1":
        import webbrowser, threading, time as _t
        def _open_after_ready():
            # Wait briefly so Flask is actually listening before we hit it
            _t.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception as e:
                print(f"[warn] couldn't auto-open browser: {e}")
        threading.Thread(target=_open_after_ready, daemon=True).start()

    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
