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

import os
import sys
import time
import random
import signal
import logging
import subprocess
from datetime import datetime, timedelta, time as dtime

from db import get_db


# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────

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
    """Return the next profile name to run."""
    global _round_robin_idx
    pool = cfg["profile_names"]
    if not pool:
        return cfg["default_profile"]
    if cfg["selection_mode"] == "round-robin":
        name = pool[_round_robin_idx % len(pool)]
        _round_robin_idx += 1
        return name
    return random.choice(pool)


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
# Run one iteration via subprocess
# ──────────────────────────────────────────────────────────────

def run_one_iteration(profile_name: str) -> tuple:
    """Launch main.py with GHOST_SHELL_PROFILE env var so the worker picks it up."""
    started = time.time()

    env = os.environ.copy()
    env["GHOST_SHELL_PROFILE"] = profile_name

    try:
        result = subprocess.run(
            [sys.executable, "-u", "main.py"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env,
            timeout=30 * 60,
        )
        return result.returncode, time.time() - started
    except subprocess.TimeoutExpired:
        logging.error(f"Run for {profile_name} timed out after 30 min")
        return -1, time.time() - started
    except Exception as e:
        logging.error(f"subprocess failed: {e}")
        return -1, time.time() - started


# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────

def main():
    db = get_db()
    db.config_set("scheduler.started_at",
                  datetime.now().isoformat(timespec="seconds"))
    heartbeat()

    cfg = load_cfg()
    logging.info("═" * 60)
    logging.info(" SCHEDULER STARTED")
    logging.info(f" Target runs/day   : {cfg['target_runs']}")
    logging.info(f" Active hours      : {cfg['active_hours'][0]:02d}:00 – "
                 f"{cfg['active_hours'][1]:02d}:00")
    logging.info(f" Profiles          : {cfg['profile_names'] or '(default ' + cfg['default_profile'] + ')'}")
    logging.info(f" Selection mode    : {cfg['selection_mode']}")
    logging.info(f" Interval          : {cfg['min_interval']}–{cfg['max_interval']}s "
                 f"(+{cfg['jitter_percent']}% jitter)")
    logging.info("═" * 60)

    try:
        while not _shutdown:
            cfg = load_cfg()
            heartbeat()

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

            profile = pick_profile(cfg)
            run_num = done_today + 1

            logging.info("")
            logging.info(
                f"▶ Run {run_num}/{cfg['target_runs']} at "
                f"{datetime.now().strftime('%H:%M:%S')} — profile: {profile}"
            )
            heartbeat({"last_run_profile": profile})

            exit_code, duration = run_one_iteration(profile)
            if exit_code == 0:
                logging.info(f"✓ Run #{run_num} ok ({duration:.0f}s)")
            else:
                logging.error(
                    f"✗ Run #{run_num} failed "
                    f"(exit={exit_code}, {duration:.0f}s)"
                )

            if _shutdown:
                break

            interval = calc_interval(cfg, runs_today())
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
