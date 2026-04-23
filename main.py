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
from urllib.parse import urlparse, parse_qs, unquote, quote
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from ghost_shell_browser import GhostShellBrowser
from proxy_diagnostics import ProxyDiagnostics
from session_quality import SessionQualityMonitor
from config import Config
from db import get_db
from log_banners import (
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
if PROFILE_NAME:
    try:
        _meta = DB.profile_meta_get(PROFILE_NAME)
        _profile_proxy = (_meta.get("proxy_url") or "").strip() or None
    except Exception:
        _profile_proxy = None

if _env_proxy:
    PROXY = _env_proxy
    logging.info(f"[main] Proxy from GHOST_SHELL_PROXY_URL env: {PROXY[:50]}...")
elif _profile_proxy:
    PROXY = _profile_proxy
    logging.info(f"[main] Proxy from profile override: {PROXY[:50]}...")
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
    if PROFILE_NAME:
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
            "підключіться до інтернету", "connect to the internet",
            "в режимі офлайн", "you're offline", "you are offline",
            "нет соединения", "подключитесь к интернету",
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
            "подозрительный трафик",
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
                (document.body && /unusual traffic|невичайний трафік|нетипичный трафик/i
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
            "эта страница предназначена для тестирования",
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
# AD PARSER
# ──────────────────────────────────────────────────────────────

def extract_real_url(href: str) -> str:
    """Parse adurl/url/q parameter from Google redirect URL"""
    if not href:
        return ""
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        for key in ("adurl", "url", "q"):
            if key in qs:
                real = unquote(qs[key][0])
                if real.startswith("http"):
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
    """
    Extract ad blocks from SERP using TreeWalker + data-text-ad fallback.
    Returns list of {query, title, display_url, clean_url, google_click_url, domain, found_at}.
    """
    js_script = r"""
    const SPONSORED_MARKERS = [
        'Sponsored', 'Реклама', 'Спонсировано', 'Спонсоване',
        'Anuncio', 'Annonce', 'Werbung', 'Annuncio'
    ];

    const adBlocks = new Set();

    // Method 1: find "Sponsored" / "Реклама" label via TreeWalker
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
                    adBlocks.add(parent);
                    break;
                }
            }
        }
    }

    // Method 2: data-text-ad attribute
    document.querySelectorAll('div[data-text-ad]').forEach(el => adBlocks.add(el));

    const results = [];
    adBlocks.forEach(block => {
        let title = '';
        const heading = block.querySelector('[role="heading"], h3');
        if (heading) title = heading.textContent.trim();

        let displayUrl = '';
        const cite = block.querySelector('cite, span.VuuXrf, span.x2VHCd, span[role="text"]');
        if (cite) displayUrl = cite.textContent.trim();

        let googleClickUrl = '';
        let cleanUrl       = '';

        const allLinks = block.querySelectorAll('a[href]');
        for (const link of allLinks) {
            const href = link.href || '';
            if (href.includes('/aclk?') || href.includes('googleadservices.com')) {
                if (!googleClickUrl) googleClickUrl = href;
                for (const attr of ['data-rw', 'data-pcu', 'data-rh', 'data-agdh']) {
                    const val = link.getAttribute(attr);
                    if (val && val.startsWith('http') && !cleanUrl) {
                        cleanUrl = val;
                    }
                }
            }
        }
        if (!googleClickUrl) {
            for (const link of allLinks) {
                const href = link.href || '';
                if (href && href.startsWith('http')) {
                    googleClickUrl = href;
                    break;
                }
            }
        }

        if (googleClickUrl || displayUrl) {
            results.push({
                title: title,
                displayUrl: displayUrl,
                googleClickUrl: googleClickUrl,
                cleanFromDataRw: cleanUrl,
            });
        }
    });

    return results;
    """

    try:
        raw_ads = driver.execute_script(js_script) or []
    except Exception as e:
        logging.warning(f"  JS parse error: {e}")
        return []

    ads = []
    seen_domains = set()

    for raw in raw_ads:
        try:
            google_click_url = raw.get("googleClickUrl", "")
            clean_from_rw    = raw.get("cleanFromDataRw", "")
            display_url      = raw.get("displayUrl", "")
            title            = raw.get("title", "")

            # Clean URL priority: data-rw > parsed aclk > display
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

            domain = extract_domain(clean_url) or extract_domain(display_url)
            if not domain:
                continue

            # Filter Google internal domains
            if any(g in domain for g in ("google.com", "google.ua", "googleusercontent.com")):
                continue

            # Filter our own domains
            if any(my in domain for my in MY_DOMAINS):
                logging.info(f"  - [own] {domain} — {title[:50]}")
                continue

            # Dedup within query
            if domain in seen_domains:
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
            })
            mark = "★" if ads[-1]["is_target"] else "·"
            logging.info(f"  {mark} {domain} — {title[:60]}")

        except Exception as e:
            logging.debug(f"  block processing error: {e}")

    return ads


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
    from action_runner import run_pipeline

    pipeline = POST_AD_ACTIONS
    # Backwards-compat: if the user still has a populated legacy
    # on_target_domain_actions list AND the current ad is on a target
    # domain, run the legacy pipeline for it. This goes away once the
    # user visits Scripts (the dashboard auto-migrates on load).
    if ad.get("is_target") and ON_TARGET_DOMAIN_ACTIONS:
        pipeline = ON_TARGET_DOMAIN_ACTIONS

    if not pipeline:
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
                 current_ip: str = None, watchdog=None) -> list[dict]:
    """
    Single query execution:
    - Open direct search URL (eager page load — returns at DOMContentLoaded)
    - Try to parse ads immediately; poll briefly if SERP still rendering
    - Refresh-loop until ads appear (or max attempts)
    - Parse & return ads

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
                    logging.info(f"  → rotated {current_ip} → {new_ip}, reloading")
                    current_ip = new_ip
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
                logging.error(
                    f"  ✗ CAPTCHA — tried {captcha_rotations_used} rotations, "
                    f"Google still flags us. Burning this IP."
                )
            sqm.record("captcha", query=query, url=driver.current_url)
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
            # Pass MY_DOMAINS + TARGET_DOMAINS as exclude list so the
            # organic-click step never visits our own site. That would
            # be (a) a wasted self-click and (b) a detectable
            # "same fingerprint keeps searching then clicks its own
            # brand" pattern.
            try:
                from serp_behavior import post_ads_behavior
                own_domains = list(set((MY_DOMAINS or []) + (TARGET_DOMAINS or [])))
                post_ads_behavior(
                    driver, DB,
                    exclude_domains=own_domains,
                    watchdog=watchdog,
                )
            except Exception as e:
                logging.debug(f"  SERP behavior failed: {e}")

            return ads

        if attempt >= REFRESH_MAX_ATTEMPTS:
            logging.info(f"  ✗ no ads after {attempt} attempts")
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
            logging.warning("  browser died during wait — aborting query")
            return []

        try:
            driver.refresh()
        except Exception as e:
            err = type(e).__name__
            logging.warning(f"  refresh {err}: {str(e)[:80]}")
            if not browser.is_alive():
                return []
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
    logging.info("═" * 62)

    with GhostShellBrowser(
        profile_name       = PROFILE_NAME,
        proxy_str          = PROXY,
        auto_session       = CFG.get("browser.auto_session", True),
        is_rotating_proxy  = IS_ROTATING_PROXY,
        rotation_api_url   = ROTATION_API_URL,
        enrich_on_create   = CFG.get("browser.enrich_on_create", True),
        preferred_language = PREFERRED_LANGUAGE,
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

        # 1. Profile health sanity check (soft: never aborts on first runs)
        sqm = SessionQualityMonitor(browser.user_data_path)
        should_abort, reason = sqm.should_abort()
        if should_abort:
            logging.warning(f"  ⚠ profile health: {reason}")
            logging.warning(f"    (proceeding anyway — disable this check if too strict)")

        # 2. Fingerprint self-check (writes to DB + profile file)
        browser.health_check(verbose=True)

        # 3. Proxy diagnostics with geo-mismatch detection.
        diag = ProxyDiagnostics(driver, proxy_url=PROXY)
        report = diag.full_check(
            expected_timezone = EXPECTED_TIMEZONE,
            expected_country  = EXPECTED_COUNTRY,
        )
        diag.print_report(report)

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
                    from cookie_warmer import CookieWarmer
                    CookieWarmer(driver).fast_warmup()
                except Exception as e:
                    logging.debug(f"  cookie warmer: {e}")

            except Exception as e:
                logging.warning(f"  initial navigation: {e}")
        else:
            logging.info("✓ existing session — skipping cookie seed")

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
        from watchdog import BrowserWatchdog

        max_stall = CFG.get("watchdog.max_stall_sec", 180)
        check_every = CFG.get("watchdog.check_interval_sec", 15)

        with BrowserWatchdog(
            driver         = driver,
            run_id         = RUN_ID,
            profile_name   = PROFILE_NAME,
            max_stall_sec  = max_stall,
            check_interval = check_every,
        ) as dog:

            # ── NEW: if a "main_script" is configured in Scripts page,
            # run THAT instead of the hardcoded query-iteration loop.
            # Steps inside main_script call back into search_query() /
            # run_post_ad_pipeline() via loop_ctx callbacks.
            main_script = CFG.get("actions.main_script", []) or []
            if main_script:
                from action_runner import run_main_script
                logging.info(
                    f"[main] Running main_script with {len(main_script)} steps "
                    f"(legacy query loop bypassed)"
                )

                def _cb_search(q: str):
                    """Callback for search_query step — returns the ads list."""
                    try:
                        ads = search_query(
                            browser, q, sqm,
                            current_ip=current_ip,
                            watchdog=dog,
                        )
                        if ads:
                            save_ads(ads)
                        return ads or []
                    except Exception as e:
                        logging.warning(f"main_script._cb_search({q!r}): {e}")
                        return []

                def _cb_rotate():
                    try:
                        return browser.force_rotate_ip()
                    except Exception as e:
                        logging.warning(f"main_script._cb_rotate: {e}")
                        return None

                def _cb_per_ad(ad, query):
                    """Dispatch one ad through the per-ad pipeline."""
                    try:
                        run_post_ad_pipeline(browser, ad, query=query)
                    except Exception as e:
                        logging.warning(
                            f"main_script per-ad failed for "
                            f"{ad.get('domain', '?')!r}: {e}"
                        )

                run_main_script(browser, main_script, loop_ctx={
                    "all_queries":   SEARCH_QUERIES,
                    "search_query":  _cb_search,
                    "rotate_ip":     _cb_rotate,
                    "per_ad_runner": _cb_per_ad,
                    "watchdog":      dog,
                })
                # Skip the legacy loop. Using a local flag instead of
                # `SEARCH_QUERIES = []` — assigning to the module-level
                # name inside this function would make Python treat it
                # as a local for the WHOLE enclosing block, tripping
                # `for i, query in enumerate(SEARCH_QUERIES, 1)` below
                # with UnboundLocalError before the assignment runs.
                skip_legacy_loop = True
            else:
                skip_legacy_loop = False

            if skip_legacy_loop:
                queries_to_run = []
            else:
                queries_to_run = SEARCH_QUERIES

            for i, query in enumerate(queries_to_run, 1):
                dog.heartbeat()

                if not browser.is_alive():
                    logging.warning("⚠ Chrome window was closed — stopping run")
                    break

                t0 = time.time()
                try:
                    ads = search_query(browser, query, sqm,
                                       current_ip=current_ip,
                                       watchdog=dog)
                except Exception as e:
                    # Catch "chrome not reachable" / "session deleted" / etc
                    # — these mean the browser died mid-query (user closed it,
                    # or it crashed). Don't try to continue with a dead driver.
                    err_str = str(e).lower()
                    if any(tok in err_str for tok in (
                            "chrome not reachable",
                            "session deleted",
                            "no such window",
                            "invalid session id",
                            "disconnected: not connected to devtools")):
                        logging.warning(
                            f"⚠ Chrome died during query {i}/"
                            f"{len(SEARCH_QUERIES)} — stopping run "
                            f"({type(e).__name__})"
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

                # Record metrics
                if ads:
                    sqm.record("search_ok", query=query,
                               results_count=len(ads), duration_sec=duration)
                    if IS_ROTATING_PROXY and current_ip:
                        browser.report_rotating(current_ip, success=True)
                else:
                    sqm.record("search_empty", query=query, duration_sec=duration)

                # Save and run action pipeline per ad (can take a while → pause)
                save_ads(ads)
                if ads:
                    with dog.pause(f"action pipeline for {len(ads)} ads"):
                        for ad in ads:
                            run_post_ad_pipeline(browser, ad, query=query)

                all_ads.extend(ads)

                # Gap between queries
                if i < len(SEARCH_QUERIES):
                    lo = float(BEHAVIOR["between_queries_min"])
                    hi = float(BEHAVIOR["between_queries_max"])
                    gap = random.uniform(lo, hi)
                    logging.info(f"… waiting {gap:.0f}s before next query")
                    time.sleep(gap)

        # Final summary
        print_summary(all_ads)


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    exit_code = 0
    error_msg = None
    run_started_at = time.time()
    try:
        run_monitor()
    except KeyboardInterrupt:
        exit_code = 130
        error_msg = "Interrupted by user (Ctrl+C)"
        logging.warning(error_msg)
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
            stats_dict = {}
            try:
                summary = DB.events_summary(profile_name=PROFILE_NAME, hours=24)
                stats_dict = {
                    "queries_done":  summary.get("search_ok", 0) + summary.get("search_empty", 0),
                    "queries_total": len(SEARCH_QUERIES),
                    "total_ads":     summary.get("search_ok", 0),
                    "captchas":      summary.get("captcha", 0),
                    "empty_results": summary.get("search_empty", 0),
                }
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
