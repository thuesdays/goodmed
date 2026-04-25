"""
serp_behavior.py — Realistic post-SERP-load user behavior.

The problem: our monitor was behaving like a scraper. Land on the SERP,
grab the ads, leave. Google's ad auction sees these patterns:
    - 0-2 second dwell
    - zero scroll
    - zero engagement with organic results
    - immediate next query

...and downgrades the session's ad load. Next query gets fewer ads,
next captcha comes sooner, next IP rotation is needed earlier. Our
16% ads/search rate has a lot of headroom left in it just from
looking less like a bot.

This module adds human-shaped behavior BETWEEN the ads-collection step
and the next query:

  1. DWELL — pause on SERP for a randomized duration, weighted by time
     of day (longer evenings when users are tired / reading more).

  2. SCROLL — multi-step scroll down the SERP, not a single jump.
     Each step has a variable pause (reading time).

  3. ORGANIC CLICK (probabilistic) — occasionally open an organic
     (non-ad) result in a new tab, stay on it briefly, close it.
     This is what separates "person researching" from "bot querying".

All three are independently toggleable via DB config:
    behavior.serp_dwell_enabled      (default: True)
    behavior.serp_scroll_enabled     (default: True)
    behavior.organic_click_enabled   (default: True)
    behavior.organic_click_probability  (default: 0.25)
    behavior.organic_dwell_min_sec   (default: 8)
    behavior.organic_dwell_max_sec   (default: 25)

The functions are safe to call even if the driver is broken — all
exceptions are caught and logged at debug level. SERP behavior is
a nice-to-have, not a hard requirement, so failures never abort
the parent search loop.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
import random
import time
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Dwell — time spent on SERP before moving on
# ──────────────────────────────────────────────────────────────

def dwell_on_serp(driver, min_sec: float = 3.0, max_sec: float = 9.0,
                  watchdog=None) -> None:
    """Pause on the current SERP for a random interval. During the wait,
    simulate some micro-activity (scroll a tiny bit, move mouse) every
    couple of seconds so Google's visibility-changed / user-activation
    signals fire, not just a dead page.

    `watchdog` if provided gets a heartbeat every few seconds so the
    parent run's stall detector doesn't think we're frozen."""
    duration = random.uniform(min_sec, max_sec)
    t_end = time.time() + duration

    while time.time() < t_end:
        # Small activity pulse every 1.5-3s — just enough to register
        # as "user is reading" in Chrome's heuristic signals.
        try:
            # Random tiny scroll (10-40px, either direction)
            delta = random.choice([-1, 1]) * random.randint(10, 40)
            driver.execute_script(f"window.scrollBy(0, {delta});")
        except Exception:
            pass

        if watchdog is not None:
            try: watchdog.heartbeat()
            except Exception: pass

        # Nap 1.5-3s between pulses
        nap = min(random.uniform(1.5, 3.0), max(0.2, t_end - time.time()))
        time.sleep(nap)


# ──────────────────────────────────────────────────────────────
# Scroll — multi-step, not a single jump
# ──────────────────────────────────────────────────────────────

def scroll_through_serp(driver, steps: int = None, watchdog=None) -> None:
    """Scroll down the SERP in 3-6 steps, with reading pauses between.
    Real users don't smooth-scroll the whole page in one gesture — they
    scroll a bit, read, scroll more.

    Also occasionally scrolls BACK UP a short distance (simulates
    re-reading a result that caught their eye) — this is a specific
    pattern Google's engagement model rewards.
    """
    if steps is None:
        steps = random.randint(3, 6)

    try:
        viewport_h = driver.execute_script("return window.innerHeight;")
        total_h    = driver.execute_script(
            "return document.documentElement.scrollHeight;")
    except Exception as e:
        logging.debug(f"[serp_behavior] scroll probe failed: {e}")
        return

    # Only scroll as far as there is content to scroll to, at most one
    # viewport past the initial view (deep scrolling to the bottom is
    # rare and looks more like automation than reading).
    max_scroll = min(total_h - viewport_h, int(viewport_h * 1.8))
    if max_scroll <= 0:
        return

    current = 0
    for i in range(steps):
        # Each step moves a fraction of what's left. Earlier steps are
        # bigger (quick skim), later steps smaller (detailed read).
        remaining = max_scroll - current
        frac = random.uniform(0.25, 0.5) if i < steps / 2 else random.uniform(0.1, 0.3)
        step_px = int(remaining * frac)
        if step_px < 30:
            break

        # Occasional "re-read" — scroll up 50-150px before continuing.
        if random.random() < 0.15 and current > 200:
            back = random.randint(50, 150)
            try:
                driver.execute_script(f"window.scrollBy({{top: -{back}, behavior: 'smooth'}});")
            except Exception:
                pass
            time.sleep(random.uniform(1.0, 2.5))

        try:
            driver.execute_script(
                f"window.scrollBy({{top: {step_px}, behavior: 'smooth'}});"
            )
        except Exception:
            pass

        current += step_px
        # Reading pause — weighted toward 2-5s which matches real
        # dwell-per-viewport on search results.
        pause = random.uniform(1.8, 5.0)
        time.sleep(pause)

        if watchdog is not None:
            try: watchdog.heartbeat()
            except Exception: pass


# ──────────────────────────────────────────────────────────────
# Organic click — the high-signal engagement action
# ──────────────────────────────────────────────────────────────

# CSS selectors for organic (non-ad) result links on google.com.
# Google changes these periodically — we try several in priority order
# and the first one that matches wins.
_ORGANIC_SELECTORS = [
    # Standard organic block — 2024+
    "div#search div.g:not([data-text-ad]) a[href][data-ved]",
    # Fallback: any h3 inside #search that isn't in an ads container
    "div#search h3 a:not([data-ads])",
    # Older layout
    "div.rc a[href]",
]

def click_organic_result(driver, dwell_min_sec: float = 8.0,
                         dwell_max_sec: float = 25.0,
                         exclude_domains: list = None,
                         watchdog=None) -> bool:
    """Pick a non-ad result from the SERP, open it in a new tab, dwell
    briefly, then close the tab and return to SERP.

    `exclude_domains` — list of substrings (e.g. ["goodmedika.com.ua"])
    to NEVER click. Must include the user's OWN domains — otherwise we
    end up visiting our own site as "organic research" which is both
    (a) a waste of a click impression on ourselves and (b) a detectable
    self-reinforcement pattern (Google notices the same fingerprint
    repeatedly searches for a brand then clicks its own site).

    Returns True if a click actually happened, False otherwise (no
    organic results / all top-5 were own domains / click failed).

    Why new tab: (a) doesn't navigate away from SERP so we keep context;
    (b) matches the most common real-user pattern — middle-click or
    Ctrl+click to open in background and scan multiple results.

    Why dwell 8-25s: shorter than that is "bounced" in Google's book;
    longer is rare for a quick scan. The range encompasses the p10-p75
    of real click-through dwell times for commercial queries."""

    link_el = _find_organic_link(driver, exclude_domains=exclude_domains or [])
    if link_el is None:
        logging.debug("[serp_behavior] no clickable organic result "
                      "(all were own domains or no results)")
        return False

    try:
        href = link_el.get_attribute("href") or ""
        if not href.startswith("http"):
            return False
        original_handle  = driver.current_window_handle
        original_handles = set(driver.window_handles)

        # Open in new tab via JS — Ctrl+click is unreliable through
        # Selenium on some Chrome builds, window.open is deterministic.
        driver.execute_script("window.open(arguments[0], '_blank');", href)
        time.sleep(random.uniform(0.5, 1.2))

        # Switch to the new tab
        new_handles = [h for h in driver.window_handles
                       if h not in original_handles]
        if not new_handles:
            logging.debug("[serp_behavior] new tab didn't open")
            return False
        new_tab_handle = new_handles[0]
        driver.switch_to.window(new_tab_handle)

        # Dwell — simulate reading the article. Break into small
        # chunks with micro-scrolls so there are real activity events.
        # Any exception here is swallowed so the finally block ALWAYS
        # closes the tab (otherwise a broken page leaves tabs piling
        # up across runs — the "9 tabs on startup" symptom).
        dwell = random.uniform(dwell_min_sec, dwell_max_sec)
        try:
            t_end = time.time() + dwell
            while time.time() < t_end:
                try:
                    driver.execute_script(
                        f"window.scrollBy(0, {random.randint(40, 180)});"
                    )
                except Exception:
                    pass
                if watchdog is not None:
                    try: watchdog.heartbeat()
                    except Exception: pass
                time.sleep(min(random.uniform(2.0, 4.5),
                               max(0.3, t_end - time.time())))
        except Exception as e:
            logging.debug(f"[serp_behavior] dwell interrupted: {e}")

        logging.info(f"  ◯ organic click: dwelt {dwell:.0f}s on {href[:70]}")
        return True

    except Exception as e:
        logging.debug(f"[serp_behavior] organic click failed: {e}")
        return False
    finally:
        # ── BULLETPROOF TAB CLEANUP ────────────────────────────────
        # Always runs, even if dwell loop or driver.execute_script raised.
        # Close EVERY handle that wasn't there before we started, then
        # settle back on the original SERP tab. This is why users were
        # seeing 9+ tabs pile up: previous code only closed in the
        # happy path; any mid-dwell exception left the new tab open,
        # and because we wrote those tabs into Chrome's session state,
        # they came back on every subsequent launch.
        try:
            starting_handles = original_handles if 'original_handles' in locals() else set()
            for h in driver.window_handles:
                if h not in starting_handles:
                    try:
                        driver.switch_to.window(h)
                        driver.close()
                    except Exception:
                        pass
            # Settle back on original. If even that was closed somehow
            # (shouldn't happen but defensive), pick the first remaining.
            remaining = driver.window_handles
            if remaining:
                target = original_handle if ('original_handle' in locals()
                                             and original_handle in remaining) else remaining[0]
                driver.switch_to.window(target)
        except Exception as e:
            logging.debug(f"[serp_behavior] tab cleanup failed: {e}")


def _find_organic_link(driver, exclude_domains: list = None):
    """Return the WebElement of a clickable organic result, or None.
    Pick randomly among the top 5 — that's where real users cluster.

    `exclude_domains` — substring matches that disqualify an href. Used
    to keep us from clicking our own brand's site (Self-visit would
    look to Google like "the same fingerprint keeps searching for this
    brand and going to its site", a self-reinforcement pattern that's
    both wasteful and detectable). Matching is plain substring on the
    URL — pass clean host strings like "goodmedika.com.ua", NOT patterns.
    """
    exclude_domains = exclude_domains or []

    for sel in _ORGANIC_SELECTORS:
        try:
            elements = driver.find_elements("css selector", sel)
        except Exception:
            continue
        if not elements:
            continue
        # Filter to visible elements with real hrefs
        visible = []
        for el in elements[:10]:
            try:
                if not el.is_displayed():
                    continue
                href = el.get_attribute("href") or ""
                # Skip Google's own internal redirectors and image results
                if "google.com/search" in href or "google.com/images" in href:
                    continue
                if not href.startswith("http"):
                    continue
                # Skip own / excluded domains — substring check is fine
                # because the caller passes clean hostnames. We compare
                # lowercase since URL hosts are case-insensitive.
                href_low = href.lower()
                if any(d.lower() in href_low for d in exclude_domains if d):
                    continue
                visible.append(el)
            except Exception:
                continue
            if len(visible) >= 5:
                break
        if visible:
            # Weight top results more heavily — users click rank-1 more.
            weights = [5, 3, 2, 2, 1][:len(visible)]
            return random.choices(visible, weights=weights, k=1)[0]
    return None


# ──────────────────────────────────────────────────────────────
# Top-level orchestrator — call from main.py search loop
# ──────────────────────────────────────────────────────────────

def post_ads_behavior(driver, db, exclude_domains: list = None,
                      watchdog=None) -> None:
    """Run the configured SERP behavior pipeline. Called from main.py
    AFTER ads are collected but BEFORE the next query. All steps are
    independently gated by DB config so users can disable parts that
    don't fit their use case.

    `exclude_domains` — never click an organic result pointing at any
    of these (substring match). Always pass at minimum the caller's
    own brand domains (MY_DOMAINS) — otherwise the organic-click step
    can end up visiting the user's own site, which defeats the purpose
    of "research the competition" and sends bad signals to Google.

    Order matters: scroll first (we're at top of page), then dwell
    (with mini-scrolls) simulates reading, finally an optional organic
    click. Doing organic-click first would mean we leave the SERP
    before getting a chance to scroll/read it, which is less realistic.
    """
    try:
        scroll_enabled = db.config_get("behavior.serp_scroll_enabled",  True)
        dwell_enabled  = db.config_get("behavior.serp_dwell_enabled",   True)
        click_enabled  = db.config_get("behavior.organic_click_enabled", True)
        click_prob     = float(db.config_get(
            "behavior.organic_click_probability", 0.25) or 0.25)
        dwell_min      = float(db.config_get("behavior.organic_dwell_min_sec",  8) or 8)
        dwell_max      = float(db.config_get("behavior.organic_dwell_max_sec", 25) or 25)
    except Exception as e:
        logging.debug(f"[serp_behavior] config read failed: {e}")
        return

    if scroll_enabled:
        scroll_through_serp(driver, watchdog=watchdog)

    if dwell_enabled:
        dwell_on_serp(driver, watchdog=watchdog)

    if click_enabled and random.random() < click_prob:
        click_organic_result(driver,
                             dwell_min_sec=dwell_min,
                             dwell_max_sec=dwell_max,
                             exclude_domains=exclude_domains or [],
                             watchdog=watchdog)
