"""
dashboard_server.py — Flask server with DB backend

All data is read/written via db.py. No files other than ghost_shell.db
(plus payload_debug.json in the profile for the C++ core).

Run:
    python dashboard_server.py
    → http://127.0.0.1:5000
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
from ghost_shell.core.platform_paths import PROJECT_ROOT
import re
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

from ghost_shell.db.database import get_db
from ghost_shell.core.platform_paths import popen_flags_no_console, terminate_process_tree
from ghost_shell.core import runtime as gs_runtime


# ──────────────────────────────────────────────────────────────
# In-memory shutdown token
#
# Set by main() right before app.run() — so we know the value to
# accept on POST /api/admin/shutdown. The full record is also written
# to runtime.json so the installer can read it back without us being
# alive. Stored in-memory too because reading runtime.json on every
# shutdown request would race against installer-side cleanup.
# ──────────────────────────────────────────────────────────────
_SHUTDOWN_TOKEN: Optional[str] = None


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
        # Pub/sub broadcast — each SSE subscriber gets its own queue.
        # Previously there was ONE queue that every subscriber pulled
        # from, which meant two open tabs would see each log message
        # alternating between them (Queue.get() removes the item).
        # Now broadcast_log() fans out to every subscriber's queue.
        self._subscribers    = set()   # set of queue.Queue
        self._subs_lock      = threading.Lock()

        # Server-side ring buffer of recent log entries. Serves two
        # purposes that SSE alone can't:
        #
        #   1. Page-reload replay — when the user refreshes the Logs
        #      tab, SSE starts a new connection that only sees FUTURE
        #      messages. Without this buffer the page would be empty
        #      until the next log line arrives (could be minutes on a
        #      quiet scheduler). Now the frontend fetches this buffer
        #      on init and renders it as "history", then SSE picks up
        #      from whatever arrived after.
        #
        #   2. Late-joiners — if a user opens the dashboard mid-run
        #      they should see what's been happening, not just
        #      whatever fires next.
        #
        # Size budget: 2000 entries, ~250 bytes each ≈ 500 KB. Larger
        # than the frontend's LOG_MAX (500) because some scheduler
        # setups do bursts and we want to retain context across them.
        # Ring buffer (deque with maxlen) auto-evicts oldest.
        from collections import deque
        self._log_history      = deque(maxlen=2000)
        self._log_history_lock = threading.Lock()

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

    # ── Broadcast logging (fan-out to every SSE subscriber) ─────
    def subscribe(self) -> queue.Queue:
        """Called by an SSE endpoint to open its own pipe. Caller MUST
        call unsubscribe() (or guarantee the queue goes out of scope)
        when done, otherwise we leak one queue per disconnected client."""
        q = queue.Queue(maxsize=1000)
        with self._subs_lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._subs_lock:
            self._subscribers.discard(q)

    def broadcast_log(self, entry: dict) -> None:
        # Stamp with a monotonic sequence number so clients can
        # request "everything after this seq" after a reconnect
        # without worrying about clock skew or duplicate replay.
        with self._log_history_lock:
            self._seq_counter = getattr(self, "_seq_counter", 0) + 1
            entry = dict(entry)   # copy so we don't mutate the caller's dict
            entry["seq"] = self._seq_counter
            self._log_history.append(entry)

        # Fan out to a snapshot of subscribers — capture under the lock
        # so adding/removing subscribers during iteration is safe.
        with self._subs_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(entry)
            except queue.Full:
                # Drop oldest so slow subscribers don't starve others
                try:
                    q.get_nowait()
                    q.put_nowait(entry)
                except Exception:
                    pass

    def recent_logs(self, limit: int = 2000,
                    profile_name: str = None,
                    level: str = None,
                    since_id: int = None) -> list[dict]:
        """Snapshot of the ring buffer, optionally filtered.

        `since_id` is the sequence number of the last entry the caller
        already has — returns only entries newer than that. Useful when
        the SSE reconnects after a transient disconnect and wants to
        backfill the gap without replaying everything.
        """
        with self._log_history_lock:
            entries = list(self._log_history)
        # Filters applied in Python because the buffer is small (2000)
        # and this is called once per page load — not worth indexing.
        if since_id is not None:
            entries = [e for e in entries if e.get("seq", 0) > since_id]
        if profile_name:
            entries = [e for e in entries if e.get("profile_name") == profile_name]
        if level:
            entries = [e for e in entries if e.get("level") == level]
        if limit and len(entries) > limit:
            entries = entries[-limit:]   # keep most recent N
        return entries

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
        from ghost_shell.core.process_reaper import reap_stale_runs
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

# static_folder must be absolute after the package refactor — when
# server.py lived at the project root, "dashboard" resolved relative
# to it. Now it lives in ghost_shell/dashboard/, so relative lookup
# fails and every /css/ /js/ /favicon.* returns 404.
app = Flask(
    __name__,
    static_folder=os.path.join(PROJECT_ROOT, "dashboard"),
    static_url_path="",
)
CORS(app)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.route("/")
def index():
    """Serve the dashboard SPA entry page."""
    html_path = os.path.join(PROJECT_ROOT, "dashboard", "index.html")
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

    # HEADLINE STATS — sourced from the runs table, NOT the events
    # table. The runs table is written authoritatively by run_finish()
    # at the end of every run. The events table is optional telemetry
    # written by session_quality.py — when session_quality writes fail
    # (captcha-only runs, early errors, log-encoding issues), events
    # stayed empty and the Overview appeared frozen. By totalling runs
    # directly we show numbers that always match what actually happened.
    totals_all = db.runs_totals()               # all-time
    # Keep events_summary for "blocked" and other niche counters that
    # don't have a runs column. The key-name mapping below maps legacy
    # frontend field names to the new data shape.
    events_summary = db.events_summary(hours=24 * 365)

    total_comp, unique_domains = db.competitors_count()
    all_profiles = db.profiles_list()
    active_profiles = db.active_profiles_count(days=7)

    # Post-click action counters — rolled up from action_events
    actions_24h = db.action_events_summary(hours=24)
    actions_all = db.action_events_summary(hours=24 * 365)

    # Build info — which Chromium / Chrome versions ship with this binary
    try:
        from ghost_shell.fingerprint.device_templates import CHROMIUM_BUILD, CHROMIUM_BUILD_FULL, CHROME_VERSIONS
        stable_chrome = CHROME_VERSIONS[0]["major"] if CHROME_VERSIONS else "?"
        spoof_min = db.config_get("browser.spoof_chrome_min") or None
        spoof_max = db.config_get("browser.spoof_chrome_max") or None
        build_info = {
            "chromium_build":      CHROMIUM_BUILD,
            "chromium_build_full": CHROMIUM_BUILD_FULL,
            "chrome_spoof":        stable_chrome,
            "chrome_pool":         [v["major"] for v in CHROME_VERSIONS],
            "chrome_pool_full":    [v["full"]  for v in CHROME_VERSIONS],
            "spoof_min":           spoof_min,
            "spoof_max":           spoof_max,
        }
    except Exception:
        build_info = {}

    return jsonify({
        "total_profiles":    len(all_profiles),     # all profiles ever seen
        "active_profiles":   active_profiles,       # distinct profiles with a run in last 7d
        # Run-table-sourced headline counters (authoritative)
        "total_searches":    totals_all["searches"],
        "total_ads":         totals_all["ads"],
        "total_captchas":    totals_all["captchas"],
        "total_runs":        totals_all["runs"],
        "runs_completed":    totals_all["completed"],
        "runs_failed":       totals_all["failed"],
        # Events-sourced (telemetry — may be missing if sqm failed)
        "total_blocks":      events_summary.get("blocked", 0),
        # Competitor data — independent table, unaffected by events issue
        "total_competitors": total_comp,
        "unique_domains":    unique_domains,
        "daily":             db.daily_stats(days=14),
        "run_status":        get_run_status_dict(),
        "actions_24h":       actions_24h,
        "actions_total":     actions_all,
        "build_info":        build_info,
    })


# ──────────────────────────────────────────────────────────────
# API: TRAFFIC STATS (aggregated by profile × domain × hour)
# ──────────────────────────────────────────────────────────────

@app.route("/api/stats/reset", methods=["POST"])
def api_stats_reset():
    """Nuke all run history, events, competitors, traffic, and IP-health
    counters — the "wipe stats and start fresh" button on Overview.

    What this KEEPS (deliberately):
      - Config values (proxy settings, behavior toggles, queries)
      - `profiles` table rows (tags, notes, per-profile proxy overrides)
      - Fingerprints (expensive to regen; stale fingerprints don't affect stats)
      - On-disk profile folders (cookies, local storage, history)
        — per-profile "Clear history" buttons handle those separately.
      - Action events scripts, schedules, etc.

    What this CLEARS:
      - runs                — run history + per-run counters
      - events              — search_ok / search_empty / captcha telemetry
      - competitors         — collected ad URLs + per-query matches
      - traffic_samples     — bandwidth/domain aggregates
      - ip_history          — per-IP health (captcha count, burn state)
      - selfchecks          — cached fingerprint validation results
      - action_events       — post-click action telemetry

    Refuses while any run is active — would corrupt the active run's
    DB rows mid-flight.
    """
    # Guard: active runs. Deleting `runs` rows under a live process
    # means the process's run_finish() at the end writes an UPDATE that
    # affects 0 rows (no-op), and the active_runs() list goes stale.
    active = RUNNER_POOL.active_runs()
    if active:
        names = ", ".join(r["profile_name"] for r in active)
        return jsonify({
            "error": f"Can't reset stats while runs are active: {names}. "
                     f"Stop them first, then reset."
        }), 409

    db = get_db()
    conn = db._get_conn()

    # Tables to truncate. Order doesn't matter — no foreign keys between
    # these in the current schema — but we list them grouped by purpose
    # for easier audit.
    tables_cleared = []
    targets = [
        # Run / query telemetry
        "runs", "events", "selfchecks", "action_events",
        # What we collected
        "competitors",
        # Network/IP hygiene
        "traffic_samples", "ip_history",
    ]
    errors = []
    for t in targets:
        try:
            # Check table exists — an older DB might not have all of them.
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)
            ).fetchone()
            if not row:
                continue
            cnt = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            conn.execute(f"DELETE FROM {t}")
            tables_cleared.append({"table": t, "rows_deleted": cnt})
        except Exception as e:
            errors.append({"table": t, "error": str(e)})
            logging.warning(f"[reset stats] {t}: {e}")

    # VACUUM reclaims the freed pages back to the OS — on a heavily-used
    # db with millions of events this can save hundreds of MB. Safe
    # because we've verified no active writers above.
    try:
        conn.execute("VACUUM")
    except Exception as e:
        logging.debug(f"[reset stats] VACUUM: {e}")

    total_rows = sum(t["rows_deleted"] for t in tables_cleared)
    logging.info(
        f"[reset stats] cleared {total_rows} rows across "
        f"{len(tables_cleared)} tables"
    )

    return jsonify({
        "ok":           True,
        "tables":       tables_cleared,
        "total_rows":   total_rows,
        "errors":       errors,
    })


@app.route("/api/admin/health", methods=["GET"])
def api_admin_health():
    """Liveness probe — used by the installer to wait until the new server
    has come back up after an update, and by tooling to confirm the dashboard
    is reachable. Public, no token: returning {"ok": true} is harmless."""
    return jsonify({
        "ok":      True,
        "pid":     os.getpid(),
        "version": gs_runtime._read_version_safe(),
    })


@app.route("/api/admin/shutdown", methods=["POST"])
def api_admin_shutdown():
    """Graceful shutdown endpoint, called by the installer before it
    replaces files during an update.

    Security:
      • Loopback-only — refuses anything that isn't 127.0.0.1 / ::1
      • Token-gated   — requires X-Shutdown-Token header matching the
                        one written to runtime.json at startup. This
                        prevents a hostile webpage running in the user's
                        regular browser from killing the server via fetch().

    Behaviour:
      1. Validate origin + token. Reject with 403 on mismatch.
      2. Write a "going down" flag, return 200 immediately.
      3. Schedule actual exit on a background thread so Flask can
         finish flushing the response to the caller before we die.
    """
    # Belt: reject anything not from the loopback adapter. Werkzeug puts
    # the peer IP in request.remote_addr regardless of any X-Forwarded-For
    # we might choose to honour elsewhere.
    addr = request.remote_addr or ""
    if addr not in ("127.0.0.1", "::1", "localhost"):
        return jsonify({"error": "shutdown only allowed from loopback"}), 403

    # Suspenders: token must match the one we wrote at startup. Constant-time
    # compare in case a future caller leaks timing info via remote logs.
    token = request.headers.get("X-Shutdown-Token", "")
    expected = _SHUTDOWN_TOKEN or ""
    import hmac as _hmac
    if not expected or not _hmac.compare_digest(token, expected):
        return jsonify({"error": "invalid shutdown token"}), 403

    # Optional grace period — installer can request "wait N seconds before
    # actually exiting" so the human user has time to see the toast in the
    # dashboard. Capped to keep installer flows snappy.
    try:
        grace = float((request.get_json(silent=True) or {}).get("grace", 0.5))
    except (TypeError, ValueError):
        grace = 0.5
    grace = max(0.0, min(grace, 5.0))

    logging.info(f"[shutdown] received from {addr}, exiting in {grace}s")

    def _delayed_exit():
        # Sleep on a daemon thread so we don't block the response.
        try:
            time.sleep(grace)
        finally:
            try:
                gs_runtime.clear_runtime_info()
            except Exception:
                pass
            # os._exit bypasses Flask's clean-up hooks, but those are not
            # required here — the installer is about to overwrite files
            # underneath us anyway. SystemExit / sys.exit() inside a
            # request handler is intercepted by Werkzeug, which is why
            # we pick the harder hammer.
            os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return jsonify({"ok": True, "exit_in_sec": grace, "pid": os.getpid()})


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
        return jsonify({"error": "No selfcheck data yet. Run a monitor pass first."}), 404
    return jsonify(sc)


@app.route("/api/profiles/<name>/selfcheck/history", methods=["GET"])
def api_profile_selfcheck_history(name: str):
    return jsonify(get_db().selfchecks_history(name, limit=20))


@app.route("/api/profiles/<name>/fingerprint", methods=["GET"])
def api_profile_fingerprint(name: str):
    fp = get_db().fingerprint_current(name)
    if not fp:
        return jsonify({"error": "no fingerprint stored for this profile"}), 404
    return jsonify(fp["payload"])


@app.route("/api/profiles/<name>/reset-health", methods=["POST"])
def api_profile_reset_health(name: str):
    """Reset consecutive blocks marker — use after fixing the root cause."""
    try:
        path = os.path.join("profiles", name)
        if not os.path.exists(path):
            return jsonify({"error": f"Profile {name} not found"}), 404
        from ghost_shell.session.quality import SessionQualityMonitor
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
        from ghost_shell.session.quality import SessionQualityMonitor
        sqm = SessionQualityMonitor(path)
        sqm.clear()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<name>/meta", methods=["GET"])
def api_profile_meta_get(name: str):
    """Dashboard-level metadata for a profile: tags, per-profile proxy
    override, rotation config override, notes, group memberships."""
    meta = get_db().profile_meta_get(name)
    return jsonify(meta)


@app.route("/api/profiles/<name>/meta", methods=["POST"])
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


@app.route("/api/profiles/<name>/tags", methods=["POST"])
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

@app.route("/api/profiles/<name>/cookies", methods=["GET"])
def api_profile_cookies_list(name: str):
    """Return all stored cookies for this profile as Selenium-shape
    dicts. Merges TWO sources:

      1. profiles/<n>/ghostshell_session/cookies.json
         — snapshot written by Ghost Shell at the end of the last run.
      2. profiles/<n>/Default/Network/Cookies
         — Chrome's own live SQLite cookies DB. Chrome writes to this
         while the profile is running and keeps it on disk after exit.

    Source #2 is what users actually care about: if they logged into
    Google during a run and expect the cookies to persist, they want to
    SEE them right now, not wait for another run. Previously we only
    read source #1 which was stale by design.

    Each cookie dict has `_source` = "session" / "chrome_live" / "both"
    so the UI can visualise provenance.
    """
    import ghost_shell.session.cookies as cookie_manager
    cookies = cookie_manager.list_cookies_merged(name)
    return jsonify({
        "count":   len(cookies),
        "cookies": cookies,
    })


@app.route("/api/profiles/<name>/cookies/export", methods=["GET"])
def api_profile_cookies_export(name: str):
    """Download cookies as a JSON or Netscape file.
    Query params:
      ?format=json (default) — EditThisCookie-compatible JSON array
      ?format=netscape       — classic cookies.txt (curl/wget)
    """
    import ghost_shell.session.cookies as cookie_manager
    cookies = cookie_manager.list_cookies_merged(name)
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


@app.route("/api/profiles/<name>/cookies/import", methods=["POST"])
def api_profile_cookies_import(name: str):
    """Import cookies from a JSON or Netscape blob.

    Body: {blob: "...", mode: "merge" | "replace"}
    - merge   (default): add these cookies to existing ones, duplicates
      keyed by (name, domain, path) overwrite old values
    - replace: discard existing cookies, use imported list only

    Returns {count, added, replaced_total}. If the payload can't be
    parsed at all, returns 400 with the parse error.
    """
    import ghost_shell.session.cookies as cookie_manager
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


@app.route("/api/profiles/<name>/cookies/clear", methods=["POST"])
def api_profile_cookies_clear(name: str):
    """Delete all cookies for this profile. Note: if the profile is
    currently running, Chrome's live DB still holds them until shutdown.
    The dashboard shows a warning for active profiles."""
    import ghost_shell.session.cookies as cookie_manager
    cookie_manager.clear_cookies(name)
    return jsonify({"ok": True})


@app.route("/api/profiles/<name>/cookies/<path:cookie_name>", methods=["DELETE"])
def api_profile_cookie_delete(name: str, cookie_name: str):
    """Delete a single cookie by its name (across all domains the
    profile has for it). Used by the row-level delete button in the UI."""
    import ghost_shell.session.cookies as cookie_manager
    cookies = cookie_manager.list_cookies(name)
    before = len(cookies)
    filtered = [c for c in cookies if c.get("name") != cookie_name]
    cookie_manager.save_cookies(name, filtered)
    return jsonify({"ok": True, "removed": before - len(filtered)})


@app.route("/api/profiles/<name>/storage", methods=["GET"])
def api_profile_storage_list(name: str):
    """Return stored localStorage map (per-origin JSON dict)."""
    import ghost_shell.session.cookies as cookie_manager
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
    """Filter-aware competitor list.

    Query params:
      days    1 | 7 | 30 | 0 (0 = all-time)
      q       free-text substring against domain + title
    Returns by_domain + recent + counters + activity classification
    (new / active / quieting per domain, based on first_seen / last_seen).
    """
    from datetime import datetime, timedelta
    db = get_db()

    days = request.args.get("days", type=int, default=0) or None
    q    = request.args.get("q", "", type=str).strip() or None

    total, unique = db.competitors_count(days=days)
    all_total, all_unique = db.competitors_count()   # unscoped for stats bar
    by_domain = db.competitors_by_domain(days=days, search=q)

    # Merge action counters (clicks, skips, errors) — unscoped so users
    # see lifetime action totals even when filter window narrows.
    actions_by_domain = db.action_events_by_domain()
    now = datetime.now()
    for row in by_domain:
        stats = actions_by_domain.get(row["domain"], {})
        row["actions_ran"]     = stats.get("ran", 0)
        row["actions_skipped"] = stats.get("skipped", 0)
        row["actions_errored"] = stats.get("errored", 0)
        row["last_action_at"]  = stats.get("last_action_at")

        # Activity classification — drives the coloured badge in the UI.
        # NEW:      first_seen within 7 days
        # ACTIVE:   last_seen within 3 days (and not new)
        # QUIETING: last_seen older than 14 days
        # STEADY:   everything else
        status = "steady"
        try:
            first = datetime.fromisoformat(row["first_seen"])
            last  = datetime.fromisoformat(row["last_seen"])
            if (now - first) <= timedelta(days=7):
                status = "new"
            elif (now - last) > timedelta(days=14):
                status = "quieting"
            elif (now - last) <= timedelta(days=3):
                status = "active"
        except Exception:
            pass
        row["activity"] = status

    # Aggregate counts for the hero KPI strip
    kpis = {
        "new":      sum(1 for r in by_domain if r["activity"] == "new"),
        "active":   sum(1 for r in by_domain if r["activity"] == "active"),
        "quieting": sum(1 for r in by_domain if r["activity"] == "quieting"),
    }

    return jsonify({
        "total_records":    total,
        "unique_domains":   unique,
        "all_time_total":   all_total,
        "all_time_unique":  all_unique,
        "kpis":             kpis,
        "by_domain":        by_domain,
        "recent":           db.competitors_recent(limit=150, days=days, search=q),
        "filter": {"days": days or 0, "q": q or ""},
    })


@app.route("/api/competitors/trend", methods=["GET"])
def api_competitors_trend():
    """Daily bucket counts for top-N domains over last N days. Powers
    the hero line chart."""
    days  = request.args.get("days",  type=int, default=7)
    top_n = request.args.get("top",   type=int, default=8)
    return jsonify(get_db().competitors_trend(days=days, top_n=top_n))


@app.route("/api/competitors/sparklines", methods=["GET"])
def api_competitors_sparklines():
    """Per-domain daily counts for the small row-level sparkline. One
    query returns counts for every domain over the window."""
    days = request.args.get("days", type=int, default=7)
    return jsonify({"days": days, "data": get_db().competitors_sparklines(days=days)})


@app.route("/api/competitors/detail", methods=["GET"])
def api_competitors_detail():
    """Per-domain drill-down for expandable rows."""
    domain = request.args.get("domain", "").strip()
    if not domain:
        return jsonify({"error": "domain is required"}), 400
    days = request.args.get("days", type=int, default=30)
    return jsonify(get_db().competitor_detail(domain, days=days))


@app.route("/api/competitors/by-query", methods=["GET"])
def api_competitors_by_query():
    """Share-of-voice tab data."""
    days = request.args.get("days", type=int, default=30)
    top  = request.args.get("per_query_top", type=int, default=5)
    return jsonify({"queries": get_db().competitors_by_query(days=days,
                                                              per_query_top=top)})


@app.route("/api/competitors/export", methods=["GET"])
def api_competitors_export():
    """Export the current filter result as CSV or JSON. Delegates to
    competitors_recent() + competitors_by_domain() for proper paginated
    semantics; for simplicity the "flat" CSV view uses recent rows."""
    fmt   = request.args.get("format", "csv").lower()
    days  = request.args.get("days", type=int, default=0) or None
    q     = request.args.get("q", "", type=str).strip() or None
    rows  = get_db().competitors_recent(limit=10000, days=days, search=q)

    if fmt == "json":
        return jsonify({"records": rows})

    # CSV — use stdlib csv + StringIO, stream as a download
    import csv, io
    buf = io.StringIO()
    cols = ["timestamp", "query", "domain", "title", "display_url",
            "clean_url", "google_click_url", "run_id"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=competitors-{}d.csv".format(days or "all")},
    )


@app.route("/api/competitors/add-to-list", methods=["POST"])
def api_competitors_add_to_list():
    """One-click: push a domain into my_domains / target_domains /
    block_domains from the competitors table. Idempotent — re-adding
    an existing entry is a no-op."""
    data = request.get_json(silent=True) or {}
    domain = (data.get("domain") or "").strip().lower()
    target = (data.get("list") or "").strip()
    if not domain:
        return jsonify({"error": "domain is required"}), 400
    key_map = {
        "my":     "search.my_domains",
        "target": "search.target_domains",
        "block":  "search.block_domains",
    }
    if target not in key_map:
        return jsonify({"error": f"invalid list: {target}"}), 400
    cfg_key = key_map[target]
    db = get_db()
    current = db.config_get(cfg_key) or []
    # Tolerate both list-of-strings and newline-joined-string storage
    if isinstance(current, str):
        current = [x.strip() for x in current.splitlines() if x.strip()]
    if domain in current:
        return jsonify({"ok": True, "already": True, "list": target})
    current.append(domain)
    db.config_set(cfg_key, current)
    return jsonify({"ok": True, "list": target, "count": len(current)})


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
    """Sidebar polls this to render its "N running" widget.
    Also used by scheduler.py to poll status of a specific run it
    spawned — in that case it passes ?run_id=N. Without the run_id
    param, returns the "most recent active" shape (legacy behaviour)."""
    run_id = request.args.get("run_id", type=int)
    if run_id is not None:
        slot = RUNNER_POOL.get(run_id)
        if slot is None:
            # Not in the in-memory pool — look at the DB as fallback.
            # This matters when the run finished N minutes ago and was
            # garbage-collected from RUNNER_POOL but scheduler is still
            # polling for its exit code.
            try:
                row = get_db()._get_conn().execute(
                    "SELECT * FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if row:
                    return jsonify({
                        "running":     row["finished_at"] is None,
                        "exit_code":   row["exit_code"],
                        "run_id":      row["id"],
                        "profile_name": row["profile_name"],
                        "started_at":  row["started_at"],
                        "finished_at": row["finished_at"],
                    })
            except Exception:
                pass
            return jsonify({"running": False, "run_id": run_id, "exit_code": None})
        return jsonify({
            "running":     slot.is_running,
            "exit_code":   slot.last_exit_code,
            "run_id":      slot.run_id,
            "profile_name": slot.profile_name,
            "started_at":  slot.started_at,
            "finished_at": slot.finished_at,
        })
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


# Serialises the check-and-reserve window inside _spawn_run. Flask's
# threaded=True means multiple HTTP requests land in parallel worker
# threads; without this lock two concurrent POSTs for the same profile
# could both pass is_profile_running() check before either registers
# a slot. Held only briefly — released before the actual subprocess
# Popen, which is the slow step.
_SPAWN_LOCK = threading.Lock()


def _launch_run_thread(slot: "RunnerSlot", proxy_url: str) -> None:
    """Spawn the main.py subprocess for a reserved slot and start its
    stdout-reader + chrome-watcher threads. Called OUTSIDE _SPAWN_LOCK
    so concurrent spawns for different profiles don't serialise on
    Popen latency.

    The heavy lifting is in the inner `run_thread` closure which stays
    alive for the whole lifetime of the run — it owns the Popen, pipes
    stdout into per-slot logs + broadcast, runs the chrome-tree
    watcher, and updates DB when the process exits."""
    run_id       = slot.run_id
    profile_name = slot.profile_name
    db           = get_db()

    def run_thread():
        slot.log(
            f"Starting run #{run_id} (profile: {profile_name})...",
            "info",
        )
        # Fan out to every SSE subscriber so all open tabs see this line.
        RUNNER_POOL.broadcast_log({
            "ts":           datetime.now().strftime("%H:%M:%S"),
            "level":        "info",
            "message":      f"▶ #{run_id} {profile_name} — starting",
            "run_id":       run_id,
            "profile_name": profile_name,
        })
        try:
            env = os.environ.copy()
            env["GHOST_SHELL_RUN_ID"]       = str(run_id)
            env["GHOST_SHELL_PROFILE_NAME"] = profile_name
            if proxy_url:
                env["GHOST_SHELL_PROXY_URL"] = proxy_url

            proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "ghost_shell", "monitor"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                cwd=PROJECT_ROOT,
                env=env,
            )
            slot.process = proc

            # Record PID so reap_stale_runs can find this tree if the
            # dashboard is restarted mid-run.
            try:
                db.run_set_pid(run_id, proc.pid)
            except Exception as e:
                logging.warning(f"[api_run] run_set_pid failed: {e}")

            # Chrome-tree watcher — detects user-closed-the-window,
            # terminates main.py so we don't keep its python wrapper
            # alive after Chrome died.
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

            # Route stdout into slot.log + broadcast to SSE subscribers.
            # Reading line-by-line blocks until the child prints or exits.
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
                        "ts":           datetime.now().strftime("%H:%M:%S"),
                        "level":        lvl,
                        "message":      line,
                        "run_id":       run_id,
                        "profile_name": profile_name,
                    })
            except Exception as e:
                slot.log(f"stdout reader: {e}", "debug")

            monitor_stop.set()
            proc.wait()
            exit_code = proc.returncode

            # Finalize run row — ONLY exit_code here. Child process
            # (main.py) already wrote total_queries / total_ads /
            # captchas from its own RUN_COUNTERS via its final
            # run_finish call. If we overwrote them here with
            # events_summary (which reads 24h cumulative, not
            # per-run!) we'd clobber the correct numbers.
            #
            # events_summary fell out of favour for per-run stats because
            # (a) it's 24h window so every run inflates with siblings'
            # data, and (b) sqm.record() has silent-fail modes where
            # events don't get written at all. RUN_COUNTERS in main.py
            # is the single source of truth.
            db.run_finish(run_id, exit_code=exit_code)
            slot.log(
                f"Monitor #{run_id} finished (code {exit_code})",
                "info" if exit_code == 0 else "error",
            )
            RUNNER_POOL.mark_finished(run_id, exit_code=exit_code)

            # Notify all SSE subscribers that stats changed so the
            # Overview page refreshes IMMEDIATELY rather than waiting
            # for its 15s poll. Uses the log broadcast channel — the
            # frontend recognizes entries with type="event" as signals
            # rather than log lines and dispatches them to listeners.
            RUNNER_POOL.broadcast_log({
                "type":         "event",
                "event":        "run_finished",
                "run_id":       run_id,
                "profile_name": slot.profile_name,
                "exit_code":    exit_code,
                "ts":           datetime.now().isoformat(timespec="seconds"),
            })

        except Exception as e:
            db.run_finish(run_id, exit_code=-1, error=str(e))
            slot.log(f"Error during run: {e}", "error")
            RUNNER_POOL.mark_finished(run_id, exit_code=-1, error=str(e))
        finally:
            # Keep the slot around for 60s so the UI can show final
            # status in active_runs queries, then GC.
            def _gc():
                time.sleep(60)
                RUNNER_POOL.remove(run_id)
            threading.Thread(target=_gc, daemon=True).start()

    t = threading.Thread(target=run_thread, daemon=True)
    slot.thread = t
    t.start()


def _spawn_run(profile_name: str) -> dict:
    """
    Shared launch path — used by /api/run (legacy default) and
    /api/runs (explicit per-profile). Enforces:
      - one-run-per-profile rule (prevents user-data-dir corruption)
      - global max_parallel cap from config
    Returns {"ok": True, "run_id": N} on success, or raises ValueError
    with an HTTP-friendly message on cap violation.

    Flask runs with threaded=True, so two concurrent POSTs to the same
    URL land in two worker threads simultaneously. Without the lock
    below, both could pass the is_profile_running() check (reading
    false) before either one registers a slot — resulting in two
    main.py processes on the same profile, both trashing the same
    user-data-dir. The lock serialises the "check → reserve" window.
    """
    with _SPAWN_LOCK:
        if RUNNER_POOL.is_profile_running(profile_name):
            raise ValueError(
                f"Profile {profile_name!r} is already running — one run per "
                f"profile at a time (they'd corrupt each other's user-data-dir)"
            )

        db = get_db()

        # Cross-process guard (separate from RunnerPool check)
        try:
            from ghost_shell.core.process_reaper import ensure_no_live_run_for_profile
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

        # Reserve a slot — creates the run_id and registers the slot
        # BEFORE we leave the lock. Everything after the lock is
        # per-slot and doesn't interfere with another profile's spawn.
        proxy_cfg = db.profile_effective_proxy(profile_name)
        proxy_url = proxy_cfg["url"]
        run_id = db.run_start(profile_name, proxy_url)
        slot = RunnerSlot(run_id=run_id, profile_name=profile_name)
        RUNNER_POOL.add(slot)

    # Outside the lock from here — the actual subprocess spawn can
    # take ~2 seconds (Popen + PID recording) which we don't want
    # blocking other profiles' spawn paths.
    _launch_run_thread(slot, proxy_url)
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


@app.route("/api/run/start", methods=["POST"])
def api_run_start():
    """Scheduler-facing spawn endpoint. Identical behaviour to
    /api/runs but accepts the same payload shape scheduler.py uses
    ({profile_name: ...}). Previously scheduler posted to this URL
    and silently 404'd — which sent it to the fallback Popen path,
    bypassing the dashboard's log pipe AND the slot registry. Result:
    the run happened, but its logs never appeared in the UI and the
    active-runs panel showed nothing. This alias fixes both."""
    payload = request.get_json(silent=True) or {}
    profile_name = payload.get("profile_name")
    if not profile_name:
        return jsonify({"error": "profile_name required"}), 400
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
@app.route("/api/run/<int:run_id>/stop", methods=["POST"])
def api_runs_stop_specific(run_id: int):
    """Stop one specific run by its run_id. Leaves other active runs alone.

    Exposed under BOTH /api/runs/<id>/stop (canonical, plural) and
    /api/run/<id>/stop (singular alias used by scheduler.py). The alias
    is here for the same reason /api/run/start exists as an alias for
    /api/runs — scheduler code uses the singular form and silently
    404'd before this fix, which meant timeout-triggered kills were
    never delivered and hung runs kept running until they self-healed."""
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
    """Delete a profile — folder, related DB rows, profiles-table row,
    and — if this was the active profile — reassign browser.profile_name
    to the next available profile.

    Previously we only cleared events/selfchecks/fingerprints + folder,
    but profiles_list() unions profile_name from runs too. Deleted
    profiles kept appearing in the dropdown because run history still
    referenced them. Now we drop the profiles row AND profiles_list()
    filters names that exist ONLY as run-history tombstones.

    Run history stays on purpose: a deleted profile's runs are still
    useful aggregate data for the Overview stats. The name appears in
    run history but not in dropdowns / Profiles page.
    """
    if RUNNER_POOL.is_profile_running(name):
        return jsonify({
            "error": f"Profile '{name}' is currently running - stop it "
                     f"before deleting."
        }), 409

    try:
        import shutil
        profile_dir = os.path.join("profiles", name)
        if os.path.exists(profile_dir):
            shutil.rmtree(profile_dir, ignore_errors=True)

        db = get_db()
        conn = db._get_conn()
        for table in ("events", "selfchecks", "fingerprints"):
            conn.execute(f"DELETE FROM {table} WHERE profile_name = ?", (name,))

        try:
            db.profile_meta_delete(name)
        except Exception as e:
            logging.debug(f"[delete profile] profile_meta_delete: {e}")

        # Reassign active profile if we nuked the default. Without this,
        # browser.profile_name keeps pointing at a dead profile and every
        # page reading it breaks.
        #
        # CRITICAL: profiles_list() treats `browser.profile_name` as an
        # "alive" source (last-resort fallback for fresh installs). Since
        # we haven't reset that config value YET, profiles_list() still
        # returns the deleted name. If we pick remaining[0] without
        # filtering, we reassign the config to the deleted profile and
        # the UI sees no change — the exact bug user reported when
        # deleting the sole default profile.
        reassigned_to = None
        active = db.config_get("browser.profile_name")
        if active == name:
            remaining = [p["name"] for p in db.profiles_list()
                         if p["name"] != name]
            reassigned_to = remaining[0] if remaining else "profile_01"
            db.config_set("browser.profile_name", reassigned_to)
            logging.info(
                f"[delete profile] active profile was '{name}', "
                f"reassigned browser.profile_name -> '{reassigned_to}' "
                f"({'remaining profiles: ' + str(remaining) if remaining else 'no profiles left — fresh profile_01 will be created on next run'})"
            )

        return jsonify({
            "ok":            True,
            "deleted":       name,
            "reassigned_to": reassigned_to,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# API: SCHEDULER
# ──────────────────────────────────────────────────────────────

SCHEDULER_PID_FILE = os.path.join(PROJECT_ROOT, ".scheduler.pid")


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
            [sys.executable, "-u", "-m", "ghost_shell", "scheduler"],
            cwd=PROJECT_ROOT,
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
        from ghost_shell.core.process_reaper import reap_stale_runs
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

@app.route("/api/logs/recent", methods=["GET"])
def api_logs_recent():
    """Return recent log entries from the in-memory ring buffer.

    Called by the Logs page on every load so users see the last
    2000 log lines immediately after reload instead of a blank screen.
    Also used as a gap-filler after SSE reconnects: the frontend
    remembers the last `seq` it received and asks for everything
    newer.

    Query params:
      ?limit        — cap on entries returned (default 2000, max 2000)
      ?profile      — filter to one profile name
      ?level        — info | warning | error
      ?since_seq    — monotonic seq of last entry client already has

    Does NOT read from the DB `logs` table (slow + lossy since we only
    persist run-summary events there). Ring buffer is authoritative
    for recent-window views; DB is for long-term history via
    /api/logs/history.
    """
    limit        = min(int(request.args.get("limit", 2000) or 2000), 2000)
    profile_name = request.args.get("profile") or None
    level        = request.args.get("level") or None
    since_seq    = request.args.get("since_seq", type=int)

    entries = RUNNER_POOL.recent_logs(
        limit         = limit,
        profile_name  = profile_name,
        level         = level,
        since_id      = since_seq,
    )
    return jsonify({"entries": entries, "count": len(entries)})


@app.route("/api/logs/live")
def api_logs_live():
    """SSE live logs stream — merged across all active runs.

    Each entry includes run_id and profile_name so the Logs page can
    tag/filter lines by which run they came from. Each browser tab gets
    its own subscriber queue so open-in-two-tabs works without messages
    alternating between them.
    """
    my_queue = RUNNER_POOL.subscribe()

    def generate():
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    entry = my_queue.get(timeout=1)
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
        finally:
            # Whether client closed cleanly or via exception, release
            # our queue slot so broadcast_log() doesn't keep pushing
            # into a dead queue forever (bounded by 1000 then FIFO-drop,
            # but still wasteful).
            RUNNER_POOL.unsubscribe(my_queue)

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
    """Manually trigger the legacy on-disk-config migration."""
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
        from ghost_shell.fingerprint.device_templates import DEVICE_TEMPLATES

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
    from ghost_shell.fingerprint.device_templates import DeviceTemplateBuilder
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
    from ghost_shell.fingerprint.device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    name      = (data.get("name") or "").strip()
    template  = data.get("template") or None
    language  = data.get("language") or "uk-UA"
    enrich    = bool(data.get("enrich", True))
    # Optional per-profile proxy override accepted on creation. Empty
    # string / None = no override (inherit global). The format is the
    # same string that the Proxy page accepts. We do a coarse client-side
    # check then a stricter scheme check here.
    proxy_url = (data.get("proxy_url") or "").strip()

    if not name or not re.match(r"^[A-Za-z0-9_\-]+$", name):
        return jsonify({"error": "invalid name (letters, digits, _ and - only)"}), 400

    if proxy_url and not re.match(r"^(https?|socks5)://", proxy_url, re.IGNORECASE):
        return jsonify({"error": "proxy URL must use http://, https:// or socks5://"}), 400

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

        # Save the per-profile proxy override if the user provided one
        # in the create dialog. profile_meta_upsert is the same code
        # path used by the Edit Profile page's "Save overrides" button,
        # so format expectations stay consistent.
        if proxy_url and hasattr(db, "profile_meta_upsert"):
            db.profile_meta_upsert(name, proxy_url=proxy_url)

        # Create user data dir on disk
        import os as _os
        prof_dir = _os.path.join(PROJECT_ROOT, "profiles", name)
        _os.makedirs(prof_dir, exist_ok=True)

        return jsonify({"ok": True, "name": name,
                        "template": payload.get("template_name")})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# CHROME HISTORY IMPORT
# ──────────────────────────────────────────────────────────────

@app.route("/api/chrome-import/discover", methods=["GET"])
def api_chrome_import_discover():
    """Look for a Chrome profile on this machine and return its path.
    Used by the Edit Profile page to pre-fill the "source" field with
    a sensible default so users don't have to navigate to the Chrome
    User Data dir manually."""
    try:
        from ghost_shell.browser.chrome_import import (
            discover_source, _DEFAULT_SOURCES_WIN,
            _DEFAULT_SOURCES_MAC, _DEFAULT_SOURCES_LINUX,
        )
        found = discover_source()
        if sys.platform == "win32":
            candidates = _DEFAULT_SOURCES_WIN
        elif sys.platform == "darwin":
            candidates = _DEFAULT_SOURCES_MAC
        else:
            candidates = _DEFAULT_SOURCES_LINUX
        return jsonify({
            "source":     found,
            "candidates": candidates,
            "platform":   sys.platform,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<name>/chrome-import", methods=["POST"])
def api_profile_chrome_import(name: str):
    """Import real browsing history from a host Chrome profile into
    this Ghost Shell profile. Body:
      {
        "source":         "C:/Users/.../Chrome/User Data/Default",
        "days":           90,
        "max_urls":       5000,
        "skip_sensitive": true
      }

    Preconditions:
      - Source Chrome MUST be closed (we verify via SQLite lock probe).
      - Destination Ghost Shell profile MUST NOT be running.

    Returns a summary dict with counts per category imported."""
    data = request.get_json(silent=True) or {}

    if RUNNER_POOL.is_profile_running(name):
        return jsonify({
            "error": "Profile is currently running - stop it first, "
                     "then retry import."
        }), 409

    try:
        from ghost_shell.browser.chrome_import import ChromeImporter, discover_source
        source = data.get("source") or discover_source()
        if not source:
            return jsonify({
                "error": "No Chrome profile found on this machine - "
                         "pass an explicit 'source' path."
            }), 400

        imp = ChromeImporter(source_dir=source, dest_profile=name)
        summary = imp.import_all(
            days           = int(data.get("days") or 90),
            max_urls       = int(data.get("max_urls") or 5000),
            skip_sensitive = bool(data.get("skip_sensitive", True)),
        )
        return jsonify({"ok": True, "source": source, "summary": summary})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/profiles/<name>/regenerate-fingerprint", methods=["POST"])
def api_profile_regenerate_fingerprint(name):
    """
    Re-roll the fingerprint for an existing profile. Useful if current
    fingerprint is getting flagged — new seed = new UA / screen / fonts.
    Accepts optional JSON body with seed/template/language overrides.
    """
    from ghost_shell.fingerprint.device_templates import DeviceTemplateBuilder
    data = request.get_json(silent=True) or {}
    template  = data.get("template") or None
    language  = data.get("language") or None
    new_seed  = data.get("seed_suffix")   # optional: appended to profile name

    db = get_db()
    if hasattr(db, "profile_get") and not db.profile_get(name):
        return jsonify({"error": "profile not found"}), 404

    if not language:
        prof = db.profile_get(name) if hasattr(db, "profile_get") else {}
        language = (prof or {}).get("preferred_language") or "uk-UA"

    if template == "auto":
        template = None

    try:
        # Use a variant of the profile name to get deterministic-but-different
        # fingerprint without losing the ability to re-roll deterministically.
        seed_name = name
        if new_seed:
            seed_name = f"{name}#{new_seed}"
        else:
            # Timestamp-based seed for one-shot "just give me something new"
            import time as _t
            seed_name = f"{name}#{int(_t.time())}"

        builder = DeviceTemplateBuilder(
            profile_name       = seed_name,
            preferred_language = language,
            force_template     = template,
        )
        payload = builder.generate_payload_dict()
        # Save under the ORIGINAL profile name so the new fingerprint
        # becomes the active one.
        payload["profile_name"] = name
        db.fingerprint_save(name, payload)

        # Clear cached health state — old 13/13 no longer applies
        if hasattr(db, "reset_profile_health"):
            db.reset_profile_health(name)

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

@app.route("/api/profiles/<name>/clear-history", methods=["POST"])
def api_profile_clear_history_new(name):
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
        result = db.clear_profile_history(name, scope=scope)
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
        from ghost_shell.actions.runner import action_catalog, action_common_params
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

    Deprecated in favour of /api/actions/flow (single-list unified
    runtime), but kept for back-compat with old Scripts UI + external
    config imports. Saving here also clears any saved unified flow,
    so the legacy shape is the source of truth again.
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

    # Clear the unified flow when legacy endpoint is used — we want
    # one source of truth, not both.
    db.config_set("actions.flow", [])

    return jsonify({"ok": True, "saved": saved})


# ── Unified flow (new format) ────────────────────────────────────

def _migrate_legacy_to_flow(main_script: list,
                            post_ad_actions: list,
                            on_target_actions: list) -> list:
    """Convert the old two-pipeline shape into a single unified flow.

    Strategy: keep main_script as-is, then find every `search_query`
    step and append a `foreach_ad` wrapper containing the post_ad_actions
    right after it. This replicates the old runtime behavior (per-ad
    pipeline ran automatically after each search) in explicit form
    that the user can now see and modify.

    Legacy on_target_domain_actions (pre-flag-merging) get merged into
    post_ad_actions with an `only_on_target: true` flag, matching what
    the v1 UI migration did.
    """
    if on_target_actions:
        merged_post = list(post_ad_actions or []) + [
            {**s, "only_on_target": True} for s in on_target_actions
        ]
    else:
        merged_post = list(post_ad_actions or [])

    def wrap_search(step):
        """Replace a bare search_query with (search_query + foreach_ad).
        If there are no post-ad actions, leaves search_query alone."""
        if step.get("type") != "search_query" or not merged_post:
            return [step]
        return [
            step,
            {
                "type":    "foreach_ad",
                "enabled": True,
                "steps":   [dict(s) for s in merged_post],   # deep-ish copy
            },
        ]

    flow = []
    for step in (main_script or []):
        # Recurse into nested steps of loop actions
        if step.get("type") in ("loop", "foreach"):
            nested = step.get("steps") or []
            new_nested = []
            for ns in nested:
                new_nested.extend(wrap_search(ns))
            new_step = dict(step)
            new_step["steps"] = new_nested
            flow.append(new_step)
        else:
            flow.extend(wrap_search(step))
    return flow


@app.route("/api/actions/flow", methods=["GET"])
def api_actions_flow_get():
    """Return the unified flow — single ordered list of steps.

    If the user hasn't saved a unified flow yet but has legacy
    main_script/post_ad_actions, this endpoint migrates on the fly
    (without persisting) so the Scripts page can show the converted
    flow. Saving via POST persists it and shadows the legacy keys.
    """
    db = get_db()
    raw = db.config_get("actions.flow")
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except Exception: raw = None
    if isinstance(raw, list) and raw:
        return jsonify({"flow": raw, "migrated_from_legacy": False})

    # Nothing saved yet — migrate legacy on the fly
    main_script = db.config_get("actions.main_script") or []
    post_ad     = db.config_get("actions.post_ad_actions") or []
    on_target   = db.config_get("actions.on_target_domain_actions") or []
    for var in ("main_script", "post_ad", "on_target"):
        v = locals()[var]
        if isinstance(v, str):
            try: locals()[var] = json.loads(v)
            except Exception: locals()[var] = []

    migrated = _migrate_legacy_to_flow(main_script, post_ad, on_target)
    return jsonify({
        "flow":                 migrated,
        "migrated_from_legacy": bool(migrated),
    })


@app.route("/api/actions/flow", methods=["POST"])
def api_actions_flow_save():
    """Save a unified flow. Body: {"flow": [...steps...]}.
    Validates that every step has a `type`. Recurses into containers
    (if/foreach_ad/foreach/loop) to validate nested steps too."""
    data = request.get_json(silent=True) or {}
    flow = data.get("flow")
    if not isinstance(flow, list):
        return jsonify({"error": "flow must be a list"}), 400

    def _validate(steps, path=""):
        for i, step in enumerate(steps):
            p = f"{path}[{i}]"
            if not isinstance(step, dict):
                return f"{p} is not a dict"
            if "type" not in step:
                return f"{p} missing 'type'"
            # Recurse into container params
            for key in ("steps", "then_steps", "else_steps"):
                if isinstance(step.get(key), list):
                    err = _validate(step[key], f"{p}.{key}")
                    if err: return err
        return None

    err = _validate(flow)
    if err:
        return jsonify({"error": err}), 400

    db = get_db()
    db.config_set("actions.flow", flow)
    # Clear legacy pipelines so there's ONE source of truth.
    db.config_set("actions.main_script", [])
    db.config_set("actions.post_ad_actions", [])
    db.config_set("actions.on_target_domain_actions", [])

    return jsonify({"ok": True, "count": len(flow)})


@app.route("/api/actions/condition-kinds", methods=["GET"])
def api_condition_kinds():
    """Return the list of condition predicates the `if` action
    supports — used by the Scripts inspector to populate its
    condition picker."""
    from ghost_shell.actions.runner import CONDITION_KINDS
    return jsonify({"kinds": CONDITION_KINDS})


# ──────────────────────────────────────────────────────────────
# API: SCRIPTS LIBRARY — saved flow definitions
# ──────────────────────────────────────────────────────────────
#
# Each script is { id, name, description, flow, is_default }, with
# `flow` being the full unified-flow step tree (same shape as the
# legacy /api/actions/flow endpoint operated on).
#
# Profiles reference scripts via profiles.script_id — see the
# script_resolve_for_profile helper used by main.py at run time.

@app.route("/api/scripts", methods=["GET"])
def api_scripts_list():
    """Return a summary list of all scripts (no full flow JSON)."""
    db = get_db()
    return jsonify({"scripts": db.scripts_list()})


@app.route("/api/scripts", methods=["POST"])
def api_scripts_create():
    """Create a new script. Body:
      { "name": "...", "description": "...", "flow": [...],
        "is_default": false }
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    flow = data.get("flow") or []
    if not isinstance(flow, list):
        return jsonify({"error": "flow must be a list"}), 400

    # Validate step shape (same rules as /api/actions/flow)
    def _validate(steps, path=""):
        for i, step in enumerate(steps):
            p = f"{path}[{i}]"
            if not isinstance(step, dict) or "type" not in step:
                return f"{p} missing 'type'"
            for key in ("steps", "then_steps", "else_steps"):
                if isinstance(step.get(key), list):
                    err = _validate(step[key], f"{p}.{key}")
                    if err: return err
        return None
    err = _validate(flow)
    if err:
        return jsonify({"error": err}), 400

    db = get_db()
    try:
        script_id = db.script_create(
            name=name,
            description=data.get("description", "") or "",
            flow=flow,
            is_default=bool(data.get("is_default")),
        )
    except Exception as e:
        # Likely UNIQUE constraint on name
        return jsonify({"error": f"Could not create: {e}"}), 400
    return jsonify({"ok": True, "id": script_id})


@app.route("/api/scripts/<int:script_id>", methods=["GET"])
def api_scripts_get(script_id):
    """Fetch one script with its full flow JSON."""
    db = get_db()
    sc = db.script_get(script_id)
    if not sc:
        return jsonify({"error": "not found"}), 404
    sc["profiles"] = db.script_profiles(script_id)
    return jsonify({"script": sc})


@app.route("/api/scripts/<int:script_id>", methods=["PUT", "PATCH"])
def api_scripts_update(script_id):
    """Partial update. Any of name/description/flow/is_default."""
    data = request.get_json(silent=True) or {}
    db = get_db()
    if "flow" in data:
        # Same validation as create
        flow = data["flow"]
        if not isinstance(flow, list):
            return jsonify({"error": "flow must be a list"}), 400
        def _validate(steps, path=""):
            for i, step in enumerate(steps):
                p = f"{path}[{i}]"
                if not isinstance(step, dict) or "type" not in step:
                    return f"{p} missing 'type'"
                for key in ("steps", "then_steps", "else_steps"):
                    if isinstance(step.get(key), list):
                        err = _validate(step[key], f"{p}.{key}")
                        if err: return err
            return None
        err = _validate(flow)
        if err:
            return jsonify({"error": err}), 400
    try:
        ok = db.script_update(
            script_id,
            name=data.get("name"),
            description=data.get("description"),
            flow=data.get("flow"),
            is_default=data.get("is_default") if "is_default" in data else None,
        )
    except Exception as e:
        return jsonify({"error": f"Could not update: {e}"}), 400
    if not ok:
        return jsonify({"error": "not found or no changes"}), 404
    return jsonify({"ok": True})


@app.route("/api/scripts/<int:script_id>", methods=["DELETE"])
def api_scripts_delete(script_id):
    db = get_db()
    try:
        ok = db.script_delete(script_id)
    except ValueError as e:
        # e.g. "Cannot delete the default script"
        return jsonify({"error": str(e)}), 400
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/scripts/<int:script_id>/assign", methods=["POST"])
def api_scripts_assign(script_id):
    """Assign this script to one or more profiles. Body:
      { "profiles": ["profile_01", "profile_02"] }
    Unassigns these profiles from any other script they had."""
    data = request.get_json(silent=True) or {}
    profiles = data.get("profiles") or []
    if not isinstance(profiles, list):
        return jsonify({"error": "profiles must be a list"}), 400
    db = get_db()
    # Confirm script exists
    if not db.script_get(script_id):
        return jsonify({"error": "script not found"}), 404
    for p in profiles:
        if not isinstance(p, str) or not p:
            continue
        db.script_assign_to_profile(p, script_id)
    return jsonify({"ok": True, "assigned": len(profiles)})


@app.route("/api/profiles/<name>/script", methods=["GET"])
def api_profile_script_get(name):
    """Return the script assigned to a profile (resolves to default
    if none assigned). UI uses this for the profile-page dropdown."""
    db = get_db()
    sc = db.script_resolve_for_profile(name)
    if not sc:
        return jsonify({"script": None})
    # Strip the flow to keep payload small — UI only needs metadata
    return jsonify({"script": {
        "id":          sc["id"],
        "name":        sc["name"],
        "description": sc["description"],
        "is_default":  sc.get("is_default", 0),
    }})


@app.route("/api/profiles/<name>/script", methods=["POST", "PUT"])
def api_profile_script_set(name):
    """Assign a script (by id) to a profile. Body:
       { "script_id": 5 } or { "script_id": null } to clear.
    """
    data = request.get_json(silent=True) or {}
    script_id = data.get("script_id")
    if script_id is not None and not isinstance(script_id, int):
        return jsonify({"error": "script_id must be integer or null"}), 400
    db = get_db()
    if script_id is not None and not db.script_get(script_id):
        return jsonify({"error": "script not found"}), 404
    db.script_assign_to_profile(name, script_id)
    return jsonify({"ok": True})


# ──────────────────────────────────────────────────────────────
# API: PROXIES LIBRARY — saved proxy configurations
# ──────────────────────────────────────────────────────────────
# Parallel shape to the scripts API — same CRUD + assign pattern.
# Proxy "test" endpoint runs a plain-HTTP probe through the proxy
# via proxy_diagnostics.test_proxy() (no Chrome needed), writes the
# result into the cached last_* columns so subsequent page loads
# render fast without re-probing.

@app.route("/api/proxies", methods=["GET"])
def api_proxies_list():
    db = get_db()
    return jsonify({"proxies": db.proxies_list()})


@app.route("/api/proxies", methods=["POST"])
def api_proxies_create():
    """Create a proxy. Body:
      { "url": "http://user:pass@host:port",
        "name": "...",
        "is_default": false,
        "is_rotating": false,
        "rotation_api_url": "...",
        "auto_test": true }
    If auto_test=true (default), runs a diagnostic probe right after
    creation so the row lands in the UI already colored. When false,
    row starts in 'untested' status.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    db = get_db()
    try:
        pid = db.proxy_create(
            url=url,
            name=data.get("name") or None,
            is_rotating=bool(data.get("is_rotating")),
            rotation_api_url=data.get("rotation_api_url") or None,
            rotation_provider=data.get("rotation_provider") or None,
            rotation_api_key=data.get("rotation_api_key") or None,
            is_default=bool(data.get("is_default")),
            notes=data.get("notes") or None,
        )
    except Exception as e:
        return jsonify({"error": f"Could not create: {e}"}), 400

    # Auto-test by default — users want the flag/ISP populated
    # without a second click
    if data.get("auto_test", True):
        try:
            from ghost_shell.proxy.diagnostics import test_proxy
            proxy = db.proxy_get(pid)
            diag = test_proxy(proxy["url"], timeout=10)
            db.proxy_record_diagnostics(pid, diag)
        except Exception as e:
            logging.warning(f"[proxies] auto-test failed: {e}")

    return jsonify({"ok": True, "id": pid,
                    "proxy": db.proxy_get(pid)})


@app.route("/api/proxies/<int:proxy_id>", methods=["GET"])
def api_proxies_get(proxy_id):
    db = get_db()
    p = db.proxy_get(proxy_id)
    if not p:
        return jsonify({"error": "not found"}), 404
    p["profiles"] = db.proxy_profiles(proxy_id)
    return jsonify({"proxy": p})


@app.route("/api/proxies/<int:proxy_id>", methods=["PUT", "PATCH"])
def api_proxies_update(proxy_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    try:
        ok = db.proxy_update(proxy_id, **data)
    except Exception as e:
        return jsonify({"error": f"Could not update: {e}"}), 400
    if not ok:
        return jsonify({"error": "not found or no changes"}), 404
    # If URL changed, invalidate cached diagnostics so a stale badge
    # doesn't keep claiming the old IP is reachable
    if "url" in data:
        db._get_conn().execute("""
            UPDATE proxies SET
                last_status = 'untested',
                last_exit_ip = NULL, last_country = NULL,
                last_country_code = NULL, last_city = NULL,
                last_timezone = NULL, last_asn = NULL,
                last_provider = NULL, last_ip_type = NULL,
                last_detection_risk = NULL, last_latency_ms = NULL,
                last_error = NULL, last_checked_at = NULL
            WHERE id = ?
        """, (proxy_id,))
        db._get_conn().commit()
    return jsonify({"ok": True})


@app.route("/api/proxies/<int:proxy_id>", methods=["DELETE"])
def api_proxies_delete(proxy_id):
    db = get_db()
    try:
        ok = db.proxy_delete(proxy_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/proxies/<int:proxy_id>/test", methods=["POST"])
def api_proxies_test(proxy_id):
    """Run a diagnostic probe through a single proxy and cache the
    result. Returns the diagnostic dict so the UI can render the
    updated status without re-fetching the list."""
    db = get_db()
    proxy = db.proxy_get(proxy_id)
    if not proxy:
        return jsonify({"error": "not found"}), 404
    try:
        from ghost_shell.proxy.diagnostics import test_proxy
        diag = test_proxy(proxy["url"], timeout=15)
        db.proxy_record_diagnostics(proxy_id, diag)
        return jsonify({"ok": True, "diag": diag})
    except Exception as e:
        logging.exception("proxy test failed")
        return jsonify({
            "ok": False,
            "diag": {"ok": False, "error": str(e)},
        }), 500


@app.route("/api/proxies/test-all", methods=["POST"])
def api_proxies_test_all():
    """Probe every proxy in the library sequentially and write back
    cached diagnostics. Synchronous for simplicity — for large
    libraries we'd switch to an SSE stream, but 10-20 proxies is
    fine inline (ip-api allows 45 req/min)."""
    db = get_db()
    proxies = db.proxies_list()
    results = []
    try:
        from ghost_shell.proxy.diagnostics import test_proxy
    except ImportError:
        return jsonify({"error": "proxy_diagnostics module missing"}), 500
    for p in proxies:
        diag = test_proxy(p["url"], timeout=12)
        db.proxy_record_diagnostics(p["id"], diag)
        results.append({
            "id":     p["id"],
            "name":   p["name"],
            "status": "ok" if diag.get("ok") else "error",
            "error":  diag.get("error"),
        })
    return jsonify({"ok": True, "count": len(results), "results": results})


@app.route("/api/proxies/<int:proxy_id>/assign", methods=["POST"])
def api_proxies_assign(proxy_id):
    """Assign this proxy to one or more profiles.
      { "profiles": ["profile_01", "profile_02"] }
    """
    data = request.get_json(silent=True) or {}
    profiles = data.get("profiles") or []
    if not isinstance(profiles, list):
        return jsonify({"error": "profiles must be a list"}), 400
    db = get_db()
    if not db.proxy_get(proxy_id):
        return jsonify({"error": "proxy not found"}), 404
    for p in profiles:
        if isinstance(p, str) and p:
            db.proxy_assign_to_profile(p, proxy_id)
    return jsonify({"ok": True, "assigned": len(profiles)})


@app.route("/api/profiles/<name>/proxy", methods=["GET"])
def api_profile_proxy_get(name):
    """Return the proxy assigned to a profile (resolves to default
    if none assigned)."""
    db = get_db()
    p = db.proxy_resolve_for_profile(name)
    if not p:
        return jsonify({"proxy": None})
    # Don't expose password in the profile-page response
    return jsonify({"proxy": {
        "id":            p["id"],
        "name":          p["name"],
        "url":           p["url"],
        "host":          p["host"],
        "port":          p["port"],
        "type":          p["type"],
        "is_default":    p.get("is_default", 0),
        "is_rotating":   p.get("is_rotating", 0),
        "last_status":   p.get("last_status"),
        "last_country":  p.get("last_country"),
        "last_country_code": p.get("last_country_code"),
    }})


@app.route("/api/profiles/<name>/proxy", methods=["POST", "PUT"])
def api_profile_proxy_set(name):
    data = request.get_json(silent=True) or {}
    proxy_id = data.get("proxy_id")
    if proxy_id is not None and not isinstance(proxy_id, int):
        return jsonify({"error": "proxy_id must be integer or null"}), 400
    db = get_db()
    if proxy_id is not None and not db.proxy_get(proxy_id):
        return jsonify({"error": "proxy not found"}), 404
    db.proxy_assign_to_profile(name, proxy_id)
    return jsonify({"ok": True})


@app.route("/api/proxies/parse-preview", methods=["POST"])
def api_proxies_parse_preview():
    """Parse a bulk paste WITHOUT saving. UI uses this to show a live
    preview of which lines parsed into what proxies, and which failed
    with errors. Users confirm before hitting bulk-import.

    Body:  { "text": "paste contents", "default_scheme": "http" }
    Reply: { valid: [...], errors: [...], total: N,
             duplicates: [ "url", ... ]  # URLs already in DB }
    """
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    default_scheme = (data.get("default_scheme") or "http").lower()
    if default_scheme not in ("http", "https", "socks5", "socks4"):
        default_scheme = "http"

    try:
        from ghost_shell.proxy.diagnostics import parse_proxy_list
    except ImportError:
        return jsonify({"error": "proxy_diagnostics module missing"}), 500

    parsed = parse_proxy_list(text, default_scheme=default_scheme)

    # Flag URLs that already exist in the library — so the UI can show
    # them in gray ("will be skipped on import").
    db = get_db()
    duplicates = []
    for v in parsed["valid"]:
        existing = db.proxy_get_by_url(v["url"])
        v["duplicate"] = bool(existing)
        if existing:
            duplicates.append(v["url"])

    return jsonify({
        "valid":      parsed["valid"],
        "errors":     parsed["errors"],
        "total":      parsed["total"],
        "duplicates": duplicates,
    })


@app.route("/api/proxies/bulk-import", methods=["POST"])
def api_proxies_bulk_import():
    """Import many proxy lines at once. Accepts two body shapes for
    backward compat:

      NEW:   { "text": "paste contents", "default_scheme": "http",
               "auto_test": true, "skip_duplicates": true }
      LEGACY:{ "urls": ["url1", "url2"], "auto_test": true }

    The text shape goes through the multi-format parser (supports
    host:port, host:port:user:pass, user:pass@host:port, SOCKS, IPv6,
    etc). The legacy urls shape treats every entry as-is.

    Reply adds per-line format info so the UI can display "5 imported
    (2 host:port, 3 host:port:user:pass)".
    """
    data = request.get_json(silent=True) or {}
    db = get_db()

    if data.get("text") is not None:
        # NEW smart-parser path
        try:
            from ghost_shell.proxy.diagnostics import parse_proxy_list
        except ImportError:
            return jsonify({"error": "proxy_diagnostics module missing"}), 500
        parsed = parse_proxy_list(
            data["text"],
            default_scheme=(data.get("default_scheme") or "http").lower(),
        )
        valid_entries = parsed["valid"]
        parse_errors = parsed["errors"]
    else:
        # LEGACY urls path — normalize via parse_proxy_line individually
        # so the rest of the code works uniformly.
        try:
            from ghost_shell.proxy.diagnostics import parse_proxy_line
        except ImportError:
            return jsonify({"error": "proxy_diagnostics module missing"}), 500
        urls = data.get("urls") or []
        if not isinstance(urls, list):
            return jsonify({"error": "urls must be a list"}), 400
        valid_entries = []
        parse_errors = []
        for i, raw in enumerate(urls, 1):
            line = (raw or "").strip()
            if not line:
                continue
            r = parse_proxy_line(line)
            if r and r.get("ok"):
                valid_entries.append(r)
            else:
                parse_errors.append({
                    "line": i, "raw": raw,
                    "error": (r or {}).get("error", "unparseable"),
                })

    created = []
    skipped_dupes = []
    create_errors = []
    fmt_counts = {}

    for v in valid_entries:
        url = v["url"]
        try:
            existing = db.proxy_get_by_url(url)
            if existing:
                skipped_dupes.append(url)
                continue
            # Use the parsed parts directly — proxy_create also parses
            # internally but passing through is cleaner and avoids a
            # second parse round.
            pid = db.proxy_create(
                url=url,
                type=v.get("type"),
                host=v.get("host"),
                port=v.get("port"),
                login=v.get("login") or None,
                password=v.get("password") or None,
            )
            created.append({"id": pid, "url": url})
            fmt = v.get("format", "unknown")
            fmt_counts[fmt] = fmt_counts.get(fmt, 0) + 1
        except Exception as e:
            create_errors.append({"url": url, "error": str(e)})

    if data.get("auto_test", True) and created:
        try:
            from ghost_shell.proxy.diagnostics import test_proxy
            for item in created:
                diag = test_proxy(item["url"], timeout=10)
                db.proxy_record_diagnostics(item["id"], diag)
        except Exception as e:
            logging.warning(f"[proxies] bulk auto-test failed: {e}")

    return jsonify({
        "ok":                 True,
        "created":            len(created),
        "skipped_duplicates": len(skipped_dupes),
        "parse_errors":       len(parse_errors),
        "create_errors":      len(create_errors),
        "format_counts":      fmt_counts,
        # Detailed payload for toast/log — capped at 20 items each
        "parse_error_detail": parse_errors[:20],
        "create_error_detail": create_errors[:20],
    })


# ──────────────────────────────────────────────────────────────
# API: FINGERPRINT COHERENCE SYSTEM
# ──────────────────────────────────────────────────────────────
#
# Endpoints:
#   GET    /api/fingerprint/templates          — list available templates
#   GET    /api/fingerprint/<profile>          — current fp + validation
#   POST   /api/fingerprint/<profile>/generate — make new fingerprint
#   PUT    /api/fingerprint/<profile>          — update fields manually
#   POST   /api/fingerprint/<profile>/validate — re-run validation
#   POST   /api/fingerprint/<profile>/selftest — launch browser, verify
#   GET    /api/fingerprint/<profile>/history  — list snapshots
#   POST   /api/fingerprint/<profile>/activate/<id> — restore from history
#   DELETE /api/fingerprint/<id>               — delete a history entry
#   GET    /api/fingerprints/summary           — aggregate for overview
#

@app.route("/api/fingerprint/templates", methods=["GET"])
def api_fp_templates():
    """List all device templates. UI uses this for the template
    picker dropdown. Returns summary only — full details are in
    fingerprint_templates.py source."""
    from ghost_shell.fingerprint.templates import all_templates
    templates = []
    for t in all_templates():
        templates.append({
            "id":                t["id"],
            "label":              t["label"],
            "category":           t["category"],
            "is_mobile":          bool(t.get("is_mobile")),
            "os":                 t["os"],
            "market_share_pct":   t.get("market_share_pct", 0),
            "chrome_version_range": t["chrome_version_range"],
            "screen_options": [
                {"width": s["width"], "height": s["height"], "dpr": s["dpr"]}
                for s in t["screen_options"]
            ],
            "gpu_vendor":         t["gpu"]["vendor"],
        })
    return jsonify({"templates": templates})


@app.route("/api/fingerprint/<name>", methods=["GET"])
def api_fp_get(name):
    """Current fingerprint for a profile + cached validation.
    Returns null if the profile has no fingerprint yet."""
    db = get_db()
    fp_row = db.fingerprint_current(name)
    if not fp_row:
        return jsonify({"fingerprint": None})
    return jsonify({"fingerprint": fp_row})


@app.route("/api/fingerprint/<name>/generate", methods=["POST"])
def api_fp_generate(name):
    """Generate a new fingerprint for a profile. Body options:
        {
          "template_id":    "macbook_pro_14_m2_2023"  // or null = auto
          "locked_fields":  {"timezone": "Europe/Kyiv"}
          "mode":           "full" | "template_only" | "reshuffle"
          "reason":         "user clicked regenerate"
        }
    Saves the result + runs validation, returns full report.

    mode semantics:
      full          — fresh generation, ignores current fp
      template_only — change only template, keep locked fields
      reshuffle     — same template, different values (for same template
                      but different screen/GPU option)
    """
    from ghost_shell.fingerprint.generator import (
        generate, regenerate_preserving_locks
    )
    from ghost_shell.fingerprint.templates import get_template
    from ghost_shell.fingerprint.validator import validate

    data = request.get_json(silent=True) or {}
    template_id = data.get("template_id")
    locked_fields = data.get("locked_fields") or {}
    mode = data.get("mode", "full")
    reason = data.get("reason") or f"generate mode={mode}"

    db = get_db()

    if mode == "reshuffle":
        current = db.fingerprint_current(name)
        if not current:
            return jsonify({"error": "no current fingerprint to reshuffle"}), 400
        locked_paths = list(locked_fields.keys())
        new_fp = regenerate_preserving_locks(
            current["payload"],
            locked_paths=locked_paths,
            new_template_id=None,   # keep template
        )
    elif mode == "template_only":
        if not template_id:
            return jsonify({"error": "mode=template_only requires template_id"}), 400
        current = db.fingerprint_current(name)
        locked_paths = list(locked_fields.keys())
        new_fp = regenerate_preserving_locks(
            current["payload"] if current else {"generated_for": name},
            locked_paths=locked_paths,
            new_template_id=template_id,
        )
    else:
        # full mode — clean slate
        new_fp = generate(
            profile_name=name,
            template_id=template_id,
            locked_fields=locked_fields,
        )

    # Validate
    template = get_template(new_fp["template_id"])
    runtime_shape = _flat_fp_to_runtime_shape(new_fp)
    validation = validate(runtime_shape, template)

    # Save
    fp_id = db.fingerprint_save(
        name, new_fp,
        coherence_score=validation["score"],
        coherence_report=validation,
        locked_fields=list(locked_fields.keys()),
        source="generated",
        reason=reason,
    )

    return jsonify({
        "ok":         True,
        "id":         fp_id,
        "fingerprint": new_fp,
        "validation": validation,
    })


@app.route("/api/fingerprint/<name>", methods=["PUT", "PATCH"])
def api_fp_update(name):
    """Edit specific fields of a profile's current fingerprint.
    Body: { "patches": {"timezone": "Europe/London", "language": "en-GB"} }
    Re-runs validation and saves as a new snapshot (history
    preserved)."""
    from ghost_shell.fingerprint.templates import get_template
    from ghost_shell.fingerprint.validator import validate

    data = request.get_json(silent=True) or {}
    patches = data.get("patches") or {}
    if not patches:
        return jsonify({"error": "patches is required"}), 400

    db = get_db()
    current = db.fingerprint_current(name)
    if not current:
        return jsonify({"error": "no current fingerprint"}), 404

    # Apply patches — walk dotted paths to nested dicts
    payload = dict(current["payload"])
    for path, value in patches.items():
        keys = path.split(".")
        cur = payload
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value

    # Re-validate
    template = get_template(payload.get("template_id"))
    if not template:
        return jsonify({"error": "template_id invalid or missing in payload"}), 400
    runtime_shape = _flat_fp_to_runtime_shape(payload)
    validation = validate(runtime_shape, template)

    # Preserve locks from previous record
    locks = current.get("locked_fields") or []

    fp_id = db.fingerprint_save(
        name, payload,
        coherence_score=validation["score"],
        coherence_report=validation,
        locked_fields=locks,
        source="manual_edit",
        reason=f"edit {list(patches.keys())}",
    )

    return jsonify({
        "ok":         True,
        "id":         fp_id,
        "fingerprint": payload,
        "validation": validation,
    })


@app.route("/api/fingerprint/<name>/validate", methods=["POST"])
def api_fp_validate(name):
    """Re-run validation on the current fingerprint without changing
    anything. Useful after template data updates or validator rule
    changes."""
    from ghost_shell.fingerprint.templates import get_template
    from ghost_shell.fingerprint.validator import validate

    db = get_db()
    current = db.fingerprint_current(name)
    if not current:
        return jsonify({"error": "no current fingerprint"}), 404
    template = get_template(current["payload"].get("template_id"))
    if not template:
        return jsonify({"error": "template not found"}), 404
    runtime_shape = _flat_fp_to_runtime_shape(current["payload"])
    validation = validate(runtime_shape, template)

    # Update cached score (but don't create new history row)
    conn = db._get_conn()
    conn.execute("""
        UPDATE fingerprints SET
            coherence_score = ?, coherence_report = ?
        WHERE id = ?
    """, (validation["score"], json.dumps(validation), current["id"]))
    conn.commit()

    return jsonify({"ok": True, "validation": validation})


@app.route("/api/fingerprint/<name>/selftest", methods=["POST"])
def api_fp_selftest(name):
    """Launch a real browser with this profile, probe the actual
    fingerprint, compare vs configured. Takes 5-15 seconds."""
    from ghost_shell.fingerprint.selftest import run_selftest

    db = get_db()
    current = db.fingerprint_current(name)
    if not current:
        return jsonify({"error": "no current fingerprint — generate one first"}), 404

    report = run_selftest(name, current["payload"])
    return jsonify(report)


@app.route("/api/fingerprint/<name>/history", methods=["GET"])
def api_fp_history(name):
    limit = int(request.args.get("limit", 30))
    db = get_db()
    return jsonify({
        "profile":   name,
        "history":   db.fingerprints_history(name, limit=limit),
    })


@app.route("/api/fingerprint/<name>/activate/<int:fp_id>", methods=["POST"])
def api_fp_activate(name, fp_id):
    """Restore a historical fingerprint as current. Verifies the
    fingerprint belongs to this profile."""
    db = get_db()
    target = db.fingerprint_get(fp_id)
    if not target or target["profile_name"] != name:
        return jsonify({"error": "fingerprint not found for this profile"}), 404
    ok = db.fingerprint_activate(fp_id)
    return jsonify({"ok": ok, "activated_id": fp_id})


@app.route("/api/fingerprint/entry/<int:fp_id>", methods=["DELETE"])
def api_fp_delete(fp_id):
    """Delete a historical fingerprint (current can't be deleted)."""
    db = get_db()
    try:
        ok = db.fingerprint_delete(fp_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/fingerprint/<name>/mode", methods=["POST"])
def api_fp_switch_mode(name):
    """Switch the profile's active fingerprint between desktop and
    mobile. If a matching-category FP already exists in history,
    reactivate it; otherwise generate a fresh one picking from the
    appropriate template pool.

    Body: { "mode": "desktop" | "mobile" }
    """
    from ghost_shell.fingerprint.generator import generate as _gen_fp
    from ghost_shell.fingerprint.templates import get_template, all_templates
    from ghost_shell.fingerprint.validator import validate as _validate

    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "").lower().strip()
    if mode not in ("desktop", "mobile"):
        return jsonify({"error": "mode must be desktop or mobile"}), 400

    db = get_db()
    history = db.fingerprints_history(name, limit=200)

    # 1. Try to find an existing history entry matching the target mode.
    target = None
    for row in history:
        tmpl = get_template(row.get("template_id") or "")
        if not tmpl: continue
        is_mob = bool(tmpl.get("is_mobile"))
        if (mode == "mobile" and is_mob) or (mode == "desktop" and not is_mob):
            target = row
            break

    if target and not target.get("is_current"):
        # Reactivate the historical FP — fast path, no regeneration
        db.fingerprint_activate(target["id"])
        return jsonify({"ok": True, "mode": mode, "mode_switched": True,
                        "source": "history", "fingerprint_id": target["id"]})
    if target and target.get("is_current"):
        return jsonify({"ok": True, "mode": mode, "mode_switched": False,
                        "note": "already active"})

    # 2. No matching FP in history — generate one.
    mobile_required = (mode == "mobile")
    candidates = [t for t in all_templates()
                  if bool(t.get("is_mobile")) == mobile_required]
    if not candidates:
        return jsonify({"error": f"no {mode} templates available"}), 500

    # Weighted pick by market share
    weights = [t.get("market_share_pct", 1.0) for t in candidates]
    import random as _rand
    chosen = _rand.choices(candidates, weights=weights, k=1)[0]

    fp = _gen_fp(profile_name=name, template_id=chosen["id"])
    tmpl = get_template(fp["template_id"])
    validation = _validate(_flat_fp_to_runtime_shape(fp), tmpl)
    fp_id = db.fingerprint_save(
        name, fp,
        coherence_score=validation["score"],
        coherence_report=validation,
        locked_fields=[],
        source="generated",
        reason=f"dual-mode switch to {mode}",
    )
    return jsonify({"ok": True, "mode": mode, "mode_switched": True,
                    "source": "generated",
                    "fingerprint_id": fp_id,
                    "template_id": chosen["id"]})


@app.route("/api/fingerprints/summary", methods=["GET"])
def api_fp_summary():
    """Aggregate report for Overview — every profile's fingerprint
    score + template + history count."""
    db = get_db()
    return jsonify({"profiles": db.fingerprints_aggregate()})


def _flat_fp_to_runtime_shape(fp: dict) -> dict:
    """Convert generator's flat fingerprint dict to the nested shape
    the validator expects (matches what JS probe returns). DRY helper
    used by multiple endpoints."""
    return {
        "navigator": {
            "userAgent":          fp.get("user_agent"),
            "platform":           fp.get("platform"),
            "hardwareConcurrency": fp.get("hardware_concurrency"),
            "deviceMemory":       fp.get("device_memory"),
            "maxTouchPoints":     fp.get("max_touch_points"),
            "language":           fp.get("language"),
            "vendor":             fp.get("vendor"),
            "webdriver":          fp.get("webdriver", False),
        },
        "screen": {
            "width":  fp.get("screen", {}).get("width"),
            "height": fp.get("screen", {}).get("height"),
        },
        "window": {"devicePixelRatio": fp.get("dpr")},
        "webgl":   fp.get("webgl", {}),
        "timezone": {"intl": fp.get("timezone")},
        "fonts":   fp.get("fonts", []),
        "audio":   {"sampleRate": fp.get("audio_sample_rate")},
    }




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


def _shutdown_reap_all_runs(reason: str = "dashboard shutdown"):
    """Kill every live run's process tree. Called from atexit and
    signal handlers so Ctrl+C / killed terminal doesn't leave orphan
    Chrome + chromedriver + main.py processes running.

    The previous bug: closing the terminal killed only the Flask
    process. The per-run threads were daemons (good — they die with
    the process) but the subprocess.Popen children they spawned are
    NOT automatically killed when their Python parent dies on Windows.
    So main.py kept running, kept driving Chrome, kept writing to
    locked profile folders, and a follow-up dashboard launch couldn't
    touch those profiles until the user manually killed every
    chrome.exe / chromedriver.exe / python.exe left over.

    Now: we enumerate every RUNNER_POOL slot that has a live PID and
    kill each tree via process_reaper. Fast — one iteration usually
    completes in <1s. If we can't reach process_reaper (import fail)
    we fall back to Popen.terminate() per slot, which kills main.py
    itself. main.py has its own shutdown handlers that kill Chrome,
    so even the fallback cleanly propagates.
    """
    try:
        slots = RUNNER_POOL.all_slots()
    except Exception:
        return

    active_pids = []
    for slot_dict in slots:
        if not slot_dict.get("is_running"):
            continue
        run_id = slot_dict.get("run_id")
        slot = RUNNER_POOL.get(run_id) if run_id is not None else None
        proc = getattr(slot, "process", None) if slot else None
        pid = getattr(proc, "pid", None) if proc else None
        if pid:
            active_pids.append((run_id, pid))

    if not active_pids:
        return

    # Print to stderr rather than logging.info — at shutdown the log
    # handlers may already be half-closed.
    sys.stderr.write(
        f"\n[shutdown] {reason}: killing {len(active_pids)} live run(s) "
        f"and their Chrome trees...\n"
    )
    sys.stderr.flush()

    try:
        from ghost_shell.core.process_reaper import kill_process_tree
        for run_id, pid in active_pids:
            try:
                kill_process_tree(pid, reason=f"dashboard shutdown (run #{run_id})")
                # Mark in DB so the next dashboard start doesn't try
                # to treat these runs as "still going"
                try:
                    get_db().run_finish(run_id, exit_code=-1,
                                        error="dashboard shutdown")
                except Exception:
                    pass
            except Exception as e:
                sys.stderr.write(f"  couldn't kill run #{run_id} pid={pid}: {e}\n")
    except ImportError:
        # process_reaper not importable (no psutil?) — fall back to
        # Popen.terminate. Less thorough: terminates main.py which in
        # turn kills its Chrome via atexit handlers. Not bulletproof
        # if main.py is wedged, but better than leaving zombies.
        for run_id, _pid in active_pids:
            slot = RUNNER_POOL.get(run_id)
            proc = getattr(slot, "process", None) if slot else None
            if proc is None:
                continue
            try:
                proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# Install shutdown handlers. These fire on:
#   - normal interpreter exit (atexit)
#   - Ctrl+C → SIGINT
#   - kill / terminal close → SIGTERM (Unix), CTRL_BREAK_EVENT (Windows)
#
# Flask's dev server handles SIGINT for its own shutdown, but the
# children we spawned don't see that signal unless we explicitly
# propagate. Hence the atexit hook — it fires after Flask has stopped
# serving, right before the interpreter exits.
import atexit
import signal

_shutdown_once = {"fired": False}

def _handle_signal(signum, frame):
    # Guard against double-fire (SIGINT arriving while atexit is
    # already running, for example). Only do the reap once per process.
    if _shutdown_once["fired"]:
        return
    _shutdown_once["fired"] = True
    signame = {
        signal.SIGINT:  "SIGINT (Ctrl+C)",
        signal.SIGTERM: "SIGTERM",
    }.get(signum, f"signal {signum}")
    _shutdown_reap_all_runs(reason=signame)
    # Re-raise the default behaviour so Flask actually stops. atexit
    # will fire too but short-circuit via the _shutdown_once guard.
    sys.exit(130 if signum == signal.SIGINT else 143)

def _atexit_handler():
    if _shutdown_once["fired"]:
        return
    _shutdown_once["fired"] = True
    _shutdown_reap_all_runs(reason="interpreter exit")

atexit.register(_atexit_handler)
try:
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    # SIGBREAK is Windows-only (Ctrl+Break / console-close).
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_signal)
except (ValueError, OSError):
    # signal.signal can fail if we're not on the main thread — e.g.
    # when dashboard_server is imported for testing rather than run
    # directly. atexit still covers us in that case.
    pass


# ══════════════════════════════════════════════════════════════
# API: SESSION & WARMUP
# ══════════════════════════════════════════════════════════════
#
# Per-profile warmup robot + cookie snapshot pool. See:
#   ghost_shell/session/warmup.py       — the engine
#   ghost_shell/session/site_presets.py — site libraries
#   ghost_shell/session/cookie_pool.py  — snapshot freeze/restore
#
# Endpoints:
#   GET  /api/warmup/presets                       — list presets
#   GET  /api/session/<profile>                    — status rollup
#   POST /api/warmup/<profile>/run                 — trigger warmup now
#   GET  /api/warmup/<profile>/history             — past runs
#   GET  /api/snapshots/<profile>                  — cookie snapshots list
#   POST /api/snapshots/<profile>                  — manual snapshot (requires live run; TBD)
#   DELETE /api/snapshots/entry/<id>               — delete a snapshot
#   POST /api/snapshots/<profile>/<id>/restore     — mark for restore on next launch

import threading as _threading

# In-memory record of the currently-running warmup per profile. Keeps
# the API response /api/warmup/<p>/run idempotent (a second POST while
# a warmup is in progress returns 409 instead of spawning a second one).
_active_warmups: dict = {}
_active_warmups_lock = _threading.Lock()


@app.route("/api/warmup/presets", methods=["GET"])
def api_warmup_presets():
    from ghost_shell.session.site_presets import list_presets
    return jsonify({"presets": list_presets()})


@app.route("/api/session/<name>", methods=["GET"])
def api_session_status(name):
    """Rollup for the Session page header: last warmup, cookie count,
    snapshot count, last snapshot timestamp."""
    db = get_db()
    last = db.warmup_last(name)
    stats = db.snapshot_stats(name)
    with _active_warmups_lock:
        running = name in _active_warmups
    return jsonify({
        "profile":     name,
        "warmup": {
            "last":    last,
            "running": running,
        },
        "snapshots":   stats,
    })


@app.route("/api/warmup/<name>/run", methods=["POST"])
def api_warmup_run(name):
    """Kick off a warmup in a background thread. Returns immediately
    with the warmup_id; client polls /api/session/<name> for progress."""
    data = request.get_json(silent=True) or {}
    preset = data.get("preset", "general")
    sites  = int(data.get("sites", 7))
    trigger = data.get("trigger", "manual")

    with _active_warmups_lock:
        if name in _active_warmups:
            return jsonify({"error": "warmup already running for this profile",
                            "warmup_id": _active_warmups[name]}), 409

    def _runner():
        try:
            from ghost_shell.session.warmup import run_warmup
            run_warmup(name, preset=preset, sites=sites, trigger=trigger)
        except Exception as e:
            logging.error(f"[api_warmup_run] engine error: {e}", exc_info=True)
        finally:
            with _active_warmups_lock:
                _active_warmups.pop(name, None)

    # Create the DB row eagerly so the first status-poll sees "running"
    # rather than racing the background thread's insert.
    from ghost_shell.session.site_presets import pick_sites as _pick
    planned = len(_pick(preset, sites, seed=f"{name}:{int(time.time())}"))
    wid = get_db().warmup_start(name, preset, planned, trigger)
    with _active_warmups_lock:
        _active_warmups[name] = wid

    # Override the eager row as the engine re-creates on its own; the
    # UI won't notice because the duplicate is quickly obscured by the
    # real one. Simpler than passing wid into the thread.
    try:
        get_db()._get_conn().execute(
            "UPDATE warmup_runs SET status = 'superseded' WHERE id = ?",
            (wid,)
        )
        get_db()._get_conn().commit()
    except Exception:
        pass

    t = _threading.Thread(target=_runner, name=f"warmup-{name}", daemon=True)
    t.start()
    return jsonify({"ok": True, "warmup_id": wid})


@app.route("/api/warmup/<name>/history", methods=["GET"])
def api_warmup_history(name):
    limit = int(request.args.get("limit", 30))
    return jsonify({"profile": name,
                    "history": get_db().warmup_history(name, limit=limit)})


@app.route("/api/snapshots/<name>", methods=["GET"])
def api_snapshots_list(name):
    limit = int(request.args.get("limit", 50))
    return jsonify({"profile": name,
                    "snapshots": get_db().snapshot_list(name, limit=limit),
                    "stats":     get_db().snapshot_stats(name)})


@app.route("/api/snapshots/entry/<int:sid>", methods=["GET"])
def api_snapshot_get(sid):
    """Return a single snapshot with full cookie + storage payload.
    Used by the UI for 'view details' / inspection."""
    s = get_db().snapshot_get(sid)
    if not s:
        return jsonify({"error": "not found"}), 404
    return jsonify(s)


@app.route("/api/snapshots/entry/<int:sid>", methods=["DELETE"])
def api_snapshot_delete(sid):
    ok = get_db().snapshot_delete(sid)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/snapshots/<name>/<int:sid>/restore", methods=["POST"])
def api_snapshot_restore(name, sid):
    """Mark a snapshot as 'pending restore on next launch'. We don't
    restore directly here — the next browser start will read this
    marker from config_kv and inject. See ghost_shell/session/cookie_pool.restore_to_driver.
    """
    snap = get_db().snapshot_get(sid)
    if not snap or snap.get("profile_name") != name:
        return jsonify({"error": "snapshot not found for this profile"}), 404
    get_db().config_set(f"session.pending_restore.{name}", str(sid))
    return jsonify({"ok": True, "snapshot_id": sid,
                    "note": "will be injected on next launch of this profile"})


# ══════════════════════════════════════════════════════════════
# API: VAULT & GENERIC SECRET ITEMS
# ══════════════════════════════════════════════════════════════
#
# Encrypted storage for credentials, crypto wallets, API keys, TOTP
# secrets and arbitrary secure notes. The vault lives in
# ghost_shell.accounts.vault — unlocked with a master password held
# in-memory only. See .../accounts/kinds.py for per-kind field schemas.
#
# Endpoints:
#   GET  /api/vault/status              — initialized + unlocked flags
#   POST /api/vault/initialize          — first-time setup, body: { master_password }
#   POST /api/vault/unlock               — body: { master_password }
#   POST /api/vault/lock                 — forget the in-memory key
#   POST /api/vault/reset                — DESTRUCTIVE, body: { master_password }
#   GET  /api/vault/kinds               — list of supported item kinds + their fields
#   GET  /api/vault/items               — list (filterable), metadata only
#   POST /api/vault/items               — create an item
#   GET  /api/vault/items/<id>          — fetch single item WITH decrypted secrets
#   PUT  /api/vault/items/<id>          — partial update
#   DELETE /api/vault/items/<id>        — delete
#   POST /api/vault/items/<id>/status   — set status (active/banned/locked/...)
#   GET  /api/vault/items/<id>/totp     — current 6-digit TOTP code
#
# A note on secrets: list endpoints return metadata ONLY (no ciphertext,
# no decryption). Full decryption is opt-in through the single-item GET
# — that keeps accidental-leak surface small.


@app.route("/api/vault/status", methods=["GET"])
def api_vault_status():
    from ghost_shell.accounts import get_vault
    v = get_vault()
    return jsonify({
        "initialized": v.is_initialized(),
        "unlocked":    v.is_unlocked(),
    })


@app.route("/api/vault/initialize", methods=["POST"])
def api_vault_initialize():
    from ghost_shell.accounts import get_vault
    data = request.get_json(silent=True) or {}
    master = data.get("master_password") or ""
    try:
        get_vault().initialize(master)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True, "unlocked": True})


@app.route("/api/vault/unlock", methods=["POST"])
def api_vault_unlock():
    from ghost_shell.accounts import get_vault
    data = request.get_json(silent=True) or {}
    master = data.get("master_password") or ""
    try:
        get_vault().unlock(master)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "unlocked": True})


@app.route("/api/vault/lock", methods=["POST"])
def api_vault_lock():
    from ghost_shell.accounts import get_vault
    get_vault().lock()
    return jsonify({"ok": True, "unlocked": False})


@app.route("/api/vault/reset", methods=["POST"])
def api_vault_reset():
    """Destructive — wipes all encrypted material + master. Caller must
    pass the current master (when initialized) to prevent accidental wipes."""
    from ghost_shell.accounts import get_vault
    v = get_vault()
    data = request.get_json(silent=True) or {}
    if v.is_initialized():
        try:
            v.unlock(data.get("master_password") or "")
        except PermissionError:
            return jsonify({"error": "current master password required to reset"}), 401
    v.reset()
    return jsonify({"ok": True})


@app.route("/api/vault/kinds", methods=["GET"])
def api_vault_kinds():
    """Per-kind schema for the Add/Edit form."""
    from ghost_shell.accounts.kinds import KINDS
    return jsonify({"kinds": KINDS})


@app.route("/api/vault/items", methods=["GET"])
def api_vault_items_list():
    db = get_db()
    items = db.vault_list(
        kind=request.args.get("kind") or None,
        service=request.args.get("service") or None,
        status=request.args.get("status") or None,
        profile_name=request.args.get("profile_name") or None,
        search=request.args.get("q") or None,
    )
    return jsonify({
        "items":         items,
        "by_kind":       db.vault_count_by_kind(),
        "by_status":     db.vault_count_by_status(),
    })


@app.route("/api/vault/items", methods=["POST"])
def api_vault_items_create():
    from ghost_shell.accounts import add_item, VaultLockedError
    data = request.get_json(silent=True) or {}
    try:
        new_id = add_item(
            name=data.get("name") or "",
            kind=data.get("kind") or "account",
            service=data.get("service") or None,
            identifier=data.get("identifier") or None,
            secrets=data.get("secrets") or None,
            profile_name=data.get("profile_name") or None,
            status=data.get("status") or "active",
            tags=data.get("tags") or None,
            notes=data.get("notes") or None,
        )
    except VaultLockedError:
        return jsonify({"error": "vault is locked"}), 423
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/vault/items/<int:item_id>", methods=["GET"])
def api_vault_item_get(item_id):
    from ghost_shell.accounts import get_item_cleartext, VaultLockedError
    try:
        item = get_item_cleartext(item_id)
    except VaultLockedError:
        return jsonify({"error": "vault is locked"}), 423
    if not item:
        return jsonify({"error": "not found"}), 404
    return jsonify(item)


@app.route("/api/vault/items/<int:item_id>", methods=["PUT", "PATCH"])
def api_vault_item_update(item_id):
    from ghost_shell.accounts import update_item, VaultLockedError
    data = request.get_json(silent=True) or {}
    # Sentinel: if "secrets" key is absent, leave blob untouched
    kw = {k: data[k] for k in ("name","kind","service","identifier",
                               "profile_name","status","tags","notes")
          if k in data}
    if "secrets" in data:
        kw["secrets"] = data["secrets"]
    try:
        ok = update_item(item_id, **kw)
    except VaultLockedError:
        return jsonify({"error": "vault is locked"}), 423
    if not ok:
        return jsonify({"error": "no change"}), 400
    return jsonify({"ok": True})


@app.route("/api/vault/items/<int:item_id>", methods=["DELETE"])
def api_vault_item_delete(item_id):
    from ghost_shell.accounts import delete_item
    if not delete_item(item_id):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/vault/items/<int:item_id>/status", methods=["POST"])
def api_vault_item_status(item_id):
    from ghost_shell.accounts import set_status
    data = request.get_json(silent=True) or {}
    st = data.get("status")
    if not st:
        return jsonify({"error": "status is required"}), 400
    if not set_status(item_id, st, login_status=data.get("login_status")):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/vault/items/<int:item_id>/totp", methods=["GET"])
def api_vault_item_totp(item_id):
    from ghost_shell.accounts import totp_code, VaultLockedError
    try:
        code = totp_code(item_id)
    except VaultLockedError:
        return jsonify({"error": "vault is locked"}), 423
    if code is None:
        return jsonify({"error": "no TOTP secret stored for this item"}), 404
    return jsonify(code)


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
    print("╔" + "═"*58 + "╗")
    print("║  Ghost Shell Dashboard                                    ║")
    print(f"║  → {url:<55}║")
    print("║  Ctrl+C to stop                                           ║")
    print("╚" + "═"*58 + "╝" + "\n")

    # ────────────────────────────────────────────────────────────
    # Runtime metadata for the installer / external supervisors.
    #
    # Writes %LOCALAPPDATA%\GhostShellAnty\runtime.json with our PID,
    # bind port, and a one-shot shutdown token. The Inno Setup updater
    # reads this file to (a) confirm the dashboard is running and (b)
    # call POST /api/admin/shutdown gracefully before replacing files.
    #
    # The file is removed on graceful exit, but if we crash it sticks
    # around — that's why is_pid_alive() is used on the installer side
    # to disambiguate "stale file" from "really running".
    # ────────────────────────────────────────────────────────────
    try:
        info = gs_runtime.write_runtime_info(
            port=port,
            install_dir=PROJECT_ROOT,
        )
        _SHUTDOWN_TOKEN = info["shutdown_token"]
        print(f"[startup] runtime info → {gs_runtime.runtime_path('runtime.json')}")

        import atexit as _atexit
        _atexit.register(gs_runtime.clear_runtime_info)
    except Exception as e:
        # Non-fatal: dashboard still serves even if we can't write the
        # runtime file. The installer will fall back to taskkill on PID
        # discovery via process enumeration.
        print(f"[startup] couldn't write runtime info: {e}")

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

    # Final hand-off to Flask's dev server. Threaded so multiple HTTP
    # clients (dashboard tabs + SSE streams) can be served concurrently.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
