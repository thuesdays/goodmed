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

# Proxy selection — pool with random pick or single URL
PROXY_USE_POOL       = CFG.get("proxy.use_pool", False)
PROXY_POOL_URLS      = CFG.get("proxy.pool_urls", []) or []
PROXY_SINGLE         = CFG.get("proxy.url", "")

if PROXY_USE_POOL and PROXY_POOL_URLS:
    PROXY = random.choice([p for p in PROXY_POOL_URLS if p.strip()])
    logging.info(f"[main] Picked random proxy from pool: {PROXY[:50]}...")
else:
    PROXY = PROXY_SINGLE

PROFILE_NAME         = os.environ.get("GHOST_SHELL_PROFILE") or CFG.get("browser.profile_name")
IS_ROTATING_PROXY    = CFG.get("proxy.is_rotating", True)
ROTATION_API_URL     = CFG.get("proxy.rotation_api_url")
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
    logging.info(f"[main] Created standalone run #{RUN_ID}")

# Dup file for manual inspection
COMPETITOR_URLS_FILE = "competitor_urls.txt"


# Logging
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


def run_post_ad_pipeline(browser, ad: dict):
    """
    Run the configured action pipeline for one ad. Delegates to
    action_runner.run_pipeline which implements 17 human-like action
    types (click_ad with real mouse movement, read, type, hover,
    scroll, back, etc.).

    Pipeline is configured per-profile in the dashboard (Behavior /
    Actions pages). See action_runner.action_catalog() for the full
    list of action types and their params.

    The pipeline sees three common flags on every step:
      - probability         — chance this step runs (0..1)
      - skip_on_my_domain   — skip if ad.domain is in MY_DOMAINS
      - skip_on_target      — skip if ad.is_target (target domain)
    """
    from action_runner import run_pipeline

    pipeline = ON_TARGET_DOMAIN_ACTIONS if ad.get("is_target") \
               else POST_AD_ACTIONS
    if not pipeline:
        return

    run_pipeline(browser, pipeline, context={
        "ad":         ad,
        "my_domains": MY_DOMAINS,
    })


# ──────────────────────────────────────────────────────────────
# SEARCH LOOP
# ──────────────────────────────────────────────────────────────

def search_query(browser, query: str, sqm: SessionQualityMonitor,
                 current_ip: str = None) -> list[dict]:
    """
    Single query execution:
    - Open direct search URL
    - Refresh-loop until ads appear (or max attempts)
    - Parse & return ads
    """
    driver = browser.driver
    url = build_search_url(query)
    logging.info(f"🔎 {query}  →  {url}")

    try:
        browser.stealth_get(url, referer="https://www.google.com/")
    except Exception as e:
        logging.error(f"  navigation failed: {e}")
        return []

    _sleep("initial_load")

    for attempt in range(1, REFRESH_MAX_ATTEMPTS + 1):
        if is_offline_page(driver):
            logging.error("  ✗ offline page — proxy broken")
            return []

        # Captcha — the killer scenario. Don't retry, don't refresh
        # (that makes the captcha mark heavier). Report the IP as burned
        # so the rotator picks something else for next run.
        if is_captcha_page(driver):
            logging.error("  ✗ CAPTCHA — Google flagged this session")
            sqm.record("captcha", query=query, url=driver.current_url)
            if current_ip:
                browser.report_rotating(current_ip, success=False, captcha=True)
            return []

        if is_ads_preview_page(driver):
            logging.warning("  ✗ Ads Preview page (unexpected) — skipping")
            return []

        # Wait for SERP containers
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    "#search, #rso, [data-text-ad], #center_col"
                ))
            )
        except Exception:
            pass
        _sleep("serp_settle")

        ads = parse_ads(driver, query)
        if ads:
            logging.info(f"  ✓ {len(ads)} ads on attempt {attempt}")
            return ads

        if attempt >= REFRESH_MAX_ATTEMPTS:
            logging.info(f"  ✗ no ads after {attempt} attempts")
            return []

        wait_sec = random.uniform(REFRESH_MIN_SEC, REFRESH_MAX_SEC)
        logging.info(f"  ↻ attempt {attempt}/{REFRESH_MAX_ATTEMPTS} — refresh in {wait_sec:.0f}s")
        time.sleep(wait_sec)

        try:
            driver.refresh()
        except Exception as e:
            logging.warning(f"  refresh error: {e}")
            if not browser.is_alive():
                return []
            continue

        _sleep("post_refresh")

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

        # 6. Auto-rotate on start — gets a fresh random Ukrainian IP before
        # we touch Google. Skip if proxy is non-rotating (e.g. static ISP IP).
        current_ip = None
        auto_rotate = CFG.get("proxy.auto_rotate_on_start", True)
        if IS_ROTATING_PROXY:
            if auto_rotate:
                try:
                    log_step("rotating IP", "auto_rotate_on_start=True")
                    new_ip = browser.force_rotate_ip()
                    if new_ip:
                        current_ip = new_ip
                        logging.info(f"🌐 rotated to IP: {current_ip}")
                except Exception as e:
                    logging.warning(f"  auto-rotate failed: {e}")
            # Regardless of auto-rotate: check health + unburn stale IPs
            try:
                if current_ip is None:
                    current_ip = browser.check_and_rotate_if_burned()
                    if current_ip:
                        logging.info(f"🌐 working with IP: {current_ip}")
            except Exception as e:
                logging.debug(f"  rotation check: {e}")

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

            for i, query in enumerate(SEARCH_QUERIES, 1):
                dog.heartbeat()

                if not browser.is_alive():
                    logging.warning("⚠ Chrome window was closed — stopping run")
                    break

                t0 = time.time()
                try:
                    ads = search_query(browser, query, sqm,
                                       current_ip=current_ip)
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
                            run_post_ad_pipeline(browser, ad)

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
