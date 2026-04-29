"""
main.py — Competitor ad monitoring via Ghost Shell browser

Simplified flow (trusting custom Chromium C++ patches):
  1. First run only: inject Google consent cookies for instant trust
  2. For each query from config:
     - Open google.com/search?q=<query>&gl=ua&hl=uk directly (no form typing)
     - Wait for SERP to render
     - Parse ad blocks (TreeWalker JS)
     - For each ad: run configured post_ad actions (visit domain, dwell, scroll)
     - Save everything to SQLite
  3. Save session on exit (cookies + localStorage)
  4. Next run: restore session, start from step 2 (no warmup needed)

Custom actions are defined in config.actions.post_ad as a list of dicts:
  [
    {type: "visit",   probability: 0.3,  dwell_min: 5, dwell_max: 15},
    {type: "scroll",  min_scrolls: 1, max_scrolls: 3},
    {type: "back",    delay_sec: 2}
  ]

On matched target_domains, the actions.on_target_domain list is used instead.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import sys
import re
import time
import random
import logging
import json
import atexit
import threading
import requests
from datetime import datetime

# ──────────────────────────────────────────────────────────────
# sys.modules alias — fix double-execution of module-level code.
# ──────────────────────────────────────────────────────────────
# When main.py is launched via `python -m ghost_shell monitor`,
# ghost_shell/__main__.py calls
#   runpy.run_module("ghost_shell.main", run_name="__main__")
# Python registers this module under sys.modules['__main__'], NOT
# under 'ghost_shell.main'. So when other modules later do
#   from ghost_shell.main import parse_ads, is_captcha_page
# (we have 3 such imports in actions/runner.py), Python doesn't
# find 'ghost_shell.main' in sys.modules and triggers a FRESH
# import — re-running every module-level statement: logging
# config, [main] log lines, RUN_ID resolution, AND a brand-new
# heartbeat thread. Symptoms in the log are duplicate
#   [main] Proxy from GHOST_SHELL_PROXY_URL env: ...
#   [main] Started run #N for profile '...'
# lines mid-run, plus two pings per heartbeat interval.
# Registering the alias right after import-time identity is known
# means later `from ghost_shell.main import X` is a cache hit.
if __name__ == "__main__":
    sys.modules.setdefault("ghost_shell.main", sys.modules[__name__])
from urllib.parse import urlparse, parse_qs, unquote, quote
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from ghost_shell.browser.runtime import GhostShellBrowser
from ghost_shell.proxy.diagnostics import ProxyDiagnostics
from ghost_shell.session.quality import SessionQualityMonitor
from ghost_shell.config import Config
from ghost_shell.db.database import get_db
from ghost_shell.core.log_banners import (
    log_run_start, log_run_end, log_query_result,
    log_payload_summary, log_step, log_error_banner,
)


# ──────────────────────────────────────────────────────────────
# Configuration — read from DB
# ──────────────────────────────────────────────────────────────

CFG = Config.load()
DB  = get_db()

SEARCH_QUERIES       = CFG.get("search.queries")
MY_DOMAINS           = CFG.get("search.my_domains", [])
TARGET_DOMAINS       = CFG.get("search.target_domains", [])

# ──────────────────────────────────────────────────────────────
# PROFILE RESOLUTION — figure out which profile we're running for
# ──────────────────────────────────────────────────────────────
PROFILE_NAME         = (
    os.environ.get("GHOST_SHELL_PROFILE_NAME")   # new canonical name (set by dashboard_server pool)
    or os.environ.get("GHOST_SHELL_PROFILE")     # legacy alias — kept so manual CLI launches still work
    or CFG.get("browser.profile_name")
)

# ──────────────────────────────────────────────────────────────
# PROXY SELECTION — priority order:
#   1. GHOST_SHELL_PROXY_URL env (dashboard pool per-run override)
#   2. Per-profile proxy override in the profiles table
#   3. Global proxy.use_pool + random pick from proxy.pool_urls
#   4. Global proxy.url (single endpoint)
# This matches the effective-proxy logic in db.profile_effective_proxy()
# so behavior is consistent whether run from dashboard or CLI.
# ──────────────────────────────────────────────────────────────
PROXY_USE_POOL       = CFG.get("proxy.use_pool", False)
PROXY_POOL_URLS      = CFG.get("proxy.pool_urls", []) or []
PROXY_SINGLE         = CFG.get("proxy.url", "")

_env_proxy = os.environ.get("GHOST_SHELL_PROXY_URL", "").strip()
_profile_proxy = None
# ── NEW: proxies library → per-profile assignment ──
# Try the proxies table first — this is the new Dolphin-style flow
# where each profile points at a named proxy row. Falls through to
# legacy resolution if the table is empty or profile has no assignment
# (fresh install before migration, or someone nuked the table).
_library_proxy_url = None
_library_proxy_meta = None   # holds rotation info from the resolved row
if PROFILE_NAME:
    try:
        _lib_proxy = DB.proxy_resolve_for_profile(PROFILE_NAME)
        if _lib_proxy and _lib_proxy.get("url"):
            _library_proxy_url = _lib_proxy["url"]
            _library_proxy_meta = _lib_proxy
    except Exception as e:
        logging.debug(f"[main] proxy_resolve_for_profile failed: {e}")

# Legacy per-profile override (profiles.proxy_url column) — kept as a
# fallback for old configs that didn't migrate. New code should use
# the proxies table.
if PROFILE_NAME:
    try:
        _meta = DB.profile_meta_get(PROFILE_NAME)
        _profile_proxy = (_meta.get("proxy_url") or "").strip() or None
    except Exception:
        _profile_proxy = None

if _env_proxy:
    PROXY = _env_proxy
    logging.info(f"[main] Proxy from GHOST_SHELL_PROXY_URL env: {PROXY[:50]}...")
elif _library_proxy_url:
    PROXY = _library_proxy_url
    name = _library_proxy_meta.get("name") if _library_proxy_meta else "?"
    logging.info(f"[main] Proxy from library: {name!r} → {PROXY[:50]}...")
elif _profile_proxy:
    PROXY = _profile_proxy
    logging.info(f"[main] Proxy from profile override (legacy): {PROXY[:50]}...")
elif PROXY_USE_POOL and PROXY_POOL_URLS:
    PROXY = random.choice([p for p in PROXY_POOL_URLS if p.strip()])
    logging.info(f"[main] Picked random proxy from pool: {PROXY[:50]}...")
else:
    PROXY = PROXY_SINGLE

# Also resolve rotation settings with per-profile overrides
def _resolve_rotation():
    """Returns (is_rotating, rotation_url, rotation_provider, rotation_api_key).
    Per-profile values in the profiles table take precedence over globals.

    SEMANTICS: `is_rotating` is effectively derived — if a rotation_api_url
    is configured and reachable, we treat the proxy as rotating regardless
    of the explicit `proxy.is_rotating` flag. The flag was originally a
    hint for proxies that auto-rotate on each connect (without a trigger
    API), but we now support trigger-based rotation as the primary path
    and users kept forgetting to tick the flag after setting up the API.

    Explicit False still wins — a user can force-disable rotation for
    debugging. But the default behavior is: rotation_api set → rotation on.
    """
    explicit_flag = CFG.get("proxy.is_rotating")   # may be None (not set)
    url    = CFG.get("proxy.rotation_api_url")
    prov   = CFG.get("proxy.rotation_provider", "none")
    key    = CFG.get("proxy.rotation_api_key")

    # ── NEW: proxy library values take precedence over config ──
    # If the profile has a proxy assignment, read rotation settings
    # from that row. The assignment itself was resolved earlier in
    # _library_proxy_meta — check the module var, not an API call.
    if _library_proxy_meta:
        pm = _library_proxy_meta
        if pm.get("is_rotating") is not None:
            explicit_flag = bool(pm["is_rotating"])
        if pm.get("rotation_api_url"):
            url = pm["rotation_api_url"]
        if pm.get("rotation_provider"):
            prov = pm["rotation_provider"]
        if pm.get("rotation_api_key"):
            key = pm["rotation_api_key"]

    # Legacy per-profile override — only applied when library values
    # weren't populated (fresh install pre-migration, or proxy row
    # with no rotation columns).
    if PROFILE_NAME and not (_library_proxy_meta
                             and _library_proxy_meta.get("rotation_api_url")):
        try:
            meta = DB.profile_meta_get(PROFILE_NAME)
            if meta.get("proxy_is_rotating") is not None:
                explicit_flag = bool(meta["proxy_is_rotating"])
            if meta.get("rotation_api_url"):
                url = meta["rotation_api_url"]
            if meta.get("rotation_provider"):
                prov = meta["rotation_provider"]
            if meta.get("rotation_api_key"):
                key = meta["rotation_api_key"]
        except Exception:
            pass

    # Derivation logic:
    #   explicit_flag=False  → user explicitly disabled, respect that
    #   explicit_flag=True   → rotation on (backward-compat)
    #   explicit_flag=None   → rotation on IFF a rotation API is configured
    if explicit_flag is False:
        is_rot = False
    elif explicit_flag is True:
        is_rot = True
    else:
        is_rot = bool(url and prov and prov != "none")

    return is_rot, url, prov, key

IS_ROTATING_PROXY, ROTATION_API_URL, _ROT_PROVIDER, _ROT_KEY = _resolve_rotation()
PREFERRED_LANGUAGE   = CFG.get("browser.preferred_language", "uk-UA")
EXPECTED_COUNTRY     = CFG.get("browser.expected_country",    "Ukraine")
EXPECTED_TIMEZONE    = CFG.get("browser.expected_timezone",   "Europe/Kyiv")
GEO_MISMATCH_MODE    = CFG.get("browser.geo_mismatch_mode",   "warn")
# geo_mismatch_mode values:
#   "abort"  — refuse to run if exit country != expected
#   "rotate" — try rotating proxy up to N times to find expected country
#   "warn"   — just log a warning and continue

REFRESH_MIN_SEC      = CFG.get("search.refresh_min_sec", 10)
REFRESH_MAX_SEC      = CFG.get("search.refresh_max_sec", 15)
REFRESH_MAX_ATTEMPTS = CFG.get("search.refresh_max_attempts", 4)

# ──────────────────────────────────────────────────────────────
# BEHAVIOR TIMING — all configurable from dashboard Behavior page.
# Every sleep/delay in the monitor run reads from these values, so
# users can dial the whole pipeline from calm (long delays) to
# aggressive (short) without touching code.
# ──────────────────────────────────────────────────────────────
BEHAVIOR = {
    # Delay after first page load, before we start looking for ads
    "initial_load_min":     CFG.get("behavior.initial_load_min",     2.0),
    "initial_load_max":     CFG.get("behavior.initial_load_max",     4.0),

    # Delay between SERP being ready and reading its contents
    "serp_settle_min":      CFG.get("behavior.serp_settle_min",      1.5),
    "serp_settle_max":      CFG.get("behavior.serp_settle_max",      3.0),

    # Delay after driver.refresh() before we re-check page state
    "post_refresh_min":     CFG.get("behavior.post_refresh_min",     2.0),
    "post_refresh_max":     CFG.get("behavior.post_refresh_max",     4.0),

    # Delay after an IP rotation, before re-running geo diagnostics
    "post_rotate_min":      CFG.get("behavior.post_rotate_min",      2.0),
    "post_rotate_max":      CFG.get("behavior.post_rotate_max",      4.0),

    # Delay after stealth_get("https://google.com") on a fresh profile
    "fresh_google_min":     CFG.get("behavior.fresh_google_min",     3.0),
    "fresh_google_max":     CFG.get("behavior.fresh_google_max",     5.0),

    # Delay after accepting consent banner
    "post_consent_min":     CFG.get("behavior.post_consent_min",     2.0),
    "post_consent_max":     CFG.get("behavior.post_consent_max",     4.0),

    # Gap between consecutive queries in a single run
    "between_queries_min":  CFG.get("behavior.between_queries_min",  6.0),
    "between_queries_max":  CFG.get("behavior.between_queries_max", 12.0),
}


def _sleep(kind: str):
    """Look up a configured min..max range and sleep a random value in it."""
    lo = BEHAVIOR.get(f"{kind}_min")
    hi = BEHAVIOR.get(f"{kind}_max")
    if lo is None or hi is None:
        logging.warning(f"[behavior] unknown sleep kind: {kind}")
        return
    t = random.uniform(float(lo), float(hi))
    time.sleep(t)

POST_AD_ACTIONS          = CFG.get("actions.post_ad_actions",
                                   CFG.get("actions.post_ad", []))
ON_TARGET_DOMAIN_ACTIONS = CFG.get("actions.on_target_domain_actions",
                                   CFG.get("actions.on_target_domain", []))

# Diagnostic: surface pipeline configuration at startup so empty
# pipelines (the cause of "Actions=0 across the board" on the
# Competitors page) are visible. Only warn when there's a clear
# mismatch — user has TARGET_DOMAINS set (i.e. expects to act on ads)
# but no pipeline at all → nothing will be clicked.
if not POST_AD_ACTIONS and not ON_TARGET_DOMAIN_ACTIONS and TARGET_DOMAINS:
    logging.warning(
        "  ⚠ pipeline empty: actions.post_ad_actions=[] AND "
        "actions.on_target_domain_actions=[] but search.target_domains "
        "is set — every ad will be tagged 'no_pipeline' / skipped. "
        "Open Dashboard → Scripts to configure click_ad / scroll / "
        "read steps."
    )
elif not POST_AD_ACTIONS and ON_TARGET_DOMAIN_ACTIONS:
    logging.info(
        f"  ⓘ pipeline: legacy on_target only "
        f"({len(ON_TARGET_DOMAIN_ACTIONS)} step(s)) — "
        f"competitor ads will be logged as 'no_pipeline' (visible on "
        f"Competitors page → Actions column). Migrate via Scripts page "
        f"to enable click_ad on competitors too."
    )
else:
    logging.info(
        f"  ⓘ pipeline: post_ad={len(POST_AD_ACTIONS)} step(s), "
        f"on_target={len(ON_TARGET_DOMAIN_ACTIONS)} step(s)"
    )

# run_id passed via env from dashboard, or created standalone
RUN_ID = int(os.environ.get("GHOST_SHELL_RUN_ID", "0")) or None
if RUN_ID is None:
    RUN_ID = DB.run_start(PROFILE_NAME, proxy_url=PROXY)

# Also record our PID in the runs table.
try:
    DB.run_set_pid(RUN_ID, os.getpid())
except Exception:
    pass

HEARTBEAT_INTERVAL = 15  # seconds

# Dup file for manual inspection
COMPETITOR_URLS_FILE = "competitor_urls.txt"


# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
#
# Windows quirk: the default terminal encoding is cp1252 / cp866 / cp1251
# depending on regional settings, and Python's StreamHandler writes to
# sys.stdout with that codec. Any emoji (🌱, ✓, ✗, arrows →) or
# Cyrillic text we log crashes with UnicodeEncodeError — not fatal
# (Python recovers) but it pollutes the console with 20-line tracebacks
# every time.
#
# Fix: force stdout/stderr into UTF-8 mode BEFORE any logging call.
# Python 3.7+ has reconfigure(). `errors='replace'` gives us a fallback
# character (?) rather than an exception if even UTF-8 fails for some
# exotic codepoint — so logging becomes crash-proof for the whole run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, Exception):
    # Some terminals (older Windows Python, pipes to non-TTY) don't
    # support reconfigure — ignore and fall through to ASCII-safe text.
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)
# undetected_chromedriver dumps a 25-line C++ stacktrace on failure.
# We catch the exception at a higher level with a readable summary.
logging.getLogger("undetected_chromedriver").setLevel(logging.WARNING)


# ──────────────────────────────────────────────────────────────
# Heartbeat — lets process_reaper distinguish "alive but slow"
# from "genuinely wedged" runs. We ping DB every HEARTBEAT_INTERVAL
# seconds; if main thread is fully stuck (e.g. driver hang), this
# background thread keeps pinging — and hang detection falls back
# to the BROWSER watchdog (ghost_shell_browser.py). Conversely, if
# main dies uncleanly (kill -9, OOM), the daemon thread dies with
# it and heartbeats stop — reap_stale_runs picks it up.
#
# Started AFTER logging.basicConfig so its debug/warning lines
# route through our configured handlers.
# ──────────────────────────────────────────────────────────────

def _heartbeat_loop():
    while not getattr(_heartbeat_loop, "_stop", False):
        try:
            DB.run_heartbeat(RUN_ID)
        except Exception as e:
            logging.debug(f"[main] heartbeat failed: {e}")
        # Sleep in small slices so shutdown is responsive
        for _ in range(HEARTBEAT_INTERVAL):
            if getattr(_heartbeat_loop, "_stop", False):
                return
            time.sleep(1)

_heartbeat_thread = threading.Thread(
    target=_heartbeat_loop, daemon=True, name="main-heartbeat"
)
_heartbeat_thread.start()

def _stop_heartbeat():
    _heartbeat_loop._stop = True

atexit.register(_stop_heartbeat)
logging.info(f"[main] Started run #{RUN_ID} for profile '{PROFILE_NAME}'")


# ──────────────────────────────────────────────────────────────
# PAGE STATE HELPERS
# ──────────────────────────────────────────────────────────────

def is_offline_page(driver) -> bool:
    """Detect Chrome offline page ('Check your internet connection' etc.)"""
    try:
        title = (driver.title or "").lower()
        if any(m in title for m in ("offline", "не в мережі", "не в сети")):
            return True
        body_text = driver.execute_script(
            "return (document.body && document.body.innerText || '').substring(0, 300).toLowerCase();"
        )
        markers = [
            "підkeyіться до інтернеу", "connect to the internet",
            "в режимі офлайн", "you're offline", "you are offline",
            "no соединения", "underkeysтесь к интернеу",
        ]
        return any(m in body_text for m in markers)
    except Exception:
        return False


def is_captcha_page(driver) -> bool:
    """
    Detect Google captcha / 'unusual traffic' block pages.
    These land on /sorry/index or contain a recaptcha challenge.
    """
    try:
        url = (driver.current_url or "").lower()
        if "/sorry/" in url or "recaptcha" in url:
            return True
        title = (driver.title or "").lower()
        if any(m in title for m in (
            "unusual traffic",
            "підозрілий трафік",
            "underозрительный трафик",
            "before you continue to google",
            "are you a robot",
        )):
            return True
        # DOM-level check: captcha iframe / form
        found = driver.execute_script("""
            return !!(
                document.querySelector('iframe[src*="recaptcha"]') ||
                document.querySelector('#captcha-form') ||
                document.querySelector('#recaptcha') ||
                document.querySelector('div.g-recaptcha') ||
                (document.body && /unusual traffic|невичайний трафік|неипичный трафик/i
                    .test(document.body.innerText.substring(0, 500)))
            );
        """)
        return bool(found)
    except Exception:
        return False


def is_ads_preview_page(driver) -> bool:
    """
    Detect the Google Ads Preview warning page.
    This appears when URL has service params like pws=0 or adtest=on.
    We use a clean URL, but still check as a safety net.
    """
    try:
        body_text = driver.execute_script(
            "return (document.body && document.body.innerText || '').substring(0, 500).toLowerCase();"
        )
        markers = [
            "ця сторінка призначена для випробовування",
            "this page is for testing google ads",
            "эта page предназначена для тестирования",
        ]
        return any(m in body_text for m in markers)
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────
# SEARCH URL
# ──────────────────────────────────────────────────────────────

def build_search_url(query: str) -> str:
    """
    Clean Google search URL — no service params to avoid Ads Preview page.
    Location and language come from IP + Accept-Language header.
    """
    return f"https://www.google.com/search?q={quote(query)}"


# ──────────────────────────────────────────────────────────────
# MID-RUN CRASH RECOVERY
# ──────────────────────────────────────────────────────────────

# Module-level counter so we don't restart endlessly. Chrome on
# Windows very occasionally dies mid-run (InvalidSessionId while
# clicking a heavy ad landing page; GPU process crash on video
# ads; memory pressure from heavy SPA targets). Recovering once
# or twice per run is sensible — beyond that, something's wrong
# with the profile or the IP and restarting won't help.
_RECOVERY_ATTEMPTS = {
    "count":    0,
    "max":      2,         # two restarts per run, then we bail
}


def _browser_dead(exc: Exception = None) -> bool:
    """Check if an exception indicates the driver is actually dead vs
    just a transient error. Used to decide whether to attempt recovery
    or just retry the current operation."""
    if exc is None:
        return False
    name = type(exc).__name__
    msg  = str(exc).lower()
    if name in (
        "InvalidSessionIdException",
        "NoSuchWindowException",
        "WebDriverException",
    ):
        # WebDriverException is broad — it also fires on network blips.
        # Only treat as dead if the message suggests permanent failure.
        if name == "WebDriverException":
            return any(k in msg for k in (
                "chrome not reachable",
                "disconnected",
                "target window already closed",
                "no such session",
                "session not created",
                "unable to receive message",
            ))
        return True
    return False


def _post_rotation_warmup(driver, browser=None, count: int = None) -> None:
    """Recovery #3 — visit a few neutral domains after IP rotation
    before retrying the captcha-causing search.

    Why: Google fingerprints "cold IP, no browsing context, fires
    search query" as a strong bot signal. Real users land on a fresh
    IP via DHCP / VPN switch / mobile-tower handoff and typically have
    minutes of unrelated browsing under that IP before they search for
    a brand. We mimic that by visiting 2-3 high-traffic, non-Google
    domains for 4-8s each with realistic dwell + scroll, building up
    a plausible Referer chain and HTTP/2 connection history before
    we go back to the SERP.

    Choices are deliberately:
      - non-Google (so the SERP feels organic, not search-funnel)
      - high-traffic (won't itself flag captcha on a clean exit)
      - localised mix (works for UA/RU/EN profiles without forcing locale)
      - HTTPS-only (no mixed-content side effects)
    Recovery #3 default count: 3. Override via env / config if needed.

    The browser arg is accepted but unused; kept so callers can pass
    it without checking for it.
    """
    if count is None:
        try:
            count = int(CFG.get("captcha.warmup_count", 3))
        except Exception:
            count = 3
    if count <= 0:
        return

    # Curated list — high-traffic, fast TLS, varied geo so the IP/locale
    # doesn't immediately leak its preferences.
    _WARMUP_POOL = [
        "https://en.wikipedia.org/wiki/Special:Random",
        "https://uk.wikipedia.org/wiki/Спеціальна:Random",
        "https://www.bbc.com/news",
        "https://www.reuters.com/",
        "https://news.ycombinator.com/",
        "https://www.weather.com/",
        "https://www.yahoo.com/",
    ]
    picks = random.sample(_WARMUP_POOL, min(count, len(_WARMUP_POOL)))
    logging.info(f"  ⏳ post-rotation warmup: visiting {len(picks)} site(s) "
                 f"to break the cold-IP+immediate-search pattern")
    for i, url in enumerate(picks, 1):
        try:
            t0 = time.time()
            driver.get(url)
            # 4-8s dwell + slow scroll. Tune via captcha.warmup_min/max.
            lo = float(CFG.get("captcha.warmup_dwell_min", 4))
            hi = float(CFG.get("captcha.warmup_dwell_max", 8))
            time.sleep(random.uniform(lo, hi))
            try:
                # Three small scrolls — looks like a glance-and-leave.
                for _ in range(3):
                    driver.execute_script(
                        "window.scrollBy(0, Math.floor("
                        "(document.body.scrollHeight || 800)/4));"
                    )
                    time.sleep(random.uniform(0.4, 1.0))
            except Exception:
                pass
            logging.info(f"     [{i}/{len(picks)}] {url[:60]} "
                         f"({time.time()-t0:.1f}s)")
        except Exception as e:
            # Warmup is best-effort — if one site fails we keep going.
            logging.debug(f"     warmup site failed ({url}): {e}")
    logging.info(f"  ✓ warmup complete — retrying SERP")


def _auto_regenerate_fingerprint(profile_name: str, reason: str) -> bool:
    """Recovery #4 — replace the profile's current fingerprint with a
    fresh, randomly-templated one so the next run comes back as a
    different identity. Called when IP rotation + session restart both
    failed to clear captcha — a strong signal that Google flagged the
    fingerprint, not the IP.

    Why "next run" not "this run": regenerating mid-run would require
    a full browser teardown + relaunch with the new --ghost-shell-payload,
    which is what happens on the NEXT scheduled run anyway (the
    scheduler reads fingerprint_current() at launch). Forcing it here
    risks racing with the current process's open Chrome and producing
    weird state. Cleaner: persist the new FP, end the run, let the
    scheduler pick up the change.

    Returns True if a new fingerprint was saved, False otherwise.
    """
    try:
        from ghost_shell.fingerprint.generator import generate as _gen_fp
        from ghost_shell.fingerprint.templates import get_template
        from ghost_shell.fingerprint.validator import validate
        from ghost_shell.dashboard.server import _flat_fp_to_runtime_shape  # type: ignore
    except Exception as e:
        # Fallback when running outside the dashboard import graph
        logging.warning(
            f"  fp-regen: helper imports failed: {e} — "
            f"using minimal regen path"
        )
        try:
            from ghost_shell.fingerprint.generator import generate as _gen_fp
            from ghost_shell.fingerprint.validator import validate
        except Exception as e2:
            logging.error(f"  fp-regen aborted: {e2}")
            return False
        get_template = None
        _flat_fp_to_runtime_shape = None  # type: ignore

    try:
        # New random template — explicitly ask for a fresh one (not the
        # current one) so the new FP can't accidentally be reshuffled
        # noise on the same identity.
        new_fp = _gen_fp(
            profile_name=profile_name,
            template_id=None,
        )
    except Exception as e:
        logging.error(f"  fp-regen generate() failed: {e}")
        return False

    coherence = None
    if get_template and _flat_fp_to_runtime_shape:
        try:
            tmpl = get_template(new_fp.get("template_id"))
            shape = _flat_fp_to_runtime_shape(new_fp)
            coherence = validate(shape, tmpl)
        except Exception as e:
            logging.debug(f"  fp-regen validate skipped: {e}")

    try:
        from ghost_shell.db.database import get_db
        fp_id = get_db().fingerprint_save(
            profile_name,
            new_fp,
            coherence_score=(coherence or {}).get("score"),
            coherence_report=coherence,
            source="auto_regen_after_captcha",
            reason=reason,
        )
    except Exception as e:
        logging.error(f"  fp-regen DB save failed: {e}")
        return False

    new_template = (
        new_fp.get("template_label") or new_fp.get("template_name")
        or new_fp.get("template_id") or "?"
    )
    logging.warning(
        f"  🔄 FINGERPRINT AUTO-REGENERATED for '{profile_name}' "
        f"(fp_id={fp_id}, template={new_template}, reason={reason!r}). "
        f"Next run will launch with the new identity."
    )
    return True


def _try_recover_browser(browser, reason: str = "") -> bool:
    """Attempt one mid-run browser restart. Returns True if the driver
    came back alive, False otherwise.

    Preserves session state: Chrome's on-disk profile (cookies, history,
    localStorage) lives in user-data-dir and is untouched by restart.
    The in-memory selenium driver is what gets rebuilt. Cookies are
    re-imported via _auto_save_session() → session_manager.import_cookies
    on the fresh Chrome, so the "already warmed up" state survives.

    Budget: 2 restarts per run, enforced by _RECOVERY_ATTEMPTS. Past
    that, the run is probably doomed (profile corrupted / IP burned /
    memory exhausted) and more restarts just waste time.
    """
    if _RECOVERY_ATTEMPTS["count"] >= _RECOVERY_ATTEMPTS["max"]:
        logging.error(
            f"  ✗ browser recovery budget exhausted "
            f"({_RECOVERY_ATTEMPTS['count']}/{_RECOVERY_ATTEMPTS['max']}) — "
            f"giving up on this run"
        )
        return False

    _RECOVERY_ATTEMPTS["count"] += 1
    logging.warning(
        f"  ⚠ browser died mid-run ({reason or 'unknown'}), "
        f"attempting recovery "
        f"[{_RECOVERY_ATTEMPTS['count']}/{_RECOVERY_ATTEMPTS['max']}]..."
    )
    try:
        browser.restart()
        if browser.is_alive():
            logging.info("  ✓ browser recovered, continuing run")
            return True
    except Exception as e:
        logging.error(f"  ✗ browser recovery failed: {type(e).__name__}: {e}")
    return False


# ──────────────────────────────────────────────────────────────
# AD PARSER
# ──────────────────────────────────────────────────────────────

def extract_real_url(href: str) -> str:
    """Parse adurl/url/q/dest_url/... parameter from Google redirect URL.

    Modern Google ad-click URLs use different parameter names depending
    on the format:
      - text ads:        adurl, url, q
      - shopping_carousel: dest_url, ducr, dadu, durl
      - PLA grid:        adurl, dest_url
    Some also embed the destination twice (in two different params,
    sometimes with one truncated). We try every known key in priority
    order and pick the first one that yields a clean http URL.
    """
    if not href:
        return ""
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        # Try each candidate key in priority order. Order matters: for
        # shopping ads, dest_url is the canonical destination while
        # adurl is sometimes the merchant feed URL (less useful).
        for key in (
            "adurl", "dest_url", "ducr", "dadu", "durl",
            "url", "q", "target", "redirect_url", "u",
        ):
            if key in qs:
                real = unquote(qs[key][0] or "")
                # Recursively unwrap if the destination is itself a
                # google redirect (happens with shopping carousels:
                # aclk?dest_url=...&adurl=https://www.google.com/url?...)
                if real.startswith("http"):
                    if "google.com/url?" in real or "google.com/aclk?" in real:
                        unwrapped = extract_real_url(real)
                        if unwrapped and unwrapped != real:
                            return unwrapped
                    return real
    except Exception:
        pass
    return href


def extract_domain(url: str) -> str:
    if not url:
        return ""
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def parse_ads(driver, query: str) -> list[dict]:
    """Extract ad blocks from SERP — all formats.

    Covers: classic text ads (Sponsored label + heading + destination link),
    the shopping "Рекламируемые товары / Sponsored products" carousel, and
    the PLA (Product Listing Ad) grid below-fold. These three use
    different DOM shapes and we historically only handled the first.

    Return value: list of dicts with an `anchor_id` the DOM now carries
    on the exact <a> element the click should target. click_ad can then
    drill down by ID without re-guessing selectors and accidentally
    picking a sibling card (that was the "clicked our own goodmedika
    instead of the competitor" symptom — the carousel's first item was
    own domain, and the old click_ad just grabbed the first DOM ad link).

    Own-domain filtering happens in Python (below) using MY_DOMAINS
    — we do NOT rely on CSS class names Google could change without
    notice. We ALSO stamp each ad card with `data-gs-ad-id` attribute
    in the DOM itself so we can refer to the same element later by
    querySelector(`[data-gs-ad-id="..."]`).
    """
    # D2: declare upfront so both branches below can write to the
    # module-level scratchpad without per-branch `global` statements
    # (which Python rejects when an assignment precedes the declaration
    # in the same function scope).
    global _LAST_SERP_DIAG
    js_script = r"""
    const SPONSORED_MARKERS = [
        'Sponsored', 'Реклама', 'Спонсировано', 'Спонсоване',
        'Anuncio', 'Annonce', 'Werbung', 'Annuncio'
    ];

    // Random ID per scan — so re-parsing the same page doesn't
    // accidentally pick up anchors stamped during a previous query
    // on the same tab (Google sometimes keeps SERP markup live when
    // you nav back via history).
    const scanId = Math.random().toString(36).slice(2, 10);

    // Map of anchor_id -> {title, displayUrl, googleClickUrl, cleanFromDataRw, format}
    const byAnchorId = {};
    let anchorCounter = 0;

    /** Stamp an element with a unique id and return that id. */
    function stampAnchor(el) {
        const id = `gs-${scanId}-${anchorCounter++}`;
        el.setAttribute('data-gs-ad-id', id);
        return id;
    }

    /** Pull title / displayUrl / hrefs from a block + its best anchor. */
    function extractFromBlock(block, format) {
        // ── SHORT-CIRCUIT for shopping cards ────────────────────
        // Verified against live Google Shopping carousels (April 2026):
        //   .mnr-c.pla-unit cards carry a `data-dtld` attribute with
        //   the bare destination host (e.g. "sich.ua",
        //   "vektormed.com.ua", "ravita.shop"). This is the cleanest
        //   signal Google gives -- no aclk parsing, no display-text
        //   regex. Use it FIRST before falling through to the generic
        //   extractor below.
        const dtld = block.getAttribute && block.getAttribute('data-dtld');
        if (dtld && dtld.includes('.') && !dtld.includes('google.com')) {
            const link = block.querySelector('a[href*="aclk"], a[href^="http"]');
            if (link) {
                const anchorId = stampAnchor(link);
                // Title from heading inside the card. Fallback to the
                // visible link text (usually product name + price).
                let title = '';
                const heading = block.querySelector(
                    '[role="heading"], h3, h4, .LC20lb, .pla-unit-title-link, .pymv4e'
                );
                if (heading) title = heading.textContent.trim();
                if (!title) {
                    const linkText = (link.innerText || '').trim();
                    title = linkText.split('\n')[0].slice(0, 100);
                }
                return {
                    anchorId:        anchorId,
                    title:           title,
                    displayUrl:      dtld,
                    googleClickUrl:  link.href || '',
                    cleanFromDataRw: 'https://' + dtld,
                    format:          format,
                    anchorHref:      link.href || '',
                    debugAttrs:      {'data-dtld': dtld,
                                      'merchant-id': block.getAttribute('data-merchant-id') || '',
                                      'offer-id':    block.getAttribute('data-offer-id') || ''},
                };
            }
        }

        let title = '';
        const heading = block.querySelector('[role="heading"], h3, h4, .LC20lb');
        if (heading) title = heading.textContent.trim();

        // displayUrl: green domain text shown on the card. Modern
        // (April 2026) Google SERPs often have empty <cite> elements
        // and put the displayed host inside a plain <span> nested
        // alongside <h3>. Cast a wide net + verify the result looks
        // like a hostname before trusting it.
        let displayUrl = '';
        const cite = block.querySelector(
            'cite, span.VuuXrf, span.x2VHCd, span[role="text"], .x3G5ab, ' +
            // Shopping-specific: merchant name spans
            '.LbUacb, .E5ocAb, .aULzUe, .mr4j5, [data-merchant], ' +
            'div[data-merchant-id], span.zPEcBd'
        );
        if (cite) displayUrl = cite.textContent.trim();
        // Modern fallback: scan all spans in the card for one that
        // looks like a bare hostname (e.g. "goodmedika.com.ua" with
        // no spaces / slashes / weird chars).
        if (!displayUrl) {
            const HOST_RE = /^[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i;
            for (const sp of block.querySelectorAll('span, div')) {
                const t = (sp.textContent || '').trim();
                if (t && t.length < 60 && HOST_RE.test(t) &&
                    !t.includes('google.com')) {
                    displayUrl = t;
                    break;
                }
            }
        }

        // Shopping cards sometimes have a dedicated merchant attr
        if (!displayUrl) {
            const mEl = block.querySelector('[data-merchant], [data-merchant-id], [data-domain]');
            if (mEl) {
                displayUrl = mEl.getAttribute('data-merchant')
                          || mEl.getAttribute('data-merchant-id')
                          || mEl.getAttribute('data-domain') || '';
            }
        }

        let googleClickUrl = '';
        let cleanUrl       = '';
        let primaryAnchor  = null;
        const collectedAttrs = {};   // for debug logging when no_domain drops

        // Attribute priority -- ORDER MATTERS.
        //
        // Verified against a live `гудмедіка` SERP (April 2026, UA
        // locale, Chrome 147):
        //   data-pcu     -> the actual destination URL (e.g. goodmedika.com.ua/).
        //                   Present on the FIRST link of each ad card,
        //                   sometimes missing on subsidiary links.
        //   data-rw      -> a verbatim COPY of the aclk URL itself
        //                   (e.g. https://www.google.com/aclk?...). It
        //                   passes the `startsWith("http")` check so the
        //                   old code grabbed THIS as the "clean URL"
        //                   and then Python dropped it as google_internal.
        //                   Result: we threw away every ad on every SERP
        //                   that didn't have data-pcu on the picked anchor.
        //   data-rh      -> usually a normalised destination host (no
        //                   path), useful as a fallback for displayUrl.
        //   data-agdh    -> Google ad-group debug header (not a URL).
        //
        // So: try data-pcu FIRST. Only fall back to data-rw if nothing
        // else is found AND we explicitly strip the google.com prefix
        // before treating it as a destination.
        const PRIMARY_URL_ATTRS = [
            'data-pcu',          // canonical destination URL
            'data-pla-url',      // shopping PLA destination
            'data-href',
            'data-url',
            'data-target-url',
        ];
        const FALLBACK_URL_ATTRS = [
            'data-rh',           // host only (no path)
            'data-pcuw', 'data-pcuwe',
        ];
        // We collect (but do NOT auto-pick) data-rw because it's
        // typically a redirect URL pointing back to google.
        const REDIRECT_URL_ATTRS = ['data-rw'];

        const allLinks = block.querySelectorAll('a[href]');
        for (const link of allLinks) {
            const href = link.href || '';
            if (!href.startsWith('http')) continue;
            const isAclk = href.includes('/aclk?') ||
                           href.includes('googleadservices.com') ||
                           href.includes('googlesyndication.com');
            if (isAclk) {
                if (!googleClickUrl) {
                    googleClickUrl = href;
                    primaryAnchor = link;
                }
                // Try primary attrs first
                for (const attr of PRIMARY_URL_ATTRS) {
                    const val = link.getAttribute(attr);
                    if (val) {
                        collectedAttrs[attr] = val.slice(0, 200);
                        if (val.startsWith('http') &&
                            !val.includes('google.com/aclk') &&
                            !val.includes('googleadservices.com') &&
                            !cleanUrl) {
                            cleanUrl = val;
                        }
                    }
                }
                // Fallback attrs: bare host -> synthesise https://
                for (const attr of FALLBACK_URL_ATTRS) {
                    const val = link.getAttribute(attr);
                    if (val) {
                        collectedAttrs[attr] = val.slice(0, 200);
                        if (!cleanUrl && val && val.includes('.') &&
                            !val.startsWith('http') &&
                            !val.includes(' ')) {
                            cleanUrl = 'https://' + val;
                        }
                    }
                }
                // Redirect attrs collected for debug only
                for (const attr of REDIRECT_URL_ATTRS) {
                    const v = link.getAttribute(attr);
                    if (v) collectedAttrs[attr] = v.slice(0, 200);
                }
            }
        }

        // Visible-text fallback (the most reliable signal on modern
        // Google SERPs). Each ad anchor renders the destination domain
        // visually right under the title -- "купити медичне обладнання
        // | Гудмедіка \n goodmedika.com.ua \n https://...". Scan the
        // anchor's innerText for a line that looks like a hostname.
        if (!cleanUrl && primaryAnchor) {
            const txt = (primaryAnchor.innerText || '').slice(0, 1500);
            const lines = txt.split('\n').map(s => s.trim()).filter(Boolean);
            const HOST_RE = /^[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:\/[^\s]*)?$/i;
            for (const line of lines) {
                // Drop the ad-card prefix like "https://" or "Реклама"
                let candidate = line.replace(/^https?:\/\//i, '');
                // Cut anything after first space (description text)
                candidate = candidate.split(/\s/)[0];
                if (HOST_RE.test(candidate) && !candidate.includes('google.com')) {
                    // Looks like host[/path] -- normalise to bare host
                    const host = candidate.split('/')[0].toLowerCase();
                    if (host && host.includes('.') && host.length < 100) {
                        cleanUrl = 'https://' + host;
                        collectedAttrs['_from_visible_text'] = host;
                        break;
                    }
                }
            }
        }

        // Also scan the BLOCK itself + its children for data-* attrs
        // that contain non-google http URLs OR a bare hostname
        // (last-resort). Includes data-dtld which Shopping cards
        // carry on the OUTER container (we already short-circuit
        // for top-level shopping cards, but nested cards inside
        // commercial-unit containers may not be covered above).
        if (!cleanUrl) {
            const all = [block, ...block.querySelectorAll('*')];
            const HOST_RE = /^[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i;
            for (const el of all) {
                if (!el.attributes) continue;
                for (const attr of el.attributes) {
                    const n = attr.name;
                    const v = attr.value || '';
                    if (!n.startsWith('data-')) continue;
                    // Full http URL not on google?
                    if (v.startsWith('http') &&
                        !v.includes('google.com') && !v.includes('gstatic.com') &&
                        !v.includes('googleadservices') &&
                        !v.includes('googlesyndication')) {
                        cleanUrl = v;
                        collectedAttrs['_block_' + n] = v.slice(0, 200);
                        break;
                    }
                    // Bare hostname? data-dtld and similar.
                    if (v && v.length < 80 && HOST_RE.test(v) &&
                        !v.includes('google.com')) {
                        cleanUrl = 'https://' + v;
                        collectedAttrs['_block_' + n + '_host'] = v;
                        break;
                    }
                }
                if (cleanUrl) break;
            }
        }
        // Fallback: first http link in block
        if (!primaryAnchor) {
            for (const link of allLinks) {
                const href = link.href || '';
                if (href.startsWith('http') && !href.includes('google.com/search')) {
                    primaryAnchor = link;
                    if (!googleClickUrl) googleClickUrl = href;
                    break;
                }
            }
        }

        if (!primaryAnchor) return null;
        const anchorId = stampAnchor(primaryAnchor);

        return {
            anchorId:         anchorId,
            title:            title,
            displayUrl:       displayUrl,
            googleClickUrl:   googleClickUrl,
            cleanFromDataRw:  cleanUrl,
            format:           format,   // 'text' | 'shopping_carousel' | 'pla_grid' | 'other'
            anchorHref:       primaryAnchor.href || '',
            // Debug: only populated when extraction is interesting --
            // shipped through to Python where no_domain drops are
            // logged WITH this dict so we can see what attrs Google
            // actually had on the element.
            debugAttrs:       collectedAttrs,
        };
    }

    // ── Method 1: Classic text ads via "Sponsored" / "Реклама" label
    // Walk up from the label text to the enclosing ad card. 8 levels of
    // parent because Google nests these deeply and the exact depth
    // varies by layout A/B.
    const textAdBlocks = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node;
    while (node = walker.nextNode()) {
        const text = (node.textContent || '').trim();
        if (text.length < 20 && SPONSORED_MARKERS.some(m => text === m || text.startsWith(m))) {
            let parent = node;
            for (let i = 0; i < 8 && parent; i++) {
                parent = parent.parentElement;
                if (!parent) break;
                const link = parent.querySelector('a[href]');
                if (link && link.href && link.href.startsWith('http')) {
                    textAdBlocks.add(parent);
                    break;
                }
            }
        }
    }
    // Also data-text-ad attribute (explicit marker)
    document.querySelectorAll('div[data-text-ad]').forEach(el => textAdBlocks.add(el));

    textAdBlocks.forEach(block => {
        const extracted = extractFromBlock(block, 'text');
        if (extracted) byAnchorId[extracted.anchorId] = extracted;
    });

    // ── Method 2: Shopping carousel ("Рекламируемые товары" row)
    // This is the critical format we were missing. Each card is its own
    // ad with its own pricing and clickable anchor. When Google surfaces
    // the user's OWN domain as the #1 shopping item (common for brand
    // queries), click_ad would blindly grab the first anchor it found
    // and click onto our own site. Now we parse each carousel card
    // separately, mark each with an anchor_id, and per-card own-domain
    // filtering happens in Python.
    const shoppingCards = new Set();
    // Multiple selectors — Google A/Bs these layouts constantly. Any
    // element with these data-docid / class combos that sits under a
    // "Sponsored" / "Реклама" container counts as a shopping ad card.
    document.querySelectorAll(
        '.pla-unit, .pla-unit-container .pla-unit, ' +
        '.mnr-c.pla-unit, g-inner-card.mnr-c, ' +
        'div[data-docid][data-pla], [data-hveid][data-docid] a[href*="aclk"], ' +
        'div.KZmu8e, div.cu-container div[data-docid]'
    ).forEach(el => {
        // Walk up to the card container (element that contains a
        // heading + price + link). Cap at 5 levels.
        let p = el;
        for (let i = 0; i < 5 && p; i++) {
            if (p.querySelector('a[href*="aclk"]')) {
                shoppingCards.add(p);
                break;
            }
            p = p.parentElement;
        }
    });
    shoppingCards.forEach(card => {
        const extracted = extractFromBlock(card, 'shopping_carousel');
        if (extracted) byAnchorId[extracted.anchorId] = extracted;
    });

    // ── Method 3: PLA (Product Listing Ad) grids below-fold
    // Separate from the carousel — this is a full grid that sometimes
    // appears on commercial queries. Same per-card approach.
    document.querySelectorAll(
        'div.commercial-unit-desktop-top, ' +
        'div.commercial-unit-desktop-rhs, ' +
        '.cu-container.cu-container-unit'
    ).forEach(container => {
        container.querySelectorAll('div[data-docid], .pla-unit').forEach(card => {
            if (card.querySelector('a[href*="aclk"]')) {
                const extracted = extractFromBlock(card, 'pla_grid');
                if (extracted && !byAnchorId[extracted.anchorId]) {
                    byAnchorId[extracted.anchorId] = extracted;
                }
            }
        });
    });

    // Return flat list, preserving insertion order (text → shopping → pla)
    return Object.values(byAnchorId);
    """

    try:
        raw_ads = driver.execute_script(js_script) or []
    except Exception as e:
        logging.warning(f"  JS parse error: {e}")
        return []

    # ── DIAGNOSTIC: per-scan summary (rate-limited) ───────────
    # The polling loop in search_query() calls parse_ads() ~10 times
    # per refresh-attempt (300ms intervals across a 3s window). Each
    # call previously emitted full INFO funnel + diagnostic, producing
    # 30-40 identical lines per query. We dedupe via a per-process
    # signature cache: a scan is logged at INFO only when its result
    # CHANGES (new candidate, different drop counts, different
    # diagnostic verdict). Identical repeated scans go to DEBUG so
    # users still get the data when troubleshooting with a verbose
    # log level, but routine runs stay readable.
    fmt_counts = {"text": 0, "shopping_carousel": 0, "pla_grid": 0, "other": 0}
    for r in raw_ads:
        f = r.get("format", "other")
        fmt_counts[f] = fmt_counts.get(f, 0) + 1

    if raw_ads:
        # D2: non-empty SERP — clear the silent-demotion scratchpad so
        # the loop doesn't carry stale diag forward. Note this fires
        # on RAW candidates (before own-domain filter); a SERP that
        # had only own-domain candidates (all dropped) is still a
        # "real" SERP from Google's POV, not a silent demotion.
        _LAST_SERP_DIAG = None
        bits = []
        for f in ("text", "shopping_carousel", "pla_grid"):
            if fmt_counts.get(f):
                bits.append(f"{fmt_counts[f]} {f}")
        msg = f"  parser: {len(raw_ads)} raw candidate(s) — " + ", ".join(bits or ["no breakdown"])
        sig = ("raw", len(raw_ads), tuple(sorted(fmt_counts.items())))
        if _parse_ads_should_log(sig):
            logging.info(msg)
        else:
            logging.debug(msg)
    else:
        try:
            diag = driver.execute_script(r"""
                const out = {};
                out.url        = location.href;
                out.title      = (document.title || '').slice(0, 80);
                out.organic    = document.querySelectorAll('div.g, div.MjjYud').length;
                out.knowledge  = !!document.querySelector('[data-attrid="kc:/common/topic"]');
                out.didyoumean = !!document.querySelector('a.gL9Hy, .o5rIVb');
                out.recaptcha  = !!document.querySelector('iframe[src*="recaptcha"], #captcha-form, div.g-recaptcha');
                out.results0   = (document.body.innerText || '').includes('did not match any documents') ||
                                 (document.body.innerText || '').includes('не дав збіги') ||
                                 (document.body.innerText || '').includes('по этому запросу ничего');
                out.spons_text = ['Sponsored','Реклама','Спонсоване'].filter(m =>
                    (document.body.innerText || '').includes(m)).join(',') || 'none';
                return out;
            """) or {}
            # D2: stash for the search loop's silent-demotion check.
            # Only meaningful on the 0-candidates path; non-empty
            # SERPs reset this scratchpad explicitly below.
            _LAST_SERP_DIAG = dict(diag) if isinstance(diag, dict) else None
            msg = (
                f"  parser: 0 candidates — organic={diag.get('organic')} "
                f"didyoumean={diag.get('didyoumean')} no_results={diag.get('results0')} "
                f"recaptcha={diag.get('recaptcha')} sponsored_text={diag.get('spons_text')}"
            )
            sig = ("zero", diag.get("organic"), bool(diag.get("didyoumean")),
                   bool(diag.get("results0")), bool(diag.get("recaptcha")),
                   diag.get("spons_text") or "")
            if _parse_ads_should_log(sig):
                logging.info(msg)
            else:
                logging.debug(msg)
            # Recaptcha is rare and important enough that we always
            # warn -- even if it's the third identical detection in a
            # row, the user wants to see it loudly.
            if diag.get('recaptcha'):
                logging.warning("  parser: ⚠ recaptcha iframe present on SERP — IP likely flagged")
        except Exception as e:
            logging.debug(f"  parser: SERP diagnostic JS failed: {e}")

    ads = []
    seen_domains = set()
    own_filtered_count = 0
    google_internal_count = 0
    no_domain_count = 0
    dup_count = 0

    for raw in raw_ads:
        try:
            google_click_url = raw.get("googleClickUrl", "")
            clean_from_rw    = raw.get("cleanFromDataRw", "")
            display_url      = raw.get("displayUrl", "")
            title            = raw.get("title", "")
            anchor_id        = raw.get("anchorId", "")
            ad_format        = raw.get("format", "text")
            anchor_href      = raw.get("anchorHref", "")

            # Clean URL priority: data-rw > parsed aclk > display > anchor href
            clean_url = ""
            if clean_from_rw and clean_from_rw.startswith("http"):
                clean_url = clean_from_rw
            elif google_click_url:
                parsed = extract_real_url(google_click_url)
                if (parsed and parsed.startswith("http") and
                        "google" not in (parsed.split("/")[2] if "/" in parsed[8:] else "")):
                    clean_url = parsed

            if not clean_url and display_url:
                du = display_url.strip()
                if du.startswith("http"):
                    clean_url = du
                elif "." in du and " " not in du:
                    first = du.split("›")[0].split("·")[0].split(" ")[0].strip()
                    if first and "." in first:
                        clean_url = "https://" + first

            # Last-resort: for shopping cards the displayUrl is often a
            # pretty label ("Oxydoc", "Ravita.Shop") and aclk URL hides
            # the real target. Use anchor_href as a fallback — sometimes
            # Google's own click URL contains &adurl=... which we pick
            # up via extract_real_url, but for shopping it may need the
            # raw anchor href.
            if not clean_url and anchor_href:
                d = extract_domain(anchor_href)
                if d and "google" not in d:
                    clean_url = anchor_href

            # Try harder for Shopping/PLA: parse display_url as merchant
            # name. Google formats these as "rozetka.ua", "АЛЛО",
            # "Comfy.ua · Магазин електроніки" etc. If display_url
            # looks like a host (contains a dot, no spaces, common TLD)
            # synthesise a domain from it.
            if not clean_url and display_url and ad_format in (
                "shopping_carousel", "pla_grid",
            ):
                # Strip everything after first space / · / › / |
                first_token = display_url.split()[0] if display_url else ""
                first_token = first_token.split("·")[0].split("›")[0].split("|")[0].strip()
                # Heuristic: looks like a hostname
                if (first_token and "." in first_token and
                    " " not in first_token and len(first_token) < 60 and
                    any(first_token.lower().endswith(tld) for tld in (
                        ".ua", ".com", ".org", ".net", ".biz", ".info",
                        ".com.ua", ".kiev.ua", ".io", ".store", ".shop",
                        ".pl", ".de", ".ru", ".uk", ".eu",
                    ))):
                    clean_url = "https://" + first_token.lower()
                    logging.debug(
                        f"  parser: synthesised URL from merchant text "
                        f"{first_token!r} for {ad_format}"
                    )

            domain = extract_domain(clean_url) or extract_domain(display_url)
            if not domain:
                no_domain_count += 1
                # Debug dump that actually helps. Includes the raw
                # data-* attrs the JS side scraped from the DOM, so we
                # can see exactly what Google handed us. Bump first 5
                # to INFO so the user sees the diagnostic in normal
                # logs without enabling DEBUG -- gives us a feedback
                # loop for finding new attribute names Google added.
                debug_attrs = raw.get("debugAttrs") or {}
                msg = (
                    f"  parser: dropped {ad_format} no-domain — "
                    f"display_url={display_url[:60]!r} "
                    f"aclk={(google_click_url or '')[:120]!r} "
                    f"attrs={debug_attrs}"
                )
                if no_domain_count <= 5:
                    logging.info(msg)
                else:
                    logging.debug(msg)
                continue

            # Filter Google internal domains
            if any(g in domain for g in ("google.com", "google.ua",
                                         "googleusercontent.com",
                                         "googlesyndication.com")):
                google_internal_count += 1
                logging.debug(f"  parser: candidate dropped — google internal domain {domain}")
                continue

            # ── OWN-DOMAIN FILTER — hard gate ──────────────────────
            # Own-domain ads NEVER get into the returned list. This is
            # the single source of truth for "is this ad ours". click_ad
            # then trusts this list and clicks ONLY ads that came
            # through this filter — no independent DOM scan.
            #
            # Audit footgun #1 fix: previously used substring `in`
            # which had false-positives — `MY_DOMAINS=["a.com"]`
            # blocked "a.com.evil" or "?ref=a.com" in URL. Use proper
            # subdomain semantics: exact match OR endswith "." + my.
            d_low = domain.lower()
            if any(d_low == my.lower() or d_low.endswith("." + my.lower())
                   for my in MY_DOMAINS):
                own_filtered_count += 1
                fmt_tag = f" [{ad_format}]" if ad_format != "text" else ""
                logging.info(f"  - [own]{fmt_tag} {domain} — {title[:50]}")
                continue

            # Dedup within query (same domain across text + shopping =
            # one record). Keep the first occurrence — usually that's
            # the highest-position text ad.
            if domain in seen_domains:
                dup_count += 1
                logging.debug(f"  parser: candidate dropped — duplicate domain {domain}")
                continue
            seen_domains.add(domain)

            ads.append({
                "query":             query,
                "title":             title,
                "display_url":       display_url,
                "clean_url":         clean_url,
                "google_click_url":  google_click_url,
                "domain":            domain,
                "found_at":          datetime.now().isoformat(timespec="seconds"),
                "is_target":         any(t in domain for t in TARGET_DOMAINS),
                "anchor_id":         anchor_id,   # used by click_ad
                "ad_format":         ad_format,   # 'text' / 'shopping_carousel' / 'pla_grid'
            })
            mark = "★" if ads[-1]["is_target"] else "·"
            fmt_tag = f" [{ad_format}]" if ad_format != "text" else ""
            logging.info(f"  {mark}{fmt_tag} {domain} — {title[:60]}")

        except Exception as e:
            logging.debug(f"  block processing error: {e}")

    # ── Funnel summary (rate-limited like the per-scan logs above) ─
    drops = own_filtered_count + google_internal_count + no_domain_count + dup_count
    if raw_ads or drops:
        msg = (
            f"  parser: kept {len(ads)} of {len(raw_ads)} candidates "
            f"(dropped: own={own_filtered_count} google_internal={google_internal_count} "
            f"no_domain={no_domain_count} dup={dup_count})"
        )
        sig = ("funnel", len(ads), len(raw_ads),
               own_filtered_count, google_internal_count,
               no_domain_count, dup_count)
        if _parse_ads_should_log(sig):
            logging.info(msg)
        else:
            logging.debug(msg)

    # ── D6: cross-border TLD warning ─────────────────────────────
    # If the profile's expected_country is set (e.g. Ukraine) but
    # ALL kept ads have foreign TLDs (e.g. only .nl/.de/.com — no
    # .ua), Google is showing this proxy international PLAs instead
    # of local inventory. That's the giveaway that the IP is
    # classified as datacenter / non-residential / out-of-geo. The
    # warning makes this visible in the funnel without the operator
    # having to dig through ipapi diagnostics.
    if ads:
        try:
            cb = _cross_border_tld_check(ads, EXPECTED_COUNTRY)
            if cb:
                logging.warning(f"  parser: ⚠ {cb}")
        except Exception as _e:
            logging.debug(f"  cross-border tld check skipped: {_e}")

    return ads


# ──────────────────────────────────────────────────────────────
# D6: country → expected-TLD whitelist for cross-border detection
# ──────────────────────────────────────────────────────────────
# Map of expected-country → TLDs that count as "local" inventory.
# We treat .com/.org/.net/.eu as country-neutral so they DON'T fire
# the warning by themselves (most multi-country brands use .com).
# The warning fires only when EVERY kept ad has a non-local TLD that
# is also country-coded (e.g. .nl/.de/.fr) — that's the unambiguous
# "Google gave us another country's inventory" signal.
_COUNTRY_LOCAL_TLDS = {
    "ukraine":         (".ua", ".com.ua", ".kiev.ua", ".dp.ua",
                        ".lviv.ua", ".kh.ua", ".odessa.ua"),
    "poland":          (".pl", ".com.pl"),
    "germany":         (".de",),
    "netherlands":     (".nl",),
    "france":          (".fr",),
    "spain":           (".es",),
    "italy":           (".it",),
    "united kingdom":  (".uk", ".co.uk"),
    "united states":   (".us",),
    "russia":          (".ru", ".рф"),
    "belarus":         (".by",),
    "kazakhstan":      (".kz",),
}
# Country-neutral TLDs — presence of any of these in the kept ads
# means we shouldn't fire the warning (the inventory is plausibly
# local even if no ccTLD matches).
_COUNTRY_NEUTRAL_TLDS = (
    ".com", ".org", ".net", ".info", ".biz", ".eu", ".io",
    ".store", ".shop", ".online", ".site",
)


def _cross_border_tld_check(ads: list[dict], expected_country: str) -> str | None:
    """Return a warning message if ALL kept ads have non-local
    country-coded TLDs while expected_country has a known whitelist.
    Returns None if everything looks fine, the country is unknown,
    or any neutral/local TLD is present.
    """
    if not ads or not expected_country:
        return None
    locals_ = _COUNTRY_LOCAL_TLDS.get(expected_country.lower())
    if not locals_:
        return None  # unknown country mapping — silently skip

    foreign_ccTLDs = []   # list of (domain, foreign_tld) for the warning
    has_neutral_or_local = False
    for ad in ads:
        d = (ad.get("domain") or "").lower().rstrip(".")
        if not d:
            continue
        # Match local first (longest match wins via `endswith`).
        if any(d.endswith(t) for t in locals_):
            has_neutral_or_local = True
            break
        # Foreign country-coded? Check all OTHER countries first —
        # if a foreign ccTLD matches, that wins over a neutral suffix
        # check (e.g. "praxisdienst.com" doesn't match a foreign
        # ccTLD but should be treated as neutral; "merkala.nl" must
        # be classified as foreign-NL even though it ends in a TLD).
        matched_foreign = False
        for cc, tlds in _COUNTRY_LOCAL_TLDS.items():
            if cc == expected_country.lower():
                continue
            for t in tlds:
                if d.endswith(t):
                    foreign_ccTLDs.append((d, t))
                    matched_foreign = True
                    break
            if matched_foreign:
                break
        if matched_foreign:
            continue
        # Country-neutral TLD? Treat as plausibly local.
        if any(d.endswith(t) for t in _COUNTRY_NEUTRAL_TLDS):
            has_neutral_or_local = True
            break
    if has_neutral_or_local:
        return None
    if not foreign_ccTLDs:
        return None
    # All kept ads are foreign-ccTLD — fire the warning.
    sample = ", ".join(f"{d}({t})" for d, t in foreign_ccTLDs[:3])
    more = f" (+{len(foreign_ccTLDs) - 3} more)" if len(foreign_ccTLDs) > 3 else ""
    return (
        f"cross-border SERP: expected {expected_country} but ALL "
        f"{len(foreign_ccTLDs)} kept ad(s) have foreign ccTLDs — "
        f"{sample}{more}. Likely datacenter / non-residential IP. "
        f"Check proxy provider."
    )


# ── Per-scan log dedupe ──────────────────────────────────────
# parse_ads() is called from a 300ms polling loop -- without rate
# limiting we get 30-40 identical lines per query. Cache the most
# recent log signature so we suppress reruns of the same outcome.
# Cache cleared at the start of each search_query() (see
# search_query()'s top: _parse_ads_log_reset()) so a NEW query
# always re-emits its first scan at INFO regardless of what the
# previous query saw.
_LAST_PARSER_SIG: tuple | None = None
_PARSER_SIG_REPEAT_COUNT = 0


def _parse_ads_should_log(sig: tuple) -> bool:
    """Return True iff this scan's outcome differs from the previous
    one (or it's the first scan after a reset). Keeps INFO-level
    output proportional to *change*, not to poll frequency."""
    global _LAST_PARSER_SIG, _PARSER_SIG_REPEAT_COUNT
    if sig == _LAST_PARSER_SIG:
        _PARSER_SIG_REPEAT_COUNT += 1
        return False
    _LAST_PARSER_SIG = sig
    _PARSER_SIG_REPEAT_COUNT = 0
    return True


def _parse_ads_log_reset():
    """Forget the last-seen parser signature. Call this at the top
    of each search_query() iteration so a new query/refresh emits
    its first scan at INFO even if it happens to match the prior
    query's last scan."""
    global _LAST_PARSER_SIG, _PARSER_SIG_REPEAT_COUNT
    if _PARSER_SIG_REPEAT_COUNT > 0:
        # Tail summary on reset so users know identical scans were
        # silently happening -- without this, dedupe would hide the
        # fact that we were actively polling.
        logging.info(
            f"  parser: (above scan repeated {_PARSER_SIG_REPEAT_COUNT}x silently)"
        )
    _LAST_PARSER_SIG = None
    _PARSER_SIG_REPEAT_COUNT = 0


# ──────────────────────────────────────────────────────────────
# POST-AD ACTIONS — handled by action_runner module
# ──────────────────────────────────────────────────────────────

# Legacy execute_action() removed — all logic moved to action_runner.py
# which supports 17 action types with human-like mouse/scroll/typing.


def run_post_ad_pipeline(browser, ad: dict, query: str = None):
    """
    Run the configured action pipeline for one ad. Delegates to
    action_runner.run_pipeline which implements 17 human-like action
    types (click_ad with real mouse movement, read, type, hover,
    scroll, back, etc.).

    Pipeline is configured per-profile in the dashboard (Behavior /
    Actions pages). See action_runner.action_catalog() for the full
    list of action types and their params.

    The pipeline sees four common flags on every step:
      - probability         — chance this step runs (0..1)
      - skip_on_my_domain   — skip if ad.domain is in MY_DOMAINS
      - skip_on_target      — skip if ad.is_target (target domain)
      - only_on_target      — inverse: run ONLY for target-domain ads
      - only_on_my_domain   — inverse: run ONLY for my-domain ads

    Every step execution (ran / skipped / error) is logged to
    action_events — see db.action_event_add().

    Both competitor and target-domain ads flow through POST_AD_ACTIONS
    now. Differentiation is per-step via only_on_*/skip_on_* flags.
    (Legacy ON_TARGET_DOMAIN_ACTIONS is kept as a fallback for users
    who haven't opened the Scripts page to trigger auto-migration yet.)
    """
    from ghost_shell.actions.runner import run_pipeline

    pipeline = POST_AD_ACTIONS
    # Backwards-compat: if the user still has a populated legacy
    # on_target_domain_actions list AND the current ad is on a target
    # domain, run the legacy pipeline for it. This goes away once the
    # user visits Scripts (the dashboard auto-migrates on load).
    if ad.get("is_target") and ON_TARGET_DOMAIN_ACTIONS:
        pipeline = ON_TARGET_DOMAIN_ACTIONS

    if not pipeline:
        # Sentinel: write one skipped event per ad even when pipeline is
        # empty so the Competitors page truthfully reflects "we saw the
        # ad, but no action was configured" (otherwise actions_ran AND
        # actions_skipped both stay 0 and the user can't tell the
        # difference between "never ran" and "config missing").
        try:
            from ghost_shell.db.database import get_db
            ad_domain = (ad.get("domain") or "").lower().strip()
            if ad_domain:
                if ad.get("is_target"):
                    ad_class = "target"
                elif any((ad_domain == d.lower() or
                          ad_domain.endswith("." + d.lower()))
                         for d in MY_DOMAINS):
                    ad_class = "my_domain"
                else:
                    ad_class = "competitor"
                get_db().action_event_add(
                    run_id=RUN_ID, profile_name=PROFILE_NAME,
                    query=query or "", ad_domain=ad_domain,
                    ad_class=ad_class,
                    action_type="(no_pipeline)",
                    outcome="skipped",
                    skip_reason="empty_pipeline",
                )
        except Exception as e:
            logging.warning(f"  action_event_add (empty pipeline): {e}")
        return

    run_pipeline(browser, pipeline, context={
        "ad":           ad,
        "my_domains":   MY_DOMAINS,
        "run_id":       RUN_ID,
        "profile_name": PROFILE_NAME,
        "query":        query,
    })


# ──────────────────────────────────────────────────────────────
# SEARCH LOOP
# ──────────────────────────────────────────────────────────────

def search_query(browser, query: str, sqm: SessionQualityMonitor,
                 current_ip: str = None, watchdog=None,
                 defer_serp_behavior: bool = False) -> list[dict]:
    """
    Single query execution:
    - Open direct search URL (eager page load — returns at DOMContentLoaded)
    - Try to parse ads immediately; poll briefly if SERP still rendering
    - Refresh-loop until ads appear (or max attempts)
    - Parse & return ads

    `defer_serp_behavior=True` (set by callers that will run a click
    pipeline on the returned ads) skips the post-parse SERP behavior
    block — scroll/dwell/organic-click. Without this, scrolling the
    SERP after parse_ads re-renders Google's PLA carousel and wipes
    the data-gs-ad-id stamps we just placed, breaking click_ad
    anchor lookup. Caller should run post_ads_behavior() ITSELF
    after the click pipeline finishes (see Fix A April 2026).

    Why this is fast now:
      • page_load_strategy='eager' cuts 5-10s by not waiting for tracker
        pixels, gstatic chunks, iframe ads-preview, favicon, etc.
      • We poll for ads every 300ms up to 3s instead of WebDriverWait(10)
        + _sleep("serp_settle") which added 3-4s even on a fast SERP.
      • As soon as ads are found, we call window.stop() to kill any
        still-pending subresources — the next query's driver.get() won't
        be blocked by those.

    The `watchdog` arg, if provided, gets a heartbeat() call inside the
    polling loop + after each refresh so a slow-but-alive fetch doesn't
    trip the stall detector.
    """
    driver = browser.driver
    url = build_search_url(query)
    logging.info(f"🔎 {query}  →  {url}")

    def _beat():
        if watchdog is not None:
            try: watchdog.heartbeat()
            except Exception: pass

    # Hard cap navigation: if Google's slow, we'd rather abort + retry
    # than hang for a minute. 15s is generous — a working SERP returns
    # HTML in under 2s through a Ukrainian residential proxy.
    try:
        driver.set_page_load_timeout(15)
    except Exception:
        pass

    try:
        browser.stealth_get(url, referer="https://www.google.com/")
    except Exception as e:
        # Timeout / network error — caller's refresh loop handles retry
        err = type(e).__name__
        logging.warning(f"  navigation {err}: {str(e)[:80]}")

        # If the exception was actually a browser death (InvalidSessionId,
        # etc.), the refresh loop can't do anything — driver is gone.
        # Try to resurrect once before returning. Session (cookies,
        # localStorage) is preserved on disk so the resurrected browser
        # picks up where it left off.
        if _browser_dead(e) or not browser.is_alive():
            if _try_recover_browser(browser, reason=f"navigation {err}"):
                driver = browser.driver   # rebind — old reference is stale
                try:
                    driver.set_page_load_timeout(15)
                    browser.stealth_get(url, referer="https://www.google.com/")
                except Exception as e2:
                    logging.warning(f"  navigation after recovery also failed: {e2}")
                    return []
            else:
                return []
        else:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            # Still continue — DOM might be usable even after a timeout

    _beat()

    # Captcha recovery attempts — on first sighting of a captcha, we try
    # to force-rotate the IP and reload, up to this many times, before
    # giving up. Tuned low: each rotation costs 5-15s of wait + provider
    # API call, and if 3 rotations in a row all land on flagged IPs the
    # whole proxy pool is probably burned.
    CAPTCHA_ROTATE_MAX = 3
    captcha_rotations_used = 0

    for attempt in range(1, REFRESH_MAX_ATTEMPTS + 1):
        # Reset the parser-log dedupe cache at the top of every
        # attempt so the first scan of a fresh SERP load always emits
        # at INFO. Without this, identical SERPs across attempts would
        # be silenced -- but each refresh IS a new event the user
        # wants to see logged once.
        _parse_ads_log_reset()
        if is_offline_page(driver):
            logging.error("  ✗ offline page — proxy broken")
            return []

        # Captcha path — if rotation is configured, try it up to N times
        # with a reload between each try. If rotation is unavailable
        # (disabled in settings, or no API URL configured), fall through
        # to the "burn IP and give up" branch immediately — retrying with
        # the same IP just makes Google's flag heavier.
        if is_captcha_page(driver):
            # Single check: is rotation actually usable for this run?
            # IS_ROTATING_PROXY is the resolved flag from _resolve_rotation()
            # that already took per-profile overrides into account.
            rotation_available = bool(IS_ROTATING_PROXY and ROTATION_API_URL)

            if rotation_available and captcha_rotations_used < CAPTCHA_ROTATE_MAX:
                captcha_rotations_used += 1
                logging.warning(
                    f"  ⚠ CAPTCHA — force-rotating IP "
                    f"(attempt {captcha_rotations_used}/{CAPTCHA_ROTATE_MAX})"
                )
                sqm.record("captcha_rotated", query=query, url=driver.current_url)
                new_ip = browser.force_rotate_ip()
                if new_ip and new_ip != current_ip:
                    logging.info(f"  → rotated {current_ip} → {new_ip}, warming up")
                    current_ip = new_ip
                    # Recovery #3 — DON'T immediately retry Google with
                    # the new IP. Real users don't switch ISPs and
                    # immediately fire a search query — that's the
                    # exact pattern Google flags as suspicious. Visit
                    # 2-3 neutral domains first so the new IP picks up
                    # plausible browsing context (referer chain,
                    # cookies, request volume) before we go back to
                    # the SERP.
                    # Audit #105 #6: warmup failure was previously
                    # logged at DEBUG and the run continued straight
                    # to the SERP — exactly the "cold IP + no
                    # browsing context" pattern Recovery #3 was
                    # designed to prevent. Bump to WARNING so the
                    # operator sees it, AND fall back to a single
                    # lightweight HTTPS visit if the multi-site
                    # warmup pool failed entirely (geo-blocked / all
                    # sites down for this exit IP).
                    _warmup_visited = False
                    try:
                        _warmup_visited = bool(
                            _post_rotation_warmup(driver, browser=browser)
                        )
                    except Exception as e:
                        logging.warning(
                            f"  ⚠ post-rotation warmup raised "
                            f"({type(e).__name__}: {e}) — Recovery #3 "
                            f"degraded; cold-IP signal possible on "
                            f"next request"
                        )
                    if not _warmup_visited:
                        try:
                            # Last-ditch fallback: visit www.google.com
                            # itself (HTTPS, lightweight homepage,
                            # already in our cookie jar). At least
                            # primes one TCP connection + HTTP cookie
                            # round-trip on the new IP before the
                            # SERP query lands.
                            driver.set_page_load_timeout(8)
                            driver.get("https://www.google.com/robots.txt")
                            time.sleep(random.uniform(1.0, 2.5))
                            logging.info(
                                f"  Recovery #3 fallback: visited "
                                f"google.com/robots.txt to warm new IP"
                            )
                        except Exception as _wf_err:
                            logging.warning(
                                f"  ⚠ warmup fallback also failed "
                                f"({type(_wf_err).__name__}); "
                                f"proceeding with cold IP"
                            )
                    try:
                        driver.get(search_url)
                        time.sleep(random.uniform(1.5, 3.0))
                    except Exception as e:
                        logging.warning(f"  reload after rotate failed: {e}")
                    continue   # re-enter for-loop with fresh IP
                elif new_ip == current_ip:
                    # API responded, pool is too small / sticky — try again
                    # next iteration (the provider might serve a different
                    # exit on the next rotation request).
                    logging.warning(
                        f"  ⚠ rotation returned SAME IP ({new_ip}) — "
                        f"provider pool may be sticky"
                    )
                    time.sleep(random.uniform(2, 5))
                    continue
                else:
                    # new_ip is None → rotation API failed entirely
                    logging.error(
                        f"  ✗ rotation API call failed — check "
                        f"Dashboard → Proxy → Rotation settings"
                    )
                    # Fall through to final-give-up below

            # Final give-up path. Reached when:
            #   - rotation not configured at all, OR
            #   - tried CAPTCHA_ROTATE_MAX rotations without success
            if not rotation_available:
                logging.error(
                    f"  ✗ CAPTCHA — rotation NOT configured, cannot recover. "
                    f"Enable rotation in Dashboard → Proxy to auto-retry on captchas."
                )
            else:
                # ── Last-resort escalation: full session restart ─────
                # The 3 in-tick rotations may have all returned the same
                # IP because asocks (and similar pools) often pin a TCP
                # connection to one exit; rotation API only swaps the
                # exit for *new* connections. By tearing the browser
                # down and bringing it back up, we force a brand-new
                # TCP connection through the local proxy forwarder, and
                # the rotation API call after that lands on a fresh
                # exit. This is the user-visible "captcha → close
                # browser → auto-rotate-IP → relaunch" flow.
                #
                # Gate behind config so users with budget proxies (where
                # restart spends a real $$ rotation) can disable.
                auto_restart = bool(CFG.get("captcha.auto_restart_session", True))
                if auto_restart and captcha_rotations_used >= CAPTCHA_ROTATE_MAX:
                    logging.warning(
                        f"  ⚠ CAPTCHA — {captcha_rotations_used} in-tick "
                        f"rotations didn't help. Escalating to FULL "
                        f"SESSION RESTART (close browser → rotate IP → "
                        f"reopen). Disable via captcha.auto_restart_session=false."
                    )
                    try:
                        # Stop traffic collector + flush sessions before
                        # the close so post-mortem state is consistent.
                        browser.restart()
                        # After restart, the local proxy forwarder is
                        # holding a fresh TCP connection. Fire the
                        # rotation API one more time -- this should land
                        # on a different exit IP from the new connection.
                        post_restart_ip = browser.force_rotate_ip()
                        if post_restart_ip and post_restart_ip != current_ip:
                            logging.info(
                                f"  ✓ session-restart rotated "
                                f"{current_ip} → {post_restart_ip}, "
                                f"warming up before SERP retry"
                            )
                            current_ip = post_restart_ip
                            # Recovery #3 — warmup also after the full
                            # restart path. Doubly important here since
                            # we just nuked all session context.
                            try:
                                _post_rotation_warmup(
                                    browser.driver, browser=browser)
                            except Exception as e:
                                logging.debug(
                                    f"  warmup after restart: {e}")
                            try:
                                browser.driver.get(search_url)
                                time.sleep(random.uniform(2.0, 4.0))
                                # Loop back -- the next iteration of the
                                # outer for-loop will re-check captcha
                                # state and continue the funnel cleanly.
                                continue
                            except Exception as e:
                                logging.warning(
                                    f"  session-restart: SERP reload "
                                    f"failed: {e} -- falling through to "
                                    f"give-up"
                                )
                        else:
                            logging.warning(
                                f"  session-restart: same IP "
                                f"({post_restart_ip}) -- pool genuinely "
                                f"sticky, giving up"
                            )
                    except Exception as e:
                        logging.error(
                            f"  session-restart failed: {e} -- giving up"
                        )

                logging.error(
                    f"  ✗ CAPTCHA — tried {captcha_rotations_used} rotations"
                    + (" + 1 session restart" if auto_restart else "") +
                    f", Google still flags us. Burning this IP."
                )
                # Recovery #4 — exhausted IP rotation + session restart
                # both didn't recover. The signal Google's flagging is
                # almost certainly NOT just the IP at this point — it's
                # the fingerprint (UA / canvas / JA3 / WebGL combo).
                # Regenerate the fingerprint so the NEXT scheduled run
                # comes back as a different identity. This doesn't
                # rescue the current run (we already burned 3 rotations
                # on it) but it stops the same FP getting hammered on
                # every retry. Gated by config so users on tight FP
                # budgets can opt out.
                if bool(CFG.get("captcha.auto_regenerate_fp", True)):
                    try:
                        _auto_regenerate_fingerprint(
                            PROFILE_NAME,
                            reason=(
                                f"persistent captcha after "
                                f"{captcha_rotations_used} rotations"
                                + (" + restart" if auto_restart else "")
                            ),
                        )
                    except Exception as e:
                        logging.warning(
                            f"  fp-regen failed (non-fatal): {e}"
                        )
            sqm.record("captcha", query=query, url=driver.current_url)
            RUN_COUNTERS["total_captchas"] += 1
            if current_ip:
                browser.report_rotating(current_ip, success=False, captcha=True)
            return []

        if is_ads_preview_page(driver):
            logging.warning("  ✗ Ads Preview page (unexpected) — skipping")
            return []

        # Fast polling loop: on a healthy SERP, ads are parseable within
        # ~500ms of DOMContentLoaded. Poll every 300ms up to 3s, bail
        # immediately when we find any.
        ads = []
        poll_deadline = time.time() + 3.0
        poll_interval = 0.3
        beat_every    = 3     # every Nth iteration = ~900ms apart
        i = 0
        while time.time() < poll_deadline:
            try:
                ads = parse_ads(driver, query)
            except Exception:
                ads = []
            if ads:
                break
            # Also check for a captcha flash that appeared async
            if is_captcha_page(driver):
                logging.error("  ✗ CAPTCHA appeared during poll")
                sqm.record("captcha", query=query, url=driver.current_url)
                RUN_COUNTERS["total_captchas"] += 1
                if current_ip:
                    browser.report_rotating(current_ip, success=False, captcha=True)
                return []
            if i % beat_every == 0:
                _beat()
            i += 1
            time.sleep(poll_interval)

        if ads:
            # Stop any still-running subresources — gstatic, trackers,
            # iframes — so the next query doesn't have to queue behind them.
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            logging.info(f"  ✓ {len(ads)} ads on attempt {attempt}")

            # ── SERP behavior — make us look like a real user who read
            # the results before moving on. Without this, Google sees a
            # scrape pattern (land → grab → leave in 1-2s) and downgrades
            # ad load on the next query. Configurable via Settings; all
            # parts are independently toggleable, exceptions swallowed.
            #
            # Pass MY_DOMAINS (only) as exclude list so the organic-click
            # step never visits our own site. We do NOT include
            # TARGET_DOMAINS here — those are the domains the operator
            # is researching (competitors, tracked sites). Excluding
            # them would DEFEAT the research purpose: organic-click
            # on a target is exactly the high-signal engagement we
            # want to occasionally generate for them.
            # Fix A (Apr 2026): when callers will run a click pipeline
            # on these ads (foreach_ad → click_ad), do NOT scroll the
            # SERP yet. scroll_through_serp re-renders Google's PLA
            # carousels, replacing the <a> elements parse_ads just
            # stamped with data-gs-ad-id. The result was 7/7 click_ad
            # failures with "couldn't locate ad anchor" 30s after
            # parse. The caller is responsible for invoking
            # post_ads_behavior AFTER its click pipeline finishes (or
            # not at all, if the unified-flow script has its own
            # human-shaped behavior steps).
            if not defer_serp_behavior:
                try:
                    from ghost_shell.browser.serp_behavior import post_ads_behavior
                    post_ads_behavior(
                        driver, DB,
                        exclude_domains=list(MY_DOMAINS or []),
                        watchdog=watchdog,
                    )
                except Exception as e:
                    logging.debug(f"  SERP behavior failed: {e}")

            return ads

        if attempt >= REFRESH_MAX_ATTEMPTS:
            # More informative tail than the old "no ads after N
            # attempts" — say which interpretation we lean toward so
            # the user knows what to investigate next. parse_ads
            # already emitted per-scan funnel logs above; this is the
            # summary verdict for the query overall.
            logging.info(
                f"  ✗ no ads after {attempt} attempts — likely "
                f"genuine 0 ads or soft-block. Check parser funnel "
                f"lines above; if every scan said '0 candidates' the "
                f"query has no ads from this IP. If candidates were "
                f"found but all dropped as 'own', that means Google "
                f"only showed the user's own ads — also a 0-result "
                f"outcome but a different cause."
            )
            return []

        wait_sec = random.uniform(REFRESH_MIN_SEC, REFRESH_MAX_SEC)
        logging.info(f"  ↻ attempt {attempt}/{REFRESH_MAX_ATTEMPTS} — refresh in {wait_sec:.0f}s")
        # During this pre-refresh wait, heartbeat every ~5s so the
        # watchdog doesn't flag us for stalling.
        slept = 0
        while slept < wait_sec:
            chunk = min(5, wait_sec - slept)
            time.sleep(chunk)
            slept += chunk
            _beat()

        # Before we attempt refresh — is the browser still alive?
        if not browser.is_alive():
            # Don't just give up — try to recover once. Mid-run crashes
            # are usually recoverable because profile state (cookies,
            # localStorage) lives on disk. The in-memory driver object
            # is what needs rebuilding.
            if not _try_recover_browser(browser, reason="is_alive=False before refresh"):
                logging.warning("  browser died during wait and recovery failed — aborting query")
                return []
            driver = browser.driver   # rebind — old ref is stale after restart

        try:
            driver.refresh()
        except Exception as e:
            err = type(e).__name__
            logging.warning(f"  refresh {err}: {str(e)[:80]}")
            # Same pattern — if this was actually a dead browser, try
            # to bring it back before aborting.
            if _browser_dead(e) or not browser.is_alive():
                if not _try_recover_browser(browser, reason=f"refresh {err}"):
                    return []
                driver = browser.driver
                continue
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            continue

        _beat()

    return []


# ──────────────────────────────────────────────────────────────
# STORAGE
# ──────────────────────────────────────────────────────────────

def save_ads(ads: list[dict]):
    """Save ads to DB + duplicate file"""
    if not ads:
        return

    for ad in ads:
        try:
            DB.competitor_add(
                run_id           = RUN_ID,
                query            = ad["query"],
                domain           = ad["domain"],
                title            = ad.get("title"),
                display_url      = ad.get("display_url"),
                clean_url        = ad.get("clean_url"),
                google_click_url = ad.get("google_click_url"),
            )
        except Exception as e:
            logging.warning(f"  DB competitor_add: {e}")

    # Also append to flat file for easy grep
    try:
        with open(COMPETITOR_URLS_FILE, "a", encoding="utf-8") as f:
            for ad in ads:
                google_url = ad.get("google_click_url", "")
                if not google_url:
                    continue
                line = "\t".join([ad["found_at"], ad["query"], ad["domain"], google_url])
                f.write(line + "\n")
    except Exception as e:
        logging.debug(f"  file write error: {e}")

    logging.info(f"  💾 saved {len(ads)} ads (DB + {COMPETITOR_URLS_FILE})")


def print_summary(all_ads: list[dict]):
    """Print final report grouped by domain"""
    if not all_ads:
        logging.info("\n=== No competitors found ===")
        return

    from collections import defaultdict
    by_domain = defaultdict(lambda: {"count": 0, "queries": set(), "title": ""})
    for ad in all_ads:
        d = by_domain[ad["domain"]]
        d["count"] += 1
        d["queries"].add(ad["query"])
        if not d["title"]:
            d["title"] = ad["title"]

    logging.info("")
    logging.info("=" * 68)
    logging.info(f" COMPETITOR REPORT — {len(by_domain)} unique advertisers ".center(68))
    logging.info("=" * 68)

    ranked = sorted(by_domain.items(), key=lambda x: -x[1]["count"])
    for i, (domain, info) in enumerate(ranked, 1):
        is_target = any(t in domain for t in TARGET_DOMAINS)
        mark = "★" if is_target else " "
        logging.info(f"[{i}] {mark} {domain}")
        logging.info(f"     {info['title'][:60]}")
        logging.info(f"     queries: {', '.join(sorted(info['queries']))}")
        logging.info("")


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

# Per-run counters — incremented during run_monitor(), read by the
# `finally:` block at the end of __main__ for the run-summary banner.
#
# Why we can't use DB.events_summary() like the old code did:
#   1. events_summary(hours=24) aggregates the last 24h, not this run.
#      On a profile that did 5 runs today, the summary would show the
#      sum of ALL those runs every time — Run #5 would claim Run #1-4's
#      captchas.
#   2. sqm.record() silently swallows errors (encoding issues, locked
#      sqlite, etc.). When that happens we get the right Chrome
#      behaviour but no event row → summary shows 0 even though the
#      run actually found ads.
#   3. "search_ok" counts SUCCESSFUL QUERIES, not ads — old code was
#      mapping `total_ads = summary["search_ok"]` which was just wrong.
#
# These counters live in process memory so they're reset per run (each
# run is its own python process). Incremented next to the point where
# the corresponding sqm.record() call sits, so if sqm ever starts
# working for real the numbers stay aligned.
RUN_COUNTERS = {
    "total_ads":      0,   # Actual ad count across all queries
    "total_queries":  0,   # Successful queries (ads > 0)
    "total_empty":    0,   # Queries that returned no ads
    "total_captchas": 0,   # Captcha hits (any recovery action)
    # Audit D2 (Apr 2026): silent demotion = SERP looks healthy
    # (organic results present, no recaptcha, not a "did you mean"
    # spelling rescue) but parser found 0 ads. Different from
    # genuine "no advertisers in this market" because we can't tell
    # them apart from a single sample — only the streak gives it away.
    # When this counter hits SILENT_DEMOTION_BURN_THRESHOLD inside
    # one run, we synthesize a captcha-equivalent event so Recovery #4
    # (FP regen after N captcha) and the burn-detection logic can
    # react. Resets to 0 on the first non-empty SERP.
    "consecutive_silent_demotions":  0,
    "total_silent_demotions":        0,
}

# Module-level scratchpad: parse_ads stashes its zero-ads diag dict
# here (organic count, recaptcha state, etc.) so the search loop can
# decide whether the empty SERP was a silent demotion or a genuine
# "no advertisers" outcome. Filled only on the 0-candidates path,
# cleared on any non-empty SERP. Safe in single-process main.py.
_LAST_SERP_DIAG: dict | None = None

# Threshold for silent-demotion burn synthesis. 3 consecutive empties
# with healthy organic feels right for the GoodMedika case (every
# search returned 0 ads despite full SERP). Keep low because waiting
# 5+ runs to react means 5+ wasted queries on a flagged IP.
SILENT_DEMOTION_BURN_THRESHOLD = 3


def run_monitor():
    all_ads: list[dict] = []

    # ── Startup summary — makes it obvious what config this run is using.
    # Biggest pain point was users couldn't tell if rotation was
    # configured until something broke mid-run. Now it's on screen
    # immediately, every run.
    logging.info("═" * 62)
    logging.info(f" GHOST SHELL — profile='{PROFILE_NAME or 'default'}'")
    logging.info(f"   Proxy             : {PROXY or '(none — direct connection)'}")
    if IS_ROTATING_PROXY and ROTATION_API_URL:
        # Show only the path, not full URL — API keys sometimes end up in
        # query strings and we don't want them in user-visible logs.
        from urllib.parse import urlparse
        u = urlparse(ROTATION_API_URL)
        masked = f"{u.scheme}://{u.netloc}{u.path}"
        logging.info(f"   Rotation API      : {masked} (provider={_ROT_PROVIDER})")
        logging.info(f"   Rotation trigger  : on captcha (up to 3×), on burn-detection, "
                     f"on geo-mismatch")
    elif IS_ROTATING_PROXY:
        logging.warning(
            "   Rotation API      : ENABLED but NO URL SET — nothing will "
            "actually rotate. Dashboard → Proxy → Rotation."
        )
    else:
        logging.info(f"   Rotation API      : disabled (proxy.is_rotating=false)")
    logging.info(f"   Preferred lang    : {PREFERRED_LANGUAGE}")
    logging.info(f"   Expected country  : {EXPECTED_COUNTRY} ({EXPECTED_TIMEZONE})")

    # ── Coherence-system fingerprint consumption (Phase 2 bridge) ───
    # Read the profile's current fingerprint from the DB, log its score,
    # and let its `language` field override PREFERRED_LANGUAGE so the
    # dashboard's per-profile FP edits actually influence the runtime
    # browser. Deeper integration (UA / screen / timezone / fonts) still
    # flows through DeviceTemplateBuilder — that swap is tracked as a
    # separate follow-up because DeviceTemplateBuilder is load-bearing
    # for stealth and changing it mid-session is risky.
    #
    # Fails silently: if anything about the FP system is unavailable we
    # fall back to the old behaviour so runs never abort because of this.
    effective_language = PREFERRED_LANGUAGE
    _fp_meta = None
    try:
        _fp_meta = DB.fingerprint_current(PROFILE_NAME)
    except Exception as _e:
        logging.debug(f"[fingerprint] DB read failed: {_e}")
    if _fp_meta:
        _payload = _fp_meta.get("payload") or {}
        _score   = _fp_meta.get("coherence_score")
        _tmpl    = _fp_meta.get("template_name") or _fp_meta.get("template_id")
        _lang    = _payload.get("language")
        _grade = (_fp_meta.get("coherence_report") or {}).get("grade", "?")
        logging.info(
            f"   Fingerprint       : score={_score}/100 grade={_grade} "
            f"template={_tmpl!r} source={_fp_meta.get('source')}"
        )
        if _score is not None and _score < 55:
            logging.warning(
                f"   ⚠ Fingerprint coherence is CRITICAL ({_score}/100). "
                f"Dashboard → Fingerprint → Regenerate before next run."
            )
        if _lang and _lang != PREFERRED_LANGUAGE:
            logging.info(
                f"   Language override : {PREFERRED_LANGUAGE} → {_lang} "
                f"(from fingerprint.language)"
            )
            effective_language = _lang
    else:
        logging.info("   Fingerprint       : not generated yet "
                     "(DeviceTemplateBuilder will build one for this run)")
    logging.info("═" * 62)

    with GhostShellBrowser(
        profile_name       = PROFILE_NAME,
        proxy_str          = PROXY,
        auto_session       = CFG.get("browser.auto_session", True),
        is_rotating_proxy  = IS_ROTATING_PROXY,
        rotation_api_url   = ROTATION_API_URL,
        enrich_on_create   = CFG.get("browser.enrich_on_create", True),
        preferred_language = effective_language,
        run_id             = RUN_ID,
    ) as browser:

        driver = browser.driver
        browser.setup_profile_logging()

        # 0. Auto-rotate IP FIRST — before anything else hits the
        # network through the proxy. Previously rotate happened at
        # step 6 (after selfcheck + diagnostics), meaning the first
        # HTTPS requests (generate_204 for secure-context selfcheck,
        # ipapi for diagnostics) went through the OLD IP. If that IP
        # was already burned, we'd waste a captcha before learning it.
        # Now: rotate → everything after uses the fresh IP.
        #
        # Throttle: rotate_every_n_runs lets the user trade "fresh IP
        # every run" (looks like a new user — erases trust signal) for
        # "IP ripens over several runs" (looks like a returning user —
        # Google's trust model rewards this). Captcha-triggered rotation
        # still fires unconditionally regardless of this throttle.
        current_ip = None
        auto_rotate = CFG.get("proxy.auto_rotate_on_start", True)
        rotate_every = int(CFG.get("proxy.rotate_every_n_runs", 10) or 10)
        if rotate_every < 1:
            rotate_every = 1

        # Count includes the current run (run_start() already inserted
        # our row). So first run returns 1, which rotates (1 % N == 1).
        # Run 11 also rotates (11 % 10 == 1). Run 2-10 skip.
        try:
            profile_run_n = DB.runs_count_for_profile(PROFILE_NAME)
        except Exception:
            profile_run_n = 1

        should_rotate = auto_rotate and (
            rotate_every == 1 or profile_run_n % rotate_every == 1
        )

        # Override the throttle when the profile is health-burned. Without
        # this, a profile that hits 100% captcha rate sits on the same
        # dead IP for up to N-1 more runs, every step times out, and the
        # user watches the browser hang on the init page indefinitely.
        # Trigger the early rotation regardless of the rotate_every cadence.
        try:
            from ghost_shell.session.quality_monitor import SessionQualityMonitor as _SQM
            _sqm_early = _SQM(browser.user_data_path)
            _abort, _reason = _sqm_early.should_abort()
            if _abort and not should_rotate and IS_ROTATING_PROXY and auto_rotate:
                logging.warning(
                    f"  ⚠ profile burned ({_reason}) — overriding "
                    f"rotate-throttle to force rotation NOW"
                )
                should_rotate = True
        except Exception as _e:
            logging.debug(f"  burn-aware rotation override skipped: {_e}")

        if IS_ROTATING_PROXY:
            if should_rotate:
                try:
                    log_step(
                        "rotating IP",
                        f"run #{profile_run_n} for {PROFILE_NAME} "
                        f"(rotate_every_n_runs={rotate_every})"
                    )
                    new_ip = browser.force_rotate_ip()
                    if new_ip:
                        current_ip = new_ip
                        logging.info(f"🌐 rotated to IP: {current_ip}")
                except Exception as e:
                    logging.warning(f"  auto-rotate failed: {e}")
            elif auto_rotate:
                # Throttle skipped this run — log it so ops can see the
                # reasoning in history without digging through config.
                wait = rotate_every - (profile_run_n % rotate_every)
                logging.info(
                    f"⏸ skipping rotation — run #{profile_run_n} of "
                    f"{PROFILE_NAME}, next rotate in {wait} run(s) "
                    f"(rotate_every_n_runs={rotate_every})"
                )
            # Regardless of rotate decision: check health + unburn stale IPs
            try:
                if current_ip is None:
                    current_ip = browser.check_and_rotate_if_burned()
                    if current_ip:
                        logging.info(f"🌐 working with IP: {current_ip}")
            except Exception as e:
                logging.debug(f"  rotation check: {e}")

        # ── SELFCHECK + DIAGNOSTICS THROTTLING ─────────────────────
        # health_check() runs ~34 JS probes (1-2s each run).
        # ProxyDiagnostics.full_check() makes 1 ipapi request + WebRTC
        # probe. Running both EVERY run is overkill — fingerprint
        # doesn't change between runs of the same profile (it's
        # deterministic per-profile), and exit IP already gets checked
        # during rotation. For high-cadence schedules (run every 10min)
        # that's ~150 selfchecks/day wasting 3-5 min of cumulative time.
        #
        # Throttle: run selfcheck + diagnostics on run 1, then every
        # Nth run (configurable, default 10). Always run them after a
        # rotation since fingerprint + IP both might have changed.
        selfcheck_every = int(CFG.get("browser.selfcheck_every_n_runs", 10) or 10)
        if selfcheck_every < 1:
            selfcheck_every = 1

        # Force-flag for ad-hoc verification (e.g. after a C++ patch
        # rebuild — we want to see the selfcheck immediately, not wait
        # for the next run-modulo-10 hit). Two ways to trigger:
        #   • env GHOST_SHELL_FORCE_SELFCHECK=1   (one-shot, set in
        #     dashboard launch env or in PowerShell before manual run)
        #   • config browser.force_selfcheck_next=1 (DB-stored, gets
        #     consumed and reset by this same code path so it fires
        #     ONCE then auto-clears)
        # Both are checked here so either path works. The env wins —
        # explicit env flag should never be silently dropped.
        force_env  = os.environ.get("GHOST_SHELL_FORCE_SELFCHECK") in ("1", "true", "yes")
        force_db   = bool(CFG.get("browser.force_selfcheck_next", False))
        force_once = force_env or force_db
        if force_db:
            # Consume the DB flag so it's truly one-shot. If consumption
            # fails (db locked etc.) the worst case is one extra forced
            # selfcheck on the very next run — harmless.
            try:
                CFG.set("browser.force_selfcheck_next", False)
            except Exception:
                pass

        should_selfcheck = (
            force_once or                              # ad-hoc force
            should_rotate or                           # always after rotation
            profile_run_n == 1 or                      # always on first run
            profile_run_n % selfcheck_every == 1       # every N runs
        )
        if force_once:
            logging.info(
                f"  ⚡ selfcheck forced this run "
                f"(via {'env' if force_env else 'config'}); "
                f"normal cadence still every {selfcheck_every} runs"
            )

        # 1. Profile health sanity check
        # ────────────────────────────────────────────────────────
        # Audit D1 (Apr 2026): the previous behaviour ("proceeding
        # anyway — disable this check if too strict") meant that a
        # critically burned profile (e.g. 100% captcha rate over 24h)
        # kept hammering the same dead IP run after run, every search
        # returned 0 ads, and Google's bad-IP score for the proxy got
        # WORSE. The block is now hard:
        #
        #   • IS_ROTATING_PROXY=true  →  proceed. The burn-aware
        #     override above (line ~2172) already forced should_rotate
        #     this run, so by the time we get here we have a fresh IP
        #     and the burn diagnosis is from the OLD IP — safe to run.
        #
        #   • IS_ROTATING_PROXY=false (static proxy) →  ABORT. There
        #     is no rotation endpoint for the recovery loop to call,
        #     so each subsequent run will just re-detect the same
        #     burn. Set profiles.needs_attention=1 with a reason so
        #     the dashboard can render a banner and the scheduler can
        #     skip this profile until the user fixes the proxy or
        #     manually clears the flag.
        #
        #   • Override:  GHOST_SHELL_FORCE_BURNED_RUN=1 in env lets
        #     ops force a single run for diagnostics (e.g. "I just
        #     swapped the proxy, let me prove the new one works").
        #     The override does NOT clear needs_attention — only a
        #     subsequent healthy run does that.
        sqm = SessionQualityMonitor(browser.user_data_path)
        should_abort, reason = sqm.should_abort()
        force_burned_run = os.environ.get(
            "GHOST_SHELL_FORCE_BURNED_RUN"
        ) in ("1", "true", "yes")
        if should_abort:
            logging.warning(f"  ⚠ profile health: {reason}")
            if IS_ROTATING_PROXY:
                # The override at line ~2172 already forced rotation
                # for THIS run, so we now sit on a fresh exit IP.
                # The burn judgement was from the OLD IP — proceed.
                logging.info(
                    "    rotating proxy → already rotated to a fresh IP "
                    "above; proceeding (status will reset as healthy "
                    "runs accumulate)"
                )
                # Persist the diagnosis but don't block: needs_attention
                # stays 0 because we have a recovery path.
            elif force_burned_run:
                logging.warning(
                    "    GHOST_SHELL_FORCE_BURNED_RUN=1 → proceeding "
                    "despite static proxy + burned profile (one-shot "
                    "diagnostic override). Flag persists until next "
                    "healthy run."
                )
                try:
                    DB.profile_meta_upsert(
                        PROFILE_NAME,
                        needs_attention        = 1,
                        needs_attention_reason = (reason or "burned")[:240],
                        needs_attention_at     = datetime.now().isoformat(
                            timespec="seconds"
                        ),
                    )
                except Exception as _e:
                    logging.debug(f"  needs_attention persist skipped: {_e}")
            else:
                # Hard block: static proxy + burned + no override.
                blk_msg = (
                    f"profile burned and proxy is static (no rotation "
                    f"endpoint). Recovery loop has nothing it can do. "
                    f"Reason: {reason}. To force one diagnostic run set "
                    f"GHOST_SHELL_FORCE_BURNED_RUN=1 in env."
                )
                log_error_banner("RUN BLOCKED — needs attention", blk_msg)
                try:
                    DB.profile_meta_upsert(
                        PROFILE_NAME,
                        needs_attention        = 1,
                        needs_attention_reason = (reason or "burned")[:240],
                        needs_attention_at     = datetime.now().isoformat(
                            timespec="seconds"
                        ),
                    )
                except Exception as _e:
                    logging.debug(f"  needs_attention persist skipped: {_e}")
                # Exit code 75 = EX_TEMPFAIL (BSD sysexits convention,
                # widely used to signal "service unavailable, retry
                # later or fix config"). Scheduler already treats
                # non-zero as failure; UI will show needs_attention.
                sys.exit(75)
        else:
            # Healthy run — auto-clear any stale needs_attention flag.
            # We only do this when we ALREADY have data (not on first
            # run where total_in_log<3) so we don't clear flags before
            # the metrics catch up.
            try:
                _h = sqm.get_health()
                if _h.get("status") in ("healthy", "warning") and \
                   _h.get("total_in_log", 0) >= 3:
                    DB.profile_meta_upsert(
                        PROFILE_NAME,
                        needs_attention        = 0,
                        needs_attention_reason = None,
                        needs_attention_at     = None,
                    )
            except Exception as _e:
                logging.debug(f"  needs_attention clear skipped: {_e}")

        if should_selfcheck:
            # 2. Fingerprint self-check (writes to DB + profile file)
            browser.health_check(verbose=True)

            # 3. Proxy diagnostics with geo-mismatch detection.
            diag = ProxyDiagnostics(driver, proxy_url=PROXY)
            report = diag.full_check(
                expected_timezone = EXPECTED_TIMEZONE,
                expected_country  = EXPECTED_COUNTRY,
            )
            diag.print_report(report)
        else:
            wait = selfcheck_every - (profile_run_n % selfcheck_every)
            logging.info(
                f"⏸ skipping selfcheck + diagnostics — run #{profile_run_n}, "
                f"next in {wait} run(s) (selfcheck_every_n_runs={selfcheck_every})"
            )
            # Still need a minimal `report` object for the ip_record_start
            # block below — it reads report['ip_info']. Build a stub with
            # whatever we already know.
            report = {"ip_info": {"ip": current_ip}}

        # Record the current exit IP in ip_history — this is the only
        # place that catches static (non-rotating) proxy IPs. For rotating
        # proxies report() also fires later, but record_start ensures we
        # never miss an IP just because its first request didn't captcha.
        try:
            ip_info = report.get("ip_info") or {}
            exit_ip = ip_info.get("ip")
            if exit_ip:
                DB.ip_record_start(
                    ip      = exit_ip,
                    country = ip_info.get("country"),
                    city    = ip_info.get("city"),
                    org     = ip_info.get("org") or ip_info.get("isp"),
                    asn     = ip_info.get("asn"),
                )
                # Remember current_ip even for static proxies (used below for
                # later reporting on captchas). Don't overwrite a rotation-set
                # value that might already be there.
                if current_ip is None:
                    current_ip = exit_ip
        except Exception as e:
            logging.debug(f"ip_record_start failed: {e}")

        if report.get("webrtc_leak"):
            logging.error("✗ WebRTC leak detected — aborting")
            return

        # Geo mismatch handling. Happens when rotating proxy returns an exit
        # in a country that doesn't match our profile locale (e.g. AR vs UA).
        # This causes Google to serve wrong-locale SERPs and triggers anti-bot
        # heuristics because the fingerprint says "Ukrainian user" but the IP
        # is foreign.
        if report.get("geo_mismatch"):
            actual = report.get("actual_country")
            logging.warning(
                f"⚠ Exit country mismatch: expected {EXPECTED_COUNTRY!r}, "
                f"got {actual!r}"
            )
            if GEO_MISMATCH_MODE == "abort":
                logging.error(
                    f"  (geo_mismatch_mode=abort — skipping this run to "
                    f"preserve query budget)"
                )
                return
            elif GEO_MISMATCH_MODE == "rotate" and IS_ROTATING_PROXY:
                MAX_ROTATE_TRIES = 5
                rotated_to_country = None
                for attempt in range(1, MAX_ROTATE_TRIES + 1):
                    logging.info(
                        f"  ↻ rotating proxy, attempt {attempt}/"
                        f"{MAX_ROTATE_TRIES}…"
                    )
                    try:
                        browser.force_rotate_ip()
                    except Exception as e:
                        logging.warning(f"    rotate failed: {e}")
                        break
                    _sleep("post_rotate")
                    report2 = diag.full_check(
                        expected_timezone = EXPECTED_TIMEZONE,
                        expected_country  = EXPECTED_COUNTRY,
                    )
                    if not report2.get("geo_mismatch"):
                        rotated_to_country = report2.get("actual_country")
                        logging.info(f"  ✓ rotated into {rotated_to_country}")
                        report = report2
                        break
                else:
                    logging.error(
                        f"  (still in wrong country after "
                        f"{MAX_ROTATE_TRIES} rotations — aborting)"
                    )
                    return
            else:
                logging.warning(
                    "  (geo_mismatch_mode=warn — proceeding despite mismatch; "
                    "detection risk is elevated)"
                )

        # 4. Tracker blocking
        try:
            browser.enable_request_blocking()
        except Exception:
            pass

        # 5. First-run cookie seed (only once per profile, on the very first launch)
        is_fresh_profile = not os.path.exists(browser.session_dir)
        if is_fresh_profile:
            logging.info("🌱 Fresh profile — seeding Google consent cookies...")
            try:
                browser.stealth_get("https://www.google.com/")
                _sleep("fresh_google")

                if is_offline_page(driver):
                    logging.error("✗ Google offline — proxy broken")
                    return

                # Accept consent if shown
                try:
                    btn = WebDriverWait(driver, 5).until(
                        EC.element_to_be_clickable((
                            By.XPATH,
                            "//*[contains(text(),'Accept all') or contains(text(),'Принять все') or contains(text(),'Прийняти все')]"
                        ))
                    )
                    btn.click()
                    _sleep("post_consent")
                    logging.info("  🍪 consent accepted")
                except Exception:
                    pass

                # Quick cookie seeding (non-blocking)
                try:
                    from ghost_shell.session.cookie_warmer import CookieWarmer
                    CookieWarmer(driver).fast_warmup()
                except Exception as e:
                    logging.debug(f"  cookie warmer: {e}")

            except Exception as e:
                logging.warning(f"  initial navigation: {e}")
        else:
            logging.info("✓ existing session — skipping cookie seed")

        # ─── 5b. Profile Quality Manager — auto-warmup gate ─────────
        # Two triggers:
        #   1. A row in warmup_runs with status='requested' was queued
        #      from the dashboard ("Run warmup now" on the quality
        #      badge) -- always honoured.
        #   2. The Quality Manager assesses the profile and recommends
        #      warmup (yellow status) AND quality.auto_warmup is true.
        #
        # When triggered, we run hybrid_warmup with the existing driver
        # BEFORE the first real Google query. This is the user-asked-for
        # "control manager that programmatically tops up profile health"
        # piece -- no manual browser-opening, the runtime decides on
        # behalf of the user.
        try:
            requested_warmup = False
            warmup_id        = None
            try:
                row = DB._get_conn().execute(
                    "SELECT id FROM warmup_runs WHERE profile_name = ? "
                    "AND status = 'requested' "
                    "ORDER BY id DESC LIMIT 1",
                    (PROFILE_NAME,),
                ).fetchone()
                if row:
                    requested_warmup = True
                    warmup_id        = row["id"]
            except Exception:
                pass

            quality_says_warmup = False
            quality_reason      = ""
            if not requested_warmup and bool(CFG.get("quality.auto_warmup", True)):
                try:
                    from ghost_shell.profile.quality_manager import should_auto_warmup
                    quality_says_warmup, quality_reason = should_auto_warmup(PROFILE_NAME)
                except Exception as e:
                    logging.debug(f"  quality assessment skipped: {e}")

            if requested_warmup or quality_says_warmup:
                trigger = "manual_request" if requested_warmup else "auto_quality"
                why     = ("user-requested via dashboard"
                           if requested_warmup else quality_reason)
                logging.info(
                    f"🔥 Auto-warmup triggered — {trigger} ({why})"
                )
                try:
                    from ghost_shell.session.cookie_warmer import CookieWarmer
                    started = datetime.now().isoformat(timespec="seconds")
                    res = CookieWarmer(driver).hybrid_warmup() or {}
                    finished = datetime.now().isoformat(timespec="seconds")
                    if requested_warmup and warmup_id:
                        # Update the existing requested row to ok
                        try:
                            DB._get_conn().execute(
                                "UPDATE warmup_runs SET finished_at = ?, "
                                "status = 'ok', sites_planned = ?, "
                                "sites_visited = ? WHERE id = ?",
                                (finished, res.get("sites_planned", 0),
                                 res.get("sites_visited", 0), warmup_id),
                            )
                            DB._get_conn().commit()
                        except Exception as e:
                            logging.debug(f"  warmup_runs update: {e}")
                    else:
                        # Auto-trigger -- insert a new fresh row
                        try:
                            DB._get_conn().execute(
                                "INSERT INTO warmup_runs ("
                                "profile_name, started_at, finished_at, "
                                "preset, sites_planned, sites_visited, "
                                "status, trigger) VALUES "
                                "(?, ?, ?, 'default', ?, ?, 'ok', ?)",
                                (PROFILE_NAME, started, finished,
                                 res.get("sites_planned", 0),
                                 res.get("sites_visited", 0), trigger),
                            )
                            DB._get_conn().commit()
                        except Exception as e:
                            logging.debug(f"  warmup_runs insert: {e}")
                    # D3: log full visit outcome including reasons.
                    # Previously this line always said "visited 0
                    # sites" because hybrid_warmup() returned None
                    # and res.get('sites_visited', 0) defaulted to 0.
                    # Now we surface succeeded/visited/planned + the
                    # `notes` reason string for any non-perfect run.
                    visited_n   = res.get("sites_visited", 0)
                    succeeded_n = res.get("sites_succeeded", visited_n)
                    planned_n   = res.get("sites_planned", 0)
                    notes       = (res.get("notes") or "").strip()
                    if planned_n and succeeded_n == planned_n:
                        logging.info(
                            f"  ✓ warmup completed: {succeeded_n}/"
                            f"{planned_n} sites ok"
                        )
                    elif planned_n:
                        # Partial / failed warmup — log at WARNING so
                        # ops sees the reason without DEBUG enabled.
                        logging.warning(
                            f"  ⚠ warmup partial: {succeeded_n}/"
                            f"{planned_n} sites ok ({notes})"
                        )
                    else:
                        # 0 planned visits (e.g. short_visits=False
                        # or skip path). Log notes verbatim.
                        logging.info(f"  ✓ warmup: {notes or 'cookies-only'}")
                except Exception as e:
                    logging.warning(f"  ✗ warmup failed: {e}")
                    if warmup_id:
                        try:
                            DB._get_conn().execute(
                                "UPDATE warmup_runs SET finished_at = ?, "
                                "status = 'failed' WHERE id = ?",
                                (datetime.now().isoformat(timespec="seconds"),
                                 warmup_id),
                            )
                            DB._get_conn().commit()
                        except Exception:
                            pass
        except Exception as e:
            logging.debug(f"  quality-warmup gate: {e}")

        # (Rotation moved to step 0, before selfcheck, so the diagnostics
        # report reflects the IP we'll actually be using.)

        # ─── STARTUP BANNER (pretty summary of this run's context) ──
        try:
            exit_ip_geo = None
            if current_ip:
                row = DB.ip_get(current_ip) if hasattr(DB, "ip_get") else None
                if row:
                    exit_ip_geo = {"country": row.get("country"),
                                   "org":     row.get("org")}
            log_run_start(
                run_id            = RUN_ID,
                profile_name      = PROFILE_NAME,
                payload           = getattr(browser, "last_payload", None),
                proxy_url         = PROXY,
                exit_ip           = current_ip,
                exit_ip_geo       = exit_ip_geo,
                queries           = SEARCH_QUERIES,
                target_domains    = MY_DOMAINS,
                chrome_path       = browser.browser_path,
                rotating          = IS_ROTATING_PROXY,
                rotation_provider = DB.config_get("proxy.rotation_provider"),
            )
            if getattr(browser, "last_payload", None):
                log_payload_summary(browser.last_payload,
                                    level=logging.INFO)
        except Exception as e:
            logging.debug(f"banner render: {e}")

        # ─── MAIN SEARCH LOOP (wrapped in watchdog) ──────────
        from ghost_shell.browser.watchdog import BrowserWatchdog

        max_stall = CFG.get("watchdog.max_stall_sec", 180)
        check_every = CFG.get("watchdog.check_interval_sec", 15)

        with BrowserWatchdog(
            driver         = driver,
            run_id         = RUN_ID,
            profile_name   = PROFILE_NAME,
            max_stall_sec  = max_stall,
            check_interval = check_every,
        ) as dog:

            # ── Runtime-selection ladder ──────────────────────────
            # Resolution order:
            #
            #   1. PROFILE-ASSIGNED SCRIPT — profiles.script_id → scripts
            #      row, or falls back to is_default=1 if the assignment
            #      is missing/invalid. This is the new norm after the
            #      scripts-library upgrade.
            #
            #   2. LEGACY MAIN + POST_AD  (actions.main_script +
            #      actions.post_ad_actions). Kept for configs that
            #      didn't migrate yet — harmless because the migration
            #      runs at DB init and copies these into the Default
            #      script on first boot after the upgrade.
            #
            #   3. HARDCODED LOOP over SEARCH_QUERIES + POST_AD_ACTIONS.
            #
            # Most users will now hit #1. The fallback ladder is still
            # there because older config bundles, imports, or DB copies
            # from pre-upgrade versions can legitimately have main_script
            # populated with no scripts row yet.
            try:
                from ghost_shell.db.database import get_db as _getdb
                _db = _getdb()
                # Phase 5.1: GHOST_SHELL_SCRIPT_ID env (set by the
                # dashboard when /api/scripts/<id>/run is invoked from
                # a library card) takes priority over the profile-bound
                # resolution. This makes "Run on profiles" from a script
                # card a true one-shot: the user picks a script,
                # presses run, that script runs THIS time only, and
                # the profile's own settings (use_script_on_launch +
                # script_id) stay untouched for next time.
                _override = os.environ.get("GHOST_SHELL_SCRIPT_ID")
                _script = None
                if _override:
                    try:
                        _script = _db.script_get(int(_override))
                        if _script:
                            logging.info(
                                f"[main] using one-shot script override: "
                                f"id={_override} name={_script.get('name')!r}")
                    except Exception as e:
                        logging.warning(
                            f"[main] script_id override {_override} "
                            f"unresolvable: {e}")
                if _script is None:
                    _script = _db.script_resolve_for_profile(PROFILE_NAME)
                unified_flow = _script["flow"] if _script else []
            except Exception as e:
                logging.warning(f"[main] script_resolve failed: {e}")
                unified_flow = CFG.get("actions.flow", []) or []
            main_script = CFG.get("actions.main_script", []) or []

            def _cb_search(q: str):
                """Callback for search_query step — returns the ads list.
                Shared between unified and legacy paths.

                Fix A (Apr 2026): pass defer_serp_behavior=True so
                search_query skips its own scroll/dwell pass. The
                unified-flow / main_script path that called us will
                run its own click pipeline next, and PLA carousels
                must remain stamped until those clicks resolve.
                Scripts that want post-click human behavior should
                add explicit `dwell` / `scroll` steps after foreach_ad.
                """
                try:
                    ads = search_query(
                        browser, q, sqm,
                        current_ip=current_ip,
                        watchdog=dog,
                        defer_serp_behavior=True,
                    )
                    if ads:
                        save_ads(ads)
                    return ads or []
                except Exception as e:
                    logging.warning(f"_cb_search({q!r}): {e}")
                    return []

            def _cb_rotate():
                try:
                    return browser.force_rotate_ip()
                except Exception as e:
                    logging.warning(f"_cb_rotate: {e}")
                    return None

            def _cb_per_ad(ad, query):
                try:
                    run_post_ad_pipeline(browser, ad, query=query)
                except Exception as e:
                    logging.warning(
                        f"per-ad pipeline failed for "
                        f"{ad.get('domain', '?')!r}: {e}"
                    )

            shared_loop_ctx = {
                "all_queries":    SEARCH_QUERIES,
                "search_query":   _cb_search,
                "rotate_ip":      _cb_rotate,
                "per_ad_runner":  _cb_per_ad,
                "watchdog":       dog,
                "my_domains":     MY_DOMAINS,
                "target_domains": TARGET_DOMAINS,
                "run_id":         RUN_ID,
                "profile_name":   PROFILE_NAME,
            }

            skip_legacy_loop = False

            # ── Loud warning if neither path is wired ─────────────
            # Users repeatedly hit the surprise where their profile
            # "found 5 ads" but never clicked a single one. The
            # silent fallback to the bottom for-loop (which only
            # parses + saves, never clicks) was the cause. Make this
            # visible at run-start so the misconfiguration shows up
            # in the log next to the run banner instead of being
            # invisible until they audit a 200-line log later.
            if not unified_flow and not main_script:
                logging.warning(
                    "[main] ⚠ NO SCRIPT BOUND for profile %r -- ads "
                    "will be DETECTED + SAVED but NEVER CLICKED. "
                    "Fix: Scripts page -> 'Apply to profile' on "
                    "Smart Search & Click (or any script with a "
                    "click_ad step). Or open this profile -> "
                    "Active script -> bind a script + enable "
                    "'Use script on launch'.",
                    PROFILE_NAME,
                )

            if unified_flow:
                # ── UNIFIED FLOW PATH ──────────────────────────
                from ghost_shell.actions.runner import run_flow
                logging.info(
                    f"[main] Running unified flow with "
                    f"{len(unified_flow)} top-level step(s)"
                )
                try:
                    run_flow(browser, unified_flow,
                             loop_ctx=shared_loop_ctx)
                except Exception as e:
                    logging.error(
                        f"[main] unified flow execution failed: "
                        f"{type(e).__name__}: {e}"
                    )
                skip_legacy_loop = True

            elif main_script:
                # ── LEGACY MAIN_SCRIPT PATH ────────────────────
                from ghost_shell.actions.runner import run_main_script
                logging.info(
                    f"[main] Running main_script with {len(main_script)} steps "
                    f"(unified flow not configured)"
                )
                run_main_script(browser, main_script, loop_ctx=shared_loop_ctx)
                # Skip the legacy loop. Using a local flag instead of
                # `SEARCH_QUERIES = []` — assigning to the module-level
                # name inside this function would make Python treat it
                # as a local for the WHOLE enclosing block, tripping
                # `for i, query in enumerate(SEARCH_QUERIES, 1)` below
                # with UnboundLocalError before the assignment runs.
                skip_legacy_loop = True

            if skip_legacy_loop:
                queries_to_run = []
            else:
                queries_to_run = SEARCH_QUERIES

            for i, query in enumerate(queries_to_run, 1):
                dog.heartbeat()

                if not browser.is_alive():
                    # Mid-run death — try to recover once before giving up.
                    if _try_recover_browser(browser, reason="is_alive=False in legacy loop"):
                        logging.info("  continuing legacy loop after recovery")
                    else:
                        logging.warning("⚠ Chrome window was closed and recovery failed — stopping run")
                        break

                t0 = time.time()
                # Fix A (Apr 2026): only defer SERP behavior when we
                # actually have a click pipeline that will run after
                # parse. Empty pipeline → harmless to scroll, and
                # legacy users without scripts still want the
                # human-shaped post-parse behavior to fire.
                _legacy_will_click = bool(POST_AD_ACTIONS) or \
                                     bool(ON_TARGET_DOMAIN_ACTIONS)
                try:
                    ads = search_query(browser, query, sqm,
                                       current_ip=current_ip,
                                       watchdog=dog,
                                       defer_serp_behavior=_legacy_will_click)
                except Exception as e:
                    # Catch "chrome not reachable" / "session deleted" / etc
                    # — these mean the browser died mid-query. Try to
                    # recover once before breaking out of the run loop.
                    err_str = str(e).lower()
                    if any(tok in err_str for tok in (
                            "chrome not reachable",
                            "session deleted",
                            "no such window",
                            "invalid session id",
                            "disconnected: not connected to devtools")):
                        if _try_recover_browser(
                            browser,
                            reason=f"query {i}/{len(SEARCH_QUERIES)} raised {type(e).__name__}"
                        ):
                            # Skip this query — it's lost — but keep the
                            # run alive for remaining queries.
                            logging.info(
                                f"  query {i} aborted due to crash, "
                                f"continuing with remaining queries"
                            )
                            continue
                        logging.warning(
                            f"⚠ Chrome died during query {i}/"
                            f"{len(SEARCH_QUERIES)} and recovery failed — "
                            f"stopping run ({type(e).__name__})"
                        )
                        break
                    raise
                duration = time.time() - t0
                dog.heartbeat()

                # Compact one-line result per query
                competitors_count = sum(
                    1 for a in (ads or [])
                    if not any(my in (a.get("domain") or "")
                               for my in MY_DOMAINS)
                )
                my_matched = any(
                    any(my in (a.get("domain") or "") for my in MY_DOMAINS)
                    for a in (ads or [])
                )
                log_query_result(
                    idx               = i,
                    total             = len(SEARCH_QUERIES),
                    query             = query,
                    ads_found         = len(ads or []),
                    competitors_found = competitors_count,
                    duration_sec      = duration,
                    my_domain_matched = my_matched,
                )

                # Record metrics — both in DB (sqm) AND in local counters.
                # Local counters are the source of truth for the run
                # summary since sqm.record() has silent-fail modes.
                #
                # Counter semantics (important — this was recently fixed):
                #   total_queries  = every query that ran (with or without ads)
                #                    — this is the "searches" metric everyone
                #                    expects on the dashboard
                #   total_ads      = actual ad count across all queries
                #   total_empty    = subset of total_queries that got 0 ads
                #
                # Previously total_queries incremented only when ads were
                # found, which made Run #49 (0 ads across 3 queries) report
                # `queries: 0/3` and the dashboard chart show a flat zero
                # line even though 3 searches had happened. Overview's
                # SEARCHES column now matches reality.
                RUN_COUNTERS["total_queries"] += 1
                if ads:
                    sqm.record("search_ok", query=query,
                               results_count=len(ads), duration_sec=duration)
                    RUN_COUNTERS["total_ads"] += len(ads)
                    if IS_ROTATING_PROXY and current_ip:
                        browser.report_rotating(current_ip, success=True)
                    # D2: non-empty result — break any silent-demotion streak.
                    RUN_COUNTERS["consecutive_silent_demotions"] = 0
                else:
                    sqm.record("search_empty", query=query, duration_sec=duration)
                    RUN_COUNTERS["total_empty"] += 1

                    # ── D2: silent demotion detector ────────────────────
                    # An empty-but-healthy SERP is the giveaway for an IP
                    # that Google has silently demoted (no captcha, no
                    # block screen, just no PLA / sponsored slots given
                    # to this datacenter / fingerprint). The signature:
                    #
                    #   • parser found 0 ads
                    #   • organic results are plentiful (>= 5)
                    #   • no recaptcha iframe
                    #   • not a "did you mean" spelling rescue page
                    #   • no "0 results" no-match message
                    #
                    # Single empty SERP can be legitimate (niche query,
                    # no advertisers). The streak across multiple
                    # different queries is what makes it diagnostic.
                    # Once SILENT_DEMOTION_BURN_THRESHOLD is hit we
                    # synthesize a captcha event so Recovery #4 (FP
                    # regen after N captcha) can react, AND we bump
                    # total_captchas so the post-run health snapshot
                    # records the burn signal.
                    try:
                        d = _LAST_SERP_DIAG or {}
                        organic_n = int(d.get("organic") or 0)
                        is_silent = (
                            organic_n >= 5
                            and not d.get("recaptcha")
                            and not d.get("didyoumean")
                            and not d.get("results0")
                        )
                        if is_silent:
                            RUN_COUNTERS["consecutive_silent_demotions"] += 1
                            RUN_COUNTERS["total_silent_demotions"] += 1
                            streak = RUN_COUNTERS["consecutive_silent_demotions"]
                            logging.warning(
                                f"  ⚠ silent demotion streak {streak}/"
                                f"{SILENT_DEMOTION_BURN_THRESHOLD} "
                                f"(empty ads but organic={organic_n}, no "
                                f"captcha) — IP likely shadow-banned for "
                                f"ads inventory"
                            )
                            if streak == SILENT_DEMOTION_BURN_THRESHOLD:
                                logging.warning(
                                    "  ⚠ silent demotion threshold reached "
                                    "— synthesizing captcha event so Recovery "
                                    "loop and burn-detection can react. "
                                    "Recommend manual proxy swap if streak "
                                    "doesn't break."
                                )
                                # Synthetic captcha event — feeds:
                                #   1) sqm.captcha_rate_24h (used by
                                #      should_abort + needs_attention)
                                #   2) RUN_COUNTERS.total_captchas (the
                                #      run-summary banner + Recovery #4
                                #      threshold check)
                                #   3) silent_demotion event (new) for
                                #      timeline / forensics on the
                                #      health page.
                                try:
                                    sqm.record(
                                        "silent_demotion",
                                        query=query,
                                        details=(
                                            f"streak={streak} "
                                            f"organic={organic_n}"
                                        ),
                                    )
                                except Exception:
                                    pass
                                try:
                                    sqm.record(
                                        "captcha",
                                        query=query,
                                        details="synthesized:silent_demotion",
                                    )
                                except Exception:
                                    pass
                                RUN_COUNTERS["total_captchas"] += 1
                        else:
                            # Empty SERP but NOT silent demotion (genuine
                            # niche / no-results / captcha-already-fired).
                            # Reset streak so a single off-pattern empty
                            # doesn't re-trigger threshold next loop.
                            RUN_COUNTERS["consecutive_silent_demotions"] = 0
                    except Exception as _e:
                        logging.debug(f"  silent-demotion check skipped: {_e}")

                # Save and run action pipeline per ad (can take a while → pause)
                save_ads(ads)
                if ads:
                    with dog.pause(f"action pipeline for {len(ads)} ads"):
                        for ad in ads:
                            run_post_ad_pipeline(browser, ad, query=query)

                all_ads.extend(ads)

                # Fix A (Apr 2026): if we deferred SERP behavior so
                # the click pipeline could run on still-stamped DOM,
                # run it NOW (after clicks). This preserves the
                # human-shaped "read SERP / scroll / occasional
                # organic click" pattern between queries without
                # destroying parse_ads stamps before they get used.
                # No-op when defer didn't fire (search_query
                # already ran post_ads_behavior internally).
                if _legacy_will_click and ads:
                    try:
                        from ghost_shell.browser.serp_behavior import post_ads_behavior
                        post_ads_behavior(
                            browser.driver, DB,
                            exclude_domains=list(MY_DOMAINS or []),
                            watchdog=dog,
                        )
                    except Exception as e:
                        logging.debug(f"  post-pipeline SERP behavior failed: {e}")

                # Gap between queries
                if i < len(SEARCH_QUERIES):
                    lo = float(BEHAVIOR["between_queries_min"])
                    hi = float(BEHAVIOR["between_queries_max"])
                    gap = random.uniform(lo, hi)
                    logging.info(f"… waiting {gap:.0f}s before next query")
                    time.sleep(gap)

        # ── Auto-snapshot cookie pool on clean run ─────────────────
        # End of run reached naturally (no exception propagated out of
        # the query loop). If captcha count stayed at 0 we freeze the
        # current cookie + storage state for later resurrection.
        # The helper is defensive: never raises, skips silently when
        # conditions aren't met. See ghost_shell/session/cookie_pool.py.
        try:
            from ghost_shell.session.cookie_pool import snapshot_after_run
            snapshot_after_run(
                PROFILE_NAME, browser.driver,
                run_id=RUN_ID,
                had_captcha=RUN_COUNTERS["total_captchas"] > 0,
                exit_code=0,   # we reached this line → exit will be 0
            )
        except Exception as _snap_err:
            logging.debug(f"[cookie_pool] snapshot hook skipped: {_snap_err}")

        # Final summary
        print_summary(all_ads)


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ── Signal → exception bridge ──────────────────────────────────
    # On Windows, dashboard_server.py terminates us via Popen.terminate(),
    # which sends SIGTERM. Python's DEFAULT SIGTERM handler is SIG_DFL,
    # which kills the process instantly — no `finally`, no __exit__,
    # no browser.close() → Chrome + chromedriver become orphans.
    #
    # Raising KeyboardInterrupt from the signal handler converts the
    # abrupt kill into a regular exception that propagates out through
    # our `with GhostShellBrowser(...) as browser:` block, letting
    # __exit__ → close() tear down Chrome cleanly.
    #
    # CTRL+C is already handled (raises KeyboardInterrupt natively);
    # this just extends the same behavior to SIGTERM + CTRL_BREAK.
    import signal as _signal
    def _sig_to_interrupt(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    try:
        _signal.signal(_signal.SIGTERM, _sig_to_interrupt)
        if hasattr(_signal, "SIGBREAK"):
            _signal.signal(_signal.SIGBREAK, _sig_to_interrupt)
    except (ValueError, OSError):
        pass   # not on main thread (shouldn't happen in __main__)

    exit_code = 0
    error_msg = None
    run_started_at = time.time()
    try:
        run_monitor()
    except KeyboardInterrupt:
        exit_code = 130
        error_msg = "Interrupted by user (Ctrl+C)"
        logging.warning(error_msg)
    except SystemExit as _se:
        # Audit D1 fix (Apr 2026): the burned-profile block uses
        # sys.exit(75) inside run_monitor() to signal "needs attention,
        # do not retry until human intervenes". SystemExit doesn't
        # inherit from Exception, so the `except Exception` arm below
        # would NOT catch it — and the original exit_code=0 would
        # leak through to DB.run_finish(), making the dashboard show
        # the run as successful. Capture the code here and let the
        # finally block record it correctly.
        try:
            exit_code = int(_se.code) if _se.code is not None else 0
        except (TypeError, ValueError):
            # sys.exit("string") sets code to the string, not an int.
            # Fall back to 1 and stash the message for run_finish.
            exit_code = 1
            error_msg = str(_se.code)[:240]
        if exit_code == 75 and not error_msg:
            error_msg = "blocked: profile needs attention (D1)"
    except Exception as e:
        exit_code = 1
        error_msg = f"{type(e).__name__}: {e}"
        # Short structured banner rather than a 50-line Python traceback
        log_error_banner("RUN FAILED", error_msg)
        # Full trace only at DEBUG — shows up in the profile log file
        logging.debug("Full traceback:", exc_info=True)
    finally:
        run_duration = time.time() - run_started_at

        if RUN_ID:
            # Read per-run stats from our local counters instead of
            # DB.events_summary(hours=24), which aggregated across the
            # last 24h AND miscounted `search_ok` as ads. See the
            # RUN_COUNTERS definition at the top for rationale.
            stats_dict = {
                "queries_done":  RUN_COUNTERS["total_queries"],
                "queries_total": len(SEARCH_QUERIES),
                "total_ads":     RUN_COUNTERS["total_ads"],
                "captchas":      RUN_COUNTERS["total_captchas"],
                "empty_results": RUN_COUNTERS["total_empty"],
            }
            try:
                DB.run_finish(
                    RUN_ID,
                    exit_code    = exit_code,
                    error        = error_msg,
                    total_queries= stats_dict["queries_done"],
                    total_ads    = stats_dict["total_ads"],
                    captchas     = stats_dict["captchas"],
                )
            except Exception as e:
                logging.error(f"[main] run_finish error: {e}")

            try:
                log_run_end(
                    run_id       = RUN_ID,
                    duration_sec = run_duration,
                    exit_code    = exit_code,
                    stats        = stats_dict,
                    error        = error_msg,
                )
            except Exception as e:
                logging.debug(f"end banner: {e}")

        sys.exit(exit_code)
