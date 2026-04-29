"""
scheduler.py — Standalone scheduler for the monitor (multi-profile).

Launches main.py as a subprocess N times per day, cycling through a set
of profiles selected in the dashboard. Reads all config live from DB
(config_kv), writes a heartbeat every iteration so the dashboard can
show "running since X — Y runs today" without inspecting processes.

Config keys (all editable on the Scheduler dashboard page):

    scheduler.target_runs_per_day      int          (default 30)
    scheduler.active_hours             [h1, h2]     (default [7, 20])
    scheduler.min_interval_sec         int          (default 180)
    scheduler.max_interval_sec         int          (default 1200)
    scheduler.jitter_percent           int          (default 25)
    scheduler.max_consecutive_fails    int          (default 5)
    scheduler.fail_pause_sec           int          (default 1800)
    scheduler.profile_names            [str]        (empty → use browser.profile_name)
    scheduler.selection_mode           "random" | "round-robin"

Dashboard-written status keys (read-only from here):

    scheduler.heartbeat_at             iso string
    scheduler.started_at               iso string
    scheduler.last_run_profile         str
    scheduler.next_run_at              iso string

Usage:
    python scheduler.py          # interactive
    python scheduler.py --quiet  # no stdout (dashboard spawns it that way)
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
from ghost_shell.core.platform_paths import PROJECT_ROOT
import sys
import time
import random
import signal
import logging
import subprocess
from datetime import datetime, timedelta, time as dtime
from typing import Optional

from ghost_shell.db.database import get_db
from ghost_shell.core import runtime as gs_runtime


# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
#
# Same Windows cp1252 stdout quirk as main.py — force UTF-8 so emoji
# and Cyrillic don't crash StreamHandler. See main.py comment block
# for details.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scheduler.log", encoding="utf-8"),
    ],
)


# ──────────────────────────────────────────────────────────────
# Config loader
# ──────────────────────────────────────────────────────────────

def load_cfg():
    db = get_db()
    return {
        "target_runs":      db.config_get("scheduler.target_runs_per_day") or 30,
        "active_hours":     db.config_get("scheduler.active_hours") or [7, 20],
        "min_interval":     db.config_get("scheduler.min_interval_sec") or 180,
        "max_interval":     db.config_get("scheduler.max_interval_sec") or 1200,
        "jitter_percent":   db.config_get("scheduler.jitter_percent") or 25,
        "max_fails_in_row": db.config_get("scheduler.max_consecutive_fails") or 5,
        "fail_pause_sec":   db.config_get("scheduler.fail_pause_sec") or 1800,
        "profile_names":    db.config_get("scheduler.profile_names") or [],
        "selection_mode":   db.config_get("scheduler.selection_mode") or "random",
        "default_profile":  db.config_get("browser.profile_name") or "profile_01",
        # Group-based trigger — when set, the scheduler launches the
        # group's members as a concurrent batch instead of picking one
        # profile at a time. Takes precedence over profile_names.
        "group_id":         db.config_get("scheduler.group_id"),
        # Whether a group launch should wait for ALL members to finish
        # before counting the "iteration" done (serial group) or just
        # fire-and-forget (parallel group, default).
        "group_mode":       db.config_get("scheduler.group_mode") or "parallel",
        # Schedule mode — determines how the interval between runs is
        # computed. "density" = the legacy density-based random interval
        # (N runs spread across active window). "interval" = a fixed
        # gap between runs. "cron" = fire on cron-expression matches.
        "schedule_mode":    db.config_get("scheduler.schedule_mode") or "density",
        "interval_sec":     db.config_get("scheduler.interval_sec") or 600,
        "cron_expression":  db.config_get("scheduler.cron_expression") or "",
        # Active days of week. Stored as a list of weekday numbers:
        # Mon=1..Sun=7 (ISO). [] or None = every day (legacy behavior).
        "active_days":      db.config_get("scheduler.active_days") or [],
    }


# ──────────────────────────────────────────────────────────────
# Heartbeat (so dashboard can see we're alive)
# ──────────────────────────────────────────────────────────────

def heartbeat(state: dict = None):
    db = get_db()
    db.config_set("scheduler.heartbeat_at",
                  datetime.now().isoformat(timespec="seconds"))
    if state:
        for k, v in state.items():
            db.config_set(f"scheduler.{k}", v)


def mark_stopped():
    db = get_db()
    # Remove heartbeat so dashboard treats it as stopped
    db.config_set("scheduler.heartbeat_at", None)
    db.config_set("scheduler.next_run_at", None)


# ──────────────────────────────────────────────────────────────
# DB-derived runtime state
# ──────────────────────────────────────────────────────────────

def runs_today() -> int:
    """Sprint 10.1 (SC-06): tz-tolerant day boundary. Previously we
    matched ``started_at LIKE 'YYYY-MM-DD%'`` against the local
    ``today`` string — works only because ``started_at`` is written
    in local-naive form. The strftime form normalises the row to
    local-tz date and compares to local ``today``. We keep the LIKE
    branch as a fallback so pre-migration rows still match."""
    today = datetime.now().strftime("%Y-%m-%d")
    row = get_db()._get_conn().execute(
        """SELECT COUNT(*) AS n FROM runs
           WHERE strftime('%Y-%m-%d', started_at, 'localtime') = ?
              OR substr(started_at, 1, 10) = ?""",
        (today, today),
    ).fetchone()
    return row["n"] if row else 0


def consecutive_failures() -> int:
    """Count consecutive failed runs since the CURRENT scheduler session
    started. Failures from prior sessions (e.g. before the user fixed
    a proxy) no longer count -- otherwise we used to deadlock: counter
    >= max_fails_in_row -> immediate pause -> can't get to a success
    that resets it.

    A run "counts" if started_at >= scheduler.started_at AND finished
    with non-zero exit_code. Counting walks backward from the most
    recent run, stopping at the first success (or session boundary).
    """
    db = get_db()
    started_at = db.config_get("scheduler.started_at") or ""
    rows = db._get_conn().execute(
        "SELECT id, profile_name, exit_code, error, started_at, finished_at "
        "FROM runs WHERE finished_at IS NOT NULL "
        "AND started_at >= ? "
        "ORDER BY id DESC LIMIT 200  -- Sprint 10.1 SC-11: was 20",
        (started_at,),
    ).fetchall()
    count = 0
    failed_rows = []
    for r in rows:
        if r["exit_code"] not in (0, None):
            count += 1
            failed_rows.append(dict(r))
        else:
            break
    # Verbose logging on first call per tick that finds failures, so
    # the user knows WHICH runs are blocking.
    if failed_rows:
        for fr in failed_rows[:5]:
            logging.warning(
                f"   - failed run id={fr['id']} profile={fr['profile_name']} "
                f"exit={fr['exit_code']} at={fr['finished_at']} "
                f"error={(fr.get('error') or '')[:120]}"
            )
    return count


# ──────────────────────────────────────────────────────────────
# Profile picker
# ──────────────────────────────────────────────────────────────

_round_robin_idx = 0


def _load_rr_idx() -> int:
    """Sprint 10.1 (SC-10): persist round-robin position across
    Stop+Start cycles. Without this a mid-day restart rewinds to
    profile #0 — a 'fair rotation' no longer rotates fairly."""
    try:
        v = get_db().config_get("scheduler.round_robin_idx")
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _save_rr_idx(idx: int) -> None:
    try:
        get_db().config_set("scheduler.round_robin_idx", int(idx))
    except Exception:
        pass


def pick_profile(cfg: dict) -> str:
    """Single-profile picker — kept for back-compat with any older code
    paths. New iterations use pick_batch() so they can spawn a group
    at once. If a group_id is set, returns the group's *first* member
    to keep callers returning strings."""
    names = pick_batch(cfg)
    return names[0] if names else cfg["default_profile"]


def pick_batch(cfg: dict) -> list:
    """Return the list of profile names to run in the next iteration.

    Priority:
      1. scheduler.group_id (if set) — returns ALL members of the group,
         so the iteration spawns a concurrent batch
      2. scheduler.profile_names — picks ONE using selection_mode
      3. Falls back to browser.profile_name default — ONE profile
    """
    global _round_robin_idx

    # Group-based triggering
    group_id = cfg.get("group_id")
    if group_id:
        try:
            g = get_db().group_get(int(group_id))
            if g and g.get("members"):
                return list(g["members"])
        except Exception as e:
            logging.warning(
                f"Failed to resolve group {group_id} → falling back to profile_names: {e}"
            )

    pool = cfg["profile_names"]
    if not pool:
        return [cfg["default_profile"]]
    if cfg["selection_mode"] == "round-robin":
        # Sprint 10.1 (SC-10): index is persisted, restored on every
        # call. Stop+Start mid-day no longer rewinds rotation to 0.
        global _round_robin_idx
        if _round_robin_idx == 0:
            _round_robin_idx = _load_rr_idx()
        name = pool[_round_robin_idx % len(pool)]
        _round_robin_idx += 1
        _save_rr_idx(_round_robin_idx)
        return [name]
    return [random.choice(pool)]


# ──────────────────────────────────────────────────────────────
# Time helpers
# ──────────────────────────────────────────────────────────────

def is_active_time(active_hours: list) -> bool:
    h_start, h_end = active_hours
    now = datetime.now().time()
    return dtime(h_start) <= now < dtime(h_end)


def time_until_next_window(active_hours: list) -> float:
    h_start, _ = active_hours
    now    = datetime.now()
    target = now.replace(hour=h_start, minute=0, second=0, microsecond=0)
    if now.time() >= dtime(h_start):
        target += timedelta(days=1)
    return max(60, (target - now).total_seconds())


def minutes_remaining_today(active_hours: list) -> float:
    _, h_end = active_hours
    now      = datetime.now()
    end_time = now.replace(hour=h_end, minute=0, second=0, microsecond=0)
    return max(0, (end_time - now).total_seconds() / 60)


def calc_interval(cfg: dict, done_today: int) -> float:
    remaining_runs = max(1, cfg["target_runs"] - done_today)
    remaining_min  = minutes_remaining_today(cfg["active_hours"])
    if remaining_min <= 0:
        return 0

    avg_sec  = (remaining_min * 60) / remaining_runs
    # Base jitter 50–150%
    interval = avg_sec * random.uniform(0.5, 1.5)
    # Plus extra jitter_percent on top
    jp = cfg["jitter_percent"] / 100.0
    interval *= random.uniform(1 - jp, 1 + jp)

    interval = max(cfg["min_interval"], min(cfg["max_interval"], interval))

    fails = consecutive_failures()
    if fails > 0:
        backoff = min(8, 2 ** fails)
        interval *= backoff
        logging.warning(f"Backoff x{backoff} (consecutive fails: {fails})")
    return interval


def is_active_day(active_days: list) -> bool:
    """True if today is in the permitted weekday list. Empty = any day.
    active_days stores ISO weekday numbers: Mon=1, Sun=7."""
    if not active_days:
        return True
    today = datetime.now().isoweekday()   # 1..7
    return today in active_days


def time_until_next_active_day(active_days: list) -> float:
    """Seconds until the start of the next active-day at 00:00 local."""
    now = datetime.now()
    for delta in range(1, 8):
        dt = (now + timedelta(days=delta)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if dt.isoweekday() in active_days:
            return (dt - now).total_seconds()
    return 0


def next_fire_delay(cfg: dict, done_today: int) -> float:
    """Return seconds until the next scheduled fire, respecting schedule_mode.

    density  → legacy calc_interval (spreads target_runs over active_hours)
    interval → fixed gap cfg["interval_sec"], backoff on fails
    cron     → delta to next match of cfg["cron_expression"]
    """
    mode = (cfg.get("schedule_mode") or "density").lower()

    if mode == "cron":
        from ghost_shell.scheduler.cron import next_fire
        expr = (cfg.get("cron_expression") or "").strip()
        if not expr:
            logging.warning("cron mode without expression — falling back to density")
        else:
            try:
                nxt = next_fire(expr, datetime.now())
            except Exception as e:
                logging.error(f"cron parse error: {e} — falling back to density")
                nxt = None
            if nxt:
                return max(1.0, (nxt - datetime.now()).total_seconds())

    if mode == "interval":
        interval = float(cfg.get("interval_sec") or 600)
        # Apply the same consecutive-fail backoff as density mode so
        # a broken run doesn't hammer the same minute forever.
        fails = consecutive_failures()
        if fails > 0:
            backoff = min(8, 2 ** fails)
            interval *= backoff
            logging.warning(f"Backoff x{backoff} (consecutive fails: {fails})")
        return max(30.0, interval)

    # Default / density
    return calc_interval(cfg, done_today)


# ──────────────────────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────────────────────

_shutdown = False

def _signal_handler(signum, frame):
    global _shutdown
    logging.info("🛑 Signal received — stopping after current iteration")
    _shutdown = True

signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def sleep_interruptible(seconds: float):
    end = time.time() + seconds
    while time.time() < end and not _shutdown:
        time.sleep(min(5, max(0.1, end - time.time())))
        heartbeat()


# ──────────────────────────────────────────────────────────────
# Run one iteration — DASHBOARD-ROUTED
#
# Previously spawned main.py directly via subprocess.Popen. That worked
# but the resulting runs were invisible to the dashboard:
#   - didn't appear in the Logs page live SSE stream
#   - didn't show up in the sidebar active-runs panel
#   - only left a row in the `runs` table (visible on the Runs page)
#
# We now POST to /api/run/start instead. The dashboard handles spawning,
# so its SSE broadcaster captures every line of output and the whole
# UI lights up as though you'd clicked Start in the browser.
#
# If the dashboard isn't running we fall back to the old direct-Popen
# path so the scheduler still works standalone (e.g. on a headless
# server with no browser UI).
# ──────────────────────────────────────────────────────────────

DASHBOARD_BASE_URL = os.environ.get(
    "GHOST_SHELL_DASHBOARD_URL", "http://127.0.0.1:5000"
)


def _spawn_via_dashboard(profile_name: str) -> Optional[int]:
    """Ask the dashboard to spawn a run. Returns the run_id on success,
    None if the dashboard wasn't reachable (caller should fall back to
    direct Popen). Never raises."""
    import json as _json
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(
            f"{DASHBOARD_BASE_URL}/api/run/start",
            data=_json.dumps({"profile_name": profile_name}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            body = _json.loads(r.read().decode("utf-8"))
            return body.get("run_id")
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # Sprint 10.1 (RP-01): manual run already active for this
            # profile. Return sentinel 0 so the caller treats it as
            # skip-not-fail (vs None which falls through to direct
            # Popen). Avoids spurious consecutive-failure counts.
            logging.info(
                f"[scheduler] dashboard rejected spawn (409) — "
                f"manual run already active for {profile_name!r}, skipping"
            )
            return 0
        logging.warning(f"[scheduler] dashboard spawn HTTP {e.code}: {e.reason}")
        return None
    except urllib.error.URLError:
        return None   # dashboard offline
    except Exception as e:
        logging.warning(f"[scheduler] dashboard spawn failed: {e}")
        return None


def _wait_for_run_via_dashboard(run_id: int, timeout: int = 30 * 60) -> tuple:
    """Poll /api/run/status until the run finishes or times out.
    Returns (exit_code, duration_sec).

    Sprint 10.1 (SC-08): bound consecutive poll failures so a flaky
    dashboard doesn't wedge us for a full 30min on a single run.

    Sprint 10.1 (SC-07): after the timeout-triggered stop POST,
    verify the run is actually dead via one final status check.
    If still alive, escalate to /api/admin/reap-zombies (taskkill
    /F /T) so the user-data-dir doesn't stay locked."""
    import json as _json
    import urllib.request

    started = time.time()
    poll_interval = 5
    consecutive_poll_fails = 0
    POLL_FAIL_BUDGET = 12  # ~60s of failed polls before bailing

    while time.time() - started < timeout:
        if _shutdown:
            return -1, time.time() - started
        try:
            with urllib.request.urlopen(
                f"{DASHBOARD_BASE_URL}/api/run/status?run_id={run_id}",
                timeout=5,
            ) as r:
                status = _json.loads(r.read().decode("utf-8"))
            consecutive_poll_fails = 0
            if not status.get("running"):
                return status.get("exit_code", 0) or 0, time.time() - started
        except Exception as e:
            consecutive_poll_fails += 1
            logging.debug(
                f"[scheduler] status poll error "
                f"({consecutive_poll_fails}/{POLL_FAIL_BUDGET}): {e}"
            )
            if consecutive_poll_fails >= POLL_FAIL_BUDGET:
                logging.error(
                    f"[scheduler] dashboard unreachable for run {run_id} "
                    f"after {consecutive_poll_fails} polls — giving up; "
                    f"run may still be alive"
                )
                return -2, time.time() - started
        time.sleep(poll_interval)

    # Timeout — ask dashboard to kill the run.
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                f"{DASHBOARD_BASE_URL}/api/run/{run_id}/stop",
                method="POST",
            ),
            timeout=5,
        )
    except Exception:
        pass

    # Verify dead (SC-07)
    final_running = True
    for _ in range(3):  # ~6s grace
        time.sleep(2)
        try:
            with urllib.request.urlopen(
                f"{DASHBOARD_BASE_URL}/api/run/status?run_id={run_id}",
                timeout=3,
            ) as r:
                final = _json.loads(r.read().decode("utf-8"))
            if not final.get("running"):
                final_running = False
                break
        except Exception:
            continue
    if final_running:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{DASHBOARD_BASE_URL}/api/admin/reap-zombies",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=10,
            )
            logging.warning(
                f"[scheduler] run {run_id} survived stop POST — "
                f"escalated to reap-zombies"
            )
        except Exception as e:
            logging.error(
                f"[scheduler] run {run_id} timed out AND reap-zombies "
                f"failed: {e}"
            )
    logging.error(f"[scheduler] run {run_id} timed out after {timeout}s")
    return -1, time.time() - started


def run_one_iteration(profile_name: str) -> tuple:
    """Launch one main.py instance for the given profile.
    Prefers dashboard-routed spawn (gives live logs + run tracking),
    falls back to direct subprocess if dashboard is offline."""
    started = time.time()

    # Try dashboard route first — gives us full SSE integration
    run_id = _spawn_via_dashboard(profile_name)
    if run_id == 0:
        # Sprint 10.1 (RP-01): dashboard returned 409. Skip cleanly,
        # exit_code=0 so consecutive_failures() doesn't tick up.
        logging.info(
            f"  ⏭ skip iteration: manual run already in flight for "
            f"{profile_name!r}"
        )
        return 0, time.time() - started
    if run_id is not None:
        logging.info(f"  → dashboard spawned run #{run_id} for {profile_name}")
        return _wait_for_run_via_dashboard(run_id)

    # Fallback: direct Popen (old behavior). Used when scheduler runs
    # standalone without a dashboard server.
    logging.warning(
        "[scheduler] Dashboard unreachable — falling back to direct "
        "subprocess. Run will NOT appear in dashboard UI."
    )

    # Pre-spawn guard — without the dashboard as gatekeeper, WE have to
    # check for live runs and reap stale ones. Otherwise we'd happily
    # spawn a second main.py for a profile whose previous iteration
    # wedged.
    try:
        from ghost_shell.core.process_reaper import ensure_profile_ready_to_launch
        err = ensure_profile_ready_to_launch(get_db(), profile_name)
        if err:
            logging.error(f"[scheduler] Refusing to spawn: {err}")
            return -2, time.time() - started
    except Exception as e:
        logging.debug(f"[scheduler] pre-spawn guard skipped: {e}")

    env = os.environ.copy()
    env["GHOST_SHELL_PROFILE_NAME"] = profile_name
    env["GHOST_SHELL_PROFILE"]      = profile_name   # legacy alias

    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "ghost_shell", "monitor"],
            cwd=PROJECT_ROOT,
            env=env,
        )
        # main.py will call db.run_set_pid() itself once it inits —
        # but there's a race window where this scheduler sees "no pid"
        # while main.py is still booting. That's fine: reap will just
        # skip it until heartbeat appears.
        try:
            proc.wait(timeout=30 * 60)
            return proc.returncode, time.time() - started
        except subprocess.TimeoutExpired:
            logging.error(f"Run for {profile_name} timed out after 30 min — killing tree")
            try:
                from ghost_shell.core.process_reaper import kill_process_tree
                kill_process_tree(proc.pid, reason="scheduler 30-min timeout")
            except Exception as e:
                logging.warning(f"[scheduler] kill_process_tree failed: {e}")
            return -1, time.time() - started
    except Exception as e:
        logging.error(f"subprocess failed: {e}")
        return -1, time.time() - started


# ──────────────────────────────────────────────────────────────
# Sprint 10.1 (ARCH-01): scheduled_tasks dispatcher
# ──────────────────────────────────────────────────────────────

def _fire_scheduled_tasks():
    """Walk enabled rows in ``scheduled_tasks``, dispatch any whose
    ``next_run_at`` is in the past, then bump last_run_at +
    next_run_at to the NEXT cron match.

    Dispatch uses /api/scripts/<id>/run with assign=False (one-shot
    override, NOT a permanent re-bind). If the dashboard is offline
    we skip the row this tick — next_run_at is preserved across
    restarts in the DB. Misfires are NOT replayed: cron matches are
    wall-clock events, not jobs in a queue.

    Defensive features applied per audit #104 #4-#10:

      • Clock-skew guard (#4): if stored next_run_at is more than
        25 hours in the future, treat it as "stuck" — recompute from
        the cron expression. Catches the case where the system clock
        jumped backward (NTP correction, manual set), leaving
        next_run_at far ahead of the new now.

      • Re-read-before-fire (#5, #6): the row was loaded at the top
        of this tick. Between then and the actual fire, the user
        could have disabled / deleted / edited the row from the
        dashboard. Re-read just before firing; if the row is gone
        (deleted) we log + skip; if disabled or its enabled flag
        flipped we skip silently.

      • Profile-missing warning (#8): if the schedule references a
        profile that no longer exists in the profiles table, log a
        warning naming each missing profile so the user can see WHY
        their schedule isn't producing runs.

      • Timezone documentation (#10): cron expressions are evaluated
        against `datetime.now()` in the LOCAL timezone of the host
        running the scheduler. If you move the host between
        timezones, scheduled times shift accordingly. DST is honored
        because Python's datetime.now() follows the system clock.
    """
    import json as _json
    import urllib.request
    import urllib.error
    from ghost_shell.scheduler.cron import next_fire as _cron_next

    db = get_db()
    try:
        rows = db.scheduled_tasks_list()
    except Exception as e:
        logging.debug(f"[scheduled_tasks] list failed: {e}")
        return
    if not rows:
        return

    # Cache of known profile names — used by the missing-profile guard
    # below. Loaded once per tick, ok if it's a few seconds stale (a
    # profile deleted mid-tick won't fire, which is the desired
    # behaviour anyway).
    known_profiles = set()
    try:
        if hasattr(db, "profiles_list"):
            for _p in (db.profiles_list() or []):
                _name = _p.get("name") if isinstance(_p, dict) else _p
                if _name:
                    known_profiles.add(str(_name))
    except Exception as _le:
        logging.debug(f"[scheduled_tasks] profile list lookup failed: {_le}")

    # Helper: re-read a single row by id. Returns None if the row was
    # deleted or unreadable (which is treated as "skip this tick").
    def _reread(row_id):
        try:
            for cur_row in (db.scheduled_tasks_list() or []):
                if cur_row.get("id") == row_id:
                    return cur_row
        except Exception:
            return None
        return None

    now = datetime.now()
    # Audit #104 #4: anchor for "unreasonably far future" detection.
    # 25h covers the longest practical cron cadence (daily). Anything
    # beyond that is almost certainly stale clock-skew artefact, not
    # a legitimate cron match.
    CLOCK_SKEW_FUTURE_THRESHOLD = timedelta(hours=25)
    fired = 0
    for r in rows:
        if not r.get("enabled"):
            continue
        cron = (r.get("cron_expr") or "").strip()
        if not cron:
            continue

        next_run_at = r.get("next_run_at")
        if not next_run_at:
            try:
                nxt = _cron_next(cron, now)
                if nxt:
                    db.scheduled_task_update(
                        r["id"], next_run_at=nxt.isoformat(timespec="seconds")
                    )
            except Exception as e:
                logging.warning(
                    f"[scheduled_tasks] row {r['id']} bad cron "
                    f"{cron!r}: {e}"
                )
            continue

        try:
            due_at = datetime.fromisoformat(next_run_at)
        except ValueError:
            try:
                nxt = _cron_next(cron, now)
                db.scheduled_task_update(
                    r["id"],
                    next_run_at=nxt.isoformat(timespec="seconds") if nxt else None,
                )
            except Exception:
                pass
            continue

        # Audit #104 #4: clock-skew guard. If due_at sits more than
        # CLOCK_SKEW_FUTURE_THRESHOLD ahead of now, the most likely
        # explanation is that the system clock jumped backward and
        # the previously-stored next_run_at is now stale. Recompute
        # from cron+now and use that — otherwise the schedule would
        # remain wedged for hours/days waiting for `now` to catch up.
        if due_at - now > CLOCK_SKEW_FUTURE_THRESHOLD:
            logging.warning(
                f"[scheduled_tasks] row {r['id']} next_run_at "
                f"{next_run_at!r} is {(due_at-now).total_seconds()/3600:.1f}h "
                f"in the future — likely a clock-skew artefact. "
                f"Recomputing from cron {cron!r}."
            )
            try:
                nxt = _cron_next(cron, now)
                if nxt:
                    db.scheduled_task_update(
                        r["id"],
                        next_run_at=nxt.isoformat(timespec="seconds"),
                    )
                    due_at = nxt
            except Exception:
                continue
        if due_at > now:
            continue

        profiles = r.get("profiles") or []
        if not profiles:
            try:
                nxt = _cron_next(cron, now)
                if nxt:
                    db.scheduled_task_update(
                        r["id"], next_run_at=nxt.isoformat(timespec="seconds")
                    )
            except Exception:
                pass
            continue

        # Audit #104 #5+#6: re-read row immediately before firing so
        # we catch deletions / edits that happened between the
        # top-of-tick snapshot and now. Tiny window, but it's exactly
        # the window where a user clicks "Disable" / "Delete" in the
        # UI thinking the next fire won't happen.
        cur_row = _reread(r["id"])
        if cur_row is None:
            logging.info(
                f"[scheduled_tasks] row {r['id']} was deleted between "
                f"snapshot and fire — skipping"
            )
            continue
        if not cur_row.get("enabled"):
            logging.info(
                f"[scheduled_tasks] row {r['id']} was disabled between "
                f"snapshot and fire — skipping"
            )
            continue
        # Use the freshly-read profiles + script_id so an edit
        # mid-tick takes effect immediately.
        profiles = cur_row.get("profiles") or profiles
        live_script_id = cur_row.get("script_id") or r.get("script_id")

        # Audit #104 #8: warn loudly if any profile referenced by the
        # schedule no longer exists. Without this, the dashboard
        # silently skips missing profiles and the user wonders why
        # their schedule produces 0 runs.
        if known_profiles:
            missing = [p for p in profiles if p not in known_profiles]
            if missing:
                logging.warning(
                    f"[scheduled_tasks] row {r['id']}: profile(s) "
                    f"{missing!r} no longer exist in DB — they will "
                    f"be silently skipped by the dashboard. Edit the "
                    f"schedule (Settings → Scheduler) to remove them "
                    f"or recreate the profile."
                )
                # If EVERY profile in the schedule is missing, this
                # row is effectively a no-op — disable it so it stops
                # consuming tick budget. User can re-enable after
                # adding profiles.
                if len(missing) == len(profiles):
                    try:
                        db.scheduled_task_update(r["id"], enabled=False)
                        logging.warning(
                            f"[scheduled_tasks] row {r['id']}: all "
                            f"profiles missing — auto-disabled."
                        )
                    except Exception:
                        pass
                    continue

        # Audit D5 (Apr 2026): skip profiles flagged needs_attention=1.
        # main.py sets this flag when a static-proxy profile is burned
        # (no rotation endpoint → recovery loop has nothing to do, each
        # subsequent run just re-detects the burn and exits). Firing
        # them again on schedule wastes runs and degrades the proxy's
        # Google score further. The flag is auto-cleared by main.py
        # when a healthy run starts (status != critical).
        if hasattr(db, "profile_meta_get"):
            healthy_profiles = []
            blocked = []
            for pn in profiles:
                try:
                    meta = db.profile_meta_get(pn) or {}
                    if int(meta.get("needs_attention") or 0) == 1:
                        reason = (meta.get("needs_attention_reason")
                                  or "needs_attention=1").strip()
                        blocked.append(f"{pn} ({reason})")
                        continue
                    healthy_profiles.append(pn)
                except Exception:
                    # On read failure, default to allowing the profile
                    # — better to fire than to skip silently.
                    healthy_profiles.append(pn)
            if blocked:
                logging.warning(
                    f"[scheduled_tasks] row {r['id']}: skipping "
                    f"{len(blocked)} profile(s) flagged needs_attention: "
                    f"{', '.join(blocked)}"
                )
            if not healthy_profiles:
                logging.warning(
                    f"[scheduled_tasks] row {r['id']}: ALL "
                    f"{len(profiles)} profile(s) flagged "
                    f"needs_attention — bumping next_run_at without "
                    f"firing. Clear the flag in the dashboard or run "
                    f"GHOST_SHELL_FORCE_BURNED_RUN=1 manually."
                )
                try:
                    nxt = _cron_next(cron, now)
                    db.scheduled_task_update(
                        r["id"],
                        last_run_at=now.isoformat(timespec="seconds"),
                        next_run_at=(nxt.isoformat(timespec="seconds")
                                     if nxt else None),
                    )
                except Exception:
                    pass
                continue
            profiles = healthy_profiles

        logging.info(
            f"[scheduled_tasks] firing row {r['id']} "
            f"(script {live_script_id}, {len(profiles)} profile(s))"
        )

        try:
            req = urllib.request.Request(
                f"{DASHBOARD_BASE_URL}/api/scripts/"
                f"{int(live_script_id)}/run",
                data=_json.dumps({
                    "profiles": list(profiles),
                    "assign": False,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = _json.loads(resp.read().decode("utf-8"))
            started = body.get("started", 0)
            total = body.get("total", len(profiles))
            logging.info(
                f"[scheduled_tasks] row {r['id']}: {started}/{total} started"
            )
            fired += 1
        except urllib.error.HTTPError as e:
            logging.warning(
                f"[scheduled_tasks] row {r['id']} HTTP {e.code}: {e.reason}"
            )
        except urllib.error.URLError:
            logging.warning(
                f"[scheduled_tasks] row {r['id']}: dashboard offline"
            )
            continue
        except Exception as e:
            logging.warning(
                f"[scheduled_tasks] row {r['id']} failed: {e}"
            )

        # Bump bookkeeping. We compute next_run_at from `now` rather
        # than `due_at` so a long-stalled scheduler doesn't try to
        # catch up by firing every missed match.
        try:
            nxt = _cron_next(cron, now)
            db.scheduled_task_update(
                r["id"],
                last_run_at=now.isoformat(timespec="seconds"),
                next_run_at=(nxt.isoformat(timespec="seconds") if nxt else None),
            )
        except Exception as e:
            logging.warning(
                f"[scheduled_tasks] row {r['id']}: bookkeeping failed: {e}"
            )
    if fired:
        logging.info(f"[scheduled_tasks] tick fired {fired} task(s)")


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────

def main():
    db = get_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    db.config_set("scheduler.started_at", now_iso)
    db.config_set("scheduler.pid", os.getpid())

    # Drop a PID file so the Inno Setup updater can stop us before
    # replacing files. Mirrors what dashboard server.py does with
    # runtime.json — see ghost_shell/core/runtime.py for the layout.
    # This is best-effort: a heartbeat-based scheduler still works
    # without it; the installer will then fall back to scanning
    # `tasklist` for a python.exe running our module.
    try:
        gs_runtime.write_pid_file("scheduler.pid")
        import atexit as _atexit
        _atexit.register(gs_runtime.clear_pid_file, "scheduler.pid")
    except Exception as e:
        logging.warning(f"[scheduler] couldn't write PID file: {e}")

    heartbeat()

    # Background heartbeat ticker — writes scheduler.heartbeat_at every
    # 15 seconds REGARDLESS of main-loop position. Previously we only
    # pinged at the top of each iteration, and iterations with long
    # runs (5-10 min) caused the dashboard to think scheduler was wedged
    # when it was just waiting for a run to complete. This thread also
    # continues pinging when the main loop is in sleep_interruptible(),
    # so "sleeping 4h until morning window" doesn't flip scheduler into
    # "crashed (stale state)" on the UI.
    import threading as _th
    def _hb_ticker():
        # Sprint 10.1 (SC-04): re-check _shutdown BEFORE every write
        # so a Stop click doesn't cause one final overwrite of the
        # heartbeat AFTER mark_stopped() cleared it.
        while not _shutdown:
            if _shutdown:
                return
            try:
                db2 = get_db()
                db2.config_set(
                    "scheduler.heartbeat_at",
                    datetime.now().isoformat(timespec="seconds")
                )
            except Exception:
                pass
            for _ in range(15):
                if _shutdown:
                    return
                time.sleep(1)
    _hb_thread = _th.Thread(target=_hb_ticker, daemon=True, name="sched-heartbeat")
    _hb_thread.start()

    cfg = load_cfg()
    logging.info("═" * 60)
    logging.info(" SCHEDULER STARTED")
    logging.info(f" Target runs/day   : {cfg['target_runs']}")
    logging.info(f" Active hours      : {cfg['active_hours'][0]:02d}:00 – "
                 f"{cfg['active_hours'][1]:02d}:00")
    logging.info(f" Profiles          : {cfg['profile_names'] or '(default ' + cfg['default_profile'] + ')'}")
    logging.info(f" Selection mode    : {cfg['selection_mode']}")
    mode = cfg["schedule_mode"]
    if mode == "cron":
        logging.info(f" Mode              : CRON — {cfg['cron_expression']!r}")
    elif mode == "interval":
        logging.info(f" Mode              : INTERVAL — every {cfg['interval_sec']}s")
    else:
        logging.info(f" Mode              : DENSITY — {cfg['min_interval']}–{cfg['max_interval']}s "
                     f"(+{cfg['jitter_percent']}% jitter)")
    if cfg["active_days"]:
        _dn = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        logging.info(f" Active days       : " + ", ".join(_dn[d] for d in cfg["active_days"] if 1 <= d <= 7))
    logging.info("═" * 60)

    try:
        _iter_no = 0
        while not _shutdown:
            _iter_no += 1
            # AUDIT #104 #3 fix: each tick body MUST be wrapped in its
            # own try/except. Previously a transient SQLite "database
            # is locked" or a single bad cron expression in one row
            # would propagate up and kill the entire scheduler — the
            # outer try/finally catches it but the WHILE loop exits.
            # Now the loop survives bad ticks; we just log + sleep +
            # retry on the next interval.
            try:
                cfg = load_cfg()
                heartbeat()
                # Verbose tick header so the user can grep "tick #" in
                # logs and see exactly when each iteration starts + what
                # the gates evaluate to.
                logging.info(
                    f"-- tick #{_iter_no} at "
                    f"{datetime.now().strftime('%H:%M:%S')} "
                    f"target={cfg['target_runs']} "
                    f"hours={cfg['active_hours']}"
                )
            except Exception as _tick_err:
                logging.warning(
                    f"[scheduler] tick #{_iter_no} setup failed: "
                    f"{type(_tick_err).__name__}: {_tick_err} — "
                    f"sleeping 30s and retrying"
                )
                time.sleep(30)
                continue

            # Sprint 10.1 (ARCH-01): fire any per-script scheduled_tasks
            # whose next_run_at has elapsed. Previously the table was
            # dead code — users created schedules via the UI that did
            # nothing. Now it's wired up.
            try:
                _fire_scheduled_tasks()
            except Exception as e:
                logging.warning(f"[scheduler] scheduled_tasks tick failed: {e}")

            if not is_active_day(cfg["active_days"]):
                sleep_sec = time_until_next_active_day(cfg["active_days"])
                wake_at = datetime.now() + timedelta(seconds=sleep_sec)
                # Two-line log so the next-run-time stands out -- the
                # user reported the sleeping line was easy to miss
                # mixed in with the rest of stdout.
                logging.info(
                    f"💤 Outside active-days — sleeping {sleep_sec/3600:.1f}h"
                )
                logging.info(
                    f"⏰ Next run at "
                    f"{wake_at.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(in {sleep_sec/3600:.1f}h)"
                )
                heartbeat({"next_run_at": wake_at.isoformat(timespec="seconds")})
                sleep_interruptible(sleep_sec)
                continue

            if not is_active_time(cfg["active_hours"]):
                sleep_sec = time_until_next_window(cfg["active_hours"])
                wake_at = datetime.now() + timedelta(seconds=sleep_sec)
                logging.info(
                    f"💤 Outside window — sleeping {sleep_sec/3600:.1f}h"
                )
                logging.info(
                    f"⏰ Next run at "
                    f"{wake_at.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(in {sleep_sec/3600:.1f}h)"
                )
                heartbeat({"next_run_at": wake_at.isoformat(timespec="seconds")})
                sleep_interruptible(sleep_sec)
                continue

            done_today = runs_today()
            if done_today >= cfg["target_runs"]:
                # Sprint 10.1 (SC-05): for 24/7 setup (active_hours=[0,24]),
                # time_until_next_window returns ~60s because h_start=0
                # and "now is past h_start" is always true. That made
                # the scheduler busy-poll once a minute when quota was
                # met. Sleep till local midnight (when runs_today rolls
                # over) instead.
                h_start, h_end = cfg["active_hours"]
                if h_start == 0 and h_end >= 24:
                    now_dt = datetime.now()
                    tomorrow = (now_dt + timedelta(days=1)).replace(
                        hour=0, minute=0, second=1, microsecond=0
                    )
                    sleep_sec = max(60.0, (tomorrow - now_dt).total_seconds())
                else:
                    sleep_sec = time_until_next_window(cfg["active_hours"])
                wake_at = datetime.now() + timedelta(seconds=sleep_sec)
                logging.info(
                    f"✅ Quota met ({done_today}/{cfg['target_runs']}) — "
                    f"sleeping until tomorrow"
                )
                logging.info(
                    f"⏰ Next run at "
                    f"{wake_at.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(in {sleep_sec/3600:.1f}h)"
                )
                heartbeat({"next_run_at": wake_at.isoformat(timespec="seconds")})
                sleep_interruptible(sleep_sec)
                continue

            fails = consecutive_failures()
            if fails >= cfg["max_fails_in_row"]:
                pause = cfg["fail_pause_sec"]
                resume = (datetime.now() + timedelta(seconds=pause)
                          ).strftime("%H:%M:%S")
                logging.error(
                    f"🚨 {fails} consecutive failures (threshold "
                    f"{cfg['max_fails_in_row']}) -- pausing {pause}s, "
                    f"resume at {resume}"
                )
                logging.error(
                    f"   tip: open Settings -> reset 'failure counter' "
                    f"to clear, or fix proxy/profile and let one good run "
                    f"reset the counter"
                )
                sleep_interruptible(pause)
                continue

            # A batch is 1 profile in the simple case, or N profiles if
            # scheduler.group_id is set. We spawn each member as its own
            # subprocess and optionally wait for them depending on
            # group_mode ("parallel" = fire-and-forget, "serial" = sequential).
            batch = pick_batch(cfg)
            run_num = done_today + 1

            logging.info("")
            logging.info(
                f"▶ Run {run_num}/{cfg['target_runs']} at "
                f"{datetime.now().strftime('%H:%M:%S')} — "
                f"{'batch' if len(batch) > 1 else 'profile'}: "
                f"{batch[0] if len(batch) == 1 else str(len(batch)) + ' profiles'}"
            )
            heartbeat({"last_run_profile": batch[0] if batch else ""})

            if len(batch) == 1:
                # Single-profile path — unchanged from the old loop
                exit_code, duration = run_one_iteration(batch[0])
                if exit_code == 0:
                    logging.info(f"✓ Run #{run_num} ok ({duration:.0f}s)")
                else:
                    logging.error(
                        f"✗ Run #{run_num} failed "
                        f"(exit={exit_code}, {duration:.0f}s)"
                    )
            else:
                # Batch path — spawn all, wait (or not) depending on mode.
                # NOTE: each member runs in its own process, so total RAM
                # scales linearly. The group's max_parallel cap is NOT
                # enforced here (scheduler bypasses the dashboard's
                # RUNNER_POOL because it runs in a separate process) —
                # user sets group size responsibly.
                if cfg["group_mode"] == "serial":
                    for name in batch:
                        if _shutdown:
                            break
                        ec, dur = run_one_iteration(name)
                        logging.info(f"  • {name}: exit={ec} ({dur:.0f}s)")
                else:
                    # Parallel — spawn all via dashboard so runs appear
                    # in the UI and their logs stream. Previously we did
                    # raw Popen here which bypassed the dashboard's log
                    # pipe AND RunnerPool registration — same class of
                    # bug as the /api/run/start 404 we fixed earlier.
                    #
                    # If the dashboard is offline, fall back to raw
                    # Popen per profile (each will print to its own
                    # stdout but the main.py heartbeat still reaches
                    # the DB so the UI shows them in active-runs).
                    run_ids = []
                    for name in batch:
                        if _shutdown:
                            break
                        rid = _spawn_via_dashboard(name)
                        if rid == 0:
                            logging.info(
                                f"  ⏭ skip {name} (manual run active)"
                            )
                            continue
                        if rid is not None:
                            run_ids.append((name, rid, "dashboard"))
                            logging.info(f"  ▶ spawned {name} (run #{rid}) via dashboard")
                            continue
                        # Fallback: direct Popen with pre-spawn guard
                        try:
                            from ghost_shell.core.process_reaper import ensure_profile_ready_to_launch
                            err = ensure_profile_ready_to_launch(get_db(), name)
                            if err:
                                logging.warning(f"  ✗ {name}: skipped — {err}")
                                continue
                        except Exception:
                            pass
                        env = os.environ.copy()
                        env["GHOST_SHELL_PROFILE_NAME"] = name
                        try:
                            p = subprocess.Popen(
                                [sys.executable, "-u", "-m", "ghost_shell", "monitor"],
                                cwd=PROJECT_ROOT,
                                env=env,
                            )
                            run_ids.append((name, p, "popen"))
                            logging.info(f"  ▶ spawned {name} (pid {p.pid}) direct")
                        except Exception as e:
                            logging.error(f"  ✗ {name}: spawn failed — {e}")

                    # Wait — with a generous timeout per process. Entries
                    # are (name, handle, kind) where handle is either a
                    # run_id int (dashboard) or a Popen object (direct).
                    for name, handle, kind in run_ids:
                        if kind == "dashboard":
                            rc, _dur = _wait_for_run_via_dashboard(handle)
                            logging.info(f"  • {name} (run #{handle}): exit={rc}")
                        else:
                            try:
                                rc = handle.wait(timeout=30 * 60)
                                logging.info(f"  • {name}: exit={rc}")
                            except subprocess.TimeoutExpired:
                                logging.error(f"  ✗ {name}: timed out, killing")
                                try:
                                    from ghost_shell.core.process_reaper import kill_process_tree
                                    kill_process_tree(handle.pid,
                                                      reason="batch 30-min timeout")
                                except Exception:
                                    try: handle.kill()
                                    except Exception: pass
                logging.info(f"✓ Batch #{run_num} done ({len(batch)} profiles)")

            if _shutdown:
                break

            try:
                interval = next_fire_delay(cfg, runs_today())
            except Exception as _delay_err:
                logging.warning(
                    f"[scheduler] next_fire_delay failed "
                    f"({type(_delay_err).__name__}: {_delay_err}) — "
                    f"defaulting to 5min retry interval"
                )
                interval = 300
            if interval <= 0:
                continue
            wake_at = datetime.now() + timedelta(seconds=interval)
            try:
                heartbeat({"next_run_at": wake_at.isoformat(timespec="seconds")})
            except Exception:
                pass
            logging.info(
                f"⏰ Next run at {wake_at.strftime('%H:%M:%S')} "
                f"(in {interval/60:.1f}min)"
            )
            sleep_interruptible(interval)

    finally:
        mark_stopped()
        done = runs_today()
        logging.info("═" * 60)
        logging.info(" SCHEDULER STOPPED")
        logging.info(f" Runs today : {done}")
        logging.info("═" * 60)


if __name__ == "__main__":
    main()
