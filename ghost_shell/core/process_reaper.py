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


def kill_chrome_for_user_data_dir(user_data_dir: str,
                                  reason: str = "orphan sweep") -> int:
    """Find and kill any chrome.exe / chromedriver.exe / chromium binaries
    whose command line references our --user-data-dir. Returns the count
    of process trees killed.

    Why this exists: when ``webdriver.Chrome(...)`` constructor raises
    SessionNotCreatedException, ``self.driver`` is never assigned, so the
    regular ``self.driver.service.process.pid`` cleanup path doesn't have
    a PID to kill. Chrome and chromedriver are still alive, holding our
    user-data-dir's files. The next retry then fails with WinError 32 on
    History/Top Sites and WinError 5 on quarantine rename.

    This helper does NOT need a known PID — it scans every process by
    command-line and kills the matches. Idempotent and safe to call any
    time before/after a launch attempt.

    Matching strategy:
      * chrome.exe / chromium binaries: parent (browser) process has
        ``--user-data-dir=<our_path>`` in cmdline. Renderer / utility /
        GPU subprocesses are children of the parent and inherit no such
        flag, so they only get hit transitively via
        ``kill_process_tree``.
      * chromedriver.exe: its cmdline doesn't contain --user-data-dir,
        but its child (Chrome) does. We catch chromedriver indirectly by
        also walking parent->child relationships of any matched chrome
        process and confirming the chromedriver's parent matches our
        Python PID. Stragglers parented elsewhere are left alone — they
        belong to some other ghost_shell session.

    Path comparison is case-insensitive and tolerates both forward- and
    back-slash forms (Windows quirks)."""
    if not HAVE_PSUTIL:
        # Promote to WARNING — this is a real safety-net failure mode.
        # Without psutil the orphan-cleanup story collapses and the user
        # will hit the WinError-32 cascade we worked hard to prevent.
        # The dashboard's normal install includes psutil; this only
        # fires on bare-bones Python environments. Suggest the fix
        # inline so users don't dig.
        logging.warning(
            "[ProcessReaper] psutil not available — orphan sweep "
            "DISABLED. Chrome processes from failed runs cannot be "
            "auto-killed and may lock user-data-dir on retry. "
            "Install psutil: pip install psutil"
        )
        return 0
    if not user_data_dir:
        return 0

    # Normalize the target both ways so we can match cmdlines that came
    # through with either separator.
    target_native = os.path.normpath(user_data_dir).lower()
    target_fwd    = target_native.replace("\\", "/")

    own_pid = os.getpid()

    # Pass 1: find chrome browser-process matches by cmdline.
    chrome_matches: list[psutil.Process] = []
    chromedriver_candidates: list[psutil.Process] = []

    for p in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
        try:
            name = (p.info.get("name") or "").lower()
            if not name:
                continue
            cmdline = p.info.get("cmdline") or []
            if not cmdline:
                continue
            joined = " ".join(cmdline).lower()
            joined_fwd = joined.replace("\\", "/")

            # chrome.exe / chrome / chromium with our user-data-dir
            is_chrome = (
                name in ("chrome.exe", "chrome", "chromium",
                         "chromium.exe", "chromium-browser")
                or name.startswith("chrome ")
            )
            if is_chrome and (target_native in joined or target_fwd in joined_fwd):
                chrome_matches.append(p)
                continue

            # chromedriver — record as candidate; we'll filter by parent below
            is_chromedriver = (
                name in ("chromedriver.exe", "chromedriver")
                or "chromedriver" in name
            )
            if is_chromedriver:
                chromedriver_candidates.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            # process_iter rarely throws other things; be defensive
            continue

    # Pass 2: filter chromedriver candidates — keep only those that are
    # ours (parent is our Python OR a direct ancestor of one of the
    # matched chrome processes).
    matched_chrome_pids = {p.info["pid"] for p in chrome_matches}
    relevant_chromedriver: list[psutil.Process] = []

    for cd in chromedriver_candidates:
        try:
            ppid = cd.info.get("ppid")
            # Direct child of our Python — definitely ours
            if ppid == own_pid:
                relevant_chromedriver.append(cd)
                continue
            # Or: any of the matched chrome processes is a descendant
            # of this chromedriver. Walk down once.
            try:
                children = cd.children(recursive=False)
            except Exception:
                children = []
            for child in children:
                if child.pid in matched_chrome_pids:
                    relevant_chromedriver.append(cd)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    targets = chrome_matches + relevant_chromedriver
    if not targets:
        return 0

    killed = 0
    for proc in targets:
        try:
            pid = proc.info["pid"]
        except Exception:
            continue
        # kill_process_tree handles "already dead" gracefully
        if kill_process_tree(pid, reason=reason):
            killed += 1

    if killed:
        # Windows takes a beat to release file handles after process exit.
        # Without this delay the next pre-flight still hits WinError 32.
        time.sleep(0.6)
        logging.warning(
            f"[ProcessReaper] orphan sweep: killed {killed} process(es) "
            f"holding user-data-dir={user_data_dir}"
        )
    return killed


def _windows_schedule_delete_on_reboot(path: str) -> bool:
    """Use the Win32 ``MoveFileExW`` API with ``MOVEFILE_DELAY_UNTIL_REBOOT``
    to schedule a path for deletion on the next system restart. Returns
    True on success, False if the API call failed (e.g. non-Windows OS,
    no admin rights for system-level paths, the path doesn't exist).

    This is the last resort when ``shutil.rmtree`` fails because some
    file is held open by an orphan process we can't reach (e.g. a Chrome
    process belonging to a different Windows session, anti-virus
    scanning, etc.). After reboot the path is gone, no manual cleanup
    needed."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ]
        kernel32.MoveFileExW.restype = wintypes.BOOL
        ok = kernel32.MoveFileExW(path, None, MOVEFILE_DELAY_UNTIL_REBOOT)
        if ok:
            return True
        err = ctypes.get_last_error()
        logging.debug(
            f"[ProcessReaper] MoveFileExW({path}, DELAY_UNTIL_REBOOT) "
            f"failed: GetLastError={err}"
        )
        return False
    except Exception as e:
        logging.debug(f"[ProcessReaper] MoveFileExW unavailable: {e}")
        return False


def cleanup_quarantine_dirs(parent_dir: str,
                            max_age_days: int = 0,
                            schedule_delete_on_reboot: bool = True) -> dict:
    """Scan ``parent_dir`` for ``*.quarantine-*`` subfolders left behind by
    failed runs and try to remove them robustly.

    Strategy per dir:
      1. Kill any orphan chrome.exe / chromedriver.exe whose cmdline
         references the dir (could keep it locked).
      2. ``shutil.rmtree`` with one short backoff retry.
      3. On Windows, if the dir is still around, schedule it for
         deletion on the next reboot via ``MoveFileExW``.

    Args:
      parent_dir: Folder to scan (e.g. ``%LOCALAPPDATA%\\GhostShellAnty\\profiles``).
      max_age_days: If > 0, only touch dirs whose ``mtime`` is older than
        this many days. Set to 0 (default) to clean every quarantine
        regardless of age.
      schedule_delete_on_reboot: Apply the MoveFileExW fallback if
        rmtree fails. Disable if you're testing and don't want a reboot
        to silently delete data.

    Returns: stats dict with keys ``scanned``, ``deleted``, ``deferred``
    (scheduled for reboot), ``failed``."""
    import glob
    import shutil
    stats = {"scanned": 0, "deleted": 0, "deferred": 0, "failed": 0}

    if not parent_dir or not os.path.isdir(parent_dir):
        return stats

    cutoff = None
    if max_age_days > 0:
        cutoff = time.time() - (max_age_days * 86400)

    try:
        entries = os.listdir(parent_dir)
    except OSError as e:
        logging.warning(f"[ProcessReaper] cleanup_quarantine_dirs list: {e}")
        return stats

    for name in entries:
        if ".quarantine-" not in name:
            continue
        full = os.path.join(parent_dir, name)
        if not os.path.isdir(full):
            continue
        if cutoff is not None:
            try:
                if os.path.getmtime(full) > cutoff:
                    continue   # too recent, leave alone
            except OSError:
                pass
        stats["scanned"] += 1

        # Step 1: kill orphans that might be holding the dir
        try:
            kill_chrome_for_user_data_dir(full, reason="quarantine cleanup")
        except Exception as e:
            logging.debug(f"[ProcessReaper] orphan sweep ({full}): {e}")

        # Step 2: rmtree with retry
        deleted = False
        last_err = None
        for attempt in range(2):
            try:
                shutil.rmtree(full)
                deleted = True
                break
            except OSError as e:
                last_err = e
                if attempt == 0:
                    time.sleep(0.6)
        if deleted:
            stats["deleted"] += 1
            continue

        # Step 3: still here. Schedule for reboot deletion.
        if schedule_delete_on_reboot and _windows_schedule_delete_on_reboot(full):
            stats["deferred"] += 1
            logging.info(
                f"[ProcessReaper] quarantine {name}: rmtree failed "
                f"({last_err}); scheduled for delete-on-next-reboot"
            )
            continue

        stats["failed"] += 1
        logging.warning(
            f"[ProcessReaper] quarantine {name}: couldn't remove and "
            f"couldn't schedule reboot-delete. {last_err}"
        )

    if stats["scanned"]:
        logging.info(
            f"[ProcessReaper] quarantine cleanup in {parent_dir}: "
            f"scanned={stats['scanned']} deleted={stats['deleted']} "
            f"deferred={stats['deferred']} failed={stats['failed']}"
        )
    return stats


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


def is_profile_actually_running(db, profile_name: str) -> bool:
    """Cross-process liveness check for a profile. Replaces the
    dashboard-local ``RUNNER_POOL.is_profile_running()`` which only
    sees runs spawned by THIS Flask process — scheduler-spawned runs
    are invisible to it (PR-31/PR-66 disaster scenario).

    Returns True iff the DB has at least one ``runs`` row for this
    profile with ``finished_at IS NULL`` AND either:
      * the recorded PID is alive and looks like ghost_shell, OR
      * the heartbeat is fresher than ``STALE_HEARTBEAT_SEC``

    False otherwise — including the case where the run row exists but
    its PID is dead and heartbeat is stale (a crashed run that nobody
    finished).

    This is the canonical liveness check for "should I refuse to
    delete / mutate this profile because something's running it?"
    """
    try:
        live_rows = db.runs_live_for_profile(profile_name)
    except Exception as e:
        # On DB error, conservative: assume alive, refuse the
        # destructive op. Matches the policy of the lock helpers in
        # browser/runtime.py.
        logging.warning(
            f"[ProcessReaper] runs_live_for_profile failed for "
            f"{profile_name!r}: {e}. Assuming run is alive."
        )
        return True

    if not live_rows:
        return False

    now = datetime.now()
    for row in live_rows:
        # Heartbeat-fresh runs are definitively alive
        hb_str = row.get("heartbeat_at")
        if hb_str:
            try:
                hb_age = (now - datetime.fromisoformat(hb_str)).total_seconds()
                if hb_age < STALE_HEARTBEAT_SEC:
                    return True
            except (ValueError, TypeError):
                pass
        # Heartbeat is stale or missing — fall back to PID check
        pid = row.get("pid")
        if pid and HAVE_PSUTIL:
            try:
                if psutil.pid_exists(pid) and pid_looks_like_ghost_shell(pid):
                    return True
            except Exception:
                pass
        # Else: run is dead per heartbeat staleness AND PID check;
        # this row should be reaped, but we don't reap during a
        # liveness check. Caller can call reap_stale_runs() if the
        # check returned False but they want belt-and-braces.

    # All live rows are actually stale → caller can proceed with
    # destructive op. The stale rows will be cleaned up by the next
    # reap_stale_runs() pass.
    return False


def ensure_profile_ready_to_launch(db, profile_name: str) -> Optional[str]:
    """Pre-spawn check used by scheduler/dashboard before they fire a
    new run. Returns None if the profile is ready to launch, or an
    error string if it isn't.

    Two reasons we'd refuse:

    1. **Not ready** — bulk-create populated the row but didn't finish
       its setup pipeline (RC-31 fix). Profile_extensions, proxy
       assignment, cookie inject etc. may still be in flight, and a
       launch right now would see partial state.
    2. **Already running** — handled by ``ensure_no_live_run_for_profile``
       below; we delegate.

    Combined helper so callers don't need two checks. Order matters:
    readiness check is cheap (one SQL row), liveness check involves
    process scans, so gate on readiness first.
    """
    try:
        if hasattr(db, "profile_is_ready") and not db.profile_is_ready(profile_name):
            return (
                f"profile '{profile_name}' is not ready yet "
                f"(setup pipeline incomplete — likely a bulk-create "
                f"in progress). Wait a moment and try again."
            )
    except Exception as e:
        # Don't break legacy DBs that lack the column — log and proceed
        logging.debug(f"[ProcessReaper] ready check skipped: {e}")
    return ensure_no_live_run_for_profile(db, profile_name)


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
