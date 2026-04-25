"""
watchdog.py — Browser hang protection.

Monitors a Selenium driver from a separate thread. If the main thread
stops calling heartbeat() for `max_stall_sec`, OR if the driver stops
responding to pings, the watchdog:
  1. Records a "browser_hang" event in the DB (visible in Runs page)
  2. Invokes user-defined on_hang callback
  3. Kills the Chrome / chromedriver processes forcefully

Usage (context manager):

    from ghost_shell.browser.watchdog import BrowserWatchdog

    with BrowserWatchdog(browser.driver, run_id=RUN_ID) as dog:
        for query in queries:
            dog.heartbeat()            # signal: main thread alive
            do_search(query)

            with dog.pause("scraping heavy page"):
                # watchdog won't fire during this block — useful for
                # operations you know take a long time
                big_scrape()

Usage (manual):

    dog = BrowserWatchdog(driver, run_id=123)
    dog.start()
    try:
        ...
        dog.heartbeat()
        ...
    finally:
        dog.stop()
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import time
import signal
import logging
import threading
import contextlib
from datetime import datetime
from typing import Callable, Optional


class BrowserWatchdog:
    """
    Background thread that monitors driver liveness.

    Two signals are watched:
      1. Heartbeat age — main thread must call `heartbeat()` at least
         once per `max_stall_sec`. If it doesn't, we assume the main
         thread itself is stuck (e.g. deadlock, never-returning CDP call).
      2. Ping response — every `check_interval` seconds, we read
         `driver.title` from a sub-thread with a short timeout. If the
         driver can't reply, the browser is hung.

    Either signal triggers recovery: callback + kill.
    """

    def __init__(
        self,
        driver,
        run_id: Optional[int] = None,
        profile_name: str = "",
        max_stall_sec: int = 120,
        check_interval: int = 15,
        ping_timeout_sec: float = 10.0,
        on_hang: Optional[Callable[[dict], None]] = None,
    ):
        self.driver           = driver
        self.run_id           = run_id
        self.profile_name     = profile_name
        self.max_stall_sec    = max_stall_sec
        self.check_interval   = check_interval
        self.ping_timeout_sec = ping_timeout_sec
        self.on_hang          = on_hang

        self._stop         = threading.Event()
        self._thread       = None
        self._paused_until = 0.0            # epoch seconds
        self._pause_lock   = threading.Lock()
        self._last_beat    = time.time()

        # Process tracking — captured at start()
        self._driver_pid   = None           # chromedriver service pid
        self._browser_pids: list[int] = []  # chrome.exe pids

    # ─── Public API ────────────────────────────────────────────

    def heartbeat(self):
        """Main thread signals it's alive and making progress."""
        self._last_beat = time.time()

    @contextlib.contextmanager
    def pause(self, reason: str = ""):
        """
        Temporarily suspend hang detection for operations you know take
        a long time (e.g. heavy scrape, captcha solving).

        Usage:
            with dog.pause("solving captcha"):
                captcha_solver.solve()   # watchdog tolerant here
        """
        # Pause for up to (reason-free) 10 minutes — safety net
        pause_seconds = 10 * 60
        with self._pause_lock:
            self._paused_until = time.time() + pause_seconds
        logging.debug(f"[Watchdog] paused ({reason or 'no reason'})")
        try:
            yield
        finally:
            with self._pause_lock:
                self._paused_until = 0.0
            self.heartbeat()    # reset to fresh state
            logging.debug(f"[Watchdog] resumed ({reason or 'no reason'})")

    def start(self):
        """Start the monitor thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_beat = time.time()
        self._collect_pids()

        self._thread = threading.Thread(
            target=self._watch_loop, daemon=True, name="BrowserWatchdog"
        )
        self._thread.start()
        logging.info(
            f"[Watchdog] started (max_stall={self.max_stall_sec}s, "
            f"ping_every={self.check_interval}s)"
        )

    def stop(self):
        """Stop the monitor thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logging.debug("[Watchdog] stopped")

    # ─── Context manager ───────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # ─── Internals ─────────────────────────────────────────────

    def _collect_pids(self):
        """Discover the chromedriver + chrome.exe pids (best-effort)."""
        try:
            service = getattr(self.driver, "service", None)
            if service and getattr(service, "process", None):
                self._driver_pid = service.process.pid
        except Exception:
            pass

        # Browser pids via psutil (best option on Windows)
        try:
            import psutil
            if self._driver_pid:
                try:
                    parent = psutil.Process(self._driver_pid)
                    for child in parent.children(recursive=True):
                        name = (child.name() or "").lower()
                        if "chrome" in name:
                            self._browser_pids.append(child.pid)
                except psutil.NoSuchProcess:
                    pass
        except ImportError:
            # psutil not installed — degrade to driver pid only
            logging.debug("[Watchdog] psutil missing, pid tracking limited")

    def _is_paused(self) -> bool:
        with self._pause_lock:
            return time.time() < self._paused_until

    def _watch_loop(self):
        while not self._stop.is_set():
            # Sleep in 1-second slices so stop() is responsive
            for _ in range(self.check_interval):
                if self._stop.is_set():
                    return
                time.sleep(1)

            if self._is_paused():
                continue

            stalled = time.time() - self._last_beat

            # Hard limit — main thread hasn't pinged us in max_stall_sec,
            # something is definitely wrong (probably a hung driver.get).
            if stalled >= self.max_stall_sec:
                self._handle_hang(
                    reason=f"main-thread heartbeat older than {stalled:.0f}s "
                           f"(limit {self.max_stall_sec}s)"
                )
                return

            # Active driver ping is DISABLED by default because it
            # races with main-thread selenium calls through the same
            # HTTP connection pool. Even a cheap `driver.title` was
            # competing with `driver.get()` and producing
            # "Connection pool is full" warnings + false positives
            # ("driver unresponsive — retrying ping" during a totally
            # normal page load).
            #
            # Heartbeat-based stall detection is sufficient: main.py
            # calls dog.heartbeat() between steps, and a real hang
            # (dead browser, network black hole) will definitely fail
            # to advance heartbeats within max_stall_sec.
            #
            # To re-enable active ping (if you ever need it), set
            # self.enable_active_ping = True in __init__.
            if getattr(self, "enable_active_ping", False):
                # Only ping when heartbeat is getting old — within 30s
                # of the hard limit. No point pinging during active work.
                if stalled > max(30, self.max_stall_sec * 0.5):
                    alive = self._ping_driver()
                    if not alive:
                        logging.warning(
                            "[Watchdog] driver unresponsive AND main thread "
                            "stalled — retrying ping"
                        )
                        time.sleep(5)
                        if not self._ping_driver():
                            self._handle_hang(
                                reason=f"driver unresponsive for "
                                       f"{self.ping_timeout_sec*2:.0f}s "
                                       f"and heartbeat {stalled:.0f}s old"
                            )
                            return

    def _ping_driver(self) -> bool:
        """
        Call a lightweight driver API from a sub-thread with timeout.
        Returns True if response arrived in time.
        """
        result = {"ok": False}

        def ping():
            try:
                _ = self.driver.title   # cheap round-trip
                result["ok"] = True
            except Exception as e:
                logging.debug(f"[Watchdog] ping raised: {e}")

        t = threading.Thread(target=ping, daemon=True)
        t.start()
        t.join(timeout=self.ping_timeout_sec)
        return result["ok"]

    def _handle_hang(self, reason: str):
        """
        Common path: log, record DB event, invoke callback, kill processes.
        """
        details = {
            "reason":       reason,
            "profile":      self.profile_name,
            "run_id":       self.run_id,
            "driver_pid":   self._driver_pid,
            "browser_pids": self._browser_pids,
            "timestamp":    datetime.now().isoformat(timespec="seconds"),
        }
        logging.error(f"[Watchdog] 🚨 browser hang — {reason}")

        # Record in DB so it shows up in Runs / Logs
        try:
            from ghost_shell.db.database import get_db
            get_db().event_record(
                run_id       = self.run_id,
                profile_name = self.profile_name or "unknown",
                event_type   = "browser_hang",
                details      = reason,
            )
            get_db().log_add(self.run_id, "error",
                             f"[Watchdog] browser hang — {reason}")
        except Exception as e:
            logging.debug(f"[Watchdog] DB log failed: {e}")

        # User callback (before killing — lets caller save state etc.)
        if self.on_hang:
            try:
                self.on_hang(details)
            except Exception as e:
                logging.error(f"[Watchdog] on_hang callback error: {e}")

        self._kill_processes()

    def _kill_processes(self):
        """Forcefully terminate chrome + chromedriver processes."""
        # Try psutil first (cleanest)
        try:
            import psutil
            for pid in set(self._browser_pids + [self._driver_pid]):
                if not pid:
                    continue
                try:
                    proc = psutil.Process(pid)
                    proc.kill()
                    logging.warning(f"[Watchdog] kill {proc.name()} pid={pid}")
                except psutil.NoSuchProcess:
                    continue
                except Exception as e:
                    logging.error(f"[Watchdog] psutil kill {pid}: {e}")
            return
        except ImportError:
            pass

        # Fallback: OS-level kill
        for pid in set(self._browser_pids + [self._driver_pid]):
            if not pid:
                continue
            try:
                if os.name == "nt":
                    os.system(f"taskkill /F /T /PID {pid} >nul 2>&1")
                else:
                    os.kill(pid, signal.SIGKILL)
                logging.warning(f"[Watchdog] killed pid={pid}")
            except Exception as e:
                logging.error(f"[Watchdog] kill {pid}: {e}")

        # Last resort: kill any orphan chromedriver (Windows)
        if os.name == "nt":
            try:
                os.system("taskkill /F /IM chromedriver.exe >nul 2>&1")
                os.system("taskkill /F /IM undetected_chromedriver.exe >nul 2>&1")
            except Exception:
                pass
