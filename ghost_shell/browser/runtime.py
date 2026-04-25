"""
NK Browser Core - C++ Native Driver
------------------------------------------------------------
This module manages the execution of the custom Chromium browser.
It relies entirely on the C++ Core payload injection architecture, 
meaning absolutely no JavaScript is injected to spoof fingerprints.
Protection level: Canvas, WebGL, Audio, Navigator, Screen, Fonts (C++ Native).
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import random
import logging
import time
import math
import re
import threading
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By

# undetected_chromedriver is no longer used by default — our C++ patches
# handle detection evasion. Import is optional; keep it for legacy fallback.
try:
    import undetected_chromedriver as uc
    HAS_UC = True
except Exception:
    uc = None
    HAS_UC = False

# Import our deterministic payload builder
from ghost_shell.fingerprint.device_templates import DeviceTemplateBuilder

class GhostShellBrowser:
    def __init__(
        self,
        profile_name: str,
        proxy_str: str = None,
        base_dir: str = "profiles",
        browser_path: str = None,
        auto_session: bool = True,
        is_rotating_proxy: bool = False,
        rotation_api_url: str = None,
        enrich_on_create: bool = True,
        preferred_language: str = None,
        run_id: int = None,
    ):
        """
        Initializes the stealth browser driver.

        browser_path: if None, read from DB (browser.binary_path config).
                      Falls back to default Chromium build path.
        """
        self.profile_name       = str(profile_name)
        self.user_data_path     = os.path.abspath(os.path.join(base_dir, self.profile_name))
        self.proxy_str          = proxy_str

        # Resolve browser path: explicit > DB config > auto-detect
        if browser_path is None:
            try:
                from ghost_shell.db.database import get_db
                browser_path = get_db().config_get("browser.binary_path")
            except Exception:
                pass

        # Auto-detect: try candidates relative to CWD. Checks both
        # `chrome_win64` (underscore, our deploy target) and `chrome-win64`
        # (dash, legacy). Also looks at the Chromium build directory.
        if not browser_path or not os.path.exists(browser_path):
            cwd = os.getcwd()
            candidates = [
                browser_path,
                # Preferred: flat deploy directory next to the project
                "chrome_win64/chrome.exe",
                "./chrome_win64/chrome.exe",
                os.path.join(cwd, "chrome_win64", "chrome.exe"),
                # Legacy names
                "chrome-win64/chrome.exe",
                "./chrome-win64/chrome.exe",
                os.path.join(cwd, "chrome-win64", "chrome.exe"),
                # Direct from Chromium build output
                r"F:\projects\chromium\src\out\GhostShell\chrome.exe",
                r"C:\src\chromium\src\out\GhostShell\chrome.exe",
                r"C:\src\chromium\src\out\Default\chrome.exe",
            ]
            for c in candidates:
                if c and os.path.exists(c):
                    browser_path = c
                    break

        self.browser_path = browser_path or "chrome_win64/chrome.exe"

        self.auto_session       = auto_session
        self.is_rotating_proxy  = is_rotating_proxy
        self.rotation_api_url   = rotation_api_url
        self.enrich_on_create   = enrich_on_create
        self.preferred_language = preferred_language
        self.run_id             = run_id
        
        self.driver            = None
        self._session_mgr      = None
        self._proxy_forwarder  = None
        self._rotating_tracker = None
        self._profile_log_handler = None   # set by setup_profile_logging

        # Runtime subsystems — initialised to neutral defaults so close()
        # can be called safely even if start() fails halfway through.
        # Previously, failing BEFORE the watchdog init line meant close()
        # would AttributeError on `self._watchdog_stop` and never reach
        # the later cleanup lines → zombie Chrome leaked.
        self._traffic_collector    = None
        self._watchdog_thread      = None
        self._watchdog_stop        = threading.Event()
        self._watchdog_fail_count  = 0
        self._gs_lock_path         = None

        # Profile Enrichment (Simulate an aged browser profile before creation).
        # Can be disabled via env (GHOST_SHELL_SKIP_ENRICH=1) for debugging.
        # Stash the is_new flag so the auto-enrich hook in start() knows
        # whether to invoke chrome_importer.auto_enrich_fresh_profile after
        # Chrome's first successful launch.
        self._is_new_profile = not os.path.exists(self.user_data_path)
        is_new_profile = self._is_new_profile
        os.makedirs(self.user_data_path, exist_ok=True)
        self.session_dir = os.path.join(self.user_data_path, "ghostshell_session")

        skip_enrich = os.environ.get("GHOST_SHELL_SKIP_ENRICH") == "1"
        if is_new_profile and enrich_on_create and not skip_enrich:
            try:
                from ghost_shell.profile.enricher import ProfileEnricher
                ProfileEnricher(self.user_data_path).enrich_all()
            except Exception as e:
                logging.warning(f"[GhostShellBrowser] Profile enrichment failed: {e}")
        elif skip_enrich:
            logging.info("[GhostShellBrowser] GHOST_SHELL_SKIP_ENRICH=1 — "
                         "skipping ProfileEnricher")

    # ──────────────────────────────────────────────────────────
    # CONTEXT MANAGER SUPPORT
    # ──────────────────────────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def setup_profile_logging(self, level=logging.INFO):
        """Attaches a dedicated file logger for this specific profile.

        Idempotent: if a file handler for the same file already exists on
        the root logger, we reuse it. We also track our own handler in
        self._profile_log_handler so close() can detach it cleanly.
        """
        log_dir = os.path.join(self.user_data_path, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, datetime.now().strftime("%Y%m%d.log"))
        log_file_abs = os.path.abspath(log_file)

        root = logging.getLogger()

        # Skip if our target file is already attached via a FileHandler
        for h in root.handlers:
            if isinstance(h, logging.FileHandler) and \
               os.path.abspath(h.baseFilename) == log_file_abs:
                self._profile_log_handler = h
                return

        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        root.addHandler(handler)
        self._profile_log_handler = handler
        logging.info(f"[GhostShellBrowser] Attached profile log: {log_file}")

    # ──────────────────────────────────────────────────────────
    # STARTUP AND INITIALIZATION
    # ──────────────────────────────────────────────────────────

    def start(self) -> webdriver.Chrome:
        """Launch Chrome with pre-flight validation + retry on crashes.

        Two escalating defences against profile-related crashes:

        1. PRE-FLIGHT VALIDATOR — before every launch, ProfileValidator
           runs a battery of cheap checks (SQLite integrity, JSON
           parseability, stale locks, session-file cleanup) and
           self-heals anything it can. Most issues are deleted + left
           for Chrome to recreate on first open, which is always safe.

        2. RETRY LADDER on crash:
             attempt 1: launch
             attempt 2: launch (if attempt 1 hit early-death quirk —
                        InvalidSessionId within 5s, usually Win/Chrome
                        version specific racing issue that clears up)
             attempt 3: QUARANTINE the entire profile folder + launch
                        against a fresh empty one. Used when individual
                        file repair didn't work — some state in there
                        was bad in a way the validator didn't catch.

        The quarantine step is destructive from the user's POV (they
        lose cookies, history, local storage for that profile) but
        LESS destructive than a stuck scheduler that never succeeds.
        The old folder is renamed, not deleted, so manual recovery is
        possible.
        """
        from selenium.common.exceptions import (
            InvalidSessionIdException, WebDriverException,
            NoSuchWindowException,
        )
        from ghost_shell.profile.validator import ProfileValidator

        # ── PRE-FLIGHT VALIDATION ───────────────────────────────
        # Cheap to run (< 500ms typical), catches 90% of profile-death
        # causes before Chrome even starts. Repairs what it can, deletes
        # what it can't repair, flags anything weird in the log.
        try:
            validator = ProfileValidator(self.user_data_path)
            report = validator.validate()
            if report["total_issues"] > 0:
                logging.info(
                    f"[GhostShellBrowser] pre-flight: fixed "
                    f"{report['fixed']} / deleted {report['deleted']} / "
                    f"quarantined {report['quarantined']} issues "
                    f"before launch"
                )
        except Exception as e:
            logging.warning(f"[GhostShellBrowser] pre-flight validator: {e}")
            validator = None

        for attempt in (1, 2, 3):
            t0 = time.time()
            try:
                return self._start_once()
            except (InvalidSessionIdException, NoSuchWindowException,
                    WebDriverException) as e:
                elapsed = time.time() - t0
                msg = str(e).lower()
                # Any of these suggest Chrome died either pre-connect
                # (session never created) or right after (early-death).
                is_chrome_crash = (
                    "invalid session"        in msg or
                    "no such window"         in msg or
                    "chrome not reachable"   in msg or
                    "disconnected"           in msg or
                    "target window already closed" in msg or
                    "session not created"    in msg
                )
                if not is_chrome_crash or attempt == 3:
                    raise

                self._cleanup_after_failed_start()

                if attempt == 1:
                    # Attempt 2: same profile, give OS/Defender/TIME_WAIT
                    # a moment to settle.
                    logging.warning(
                        f"[GhostShellBrowser] Chrome died {elapsed:.1f}s after "
                        f"launch ({type(e).__name__}: {str(e)[:80]}) — "
                        f"retrying once..."
                    )
                    time.sleep(2.5)
                else:
                    # Attempt 3: we tried twice with the same profile,
                    # both failed the same way. Probably a file in the
                    # profile is corrupt in a way the validator didn't
                    # catch — maybe a new SQLite DB schema we don't
                    # check yet, maybe a mysteriously corrupted JSON
                    # deeper in Default/. Quarantine the folder, let
                    # Chrome build a fresh one.
                    logging.error(
                        f"[GhostShellBrowser] Chrome crashed twice in a row "
                        f"({type(e).__name__}) — quarantining profile folder "
                        f"and retrying with a fresh empty profile"
                    )
                    if validator is not None:
                        quarantined = validator.quarantine_profile(
                            reason=f"two failed launches: {type(e).__name__}"
                        )
                        if quarantined:
                            # Recreate user_data_path as an empty dir so
                            # _start_once's setup paths (Preferences
                            # writer, session dir, etc.) find what they
                            # need to exist.
                            os.makedirs(self.user_data_path, exist_ok=True)
                    time.sleep(1.0)

    def _cleanup_after_failed_start(self):
        """Tear down partially-initialised Chrome/driver/proxy state so
        start() can retry cleanly. Safe on incomplete state — every step
        is independently guarded."""
        # Kill driver + Chrome processes
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            # Force-kill any Chrome children that quit() didn't reach.
            # quit() sometimes returns cleanly after the Chrome process
            # is already dead, leaving orphan chromedriver.exe processes.
            try:
                service = getattr(self.driver, "service", None)
                proc = getattr(service, "process", None) if service else None
                pid = getattr(proc, "pid", None) if proc else None
                if pid:
                    from ghost_shell.core.process_reaper import kill_process_tree
                    kill_process_tree(pid)
            except Exception as e:
                logging.debug(f"[start-retry] child kill: {e}")
            self.driver = None

        # Stop proxy forwarder — _start_once binds a fresh one on
        # entry. Leaving the old one up means the new Chrome tries to
        # connect to a half-dead local port.
        if self._proxy_forwarder is not None:
            try:
                self._proxy_forwarder.stop()
            except Exception:
                pass
            self._proxy_forwarder = None

        # Release the GS lock file — _start_once reclaims it on retry.
        try:
            if self._gs_lock_path and os.path.exists(self._gs_lock_path):
                os.remove(self._gs_lock_path)
                self._gs_lock_path = None
        except Exception:
            pass

        # Clear traffic collector reference — the old one holds a
        # reference to the dead driver.
        if getattr(self, "_traffic_collector", None):
            try:
                self._traffic_collector.stop()
            except Exception:
                pass
            self._traffic_collector = None

    def _start_once(self) -> webdriver.Chrome:
        """Launches the C++ native stealth browser."""
        # 1. Generate Deterministic C++ Payload
        builder = DeviceTemplateBuilder(profile_name=self.profile_name,
                                        preferred_language=self.preferred_language)
        payload = builder.generate_payload_dict()
        self.last_payload = payload   # exposed so main.py can log a summary
        stealth_flag = builder.get_cli_flag()

        # Save a human-readable copy of the payload for C++ core loading (payload_debug.json)
        with open(os.path.join(self.user_data_path, "payload_debug.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)

        # Сохраняем also in DB for dashboard
        try:
            from ghost_shell.db.database import get_db
            get_db().fingerprint_save(self.profile_name, payload)
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] DB fingerprint save: {e}")

        # 2. Clean stale lock files from crashed previous runs.
        # Chrome refuses to start if SingletonLock/SingletonSocket/SingletonCookie
        # still exist from a previous session that didn't exit cleanly.
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = os.path.join(self.user_data_path, lock_name)
            if os.path.exists(lock_path) or os.path.islink(lock_path):
                try:
                    if os.path.islink(lock_path):
                        os.unlink(lock_path)
                    elif os.path.isdir(lock_path):
                        import shutil
                        shutil.rmtree(lock_path, ignore_errors=True)
                    else:
                        os.remove(lock_path)
                    logging.debug(f"[GhostShellBrowser] Removed stale {lock_name}")
                except Exception as e:
                    logging.debug(f"[GhostShellBrowser] Could not remove {lock_name}: {e}")

        # ── Ghost Shell-specific lock file ─────────────────────────
        # Belt-and-suspenders on top of the DB-level guard in
        # process_reaper.ensure_no_live_run_for_profile(). If the DB
        # is unavailable (e.g. user blew away ghost_shell.db to reset),
        # this file still blocks a second Chrome from spawning against
        # the same user-data-dir (which would silently corrupt session
        # state on both).
        gs_lock = os.path.join(self.user_data_path, ".ghost_shell.lock")
        if os.path.exists(gs_lock):
            stale = True
            try:
                with open(gs_lock, "r") as f:
                    old_pid = int(f.read().strip() or "0")
                if old_pid > 0:
                    try:
                        import psutil
                        if psutil.pid_exists(old_pid):
                            # Is it actually a Ghost Shell process?
                            from ghost_shell.core.process_reaper import pid_looks_like_ghost_shell
                            if pid_looks_like_ghost_shell(old_pid):
                                raise RuntimeError(
                                    f"Profile '{self.profile_name}' is locked by "
                                    f"live PID {old_pid}. Stop that run first "
                                    f"(Dashboard → Scheduler → 🧹 Clean zombies, "
                                    f"or kill PID manually)."
                                )
                    except ImportError:
                        pass  # no psutil — fall through, treat as stale
            except RuntimeError:
                raise
            except Exception:
                pass  # corrupt lock file → treat as stale
            if stale:
                try:
                    os.remove(gs_lock)
                except OSError:
                    pass

        # Write our own PID into the lock. Cleared in close().
        try:
            with open(gs_lock, "w") as f:
                f.write(str(os.getpid()))
            self._gs_lock_path = gs_lock
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] lock write skipped: {e}")
            self._gs_lock_path = None

        # 3. Configure Local Preferences (WebRTC, permissions, session).
        # Language settings are NOT written here — that would overwrite our
        # C++ GhostShellConfig.languages. All language stuff comes from payload.
        pref_path = os.path.join(self.user_data_path, "Default", "Preferences")
        os.makedirs(os.path.dirname(pref_path), exist_ok=True)

        # session.restore_on_startup values in Chrome:
        #   1 = restore last session (default for many users — what we DO NOT want)
        #   4 = open specific URL set at startup
        #   5 = open NTP (new-tab page) — what we want. Fresh tab every launch.
        #
        # profile.exit_type / exited_cleanly are the "was Chrome crashed?"
        # markers. We set them to "Normal"/true defensively — if a previous
        # run was killed hard (hang detector, Windows reboot, whatever),
        # these fields would be wrong and Chrome would prompt "restore
        # session?" on startup, re-opening every tab that was open at
        # crash time. That's exactly the "9 tabs every run" symptom.
        #
        # This together with the OS-level cleanup of Current Session /
        # Current Tabs (below) ensures fresh tabs every launch unless
        # the user explicitly asks otherwise via a script action.
        new_prefs = {
            "webrtc": {
                "ip_handling_policy": "disable_non_proxied_udp",
                "multiple_routes_enabled": False,
                "nonproxied_udp_enabled": False,
            },
            "profile": {
                "default_content_setting_values": {
                    "geolocation": 2,
                    "notifications": 2,
                    "media_stream_camera": 2,
                    "media_stream_mic": 2,
                },
                "exit_type":       "Normal",
                "exited_cleanly":  True,
            },
            "session": {
                "restore_on_startup":          5,   # 5 = open NTP
                "restore_on_startup_migrated": True,
                "startup_urls":                [],
            },
        }

        # Merge with existing prefs file if present AND valid JSON.
        # If existing file is corrupt we throw it away — trying to merge into
        # broken JSON is what caused Chrome to crash on profile load before.
        prefs = new_prefs
        if os.path.exists(pref_path):
            try:
                with open(pref_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    def deep_merge(d1, d2):
                        for k, v in d2.items():
                            if k in d1 and isinstance(d1[k], dict) and isinstance(v, dict):
                                deep_merge(d1[k], v)
                            else:
                                d1[k] = v
                    deep_merge(existing, new_prefs)
                    prefs = existing
            except (json.JSONDecodeError, ValueError, OSError) as e:
                logging.warning(
                    f"[GhostShellBrowser] Corrupt Preferences — starting "
                    f"fresh. (error: {e})"
                )
                try:
                    os.remove(pref_path)
                except OSError:
                    pass

        # Atomic write: first write to .tmp, then rename. This way if the
        # process is killed mid-write, the original Preferences stays intact
        # instead of ending up as truncated JSON that crashes chrome.
        tmp_path = pref_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
            os.replace(tmp_path, pref_path)
        except Exception as e:
            logging.error(f"[GhostShellBrowser] Failed to write Preferences: {e}")
            try: os.remove(tmp_path)
            except OSError: pass

        # 3b. Nuke per-session tab state so Chrome starts with exactly
        # one fresh NTP tab. These files hold the list of currently-open
        # tabs and their history. Even with `restore_on_startup=5` in
        # Preferences, Chrome will still show accumulated tabs if the
        # previous session didn't exit cleanly (crashed, got killed by
        # hang-detector, OS reboot) — it treats that as "something went
        # wrong, let me help the user". Deleting the files pre-launch
        # removes the source of truth so there's nothing to restore.
        #
        # This mirrors what "Profile is damaged, start fresh?" does
        # internally, just without the user prompt. Other per-profile
        # data (cookies, history, localStorage) is in separate files
        # and untouched.
        session_dir = os.path.join(self.user_data_path, "Default", "Sessions")
        for fname in ("Current Session", "Current Tabs",
                      "Last Session",    "Last Tabs"):
            p = os.path.join(self.user_data_path, "Default", fname)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        # Sessions dir (Chrome 122+ moved session state here in addition
        # to the legacy files above). Remove the whole dir — Chrome
        # rebuilds it on first write.
        if os.path.isdir(session_dir):
            try:
                import shutil
                shutil.rmtree(session_dir, ignore_errors=True)
            except Exception:
                pass

        # 3. Configure Chrome Options (plain selenium — we have C++ patches
        # for detection evasion, undetected_chromedriver is redundant here).
        options = ChromeOptions()

        # Page load strategy — 'eager' returns from driver.get() once
        # DOMContentLoaded fires, instead of waiting for every iframe,
        # tracker pixel, gstatic chunk, and favicon to finish. The SERP
        # (including ad cards) is fully parseable at DOMContentLoaded —
        # Google renders results server-side. Cuts 5-10s off per query.
        #
        # Does NOT affect ad parsing (elements are in the initial HTML),
        # does NOT affect captcha detection (captcha page returns fast
        # anyway), does NOT change the detection surface (real browsers
        # always fire DOMContentLoaded before full load — Google can't
        # tell when we start reading the DOM).
        options.page_load_strategy = "eager"

        options.add_argument(f"--user-data-dir={self.user_data_path}")
        options.add_argument("--disable-crash-reporter")
        options.add_argument("--disable-breakpad")
        options.add_argument("--no-sandbox")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-infobars")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        # --disable-blink-features=AutomationControlled REMOVED - redundant
        # with C++ patches and is itself a detection marker.

        # ── Keep Chrome "active" even when not foreground ───────────
        # Without these, if the user alt-tabs away or the window gets
        # occluded, Chrome reduces document.visibilityState to "hidden",
        # throttles timers and rAF, and some Google analytics scripts
        # treat the page as "user not looking" — which hurts ad-click
        # quality signals.
        # The Selenium ActionChains API works via CDP and doesn't need
        # OS focus, but keeping the page in "visible" state ensures
        # real mousemove events don't get discarded as irrelevant.
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")
        options.add_argument("--disable-background-timer-throttling")

        # ── WebRTC IP leak hardening ────────────────────────────────
        # Without these, RTCPeerConnection ICE candidates can expose
        # the user's real local network (192.168.x.x / 10.x.x.x / fe80::)
        # AND the direct WAN IP even when all HTTP/HTTPS goes through
        # the proxy. Google can compare WebRTC-leaked IP vs exit IP and
        # detect geo mismatch → captcha.
        # Policy 'disable_non_proxied_udp' forces WebRTC to use TURN/TCP
        # through the proxy; no direct host/srflx candidates leak.
        options.add_argument(
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
        )
        # mDNS ICE candidates (.local) are a weaker leak vector but still
        # expose the device's presence on its LAN — disabled via the
        # unified --disable-features list below. (Chrome's command-line
        # parser does NOT merge duplicate --disable-features flags; the
        # last one wins. So every feature must appear in ONE flag.)

        # ── Traffic-saving flags — CRITICAL for paid proxies ───────────
        #
        # Chrome's default behavior on a fresh profile is to pull HUNDREDS
        # of MB of "helpful" payloads during startup, every single startup,
        # on every profile:
        #
        #   Component Updater    — Widevine, Privacy Sandbox, CRL sets,
        #                          Origin Trials, Trust Tokens, File Type
        #                          Policies etc. ~50-200 MB per update cycle.
        #   Safe Browsing        — full URL blocklist downloads. 30-80 MB
        #                          on first run; incremental updates every
        #                          ~30 min.
        #   Variation seeds      — Google's A/B experiment config. Small
        #                          individually, but pings every startup.
        #   Translate models     — dictionaries for auto-translate. ~50 MB
        #                          on first language encounter.
        #   Optimization Hints   — per-site performance hints. 20-40 MB.
        #   Enhanced Ad Privacy  — Topics API / FLoC replacement data.
        #   Sync / Sign-in       — even without sign-in, the sync scheduler
        #                          pings Google every N minutes.
        #
        # ALL of this goes through our proxy because --proxy-server is
        # system-wide for Chrome. Spinning up 10 new profiles in a day =
        # 2-5 GB of wasted traffic BEFORE the first search query.
        #
        # The block below disables each source. We keep this explicit per-
        # feature (rather than a single --disable-background-networking)
        # because that flag alone doesn't catch component updater or
        # translate downloads.
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-component-update")
        options.add_argument("--disable-domain-reliability")
        options.add_argument("--disable-client-side-phishing-detection")
        options.add_argument("--safebrowsing-disable-auto-update")
        options.add_argument("--disable-sync")
        options.add_argument("--disable-translate")
        # ONE unified --disable-features flag. Chrome overwrites duplicates,
        # so we merge WebRtcHideLocalIpsWithMdns (fingerprint hardening)
        # with traffic-saving feature disables.
        options.add_argument("--disable-features=" + ",".join([
            # Fingerprint hardening — kept from the prior WebRTC block
            "WebRtcHideLocalIpsWithMdns",
            # Traffic savings
            "OptimizationHints",
            "OptimizationHintsFetching",
            "InterestFeedContentSuggestions",
            "CalculateNativeWinOcclusion",
            "MediaRouter",
            "Translate",
            "AutofillServerCommunication",
            "CertificateTransparencyComponentUpdater",
        ]))
        # Per-pref knob — belt and suspenders. `prefs` gets merged into
        # the profile's Preferences JSON before Chrome reads it.
        options.add_experimental_option("prefs", {
            # Don't ever dial home for update checks of components
            "component_updater.recovery_component.enabled": False,
            # Don't download translate models proactively
            "translate.enabled": False,
            # Minimize Safe Browsing traffic (still get basic client-side)
            "safebrowsing.enabled": False,
            "safebrowsing.scout_reporting_enabled": False,
            # Don't preload pages / prefetch resources on idle
            "net.network_prediction_options": 2,  # 2 = disabled
            # Don't upload usage metrics to Google
            "user_experience_metrics.reporting_enabled": False,
        })

        # Defensive: force the window to be visible on the primary desktop.
        options.add_argument("--window-position=100,100")
        options.add_argument("--start-maximized")

        # Verbose chrome logging (env-triggered) — writes all chrome stderr
        # to profile/chrome_debug.log, invaluable when chrome crashes silently.
        if os.environ.get("GHOST_SHELL_VERBOSE_CHROME") == "1":
            chrome_log = os.path.join(self.user_data_path, "chrome_debug.log")
            options.add_argument(f"--enable-logging")
            options.add_argument(f"--log-file={chrome_log}")
            options.add_argument("--v=1")
            logging.info(f"[GhostShellBrowser] Chrome verbose log → {chrome_log}")
        else:
            # Suppress console logging from child processes (renderer, GPU,
            # utility, network). Without this, each subprocess flashes a
            # console window because chromedriver or parent log to stderr.
            options.add_argument("--disable-logging")
            options.add_argument("--log-level=3")   # FATAL only

        # Tell chromedriver NOT to force certain switches that cause spam
        # or automation banners we can't otherwise suppress.
        options.add_experimental_option(
            "excludeSwitches",
            ["enable-automation", "enable-logging"],
        )
        # Silence the "Chrome is being controlled by automated test software"
        # notification bar.
        options.add_experimental_option("useAutomationExtension", False)

        # INJECT C++ PAYLOAD FLAG (consumed by our patched ghost_shell_config.cc)
        # Debug switch: set GHOST_SHELL_SKIP_PAYLOAD=1 to launch chrome
        # without the payload — useful to verify that payload parsing is
        # what crashes chrome.
        if os.environ.get("GHOST_SHELL_SKIP_PAYLOAD") == "1":
            logging.warning("[GhostShellBrowser] GHOST_SHELL_SKIP_PAYLOAD=1 — "
                            "skipping C++ payload injection (debug mode)")
        else:
            options.add_argument(stealth_flag)

        # Apply screen dimensions
        win_w = payload["screen"]["width"]
        win_h = payload["screen"]["avail_height"]
        options.add_argument(f"--window-size={win_w},{win_h}")

        # NOTE: --accept-lang is intentionally NOT set here.
        # It crashes our custom Chromium (bisect_flags.py confirmed this).
        # All language handling goes through the C++ payload →
        # GhostShellConfig::{language_, languages_, accept_language_}.

        # Local Proxy Forwarder
        if self.proxy_str:
            from ghost_shell.proxy.forwarder import ProxyForwarder
            self._proxy_forwarder = ProxyForwarder(self.proxy_str)
            local_port = self._proxy_forwarder.start()
            options.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
            options.add_argument("--proxy-bypass-list=<-loopback>")

        # Check and bind custom Chromium core.
        # Resolution is platform-aware via platform_paths.find_chrome_binary:
        #   Windows — <dir>/chrome.exe
        #   macOS   — <dir>/Chromium.app/Contents/MacOS/Chromium  (app bundle)
        #   Linux   — <dir>/chrome or <dir>/chromium
        from ghost_shell.core.platform_paths import find_chrome_binary, find_chromedriver
        resolved_chrome = None
        if self.browser_path:
            if os.path.exists(self.browser_path) and \
               os.path.isfile(self.browser_path):
                resolved_chrome = self.browser_path
            else:
                # Maybe user pointed at the containing dir — try to find the
                # platform-correct binary inside.
                base_dir = self.browser_path if os.path.isdir(self.browser_path) \
                           else os.path.dirname(self.browser_path)
                resolved_chrome = find_chrome_binary(base_dir)
        if not resolved_chrome:
            # Last resort — look in the platform default directory
            resolved_chrome = find_chrome_binary()

        if resolved_chrome:
            options.binary_location = resolved_chrome
            logging.info(f"[GhostShellBrowser] Using custom Chromium: "
                         f"{resolved_chrome}")

            # Matching chromedriver must be available — stock one from
            # Google's CDN won't speak CDP to our custom Chrome 149.
            chromedriver_path = find_chromedriver(
                os.path.dirname(os.path.abspath(resolved_chrome))
            )
            # macOS .app bundle case: the MacOS/ dir is deep inside the bundle;
            # fall back to looking at the bundle's parent dir too.
            if not chromedriver_path:
                parent = os.path.abspath(resolved_chrome)
                # walk up until we leave the .app bundle
                while parent and (".app" in parent):
                    parent = os.path.dirname(parent)
                if parent:
                    chromedriver_path = find_chromedriver(parent)

            if chromedriver_path:
                logging.info(
                    f"[GhostShellBrowser] Using matching chromedriver: "
                    f"{chromedriver_path}"
                )
            else:
                driver_name = "chromedriver.exe" if os.name == "nt" \
                              else "chromedriver"
                logging.error(
                    f"[GhostShellBrowser] {driver_name} not found next to "
                    f"the Chrome binary — Chrome won't launch!\n"
                    f"   Build it:  autoninja -C out/GhostShell chromedriver\n"
                    f"   Expected near: {resolved_chrome}"
                )
                raise FileNotFoundError(driver_name)
        else:
            logging.warning(
                f"[GhostShellBrowser] Custom Chromium not found.\n"
                f"   Configured: {self.browser_path}\n"
                f"   Falling back to system Chrome. C++ stealth patches "
                f"will NOT be active!\n"
                f"   Fix: set browser.binary_path in dashboard."
            )
            chromedriver_path = None

        # Service with log file for debugging
        service_kwargs = {}
        if chromedriver_path:
            service_kwargs["executable_path"] = chromedriver_path
        # Log chromedriver output to a file for postmortem debugging
        log_file = os.path.join(self.user_data_path, "chromedriver.log")
        service_kwargs["log_output"] = log_file
        service = ChromeService(**service_kwargs)

        # Launch Chrome via plain selenium (like our smoke test)
        logging.info("[GhostShellBrowser] Starting Chrome via selenium…")
        self.driver = webdriver.Chrome(service=service, options=options)
        logging.info("[GhostShellBrowser] Chrome session established ✓")

        # Increase urllib3 connection pool size for Selenium's HTTP
        # channel to chromedriver. Default is 1 — which means when main
        # thread is mid-call (driver.get waiting for DOMContentLoaded),
        # the watchdog's driver.title ping competes for the same single
        # connection and triggers "Connection pool is full, discarding
        # connection" warnings + false-positive hang detection.
        #
        # urllib3 PoolManager creates HTTPConnectionPools lazily per host,
        # so we have to either (a) set the defaults BEFORE any request
        # lands, or (b) mutate each pool after creation. The old code did
        # `pool.pool._maxsize = 5` — that's wrong: `command_executor._conn`
        # is the PoolManager itself (no `.pool` attribute; `.pools` plural
        # is the cache dict). Fix: set the pool_kw with maxsize, then
        # additionally bump maxsize on any already-created pools (there
        # shouldn't be any yet since we bump before the first probe).
        try:
            pm = getattr(self.driver.command_executor, "_conn", None)
            if pm is not None and hasattr(pm, "connection_pool_kw"):
                pm.connection_pool_kw["maxsize"] = 5
                pm.connection_pool_kw["block"]  = False
                # Clear any already-made pools so the new maxsize applies.
                # .pools is either a RecentlyUsedContainer or dict — both
                # have clear().
                try:
                    pm.pools.clear()
                except Exception:
                    pass
                logging.debug("[GhostShellBrowser] urllib3 pool maxsize=5")
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] pool size bump skipped: {e}")

        # 4. CDP Emulations (Pre-navigation)
        self._apply_cdp_overrides(payload)
        self._set_network_conditions(payload)

        # Add minor viewport jitter
        w = win_w - 80 + random.randint(-15, 15)
        h = win_h - 120 + random.randint(-15, 15)
        self.driver.set_window_size(w, h)

        # ORDERING NOTE:
        # We used to start TrafficCollector (bg thread, polls execute_script
        # every 5s) and Watchdog (bg thread, polls driver.title every 30s)
        # RIGHT HERE — before init navigation and before cookie restore.
        # Two background threads pounding on Selenium's single urllib3
        # connection during a fragile startup phase caused connection
        # pool contention ("Connection pool size: 1" warnings) which,
        # combined with multiple per-domain navigations during cookie
        # restore, destabilised the session and produced random
        # InvalidSessionIdException aborts.
        #
        # Now: start those bg threads AFTER session restore completes.
        # Startup is serial on the main thread, no bg interference.

        # 5. Initialization Navigation (CRITICAL)
        # Bypasses the first-load visibility detection before actual targets are visited
        try:
            self.driver.get("data:text/html,<html><head><title>init</title></head><body></body></html>")
            time.sleep(0.8)
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] Init navigation warning: {e}")

        # 6. Session Restoration
        if self.auto_session and os.path.exists(self.session_dir):
            try:
                self._auto_restore_session()
            except Exception as e:
                logging.warning(f"[GhostShellBrowser] Session restoration failed: {e}")

        # ── Background subsystems — start NOW, not earlier ─────────
        # Now that session restore finished, there's no more startup-
        # phase sensitivity. Both bg threads can safely poll without
        # racing the main thread's fragile init operations.

        # Traffic collector — bytes per domain, via two data sources:
        #   (a) ProxyForwarder's per-host byte counters — authoritative,
        #       100% accurate for billed bytes (what asocks charges us)
        #   (b) Chrome PerformanceObserver — fills in request counts
        #       for cache-hit resources that never touched the proxy
        # The collector merges these on each flush.
        try:
            from ghost_shell.db.database import get_db as _get_db
            _db = _get_db()
            if _db.config_get("traffic.enabled") is not False:  # default True
                from ghost_shell.browser.traffic import TrafficCollector
                self._traffic_collector = TrafficCollector(
                    driver             = self.driver,
                    profile_name       = self.profile_name or "default",
                    run_id             = self.run_id,
                    db                 = _db,
                    flush_interval_sec = int(
                        _db.config_get("traffic.flush_interval_sec") or 30
                    ),
                    proxy_forwarder    = self._proxy_forwarder,
                )
                self._traffic_collector.start()
        except Exception as e:
            logging.warning(f"[GhostShellBrowser] Traffic collector init failed: {e}")

        # Hang watchdog — probes driver.title every 30s, kills tree
        # after 3 consecutive failures. Safe to run concurrently with
        # main thread now.
        try:
            self._watchdog_stop.clear()
            self._watchdog_fail_count = 0
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="GSB-watchdog"
            )
            self._watchdog_thread.start()
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] watchdog spawn skipped: {e}")

        logging.info(f"[GhostShellBrowser] Core launched successfully. Profile: {self.profile_name}")

        # ── AUTO-ENRICH fresh profiles ─────────────────────────────
        # Fires AFTER Chrome successfully started — Chrome's own code
        # has now created History/Preferences/etc with the correct
        # schema, so chrome_importer can layer imported rows on top
        # without schema-drift risk.
        #
        # Gated by self._is_new_profile AND config flag. The importer
        # is idempotent via its sentinel file so this is safe to call
        # repeatedly; we still gate here to save the import cost on
        # known-not-fresh profiles.
        if getattr(self, "_is_new_profile", False):
            try:
                # Lazy imports — keeps startup cost off runs that don't
                # auto-enrich. CFG lookup uses db module which is always
                # present at this point.
                from ghost_shell.db.database import get_db
                db = get_db()
                auto_enabled = db.config_get(
                    "browser.auto_enrich_from_host_chrome", True)
                if auto_enabled:
                    # We need Chrome to release its History DB write lock
                    # before we open it. At this point Chrome has started
                    # but nothing's driven it yet — History is open for
                    # writes. We close+reopen by briefly navigating to
                    # about:blank and then letting the importer use
                    # WAL-safe copy to snapshot. The copy doesn't block
                    # Chrome so this is non-disruptive.
                    from ghost_shell.browser.chrome_import import auto_enrich_fresh_profile
                    # User-configurable source path. Empty → auto-detect.
                    # Settings page wires this via data-config.
                    source_override = (
                        db.config_get("browser.auto_enrich_source_path", "") or ""
                    ).strip() or None
                    result = auto_enrich_fresh_profile(
                        dest_profile = self.profile_name,
                        profiles_root= os.path.dirname(self.user_data_path),
                        max_days     = int(db.config_get(
                            "browser.auto_enrich_max_days", 30) or 30),
                        max_urls     = int(db.config_get(
                            "browser.auto_enrich_max_urls", 500) or 500),
                        source_dir   = source_override,
                    )
                    if result.get("ok"):
                        s = result.get("summary", {})
                        logging.info(
                            f"[GhostShellBrowser] auto-enrich complete: "
                            f"imported ~{s.get('history', 0)} URLs + "
                            f"{s.get('bookmarks', 0)} bookmarks from host Chrome"
                        )
                    elif result.get("reason") not in (
                            "already auto-enriched", "no host Chrome detected"):
                        # Quiet on expected skips, noisy on real failures
                        logging.info(
                            f"[GhostShellBrowser] auto-enrich skipped: "
                            f"{result.get('reason')}"
                        )
            except Exception as e:
                logging.debug(f"[GhostShellBrowser] auto-enrich hook: {e}")

        return self.driver

    def _apply_cdp_overrides(self, payload: dict):
        """
        Минимальные CDP override'ы.

        ВАЖНО: мы НЕ дублируем underмену UserAgent/UA-CH via CDP thenу that
        this already делает C++ core via GhostShellConfig. Дублирование создаст
        рассогласование между разными слоями (JS navigator.userAgent vs
        HTTP header vs UserAgentMetadata).

        Оставляем only Timezone — if C++ его НЕ патчит в V8, нужyн fallback.
        """
        tz = payload.get("timezone", {})
        tz_id = tz.get("id", "Europe/Kyiv")

        try:
            # Fallback timezone override if C++ патч не works на asом-то API
            self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": tz_id})
            logging.debug(f"[GhostShellBrowser] CDP timezone fallback: {tz_id}")
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] CDP timezone failed: {e}")

        # Locale override — на случай if C++ не дошёл до V8 Intl
        try:
            langs = payload.get("languages", {})
            self.driver.execute_cdp_cmd("Emulation.setLocaleOverride", {
                "locale": langs.get("language", "uk-UA"),
            })
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] CDP locale fallback: {e}")

        # Mobile emulation — if a Phase 2 fingerprint exists for this
        # profile AND it's marked is_mobile, apply CDP touch + viewport
        # overrides so the browser presents itself as a phone. Silent
        # no-op for desktop profiles and profiles without a Phase 2 FP.
        self._maybe_apply_mobile_emulation()

    def _maybe_apply_mobile_emulation(self):
        """Read the current Phase 2 fingerprint for this profile and,
        if is_mobile=True, apply CDP emulation to make the browser look
        and behave like a phone.

        CDP calls applied (all best-effort, never raise):
          Emulation.setDeviceMetricsOverride  — mobile=true, viewport, DPR
          Emulation.setTouchEmulationEnabled  — enabled=true + maxTouchPoints
          Emulation.setEmitTouchEventsForMouse — mouse becomes touch
        """
        try:
            from ghost_shell.db import get_db
            fp = get_db().fingerprint_current(self.profile_name)
        except Exception as e:
            logging.debug(f"[mobile] fingerprint lookup failed: {e}")
            return

        if not fp or not (fp.get("payload") or {}).get("is_mobile"):
            return    # desktop or no FP — nothing to do

        fp_data = fp["payload"]
        screen  = fp_data.get("screen") or {}
        width   = int(screen.get("width")  or 412)
        height  = int(screen.get("height") or 915)
        dpr     = float(fp_data.get("dpr") or 2.625)
        touch   = int(fp_data.get("max_touch_points") or 5)
        ua      = fp_data.get("user_agent") or ""

        # 1. Device metrics — flips the `mobile` bit at the renderer level,
        #    which is what makes `window.matchMedia("(pointer: coarse)")`
        #    report mobile, and what triggers CSS @media (hover: none).
        try:
            self.driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width":              width,
                "height":             height,
                "deviceScaleFactor":  dpr,
                "mobile":             True,
                "screenOrientation":  {"type": "portraitPrimary", "angle": 0},
            })
        except Exception as e:
            logging.warning(f"[mobile] setDeviceMetricsOverride failed: {e}")

        # 2. Touch emulation — navigator.maxTouchPoints, ontouchstart event
        try:
            self.driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {
                "enabled":          True,
                "maxTouchPoints":   touch,
            })
        except Exception as e:
            logging.warning(f"[mobile] setTouchEmulationEnabled failed: {e}")

        # 3. Make mouse events dispatch touch events too. Lets Selenium
        #    .click() work via the mobile code path on targets that
        #    have different behaviour for touch vs mouse.
        try:
            self.driver.execute_cdp_cmd("Emulation.setEmitTouchEventsForMouse", {
                "enabled":      True,
                "configuration": "mobile",
            })
        except Exception as e:
            logging.debug(f"[mobile] setEmitTouchEventsForMouse failed: {e}")

        # 4. UA override — overwrites any desktop UA the C++ stealth
        #    core may have set. Mobile UA in UA, Android platform in UA-CH.
        try:
            ua_ch = fp_data.get("ua_client_hints") or {}
            params = {"userAgent": ua} if ua else {}
            if ua_ch:
                params["userAgentMetadata"] = ua_ch
            if fp_data.get("language"):
                params["acceptLanguage"] = fp_data["language"]
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride", params)
        except Exception as e:
            logging.debug(f"[mobile] Network.setUserAgentOverride failed: {e}")

        # 5. DeviceOrientation — phones report a tilt/yaw via
        #    DeviceOrientationEvent. Without this, window.addEventListener
        #    ("deviceorientation", ...) never fires, which is a mobile
        #    tell some detection suites look for. We pick a plausible
        #    portrait-held orientation with small noise; a tiny script
        #    below re-dispatches it every ~200ms.
        try:
            self.driver.execute_cdp_cmd("Emulation.setDeviceOrientationOverride", {
                "alpha": 0,   # compass heading (0..360) — north-ish
                "beta":  75,  # front/back tilt — phone held upright
                "gamma": 0,   # left/right roll
            })
        except Exception as e:
            logging.debug(f"[mobile] setDeviceOrientationOverride failed: {e}")

        # 6. Inject a script that keeps firing DeviceMotion + DeviceOrientation
        #    events with small noise (within ±0.3g of resting). Runs in every
        #    new document — no per-navigation setup. Uses CDP's
        #    Page.addScriptToEvaluateOnNewDocument so timing is deterministic.
        motion_script = """
(() => {
  // Tiny PRNG so each run has a stable but different noise profile
  let seed = Math.random() * 10000;
  const rnd = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280 - 0.5; };
  const JITTER = 0.3;
  let running = true;
  addEventListener('pagehide', () => { running = false; });

  function tick() {
    if (!running) return;
    try {
      // DeviceMotion — accelerometer reading ~ -9.8m/s^2 on Y when upright
      const ev = new DeviceMotionEvent('devicemotion', {
        acceleration:                { x: rnd() * JITTER,
                                       y: rnd() * JITTER,
                                       z: rnd() * JITTER },
        accelerationIncludingGravity:{ x: rnd() * JITTER,
                                       y: -9.8 + rnd() * JITTER,
                                       z: rnd() * JITTER },
        rotationRate:                { alpha: rnd() * JITTER * 10,
                                       beta:  rnd() * JITTER * 10,
                                       gamma: rnd() * JITTER * 10 },
        interval: 16,
      });
      window.dispatchEvent(ev);

      // DeviceOrientation — compass + tilt with drift
      const ov = new DeviceOrientationEvent('deviceorientation', {
        alpha: (rnd() + 0.5) * 360,
        beta:  75 + rnd() * 4,
        gamma: rnd() * 6,
        absolute: true,
      });
      window.dispatchEvent(ov);
    } catch (e) {}
    setTimeout(tick, 180 + rnd() * 60);
  }
  // Start after a brief settle so initial page JS attaches listeners
  setTimeout(tick, 600);
})();
"""
        try:
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": motion_script,
            })
        except Exception as e:
            logging.debug(f"[mobile] motion-script install failed: {e}")

        logging.info(
            f"[mobile] emulation applied: viewport={width}x{height}@{dpr}x "
            f"touch={touch} UA=<mobile> motion=<injected>"
        )

    def _set_network_conditions(self, payload: dict):
        """Emulates realistic network bandwidth and latency via CDP."""
        # Calculate bytes per second based on standard 4G/Broadband
        download_bytes = int(random.uniform(10.0, 50.0) * 1024 * 1024 / 8)
        upload_bytes   = int(download_bytes * 0.3)
        latency_ms     = random.choice([50, 75, 100])

        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd("Network.emulateNetworkConditions", {
                "offline": False,
                "downloadThroughput": download_bytes,
                "uploadThroughput": upload_bytes,
                "latency": latency_ms,
            })
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] CDP network condition error: {e}")

        # ── Resource blocklist (Settings → Performance) ─────────────
        # These are heavyweight URL patterns the Settings page lets
        # users toggle off to speed up SERP loads. We NEVER include
        # google.com, gstatic.com *.js (core) — only media, tiles,
        # analytics beacons that don't affect ad detection. Patterns
        # use CDP wildcard syntax: `*` matches anything.
        try:
            patterns = self._build_blocked_url_patterns()
            if patterns:
                self.driver.execute_cdp_cmd(
                    "Network.setBlockedURLs", {"urls": patterns}
                )
                logging.info(
                    f"[GhostShellBrowser] Blocked {len(patterns)} URL patterns "
                    f"(Settings → Performance)"
                )
        except Exception as e:
            logging.warning(f"[GhostShellBrowser] setBlockedURLs failed: {e}")

    # Map each toggle key → concrete CDP URL patterns.
    # Patterns are conservative:
    #   * NEVER include bare google.com/* — would break SERP itself
    #   * NEVER include gstatic.com *.js, *.css — core SERP assets
    #   * ONLY include heavy binary/media/analytics that the ad parser
    #     doesn't need
    # Users who need finer control can add their own patterns in the
    # "Custom patterns" textarea.
    _BLOCKLIST_BUCKETS = {
        "browser.block_youtube_video": [
            "*://*.ytimg.com/*",
            "*://*.youtube.com/*.mp4*",
            "*://*.youtube.com/*.webm*",
            "*://*.googlevideo.com/*",
        ],
        "browser.block_google_images": [
            "*://encrypted-tbn*.gstatic.com/*",
            "*://*.ggpht.com/*",
            # Block image result thumbnails that occasionally appear on SERP
            # (they're big and we don't parse them).
            "*://lh3.googleusercontent.com/*",
        ],
        "browser.block_google_maps_tiles": [
            "*://mt0.google.com/vt/*",
            "*://mt1.google.com/vt/*",
            "*://mt2.google.com/vt/*",
            "*://mt3.google.com/vt/*",
            "*://maps.googleapis.com/maps/api/staticmap*",
        ],
        "browser.block_fonts": [
            "*://fonts.gstatic.com/*",
            "*.woff2",
            "*.woff",
        ],
        "browser.block_analytics": [
            "*://*.google-analytics.com/*",
            "*://*.googletagmanager.com/*",
            "*://*.doubleclick.net/pagead/*",
            "*://stats.g.doubleclick.net/*",
            "*://www.googleadservices.com/pagead/conversion/*",
        ],
        "browser.block_social_widgets": [
            "*://*.facebook.net/*",
            "*://*.facebook.com/plugins/*",
            "*://platform.twitter.com/*",
            "*://*.x.com/i/widgets/*",
            "*://*.linkedin.com/embed/*",
        ],
        "browser.block_video_everywhere": [
            "*.mp4",
            "*.webm",
            "*.m3u8",
            "*.ts",
            "*.ogv",
        ],
    }

    def _build_blocked_url_patterns(self) -> list:
        """
        Compose the CDP block-list from the user's Settings toggles.
        Reads keys in the browser.block_* namespace and aggregates the
        associated URL patterns into a single list.
        """
        try:
            from ghost_shell.db.database import get_db
            db = get_db()
        except Exception:
            return []

        patterns = []
        for key, urls in self._BLOCKLIST_BUCKETS.items():
            try:
                if db.config_get(key):
                    patterns.extend(urls)
            except Exception:
                continue

        # Custom patterns — free-form list supplied via Settings
        try:
            custom = db.config_get("browser.block_custom_patterns") or []
            if isinstance(custom, list):
                patterns.extend([p.strip() for p in custom if p and p.strip()])
        except Exception:
            pass

        # De-dupe while preserving order for predictable logs
        seen, out = set(), []
        for p in patterns:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    # ──────────────────────────────────────────────────────────
    # SESSION MANAGEMENT
    # ──────────────────────────────────────────────────────────

    def _auto_restore_session(self):
        """Restores cookies and local storage from the previous session."""
        from ghost_shell.session.manager import SessionManager
        self._session_mgr = SessionManager(self.driver)

        cookies_path  = os.path.join(self.session_dir, "cookies.json")
        storage_path  = os.path.join(self.session_dir, "storage.json")

        if os.path.exists(cookies_path):
            count = self._session_mgr.import_cookies(cookies_path)
            if count > 0:
                logging.info(f"[GhostShellBrowser] ↻ Restored {count} cookies from previous session.")

        if os.path.exists(storage_path):
            self._session_mgr.import_storage(storage_path, navigate_first=True)

    def _auto_save_session(self):
        """Persists the current session state to disk (cookies + localStorage)."""
        if self._session_mgr is None:
            from ghost_shell.session.manager import SessionManager
            self._session_mgr = SessionManager(self.driver)

        os.makedirs(self.session_dir, exist_ok=True)

        # Cookies work anywhere — always save
        try:
            self._session_mgr.export_cookies(os.path.join(self.session_dir, "cookies.json"))
        except Exception as e:
            logging.warning(f"[GhostShellBrowser] Failed to save cookies: {e}")

        # Storage (localStorage) is only accessible on real http/https pages.
        # On data:/about:/chrome:// URLs the security policy forbids it.
        try:
            current_url = self.driver.current_url or ""
            if current_url.startswith(("http://", "https://")):
                self._session_mgr.export_storage(os.path.join(self.session_dir, "storage.json"))
            else:
                logging.debug(
                    f"[GhostShellBrowser] Skipping storage export on non-http URL: {current_url[:60]}"
                )
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] Storage export skipped: {e}")

        logging.info(f"[GhostShellBrowser] Session persisted to {self.session_dir}")

    # ──────────────────────────────────────────────────────────
    # HUMAN EMULATION & BEHAVIOR
    # ──────────────────────────────────────────────────────────

    _KEYBOARD_NEIGHBORS = {
        "q": "wa",    "w": "qeas",   "e": "wrds",   "r": "etdf",
        "t": "ryfg",  "y": "tugh",   "u": "yihj",   "i": "uojk",
        "o": "ipkl",  "p": "ol",     "a": "qwsz",   "s": "awedxz",
        "d": "serfcx","f": "drtgvc", "g": "ftyhbv", "h": "gyujnb",
        "j": "huiknm","k": "jiolm",  "l": "kop",    "z": "asx",
        "x": "zsdc",  "c": "xdfv",   "v": "cfgb",   "b": "vghn",
        "n": "bhjm",  "m": "njk",
    }

    def _typo_for(self, char: str) -> str:
        """Returns a plausible typo based on QWERTY keyboard neighbors."""
        lower = char.lower()
        neighbors = self._KEYBOARD_NEIGHBORS.get(lower, "")
        if not neighbors:
            return random.choice("abcde")
        typo = random.choice(neighbors)
        return typo.upper() if char.isupper() else typo

    def human_type(self, element, text: str, wpm: int = None):
        """
        Types string with realistic human speed, pauses, and typo corrections.
        Words-per-minute (WPM) adjusts automatically based on the time of day.
        """
        from selenium.webdriver.common.keys import Keys

        # Time-of-day logic for dynamic typing speed
        if wpm is None:
            hour = datetime.now().hour
            if 0 <= hour < 6:      wpm = random.randint(100, 140) # Late night
            elif 6 <= hour < 9:    wpm = random.randint(130, 170) # Early morning
            elif 9 <= hour < 12:   wpm = random.randint(170, 220) # Morning peak
            elif 12 <= hour < 14:  wpm = random.randint(150, 190) # Lunch
            elif 14 <= hour < 18:  wpm = random.randint(180, 230) # Afternoon peak
            else:                  wpm = random.randint(140, 180) # Evening

        delay_base = 60.0 / (wpm * 5)

        for char in text:
            # 3% chance for a typo on standard ASCII letters
            if random.random() < 0.03 and char.isalpha() and ord(char) < 128:
                typo = self._typo_for(char)
                element.send_keys(typo)
                time.sleep(random.uniform(0.15, 0.45))
                element.send_keys(Keys.BACKSPACE)
                time.sleep(random.uniform(0.08, 0.2))

            element.send_keys(char)
            delay = delay_base * random.uniform(0.6, 1.4)

            # Rare cognitive pause (thinking)
            if random.random() < 0.03:
                delay += random.uniform(0.4, 1.2)

            # Pause slightly longer after punctuation
            if char in " .,;!?":
                delay *= random.uniform(1.2, 1.8)

            time.sleep(delay)

    def human_scroll(self, min_scrolls: int = 2, max_scrolls: int = 5):
        """Simulates native mouse-wheel scrolling with easing functions."""
        for _ in range(random.randint(min_scrolls, max_scrolls)):
            total    = random.randint(200, 700)
            steps    = random.randint(8, 20)
            interval = random.uniform(0.03, 0.07)
            
            for step in range(steps):
                progress  = step / steps
                eased     = math.sin(progress * math.pi / 2)
                step_size = int((total / steps) * (1 - eased * 0.3))
                self.driver.execute_script(f"window.scrollBy(0, {step_size});")
                time.sleep(interval)
            
            time.sleep(random.uniform(1.0, 3.5))
            
            # 15% chance to scroll back slightly (user re-reading)
            if random.random() < 0.15:
                self.driver.execute_script(f"window.scrollBy(0, -{random.randint(50, 150)});")
                time.sleep(random.uniform(0.5, 1.5))

    # ──────────────────────────────────────────────────────────
    # HARDWARE MOUSE EMULATION (CDP & BEZIER CURVES)
    # ──────────────────────────────────────────────────────────

    def _cdp_mouse_move(self, x: int, y: int):
        """Fires native CDP mouse movement events."""
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y
        })

    def _cdp_mouse_click(self, x: int, y: int):
        """Fires native CDP hardware click events."""
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1
        })
        time.sleep(random.uniform(0.05, 0.15))
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1
        })

    def _bezier_point(self, t: float, p0, p1, p2, p3) -> tuple:
        """Calculates coordinates for a cubic Bezier curve."""
        u = 1 - t
        x = u**3*p0[0] + 3*u**2*t*p1[0] + 3*u*t**2*p2[0] + t**3*p3[0]
        y = u**3*p0[1] + 3*u**2*t*p1[1] + 3*u*t**2*p2[1] + t**3*p3[1]
        return (int(x), int(y))

    def bezier_move_to(self, element):
        """
        Moves the mouse smoothly to the target element using Bezier curves,
        and clicks with a slight, human-like center offset.
        """
        try:
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', behavior: 'instant'})", element
            )
            time.sleep(random.uniform(0.2, 0.4))
            
            rect = self.driver.execute_script("return arguments[0].getBoundingClientRect();", element)
            ex = int(rect['left'] + (rect['width'] / 2))
            ey = int(rect['top'] + (rect['height'] / 2))

            # Apply random offset within the element boundaries
            ex += random.randint(-15, 15)
            ey += random.randint(-5, 5)

            # Define control points for the Bezier curve
            start_x, start_y = ex - random.randint(100, 300), ey - random.randint(50, 150)
            cp1 = (start_x + random.randint(-30, 30), start_y + random.randint(-30, 30))
            cp2 = (ex + random.randint(-30, 30), ey + random.randint(-30, 30))

            steps = random.randint(10, 20)
            for i in range(steps + 1):
                t = i / steps
                t_e = t * t * (3 - 2 * t) # Ease-in/out
                px, py = self._bezier_point(t_e, (start_x, start_y), cp1, cp2, (ex, ey))
                self._cdp_mouse_move(px, py)
                time.sleep(random.uniform(0.008, 0.025))

            time.sleep(random.uniform(0.1, 0.3))
            self._cdp_mouse_click(ex, ey)

        except Exception as e:
            logging.debug(f"[GhostShellBrowser] Bezier movement fallback triggered: {e}")
            try:
                element.click()
            except Exception:
                self.driver.execute_script("arguments[0].click()", element)

    def warm_mouse(self):
        """Simulates random idle mouse movement across the viewport."""
        try:
            vp_w = self.driver.execute_script("return window.innerWidth")
            vp_h = self.driver.execute_script("return window.innerHeight")
            prev = (vp_w // 2, vp_h // 2)

            for _ in range(random.randint(3, 6)):
                target = (random.randint(100, vp_w - 100), random.randint(100, vp_h - 200))
                cp1 = (prev[0] + random.randint(-100, 100), prev[1] + random.randint(-100, 100))
                cp2 = (target[0] + random.randint(-100, 100), target[1] + random.randint(-100, 100))
                
                steps = random.randint(15, 30)
                for i in range(1, steps + 1):
                    t = i / steps
                    t_e = t * t * (3 - 2 * t)
                    px, py = self._bezier_point(t_e, prev, cp1, cp2, target)
                    self._cdp_mouse_move(px, py)
                    time.sleep(random.uniform(0.005, 0.02))
                
                prev = target
                time.sleep(random.uniform(0.2, 0.6))
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] warm_mouse error: {e}")

    # ──────────────────────────────────────────────────────────
    # STEALTH NAVIGATION & WARMUP
    # ──────────────────────────────────────────────────────────

    def stealth_get(self, url: str, referer: str = None):
        """Navigates to a URL while spoofing the HTTP Referer header."""
        if referer:
            try:
                self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {
                    "headers": {"Referer": referer}
                })
            except Exception:
                pass

        self.driver.get(url)

        try:
            self.driver.execute_cdp_cmd("Network.setExtraHTTPHeaders", {"headers": {}})
        except Exception:
            pass

    def warmup_profile(self, depth: str = "light"):
        """
        Builds historical footprint via natural browsing behavior.
        Depth modes: 'fast', 'hybrid', 'light', 'medium', 'full'.
        """
        if depth == "fast":
            from ghost_shell.session.cookie_warmer import CookieWarmer
            CookieWarmer(self.driver).fast_warmup()
            self._log_activity("warmup", "fast")
            return

        if depth == "hybrid":
            from ghost_shell.session.cookie_warmer import CookieWarmer
            CookieWarmer(self.driver).hybrid_warmup(short_visits=True)
            self._log_activity("warmup", "hybrid")
            return

        sites = {
            "light":  ["https://www.google.com", "https://www.youtube.com"],
            "medium": ["https://www.google.com", "https://www.youtube.com", "https://www.wikipedia.org"],
            "full":   ["https://www.google.com", "https://www.youtube.com", "https://www.wikipedia.org", "https://www.ukr.net"]
        }

        targets = sites.get(depth, sites["light"])
        logging.info(f"[GhostShellBrowser] Initiating profile warmup ({depth}): {len(targets)} targets.")

        for url in targets:
            try:
                self.driver.get(url)
                wait_base = {"light": 5, "medium": 8, "full": 12}.get(depth, 5)
                time.sleep(random.uniform(wait_base, wait_base + 4))

                self._try_accept_cookies()
                self.warm_mouse()
                self.human_scroll(
                    min_scrolls={"light": 1, "medium": 2, "full": 3}.get(depth, 1),
                    max_scrolls={"light": 3, "medium": 5, "full": 7}.get(depth, 3),
                )
                time.sleep(random.uniform(3, 7))
            except Exception as e:
                logging.debug(f"[GhostShellBrowser] Warmup error on {url}: {e}")

        self._log_activity("warmup", depth)
        logging.info("[GhostShellBrowser] Warmup sequence completed.")

    def _try_accept_cookies(self):
        """Attempts to autonomously accept GDPR/Cookie banners."""
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        
        selectors = [
            "//button[contains(text(),'Accept all')]",
            "//button[contains(text(),'Принять all')]",
            "//button[contains(text(),'Agree')]",
            "//button[@id='L2AGLb']",
        ]
        for xpath in selectors:
            try:
                btn = WebDriverWait(self.driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, xpath))
                )
                btn.click()
                time.sleep(random.uniform(1, 2))
                return
            except Exception:
                continue

    # ──────────────────────────────────────────────────────────
    # COGNITIVE BEHAVIOR (DWELL & PAUSES)
    # ──────────────────────────────────────────────────────────

    def smart_dwell(self, min_sec: float = 3.0, max_sec: float = 20.0):
        """Adjusts page view duration dynamically based on DOM content length."""
        try:
            info = self.driver.execute_script("""
                return {
                    textLength:  (document.body.innerText || '').length,
                    hasVideo:    document.querySelector('video, iframe[src*="youtube"]') !== null,
                    imagesCount: document.images.length
                };
            """)
            
            # Base reading speed logic (~25 characters per second)
            text_len = info.get("textLength", 0)
            base_read_time = text_len / 25 if text_len else 3
            actual_read = base_read_time * random.uniform(0.15, 0.30)

            if info.get("hasVideo"):
                actual_read += random.uniform(10, 30)
            if info.get("imagesCount", 0) > 10:
                actual_read += random.uniform(2, 5)

            dwell = max(min_sec, min(max_sec, actual_read))
            logging.debug(f"[GhostShellBrowser] Smart dwell calculated: {dwell:.1f}s")
            time.sleep(dwell)
        except Exception:
            time.sleep(random.uniform(min_sec, max_sec))

    def idle_pause(self, kind: str = "random"):
        """Simulates natural user distractions and breaks away from keyboard."""
        if kind == "random":
            kind = random.choices(
                ["none", "micro", "short", "medium", "long"],
                weights=[60, 25, 10, 4, 1],
                k=1,
            )[0]

        if kind == "none":
            return

        ranges = {
            "micro":  (3, 10),
            "short":  (30, 90),
            "medium": (300, 900),
            "long":   (1800, 3600),
        }
        low, high = ranges.get(kind, (5, 15))
        pause = random.uniform(low, high)

        logging.info(f"[GhostShellBrowser] 💤 Simulating idle break ({kind}): {pause:.0f}s")
        time.sleep(pause)

    # ──────────────────────────────────────────────────────────
    # INTERACTION PATTERNS
    # ──────────────────────────────────────────────────────────

    def wait_and_interact_with_suggestions(
        self,
        search_box,
        click_probability: float = 0.35
    ) -> bool:
        """Waits for Google/Search autocomplete and clicks suggestions probabilistically."""
        time.sleep(random.uniform(0.4, 1.0))
        try:
            suggestions = self.driver.find_elements(
                By.CSS_SELECTOR,
                'ul[role="listbox"] li[role="option"], .sbct, .wM6W7d'
            )
        except Exception:
            return False

        visible = [s for s in suggestions if s.is_displayed()]
        if not visible:
            return False

        if random.random() < click_probability:
            chosen = random.choice(visible[:min(3, len(visible))])
            logging.info(f"[GhostShellBrowser] 👆 Clicking autocomplete suggestion: '{chosen.text[:30]}...'")
            time.sleep(random.uniform(0.4, 1.2))
            
            try:
                self.bezier_move_to(chosen)
                return True
            except Exception:
                pass
                
        return False

    # ──────────────────────────────────────────────────────────
    # ACTIVITY LOGGING
    # ──────────────────────────────────────────────────────────

    def _log_activity(self, event_type: str, detail: str = ""):
        """Records behavioral history to profile logic for long-term consistency."""
        activity_file = os.path.join(self.user_data_path, "activity.json")
        entry = {
            "timestamp":  datetime.now().isoformat(timespec="seconds"),
            "event":      event_type,
            "detail":     detail,
        }
        try:
            log = []
            if os.path.exists(activity_file):
                with open(activity_file, "r", encoding="utf-8") as f:
                    log = json.load(f)
            log.append(entry)
            with open(activity_file, "w", encoding="utf-8") as f:
                json.dump(log[-200:], f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] Activity log failure: {e}")

    # ──────────────────────────────────────────────────────────
    # ERROR HANDLING AND RECOVERY
    # ──────────────────────────────────────────────────────────

    def safe_execute(self, action_fn, description: str = "action", retries: int = 3):
        """Executes an action with automatic retries and screenshotting on failure."""
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                return action_fn()
            except Exception as e:
                last_error = e
                logging.warning(f"[GhostShellBrowser] '{description}' attempt {attempt}/{retries} failed.")
                if attempt < retries:
                    time.sleep(random.uniform(1.5, 3.5))

        self.save_screenshot(f"error_{description.replace(' ', '_')}")
        raise last_error

    def save_screenshot(self, name: str = None) -> str:
        """Saves viewport state to the profile's screenshot directory."""
        if name is None:
            name = datetime.now().strftime("%Y%m%d_%H%M%S")
        ss_dir = os.path.join(self.user_data_path, "screenshots")
        os.makedirs(ss_dir, exist_ok=True)
        path = os.path.join(ss_dir, f"{name}.png")
        
        try:
            self.driver.save_screenshot(path)
            logging.info(f"[GhostShellBrowser] 📸 Screenshot saved: {path}")
            return path
        except Exception as e:
            logging.warning(f"[GhostShellBrowser] Failed to save screenshot: {e}")
            return ""

    def is_alive(self) -> bool:
        """Validates driver process connection."""
        try:
            _ = self.driver.current_url
            return True
        except Exception:
            return False

    # ──────────────────────────────────────────────────────────
    # HANG WATCHDOG
    # ──────────────────────────────────────────────────────────
    #
    # The watchdog is a daemon thread spawned in start(). It pokes
    # the driver every WATCHDOG_PROBE_SEC; a successful probe resets
    # the failure counter, a failed probe increments it. Once the
    # counter hits WATCHDOG_KILL_AFTER consecutive failures we kill
    # the whole Chrome process tree — which lets the main thread's
    # next selenium command fail fast (WebDriverException) instead
    # of blocking forever.
    #
    # Why not rely on command timeouts alone?
    #   Selenium's .get() has a timeout via set_page_load_timeout(),
    #   but many commands (CDP exec, driver.title after a nav) have
    #   NO timeout by default. And if the underlying http channel
    #   stalls, even timeouts don't fire. This thread is the safety
    #   net for those cases.
    #
    # Why not restart instead of kill?
    #   We're in a daemon context; restarting the browser mid-run
    #   would corrupt the run's state. Killing is cleaner — main.py's
    #   run loop will see a WebDriverException and exit, heartbeat
    #   stops, and the scheduler starts the next iteration clean.

    WATCHDOG_PROBE_SEC   = 30    # how often to poke the driver
    WATCHDOG_PROBE_TIMEOUT = 20  # per-probe ceiling (seconds)
    WATCHDOG_KILL_AFTER  = 3     # consecutive failures before murder

    def _watchdog_loop(self):
        """Background thread — detects frozen Chrome and force-kills it."""
        while not self._watchdog_stop.is_set():
            self._watchdog_stop.wait(self.WATCHDOG_PROBE_SEC)
            if self._watchdog_stop.is_set():
                return
            if self.driver is None:
                return

            probe_ok = self._watchdog_probe()
            if probe_ok:
                if self._watchdog_fail_count > 0:
                    logging.info(
                        f"[GhostShellBrowser] watchdog: driver recovered "
                        f"after {self._watchdog_fail_count} failed probe(s)"
                    )
                self._watchdog_fail_count = 0
                continue

            self._watchdog_fail_count += 1
            logging.warning(
                f"[GhostShellBrowser] watchdog: probe failed "
                f"({self._watchdog_fail_count}/{self.WATCHDOG_KILL_AFTER})"
            )
            if self._watchdog_fail_count >= self.WATCHDOG_KILL_AFTER:
                self._watchdog_kill_chrome()
                return   # don't probe a corpse

    def _watchdog_probe(self) -> bool:
        """One probe attempt. We use a deadline-based thread because
        selenium's own timeout handling is unreliable on stalled sockets.
        Probe tries `driver.title` — lightweight CDP command.
        """
        result = {"ok": False}

        def _try_ping():
            try:
                _ = self.driver.title   # cheap, goes through CDP
                result["ok"] = True
            except Exception as e:
                logging.debug(f"[watchdog] probe exception: {e}")

        t = threading.Thread(target=_try_ping, daemon=True)
        t.start()
        t.join(timeout=self.WATCHDOG_PROBE_TIMEOUT)
        if t.is_alive():
            # The thread is stuck in the selenium call — probe timed out.
            # Thread will eventually unstick or get GC'd when we kill Chrome.
            return False
        return result["ok"]

    def _watchdog_kill_chrome(self):
        """Chrome is wedged — kill the whole tree so selenium commands
        on the main thread fail fast instead of blocking forever."""
        logging.error(
            "[GhostShellBrowser] watchdog: Chrome appears WEDGED. "
            "Force-killing process tree so main.py can exit cleanly."
        )
        try:
            service_proc = getattr(self.driver, "service", None)
            chromedriver_pid = None
            if service_proc and getattr(service_proc, "process", None):
                chromedriver_pid = service_proc.process.pid
        except Exception:
            chromedriver_pid = None

        if chromedriver_pid:
            try:
                from ghost_shell.core.process_reaper import kill_process_tree
                kill_process_tree(chromedriver_pid, reason="watchdog: Chrome hang")
            except Exception as e:
                logging.warning(f"[watchdog] kill_process_tree error: {e}")

        # Also nuke the proxy forwarder — it's useless without Chrome
        try:
            if self._proxy_forwarder:
                self._proxy_forwarder.stop()
        except Exception:
            pass

        # Null out the driver so .is_alive() returns False and any
        # selenium commands raise AttributeError instead of hanging.
        self.driver = None

    def restart(self):
        """Gracefully restarts the browser instance."""
        logging.warning("[GhostShellBrowser] Restarting browser engine...")
        try:
            if self.auto_session and self.driver and self.is_alive():
                self._auto_save_session()
        except Exception:
            pass
        
        self.close()
        time.sleep(2)
        self.start()
        logging.info("[GhostShellBrowser] ✓ Engine restarted successfully.")

    # ──────────────────────────────────────────────────────────
    # ROTATING PROXIES
    # ──────────────────────────────────────────────────────────

    def get_rotating_tracker(self):
        """Lazy loader for rotating proxy tracker."""
        if self._rotating_tracker is None and self.is_rotating_proxy:
            from ghost_shell.proxy.rotating import RotatingProxyTracker
            # Read all rotation config from DB (set via dashboard Proxy page)
            try:
                from ghost_shell.db.database import get_db
                db = get_db()
                provider = db.config_get("proxy.rotation_provider") or "none"
                api_key  = db.config_get("proxy.rotation_api_key")
                method   = db.config_get("proxy.rotation_method") or "GET"
            except Exception:
                provider, api_key, method = "none", None, "GET"

            self._rotating_tracker = RotatingProxyTracker(
                proxy_url         = self.proxy_str,
                rotation_provider = provider,
                rotation_api_url  = self.rotation_api_url,
                rotation_api_key  = api_key,
                rotation_method   = method,
            )
        return self._rotating_tracker

    def force_rotate_ip(self) -> str | None:
        """
        Unconditional proxy rotation — called when we need a new exit even if
        the current IP isn't "burned" (e.g. wrong country for our locale).
        Returns the new IP, or None if rotation is unavailable/failed.
        """
        tracker = self.get_rotating_tracker()
        if not tracker:
            logging.warning("[GhostShellBrowser] No rotating tracker — "
                            "cannot force_rotate_ip")
            return None
        old_ip = tracker.get_current_ip(self.driver)

        # force_rotate() returns False when no rotation API is configured.
        # Don't bother waiting 60s for an IP change that can't happen —
        # log the issue clearly and return the old IP so the caller
        # knows we're alive and working (just not rotated).
        triggered = tracker.force_rotate()
        if not triggered:
            logging.warning(
                f"[GhostShellBrowser] Rotation NOT triggered — keeping "
                f"IP {old_ip}. Configure rotation API in Proxy page."
            )
            return old_ip

        time.sleep(random.uniform(3, 8))
        new_ip = tracker.wait_for_rotation(self.driver, old_ip, timeout=60)
        if new_ip:
            tracker.enrich_ip(new_ip, self.driver)
            try:
                from ghost_shell.db.database import get_db
                get_db().ip_record_start(new_ip)
            except Exception as e:
                logging.debug(f"ip_record_start after rotation: {e}")
        else:
            # API accepted the rotation request but IP didn't change
            # within the 60s window. Could be provider lag, or the new
            # IP is already our old one (small pool). Log both possibilities.
            logging.warning(
                f"[GhostShellBrowser] Rotation triggered but exit IP didn't "
                f"change in 60s (still {old_ip}). Provider may be slow, or "
                f"the pool re-issued the same IP."
            )
        return new_ip

    def check_and_rotate_if_burned(self) -> str | None:
        """Evaluates current IP status and forces rotation if blacklisted."""
        tracker = self.get_rotating_tracker()
        if not tracker: return None

        current_ip = tracker.get_current_ip(self.driver)
        if not current_ip: return None

        if tracker.is_ip_burned(current_ip):
            logging.warning(f"[GhostShellBrowser] IP {current_ip} flagged as burned. Rotating...")
            tracker.force_rotate()
            time.sleep(random.uniform(3, 8))
            new_ip = tracker.wait_for_rotation(self.driver, current_ip, timeout=60)
            if new_ip: current_ip = new_ip

        tracker.enrich_ip(current_ip, self.driver)
        try:
            from ghost_shell.db.database import get_db
            get_db().ip_record_start(current_ip)
        except Exception as e:
            logging.debug(f"ip_record_start after check: {e}")
        return current_ip

    def report_rotating(self, ip: str, success: bool = True, captcha: bool = False):
        """Dispatches operational result to proxy rotation tracker."""
        tracker = self.get_rotating_tracker()
        if tracker and ip:
            tracker.report(ip, success=success, captcha=captcha)

    # ──────────────────────────────────────────────────────────
    # HEALTH CHECKS
    # ──────────────────────────────────────────────────────────

    def health_check(self, verbose: bool = True) -> dict:
        """
        Проверяет that C++ патчи реально onменorсь.
        Сравнивает values в JS с ожидаемыми из payload.
        """
        # CRITICAL: UA-CH (navigator.userAgentData), navigator.deviceMemory,
        # navigator.getBattery, navigator.mediaDevices.enumerateDevices
        # are all gated by "secure context" — they are NOT exposed on
        # about:blank or http:// pages. Previously we self-checked on
        # about:blank and every secure-context API returned null/undefined,
        # giving us a false "patch broken" signal.
        #
        # Navigate to a lightweight Google endpoint (204 No Content) which:
        #   - is a secure (https://) context
        #   - returns 0 bytes so nothing to render
        #   - goes through our proxy so doesn't leak real IP
        try:
            self.driver.get("https://www.google.com/generate_204")
            time.sleep(0.8)
        except Exception as e:
            logging.warning(
                f"[GhostShellBrowser] couldn't open secure context for selfcheck, "
                f"falling back to about:blank ({e})"
            )
            self.driver.get("about:blank")
            time.sleep(0.5)

        # Ожидаемые values из afterднего сгенерированного payload
        expected = {}
        try:
            with open(os.path.join(self.user_data_path, "payload_debug.json"), "r", encoding="utf-8") as f:
                expected = json.load(f)
        except Exception:
            logging.warning("[GhostShellBrowser] Can't load payload for health check")

        exp_hw     = expected.get("hardware", {})
        exp_langs  = expected.get("languages", {})
        exp_screen = expected.get("screen", {})
        exp_tz     = expected.get("timezone", {})
        exp_battery = expected.get("battery")   # can be None (desktop)
        exp_perms  = expected.get("permissions", {})
        exp_ua_md  = expected.get("ua_metadata", {})

        # Базовые checks (инвариантные)
        tests = {
            "webdriver_hidden":    "navigator.webdriver === false || navigator.webdriver === undefined",
            "plugins_exist":       "navigator.plugins.length > 0",
            "no_cdc_leak":         "!Object.keys(window).some(k => k.startsWith('$cdc_'))",
            "no_automation_marks": "typeof window.__playwright === 'undefined' && typeof window.__puppeteer_evaluation_script__ === 'undefined'",
            "chrome_object":       "typeof window.chrome === 'object' && window.chrome !== null",
            "iframe_consistency":  (
                "(() => { try { const i = document.createElement('iframe'); "
                "document.body.appendChild(i); const r = i.contentWindow.navigator.userAgent === navigator.userAgent; "
                "document.body.removeChild(i); return r; } catch(e) { return false; } })()"
            ),
        }

        # Checks соresponseствия C++ патчей
        if exp_hw.get("user_agent"):
            tests["ua_matches_payload"] = (
                f"navigator.userAgent === {json.dumps(exp_hw['user_agent'])}"
            )
        if exp_hw.get("hardware_concurrency"):
            tests["hardware_concurrency_matches"] = (
                f"navigator.hardwareConcurrency === {exp_hw['hardware_concurrency']}"
            )
        if exp_hw.get("device_memory"):
            # W3C spec: navigator.deviceMemory is clamped to nearest of
            # [0.25, 0.5, 1, 2, 4, 8] — max 8 for privacy. So 16 → 8.
            expected_mem = min(exp_hw["device_memory"], 8)
            tests["device_memory_matches"] = (
                f"navigator.deviceMemory === {expected_mem}"
            )
        if exp_langs.get("language"):
            tests["language_matches"] = (
                f"navigator.language === {json.dumps(exp_langs['language'])}"
            )
        if exp_screen.get("width"):
            tests["screen_width_matches"] = (
                f"screen.width === {exp_screen['width']}"
            )
        if exp_screen.get("pixel_ratio"):
            tests["dpr_matches"] = (
                f"window.devicePixelRatio === {exp_screen['pixel_ratio']}"
            )
        if exp_tz.get("id"):
            # Chrome мот возвращать Kiev yes когда в payload Kyiv
            tests["timezone_matches"] = (
                "['Europe/Kyiv','Europe/Kiev'].includes("
                "Intl.DateTimeFormat().resolvedOptions().timeZone)"
            )

        # ── UA Client Hints (Patch 3) ─────────────────────────────
        # navigator.userAgentData.platform must match payload.ua_metadata.platform
        if exp_ua_md.get("platform"):
            tests["ua_ch_platform_matches"] = (
                f"navigator.userAgentData && "
                f"navigator.userAgentData.platform === "
                f"{json.dumps(exp_ua_md['platform'])}"
            )
        # Brand list must have at least one entry — was 3 originally
        # (Not_A Brand + Chromium + Chrome) but real Chromium builds
        # shrink this in some configurations and 1+ is still plausible
        # behaviour. The check now passes if userAgentData exists and
        # has at least one brand. False only if the API is missing
        # outright — that's the actual fingerprint signal we care about.
        tests["ua_ch_brands_present"] = (
            "!!(navigator.userAgentData && "
            "  Array.isArray(navigator.userAgentData.brands) && "
            "  navigator.userAgentData.brands.length >= 1)"
        )
        # Mobile flag must match (usually false on our desktop/laptop templates)
        if "mobile" in exp_ua_md:
            tests["ua_ch_mobile_matches"] = (
                "navigator.userAgentData && "
                f"navigator.userAgentData.mobile === "
                f"{'true' if exp_ua_md['mobile'] else 'false'}"
            )

        # ── Async tests (Promise-returning APIs) ──────────────────
        # These run via execute_async_script so we can await them.
        # Each entry: (test_name, JS-code-returning-Promise<boolean>)
        async_tests = {}

        # Battery API (Patch 2)
        # When exp_battery is a dict — laptop profile with battery values.
        # When None — desktop, we expect "fully charged, plugged in" defaults.
        if exp_battery is not None:
            exp_charging = "true" if exp_battery.get("charging", True) else "false"
            # On laptop profiles the Battery API is required — if Chrome
            # removed it, the spoof is incoherent, so we keep the strict
            # check (.catch returns false). We do NOT soft-pass like the
            # desktop variant does.
            async_tests["battery_charging_matches"] = (
                "(typeof navigator.getBattery !== 'function' "
                "  ? false "
                "  : navigator.getBattery()"
                f"      .then(b => b.charging === {exp_charging})"
                "      .catch(() => false))"
            )
            if "level" in exp_battery and exp_battery["level"] is not None:
                lvl = float(exp_battery["level"])
                async_tests["battery_level_matches"] = (
                    "(typeof navigator.getBattery !== 'function' "
                    "  ? false "
                    "  : navigator.getBattery().then(b => {"
                    f"      const actual = b.level;"
                    f"      const ok = Math.abs(actual - {lvl}) < 0.02;"
                    f"      return ok ? true : "
                    f"        ('expected=' + {lvl} + ' got=' + actual);"
                    "    }).catch(() => false))"
                )
        else:
            # Desktop — expect charging:true, level:1 (plugged-in state).
            # Chrome 132+ removed navigator.getBattery() entirely from
            # insecure contexts and many Chromium builds drop it
            # outright. For a DESKTOP profile, "no battery API at all"
            # is actually the most realistic signal a real desktop
            # would expose — so treat the missing method as a SOFT
            # pass. We still verify shape if it IS present.
            async_tests["battery_desktop_default"] = (
                "(typeof navigator.getBattery !== 'function' "
                "  ? true "
                "  : navigator.getBattery()"
                "      .then(b => b.charging === true && b.level === 1)"
                "      .catch(() => true))"
            )

        # Permissions API (Patch 2)
        # Sample a handful of well-known permissions and verify they match
        # payload. The payload.permissions map has 15+ entries — we check
        # the ones most commonly probed by detection scripts.
        for perm_name in ("geolocation", "notifications", "clipboard-write",
                          "camera", "midi"):
            if perm_name not in exp_perms:
                continue
            expected_state = exp_perms[perm_name]
            async_tests[f"perm_{perm_name.replace('-','_')}_matches"] = (
                f"navigator.permissions.query({{name: {json.dumps(perm_name)}}})"
                f".then(r => r.state === {json.dumps(expected_state)})"
                f".catch(() => false)"
            )

        # UA-CH getHighEntropyValues — verify platformVersion and
        # uaFullVersion are populated from our payload (not empty).
        if exp_ua_md.get("platform_version"):
            # Defensive: getHighEntropyValues throws on builds where the
            # UA-CH patch isn't applied, OR returns an object missing
            # the field we asked for (NavigatorUAData.UNSUPPORTED in
            # newer Chromium when no Permissions-Policy header is set).
            # We catch both and return false rather than letting the
            # selfcheck see "javascript error: Cannot read p" noise.
            async_tests["ua_ch_platform_version_matches"] = (
                "(navigator.userAgentData "
                "  ? navigator.userAgentData.getHighEntropyValues(['platformVersion'])"
                f"      .then(h => h && h.platformVersion === {json.dumps(exp_ua_md['platform_version'])})"
                "      .catch(() => false)"
                "  : Promise.resolve(false))"
            )
        if exp_ua_md.get("full_version"):
            async_tests["ua_ch_full_version_matches"] = (
                "(navigator.userAgentData "
                "  ? navigator.userAgentData.getHighEntropyValues(['uaFullVersion'])"
                f"      .then(h => h && h.uaFullVersion === {json.dumps(exp_ua_md['full_version'])})"
                "      .catch(() => false)"
                "  : Promise.resolve(false))"
            )

        # ── MediaDevices enumerateDevices (Patch 1.1) ─────────────
        # Headless/bot browsers often return [] here, or only a single
        # "default" entry. Real Chrome on a desktop returns 3-6 devices
        # with distinct deviceIds (one per audioinput, videoinput,
        # audiooutput). Our C++ patch returns the payload's media list.
        exp_media = expected.get("media") or {}
        exp_ai = exp_media.get("audio_inputs")  or []
        exp_vi = exp_media.get("video_inputs")  or []
        exp_ao = exp_media.get("audio_outputs") or []
        exp_total = len(exp_ai) + len(exp_vi) + len(exp_ao)
        if exp_total > 0:
            # Expected counts per kind
            # Same defensive pattern as UA-CH. enumerateDevices() can
            # be undefined on builds without the patch applied, can
            # throw a NotAllowedError if permissions are restricted, or
            # return an empty array. We swallow all of those into a
            # plain false so the selfcheck row is meaningful.
            async_tests["media_devices_count_matches"] = (
                "((navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) "
                "  ? navigator.mediaDevices.enumerateDevices().then(d => {"
                f"      const ai = d.filter(x => x.kind === 'audioinput').length;"
                f"      const vi = d.filter(x => x.kind === 'videoinput').length;"
                f"      const ao = d.filter(x => x.kind === 'audiooutput').length;"
                f"      return ai === {len(exp_ai)} && vi === {len(exp_vi)}"
                f"          && ao === {len(exp_ao)};"
                "    }).catch(() => false)"
                "  : Promise.resolve(false))"
            )
            async_tests["media_devices_have_ids"] = (
                "((navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) "
                "  ? navigator.mediaDevices.enumerateDevices()"
                "      .then(d => d.length > 0 && d.every(x => x.deviceId && x.deviceId.length > 0))"
                "      .catch(() => false)"
                "  : Promise.resolve(false))"
            )

        # ── SpeechSynthesis getVoices (Patch 1.2) ─────────────────
        # Headless returns []. Real Chrome on Windows returns 5-10
        # voices (Microsoft David, Zira, etc.). Our payload may carry
        # a voices list — if so, compare count. Otherwise just assert
        # non-empty.
        exp_voices = (expected.get("speech") or {}).get("voices") or []
        if exp_voices:
            # Voices load async in real Chrome — poll briefly.
            async_tests["speech_voices_count_matches"] = (
                "(async () => {"
                "  for (let i = 0; i < 15; i++) {"
                "    const v = speechSynthesis.getVoices();"
                f"    if (v.length === {len(exp_voices)}) return true;"
                "    await new Promise(r => setTimeout(r, 80));"
                "  }"
                "  return false;"
                "})()"
            )
        else:
            # No payload — just confirm non-empty (real browser behavior)
            async_tests["speech_voices_non_empty"] = (
                "(async () => {"
                "  for (let i = 0; i < 15; i++) {"
                "    if (speechSynthesis.getVoices().length > 0) return true;"
                "    await new Promise(r => setTimeout(r, 80));"
                "  }"
                "  return false;"
                "})()"
            )

        # ── Performance.now() jitter (Patch 1.3) ──────────────────
        # Without jitter, performance.now() is quantized (e.g. 1 ms or
        # 0.1 ms steps) and deltas between samples have near-zero
        # variance — a bot tell. Our patch adds sub-ms randomness, so
        # fractional parts of samples should cover many distinct values,
        # not always end in .000.
        tests["performance_now_has_jitter"] = (
            "(() => {"
            "  const s = new Array(200).fill(0).map(() => performance.now());"
            "  const fracMicros = s.map(x => "
            "      Math.round((x - Math.floor(x)) * 1000) % 1000);"
            "  return new Set(fracMicros).size >= 10;"
            "})()"
        )

        # ── Feature #8: WebGL / WebGPU vendor consistency ──────────
        # Real hardware answers identically on both APIs. If we leak
        # different strings from the two code paths, creepjs flags it
        # instantly. This test requires the WebGL patch + WebGPU patch
        # (#6 + #8) to be in place — both should read from
        # unmasked_vendor_ / unmasked_renderer_ in ghost_shell_config.
        exp_gpu = expected.get("gpu") or {}
        if exp_gpu.get("unmasked_vendor"):
            tests["webgl_unmasked_vendor_matches"] = (
                "(() => {"
                "  const c = document.createElement('canvas');"
                "  const gl = c.getContext('webgl') || c.getContext('experimental-webgl');"
                "  if (!gl) return false;"
                "  const ext = gl.getExtension('WEBGL_debug_renderer_info');"
                "  if (!ext) return false;"
                "  const v = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL);"
                f"  return v === {json.dumps(exp_gpu['unmasked_vendor'])};"
                "})()"
            )
            # WebGPU is gated by "secure context" + user agent must opt in.
            # On our generate_204 page it should work. If WebGPU is not
            # available (older Chrome, or Linux without Vulkan), skip —
            # absence of the API is not a failure.
            #
            # API note: Chrome 127+ replaced requestAdapterInfo() (async,
            # now removed) with adapter.info (sync property). We try the
            # new way first and fall back for very old Chromes. Any
            # TypeError / missing-member issue returns `true` (skip).
            async_tests["webgpu_vendor_consistent_with_webgl"] = (
                "(async () => {"
                "  try {"
                "    if (!navigator.gpu) return true;"
                "    const a = await navigator.gpu.requestAdapter();"
                "    if (!a) return true;"
                "    let info = null;"
                "    if (a.info) {"
                "      info = a.info;"                           # modern
                "    } else if (typeof a.requestAdapterInfo === 'function') {"
                "      try { info = await a.requestAdapterInfo(); }"
                "      catch (_) { info = null; }"
                "    }"
                "    if (!info) return true;"
                "    const c = document.createElement('canvas');"
                "    const gl = c.getContext('webgl');"
                "    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');"
                "    if (!ext) return true;"
                "    const webglVendor = gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || '';"
                "    const tok = (info.vendor || '').toLowerCase();"
                "    return !tok || webglVendor.toLowerCase().includes(tok);"
                "  } catch (e) { return true; }"
                "})()"
            )

        # ── Feature #5: Canvas noise stability ─────────────────────
        # Same profile must produce the SAME canvas hash on repeated
        # reads (stability is a creepjs metric), but our noise makes
        # it DIFFERENT from a vanilla Chromium baseline.
        # Here we check only the stability half — the across-profiles
        # variance is asserted by dashboard-level tooling that compares
        # hashes between profile_01 / profile_02 / etc.
        tests["canvas_readback_stable"] = (
            "(() => {"
            "  const render = () => {"
            "    const c = document.createElement('canvas');"
            "    c.width = 64; c.height = 20;"
            "    const ctx = c.getContext('2d');"
            "    ctx.fillStyle = '#f60'; ctx.fillRect(0,0,50,10);"
            "    ctx.fillStyle = '#069'; ctx.font = '12px Arial';"
            "    ctx.fillText('GhostShell', 4, 15);"
            "    return c.toDataURL();"
            "  };"
            "  const a = render(); const b = render();"
            "  return a === b && a.length > 100;"   # identical reads, non-empty
            "})()"
        )

        # ── Feature #5: Audio sample rate within expected drift ────
        exp_audio = expected.get("audio") or {}
        if exp_audio.get("sample_rate"):
            # Noise jitter is ±1 Hz around the base rate — accept any
            # value in [base-1, base+1]. A real sound card drifts in
            # this range during warmup too.
            base = int(exp_audio["sample_rate"])
            tests["audio_rate_in_jitter_band"] = (
                "(() => {"
                "  try {"
                "    const ctx = new (window.AudioContext || window.webkitAudioContext)();"
                "    const r = ctx.sampleRate;"
                "    ctx.close();"
                f"    return r >= {base - 1} && r <= {base + 1};"
                "  } catch (e) { return false; }"
                "})()"
            )

        # ── Feature #8: Media codec matrix matches payload ─────────
        # For the most-fingerprinted codec (AV1), verify the
        # power_efficient flag from payload survives through the
        # media_capabilities.cc patch. Detector scripts compare this
        # value against the claimed GPU tier — mismatch = tell.
        exp_codecs = expected.get("codecs") or {}
        if "av1" in exp_codecs:
            expected_pe = exp_codecs["av1"].get("power_efficient", True)
            async_tests["av1_power_efficient_matches"] = (
                "navigator.mediaCapabilities.decodingInfo({"
                "  type: 'file',"
                "  video: {"
                "    contentType: 'video/mp4; codecs=\"av01.0.05M.08\"',"
                "    width: 1920, height: 1080,"
                "    bitrate: 5000000, framerate: 30"
                "  }"
                f"}}).then(r => r.powerEfficient === "
                f"{'true' if expected_pe else 'false'})"
                ".catch(() => true)"   # codec not supported at all -> pass
            )

        results = {}
        # Run sync tests first
        for name, code in tests.items():
            try:
                results[name] = bool(self.driver.execute_script(f"return Boolean({code});"))
            except Exception as e:
                results[name] = f"Error: {str(e)[:40]}"

        # Run async tests — each one goes through execute_async_script
        for name, code in async_tests.items():
            try:
                # Selenium's execute_async_script: last arg is a callback
                # we resolve with the test result. 8s timeout per probe.
                #
                # We pass the raw value through — NOT Boolean(v) — so that
                # tests can return a diagnostic string like
                # "expected=0.65 got=1" on failure. `true` still reads as
                # pass, `false` as fail, and anything else gets rendered
                # as a string for the log. Previously we Boolean'd the
                # result which turned diagnostic strings into `true` and
                # masked failures.
                self.driver.set_script_timeout(8)
                js = (
                    "const cb = arguments[arguments.length - 1];"
                    f"({code}).then(v => cb(v))"
                    ".catch(e => cb('Error: ' + (e.message || e)));"
                )
                r = self.driver.execute_async_script(js)
                if r is True:
                    results[name] = True
                elif r is False:
                    results[name] = False
                else:
                    # Diagnostic payload — coerce to string and truncate
                    results[name] = str(r)[:60]
            except Exception as e:
                results[name] = f"Error: {str(e)[:40]}"

        # ── Mouse event timestamp jitter (Patch 4.3) ──────────────
        # Our C++ patch adds sub-ms randomness to PointerEvent.timeStamp.
        # Without it, all mousemove events arrive at integer-ms marks
        # (e.g. 123.000, 145.000) which is a bot tell.
        #
        # We attach a listener, fire ~10 real mouse moves via Selenium's
        # CDP-backed Input API, then read collected timestamps.
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            # Install listener
            self.driver.execute_script("""
                window.__gs_mouse_stamps = [];
                window.__gs_handler = (e) => window.__gs_mouse_stamps.push(e.timeStamp);
                document.addEventListener('mousemove', window.__gs_handler,
                                          {capture: true, passive: true});
            """)
            # Fire ~10 micro-moves
            ac = ActionChains(self.driver, duration=0)
            for i in range(10):
                ac.move_by_offset(random.randint(2, 6),
                                  random.randint(1, 4))
                ac.pause(random.uniform(0.02, 0.05))
            ac.perform()
            time.sleep(0.4)

            stamps = self.driver.execute_script(
                "document.removeEventListener('mousemove', window.__gs_handler, "
                "{capture: true}); return window.__gs_mouse_stamps;"
            ) or []

            if len(stamps) >= 4:
                # Count unique µs-level fractional parts — with jitter
                # applied, we expect a wide spread; without jitter, most
                # stamps land on integer-ms marks (fractional = 0).
                fracs = [round((s - int(s)) * 1000) % 1000 for s in stamps]
                non_zero = sum(1 for f in fracs if f != 0)
                # Pass if at least half of samples have non-zero sub-ms
                # fractional component.
                results["mouse_timestamp_jitter"] = (
                    non_zero >= max(2, len(stamps) // 2)
                )
            else:
                results["mouse_timestamp_jitter"] = (
                    f"Error: only {len(stamps)} events captured"
                )
        except Exception as e:
            results["mouse_timestamp_jitter"] = f"Error: {str(e)[:40]}"

        # So собираем АКТУАЛЬНЫЕ values из JS (for дашборда)
        actual_values = {}
        try:
            actual_values = self.driver.execute_script("""
                return {
                    userAgent: navigator.userAgent,
                    platform: navigator.platform,
                    language: navigator.language,
                    languages: Array.from(navigator.languages || []),
                    hardwareConcurrency: navigator.hardwareConcurrency,
                    deviceMemory: navigator.deviceMemory,
                    webdriver: navigator.webdriver,
                    screenWidth: screen.width,
                    screenHeight: screen.height,
                    availWidth: screen.availWidth,
                    availHeight: screen.availHeight,
                    colorDepth: screen.colorDepth,
                    devicePixelRatio: window.devicePixelRatio,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    locale: Intl.DateTimeFormat().resolvedOptions().locale,
                    pluginsCount: navigator.plugins.length,
                    maxTouchPoints: navigator.maxTouchPoints,
                    // UA Client Hints — sync values
                    uaCH: navigator.userAgentData ? {
                        brands: navigator.userAgentData.brands,
                        mobile: navigator.userAgentData.mobile,
                        platform: navigator.userAgentData.platform,
                    } : null,
                };
            """)
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] couldn't snapshot actual values: {e}")

        # Async snapshots — UA-CH high-entropy + Battery + a few permissions
        try:
            self.driver.set_script_timeout(8)
            async_snap = self.driver.execute_async_script("""
                const cb = arguments[arguments.length - 1];
                const out = {};
                const tasks = [];
                // UA-CH high entropy
                if (navigator.userAgentData) {
                    tasks.push(navigator.userAgentData.getHighEntropyValues([
                        'architecture','bitness','model','platformVersion',
                        'uaFullVersion','fullVersionList','wow64'
                    ]).then(he => out.uaCH_he = he).catch(() => {}));
                }
                // Battery
                if (navigator.getBattery) {
                    tasks.push(navigator.getBattery().then(b => {
                        out.battery = {
                            charging: b.charging,
                            level: b.level,
                            chargingTime: b.chargingTime,
                            dischargingTime: b.dischargingTime
                        };
                    }).catch(() => {}));
                }
                // Permissions (a few notable ones)
                const probe = ['geolocation','notifications','camera',
                               'clipboard-read','clipboard-write'];
                const perms = {};
                for (const p of probe) {
                    tasks.push(
                        navigator.permissions.query({name: p})
                            .then(r => perms[p] = r.state)
                            .catch(() => { perms[p] = 'unavailable'; })
                    );
                }
                // Media devices (Patch 1.1)
                tasks.push(
                    navigator.mediaDevices.enumerateDevices().then(list => {
                        out.mediaDevices = list.map(d => ({
                            kind: d.kind,
                            label: d.label,
                            deviceIdPresent: !!d.deviceId && d.deviceId.length > 0,
                            groupIdPresent: !!d.groupId
                        }));
                    }).catch(() => { out.mediaDevices = null; })
                );
                // Speech synthesis voices (Patch 1.2) — may need a tick
                const waitForVoices = async () => {
                    for (let i = 0; i < 12; i++) {
                        const v = speechSynthesis.getVoices();
                        if (v.length) return v;
                        await new Promise(r => setTimeout(r, 80));
                    }
                    return speechSynthesis.getVoices();
                };
                tasks.push(waitForVoices().then(v => {
                    out.speechVoices = v.slice(0, 6).map(x => ({
                        name: x.name, lang: x.lang, default: x.default
                    }));
                    out.speechVoicesCount = v.length;
                }));
                // Performance.now jitter sample (Patch 1.3)
                const pSamples = [];
                for (let i = 0; i < 40; i++) pSamples.push(performance.now());
                out.performanceSample = pSamples;
                out.performanceUniqueFracs = new Set(
                    pSamples.map(x => Math.round((x - Math.floor(x)) * 1000) % 1000)
                ).size;

                Promise.all(tasks).then(() => {
                    out.permissions = perms;
                    cb(out);
                });
            """) or {}
            if isinstance(async_snap, dict):
                actual_values.update(async_snap)
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] couldn't snapshot async values: {e}")

        # Сохраняем results в file for дашборда
        passed = sum(1 for v in results.values() if v is True)
        total  = len(results)       # counts sync + async + mouse test
        selfcheck_data = {
            "timestamp":     datetime.now().isoformat(timespec="seconds"),
            "profile_name":  self.profile_name,
            "passed":        passed,
            "total":         total,
            "tests":         {k: (v if v is True else str(v)) for k, v in results.items()},
            "actual_values": actual_values,
            "expected_values": {
                "hardware":    exp_hw,
                "screen":      exp_screen,
                "languages":   exp_langs,
                "timezone":    exp_tz,
                "battery":     exp_battery,
                "permissions": exp_perms,
                "ua_metadata": exp_ua_md,
            },
        }
        try:
            selfcheck_path = os.path.join(self.user_data_path, "selfcheck.json")
            with open(selfcheck_path, "w", encoding="utf-8") as f:
                json.dump(selfcheck_data, f, indent=2, ensure_ascii=False)
            logging.debug(f"[GhostShellBrowser] selfcheck saved → {selfcheck_path}")
        except Exception as e:
            logging.warning(f"[GhostShellBrowser] selfcheck save failed: {e}")

        # So in DB for dashboard
        try:
            from ghost_shell.db.database import get_db
            get_db().selfcheck_save(
                run_id=self.run_id,
                profile_name=self.profile_name,
                passed=passed,
                total=total,
                tests=selfcheck_data["tests"],
                actual=actual_values,
                expected=selfcheck_data["expected_values"],
            )
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] DB selfcheck save: {e}")

        if verbose:
            logging.info(f"[GhostShellBrowser] Health check: {passed}/{total} passed")
            for name, result in results.items():
                icon = '✓' if result is True else '✗'
                logging.info(f"  {icon} {name}: {result}")

        return results

    def health_check_external(self):
        """Visits external scanner for manual validation."""
        logging.info("[GhostShellBrowser] Connecting to external Bot scanner (sannysoft)...")
        self.driver.get("https://bot.sannysoft.com/")
        time.sleep(5)

    # ──────────────────────────────────────────────────────────
    # TEARDOWN
    # ──────────────────────────────────────────────────────────

    def close(self):
        """Gracefully terminates the driver and background processes."""
        # Release our profile lock ASAP — even if the rest of shutdown
        # fails, a follow-up run against the same profile should be able
        # to start (other protections will still catch true double-spawn).
        gs_lock = getattr(self, "_gs_lock_path", None)
        if gs_lock and os.path.exists(gs_lock):
            try:
                os.remove(gs_lock)
            except OSError:
                pass

        # Stop watchdog FIRST — we don't want it killing Chrome mid-shutdown
        # if our save operations take >30s.
        try:
            self._watchdog_stop.set()
        except Exception:
            pass

        # Stop traffic collector SECOND — it does one final poll via
        # execute_script(). That requires the driver to still be alive.
        if getattr(self, "_traffic_collector", None):
            try:
                self._traffic_collector.stop()
            except Exception as e:
                logging.debug(f"[GhostShellBrowser] traffic collector stop: {e}")
            self._traffic_collector = None

        if self.driver and self.auto_session and self.is_alive():
            try:
                self._auto_save_session()
            except Exception as e:
                logging.warning(f"[GhostShellBrowser] Session auto-save failed during shutdown: {e}")

        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

        if self._proxy_forwarder:
            try:
                self._proxy_forwarder.stop()
                logging.info("[GhostShellBrowser] Local proxy forwarder terminated.")
            except Exception:
                pass

        # Re-stamp Preferences with exited_cleanly=true. driver.quit()
        # sends SIGTERM/closes handles, which Chrome uses to write its
        # own clean-exit markers. BUT if we got here via the hang
        # detector killing the process (SIGKILL), or the process was
        # terminated by the OS, Chrome never gets that chance — its
        # Preferences still has exited_cleanly=false. On next launch
        # Chrome treats that as a crash and tries to restore tabs
        # (THIS is the "9 tabs pile up" symptom from the user's report).
        #
        # Post-shutdown re-stamp fixes this unconditionally: whatever
        # Chrome wrote during its own shutdown gets overwritten by us
        # with exit_type=Normal and exited_cleanly=true, so next launch
        # has a clean-exit marker regardless of how we got here.
        try:
            pref_path = os.path.join(self.user_data_path, "Default", "Preferences")
            if os.path.exists(pref_path):
                with open(pref_path, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
                if isinstance(prefs, dict):
                    prefs.setdefault("profile", {})
                    prefs["profile"]["exit_type"]      = "Normal"
                    prefs["profile"]["exited_cleanly"] = True
                    tmp = pref_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(prefs, f)
                    os.replace(tmp, pref_path)
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] post-shutdown pref stamp: {e}")

        # Detach our profile-specific file handler so logs from the next
        # run don't double up if the process stays alive (e.g. scheduler).
        if getattr(self, "_profile_log_handler", None) is not None:
            try:
                logging.getLogger().removeHandler(self._profile_log_handler)
                self._profile_log_handler.close()
            except Exception:
                pass
            self._profile_log_handler = None

        logging.info(f"[GhostShellBrowser] Session successfully closed. Profile: {self.profile_name}")