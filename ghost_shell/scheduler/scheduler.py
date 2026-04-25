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
    today = datetime.now().strftime("%Y-%m-%d")
    row = get_db()._get_conn().execute(
        "SELECT COUNT(*) AS n FROM runs WHERE started_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    return row["n"] if row else 0


def consecutive_failures() -> int:
    rows = get_db()._get_conn().execute(
        "SELECT exit_code FROM runs WHERE finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    count = 0
    for r in rows:
        if r["exit_code"] not in (0, None):
            count += 1
        else:
            break
    return count


# ──────────────────────────────────────────────────────────────
# Profile picker
# ──────────────────────────────────────────────────────────────

_round_robin_idx = 0

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
        name = pool[_round_robin_idx % len(pool)]
        _round_robin_idx += 1
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
    except urllib.error.URLError:
        return None   # dashboard offline
    except Exception as e:
        logging.warning(f"[scheduler] dashboard spawn failed: {e}")
        return None


def _wait_for_run_via_dashboard(run_id: int, timeout: int = 30 * 60) -> tuple:
    """Poll /api/run/status until the run finishes or times out.
    Returns (exit_code, duration_sec). Uses generous polling intervals
    so we don't hammer the dashboard."""
    import json as _json
    import urllib.request

    started = time.time()
    poll_interval = 5  # seconds

    while time.time() - started < timeout:
        if _shutdown:
            return -1, time.time() - started
        try:
            with urllib.request.urlopen(
                f"{DASHBOARD_BASE_URL}/api/run/status?run_id={run_id}",
                timeout=5,
            ) as r:
                status = _json.loads(r.read().decode("utf-8"))
            if not status.get("running"):
                # exit_code is None for still-running runs; dashboard sets
                # it to the subprocess returncode on completion.
                return status.get("exit_code", 0) or 0, time.time() - started
        except Exception as e:
            logging.debug(f"[scheduler] status poll error (non-fatal): {e}")
        time.sleep(poll_interval)

    # Timeout — ask dashboard to kill the run, best-effort
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
    logging.error(f"[scheduler] run {run_id} timed out after {timeout}s")
    return -1, time.time() - started


def run_one_iteration(profile_name: str) -> tuple:
    """Launch one main.py instance for the given profile.
    Prefers dashboard-routed spawn (gives live logs + run tracking),
    falls back to direct subprocess if dashboard is offline."""
    started = time.time()

    # Try dashboard route first — gives us full SSE integration
    run_id = _spawn_via_dashboard(profile_name)
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
        from ghost_shell.core.process_reaper import ensure_no_live_run_for_profile
        err = ensure_no_live_run_for_profile(get_db(), profile_name)
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
        while not _shutdown:
            try:
                db2 = get_db()
                db2.config_set(
                    "scheduler.heartbeat_at",
                    datetime.now().isoformat(timespec="seconds")
                )
            except Exception:
                pass
            # 15-sec sleep in small slices for responsive shutdown
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
        while not _shutdown:
            cfg = load_cfg()
            heartbeat()

            if not is_active_day(cfg["active_days"]):
                sleep_sec = time_until_next_active_day(cfg["active_days"])
                wake_at = datetime.now() + timedelta(seconds=sleep_sec)
                logging.info(
                    f"💤 Outside active-days — sleeping until "
                    f"{wake_at.strftime('%Y-%m-%d %H:%M')} "
                    f"({sleep_sec/3600:.1f}h)"
                )
                heartbeat({"next_run_at": wake_at.isoformat(timespec="seconds")})
                sleep_interruptible(sleep_sec)
                continue

            if not is_active_time(cfg["active_hours"]):
                sleep_sec = time_until_next_window(cfg["active_hours"])
                wake_at = datetime.now() + timedelta(seconds=sleep_sec)
                logging.info(
                    f"💤 Outside window — sleeping until "
                    f"{wake_at.strftime('%Y-%m-%d %H:%M')} "
                    f"({sleep_sec/3600:.1f}h)"
                )
                heartbeat({"next_run_at": wake_at.isoformat(timespec="seconds")})
                sleep_interruptible(sleep_sec)
                continue

            done_today = runs_today()
            if done_today >= cfg["target_runs"]:
                sleep_sec = time_until_next_window(cfg["active_hours"])
                logging.info(
                    f"✅ Quota met ({done_today}/{cfg['target_runs']}) — "
                    f"sleeping until tomorrow"
                )
                sleep_interruptible(sleep_sec)
                continue

            fails = consecutive_failures()
            if fails >= cfg["max_fails_in_row"]:
                pause = cfg["fail_pause_sec"]
                logging.error(
                    f"🚨 {fails} consecutive failures — pausing for {pause}s"
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
                        if rid is not None:
                            run_ids.append((name, rid, "dashboard"))
                            logging.info(f"  ▶ spawned {name} (run #{rid}) via dashboard")
                            continue
                        # Fallback: direct Popen with pre-spawn guard
                        try:
                            from ghost_shell.core.process_reaper import ensure_no_live_run_for_profile
                            err = ensure_no_live_run_for_profile(get_db(), name)
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

            interval = next_fire_delay(cfg, runs_today())
            if interval <= 0:
                continue
            wake_at = datetime.now() + timedelta(seconds=interval)
            heartbeat({"next_run_at": wake_at.isoformat(timespec="seconds")})
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
