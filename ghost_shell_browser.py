"""
NK Browser Core - C++ Native Driver
------------------------------------------------------------
This module manages the execution of the custom Chromium browser.
It relies entirely on the C++ Core payload injection architecture, 
meaning absolutely no JavaScript is injected to spoof fingerprints.
Protection level: Canvas, WebGL, Audio, Navigator, Screen, Fonts (C++ Native).
"""

import os
import json
import random
import logging
import time
import math
import re
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
from device_templates import DeviceTemplateBuilder

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
                from db import get_db
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

        # Profile Enrichment (Simulate an aged browser profile before creation).
        # Can be disabled via env (GHOST_SHELL_SKIP_ENRICH=1) for debugging.
        is_new_profile = not os.path.exists(self.user_data_path)
        os.makedirs(self.user_data_path, exist_ok=True)
        self.session_dir = os.path.join(self.user_data_path, "ghostshell_session")

        skip_enrich = os.environ.get("GHOST_SHELL_SKIP_ENRICH") == "1"
        if is_new_profile and enrich_on_create and not skip_enrich:
            try:
                from profile_enricher import ProfileEnricher
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
            from db import get_db
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

        # 3. Configure Local Preferences (WebRTC & permissions ONLY).
        # Language settings are NOT written here — that would overwrite our
        # C++ GhostShellConfig.languages. All language stuff comes from payload.
        pref_path = os.path.join(self.user_data_path, "Default", "Preferences")
        os.makedirs(os.path.dirname(pref_path), exist_ok=True)

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
                }
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

        # 3. Configure Chrome Options (plain selenium — we have C++ patches
        # for detection evasion, undetected_chromedriver is redundant here).
        options = ChromeOptions()
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
            from proxy_forwarder import ProxyForwarder
            self._proxy_forwarder = ProxyForwarder(self.proxy_str)
            local_port = self._proxy_forwarder.start()
            options.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
            options.add_argument("--proxy-bypass-list=<-loopback>")

        # Check and bind custom Chromium core.
        # Resolution is platform-aware via platform_paths.find_chrome_binary:
        #   Windows — <dir>/chrome.exe
        #   macOS   — <dir>/Chromium.app/Contents/MacOS/Chromium  (app bundle)
        #   Linux   — <dir>/chrome or <dir>/chromium
        from platform_paths import find_chrome_binary, find_chromedriver
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

        # 4. CDP Emulations (Pre-navigation)
        self._apply_cdp_overrides(payload)
        self._set_network_conditions(payload)

        # Add minor viewport jitter
        w = win_w - 80 + random.randint(-15, 15)
        h = win_h - 120 + random.randint(-15, 15)
        self.driver.set_window_size(w, h)

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

        logging.info(f"[GhostShellBrowser] Core launched successfully. Profile: {self.profile_name}")
        return self.driver

    def _apply_cdp_overrides(self, payload: dict):
        """
        Минимальные CDP override'ы.

        ВАЖНО: мы НЕ дублируем подмену UserAgent/UA-CH via CDP thenу that
        this already делает C++ ядро via GhostShellConfig. Дублирование созyesст
        рассогласование между разными слоями (JS navigator.userAgent vs
        HTTP header vs UserAgentMetadata).

        Оставляем only Timezone — if C++ его НЕ патчит в V8, нalreadyн fallback.
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

    # ──────────────────────────────────────────────────────────
    # SESSION MANAGEMENT
    # ──────────────────────────────────────────────────────────

    def _auto_restore_session(self):
        """Restores cookies and local storage from the previous session."""
        from session_manager import SessionManager
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
            from session_manager import SessionManager
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
            from cookie_warmer import CookieWarmer
            CookieWarmer(self.driver).fast_warmup()
            self._log_activity("warmup", "fast")
            return

        if depth == "hybrid":
            from cookie_warmer import CookieWarmer
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
            from rotating_proxy import RotatingProxyTracker
            # Read all rotation config from DB (set via dashboard Proxy page)
            try:
                from db import get_db
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
        tracker.force_rotate()
        time.sleep(random.uniform(3, 8))
        new_ip = tracker.wait_for_rotation(self.driver, old_ip, timeout=60)
        if new_ip:
            tracker.enrich_ip_info(new_ip, self.driver)
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

        tracker.enrich_ip_info(current_ip, self.driver)
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
        Сравнивает значения в JS с ожиyesемыми из payload.
        """
        self.driver.get("about:blank")
        time.sleep(1)

        # Ожиyesемые значения из afterднего сгенерированного payload
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

        # Checks соответствия C++ патчей
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
            # Chrome может возвращать Kiev yesже когyes в payload Kyiv
            tests["timezone_matches"] = (
                "['Europe/Kyiv','Europe/Kiev'].includes("
                "Intl.DateTimeFormat().resolvedOptions().timeZone)"
            )

        results = {}
        for name, code in tests.items():
            try:
                results[name] = bool(self.driver.execute_script(f"return Boolean({code});"))
            except Exception as e:
                results[name] = f"Error: {str(e)[:40]}"

        # Также собираем АКТУАЛЬНЫЕ значения из JS (for yesшборyes)
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
                };
            """)
        except Exception as e:
            logging.debug(f"[GhostShellBrowser] couldn't snapshot actual values: {e}")

        # Сохраняем результаты в файл for yesшборyes
        passed = sum(1 for v in results.values() if v is True)
        total  = len(tests)
        selfcheck_data = {
            "timestamp":     datetime.now().isoformat(timespec="seconds"),
            "profile_name":  self.profile_name,
            "passed":        passed,
            "total":         total,
            "tests":         {k: (v if v is True else str(v)) for k, v in results.items()},
            "actual_values": actual_values,
            "expected_values": {
                "hardware": exp_hw,
                "screen":   exp_screen,
                "languages": exp_langs,
                "timezone": exp_tz,
            },
        }
        try:
            selfcheck_path = os.path.join(self.user_data_path, "selfcheck.json")
            with open(selfcheck_path, "w", encoding="utf-8") as f:
                json.dump(selfcheck_data, f, indent=2, ensure_ascii=False)
            logging.debug(f"[GhostShellBrowser] selfcheck saved → {selfcheck_path}")
        except Exception as e:
            logging.warning(f"[GhostShellBrowser] selfcheck save failed: {e}")

        # Также in DB for dashboard
        try:
            from db import get_db
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