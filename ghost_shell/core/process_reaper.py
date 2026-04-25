"""
process_reaper.py — Centralised process-tree management.

Solves three concrete problems the user hits repeatedly:

  1. Browser hangs mid-script → main.py doesn't exit → Chrome stays alive
     → scheduler spawns another run → two Chromes per profile.

  2. Dashboard restart while runs are active → RunnerPool starts empty
     but Chrome subprocesses from the previous dashboard are still
     alive. Fresh runs collide with them on user-data-dir locks.

  3. Scheduler PID file points at a dead PID but the file stayed on
     disk → "already running" error on manual start.

All three paths end in "force-kill a process TREE". Windows makes this
particularly nasty — killing a Python process without its kids leaves
Chrome and chromedriver as orphaned zombies. psutil.Process.children()
walks the tree correctly on all three OSes.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
import os
import time
from datetime import datetime, timedelta
from typing import List, Optional

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    HAVE_PSUTIL = False


# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

# How old a heartbeat can get before we consider a run stuck.
# main.py pings every 15s; 3 min gives us plenty of slack for a
# slow CDP call or a temporarily stalled GIL.
STALE_HEARTBEAT_SEC = 180

# How long to wait for a process to exit after SIGTERM before
# escalating to SIGKILL. Chrome is usually down in 2-3 seconds;
# main.py sometimes waits on a blocking socket read and needs longer.
GRACEFUL_KILL_TIMEOUT = 8


# ──────────────────────────────────────────────────────────────
# Primitive: kill a single process tree
# ──────────────────────────────────────────────────────────────

def kill_process_tree(pid: int, reason: str = "cleanup") -> bool:
    """Kill a process AND all its descendants. Returns True if the root
    process is gone (whether we killed it or it was already dead) and
    False if something prevented the kill (permission denied etc.).

    The logic matches what `taskkill /F /T /PID N` does on Windows and
    `kill -TERM -<pgid>` does on Unix, but portable via psutil.

    We do it in stages:
      1. Collect all descendants FIRST — enumerating after the root
         dies is unreliable on Windows.
      2. terminate() everyone → they get SIGTERM / CTRL_BREAK.
      3. Wait briefly. Anyone still alive gets kill().
      4. Final wait to confirm exit.
    """
    if not HAVE_PSUTIL:
        logging.warning("[ProcessReaper] psutil not available, cannot kill pid=%s", pid)
        return False
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True  # already dead
    except Exception as e:
        logging.warning(f"[ProcessReaper] Process({pid}) lookup failed: {e}")
        return False

    try:
        # Snapshot children before we touch anything — children list
        # becomes empty-then-invalid the moment the root dies on Windows.
        descendants = root.children(recursive=True)
    except Exception:
        descendants = []

    all_procs = [root] + descendants
    logging.info(
        f"[ProcessReaper] Killing pid={pid} (+{len(descendants)} children) "
        f"because: {reason}"
    )

    # Stage 1: graceful termination
    for p in all_procs:
        try:
            p.terminate()
        except Exception:
            pass

    # Stage 2: wait briefly for clean exit
    gone, alive = psutil.wait_procs(all_procs, timeout=GRACEFUL_KILL_TIMEOUT)

    # Stage 3: force-kill stragglers
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    if alive:
        psutil.wait_procs(alive, timeout=3)

    # Final verification: is the root really dead?
    try:
        if psutil.pid_exists(pid):
            # A tiny race window — Windows sometimes reports the PID as
            # existing for a moment after exit. Give it another tick.
            time.sleep(0.5)
            if psutil.pid_exists(pid):
                logging.warning(f"[ProcessReaper] pid={pid} still alive after kill")
                return False
    except Exception:
        pass
    return True


def pid_looks_like_ghost_shell(pid: int) -> bool:
    """Defensive check: before we kill a stored PID, confirm it's actually
    one of ours. An ancient PID could have been recycled by the OS for an
    unrelated process (rare but possible on long uptimes)."""
    if not HAVE_PSUTIL:
        return False
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return False
    except Exception:
        return False
    try:
        name = (p.name() or "").lower()
        cmd  = " ".join(p.cmdline() or []).lower()
    except Exception:
        return False
    # Accept python processes whose cmdline mentions main.py or scheduler.py.
    # We're generous here — missing kills are worse than false positives.
    if "python" in name and ("main.py" in cmd or "scheduler.py" in cmd):
        return True
    # Chrome / chromedriver alone isn't enough — could be user's own Chrome.
    # Require the user-data-dir arg to include our profiles/ folder.
    if ("chrome" in name or "chromedriver" in name) and \
       ("ghost_shell" in cmd or "profiles" in cmd):
        return True
    return False


# ──────────────────────────────────────────────────────────────
# Higher-level: reap stale runs listed in the DB
# ──────────────────────────────────────────────────────────────

def reap_stale_runs(db, reason_prefix: str = "startup") -> dict:
    """Walk runs table for entries that have no finished_at and decide
    what to do with each:

      - PID is 0/null and heartbeat is old → just mark finished (already dead)
      - PID is dead (no such process)     → mark finished (crashed uncleanly)
      - PID is alive, heartbeat is old    → kill tree, mark finished
      - PID is alive, heartbeat is fresh  → leave alone (still running)

    Returns a dict {alive_left_alone, marked_finished, killed} for logging.
    Called by dashboard_server at startup AND every time scheduler tries
    to spawn a new run (so a wedged previous iteration doesn't stack).
    """
    stats = {"alive_left_alone": 0, "marked_finished": 0, "killed": 0}
    try:
        rows = db.runs_find_unfinished_with_pid()
    except Exception as e:
        logging.warning(f"[ProcessReaper] runs_find_unfinished failed: {e}")
        return stats

    now = datetime.now()

    for row in rows:
        run_id = row["id"]
        pid    = row.get("pid")
        hb_str = row.get("heartbeat_at")

        # Parse heartbeat age
        hb_age = None
        if hb_str:
            try:
                hb_age = (now - datetime.fromisoformat(hb_str)).total_seconds()
            except Exception:
                hb_age = None

        def _mark_dead(why: str):
            try:
                db.run_finish(run_id, exit_code=-99, error=f"{reason_prefix}: {why}")
                stats["marked_finished"] += 1
            except Exception as e:
                logging.warning(
                    f"[ProcessReaper] mark run={run_id} finished failed: {e}"
                )

        # No PID recorded — best we can do is mark it finished if heartbeat
        # is also stale (otherwise leave alone: this might be a run whose
        # dashboard_server just restarted mid-spawn, DB already has the row
        # but PID assignment hasn't happened yet).
        if not pid:
            if hb_age is None or hb_age > STALE_HEARTBEAT_SEC:
                _mark_dead("no pid, no recent heartbeat")
            else:
                stats["alive_left_alone"] += 1
            continue

        # Is the PID actually alive and ours?
        if not HAVE_PSUTIL:
            # Fall back to "trust the DB" — if heartbeat stale, mark dead.
            if hb_age is None or hb_age > STALE_HEARTBEAT_SEC:
                _mark_dead(f"heartbeat stale ({hb_age}s) and psutil missing")
            else:
                stats["alive_left_alone"] += 1
            continue

        if not psutil.pid_exists(pid):
            _mark_dead(f"pid={pid} no longer exists")
            continue

        if not pid_looks_like_ghost_shell(pid):
            # PID recycled by OS. Don't touch the other process, just
            # stop pretending the run is alive.
            _mark_dead(f"pid={pid} recycled to unrelated process")
            continue

        # PID is alive and ours. Is the run wedged?
        if hb_age is None or hb_age > STALE_HEARTBEAT_SEC:
            logging.warning(
                f"[ProcessReaper] Run {run_id} (pid={pid}, profile="
                f"{row.get('profile_name')}) has stale heartbeat "
                f"({hb_age}s). Killing."
            )
            kill_process_tree(pid, reason=f"heartbeat stale ({hb_age}s)")
            _mark_dead(f"killed (heartbeat stale {hb_age}s)")
            stats["killed"] += 1
        else:
            stats["alive_left_alone"] += 1

    return stats


def ensure_no_live_run_for_profile(db, profile_name: str) -> Optional[str]:
    """Pre-spawn guard. Return None if the profile is free to start,
    or an error string describing why it isn't.

    Called by:
      * api_runs_start / api_run (dashboard manual spawn)
      * scheduler._spawn_via_dashboard (scheduler iteration spawn)

    The contract: if this returns None, the caller MAY safely create
    a new run. We check the DB first (source of truth), then reap any
    stale entries, then re-check.
    """
    live = db.runs_live_for_profile(profile_name)
    if not live:
        return None

    # Try to reap — if the old run is actually dead we can proceed.
    reap_stale_runs(db, reason_prefix="pre-spawn")

    live = db.runs_live_for_profile(profile_name)
    if not live:
        return None  # reaped, clear to proceed

    # Still live. Report the newest offender.
    row = live[0]
    hb = row.get("heartbeat_at") or "never"
    return (
        f"profile '{profile_name}' still has an active run "
        f"(run_id={row['id']}, pid={row.get('pid')}, last heartbeat={hb}). "
        f"Wait for it to finish or stop it manually."
    )
