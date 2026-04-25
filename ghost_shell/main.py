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
        let title = '';
        const heading = block.querySelector('[role="heading"], h3, h4, .LC20lb');
        if (heading) title = heading.textContent.trim();

        let displayUrl = '';
        const cite = block.querySelector(
            'cite, span.VuuXrf, span.x2VHCd, span[role="text"], .x3G5ab'
        );
        if (cite) displayUrl = cite.textContent.trim();

        let googleClickUrl = '';
        let cleanUrl       = '';
        let primaryAnchor  = null;

        const allLinks = block.querySelectorAll('a[href]');
        for (const link of allLinks) {
            const href = link.href || '';
            if (!href.startsWith('http')) continue;
            if (href.includes('/aclk?') ||
                href.includes('googleadservices.com') ||
                href.includes('googlesyndication.com')) {
                if (!googleClickUrl) {
                    googleClickUrl = href;
                    primaryAnchor = link;
                }
                for (const attr of ['data-rw', 'data-pcu', 'data-rh', 'data-agdh']) {
                    const val = link.getAttribute(attr);
                    if (val && val.startsWith('http') && !cleanUrl) {
                        cleanUrl = val;
                    }
                }
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

    ads = []
    seen_domains = set()
    own_filtered_count = 0

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

            domain = extract_domain(clean_url) or extract_domain(display_url)
            if not domain:
                continue

            # Filter Google internal domains
            if any(g in domain for g in ("google.com", "google.ua",
                                         "googleusercontent.com",
                                         "googlesyndication.com")):
                continue

            # ── OWN-DOMAIN FILTER — hard gate ──────────────────────
            # Own-domain ads NEVER get into the returned list. This is
            # the single source of truth for "is this ad ours". click_ad
            # then trusts this list and clicks ONLY ads that came
            # through this filter — no independent DOM scan.
            if any(my.lower() in domain.lower() for my in MY_DOMAINS):
                own_filtered_count += 1
                fmt_tag = f" [{ad_format}]" if ad_format != "text" else ""
                logging.info(f"  - [own]{fmt_tag} {domain} — {title[:50]}")
                continue

            # Dedup within query (same domain across text + shopping =
            # one record). Keep the first occurrence — usually that's
            # the highest-position text ad.
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
                "anchor_id":         anchor_id,   # used by click_ad
                "ad_format":         ad_format,   # 'text' / 'shopping_carousel' / 'pla_grid'
            })
            mark = "★" if ads[-1]["is_target"] else "·"
            fmt_tag = f" [{ad_format}]" if ad_format != "text" else ""
            logging.info(f"  {mark}{fmt_tag} {domain} — {title[:60]}")

        except Exception as e:
            logging.debug(f"  block processing error: {e}")

    if own_filtered_count > 0:
        logging.debug(f"  parser: filtered {own_filtered_count} own-domain ad(s) across all formats")

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
    from ghost_shell.actions.runner import run_pipeline

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
}


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

        should_selfcheck = (
            should_rotate or                           # always after rotation
            profile_run_n == 1 or                      # always on first run
            profile_run_n % selfcheck_every == 1       # every N runs
        )

        # 1. Profile health sanity check (soft: never aborts on first runs)
        sqm = SessionQualityMonitor(browser.user_data_path)
        should_abort, reason = sqm.should_abort()
        if should_abort:
            logging.warning(f"  ⚠ profile health: {reason}")
            logging.warning(f"    (proceeding anyway — disable this check if too strict)")

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
                _script = _getdb().script_resolve_for_profile(PROFILE_NAME)
                unified_flow = _script["flow"] if _script else []
            except Exception as e:
                logging.warning(f"[main] script_resolve failed: {e}")
                unified_flow = CFG.get("actions.flow", []) or []
            main_script  = CFG.get("actions.main_script", []) or []

            def _cb_search(q: str):
                """Callback for search_query step — returns the ads list.
                Shared between unified and legacy paths."""
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
                try:
                    ads = search_query(browser, query, sqm,
                                       current_ip=current_ip,
                                       watchdog=dog)
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
                else:
                    sqm.record("search_empty", query=query, duration_sec=duration)
                    RUN_COUNTERS["total_empty"] += 1

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
