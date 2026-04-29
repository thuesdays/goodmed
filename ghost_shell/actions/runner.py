"""
action_runner.py — human-like action pipeline executor.

Runs a list of actions the user configured in the dashboard. Each
action simulates a realistic human interaction: curved mouse paths,
variable delays, scroll patterns with pauses, actual DOM clicks
(not window.open), etc.

──────────────────────────────────────────────────────────────────
Supported action types
──────────────────────────────────────────────────────────────────

  click_ad            Click the ad element itself (real click, not JS
                      navigate). Triggers Google's /aclk?sa=L tracker.
                      Opens in a new tab (Ctrl+Click). This is the
                      most realistic ad-click signal we can produce.

  click_selector      Click any element by CSS selector. Supports
                      probability, hover-before-click, and new-tab.

  visit               Navigate directly to a URL (no click). Use only
                      when you need to force a specific URL without a
                      click-origin — e.g. jumping straight to the
                      competitor home page. Less human-looking than
                      click_ad.

  hover               Move mouse over a selector and pause. Useful
                      before a click, or just to look like reading.

  move_random         Move mouse to a random position on the page
                      (curved path, variable speed).

  scroll              Scroll the page in human increments with
                      variable speed and occasional back-scrolls.

  read                Scroll-pause-scroll pattern that mimics reading:
                      small scroll → pause 2-5s → larger scroll → pause.

  type                Type text into an input element with per-char
                      delay (40-180 ms). No "paste".

  press_key           Send a single key (ENTER, ESCAPE, TAB, …).

  select_text         Drag-select some text (mouse-down, move, up).

  dwell               Just wait. min_sec..max_sec.

  random_delay        Shorter alias for dwell — "small" (1-3 s) or
                      "medium" (4-8 s) or "long" (10-20 s).

  scroll_to_bottom    Scroll all the way down in chunks with pauses.

  back                browser.back() with small delay.

  new_tab             Open blank new tab.

  close_tab           Close current tab, switch back to opener.

  switch_tab          Switch to the Nth tab (0 = first).

  wait_for            Wait until a selector appears. Times out gracefully.

──────────────────────────────────────────────────────────────────
Generic web automation (added for Facebook / Twitter / crypto /
scraping / QA — not tied to Google Ads)
──────────────────────────────────────────────────────────────────

  open_url            Alias of `visit` with a clearer name. Supports
                      {variable} substitution in the URL.

  fill_form           Type into one field (`selector`+`value`) or many
                      (`fields` JSON array). Human keystroke timing.

  extract_text        Pull element.text or an attribute into
                      ctx.vars[store_as]. Later steps reference it as
                      {store_as}.

  execute_js          Run arbitrary JS in the page context. Runs as a
                      function body — use `return` to send a value back
                      into ctx.vars[store_as]. Power tool; anti-bot
                      defenses can fingerprint automated JS calls.

  screenshot          Save a timestamped PNG to profiles/<n>/screenshots/.

  wait_for_url        Block until the browser URL contains a substring
                      (or matches a regex). Use after OAuth redirects,
                      form submits, SPA route changes.

──────────────────────────────────────────────────────────────────
Variable substitution
──────────────────────────────────────────────────────────────────

  Every string param in an action flows through _subst(template, ctx),
  which expands {var_name} placeholders. Variables come from three
  sources, in precedence order:

    1. ctx["vars"]    — written by extract_text / execute_js (store_as)
    2. ctx top-level  — {ad}, {query}, {profile_name}, {item}, {index}
    3. Literal match  — unknown {typo} is left as-is so errors are visible

  Dotted paths walk nested dicts: {ad.clean_url}, {profile.tags.0}.

  Example pipeline:

    [
      {"type": "open_url",     "url":      "https://shop.com/search?q=toys"},
      {"type": "extract_text", "selector": ".product-title", "store_as": "first_title"},
      {"type": "open_url",     "url":      "https://google.com/search?q={first_title}"}
    ]

──────────────────────────────────────────────────────────────────
Usage
──────────────────────────────────────────────────────────────────

    from ghost_shell.actions.runner import run_pipeline
    run_pipeline(browser, pipeline, context={"ad": ad})

Each action is a dict like:
    {
      "type": "click_ad",
      "enabled": true,
      "probability": 0.8,    # chance this step runs (0..1)
      # type-specific params below…
    }
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
import random
import re
import time
from typing import Any, Callable

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# HUMAN-LIKE MOUSE MOVEMENT
# ──────────────────────────────────────────────────────────────

def _human_move_to(driver, element, steps: int = 15):
    """
    Move the mouse to `element` along a curved path (Bezier-ish) with
    jitter and variable speed — NOT teleport.

    Selenium's ActionChains.move_to_element() teleports; real users
    traverse the screen. We fake that by chaining small move_by_offset()
    calls that approximate a curve from the current position to the
    target.
    """
    try:
        rect = element.rect
    except Exception:
        return
    target_x = rect["x"] + rect["width"]  / 2 + random.uniform(-8, 8)
    target_y = rect["y"] + rect["height"] / 2 + random.uniform(-4, 4)

    # Ask the browser for current cursor position — Selenium doesn't track
    # it, so we just start the chain from ActionChains current state
    # (which is the last place we moved to).
    ac = ActionChains(driver, duration=0)

    # Overshoot slightly then correct — humans do this
    overshoot_x = target_x + random.uniform(-20, 20)
    overshoot_y = target_y + random.uniform(-15, 15)
    last_x, last_y = 0, 0

    for i in range(1, steps + 1):
        t = i / steps
        # Bezier-ish curve — quadratic easing
        ease = t * t * (3 - 2 * t)
        if i < steps - 2:
            x = overshoot_x * ease
            y = overshoot_y * ease
        else:
            # Last two steps: correct onto actual target
            x = target_x
            y = target_y
        dx = x - last_x
        dy = y - last_y
        ac.move_by_offset(dx, dy)
        last_x, last_y = x, y
        # Micro-pause between steps — ~ 8-30 ms each
        ac.pause(random.uniform(0.008, 0.030))

    try:
        ac.perform()
    except Exception as e:
        log.debug(f"human_move_to failed: {e}")


def _random_sleep(lo: float, hi: float):
    time.sleep(random.uniform(lo, hi))


# ──────────────────────────────────────────────────────────────
# Watchdog interlock — protect long blocking ops
# ──────────────────────────────────────────────────────────────
#
# Audit footgun #2 mitigation. The watchdog probes driver.title every
# 30s and force-kills Chrome after 3 consecutive 20s-timeouts (see
# runtime.py WATCHDOG_*). Several action handlers do blocking
# operations 10-30s long without yielding to the watchdog, so on a
# slow proxy or heavy landing page the probe can fail mid-dwell and
# the run gets killed. Wrapping the blocking section in this context
# manager pauses the watchdog probe loop for the duration, then
# resumes it — ALWAYS via finally, so an exception inside the
# protected block doesn't leave the watchdog disabled forever.
#
# The browser handle lives at ctx.browser (RunContext) or
# loop_ctx["browser"] (legacy bridge). Both must support
# .watchdog_pause(reason)/.watchdog_resume(); if neither is reachable
# (legacy callers, tests), the wrapper degrades to a no-op.
class _WatchdogShield:
    def __init__(self, ctx_or_loopctx, reason: str):
        self.browser = None
        self.reason = reason or "action-handler"
        # Resolve browser from either RunContext (.browser attr) or
        # legacy loop_ctx dict ("browser" key) — both shapes appear
        # at different call-sites depending on dispatcher path.
        if ctx_or_loopctx is None:
            return
        b = getattr(ctx_or_loopctx, "browser", None)
        if b is None and isinstance(ctx_or_loopctx, dict):
            b = ctx_or_loopctx.get("browser")
        if b is None:
            # Legacy ctx dict carries loop_ctx → browser
            try:
                b = (ctx_or_loopctx.get("loop_ctx") or {}).get("browser")
            except Exception:
                b = None
        # Sanity: browser must have both watchdog_pause + resume methods
        if b is not None and hasattr(b, "watchdog_pause") and \
                hasattr(b, "watchdog_resume"):
            self.browser = b

    def __enter__(self):
        if self.browser is not None:
            try:
                self.browser.watchdog_pause(reason=self.reason)
            except Exception as e:
                log.debug(f"[watchdog-shield] pause failed: {e}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.browser is not None:
            try:
                self.browser.watchdog_resume()
            except Exception as e:
                log.debug(f"[watchdog-shield] resume failed: {e}")
        return False  # don't swallow exceptions


# ──────────────────────────────────────────────────────────────
# HUMAN-LIKE SCROLL
# ──────────────────────────────────────────────────────────────

def _human_scroll(driver, total_min: int = 300, total_max: int = 900,
                  with_backtracking: bool = True):
    """
    Scroll a variable amount (total px) in 2-6 chunks with pauses.
    Sometimes scrolls back up a bit (like re-reading something).
    """
    target_total = random.randint(total_min, total_max)
    scrolled = 0
    n_chunks = random.randint(2, 6)
    avg_chunk = target_total // n_chunks

    for i in range(n_chunks):
        # Chunk size varies around average
        delta = avg_chunk + random.randint(-80, 80)
        delta = max(80, delta)
        driver.execute_script(f"window.scrollBy({{top: {delta}, behavior: 'smooth'}});")
        scrolled += delta
        _random_sleep(0.4, 1.2)

        # Occasional back-scroll
        if with_backtracking and random.random() < 0.2 and i > 0:
            back = random.randint(50, 180)
            driver.execute_script(f"window.scrollBy({{top: -{back}, behavior: 'smooth'}});")
            _random_sleep(0.5, 1.5)


# ──────────────────────────────────────────────────────────────
# ACTION IMPLEMENTATIONS
# ──────────────────────────────────────────────────────────────

def _act_click_ad(driver, action: dict, ctx: dict):
    """Click the EXACT ad anchor that parse_ads returned.

    parse_ads stamps the chosen <a> with data-gs-ad-id during its scan.
    That attribute stays on the element until the page is rebuilt. We
    look up by that ID = 100% deterministic; we click exactly what the
    parser saw + approved, no "find any ad anchor" fallback that could
    grab a different card (that was the bug where click_ad would pick
    up the shopping carousel's first item = the user's own domain).

    ONLY if anchor_id lookup fails (page was reloaded between parse and
    click, shouldn't happen but defensively) do we fall back to URL
    fragment + own-domain filter. We NEVER silently grab "the first ad
    anchor" as a last resort — better to abort the click than click the
    wrong ad.
    """
    ad = ctx.get("ad") or {}
    anchor_id = ad.get("anchor_id") or ""
    own_domains = [d.lower() for d in (ctx.get("my_domains") or []) if d]

    def _href_is_own(href: str) -> bool:
        # Audit footgun #1 fix: use hostname-aware comparison instead
        # of substring `in`. Substring matched "a.com" against URLs
        # like "https://x.com/?ref=a.com" or "a.com.evil.example",
        # bailing on legitimate competitor clicks. Now: parse URL,
        # take hostname, do exact-or-subdomain match against each
        # own_domain.
        if not href:
            return False
        try:
            from urllib.parse import urlparse
            host = (urlparse(href).hostname or "").lower()
        except Exception:
            return False
        if not host:
            return False
        for d in own_domains:
            d = d.strip().lower()
            if not d:
                continue
            if host == d or host.endswith("." + d):
                return True
        return False

    anchor = None

    # ── PRIMARY: lookup by stamped anchor_id ────────────────────
    # This is the happy path. parse_ads already validated the domain
    # and stamped the right element.
    if anchor_id:
        try:
            anchor = driver.find_element(
                By.CSS_SELECTOR, f'a[data-gs-ad-id="{anchor_id}"]'
            )
        except Exception:
            anchor = None

    # ── FALLBACK 1: URL fragment match with own-domain guard ────
    # Reached if the stamped anchor vanished (page re-rendered,
    # Google dynamically swapped the ad block, etc). Still requires
    # own-domain verification on whatever we find.
    if anchor is None:
        url = ad.get("google_click_url") or ad.get("clean_url") or ""
        if url:
            try:
                candidate = driver.find_element(
                    By.CSS_SELECTOR, f"a[href*='{url[:80]}']"
                )
                cand_href = candidate.get_attribute("href") or ""
                if not _href_is_own(cand_href):
                    anchor = candidate
                    log.debug("    click_ad: anchor_id lookup failed, fell back to URL match")
                else:
                    log.info(f"    click_ad: URL-match fallback hit own domain, aborting")
            except Exception:
                pass

    # ── FALLBACK 2: domain match via JS scan ────────────────────
    # Last-resort fallback when both anchor_id AND URL fragment miss.
    # Use the stable `domain` field — Google's /aclk URLs include the
    # destination host as a query param (most modern templates have
    # `&adurl=https://...`). We do a JS-side scan across all anchors
    # whose href contains the domain string AND that go through
    # /aclk (so it's a real ad, not an organic result with the same
    # domain). Apply own-domain + maps + google-internal guards.
    # This catches the "Google rebuilt the SERP between parse and
    # click" case where data-gs-ad-id attributes were wiped.
    if anchor is None:
        ad_domain = (ad.get("domain") or "").lower().strip()
        if ad_domain:
            try:
                # Audit footgun #5: previously picked the FIRST anchor
                # matching the domain. If two ads from the same domain
                # rendered on the SERP (rare but possible — second-tier
                # PLA carousel + text ad), this could grab the WRONG
                # one. Now: prefer anchors that are NOT already stamped
                # with a data-gs-ad-id (those are claimed by other
                # planned clicks in this foreach_ad iteration), AND
                # use proper hostname matching (urlparse) instead of
                # substring `in` to avoid `?ref=domain.com` false hits.
                candidate = driver.execute_script(r"""
                    const dom = arguments[0].toLowerCase();
                    const own = arguments[1] || [];
                    const all = document.querySelectorAll('a[href]');

                    // Hostname match (audit footgun #1 + #5):
                    // exact-or-subdomain check via URL parsing, not
                    // substring `in` href.
                    function hostMatches(host, target) {
                        if (!host || !target) return false;
                        return host === target ||
                               host.endsWith('.' + target);
                    }
                    function hostOfHref(href) {
                        try { return new URL(href, location.origin).hostname.toLowerCase(); }
                        catch (e) { return ''; }
                    }
                    function isOwn(host) {
                        return own.some(d => hostMatches(host, (d || '').toLowerCase()));
                    }

                    // First pass: prefer candidates NOT already
                    // claimed by another planned click (no
                    // data-gs-ad-id). Second pass: anything goes.
                    function pick(predicate) {
                        for (const a of all) {
                            const href = (a.href || '').toLowerCase();
                            const host = hostOfHref(a.href || '');
                            if (!hostMatches(host, dom)) continue;
                            const isAd = href.includes('/aclk?') ||
                                         href.includes('googleadservices') ||
                                         href.includes('googlesyndication');
                            if (!isAd) continue;
                            if (isOwn(host)) continue;
                            if (href.includes('google.com/maps') ||
                                href.includes('maps.google.com')) continue;
                            if (!predicate(a)) continue;
                            return a;
                        }
                        return null;
                    }

                    return pick(a => !a.hasAttribute('data-gs-ad-id'))
                        || pick(_ => true);
                """, ad_domain, own_domains)
                if candidate:
                    anchor = candidate
                    log.info(
                        f"    click_ad: anchor_id+URL-fragment both missed; "
                        f"recovered via domain-match JS scan for {ad_domain!r}"
                    )
            except Exception as _ds_err:
                log.debug(f"    click_ad: domain-match scan failed: {_ds_err}")

    # ── FALLBACK 3 (Fix B, Apr 2026): full re-parse of the SERP ──
    # Reached when all three fallbacks above missed. Most common
    # cause: Google's PLA carousel re-rendered between parse_ads
    # and click_ad (lazy-load on scroll, dynamic refresh on
    # visibility change, or post_ads_behavior scrolling — Fix A
    # addresses the latter, but Google can do this on its own
    # any time too). Re-running parse_ads on the live DOM stamps
    # fresh data-gs-ad-id values; we then look up by domain.
    #
    # Cost: ~1.5s for the JS scan. Only paid when normal lookups
    # fail, so worst-case adds the cost to a click that would
    # otherwise have aborted with "couldn't locate ad anchor".
    # Strict: only matches when the re-parsed ad has the SAME
    # domain we expected — never silently grabs a different ad.
    if anchor is None:
        ad_domain = (ad.get("domain") or "").lower().strip()
        if ad_domain:
            try:
                # Lazy import — main.py registers itself in
                # sys.modules under both '__main__' and
                # 'ghost_shell.main' (alias near top of main.py)
                # so this is a cache hit, not a fresh execution.
                from ghost_shell.main import parse_ads as _reparse_ads
                # The re-parse re-stamps the DOM; we ignore its
                # return value's anchor_ids (the strings differ
                # from the parser's first scan since each scan
                # uses its own scanId prefix) and look up by
                # domain on the freshly-stamped document.
                fresh_ads = _reparse_ads(driver, ctx.get("query") or "") or []
                match = None
                for fa in fresh_ads:
                    fd = (fa.get("domain") or "").lower().strip()
                    if fd == ad_domain:
                        match = fa
                        break
                if match and match.get("anchor_id"):
                    new_aid = match["anchor_id"]
                    try:
                        anchor = driver.find_element(
                            By.CSS_SELECTOR,
                            f'a[data-gs-ad-id="{new_aid}"]'
                        )
                        log.info(
                            f"    click_ad: full re-parse recovered "
                            f"anchor for domain={ad_domain!r} "
                            f"(new anchor_id={new_aid!r}, original "
                            f"{anchor_id!r} was stale)"
                        )
                        # Update ad dict so post-click bookkeeping
                        # uses the fresh URL/clicks-through.
                        ad["anchor_id"] = new_aid
                        if match.get("clean_url"):
                            ad["clean_url"] = match["clean_url"]
                        if match.get("google_click_url"):
                            ad["google_click_url"] = match["google_click_url"]
                    except Exception as _fe:
                        log.debug(
                            f"    click_ad: re-parse stamped fresh "
                            f"id but find_element still missed: {_fe}"
                        )
                else:
                    log.debug(
                        f"    click_ad: re-parse found "
                        f"{len(fresh_ads)} ad(s) but none matched "
                        f"domain={ad_domain!r} — Google likely "
                        f"removed this ad from current SERP"
                    )
            except Exception as _re_err:
                log.debug(f"    click_ad: re-parse fallback failed: {_re_err}")

    if anchor is None:
        log.warning(
            f"    click_ad: couldn't locate ad anchor "
            f"(anchor_id='{anchor_id}', domain='{ad.get('domain','')}') — "
            f"refusing to guess. No click will happen."
        )
        return

    # Scroll element into view (humanly)
    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center', behavior:'smooth'});",
        anchor
    )
    _random_sleep(0.6, 1.3)

    # Move mouse along curve, then click with Ctrl (opens in new tab
    # while keeping SERP as current tab).
    _human_move_to(driver, anchor)
    _random_sleep(0.1, 0.4)

    original = driver.current_window_handle
    # Three-stage click ladder:
    #   1. ActionChains Ctrl-click (preferred — most realistic, opens
    #      in new tab via OS-level Ctrl modifier)
    #   2. Native element.click() (Selenium API)
    #   3. JS-side .click() (works even on 0×0 / hidden / absolute-
    #      positioned elements where Selenium throws
    #      ElementNotInteractableException — Google ad anchors are
    #      sometimes wrapped this way: the visible click target is a
    #      different element with the geometry, while the /aclk
    #      anchor itself has display:none-style positioning)
    # We force OPEN_IN_NEW_TAB at the JS layer by setting target=_blank
    # before .click(); the post-click logic below already expects a
    # new tab to appear and switch to it.
    click_ok = False
    try:
        ac = ActionChains(driver)
        ac.key_down(Keys.CONTROL).click(anchor).key_up(Keys.CONTROL).perform()
        click_ok = True
    except Exception as e:
        log.warning(
            f"    click_ad: ctrl-click failed "
            f"({type(e).__name__}: {str(e)[:120]}); "
            f"trying plain click"
        )
        try:
            anchor.click()
            click_ok = True
        except Exception as e2:
            log.warning(
                f"    click_ad: plain click failed "
                f"({type(e2).__name__}: {str(e2)[:120]}); "
                f"falling back to JS click"
            )
            try:
                # JS-side click — bypasses Selenium's interactability
                # geometry check. Force target=_blank first so the
                # tab-tracking code below picks up a new window.
                driver.execute_script(
                    "arguments[0].setAttribute('target', '_blank');"
                    "arguments[0].click();",
                    anchor,
                )
                click_ok = True
                log.info("    click_ad: JS click succeeded")
            except Exception as e3:
                log.warning(
                    f"    click_ad: JS click also failed "
                    f"({type(e3).__name__}: {str(e3)[:120]}); "
                    f"giving up on this ad"
                )
                return

    if not click_ok:
        return

    _random_sleep(1.0, 2.5)
    # Switch to the new tab if one opened. Wrap in try/except — the
    # tab can VANISH between handles enumeration and switch_to (Maps
    # redirects, popup-blocker close, antifraud heuristic close).
    tabs = []
    landed_url = ""
    try:
        tabs = [h for h in driver.window_handles if h != original]
    except Exception as _wh_err:
        log.warning(f"    click_ad: window_handles failed ({_wh_err}); aborting")
        return
    if tabs:
        try:
            driver.switch_to.window(tabs[-1])
            landed_url = driver.current_url or ""
        except Exception as _sw_err:
            log.warning(
                f"    click_ad: tab vanished before switch "
                f"({type(_sw_err).__name__}); returning to SERP"
            )
            try: driver.switch_to.window(original)
            except Exception: pass
            return
        log.info(f"    → clicked ad, now on: {landed_url[:80]}")

        # ── POST-CLICK SAFETY NET ──────────────────────────────────
        # Last line of defence: even with anchor_id deterministic match,
        # Google's /aclk redirect chain can occasionally land on an own
        # domain (rare — happens when a competitor's ad URL 302s through
        # our site as part of affiliate tracking, or when Google swaps
        # the landing page at serve time). If we end up on our own site,
        # close immediately — no dwell, no self-click cost.
        if _href_is_own(landed_url):
            log.warning(
                f"    click_ad: LANDED on own domain ({landed_url[:60]}) — "
                f"closing tab immediately without dwell"
            )
            try:
                driver.close()
            finally:
                try: driver.switch_to.window(original)
                except Exception: pass
            return

        # ── MAPS / GOOGLE-INTERNAL BAIL-OUT ────────────────────────
        # Local Pack ads have a "see on Google Maps" anchor that sits
        # inside the same ad container as the merchant link. parse_ads
        # sometimes stamps that maps anchor instead of the merchant
        # one — when we click it we land on google.com/maps/place,
        # which is (a) not a real ad click, (b) prone to closing its
        # own tab via maps' embedded behaviour, causing
        # NoSuchWindowException further in the dwell loop.
        # Same goes for any other google-internal landing
        # (search-redirect chains, AMP viewer fallback, etc).
        # Closing immediately keeps the SERP clean and the next
        # foreach_ad iteration from running on a dead window.
        low_url = landed_url.lower()
        if ("google.com/maps" in low_url or
            "maps.google.com"  in low_url or
            "google.com/url?"  in low_url or
            "google.com/aclk"  in low_url and "redirect" in low_url):
            log.warning(
                f"    click_ad: landed on google-internal page "
                f"({landed_url[:60]}) — not a real ad destination; "
                f"closing tab without dwell"
            )
            try:
                driver.close()
            finally:
                try: driver.switch_to.window(original)
                except Exception: pass
            return

    # Dwell — wrap so a tab close mid-sleep doesn't propagate as a
    # crash. NoSuchWindowException can come from chrome auto-closing
    # the tab during a redirect chain on the destination site (some
    # antifraud setups serve a JS-driven `window.close()` to bots).
    dwell_lo = float(action.get("dwell_min", 6))
    dwell_hi = float(action.get("dwell_max", 18))
    # Audit footgun #2: dwell can be up to 18s, watchdog probes every
    # 30s — without pausing, two consecutive probes can fall during
    # one dwell on a flaky proxy and trigger a kill mid-action. Pause
    # for the duration; auto-resume via finally inside _WatchdogShield.
    with _WatchdogShield(ctx, reason="click_ad-dwell"):
        try:
            _random_sleep(dwell_lo, dwell_hi)
        except Exception:
            pass

    # Audit footgun #6: captcha on click_ad landing site previously
    # didn't update ctx.flags["captcha_present"] — only search_query
    # / catch_ads did. So a script with `if captcha_present ->
    # rotate_ip` after click_ad would never trigger. Refresh the
    # flag now that we've finished the dwell on the landing page.
    # Try-context: ctx may be a RunContext (unified) or a legacy
    # dict. Both shapes support .flags reading via getattr/dict.
    try:
        from ghost_shell.main import is_captcha_page
        captcha_now = bool(is_captcha_page(driver))
        flags = getattr(ctx, "flags", None)
        if flags is None and isinstance(ctx, dict):
            flags = ctx.get("flags") or ctx.setdefault("flags", {})
        if isinstance(flags, dict):
            flags["captcha_present"] = captcha_now
        if captcha_now:
            log.warning(
                f"    click_ad: CAPTCHA detected on landing site — "
                f"ctx.flags['captcha_present']=True so subsequent "
                f"`if captcha_present -> rotate_ip` steps will fire"
            )
    except Exception as _cf_err:
        log.debug(f"    click_ad: captcha refresh skipped: {_cf_err}")

    # Mid-dwell health-probe: if the window vanished while sleeping,
    # bail out cleanly so the parent foreach_ad picks up the next ad.
    try:
        _ = driver.current_url
    except Exception as _hp_err:
        log.warning(
            f"    click_ad: window closed during dwell "
            f"({type(_hp_err).__name__}); returning to SERP"
        )
        try: driver.switch_to.window(original)
        except Exception: pass
        return

    # Optional post-click scroll — also protected.
    if action.get("scroll_after_click", True):
        try:
            _human_scroll(driver)
        except Exception:
            pass
        try:
            _random_sleep(1, 3)
        except Exception:
            pass

    # ── DEEP DIVE: click 1-2 internal links ──────────────────────
    # A real shopper who clicks an ad doesn't just scroll the
    # landing page and leave -- they click into a product, look at
    # the catalog, maybe check shipping. This pattern is one of
    # Google's strongest "engaged user" signals (CTR-prediction
    # gets a noticeable bump). Same-host links only -- we don't
    # want to wander off into Facebook share buttons or back to
    # Google.
    if action.get("deep_dive", False) and tabs:
        try:
            depth_min = int(action.get("depth_min", 1))
            depth_max = int(action.get("depth_max", 2))
            inner_lo  = float(action.get("inner_dwell_min", 5))
            inner_hi  = float(action.get("inner_dwell_max", 12))
            depth     = random.randint(depth_min, max(depth_min, depth_max))
            for step_i in range(1, depth + 1):
                # Find a same-host internal link via JS. Skip nav,
                # footer, and social (heuristic: prefer links inside
                # main/article/[role=main] / product-card containers).
                clicked_href = driver.execute_script(r"""
                const here = location.hostname;
                const candidates = [];
                // Prefer "content" containers; fall back to any <a>
                const containers = document.querySelectorAll(
                    'main a, article a, [role="main"] a, ' +
                    '[class*="product"] a, [class*="catalog"] a, ' +
                    '[class*="card"] a, .container a'
                );
                const all = containers.length ? containers
                                              : document.querySelectorAll('a[href]');
                for (const a of all) {
                    if (!a.href || !a.href.startsWith('http')) continue;
                    let host;
                    try { host = new URL(a.href).hostname; }
                    catch { continue; }
                    // Same host (or subdomain of it)
                    if (host !== here && !host.endsWith('.' + here) &&
                        !here.endsWith('.' + host)) continue;
                    // Skip obvious nav / utility / share links
                    const txt = (a.innerText || '').toLowerCase();
                    if (!txt || txt.length < 2 || txt.length > 80) continue;
                    if (/^(home|main|menu|cart|корзин|меню|логин|search|пошук|войти|вийти)$/.test(txt)) continue;
                    if (a.href.includes('/cart') || a.href.includes('/checkout') ||
                        a.href.includes('/auth') || a.href.includes('mailto:') ||
                        a.href.includes('tel:') || a.href.includes('#')) continue;
                    // Candidate looks reasonable
                    candidates.push(a);
                }
                if (!candidates.length) return null;
                // Random pick from top 8 -- top of catalog usually
                // most relevant
                const pool = candidates.slice(0, 8);
                const pick = pool[Math.floor(Math.random() * pool.length)];
                pick.scrollIntoView({block: 'center', behavior: 'smooth'});
                // Strip target=_blank to keep navigation in-tab
                pick.removeAttribute('target');
                pick.click();
                return pick.href;
                """)
                if not clicked_href:
                    log.debug(f"    deep_dive[{step_i}/{depth}]: no good internal link -- stopping")
                    break
                log.info(f"    ↪ deep_dive[{step_i}/{depth}]: → {clicked_href[:80]}")
                _random_sleep(inner_lo, inner_hi)

                # Audit footgun #8: post-click hostname verification.
                # The JS picker filters by location.hostname BEFORE
                # clicking, but the click itself can still navigate
                # off-host if the link target uses iframe-window
                # tricks, server-side 302 to another domain, or the
                # picker picked a link inside a 3rd-party iframe
                # that we mis-attributed to the main frame's host.
                # If we ended up off the original landing host,
                # bail out of deep_dive — we don't want to add
                # browsing-context to Facebook / Twitter / wherever.
                try:
                    from urllib.parse import urlparse
                    cur_host = urlparse(driver.current_url or "").hostname or ""
                    pick_host = urlparse(clicked_href or "").hostname or ""
                    landing_host = urlparse(landed_url or "").hostname or ""
                    if cur_host and landing_host and \
                       cur_host != landing_host and \
                       not cur_host.endswith("." + landing_host) and \
                       not landing_host.endswith("." + cur_host):
                        log.warning(
                            f"    ↪ deep_dive[{step_i}/{depth}]: "
                            f"redirect/iframe sent us off-host "
                            f"({landing_host} → {cur_host}); "
                            f"stopping deep_dive"
                        )
                        break
                except Exception:
                    pass

                # Light scroll on the inner page
                try:
                    _human_scroll(driver)
                except Exception:
                    pass
                _random_sleep(0.8, 2.0)
        except Exception as e:
            log.debug(f"    deep_dive failed: {e}")

    # Close tab and return to SERP. Both the close() and the
    # subsequent switch_to.window() can fail if the tab is already
    # gone (chrome closed it itself, popup-blocker, deep_dive
    # navigated us off-host into a closing iframe). We MUST
    # successfully restore focus to the SERP either way — otherwise
    # the next foreach_ad iteration runs against a dead window.
    if action.get("close_after", True) and tabs:
        try:
            driver.close()
        except Exception:
            pass
        try:
            driver.switch_to.window(original)
        except Exception:
            # Last-ditch: try the first surviving handle if `original`
            # is also gone. The watchdog will catch this and force a
            # driver-level recovery if we can't find anything.
            try:
                handles = driver.window_handles
                if handles:
                    driver.switch_to.window(handles[0])
            except Exception:
                pass


def _resolve_selector(sel: str):
    """Translate a user-friendly selector string into a (By, value)
    pair the Selenium API accepts.

    Sprint 9 (TPL-01): templates inherited from a Playwright-style
    syntax that includes ``:has-text('foo')`` and ``text:foo`` — these
    are NOT valid CSS selectors. Selenium ≥ 4.18 raises
    ``InvalidSelectorException`` on them. We rewrite the common
    forms into XPath so the same templates work without per-template
    edits:

      ``xpath=//div``               → (By.XPATH, '//div')
      ``xpath://div``               → (By.XPATH, '//div')
      ``text:Foo``                  → (By.XPATH, "//*[contains(., 'Foo')]")
      ``button:has-text('Foo')``    → (By.XPATH,
                                       "//button[contains(., 'Foo')]")
      ``a, b:has-text('X')``        → first non-`:has-text` branch as
                                       CSS; if all branches contain
                                       it, the whole thing converts
                                       to a chained XPath OR.

    Falls back to ``By.CSS_SELECTOR`` for anything else."""
    if not sel:
        return (By.CSS_SELECTOR, sel)
    s = sel.strip()
    if s.startswith("xpath="):
        return (By.XPATH, s[len("xpath="):])
    if s.startswith("xpath:"):
        return (By.XPATH, s[len("xpath:"):])
    if s.startswith("text:"):
        text = s[len("text:"):].strip().strip("'").strip('"')
        # XPath 1.0 has no escape — single quotes inside the text
        # break the literal. Swap to double-quote literal if needed.
        quote = '"' if "'" in text else "'"
        return (By.XPATH, f"//*[contains(., {quote}{text}{quote})]")
    if ":has-text(" in s:
        # Try to keep the CSS branches that DON'T use :has-text and
        # discard the others. If everything uses it, fall back to a
        # chained XPath OR over the tag-prefixed branches.
        parts = [p.strip() for p in s.split(",")]
        css_parts = [p for p in parts if ":has-text(" not in p]
        if css_parts:
            return (By.CSS_SELECTOR, ", ".join(css_parts))
        # Build XPath OR from each :has-text branch.
        xpath_branches = []
        import re as _re
        for p in parts:
            m = _re.match(r"^([a-zA-Z][\w-]*)?\s*:has-text\(\s*['\"](.+?)['\"]\s*\)\s*$", p)
            if not m:
                continue
            tag = m.group(1) or "*"
            text = m.group(2)
            quote = '"' if "'" in text else "'"
            xpath_branches.append(
                f"//{tag}[contains(., {quote}{text}{quote})]"
            )
        if xpath_branches:
            return (By.XPATH, " | ".join(xpath_branches))
        # Couldn't parse — pass through as CSS and let it fail loudly.
        return (By.CSS_SELECTOR, sel)
    return (By.CSS_SELECTOR, sel)


def _act_click_selector(driver, action: dict, ctx: dict):
    """Click any element by CSS selector with human mouse movement."""
    sel = action.get("selector")
    if not sel:
        log.warning("    click_selector: no selector given")
        return

    by, value = _resolve_selector(sel)
    try:
        el = driver.find_element(by, value)
    except Exception:
        log.warning(f"    click_selector: not found: {sel}")
        return

    driver.execute_script(
        "arguments[0].scrollIntoView({block:'center'});", el
    )
    _random_sleep(0.3, 1.0)
    _human_move_to(driver, el)
    _random_sleep(0.1, 0.3)

    new_tab = bool(action.get("new_tab", False))
    try:
        if new_tab:
            ActionChains(driver).key_down(Keys.CONTROL).click(el)\
                .key_up(Keys.CONTROL).perform()
        else:
            el.click()
        log.info(f"    → clicked: {sel}")
    except Exception as e:
        log.warning(f"    click_selector failed: {e}")


def _act_visit(driver, action: dict, ctx: dict):
    """Navigate directly to a URL. Either in a new tab (default) or the
    current one.

    Own-domain protection: if the target URL matches any of our own
    domains (from ctx.my_domains), abort before navigating. This is
    the last stop for accidental self-visits. Note `visit` typically
    uses the ad's click URL from ctx, so if parse_ads already filtered
    own domains out, this guard is redundant — but defence-in-depth.
    """
    ad  = ctx.get("ad") or {}
    url = action.get("url") or ad.get("google_click_url") or ad.get("clean_url")
    if not url:
        return

    own_domains = [d.lower() for d in (ctx.get("my_domains") or []) if d]
    if own_domains and any(d in url.lower() for d in own_domains):
        log.info(f"    visit: skipping own-domain URL ({url[:60]})")
        return

    new_tab = bool(action.get("new_tab", True))
    log.info(f"    → visit {url[:80]}")

    if new_tab:
        original = driver.current_window_handle
        original_handles = set(driver.window_handles)
        driver.execute_script(f"window.open(arguments[0], '_blank');", url)
        _random_sleep(1.0, 2.0)
        new_handles = [h for h in driver.window_handles
                       if h not in original_handles]
        if not new_handles:
            log.debug("    visit: new tab didn't open")
            return

        try:
            driver.switch_to.window(new_handles[-1])

            # Landing URL check — same rationale as click_ad's
            # post-click guard: a URL that looked safe in ad metadata
            # can 302 onto an own domain.
            landed = driver.current_url or ""
            if own_domains and any(d in landed.lower() for d in own_domains):
                log.warning(
                    f"    visit: LANDED on own domain ({landed[:60]}), "
                    f"closing tab immediately"
                )
                return

            dwell_lo = float(action.get("dwell_min", 5))
            dwell_hi = float(action.get("dwell_max", 15))
            _random_sleep(dwell_lo, dwell_hi)
        except Exception as e:
            log.debug(f"    visit: dwell raised: {e}")
        finally:
            # Bulletproof cleanup — ALWAYS close tabs we opened, even
            # if the dwell loop crashed. Without this, any mid-dwell
            # exception (page crash, target site's JS blew up) leaked
            # a tab. Those tabs then persist into Chrome's session
            # state and re-open on every subsequent run (the "9 tabs
            # stacking up" symptom).
            if action.get("close_after", True):
                try:
                    for h in driver.window_handles:
                        if h not in original_handles:
                            try:
                                driver.switch_to.window(h)
                                driver.close()
                            except Exception:
                                pass
                    if original in driver.window_handles:
                        driver.switch_to.window(original)
                    elif driver.window_handles:
                        driver.switch_to.window(driver.window_handles[0])
                except Exception as e:
                    log.debug(f"    visit: cleanup failed: {e}")
    else:
        driver.get(url)
        dwell_lo = float(action.get("dwell_min", 5))
        dwell_hi = float(action.get("dwell_max", 15))
        _random_sleep(dwell_lo, dwell_hi)


def _act_hover(driver, action: dict, ctx: dict):
    """Hover over a selector."""
    sel = action.get("selector")
    if not sel: return
    try:
        el = driver.find_element(By.CSS_SELECTOR, sel)
    except Exception:
        log.debug(f"    hover: not found: {sel}")
        return
    _human_move_to(driver, el)
    pause_lo = float(action.get("hold_min", 0.5))
    pause_hi = float(action.get("hold_max", 2.0))
    _random_sleep(pause_lo, pause_hi)
    log.info(f"    → hovered: {sel}")


def _act_move_random(driver, action: dict, ctx: dict):
    """Move mouse to random coords inside viewport."""
    try:
        w = driver.execute_script("return window.innerWidth;")
        h = driver.execute_script("return window.innerHeight;")
    except Exception:
        return
    # Create an invisible temporary div at the target position so we
    # can reuse _human_move_to's curve logic.
    target_x = random.randint(50, max(51, w - 50))
    target_y = random.randint(50, max(51, h - 50))
    driver.execute_script(f"""
        var d = document.createElement('div');
        d.id = '__gs_cursor_target__';
        d.style.cssText = 'position:fixed;width:1px;height:1px;' +
            'left:{target_x}px;top:{target_y}px;pointer-events:none;';
        document.body.appendChild(d);
    """)
    try:
        el = driver.find_element(By.CSS_SELECTOR, "#__gs_cursor_target__")
        _human_move_to(driver, el)
    finally:
        driver.execute_script(
            "var d=document.getElementById('__gs_cursor_target__');"
            "if(d) d.remove();"
        )


def _act_scroll(driver, action: dict, ctx: dict):
    """Human scroll."""
    total_min = int(action.get("min_px", 300))
    total_max = int(action.get("max_px", 900))
    log.info(f"    → scroll {total_min}..{total_max}px")
    _human_scroll(driver, total_min, total_max,
                  with_backtracking=action.get("backtracking", True))


def _act_read(driver, action: dict, ctx: dict):
    """Reading pattern: small scroll → pause 2-5s → repeat 3-6 times."""
    n_paragraphs = random.randint(
        int(action.get("min_paragraphs", 3)),
        int(action.get("max_paragraphs", 6))
    )
    log.info(f"    → read {n_paragraphs} 'paragraphs'")
    for _ in range(n_paragraphs):
        chunk = random.randint(120, 280)
        driver.execute_script(
            f"window.scrollBy({{top: {chunk}, behavior: 'smooth'}});"
        )
        _random_sleep(
            float(action.get("pause_min", 2.0)),
            float(action.get("pause_max", 5.5))
        )


def _act_type(driver, action: dict, ctx: dict):
    """Type text char-by-char with realistic per-key delay."""
    sel  = action.get("selector")
    text = action.get("text", "")
    if not sel or not text: return
    by, value = _resolve_selector(sel)
    try:
        el = driver.find_element(by, value)
    except Exception:
        log.warning(f"    type: selector not found: {sel}")
        return
    _human_move_to(driver, el)
    el.click()
    _random_sleep(0.2, 0.6)
    for ch in text:
        el.send_keys(ch)
        # Realistic typing cadence — 40-180 ms between chars,
        # occasional longer pause for "thinking"
        delay = random.uniform(0.04, 0.18)
        if random.random() < 0.08:
            delay += random.uniform(0.25, 0.7)
        time.sleep(delay)
    log.info(f"    → typed: {text[:40]}...")


def _act_press_key(driver, action: dict, ctx: dict):
    """Send a single key to the currently focused element."""
    key_name = (action.get("key") or "").upper()
    key = getattr(Keys, key_name, None)
    if key is None:
        log.warning(f"    press_key: unknown key {key_name}")
        return
    try:
        ActionChains(driver).send_keys(key).perform()
        log.info(f"    → key: {key_name}")
    except Exception as e:
        log.warning(f"    press_key failed: {e}")


def _act_dwell(driver, action: dict, ctx: dict):
    lo = float(action.get("min_sec", 2))
    hi = float(action.get("max_sec", 6))
    t = random.uniform(lo, hi)
    log.info(f"    → dwell {t:.1f}s")
    time.sleep(t)


def _act_random_delay(driver, action: dict, ctx: dict):
    size = action.get("size", "medium")
    ranges = {
        "tiny":   (0.3, 1.0),
        "small":  (1.0, 3.0),
        "medium": (4.0, 8.0),
        "long":   (10.0, 20.0),
    }
    lo, hi = ranges.get(size, (2.0, 5.0))
    t = random.uniform(lo, hi)
    log.info(f"    → delay ({size}) {t:.1f}s")
    time.sleep(t)


def _act_scroll_to_bottom(driver, action: dict, ctx: dict):
    log.info("    → scrolling to bottom")
    last_y = 0
    for _ in range(30):   # safety cap
        y = driver.execute_script("return window.scrollY + window.innerHeight;")
        height = driver.execute_script("return document.body.scrollHeight;")
        if y >= height - 10:
            break
        driver.execute_script(
            f"window.scrollBy({{top: {random.randint(400, 800)}, "
            f"behavior: 'smooth'}});"
        )
        _random_sleep(0.6, 1.3)
        if y == last_y:
            break
        last_y = y


def _act_back(driver, action: dict, ctx: dict):
    delay = float(action.get("delay_sec", 1))
    log.info(f"    → back (delay {delay}s)")
    time.sleep(delay)
    driver.back()


def _act_new_tab(driver, action: dict, ctx: dict):
    driver.execute_script("window.open('about:blank', '_blank');")
    _random_sleep(0.5, 1.0)
    driver.switch_to.window(driver.window_handles[-1])
    log.info("    → opened new tab")


def _act_close_tab(driver, action: dict, ctx: dict):
    if len(driver.window_handles) <= 1:
        log.warning("    close_tab: only one tab — skipping")
        return
    driver.close()
    driver.switch_to.window(driver.window_handles[0])
    log.info("    → closed tab")


def _act_switch_tab(driver, action: dict, ctx: dict):
    idx = int(action.get("index", 0))
    if idx >= len(driver.window_handles):
        log.warning(f"    switch_tab: index {idx} out of range")
        return
    driver.switch_to.window(driver.window_handles[idx])
    log.info(f"    → switched to tab {idx}")


def _act_wait_for(driver, action: dict, ctx: dict):
    sel     = action.get("selector")
    # Templates use both `timeout` and `timeout_sec` interchangeably —
    # honour either rather than silently defaulting to 10s when the
    # template author wrote `timeout: 30`.
    timeout = float(action.get("timeout_sec", action.get("timeout", 10)))
    if not sel: return
    by, value = _resolve_selector(sel)
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((by, value))
        )
        log.info(f"    → wait_for: {sel} appeared")
    except Exception:
        log.warning(f"    wait_for: {sel} didn't appear in {timeout}s")


# ──────────────────────────────────────────────────────────────
# DISPATCH TABLE + RUNNER
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# VARIABLE SUBSTITUTION
#
# Scripts can reference values collected during a run via {var_name}
# placeholders in string parameters. For example:
#
#     {"type": "extract_text", "selector": "h1", "store_as": "title"}
#     {"type": "visit",        "url": "https://google.com/search?q={title}"}
#
# The `ctx` dict is threaded through every action — we pull vars out of
# ctx["vars"] and fall back to top-level ctx keys (so loop-scoped vars
# like {item}/{index} still work without duplication).
# ──────────────────────────────────────────────────────────────

import re
_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")

# Extra deps used by the generic-automation actions below.
# Kept here (rather than at file top) to localize the diff; action_runner
# already imports re, random, time — screenshot needs os+datetime.
import os as _os_for_actions  # noqa: E402
from datetime import datetime as _dt_for_actions  # noqa: E402
# Alias so we don't shadow the top-level `datetime` module the rest of
# the file might already import indirectly.
os = _os_for_actions
datetime = _dt_for_actions

def _subst(template, ctx: dict):
    """Expand {var_name} placeholders against ctx. Non-string inputs
    are returned unchanged (caller shouldn't have to care about types).

    Dotted paths (`ctx.ad.clean_url`) are resolved through nested dicts.
    Missing vars are left as-is — gives a visible '{typo}' in logs
    rather than silently producing empty strings.
    """
    if not isinstance(template, str):
        return template

    variables = dict(ctx or {})
    # ctx["vars"] has priority over top-level ctx — explicit wins
    if isinstance(variables.get("vars"), dict):
        variables = {**variables, **variables["vars"]}

    def _lookup(path: str):
        # Dotted path: walk dicts
        parts = path.split(".")
        cur = variables
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    def _replace(m):
        val = _lookup(m.group(1))
        return str(val) if val is not None else m.group(0)

    return _VAR_PATTERN.sub(_replace, template)


def _store_var(ctx: dict, name: str, value) -> None:
    """Write an extracted value into the script's variable scope.
    Always creates ctx['vars'] if missing — keeps action handlers
    from having to check."""
    if not name:
        return
    if not isinstance(ctx.get("vars"), dict):
        ctx["vars"] = {}
    ctx["vars"][name] = value


# ──────────────────────────────────────────────────────────────
# GENERIC WEB AUTOMATION ACTIONS
#
# These are the actions we hand to power-users building non-Google-Ads
# scripts: Facebook multi-account, Twitter posting, crypto trading,
# scraping, QA. Each handler does ONE thing and composes cleanly.
# ──────────────────────────────────────────────────────────────

def _act_open_url(driver, action: dict, ctx: dict):
    """Navigate to a URL with optional variable substitution.
    Alias of `visit` but with a clearer name for non-ads scripts.

    Supported dwell params (in priority order):
      dwell_min/dwell_max — pick a random dwell from the range,
        useful when the destination is a slow-loading tester or
        single-page app that runs background work after onload.
      wait_after          — fixed-ish dwell (legacy default 1.0s).
    """
    url = _subst(action.get("url", ""), ctx)
    if not url:
        log.warning("    open_url: no url given")
        return
    log.info(f"    → open_url: {url}")
    driver.get(url)
    if "dwell_min" in action or "dwell_max" in action:
        lo = float(action.get("dwell_min", 4))
        hi = float(action.get("dwell_max", 12))
        if hi < lo:
            hi = lo
        _random_sleep(lo, hi)
        # Optional human scroll while dwelling
        if action.get("scroll"):
            try:
                steps = int(action.get("scroll_steps", 4))
                for _ in range(max(1, steps)):
                    driver.execute_script(
                        "window.scrollBy(0, Math.floor("
                        " (document.body.scrollHeight || 800) "
                        "/ arguments[0]));", steps)
                    time.sleep(random.uniform(0.6, 1.4))
            except Exception as e:
                log.debug(f"    open_url scroll failed: {e}")
    else:
        wait_sec = float(action.get("wait_after", 1.0))
        _random_sleep(wait_sec, wait_sec + 0.8)


# ──────────────────────────────────────────────────────────────
# External fingerprint tester — visit + dwell + extract + persist.
# One action handles all of CreepJS / Pixelscan / Sannysoft / etc.
# Dispatches the right extractor based on the tester_id.
# ──────────────────────────────────────────────────────────────

# Per-tester JS extractors. Each returns a JSON-serialisable dict
# with whatever stats it can pull off the page after the scan finishes.
# Extractors run inside the browser via execute_script — keep them
# defensive (try/catch around every selector, return null fields when
# absent) so a future redesign of the tester page degrades gracefully.

_EXTRACTOR_CREEPJS = r"""
return (() => {
  const out = { tester_id: "creepjs", trust: null, fp_id: null,
                lies: [], errors: [], summary: "" };
  try {
    const trustEl = document.querySelector(
      ".trusted-fingerprint, .untrusted-fingerprint, .unrustworthy-fingerprint");
    if (trustEl) {
      const m = trustEl.textContent.match(/([\d.]+)\s*%/);
      if (m) out.trust = parseFloat(m[1]);
    }
    const fpIdEl = document.querySelector(".fingerprint-header .unblurred");
    if (fpIdEl) out.fp_id = fpIdEl.textContent.trim().slice(0, 80);
    document.querySelectorAll(".lies-detection").forEach(el => {
      out.lies.push(el.textContent.trim().slice(0, 200));
    });
    document.querySelectorAll(".unblurred.erratic, .warn, .perf").forEach(el => {
      const t = el.textContent.trim();
      if (t && t.length < 500) out.errors.push(t);
    });
    const main = document.querySelector(".fingerprint-header");
    out.summary = main ? main.textContent.trim().slice(0, 1000) : "";
  } catch (e) { out.error = String(e); }
  return out;
})();
"""

_EXTRACTOR_SANNYSOFT = r"""
return (() => {
  const out = { tester_id: "sannysoft", checks: {}, fail_count: 0 };
  try {
    document.querySelectorAll("table tr").forEach(tr => {
      const cells = tr.querySelectorAll("td, th");
      if (cells.length >= 2) {
        const k = cells[0].textContent.trim();
        const v = cells[1].textContent.trim();
        if (k && v) out.checks[k] = v;
        const cls = (cells[1].className || "").toLowerCase();
        if (cls.includes("failed") || cls.includes("not")) out.fail_count++;
      }
    });
  } catch (e) { out.error = String(e); }
  return out;
})();
"""

_EXTRACTOR_PIXELSCAN = r"""
return (() => {
  const out = { tester_id: "pixelscan", verdict: null, summary: "" };
  try {
    const verdictEl = document.querySelector(
      "[class*='verdict'], [class*='result'], h1, h2");
    if (verdictEl) out.verdict = verdictEl.textContent.trim().slice(0, 200);
    out.summary = document.body.innerText.slice(0, 800);
  } catch (e) { out.error = String(e); }
  return out;
})();
"""

_EXTRACTOR_GENERIC = r"""
return (() => {
  return {
    tester_id: arguments[0] || "unknown",
    title: document.title,
    summary: (document.body.innerText || "").slice(0, 800),
  };
})();
"""

_EXTRACTORS = {
    "creepjs":      _EXTRACTOR_CREEPJS,
    "sannysoft":    _EXTRACTOR_SANNYSOFT,
    "pixelscan":    _EXTRACTOR_PIXELSCAN,
    # The remaining testers don't expose a single trust score that's
    # easy to scrape stably — fall back to title + body excerpt.
    "amiunique":    _EXTRACTOR_GENERIC,
    "browserleaks": _EXTRACTOR_GENERIC,
    "fpcom":        _EXTRACTOR_GENERIC,
}


def _act_visit_external_fp_tester(driver, action: dict, ctx: dict):
    """Navigate to one external fingerprint tester, dwell while it
    scans, extract whatever stats are exposed on the page, and write
    them to the `external_fp_results` table.

    Action params:
      tester_id     str (required) — canonical key from FP_TESTERS
                    (creepjs / sannysoft / pixelscan / amiunique /
                     browserleaks / fpcom)
      url           str — override (defaults to the canonical URL
                    embedded in the dashboard catalogue)
      tester_label  str — pretty label for the dashboard
      dwell_min/max — seconds to wait after navigation while the
                    tester runs its async scan (CreepJS needs ~15-25s
                    to compute its trust score)
      scroll        bool — scroll while dwelling
    """
    import json as _json
    tester_id    = action.get("tester_id") or "unknown"
    tester_label = action.get("tester_label") or tester_id
    url          = action.get("url") or ""
    dwell_lo     = float(action.get("dwell_min", 18))
    dwell_hi     = float(action.get("dwell_max", 32))

    log.info(f"    → fp-tester: {tester_label} ({tester_id})")

    if not url:
        log.warning(f"    fp-tester '{tester_id}': no url, skipping")
        return

    extractor = _EXTRACTORS.get(tester_id, _EXTRACTOR_GENERIC)

    # Audit footgun #4: fingerprint testers (creepjs, pixelscan,
    # browserleaks) have notoriously slow JS bundles — 10-30s+ before
    # the page completes. Without a per-action page_load_timeout the
    # navigation inherits Chrome's default 300s, and the runner thread
    # can block 5 minutes on a single tester visit. Cap at 45s — the
    # tester either renders enough by then OR the proxy is too slow
    # for this site, in which case we'd rather give up and continue.
    nav_error = None
    _previous_pageload = None
    try:
        try:
            _previous_pageload = 60  # restored below regardless
            driver.set_page_load_timeout(45)
        except Exception:
            pass
        try:
            driver.get(url)
        except Exception as e:
            nav_error = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning(f"    fp-tester '{tester_id}' navigation failed "
                        f"(45s cap, slow tester / blocked proxy): {nav_error}")
            try: driver.execute_script("window.stop();")
            except Exception: pass
    finally:
        # Restore default-ish page-load timeout so subsequent
        # actions in the flow inherit normal behavior.
        try:
            driver.set_page_load_timeout(_previous_pageload or 60)
        except Exception:
            pass

    # Dwell while the tester's JS computes scores. Some testers (CreepJS)
    # show a partial result quickly then refine it — we still wait the
    # full window so the saved score is the final one. Wrap in
    # watchdog shield (footgun #2) — dwell is 18-32s by default, well
    # over the 30s probe window.
    if not nav_error:
        with _WatchdogShield(ctx, reason=f"fp-tester-dwell-{tester_id}"):
            _random_sleep(dwell_lo, dwell_hi)
        if action.get("scroll"):
            try:
                for _ in range(4):
                    driver.execute_script(
                        "window.scrollBy(0, Math.floor("
                        "(document.body.scrollHeight || 800)/4));")
                    time.sleep(random.uniform(0.4, 1.0))
                driver.execute_script("window.scrollTo(0, 0);")
            except Exception:
                pass

    payload = None
    extract_error = None
    if not nav_error:
        try:
            payload = driver.execute_script(extractor, tester_id) or {}
        except Exception as e:
            extract_error = f"{type(e).__name__}: {str(e)[:200]}"
            log.warning(
                f"    fp-tester '{tester_id}' extractor failed: {extract_error}"
            )

    # Persist regardless — even an error row is useful so the UI can
    # show "last attempt failed at HH:MM" instead of staying blank.
    try:
        from ghost_shell.db.database import get_db
        db = get_db()
        trust = None
        fp_id = None
        lies_count = None
        summary = None
        raw_json = None
        if payload is not None:
            trust = payload.get("trust")
            fp_id = payload.get("fp_id")
            if isinstance(payload.get("lies"), list):
                lies_count = len(payload["lies"])
            summary = (payload.get("summary") or "")[:1000]
            try:
                raw_json = _json.dumps(payload, ensure_ascii=False)[:8000]
            except Exception:
                raw_json = None
        db.external_fp_add(
            run_id=ctx.get("run_id"),
            profile_name=ctx.get("profile_name") or "unknown",
            tester_id=tester_id,
            tester_label=tester_label,
            url=url,
            trust_score=trust,
            fingerprint_id=fp_id,
            lies_count=lies_count,
            summary=summary,
            raw_json=raw_json,
            error=nav_error or extract_error,
        )
        if trust is not None:
            log.info(
                f"    ✓ fp-tester '{tester_id}': "
                f"trust={trust}% lies={lies_count or 0}"
            )
        else:
            log.info(
                f"    ✓ fp-tester '{tester_id}': result saved"
                + (f" error={nav_error or extract_error}"
                   if (nav_error or extract_error) else "")
            )
    except Exception as e:
        log.warning(f"    fp-tester '{tester_id}' DB persist failed: {e}")


def _close_extra_tabs(driver, anchor_handle: str | None = None) -> int:
    """Close every tab EXCEPT the anchor (or the first surviving tab
    if anchor is gone). Returns count of tabs closed.

    Designed for cleanup after multi-step actions like
    commercial_inflate where Google's organic results / popups can
    spawn target=_blank tabs that the user never wanted. Without
    this, every pre-query potentially leaks 1-3 background tabs;
    after a 30-iteration run the browser drowns in 60+ tabs and
    eventually crashes on RAM.

    Safe to call even when only one tab exists (no-op).
    """
    closed = 0
    try:
        all_handles = list(driver.window_handles)
    except Exception:
        return 0
    if len(all_handles) <= 1:
        return 0
    keep = anchor_handle if anchor_handle in all_handles else all_handles[0]
    for h in all_handles:
        if h == keep:
            continue
        try:
            driver.switch_to.window(h)
            driver.close()
            closed += 1
        except Exception:
            pass
    # Restore focus to the kept tab
    try:
        if keep in driver.window_handles:
            driver.switch_to.window(keep)
        elif driver.window_handles:
            driver.switch_to.window(driver.window_handles[0])
    except Exception:
        pass
    return closed


def _act_commercial_inflate(driver, action: dict, ctx: dict):
    """Pre-warm Google's commercial-intent context for the next brand
    search. Visits N short generic commercial queries (no brand
    reference) WITHIN THE SAME BROWSER SESSION so the subsequent
    brand SERP comes back denser with ads.

    Why this works: Google's ad ranker reads "recent commercial-intent
    queries on this session" as one of the strongest expected-CTR
    signals. A brand-only query right after 2-3 commercial searches
    in the same vertical gets 2-5x more ads served than a brand query
    on a cold session.

    Action params:
      brand        : str (required) -- the brand the inflater should
                     warm up for. Drives category detection.
      n            : int (default 2) -- how many pre-queries to fire.
      dwell_min    : float (default 8) seconds to dwell on each SERP
      dwell_max    : float (default 15)
      locale       : "UA" | "RU" | "EN" | combos (default "UA")
      click_organic: bool (default False) -- click first organic
                     result on each pre-SERP for extra signal.

    Variable substitution applies to brand so {ad.domain} or {item}
    work for use inside foreach_ad/loop steps. Non-fatal: a failed
    pre-query never aborts the parent flow.

    TAB HYGIENE: this action only navigates the CURRENT tab via
    driver.get(...) but Google's organic results often have
    target="_blank" attributes -- when click_organic=true, the click
    can spawn a new tab that survives back-navigation. Plus some pre-
    query SERPs themselves cause popups. We snapshot the anchor tab
    at start and call _close_extra_tabs() between iterations + at the
    end, so the parent flow gets back exactly the same tab it gave
    us, no leftovers.
    """
    brand = _subst(str(action.get("brand", "")), ctx).strip()
    if not brand:
        # Try to infer from the loop's current item
        loop_item = ctx.get("vars", {}).get("item") or ""
        brand = str(loop_item).strip()
    if not brand:
        log.warning("    commercial_inflate: no brand provided + no loop item -- skip")
        return

    n         = int(action.get("n", 2))
    dwell_min = float(action.get("dwell_min", 8))
    dwell_max = float(action.get("dwell_max", 15))
    locale    = str(action.get("locale", "UA"))
    click_org = bool(action.get("click_organic", False))

    try:
        from ghost_shell.actions.query_expander import (
            commercial_inflate_queries, detect_category,
        )
    except Exception as e:
        log.warning(f"    commercial_inflate: importer failed: {e}")
        return

    cat = detect_category(brand) or "(uncategorised)"
    queries = commercial_inflate_queries(brand, n_pre=n, locale=locale)
    log.info(
        f"    🔥 commercial_inflate for {brand!r} (category={cat}): "
        f"{n} pre-queries"
    )

    # Snapshot the anchor tab. Everything we open beyond this gets
    # closed at the end so the parent flow (search_query, foreach_ad,
    # etc.) finds the browser in exactly the same shape we received
    # it -- single tab, focused on whatever was last navigated.
    try:
        anchor = driver.current_window_handle
    except Exception:
        anchor = None

    # Pre-flight TCP-reachability check. Sends a quick HEAD to
    # google.com through the same proxy via `requests` (NOT the browser),
    # 4-second timeout. Two purposes:
    #
    # 1. Proxy-burned guard. If google times out / returns 5xx through
    #    requests, the browser will hang on driver.get for 30s × N
    #    inflate queries. We'd rather skip inflate entirely and let
    #    search_query (with its own 15s timeout + captcha→rotation
    #    Recovery #1-4 flow) take over.
    # 2. Latency baseline. If google responds in < 1s through requests
    #    but Chrome still hangs, that points to a Chrome/CDP-level issue
    #    rather than a proxy problem.
    try:
        from ghost_shell.config import Config as _Cfg
        _proxy_url = (_Cfg.load().get("proxy.url") or "").strip()
    except Exception:
        _proxy_url = ""
    if _proxy_url:
        try:
            import requests as _rq
            _proxies = {"http":  f"http://{_proxy_url}",
                        "https": f"http://{_proxy_url}"}
            _t0 = time.time()
            _r = _rq.head("https://www.google.com/",
                          proxies=_proxies, timeout=4,
                          allow_redirects=False)
            log.info(
                f"      pre-flight google.com via proxy: "
                f"HTTP {_r.status_code} in {time.time()-_t0:.2f}s"
            )
            if _r.status_code in (407, 502, 503, 504):
                log.warning(
                    f"      ⚠ proxy returns {_r.status_code} for google — "
                    f"SKIPPING inflate, proceeding directly to search_query"
                )
                return
        except Exception as _pe:
            log.warning(
                f"      ⚠ pre-flight to google.com failed via proxy "
                f"({type(_pe).__name__}: {str(_pe)[:80]}). Skipping "
                f"inflate so we don't hang the run; search_query will "
                f"trigger captcha-rotation if needed."
            )
            return

    # Per-call pageload cap. Without this, a sluggish proxy + Google
    # captcha-state combo turns each inflate query into a 300-second
    # block (Chrome's default page_load_timeout) and wedges the run.
    # 15s on top of pre-flight: pre-flight already proved google is
    # reachable; if Chrome still can't open the page in 15s, give up.
    try:
        driver.set_page_load_timeout(15)
    except Exception:
        pass

    import urllib.parse as _up
    from selenium.common.exceptions import TimeoutException as _TimeoutExc
    # Audit footgun #2: commercial_inflate per-query is page_load_timeout
    # (15s) + dwell (8-15s) + scrolls (~1s) + optional organic click +
    # tab cleanup. Total per-query 30-60s, multiplied by n=2-3 queries.
    # On a slow proxy this comfortably exceeds the watchdog's 30s probe
    # window. Pause watchdog for the duration of the loop so a slow
    # google.com pageload doesn't get the run killed mid-warmup.
    with _WatchdogShield(ctx, reason="commercial_inflate-loop"):
     for i, q in enumerate(queries, 1):
        try:
            url = "https://www.google.com/search?q=" + _up.quote(q)
            log.info(f"      [{i}/{n}] inflate query: {q!r}")
            try:
                driver.get(url)
            except _TimeoutExc:
                # Google didn't finish loading within 30s — common on
                # captcha'd / sluggish proxies. We have SOME of the
                # SERP rendered which is enough to look like a real
                # commercial-intent visit. Stop further subresources
                # so the next query starts clean, then continue.
                log.warning(
                    f"      [{i}/{n}] inflate query timed out after 30s "
                    f"on slow proxy; continuing"
                )
                try: driver.execute_script("window.stop();")
                except Exception: pass
            _random_sleep(dwell_min, dwell_max)
            # Soft-scroll a bit so Google sees engagement on the SERP
            try:
                driver.execute_script(
                    "window.scrollBy({top: 400, left: 0, behavior: 'smooth'});"
                )
                _random_sleep(0.5, 1.2)
                driver.execute_script(
                    "window.scrollBy({top: 300, left: 0, behavior: 'smooth'});"
                )
                _random_sleep(0.4, 1.0)
            except Exception:
                pass
            # Optional: click first organic result. We use simple
            # heuristic -- first <a> inside a <div.g> or [data-hveid]
            # block whose href is not google.com. Modern Google often
            # sets target="_blank" on these so the click spawns a new
            # tab; we tear it down right after dwell.
            if click_org:
                try:
                    pre_handles = set(driver.window_handles)
                    js = r"""
                    const links = document.querySelectorAll(
                        'div.g a[href^="http"], div[data-hveid] a[href^="http"]');
                    for (const a of links) {
                        if (a.href && !a.href.includes('google.com') &&
                            !a.href.includes('googleadservices') &&
                            !a.href.includes('youtube.com/watch')) {
                            a.scrollIntoView({block: 'center'});
                            a.click();
                            return a.href;
                        }
                    }
                    return null;
                    """
                    clicked = driver.execute_script(js)
                    if clicked:
                        log.info(f"        ↳ clicked organic: {clicked[:80]}")
                        # If a new tab opened, switch + dwell + close.
                        # Otherwise we're still on the SERP / same tab
                        # and just need to navigate back.
                        post_handles = set(driver.window_handles)
                        new_tabs = post_handles - pre_handles
                        if new_tabs:
                            try:
                                driver.switch_to.window(next(iter(new_tabs)))
                            except Exception:
                                pass
                            _random_sleep(4, 8)
                            try:
                                driver.execute_script(
                                    "window.scrollBy({top: 600, left: 0, behavior: 'smooth'});"
                                )
                                _random_sleep(1, 3)
                            except Exception:
                                pass
                            try:
                                driver.close()
                            except Exception:
                                pass
                            # Return to the anchor explicitly
                            if anchor and anchor in driver.window_handles:
                                driver.switch_to.window(anchor)
                            elif driver.window_handles:
                                driver.switch_to.window(driver.window_handles[0])
                        else:
                            _random_sleep(4, 8)
                            try: driver.back()
                            except Exception: pass
                            _random_sleep(1, 2)
                except Exception as e:
                    log.debug(f"        organic click skipped: {e}")

            # Per-iteration tab cleanup: any rogue popups / target=
            # _blank ads/affiliate overlays that spawned during the
            # dwell get nuked here. Very common on shopping sites and
            # ad-heavy pre-query SERPs.
            try:
                killed = _close_extra_tabs(driver, anchor)
                if killed:
                    log.debug(f"        cleaned {killed} stray tab(s)")
            except Exception:
                pass

        except Exception as e:
            log.warning(f"    commercial_inflate query {i} failed: {e}")

    # Final sweep at the end so the main search_query that follows
    # gets a clean single-tab session.
    try:
        killed = _close_extra_tabs(driver, anchor)
        if killed:
            log.info(f"    cleaned up {killed} extra tab(s) before continuing")
    except Exception:
        pass

    log.info(f"    ✓ commercial_inflate complete -- main query should see denser ads")


def _act_fill_form(driver, action: dict, ctx: dict):
    """Type into one or more form fields. Accepts either:
      {selector: "...", value: "..."}  — single field
      {fields: [{selector, value, clear_first?}, ...]} — many
    Values run through variable substitution, so {email} etc work.
    """
    fields = action.get("fields")
    if fields is None and action.get("selector"):
        fields = [{
            "selector":    action["selector"],
            "value":       action.get("value", ""),
            "clear_first": action.get("clear_first", True),
        }]

    if not fields:
        log.warning("    fill_form: no fields given")
        return

    for f in fields:
        sel = f.get("selector")
        val = _subst(f.get("value", ""), ctx)
        if not sel:
            continue
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
        except Exception:
            log.warning(f"    fill_form: not found: {sel}")
            continue
        try:
            if f.get("clear_first", True):
                el.clear()
            # Human-ish typing — small random delay per character
            for ch in str(val):
                el.send_keys(ch)
                time.sleep(random.uniform(0.03, 0.12))
            log.info(f"    → filled {sel} ({len(str(val))} chars)")
        except Exception as e:
            log.warning(f"    fill_form failed for {sel}: {e}")
        _random_sleep(0.15, 0.45)


def _act_extract_text(driver, action: dict, ctx: dict):
    """Pull text from an element and store it in ctx['vars'][store_as].
    Use `attribute` to pull an attribute instead of .text.
    """
    sel = action.get("selector")
    store_as = action.get("store_as") or "last_extract"
    attr = action.get("attribute")
    if not sel:
        log.warning("    extract_text: no selector")
        return
    try:
        el = driver.find_element(By.CSS_SELECTOR, sel)
    except Exception:
        log.warning(f"    extract_text: not found: {sel}")
        _store_var(ctx, store_as, None)
        return

    try:
        value = el.get_attribute(attr) if attr else (el.text or "")
        _store_var(ctx, store_as, value)
        preview = (value or "")[:80].replace("\n", " ")
        log.info(f"    → extract_text [{store_as}] = {preview!r}")
    except Exception as e:
        log.warning(f"    extract_text failed: {e}")
        _store_var(ctx, store_as, None)


def _act_execute_js(driver, action: dict, ctx: dict):
    """Run arbitrary JavaScript. Supports variable substitution in the
    code. Return value is stored in ctx['vars'][store_as] if set.
    Power tool — use sparingly, and be aware that anti-bot defenses
    can fingerprint automated JS execution."""
    code = _subst(action.get("code") or "", ctx)
    store_as = action.get("store_as")
    if not code:
        log.warning("    execute_js: no code given")
        return
    try:
        result = driver.execute_script(code)
        if store_as:
            _store_var(ctx, store_as, result)
        preview = str(result)[:80] if result is not None else "<no return>"
        log.info(f"    → execute_js ok ({len(code)} chars) — {preview!r}")
    except Exception as e:
        log.warning(f"    execute_js failed: {e}")


def _act_health_check_canary(driver, action: dict, ctx: dict):
    """Sprint 4 — visit fingerprint detection sites, score the result,
    save to profile_health. Sprint 6 — added every_n_runs gate.

    Action params:
      sites: list of site IDs (default ["sannysoft"]). Valid:
             "sannysoft", "creepjs", "pixelscan".
      timeout: navigation timeout per site in seconds (default 20).
      settle_sec: wait time after onload before scraping (default 2).
      every_n_runs: only run the canary every Nth finished run for
        this profile. 0 / None = run every time (default). When set,
        falls back to scheduler.canary_every_n_runs config if action
        doesn't override. Lets script authors drop one canary step
        into a per-ad pipeline without spamming detection sites on
        every iteration.

    Best-effort: each site visit + parse is independent — one failure
    doesn't skip the rest. Results land in profile_health table; the
    dashboard renders sparklines + drift alerts off that data.

    Does NOT affect the surrounding script's main outcome — never
    raises out, returns None on full failure (logged)."""
    sites = action.get("sites") or ["sannysoft"]
    if isinstance(sites, str):
        sites = [s.strip() for s in sites.split(",") if s.strip()]
    # RC-82: safe-parse — caller-provided strings shouldn't crash
    # the action runner on type errors.
    def _safe_float(v, default):
        try: return float(v) if v not in (None, "") else default
        except (TypeError, ValueError): return default
    def _safe_int(v, default):
        try: return int(v) if v not in (None, "") else default
        except (TypeError, ValueError): return default
    timeout      = _safe_float(action.get("timeout"),    20.0)
    settle_sec   = _safe_float(action.get("settle_sec"),  2.0)
    every_n_runs = _safe_int(action.get("every_n_runs"),  0)

    profile_name = ctx.get("profile_name") or "unknown"

    # Sprint 6: every-N-runs gate. Action param wins; fall back to
    # global scheduler config. 0 / None = no gate (run every time).
    try:
        from ghost_shell.db.database import get_db
        db_check = get_db()
    except Exception as e:
        log.debug(f"[health_canary] DB unavailable for gate: {e}")
        db_check = None

    if every_n_runs <= 0 and db_check is not None:
        try:
            cfg_n = db_check.config_get("scheduler.canary_every_n_runs")
            if cfg_n is not None:
                every_n_runs = _safe_int(cfg_n, 0)
        except Exception:
            pass

    if every_n_runs > 0 and db_check is not None:
        try:
            count = db_check.runs_count_for_profile(profile_name,
                                                    only_finished=True)
            # Run when count is divisible by N. Count is finished runs
            # only — the IN-FLIGHT current run isn't counted yet, so
            # this fires on the iteration AFTER an Nth finish.
            if count > 0 and (count % every_n_runs) != 0:
                log.debug(
                    f"[health_canary] gate skip: {count} finished runs "
                    f"for {profile_name!r}, every_n_runs={every_n_runs} "
                    f"({count % every_n_runs} mod {every_n_runs})"
                )
                return None
            elif count == 0:
                # First-ever run — go ahead, establish baseline
                log.debug(
                    f"[health_canary] gate pass (first run): "
                    f"every_n_runs={every_n_runs}"
                )
        except Exception as e:
            log.debug(f"[health_canary] gate check failed: {e}; running anyway")

    try:
        from ghost_shell.profile.health_canary import run_canary
        results = run_canary(driver, sites=sites,
                             navigation_timeout=timeout,
                             settle_sec=settle_sec)
    except Exception as e:
        log.warning(f"[health_canary] orchestrator crashed: {e}")
        return None

    # Persist each site result to profile_health
    profile_name = ctx.get("profile_name") or "unknown"
    run_id       = ctx.get("run_id")
    db = db_check  # reuse handle from gate check above

    saved = 0
    for r in results or []:
        site = r.get("site") or "?"
        if db is not None:
            try:
                db.profile_health_save(
                    profile_name = profile_name,
                    site         = site,
                    run_id       = run_id,
                    score        = r.get("score"),
                    raw_score    = r.get("raw_score"),
                    passed       = r.get("passed"),
                    total        = r.get("total"),
                    details      = r.get("details"),
                    error        = r.get("error"),
                )
                saved += 1
            except Exception as e:
                log.debug(f"[health_canary] save({site}) failed: {e}")
        # Per-site log line so the user sees progress in tail mode
        if r.get("error"):
            log.warning(
                f"[health_canary] {site}: probe failed — {r['error']}"
            )
        else:
            log.info(
                f"[health_canary] {site}: score={r.get('score')} "
                f"raw={r.get('raw_score')!r}"
            )
    log.info(f"[health_canary] saved {saved}/{len(results or [])} site results")


def _act_screenshot(driver, action: dict, ctx: dict):
    """Save a screenshot to profiles/<n>/screenshots/<name>.png.
    The filename gets a timestamp suffix automatically to avoid clobbering
    prior shots."""
    name = _subst(action.get("name") or "shot", ctx)
    profile_dir = ctx.get("profile_dir") or "profiles/_shared"
    shot_dir = os.path.join(profile_dir, "screenshots")
    os.makedirs(shot_dir, exist_ok=True)
    # Sanitize filename — strip path separators and weird chars
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] or "shot"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(shot_dir, f"{safe}_{ts}.png")
    try:
        driver.save_screenshot(path)
        log.info(f"    → screenshot saved: {path}")
        _store_var(ctx, "last_screenshot_path", path)
    except Exception as e:
        log.warning(f"    screenshot failed: {e}")


def _act_wait_for_url(driver, action: dict, ctx: dict):
    """Pause until the browser URL matches a substring or regex.
    Useful after OAuth redirects, form submits, SPA route changes.

    Audit footgun #3: previously the polling loop did `current_url`
    every 0.4s for up to 15s. If timeout was raised by user beyond
    30s (the watchdog probe interval), and current_url calls hung
    due to CDP backlog, the watchdog could fail probes during the
    wait and kill Chrome. Now: timeouts >25s are wrapped in
    _WatchdogShield to suspend probing for the duration. Short
    waits (<25s) stay unprotected since the watchdog can't accumulate
    3 fails in that window.
    """
    target = _subst(action.get("contains") or action.get("regex") or "", ctx)
    if not target:
        log.warning("    wait_for_url: no pattern given")
        return
    timeout = float(action.get("timeout", 15.0))
    use_regex = bool(action.get("regex"))

    pattern = re.compile(target) if use_regex else None

    def _poll_loop():
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                cur = driver.current_url or ""
            except Exception:
                cur = ""
            if use_regex and pattern.search(cur):
                log.info(f"    → url matched: {cur}")
                return True
            if not use_regex and target in cur:
                log.info(f"    → url matched: {cur}")
                return True
            time.sleep(0.4)
        return False

    if timeout > 25.0:
        with _WatchdogShield(ctx, reason="wait_for_url-long"):
            matched = _poll_loop()
    else:
        matched = _poll_loop()

    if not matched:
        log.warning(f"    wait_for_url timed out after {timeout}s (wanted {target!r})")


# ──────────────────────────────────────────────────────────────
# MOBILE / TOUCH actions — CDP Input.dispatchTouchEvent
#
# These only work on a driver whose session has touch emulation
# enabled (GhostShellBrowser does this automatically for profiles
# with a mobile fingerprint). On a desktop session they degrade
# gracefully: CDP still accepts the calls, the events fire, but
# the page won't behave differently than a mouse click.
#
# We use the CDP "touchPoints" list shape rather than Selenium's
# TouchActions because the latter is deprecated in WebDriver and
# Selenium 4 removed it for CDP-backed drivers.
# ──────────────────────────────────────────────────────────────

def _find_center(driver, selector: str, timeout: int = 8) -> tuple[float, float]:
    """Locate an element and return its (x, y) center in viewport px.
    Raises TimeoutException on miss."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    el = WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((By.CSS_SELECTOR, selector))
    )
    rect = el.rect
    return (rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)


def _act_touch_click(driver, action: dict, ctx: dict):
    """Emulate a single-finger tap on an element or absolute (x,y).

    Config:
      selector  CSS selector (preferred — resolves to element center)
      x, y      direct viewport coordinates (fallback if no selector)
      duration  time between touchstart and touchend, ms (default 80)

    Touch events fired:
      touchstart → (optional tiny dwell) → touchend
    """
    import time
    selector = action.get("selector")
    if selector:
        x, y = _find_center(driver, selector)
    else:
        x = float(action.get("x", 0))
        y = float(action.get("y", 0))
    duration = float(action.get("duration", 80)) / 1000.0

    try:
        driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
            "type":        "touchStart",
            "touchPoints": [{"x": x, "y": y}],
        })
        time.sleep(max(0.03, duration))
        driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
            "type":        "touchEnd",
            "touchPoints": [],
        })
    except Exception as e:
        logging.warning(f"[touch_click] failed at ({x},{y}): {e}")
        return False
    return True


def _act_swipe(driver, action: dict, ctx: dict):
    """Emulate a swipe gesture across the viewport.

    Config:
      direction     "up" | "down" | "left" | "right" (default "up")
      from_x/y      optional starting point (default: centered on direction)
      to_x/y        optional end point
      duration      total swipe time in ms (default 400)
      steps         number of intermediate touchMove events (default 16)

    The default behaviour ("up", no coords) simulates a finger
    dragging the page content upward — the usual way to scroll on
    mobile. For pull-to-refresh use "down".
    """
    import time
    direction = (action.get("direction") or "up").lower()
    duration  = float(action.get("duration", 400)) / 1000.0
    steps     = max(4, int(action.get("steps", 16)))

    try:
        size = driver.execute_script(
            "return {w: window.innerWidth, h: window.innerHeight};"
        )
        W, H = size["w"], size["h"]
    except Exception:
        W, H = 400, 800

    if direction == "up":
        fx, fy, tx, ty = W/2, H*0.75, W/2, H*0.25
    elif direction == "down":
        fx, fy, tx, ty = W/2, H*0.25, W/2, H*0.75
    elif direction == "left":
        fx, fy, tx, ty = W*0.8, H/2, W*0.2, H/2
    elif direction == "right":
        fx, fy, tx, ty = W*0.2, H/2, W*0.8, H/2
    else:
        fx, fy, tx, ty = W/2, H*0.75, W/2, H*0.25

    # Explicit coords override auto-derived direction points
    if "from_x" in action: fx = float(action["from_x"])
    if "from_y" in action: fy = float(action["from_y"])
    if "to_x"   in action: tx = float(action["to_x"])
    if "to_y"   in action: ty = float(action["to_y"])

    dt = duration / steps

    try:
        driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
            "type":        "touchStart",
            "touchPoints": [{"x": fx, "y": fy}],
        })
        for i in range(1, steps):
            # Ease-out — most of the motion up front, decelerating
            t = i / steps
            eased = 1 - (1 - t) * (1 - t)
            x = fx + (tx - fx) * eased
            y = fy + (ty - fy) * eased
            driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
                "type":        "touchMove",
                "touchPoints": [{"x": x, "y": y}],
            })
            time.sleep(dt)
        driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
            "type":        "touchEnd",
            "touchPoints": [],
        })
    except Exception as e:
        logging.warning(f"[swipe] failed {direction} {fx},{fy}->{tx},{ty}: {e}")
        return False
    return True


ACTION_HANDLERS: dict[str, Callable] = {
    # Ads-specific (original catalog)
    "click_ad":           _act_click_ad,
    "click_selector":     _act_click_selector,
    "visit":              _act_visit,
    "hover":              _act_hover,
    "move_random":        _act_move_random,
    "scroll":             _act_scroll,
    "read":               _act_read,
    "type":               _act_type,
    "press_key":          _act_press_key,
    "dwell":              _act_dwell,
    "random_delay":       _act_random_delay,
    "scroll_to_bottom":   _act_scroll_to_bottom,
    "back":               _act_back,
    "new_tab":            _act_new_tab,
    "close_tab":          _act_close_tab,
    "switch_tab":         _act_switch_tab,
    "wait_for":           _act_wait_for,

    # Generic web automation (Feature #3) — Facebook, Twitter, crypto, etc.
    "open_url":           _act_open_url,
    "fill_form":          _act_fill_form,
    "extract_text":       _act_extract_text,
    "execute_js":         _act_execute_js,
    "screenshot":         _act_screenshot,

    # Self-check / Runtime self-check — visits an external fingerprint
    # tester (CreepJS, Pixelscan, Sannysoft, AmIUnique, BrowserLeaks,
    # fpcom), dwells while it scans, scrapes the result, and writes it
    # to external_fp_results. Used by the FP probe button on profile
    # detail page.
    "visit_external_fp_tester": _act_visit_external_fp_tester,

    # Health monitor (Sprint 4): visit detection sites + record scores
    "health_check_canary": _act_health_check_canary,
    "wait_for_url":       _act_wait_for_url,

    # Ad-density inflater: pre-warm Google's commercial-intent
    # context before a brand search to boost ad serving rate.
    "commercial_inflate": _act_commercial_inflate,

    # Mobile-specific (CDP touch emulation)
    "touch_click":        _act_touch_click,
    "swipe":              _act_swipe,
}


# ──────────────────────────────────────────────────────────────
# LOOP-LEVEL HANDLERS
#
# These run from run_main_script(). Instead of driver+action+ctx,
# they receive (browser, step, loop_ctx) where loop_ctx is a
# contract with callbacks main.py provides:
#   loop_ctx = {
#       "all_queries":    [...],          # list of strings from DB
#       "search_query":   callable,       # run_one_query(query) -> list[ad]
#       "rotate_ip":      callable,       # force_rotate() -> new_ip|None
#       "per_ad_runner":  callable,       # run_post_ad_pipeline(ad, query)
#       "watchdog":       watchdog obj    # .heartbeat() / .pause()
#   }
# ──────────────────────────────────────────────────────────────

def _loop_search_query(browser, step, loop_ctx):
    """Run one specific query string; dispatch per-ad pipeline for each ad."""
    q = (step.get("query") or "").strip()
    if not q:
        log.warning("  search_query step has empty 'query' field, skipping")
        return
    fail_on_empty = bool(step.get("fail_on_empty", False))

    search_fn = loop_ctx.get("search_query")
    if search_fn is None:
        log.warning("  search_query: no runner in loop_ctx")
        return

    log.info(f"  → search_query: {q!r}")
    ads = search_fn(q) or []
    if not ads and fail_on_empty:
        raise RuntimeError(f"search_query: no ads for {q!r} (fail_on_empty=true)")

    per_ad = loop_ctx.get("per_ad_runner")
    if per_ad and ads:
        for ad in ads:
            per_ad(ad, q)


def _loop_search_all_queries(browser, step, loop_ctx):
    """Convenience wrapper: iterate every query from DB.search.queries."""
    queries = list(loop_ctx.get("all_queries") or [])
    if step.get("shuffle", True):
        random.shuffle(queries)
    if not queries:
        log.warning("  search_all_queries: no queries configured")
        return

    search_fn = loop_ctx.get("search_query")
    per_ad    = loop_ctx.get("per_ad_runner")
    if search_fn is None:
        log.warning("  search_all_queries: no runner in loop_ctx")
        return

    log.info(f"  → search_all_queries: {len(queries)} queries")
    for q in queries:
        ads = search_fn(q) or []
        if per_ad and ads:
            for ad in ads:
                per_ad(ad, q)


def _loop_rotate_ip(browser, step, loop_ctx):
    """Force a proxy rotation. No-op on static proxies."""
    wait_after = float(step.get("wait_after_sec", 4))
    rotate_fn = loop_ctx.get("rotate_ip")
    if rotate_fn is None:
        log.info("  rotate_ip: no rotation callback (static proxy?) — skip")
        return
    new_ip = rotate_fn()
    if new_ip:
        log.info(f"  → rotate_ip: now on {new_ip}")
    if wait_after > 0:
        time.sleep(wait_after + random.uniform(0, 1.5))


def _loop_pause(browser, step, loop_ctx):
    """Sleep for a random duration between min_sec and max_sec."""
    lo = float(step.get("min_sec", 3))
    hi = float(step.get("max_sec", 8))
    if hi < lo: hi = lo
    sleep_for = random.uniform(lo, hi)
    log.info(f"  → pause: {sleep_for:.1f}s")
    time.sleep(sleep_for)


def _loop_visit_url(browser, step, loop_ctx):
    """Navigate to an arbitrary URL, dwell, then continue."""
    url = (step.get("url") or "").strip()
    if not url:
        log.warning("  visit_url: empty URL, skipping")
        return
    lo = float(step.get("dwell_min", 4))
    hi = float(step.get("dwell_max", 12))
    log.info(f"  → visit_url: {url}")
    try:
        browser.driver.get(url)
    except Exception as e:
        log.warning(f"    visit_url failed: {e}")
        return
    time.sleep(random.uniform(lo, hi))


def _loop_refresh(browser, step, loop_ctx):
    """
    Refresh the current page (typically a SERP after a search_query
    step returned no ads). Supports a retry loop with delays between
    refreshes.

    Params:
      max_attempts   int    how many times to refresh (default 3)
      delay_min_sec  float  min wait between refreshes (default 3)
      delay_max_sec  float  max wait between refreshes (default 8)
      stop_when_ads  bool   after each refresh, re-parse ads and stop
                            the retry loop early if any are found
                            (default true). Requires the nested context
                            to have a search_query callback able to
                            re-dispatch the last query — for now we just
                            drop the check and always do N refreshes.

    Typical usage: inside a loop over queries, after search_query:
      loop { items: [...], item_var: query
             steps: [
               { search_query: "{query}" }
               { refresh: max_attempts: 3, delay_min_sec: 5 }
             ] }
    """
    max_attempts = int(step.get("max_attempts", 3))
    lo = float(step.get("delay_min_sec", 3))
    hi = float(step.get("delay_max_sec", 8))
    if hi < lo: hi = lo

    driver = browser.driver
    # Cap this refresh's load time — combined with eager strategy, it
    # returns almost immediately after DOMContentLoaded. No reason to
    # wait for third-party subresources on a page we're about to re-parse.
    try:
        driver.set_page_load_timeout(15)
    except Exception:
        pass

    # Sanity guard: refresh only makes sense if the current tab is on
    # a Google SERP. If we're on fingerprint.com (leftover from a
    # previous probe run), about:blank, or some other non-SERP, the
    # refresh just spins and the user wastes 3 attempts on the wrong
    # page. Bail loudly so they see the issue.
    try:
        cur_url = (driver.current_url or "").lower()
    except Exception:
        cur_url = ""
    if not cur_url or not (
        "google." in cur_url and (
            "/search" in cur_url or "google." in cur_url[:20]
        )
    ):
        log.warning(
            f"  → refresh: SKIP -- current URL is {cur_url[:80]!r}, "
            f"not a Google SERP. The previous step likely failed to "
            f"navigate (empty query? dead browser?). Refreshing this "
            f"page would loop on the wrong content."
        )
        return

    for attempt in range(1, max_attempts + 1):
        wait = random.uniform(lo, hi)
        log.info(f"  → refresh: attempt {attempt}/{max_attempts} "
                 f"(wait {wait:.1f}s before)")
        time.sleep(wait)
        try:
            driver.refresh()
        except Exception as e:
            log.warning(f"    refresh failed: {e}")
            try: driver.execute_script("window.stop();")
            except Exception: pass
            return


def _substitute_vars(value, vars_dict):
    """Replace {var} placeholders in a string (or recursively in dict/list).

    Used inside `_loop_foreach` so that when a user writes a nested step
    like `search_query: { query: "{item}" }` inside a foreach over items
    ["apple", "banana"], each iteration actually searches "apple" then
    "banana".

    Only plain-identifier braces are substituted — `{item}` yes,
    `{"type":"x"}` no (the latter isn't an identifier).
    """
    if isinstance(value, str):
        def _sub(match):
            key = match.group(1).strip()
            return str(vars_dict.get(key, match.group(0)))
        return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _sub, value)
    if isinstance(value, dict):
        return {k: _substitute_vars(v, vars_dict) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_vars(v, vars_dict) for v in value]
    return value


def _loop_foreach(browser, step, loop_ctx):
    """
    Generic iteration step. User supplies the list explicitly; inside
    steps can reference the current item via `{item}` (or a custom
    `item_var`).

    Params:
      items       list[str]  explicit list of values to iterate over.
      items_from  optional "queries" — fetch from search.queries for
                  backwards compat with older configs.
      item_var    str        placeholder name, default "item".
      shuffle     bool       randomize order (default true).
      steps       list[dict] nested action steps to run per item.

    Example (Main script → Loop):
      type: loop
      items: ["best laptops", "gaming chairs", "smart home hub"]
      item_var: query
      steps:
        - type: pause     (min_sec: 2, max_sec: 5)
        - type: search_query   (query: "{query}")
        - type: rotate_ip
    """
    items     = list(step.get("items") or [])
    items_src = step.get("items_from")
    item_var  = step.get("item_var") or "item"
    shuffle   = bool(step.get("shuffle", True))
    steps     = step.get("steps") or []

    # Backwards-compat: pull items from the Domains page if requested
    if not items and items_src == "queries":
        items = list(loop_ctx.get("all_queries") or [])

    if not items:
        log.warning("  loop: empty items list — nothing to iterate")
        return
    if not steps:
        log.warning("  loop: no nested steps defined")
        return

    if shuffle:
        items = list(items)
        random.shuffle(items)

    log.info(f"  → loop: {len(items)} items × {len(steps)} steps "
             f"(var={item_var!r}, shuffle={shuffle})")

    dog = loop_ctx.get("watchdog")

    for idx, item in enumerate(items, 1):
        if dog:
            try: dog.heartbeat()
            except Exception: pass

        log.info(f"  [loop {idx}/{len(items)}] {item_var}={item!r}")
        vars_dict = {item_var: item, "index": idx, "total": len(items)}

        for i, nested_step in enumerate(steps, 1):
            if not nested_step.get("enabled", True):
                continue

            # Substitute {item_var} in every param value
            resolved = _substitute_vars(dict(nested_step), vars_dict)
            act_type = resolved.get("type")
            handler  = LOOP_ACTION_HANDLERS.get(act_type)

            if handler is None:
                log.warning(f"    loop step {i}: unknown action {act_type!r}")
                continue

            prob = float(resolved.get("probability", 1.0))
            if random.random() > prob:
                log.info(f"    loop step {i}: skip {act_type} (p={prob:.2f})")
                continue

            try:
                handler(browser, resolved, loop_ctx)
            except Exception as e:
                log.warning(f"    loop step {i}: {act_type} errored: "
                            f"{type(e).__name__}: {e}")
                if resolved.get("abort_on_error"):
                    raise


LOOP_ACTION_HANDLERS: dict[str, Callable] = {
    "search_query":        _loop_search_query,
    "search_all_queries":  _loop_search_all_queries,
    "rotate_ip":           _loop_rotate_ip,
    "pause":               _loop_pause,
    "visit_url":           _loop_visit_url,
    "refresh":             _loop_refresh,
    "loop":                _loop_foreach,
}


def run_main_script(browser, main_script: list, loop_ctx: dict):
    """
    Execute a top-level (loop-scope) script. Each step is one of the
    LOOP_ACTION_HANDLERS entries. Skipped when `main_script` is empty —
    caller should fall back to the legacy hardcoded query loop.

    loop_ctx contract:
      all_queries     list[str]           — queries from db.search.queries
      search_query    callable(q) → list  — main.search_query wrapper
      rotate_ip       callable()   → str  — main.rotate wrapper
      per_ad_runner   callable(ad, query) — main.run_post_ad_pipeline
      watchdog        watchdog ctx        — optional, for heartbeat
    """
    if not main_script:
        return False   # caller uses legacy behavior

    dog = loop_ctx.get("watchdog")
    for i, step in enumerate(main_script, 1):
        if not step.get("enabled", True):
            continue
        act_type = step.get("type")
        handler = LOOP_ACTION_HANDLERS.get(act_type)
        if handler is None:
            log.warning(f"[main_script step {i}] unknown loop action: {act_type}")
            continue

        prob = float(step.get("probability", 1.0))
        if random.random() > prob:
            log.info(f"[main_script step {i}] skip {act_type} (p={prob:.2f})")
            continue

        if dog:
            try: dog.heartbeat()
            except Exception: pass

        log.info(f"[main_script step {i}/{len(main_script)}] {act_type}")
        try:
            handler(browser, step, loop_ctx)
        except Exception as e:
            log.warning(f"[main_script step {i}] {act_type} errored: "
                        f"{type(e).__name__}: {e}")
            # Propagate only if the step flagged itself mandatory
            if step.get("abort_on_error"):
                raise

    return True   # main script ran


def run_pipeline(browser, pipeline: list, context: dict = None):
    """
    Execute a full pipeline of actions. Each action dict has at minimum
    `type`. Common params:
      - enabled             (bool, default true)
      - probability         (0.0..1.0, default 1.0)
      - skip_on_my_domain   (bool) — skip this step if ad's domain is
                             one of the user's own domains (search.my_domains)
      - skip_on_target      (bool) — skip this step if ad's domain is a
                             target domain (from context)
      - only_on_target      (bool) — inverse: run ONLY for target-domain ads
      - only_on_my_domain   (bool) — inverse: run ONLY for my-domain ads

    Each step's execution is recorded in the `action_events` table so the
    Overview / Competitors pages can display real "actions performed"
    counters (instead of just "ads found").
    """
    if not pipeline:
        return
    ctx = context or {}
    ad  = ctx.get("ad") or {}
    driver = browser.driver

    # Figure out once which domain we're dealing with
    ad_domain   = (ad.get("domain") or "").lower().strip()
    my_domains  = {d.lower().strip() for d in (ctx.get("my_domains") or [])}
    is_mine     = bool(ad_domain and any(
        ad_domain == d or ad_domain.endswith("." + d) for d in my_domains
    ))
    is_target   = bool(ad.get("is_target"))

    # Classify ad for stats tables
    if is_target:       ad_class = "target"
    elif is_mine:       ad_class = "my_domain"
    elif ad_domain:     ad_class = "competitor"
    else:               ad_class = "unknown"

    # DB handle for event logging; fail open if DB not available
    try:
        from ghost_shell.db.database import get_db
        _db = get_db()
    except Exception:
        _db = None

    run_id       = ctx.get("run_id")
    profile_name = ctx.get("profile_name") or "unknown"
    query_str    = ctx.get("query") or ""

    def _log_event(action_type, outcome, skip_reason=None,
                   duration_sec=None, error=None):
        if _db is None:
            return
        try:
            _db.action_event_add(
                run_id=run_id, profile_name=profile_name,
                query=query_str, ad_domain=ad_domain, ad_class=ad_class,
                action_type=action_type, outcome=outcome,
                skip_reason=skip_reason, duration_sec=duration_sec,
                error=error,
            )
        except Exception as e:
            # Bumped from DEBUG to WARNING — silent failures here meant
            # the Competitors page Actions column stayed at 0 even when
            # clicks ran. If you see this in production logs, dump the
            # underlying error and check action_events DB integrity.
            log.warning(
                f"action_event_add failed for "
                f"{action_type}/{outcome}/{ad_domain or '<no-domain>'}: {e}"
            )

    for i, action in enumerate(pipeline, 1):
        act_type = action.get("type") or "unknown"

        if not action.get("enabled", True):
            _log_event(act_type, "skipped", skip_reason="disabled")
            continue

        handler = ACTION_HANDLERS.get(act_type)
        if handler is None:
            log.warning(f"  unknown action type: {act_type}")
            _log_event(act_type, "error", error="unknown_action_type")
            continue

        # Per-step domain filters — "do nothing if ad is my own domain"
        if is_mine and action.get("skip_on_my_domain"):
            log.info(f"  [{i}] skip {act_type} (ad is on my_domain: {ad_domain})")
            _log_event(act_type, "skipped", skip_reason="my_domain")
            continue
        if is_target and action.get("skip_on_target"):
            log.info(f"  [{i}] skip {act_type} (ad is on target domain)")
            _log_event(act_type, "skipped", skip_reason="target")
            continue
        # Inverse filters — step runs ONLY for specified ad class
        if action.get("only_on_target") and not is_target:
            log.info(f"  [{i}] skip {act_type} (only_on_target, ad is not target)")
            _log_event(act_type, "skipped", skip_reason="not_target")
            continue
        if action.get("only_on_my_domain") and not is_mine:
            log.info(f"  [{i}] skip {act_type} (only_on_my_domain, ad is not mine)")
            _log_event(act_type, "skipped", skip_reason="not_my_domain")
            continue

        probability = float(action.get("probability", 1.0))
        if random.random() > probability:
            log.info(f"  [{i}] skip {act_type} (probability {probability:.2f})")
            _log_event(act_type, "skipped", skip_reason="probability")
            continue

        t0 = time.time()
        try:
            log.info(f"  [{i}] running {act_type}")
            handler(driver, action, ctx)
            _log_event(act_type, "ran", duration_sec=round(time.time() - t0, 2))
        except Exception as e:
            log.warning(f"  [{i}] {act_type} errored: {type(e).__name__}: {e}")
            _log_event(
                act_type, "error",
                duration_sec=round(time.time() - t0, 2),
                error=f"{type(e).__name__}: {str(e)[:120]}",
            )
            # Continue with remaining steps — don't abort the whole pipeline
            # for one bad selector


# Catalog of actions for dashboard UI
def action_common_params() -> list[dict]:
    """
    Params that apply to EVERY action type (probability, domain skip
    flags). UI renders these separately from action-specific params —
    they live on the step regardless of action type.
    """
    return [
        {
            "name": "probability",
            "type": "number",
            "default": 1.0,
            "label": "Probability",
            "min": 0,
            "max": 1,
            "step": 0.05,
            "hint": "Chance this step runs (0.0–1.0). 0.3 = 30 % of ads.",
        },
        {
            "name": "skip_on_my_domain",
            "type": "bool",
            "default": False,
            "label": "Skip if ad is on my domain",
            "hint": "If the ad's domain is in Search → My Domains, this "
                    "step is silently skipped. Useful to avoid clicking "
                    "your own ads (CPC cost).",
        },
        {
            "name": "skip_on_target",
            "type": "bool",
            "default": False,
            "label": "Skip if ad is on target domain",
            "hint": "Skips this step for ads matching Search → Target "
                    "Domains. Use this on pipelines meant for generic/"
                    "competitor ads only.",
        },
        {
            "name": "only_on_target",
            "type": "bool",
            "default": False,
            "label": "Run ONLY for target-domain ads",
            "hint": "Step runs only when the ad's domain is in Search → "
                    "Target Domains. Mutually exclusive with skip_on_target.",
        },
        {
            "name": "only_on_my_domain",
            "type": "bool",
            "default": False,
            "label": "Run ONLY for my-domain ads",
            "hint": "Step runs only when the ad's domain is in Search → "
                    "My Domains. Rare, but useful for a self-audit pipeline.",
        },
    ]


def _action_catalog_raw() -> list[dict]:
    """Return metadata for every action type — used by dashboard builder."""
    return [
        {
            "type": "click_ad",
            "label": "Click on advertisement",
            "category": "ads",
            "description": "Finds the ad anchor, moves mouse along a curve, "
                          "Ctrl+Clicks it to open in new tab. The most "
                          "realistic ad-click signal. Optional deep_dive "
                          "clicks 1-2 internal links on the landing page "
                          "to simulate engaged shopping behaviour.",
            "params": [
                {"name": "dwell_min", "type": "number", "default": 6,  "label": "Min dwell (s)"},
                {"name": "dwell_max", "type": "number", "default": 18, "label": "Max dwell (s)"},
                {"name": "scroll_after_click", "type": "bool", "default": True,
                 "label": "Scroll page after click"},
                {"name": "close_after", "type": "bool", "default": True,
                 "label": "Close tab afterward"},
                {"name": "deep_dive", "type": "bool", "default": False,
                 "label": "Deep dive (click internal links)",
                 "hint": "After landing on competitor's site, click 1-2 "
                         "internal links (catalog, product page) and dwell "
                         "on each. Strongest 'engaged user' signal -- "
                         "Google CTR-prediction loves this."},
                {"name": "depth_min", "type": "number", "default": 1,
                 "label": "Deep-dive min clicks"},
                {"name": "depth_max", "type": "number", "default": 2,
                 "label": "Deep-dive max clicks"},
                {"name": "inner_dwell_min", "type": "number", "default": 5,
                 "label": "Inner dwell min (s)"},
                {"name": "inner_dwell_max", "type": "number", "default": 12,
                 "label": "Inner dwell max (s)"},
            ],
        },
        {
            "type": "click_selector",
            "label": "Click element by CSS",
            "description": "Human mouse move + click on any element matched "
                          "by a CSS selector.",
            "params": [
                {"name": "selector", "type": "text", "required": True,
                 "placeholder": "a.product-link"},
                {"name": "new_tab", "type": "bool", "default": False,
                 "label": "Ctrl+Click (new tab)"},
            ],
        },
        {
            "type": "visit",
            "label": "Visit URL directly",
            "description": "Navigate to a URL without a click. Less human than "
                          "click_ad — use only when you need a specific URL.",
            "params": [
                {"name": "url", "type": "text",
                 "placeholder": "leave empty to use ad's URL"},
                {"name": "new_tab", "type": "bool", "default": True},
                {"name": "dwell_min", "type": "number", "default": 5},
                {"name": "dwell_max", "type": "number", "default": 15},
                {"name": "close_after", "type": "bool", "default": True},
            ],
        },
        {
            "type": "hover",
            "label": "Hover over element",
            "description": "Move mouse to a selector and pause.",
            "params": [
                {"name": "selector", "type": "text", "required": True},
                {"name": "hold_min", "type": "number", "default": 0.5},
                {"name": "hold_max", "type": "number", "default": 2.0},
            ],
        },
        {
            "type": "move_random",
            "label": "Move mouse to random spot",
            "description": "Micro mouse wiggle — looks human.",
            "params": [],
        },
        {
            "type": "scroll",
            "label": "Scroll (human-like)",
            "description": "Variable-speed scroll with occasional back-scrolls.",
            "params": [
                {"name": "min_px", "type": "number", "default": 300},
                {"name": "max_px", "type": "number", "default": 900},
                {"name": "backtracking", "type": "bool", "default": True,
                 "label": "Occasionally scroll back up"},
            ],
        },
        {
            "type": "read",
            "label": "Read (scroll + pause pattern)",
            "description": "Simulates a user reading — scroll a bit → pause "
                          "a few seconds → repeat. Very realistic.",
            "params": [
                {"name": "min_paragraphs", "type": "number", "default": 3},
                {"name": "max_paragraphs", "type": "number", "default": 6},
                {"name": "pause_min",      "type": "number", "default": 2.0},
                {"name": "pause_max",      "type": "number", "default": 5.5},
            ],
        },
        {
            "type": "type",
            "label": "Type text into input",
            "description": "Char-by-char typing with realistic timing.",
            "params": [
                {"name": "selector", "type": "text", "required": True,
                 "placeholder": "input[name='search']"},
                {"name": "text",     "type": "text", "required": True},
            ],
        },
        {
            "type": "press_key",
            "label": "Press a key",
            "description": "Send a single key (ENTER, ESCAPE, TAB, …).",
            "params": [
                {"name": "key", "type": "select", "required": True,
                 "options": ["ENTER", "ESCAPE", "TAB", "SPACE", "BACKSPACE",
                             "ARROW_UP", "ARROW_DOWN", "ARROW_LEFT", "ARROW_RIGHT"]},
            ],
        },
        {
            "type": "dwell",
            "label": "Wait a moment",
            "description": "Pause for a random time between min and max.",
            "params": [
                {"name": "min_sec", "type": "number", "default": 2},
                {"name": "max_sec", "type": "number", "default": 6},
            ],
        },
        {
            "type": "random_delay",
            "label": "Random delay (preset)",
            "description": "Shortcut for dwell — pick a size.",
            "params": [
                {"name": "size", "type": "select", "default": "medium",
                 "options": ["tiny", "small", "medium", "long"]},
            ],
        },
        {
            "type": "scroll_to_bottom",
            "label": "Scroll to bottom of page",
            "description": "Gradual scroll all the way down.",
            "params": [],
        },
        {
            "type": "back",
            "label": "Browser back",
            "description": "driver.back() — go to previous page.",
            "params": [
                {"name": "delay_sec", "type": "number", "default": 1},
            ],
        },
        {
            "type": "new_tab",
            "label": "Open new tab",
            "description": "Opens about:blank in a new tab and switches to it.",
            "params": [],
        },
        {
            "type": "close_tab",
            "label": "Close current tab",
            "description": "Close this tab and switch back to the first.",
            "params": [],
        },
        {
            "type": "switch_tab",
            "label": "Switch to tab by index",
            "description": "Activate tab #N (0 = first).",
            "params": [
                {"name": "index", "type": "number", "default": 0},
            ],
        },
        {
            "type": "wait_for",
            "label": "Wait for element to appear",
            "description": "Block until selector appears or timeout.",
            "scope": "per_ad",
            "params": [
                {"name": "selector",    "type": "text", "required": True},
                {"name": "timeout_sec", "type": "number", "default": 10},
            ],
        },

        # ═════════════════════════════════════════════════════════
        # LOOP-LEVEL actions — run OUTSIDE the per-ad pipeline.
        # These compose the "main script" that orchestrates a run:
        # search a query, iterate its ads, rotate IP between queries,
        # take a break, etc. Scope metadata lets the UI group them
        # separately in the builder.
        # ═════════════════════════════════════════════════════════
        {
            "type": "search_query",
            "label": "Search query",
            "category": "navigation",
            "scope": "loop",
            "description": "Navigate to google.com/search?q=… for the given "
                          "query, wait for the SERP, parse ads, and (if ads "
                          "are found) run the per-ad pipeline for each one.",
            "params": [
                {"name": "query", "type": "text", "required": True,
                 "label": "Query text",
                 "hint": "The exact string to type into Google. May contain spaces."},
                {"name": "fail_on_empty", "type": "bool", "default": False,
                 "label": "Fail if no ads",
                 "hint": "Normally no ads = just move on. Enable this if an "
                         "empty SERP should abort the whole script."},
                {"name": "refine_on_zero_ads", "type": "bool", "default": True,
                 "label": "⟳ Auto-refine to long-tail when 0 ads",
                 "hint": "If the brand query returns 0 ads, automatically "
                         "retry with long-tail commercial variants ('купити', "
                         "'ціна', 'відгуки', etc) before giving up. Real "
                         "users refine queries; this turns dead navigational "
                         "queries into productive commercial ones. "
                         "Big win for brand-only monitoring."},
                {"name": "refine_max_attempts", "type": "number", "default": 3,
                 "label": "Refine: max variants to try",
                 "hint": "Caps how many long-tail variants to attempt before "
                         "giving up. 3 is the sweet spot."},
                {"name": "refine_locale", "type": "text", "default": "UA",
                 "label": "Refine: suffix locale",
                 "hint": "UA / RU / EN or combos (UA+RU). Determines which "
                         "commercial-suffix pool is used to generate variants."},
            ],
        },
        {
            "type": "loop",
            "label": "Loop over a custom list",
            "scope": "loop",
            "description": "Iterate a list of values, running nested steps "
                          "for each. Inside the steps, use {item} (or your "
                          "custom variable name) anywhere a param value is a "
                          "string — e.g. search_query with query='{item}'. "
                          "This replaces the old hardcoded query loop.",
            "params": [
                {"name": "items", "type": "textlist", "required": True,
                 "label": "Items (one per line)",
                 "hint": "One value per line. These are substituted into "
                         "nested steps' string params."},
                {"name": "item_var", "type": "text", "default": "item",
                 "label": "Placeholder name",
                 "hint": "Use this name in braces inside nested steps. "
                         "E.g. if var is 'query', write '{query}' in "
                         "a search_query step."},
                {"name": "shuffle", "type": "bool", "default": True,
                 "label": "Randomize order"},
                {"name": "steps", "type": "steps", "default": [],
                 "label": "Nested steps",
                 "hint": "Runs once per item. Only loop-level actions "
                         "(search_query, pause, rotate_ip, visit_url) are "
                         "valid here — per-ad actions (click_ad, read, "
                         "hover) run inside search_query automatically."},
            ],
        },
        {
            "type": "rotate_ip",
            "label": "Rotate proxy IP",
            "scope": "loop",
            "description": "Force a proxy rotation mid-script. No-op on "
                          "static (non-rotating) proxies.",
            "params": [
                {"name": "wait_after_sec", "type": "number", "default": 4,
                 "label": "Pause after (s)"},
            ],
        },
        {
            "type": "pause",
            "label": "Wait / idle pause",
            "scope": "loop",
            "description": "Sleep for a random duration. Simulates a human "
                          "getting distracted between tasks.",
            "params": [
                {"name": "min_sec", "type": "number", "default": 3, "label": "Min (s)"},
                {"name": "max_sec", "type": "number", "default": 8, "label": "Max (s)"},
            ],
        },
        {
            "type": "visit_url",
            "label": "Visit a URL",
            "scope": "loop",
            "description": "Navigate to an arbitrary URL. Useful for warm-up "
                          "(news site, weather) to break the fingerprint of "
                          "\"only ever goes to Google\".",
            "params": [
                {"name": "url", "type": "text", "required": True,
                 "label": "URL", "placeholder": "https://example.com"},
                {"name": "dwell_min", "type": "number", "default": 4,
                 "label": "Min dwell (s)"},
                {"name": "dwell_max", "type": "number", "default": 12,
                 "label": "Max dwell (s)"},
            ],
        },
        {
            "type": "refresh",
            "label": "Refresh current page",
            "scope": "loop",
            "description": "Reload the current page N times with a random "
                          "delay between attempts. Use this right after a "
                          "search_query step — if Google returned no ads, "
                          "a refresh often brings them back (ad auction "
                          "runs per impression, timing-sensitive).",
            "params": [
                {"name": "max_attempts", "type": "number", "default": 3,
                 "label": "Max attempts",
                 "hint": "How many times to refresh the page."},
                {"name": "delay_min_sec", "type": "number", "default": 3,
                 "label": "Min delay (s)",
                 "hint": "Minimum wait before each refresh. Humans don't "
                         "hammer F5 — a 3-8s range looks organic."},
                {"name": "delay_max_sec", "type": "number", "default": 8,
                 "label": "Max delay (s)"},
            ],
        },

        # ─── Generic web automation (Feature #3) ────────────────
        # These are the actions power-users build Facebook / Twitter /
        # crypto / scraping scripts with. All string params support
        # {var_name} substitution — see _subst() in this file.
        {
            "type":        "open_url",
            "label":       "Open URL",
            "category":    "navigation",
            "scope":       "per_ad",
            "description": "Navigate the browser to a URL. Supports "
                           "{variable} substitution so you can chain "
                           "extract_text → open_url.",
            "params": [
                {"name": "url", "type": "text", "required": True,
                 "placeholder": "https://example.com/login",
                 "hint": "Template string. Use {var} placeholders to "
                         "reference values stored by extract_text."},
                {"name": "wait_after", "type": "number", "default": 1.0,
                 "label": "Wait after (s)",
                 "hint": "Pause after navigation before the next step."},
            ],
        },
        {
            "type":        "commercial_inflate",
            "label":       "Pre-search inflate",
            "category":    "ads",
            "scope":       "any",
            "description": (
                "Pre-warm Google's commercial-intent context BEFORE the "
                "next brand search. Visits N short generic commercial "
                "queries (no brand reference) in the same browser session "
                "so the next brand SERP comes back denser with ads "
                "(typically 2-5x more). Auto-detects the brand category "
                "(medical / beauty / tech / auto) for relevant pre-queries. "
                "Place ABOVE search_query inside a foreach/loop iteration."
            ),
            "params": [
                {"name": "brand", "type": "text",
                 "default": "{item}",
                 "label": "Brand (drives category)",
                 "placeholder": "{item}",
                 "hint": "Template string. Inside a foreach over brand "
                         "queries this is usually {item}. Used to detect "
                         "the product category (e.g. 'medika' → medical) "
                         "and choose appropriate pre-queries."},
                {"name": "n", "type": "number", "default": 2,
                 "label": "Pre-query count",
                 "hint": "How many commercial pre-queries to fire (2-3 is "
                         "the sweet spot — more = slower run, less = "
                         "weaker signal)."},
                {"name": "dwell_min", "type": "number", "default": 8,
                 "label": "Dwell min (s)"},
                {"name": "dwell_max", "type": "number", "default": 15,
                 "label": "Dwell max (s)"},
                {"name": "locale", "type": "text", "default": "UA",
                 "label": "Locale",
                 "hint": "UA, RU, EN, or combinations like 'UA+RU'. "
                         "Determines which suffix pool the commercial "
                         "queries are drawn from."},
                {"name": "click_organic", "type": "bool", "default": False,
                 "label": "Click first organic result on each pre-SERP",
                 "hint": "Stronger signal but adds 5-10s per pre-query. "
                         "Use when boosting a particularly cold profile."},
            ],
        },
        {
            "type":        "fill_form",
            "label":       "Fill form field(s)",
            "category":    "input",
            "scope":       "per_ad",
            "description": "Type into one or more form fields with human-"
                           "like keystroke timing. For multiple fields, use "
                           "the `fields` array param (raw JSON).",
            "params": [
                {"name": "selector", "type": "text",
                 "placeholder": "input[name='email']",
                 "hint": "CSS selector for a single field. For multiple, "
                         "leave empty and use `fields` below."},
                {"name": "value", "type": "text",
                 "placeholder": "{email}  or  any literal string",
                 "hint": "Text to type. {variable} substitution supported."},
                {"name": "clear_first", "type": "bool", "default": True,
                 "label": "Clear field before typing"},
                {"name": "fields", "type": "json",
                 "label": "Multiple fields (JSON array)",
                 "placeholder":
                    '[{"selector":"#email","value":"{email}"},'
                    '{"selector":"#pwd","value":"{password}"}]',
                 "hint": "Alternative: array of {selector, value, "
                         "clear_first} objects. Overrides the single-"
                         "field params above when set."},
            ],
        },
        {
            "type":        "extract_text",
            "label":       "Extract text / attribute",
            "category":    "data",
            "scope":       "per_ad",
            "description": "Pull the text (or an attribute) of an element "
                           "and store it in a variable. Later steps can "
                           "reference it with {var_name}.",
            "params": [
                {"name": "selector", "type": "text", "required": True,
                 "placeholder": "h1.product-title"},
                {"name": "attribute", "type": "text",
                 "placeholder": "href   (optional — default: element text)",
                 "hint": "Leave empty to grab element.text. Common attrs: "
                         "href, value, data-id, src."},
                {"name": "store_as", "type": "text", "required": True,
                 "default": "last_extract",
                 "label": "Store as variable",
                 "hint": "Name used to reference this value later, e.g. "
                         "{title} in a subsequent fill_form or open_url."},
            ],
        },
        {
            "type":        "execute_js",
            "label":       "Run JavaScript",
            "category":    "power",
            "scope":       "per_ad",
            "description": "Execute arbitrary JS in the page context. "
                           "Advanced — anti-bot systems can fingerprint "
                           "automated JS calls, so use sparingly.",
            "params": [
                {"name": "code", "type": "textarea", "required": True,
                 "placeholder":
                    "// Return a value from the page\n"
                    "return document.querySelector('.price').innerText;",
                 "hint": "Runs as a function body — use `return` to send a "
                         "value back. {var} substitution applies before "
                         "execution, so you can interpolate values in."},
                {"name": "store_as", "type": "text",
                 "label": "Store return as",
                 "hint": "If set, the JS return value goes into "
                         "ctx.vars[store_as]. Omit to discard."},
            ],
        },
        {
            "type":        "screenshot",
            "label":       "Take a screenshot",
            "category":    "data",
            "scope":       "per_ad",
            "description": "Save a PNG of the current viewport into the "
                           "profile's screenshots/ folder. Timestamped "
                           "automatically so shots never overwrite.",
            "params": [
                {"name": "name", "type": "text", "default": "shot",
                 "label": "Filename prefix",
                 "placeholder": "login_success",
                 "hint": "A timestamp is appended automatically. "
                         "Supports {var} substitution — e.g. "
                         "\"step1_{profile_name}\"."},
            ],
        },
        {
            "type":        "wait_for_url",
            "label":       "Wait until URL matches",
            "category":    "flow",
            "scope":       "per_ad",
            "description": "Block until the current URL contains a "
                           "substring (or matches a regex). Essential "
                           "after OAuth redirects or SPA route changes.",
            "params": [
                {"name": "contains", "type": "text",
                 "placeholder": "/dashboard",
                 "hint": "Substring match (simpler). Either this or "
                         "`regex` must be set."},
                {"name": "regex", "type": "text",
                 "placeholder": r"/user/\d+/profile",
                 "hint": "Regex alternative to `contains`. Takes "
                         "precedence when both are given."},
                {"name": "timeout", "type": "number", "default": 15.0,
                 "label": "Timeout (s)",
                 "hint": "Give up and log a warning after this many "
                         "seconds. The script continues either way."},
            ],
        },
    ]


def action_catalog() -> list[dict]:
    """Top-level catalog — wraps _action_catalog_raw() and merges in the
    unified-runtime actions (if/foreach_ad/catch_ads/save_var/http/
    extract_text). Ensures every entry has `scope` and `category` so
    the UI can group them even in legacy scripts.
    """
    catalog = _action_catalog_raw()
    for entry in catalog:
        if "scope" not in entry:
            entry["scope"] = "per_ad"
        # Back-fill a category for legacy actions so the new palette
        # grouping works. Mapping is best-guess; entries can override
        # by setting category in _action_catalog_raw itself.
        if "category" not in entry:
            entry["category"] = _default_category_for(entry["type"])
    # Merge unified-runtime entries (if/foreach_ad/...). Deduplicate by
    # type — unified catalog wins over legacy if both define the same
    # action (currently no collisions but future-proof).
    seen = {e["type"] for e in catalog}
    for entry in _unified_catalog():
        if entry["type"] not in seen:
            catalog.append(entry)
            seen.add(entry["type"])
    return catalog


# ════════════════════════════════════════════════════════════════
# UNIFIED FLOW RUNTIME
# ════════════════════════════════════════════════════════════════
#
# New single-flow model (supersedes the main_script / post_ad_actions
# split, though both coexist during migration). Key ideas:
#
#   • One ordered list of steps. No scope distinction at runtime.
#   • Container steps (foreach_ad, if, loop, foreach) have a `steps`
#     (or then_steps/else_steps) list that recursively runs through
#     this same engine.
#   • A RunContext travels through the tree. It carries:
#       - `ad`      the current ad dict if we're inside foreach_ad
#       - `ads`     the list of ads from the most recent search / catch
#       - `item`    loop variable from a foreach
#       - `vars`    user-saved variables (from save_var / extract_text)
#       - `flags`   captcha_present, ads_found, etc. computed at runtime
#       - `browser` / `driver` / `loop_ctx` for actions that need them
#   • Variables interpolate into any string param via {path} —
#     `{ad.domain}`, `{var.username}`, `{item}`, `{ads.count}`.
#   • Conditions evaluate against the context — see _eval_condition.
#
# Legacy actions (click_ad, visit, read, etc.) keep working because
# the unified runner delegates to their existing handlers when it
# encounters them; only the new types have dedicated unified logic.


def _default_category_for(act_type: str) -> str:
    """Guess a palette category for legacy catalog entries that didn't
    declare one. Extend as we add new types."""
    t = (act_type or "").lower()
    if t in ("if", "foreach_ad", "foreach", "loop", "break", "continue"):
        return "flow"
    if t in ("visit_url", "visit", "back", "refresh", "new_tab",
             "close_tab", "switch_tab", "navigate"):
        return "navigation"
    if t in ("click_ad", "click_selector", "type", "press_key", "hover",
             "scroll", "scroll_to_bottom", "move_random", "fill_form"):
        return "interaction"
    if t in ("pause", "dwell", "random_delay", "wait_for", "wait_for_url"):
        return "timing"
    if t in ("search_query", "search_all_queries", "catch_ads",
             "extract_text", "save_var"):
        return "data"
    if t in ("http_request", "rotate_ip"):
        return "external"
    return "other"


class RunContext:
    """State carried through a unified-flow execution.

    Not a plain dict — having a class lets us:
      - expose helpers like _resolve_path
      - keep child scopes (foreach vars) without polluting the parent
      - snapshot + restore around conditional branches

    Fields that matter to users (can be referenced as {path} in params):
      ad          current-ad dict  (inside foreach_ad)
      ads         list of ads from most recent catch/search
      item        current value of a foreach loop
      var.<name>  saved via save_var / extract_text
      run_id, profile_name, query   metadata
    """

    def __init__(self, browser=None, loop_ctx: dict = None,
                 parent: "RunContext" = None):
        self.browser     = browser
        self.driver      = getattr(browser, "driver", None) if browser else None
        self.loop_ctx    = loop_ctx or {}
        self.parent      = parent
        # Per-run accumulator. Child scopes inherit the SAME vars dict
        # (variables live for the whole run, not per-loop — otherwise
        # "save in one step, use in another" wouldn't work across
        # different containers).
        self.vars        = parent.vars if parent else {}
        # Phase 5.1: at the root scope, seed `vars["vault"]` from env
        # so {vault.<id>.<field>} placeholders resolve through the
        # standard template-resolution machinery (head="vault" falls
        # to the unknown-root branch, which looks up self.vars["vault"]).
        # The dashboard pre-decrypts referenced items at run-launch
        # time and passes them as JSON env -- subprocess never sees
        # the master password.
        if parent is None:
            try:
                import json as _json_v, os as _os_v
                _raw = _os_v.environ.get("GHOST_SHELL_VAULT_RESOLVED")
                if _raw:
                    _bag = _json_v.loads(_raw)
                    if isinstance(_bag, dict):
                        self.vars["vault"] = _bag
            except Exception:
                pass
        self.ad          = parent.ad   if parent else None
        self.ads         = parent.ads  if parent else []
        self.item        = parent.item if parent else None
        self.flags       = parent.flags if parent else {}
        # Metadata (flows into variable resolution as {query}, etc.)
        ctx = self.loop_ctx
        self.run_id       = ctx.get("run_id")
        self.profile_name = ctx.get("profile_name") or "unknown"
        self.query        = parent.query if parent else ""
        # Break/continue flags for loops
        self.should_break    = False
        self.should_continue = False

    def child(self, **overrides) -> "RunContext":
        """Create a child scope with overridden values. ad/item/query
        are the usual ones; other fields inherit."""
        c = RunContext(browser=self.browser, loop_ctx=self.loop_ctx, parent=self)
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    def resolve_path(self, path: str):
        """Resolve a dotted path like 'ad.domain' or 'var.foo.bar' or
        'ads.count'. Returns None for missing paths (silent — callers
        decide how to treat None, usually as empty string)."""
        if not path:
            return None
        parts = path.split(".")
        head = parts[0]
        rest = parts[1:]

        if head == "ad":
            root = self.ad or {}
        elif head == "ads":
            if rest and rest[0] == "count":
                return len(self.ads or [])
            root = self.ads or []
        elif head == "item":
            return self.item
        elif head == "var":
            if not rest:
                return None
            val = self.vars.get(rest[0])
            for k in rest[1:]:
                if isinstance(val, dict):
                    val = val.get(k)
                else:
                    return None
            return val
        elif head == "query":
            return self.query
        elif head == "profile":
            return self.profile_name
        elif head == "flag":
            return self.flags.get(rest[0] if rest else "") if rest else None
        else:
            # Unknown root — look in vars as a convenience
            val = self.vars.get(head)
            for k in rest:
                if isinstance(val, dict):
                    val = val.get(k)
                else:
                    return None
            return val

        # We landed on a dict/list root — dig in
        val = root
        for k in rest:
            if isinstance(val, dict):
                val = val.get(k)
            elif isinstance(val, list):
                try:
                    val = val[int(k)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return val


# ── Variable interpolation ────────────────────────────────────────
# Curly-brace templating: "{ad.domain}" → "example.com". Supports
# dotted paths, falls through as empty string on missing values (with
# debug log so user can see what resolved).
#
# We DON'T do full expression evaluation — just path lookup. If you
# need math, extract into a save_var step with a computed value.

_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_.]*)\}")


# Keys whose values hold NESTED STEPS (executed by container handlers
# like loop/foreach/foreach_ad/if). These nested steps must NOT be
# pre-interpolated when their parent step is interpolated -- the
# container's per-iteration scope (with `item`, `ad`, etc set) is the
# correct context to interpolate them in. If we recursed into these
# keys at the outer level, "{item}" in an inner step would resolve to
# "" because the outer ctx has item=None, then the foreach handler
# would see the already-blanked template and we'd lose the placeholder.
# This was the actual production bug behind the "[search_query] empty
# query, skipping" report after binding Smart Search & Click to a
# fresh profile.
_NESTED_STEPS_KEYS = {"steps", "then_steps", "else_steps"}


def _interpolate(value, ctx: RunContext):
    """Recursively replace {path} references in strings inside a value.
    Plain numbers / bools / None pass through unchanged. Dicts and lists
    are walked so params like {"url": "{ad.clean_url}"} work.

    Container nested-steps are SKIPPED (preserved raw): see
    _NESTED_STEPS_KEYS comment above.
    """
    if isinstance(value, str):
        def _sub(m):
            path = m.group(1)
            resolved = ctx.resolve_path(path)
            if resolved is None:
                return ""
            return str(resolved)
        return _VAR_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {
            k: (v if k in _NESTED_STEPS_KEYS else _interpolate(v, ctx))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_interpolate(v, ctx) for v in value]
    return value


# ── Condition evaluation for `if` ─────────────────────────────────
#
# A condition is a dict: {"kind": "ad_is_competitor"} or
# {"kind": "var_equals", "var": "username", "value": "anton"}.
# Complex conditions combine via and/or/not wrappers.
#
# Adding a new kind: add a case here + an entry in
# CONDITION_KINDS (UI picker metadata).

def _eval_condition(cond: dict, ctx: RunContext) -> bool:
    if not cond:
        return True
    kind = cond.get("kind") or "always"
    negate = bool(cond.get("negate"))

    result = _eval_condition_raw(kind, cond, ctx)
    return (not result) if negate else bool(result)


def _eval_condition_raw(kind: str, cond: dict, ctx: RunContext) -> bool:
    if kind == "always":
        return True
    if kind == "never":
        return False

    # Ad-scope predicates (require ctx.ad)
    #
    # Three-way classification of an ad's domain:
    #   • ad_is_mine        → ad.domain ∈ my_domains              (own)
    #   • ad_is_target      → ad.is_target=true (set by parse_ads)
    #   • ad_is_competitor  → STRICT: not-mine AND not-target
    #                         (i.e. "pure" 3rd-party competitor)
    #   • ad_is_external    → LOOSE: not-mine (target OR competitor)
    #
    # The "external" alias was added because users intuitively expect
    # `if competitor → only_on_target=true` to click target ads. With
    # the strict definition that combination is empty (target ads never
    # enter the if-competitor branch). The loose `ad_is_external`
    # matches the intuitive 2-way (mine / not-mine) split, and lets
    # `only_on_target=true` further narrow inside that branch.
    if kind == "ad_is_competitor":
        ad = ctx.ad or {}
        if not ad.get("domain"):
            return False
        if ad.get("is_target"):
            return False
        my = {d.lower() for d in (ctx.loop_ctx.get("my_domains") or [])}
        dom = (ad.get("domain") or "").lower()
        return not any(dom == d or dom.endswith("." + d) for d in my)
    if kind == "ad_is_external":
        # "Anything not on my domain" — covers competitors AND targets.
        # Use this when you want the per-action only_on_target /
        # skip_on_target flags to do the fine-grained work inside the
        # branch (vs. picking strict ad_is_competitor which excludes
        # targets at the gate).
        ad = ctx.ad or {}
        if not ad.get("domain"):
            return False
        my = {d.lower() for d in (ctx.loop_ctx.get("my_domains") or [])}
        dom = (ad.get("domain") or "").lower()
        return not any(dom == d or dom.endswith("." + d) for d in my)
    if kind == "ad_is_target":
        return bool((ctx.ad or {}).get("is_target"))
    if kind == "ad_is_mine":
        ad = ctx.ad or {}
        my = {d.lower() for d in (ctx.loop_ctx.get("my_domains") or [])}
        dom = (ad.get("domain") or "").lower()
        return any(dom == d or dom.endswith("." + d) for d in my)

    # Ads-list predicates
    if kind == "ads_found":
        return len(ctx.ads or []) > 0
    if kind == "no_ads":
        return len(ctx.ads or []) == 0
    if kind == "ads_count_gte":
        return len(ctx.ads or []) >= int(cond.get("value", 1))

    # Captcha / page state
    if kind == "captcha_present":
        return bool(ctx.flags.get("captcha_present"))
    if kind == "url_contains":
        try:
            url = (ctx.driver.current_url or "") if ctx.driver else ""
        except Exception:
            url = ""
        needle = _interpolate(cond.get("value", ""), ctx)
        return bool(needle) and needle in url
    if kind == "element_exists":
        # Bound the implicit-wait window (audit #103 #4): selenium's
        # `find_elements` honors the driver's IMPLICIT_WAIT setting,
        # which is 30s by default for our driver. So a script that
        # branches on `if element_exists -> A else -> B` can BLOCK 30s
        # on every false branch. Override with a tight per-call
        # implicit_wait via the cond dict (default 1s — element either
        # exists right now or it doesn't, this is a sync probe), then
        # restore to whatever the runtime configured.
        if not ctx.driver:
            return False
        sel = _interpolate(cond.get("selector", ""), ctx)
        if not sel:
            return False
        timeout_sec = float(cond.get("timeout") or 1.0)
        try:
            from selenium.webdriver.common.by import By
            try:
                ctx.driver.implicitly_wait(max(0.0, timeout_sec))
            except Exception:
                pass
            try:
                return len(ctx.driver.find_elements(By.CSS_SELECTOR, sel)) > 0
            finally:
                # Restore the long implicit wait that the rest of the
                # runtime relies on.
                try:
                    ctx.driver.implicitly_wait(30)
                except Exception:
                    pass
        except Exception:
            return False

    # Variable predicates — left value comes from a {path}, right is literal
    if kind == "var_equals":
        lhs = ctx.resolve_path(cond.get("var") or "")
        rhs = _interpolate(cond.get("value"), ctx)
        return str(lhs) == str(rhs)
    if kind == "var_contains":
        lhs = str(ctx.resolve_path(cond.get("var") or "") or "")
        rhs = str(_interpolate(cond.get("value"), ctx) or "")
        return rhs in lhs
    if kind == "var_matches":
        import re as _re
        lhs = str(ctx.resolve_path(cond.get("var") or "") or "")
        pattern = str(_interpolate(cond.get("value"), ctx) or "")
        if not pattern:
            return False
        try:
            return bool(_re.search(pattern, lhs))
        except _re.error:
            return False
    if kind == "var_empty":
        return not bool(ctx.resolve_path(cond.get("var") or ""))

    # Composite: and / or
    if kind == "and":
        return all(_eval_condition(c, ctx) for c in (cond.get("conditions") or []))
    if kind == "or":
        return any(_eval_condition(c, ctx) for c in (cond.get("conditions") or []))

    log.warning(f"[run_flow] unknown condition kind: {kind}")
    return False


# ── Unified flow executor ─────────────────────────────────────────

def run_flow(browser, steps: list, loop_ctx: dict = None,
             context: RunContext = None):
    """Execute a unified-flow step list. Entry point for the new
    script format.

    Returns the RunContext so callers can inspect final vars / ads.
    """
    ctx = context or RunContext(browser=browser, loop_ctx=loop_ctx or {})
    _exec_steps(steps or [], ctx)
    return ctx


def _exec_steps(steps: list, ctx: RunContext):
    """Iterate steps, dispatching each to the right handler. Respects
    break/continue signals from loops within the list."""
    for i, raw_step in enumerate(steps, 1):
        if ctx.should_break or ctx.should_continue:
            return
        if not raw_step.get("enabled", True):
            continue
        # Probability gate
        prob = float(raw_step.get("probability", 1.0))
        if prob < 1.0 and random.random() > prob:
            log.debug(f"    [flow] skip {raw_step.get('type')} (p={prob:.2f})")
            continue

        dog = ctx.loop_ctx.get("watchdog")
        if dog:
            try: dog.heartbeat()
            except Exception: pass

        step = _interpolate(raw_step, ctx)
        _exec_single(step, ctx)


def _exec_single(step: dict, ctx: RunContext):
    act_type = step.get("type") or "unknown"

    # ── Container / flow-control types ────────────────────────
    if act_type == "if":
        return _flow_if(step, ctx)
    if act_type == "foreach_ad":
        return _flow_foreach_ad(step, ctx)
    if act_type == "foreach":
        return _flow_foreach(step, ctx)
    if act_type == "loop":
        # Legacy `loop` — same as foreach with item_var convention
        return _flow_loop_legacy(step, ctx)
    if act_type == "break":
        ctx.should_break = True
        return
    if act_type == "continue":
        ctx.should_continue = True
        return

    # ── Data actions ──────────────────────────────────────────
    if act_type == "catch_ads":
        return _flow_catch_ads(step, ctx)
    if act_type == "search_query":
        return _flow_search_query(step, ctx)
    if act_type == "save_var":
        return _flow_save_var(step, ctx)
    if act_type == "extract_text":
        return _flow_extract_text(step, ctx)

    # ── External ──────────────────────────────────────────────
    if act_type == "http_request":
        return _flow_http_request(step, ctx)
    if act_type == "rotate_ip":
        return _flow_rotate_ip(step, ctx)

    # ── Extensions automation (Phase 5) ────────────────────────
    # Crypto wallets and other CWS extensions can be driven by
    # opening their popup in a new tab (chrome-extension://<id>/popup.html)
    # rather than the toolbar icon — toolbar popups close on focus
    # loss, the URL form stays open and is fully scriptable.
    if act_type == "open_extension_popup":
        return _flow_open_extension_popup(step, ctx)
    if act_type == "open_extension_page":
        return _flow_open_extension_page(step, ctx)
    if act_type == "extension_eval":
        return _flow_extension_eval(step, ctx)
    if act_type == "extension_wait_for":
        return _flow_extension_wait_for(step, ctx)
    if act_type == "extension_click":
        return _flow_extension_click(step, ctx)
    if act_type == "extension_fill":
        return _flow_extension_fill(step, ctx)
    if act_type == "extension_close":
        return _flow_extension_close(step, ctx)

    # ── Navigation & interaction: delegate to legacy handlers ─
    # These already accept (driver, action, ctx_dict); we adapt
    # the RunContext to the dict shape they expect.
    legacy_ctx = _legacy_ctx_from(ctx, step)
    legacy_handler = ACTION_HANDLERS.get(act_type)
    if legacy_handler is not None:
        # Pre-flight for per-ad-only actions: require ctx.ad
        if act_type in ("click_ad",) and not ctx.ad:
            log.warning(f"  [flow] {act_type} needs an ad in context — "
                        f"wrap in foreach_ad or run after search_query")
            return

        # ── SAFETY-CRITICAL: ad-class skip/only filter ──────────────
        #
        # The legacy pipeline (`_run_action_pipeline_for_ad`) honours
        # skip_on_my_domain / skip_on_target / only_on_target /
        # only_on_my_domain flags before invoking the action. The
        # unified runtime delegates to the same handlers via the
        # legacy bridge below, but historically did NOT apply those
        # filters here. Result: a user who set "skip_on_my_domain"
        # on a click_ad inside foreach_ad would still click their
        # OWN ad, burning CPC budget.
        #
        # We apply the filter against ctx.ad (current ad in foreach
        # loop). Domain match is exact OR wildcard subdomain
        # (ad_domain endswith "." + my_domain) -- same logic as the
        # legacy pipeline so behavior is identical between modes.
        # If ctx.ad is None (we're not inside a foreach_ad), the
        # filter is skipped silently -- meaningless without an ad.
        if ctx.ad:
            ad_domain = (ctx.ad.get("domain") or "").lower().strip()
            my_doms = {d.lower().strip()
                       for d in (ctx.loop_ctx.get("my_domains") or [])}
            is_mine = bool(ad_domain and any(
                ad_domain == d or ad_domain.endswith("." + d)
                for d in my_doms
            ))
            is_target = bool(ctx.ad.get("is_target"))

            if is_mine and step.get("skip_on_my_domain"):
                log.info(f"  [flow] skip {act_type} "
                         f"(ad on my_domain: {ad_domain})")
                return
            if is_target and step.get("skip_on_target"):
                log.info(f"  [flow] skip {act_type} "
                         f"(ad on target domain: {ad_domain})")
                return
            if step.get("only_on_target") and not is_target:
                log.info(f"  [flow] skip {act_type} "
                         f"(only_on_target, ad not target: {ad_domain})")
                return
            if step.get("only_on_my_domain") and not is_mine:
                log.info(f"  [flow] skip {act_type} "
                         f"(only_on_my_domain, ad not mine: {ad_domain})")
                return

        try:
            legacy_handler(ctx.driver, step, legacy_ctx)
        except Exception as e:
            log.warning(f"  [flow] {act_type} errored: "
                        f"{type(e).__name__}: {e}")
            if step.get("abort_on_error"):
                raise
        return

    # Loop-level legacy handlers (pause, visit_url, refresh, etc.)
    loop_handler = LOOP_ACTION_HANDLERS.get(act_type)
    if loop_handler is not None:
        try:
            loop_handler(ctx.browser, step, ctx.loop_ctx)
        except Exception as e:
            log.warning(f"  [flow] {act_type} errored: "
                        f"{type(e).__name__}: {e}")
            if step.get("abort_on_error"):
                raise
        return

    log.warning(f"  [flow] unknown action type: {act_type}")


def _legacy_ctx_from(ctx: RunContext, step: dict) -> dict:
    """Shape a dict that legacy per-ad handlers expect. They read
    ctx['ad'], ctx['my_domains'], ctx['query'] etc."""
    return {
        "ad":           ctx.ad or {},
        "ads":          ctx.ads,
        "my_domains":   ctx.loop_ctx.get("my_domains") or [],
        "target_domains": ctx.loop_ctx.get("target_domains") or [],
        "run_id":       ctx.run_id,
        "profile_name": ctx.profile_name,
        "query":        ctx.query,
        "item":         ctx.item,
        "vars":         ctx.vars,
    }


# ── if ─────────────────────────────────────────────────────────────

def _flow_if(step: dict, ctx: RunContext):
    cond = step.get("condition") or {}
    if _eval_condition(cond, ctx):
        _exec_steps(step.get("then_steps") or [], ctx)
    else:
        _exec_steps(step.get("else_steps") or [], ctx)


# ── foreach_ad ─────────────────────────────────────────────────────

def _flow_foreach_ad(step: dict, ctx: RunContext):
    """Iterate ctx.ads, run nested steps with ctx.ad = current ad.
    Respects break/continue within iterations.

    Comparison-shopping pattern (Task #17): between iterations (after
    one ad's click_ad has closed its tab and we're back on the SERP),
    do a short scan-pause that simulates a real user re-reading the
    SERP before clicking the next competitor's ad. Without this gap,
    foreach_ad fires click_ad N times back-to-back — a machine-perfect
    cadence that Google's ad-fraud heuristics flag.

    Default ON because it costs only 5-10s per ad gap and the realism
    boost is worth it. Disable via scan_between_ads=False on the step.
    """
    ads = list(ctx.ads or [])
    if not ads:
        log.info(f"  [foreach_ad] skipped — no ads in context")
        return
    inner = step.get("steps") or []
    shuffle = bool(step.get("shuffle", False))
    if shuffle:
        random.shuffle(ads)
    limit = step.get("limit")
    if limit:
        try: ads = ads[:int(limit)]
        except (TypeError, ValueError): pass

    # Comparison-shopping config (per-step overrideable from the UI)
    scan_enabled  = bool(step.get("scan_between_ads", True))
    scan_min      = float(step.get("scan_dwell_min", 3))
    scan_max      = float(step.get("scan_dwell_max", 8))
    scan_scroll   = bool(step.get("scan_scroll", True))

    log.info(f"  [foreach_ad] {len(ads)} ad(s)"
             f"{' (scan-between ON)' if scan_enabled else ''}")
    for i, ad in enumerate(ads, 1):
        if ctx.should_break:
            break
        # Scan-pause BETWEEN ads (not before the first, not after the last)
        if scan_enabled and i > 1:
            try:
                _foreach_ad_scan_pause(ctx, scan_min, scan_max, scan_scroll, i, len(ads))
            except Exception as _e:
                log.debug(f"    foreach_ad scan-pause skipped: {_e}")
        log.info(f"    [ad {i}/{len(ads)}] {ad.get('domain', '?')}")
        child = ctx.child(ad=ad)
        _exec_steps(inner, child)
        # Reset `continue` flag after the iteration that raised it
        child.should_continue = False
        if child.should_break:
            ctx.should_break = True
            break
    # Audit #103 #10: clear our own break flag once the loop has
    # exited so an INNER break doesn't accidentally short-circuit an
    # OUTER non-loop sibling. break is consumed by the loop it
    # targeted; once we leave that scope, downstream steps in the
    # parent should run normally.
    ctx.should_break = False
    ctx.should_continue = False


def _foreach_ad_scan_pause(ctx: RunContext, dwell_min: float, dwell_max: float,
                           scroll_enabled: bool, ad_idx: int, total: int):
    """Comparison-shopping pause between two foreach_ad iterations.
    Simulates a user re-reading the SERP: small scrolls + a randomised
    dwell. No-op if the driver isn't available or we're not on a
    Google SERP-like page."""
    drv = ctx.driver
    if not drv:
        return
    cur = ""
    try: cur = (drv.current_url or "")
    except Exception: pass
    # Only scan if we appear to be back on a search page. Otherwise
    # the previous iteration's click_ad may not have closed cleanly,
    # and scrolling would mess with the popup.
    if "/search" not in cur and "google." not in cur:
        log.debug(f"    scan-pause skipped (current URL not a SERP)")
        return
    dwell = random.uniform(dwell_min, dwell_max)
    log.info(f"    [scan {ad_idx-1}→{ad_idx}/{total}] re-reading SERP for {dwell:.1f}s")
    if scroll_enabled:
        # Couple of small scrolls — like a user glancing down/back up
        try:
            for _ in range(random.randint(1, 3)):
                amt = random.randint(80, 240)
                if random.random() < 0.4:
                    amt = -amt
                drv.execute_script(f"window.scrollBy(0, {amt});")
                time.sleep(random.uniform(0.6, 1.4))
        except Exception:
            pass
    # Remaining dwell budget — sit and "read"
    time.sleep(max(0, dwell - 2.0))


# ── generic foreach ────────────────────────────────────────────────

def _flow_foreach(step: dict, ctx: RunContext):
    """Iterate a custom list. `items` param is a list or a string
    reference like '{var.product_urls}'."""
    raw_items = step.get("items")
    if isinstance(raw_items, str):
        # Resolve a path reference like "{var.urls}"
        m = _VAR_PATTERN.match(raw_items.strip())
        if m:
            raw_items = ctx.resolve_path(m.group(1))
    if not isinstance(raw_items, list):
        # Textlist param — newline separated
        if isinstance(raw_items, str):
            raw_items = [line.strip() for line in raw_items.splitlines()
                         if line.strip()]
        else:
            raw_items = []
    if not raw_items:
        return
    if step.get("shuffle", False):
        random.shuffle(raw_items)
    item_var = step.get("item_var", "item")

    log.info(f"  [foreach] {len(raw_items)} item(s)")
    for i, item in enumerate(raw_items, 1):
        if ctx.should_break:
            break
        child = ctx.child(item=item)
        # Also make the item available under its custom var name, so
        # users can pick a meaningful label (item_var="query" → {query}).
        if item_var and item_var != "item":
            child.vars[item_var] = item
        log.info(f"    [item {i}/{len(raw_items)}] {str(item)[:60]}")
        _exec_steps(step.get("steps") or [], child)
        child.should_continue = False
        if child.should_break:
            ctx.should_break = True
            break
    # Audit #103 #10: same break-flag reset as foreach_ad.
    ctx.should_break = False
    ctx.should_continue = False


def _flow_loop_legacy(step: dict, ctx: RunContext):
    """Back-compat shim: old `loop` action had slightly different
    param shape. Normalize to foreach."""
    return _flow_foreach(step, ctx)


# ── catch_ads ──────────────────────────────────────────────────────

def _flow_catch_ads(step: dict, ctx: RunContext):
    """Parse ads on the current page into ctx.ads. Separate from
    search_query so users can visit a page manually, then collect
    whatever ads happen to be there.

    Also refresh ctx.flags["captcha_present"] (audit #103 #3): the
    `if captcha_present` condition was previously dangling — defined
    in _eval_condition_raw but never written by any flow handler, so
    user scripts that branched on it were silently broken. catch_ads
    is a natural sync point: we just inspected the current page, so
    if it's a captcha gate we want any subsequent `if captcha_present
    -> rotate_ip` step to actually fire.
    """
    try:
        from ghost_shell.main import parse_ads, is_captcha_page
    except ImportError:
        log.warning("  [catch_ads] main.parse_ads not available")
        return
    query = step.get("query") or ctx.query or ""
    ads = parse_ads(ctx.driver, query) or []
    ctx.ads = ads
    ctx.flags["ads_found"] = len(ads) > 0
    try:
        ctx.flags["captcha_present"] = bool(
            is_captcha_page(ctx.driver) if ctx.driver else False
        )
    except Exception:
        ctx.flags["captcha_present"] = False
    log.info(f"  [catch_ads] collected {len(ads)} ad(s)"
             + (" — CAPTCHA detected" if ctx.flags.get("captcha_present") else ""))


# ── search_query (unified wrapper) ────────────────────────────────

def _flow_search_query(step: dict, ctx: RunContext):
    """Unified search_query — calls the loop_ctx's search callback
    (which does Google navigation + parse) and stores results in
    ctx.ads. Previously this also ran the per-ad pipeline automatically
    — in the new model, the user chains a `foreach_ad` step themselves
    to make that explicit.

    Defensive resolution (April 2026): if the step's query template
    interpolates to an empty string -- typical cause: the script writes
    `query: "{item}"` but the surrounding loop variable is shadowed or
    the loop's item_var is not "item" -- we fall back to ctx.item or
    ctx.query before giving up. Without this, an empty query made the
    runtime silently skip the navigation + then refresh whatever stale
    page Chrome was on (fingerprint.com from the previous probe run,
    a blank tab, etc), which looked like Google was returning 0 ads
    when in reality we never visited Google.
    """
    raw_q = step.get("query") or ""
    q = raw_q.strip() if isinstance(raw_q, str) else str(raw_q).strip()

    # Fallback ladder when interpolation gave us nothing useful
    if not q:
        # Maybe the template used a var name that doesn't exist
        # (typo / wrong item_var). ctx.item is set by foreach.
        if ctx.item:
            q = str(ctx.item).strip()
            log.warning(
                f"  [search_query] template query was empty after "
                f"interpolation -- falling back to ctx.item={q!r}. "
                f"Check your script: query field should be \"{{item}}\" "
                f"or \"{{var.<your_item_var>}}\""
            )
        elif ctx.query:
            q = str(ctx.query).strip()
            log.warning(
                f"  [search_query] empty query, reusing previous "
                f"ctx.query={q!r}"
            )

    if not q:
        # Genuinely nothing to search. Log MORE than the old terse
        # "empty query, skipping" line so the user can debug.
        log.warning(
            f"  [search_query] empty query, skipping. "
            f"raw template={raw_q!r}, ctx.item={ctx.item!r}, "
            f"ctx.vars keys={list(ctx.vars.keys())[:8]}"
        )
        return

    search_fn = ctx.loop_ctx.get("search_query")
    if not search_fn:
        log.warning("  [search_query] no runner in loop_ctx")
        return
    log.info(f"  [search_query] {q!r}")
    ctx.query = q
    ads = search_fn(q) or []
    ctx.ads = ads
    ctx.flags["ads_found"] = len(ads) > 0
    # Refresh captcha flag (audit #103 #3) so downstream
    # `if captcha_present -> rotate_ip` branches actually fire.
    try:
        from ghost_shell.main import is_captcha_page
        ctx.flags["captcha_present"] = bool(
            is_captcha_page(ctx.driver) if ctx.driver else False
        )
    except Exception:
        ctx.flags["captcha_present"] = False
    if ctx.flags.get("captcha_present"):
        log.warning(f"  [search_query] CAPTCHA detected on result page "
                    f"for query {q!r} — captcha_present flag set")

    # Back-compat: if the step has `auto_foreach` (legacy behavior),
    # also run the per_ad_runner for each ad right here. The migration
    # logic sets auto_foreach=False; fresh scripts should use an
    # explicit foreach_ad wrapper.
    if step.get("auto_foreach"):
        per_ad = ctx.loop_ctx.get("per_ad_runner")
        if per_ad and ads:
            for ad in ads:
                per_ad(ad, q)


# ── save_var ───────────────────────────────────────────────────────

# Audit #103 #7: whitelist for save_var/extract_text variable names.
# Reject empty / dotted / prototype-pollution-shaped keys so a
# malformed step can't shadow ctx.vars structural keys (e.g. "vault",
# "ad", "item") or inject keys with "." that confuse the resolve_path
# walker into hitting ctx.ad / ctx.ads accidentally.
_VAR_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_VAR_NAME_RESERVED = {"vault", "ad", "ads", "item", "query", "profile",
                      "flag", "var", "_runtime_raw"}
# Audit #103 #8: cap individual var values to 100 KB so a runaway
# extract_text on a giant page (or a save_var literal from a
# user-pasted blob) doesn't bloat ctx.vars.
_VAR_VALUE_CAP_BYTES = 100 * 1024

def _validate_var_name(name) -> str | None:
    """Return the validated name, or None if it should be rejected."""
    if not isinstance(name, str) or not name:
        return None
    if not _VAR_NAME_PATTERN.match(name):
        return None
    if name in _VAR_NAME_RESERVED:
        return None
    return name

def _cap_var_value(val):
    """Truncate string values that exceed the per-var cap. Lists of
    strings get per-element truncation; nested dicts pass through
    unchanged (callers should size-check before passing)."""
    if isinstance(val, str):
        if len(val) > _VAR_VALUE_CAP_BYTES:
            return val[:_VAR_VALUE_CAP_BYTES] + " …(truncated)"
        return val
    if isinstance(val, list):
        out = []
        for v in val:
            if isinstance(v, str) and len(v) > _VAR_VALUE_CAP_BYTES:
                out.append(v[:_VAR_VALUE_CAP_BYTES] + " …(truncated)")
            else:
                out.append(v)
        return out
    return val


def _flow_save_var(step: dict, ctx: RunContext):
    """Save a literal or computed value into ctx.vars[name]. Useful
    for counters, flags, or pre-computed paths that downstream steps
    reference via {var.name}.

    Validates `name` (audit #103 #7) so a malformed step can't pollute
    ctx with reserved keys or path-fragmenting strings.
    """
    raw_name = step.get("name")
    name = _validate_var_name(raw_name)
    if not name:
        log.warning(
            f"  [save_var] rejected name {raw_name!r}: must match "
            f"[a-zA-Z_][a-zA-Z0-9_]* and not be a reserved key "
            f"({', '.join(sorted(_VAR_NAME_RESERVED))})"
        )
        return
    # `value` is interpolated already by _exec_steps, so {ad.domain}
    # and friends resolve before we store them.
    ctx.vars[name] = _cap_var_value(step.get("value"))
    log.info(f"  [save_var] {name} = {str(ctx.vars[name])[:80]!r}")


# ── extract_text ───────────────────────────────────────────────────

def _flow_extract_text(step: dict, ctx: RunContext):
    """Run a CSS selector, take .textContent of the first (or all)
    matching element(s), save into ctx.vars under `save_as`.

    Caps extracted text size (audit #103 #8) so a page with multiple
    megabytes of hidden DOM doesn't blow ctx.vars.
    """
    if not ctx.driver:
        return
    sel = step.get("selector") or ""
    if not sel:
        log.warning("  [extract_text] missing selector")
        return
    raw_save_as = step.get("save_as") or "extracted"
    save_as = _validate_var_name(raw_save_as)
    if not save_as:
        log.warning(
            f"  [extract_text] rejected save_as={raw_save_as!r} "
            f"(invalid var name)"
        )
        return
    multi   = bool(step.get("all"))
    try:
        from selenium.webdriver.common.by import By
        if multi:
            els = ctx.driver.find_elements(By.CSS_SELECTOR, sel)
            val = [e.text for e in els]
        else:
            el = ctx.driver.find_element(By.CSS_SELECTOR, sel)
            val = el.text
    except Exception as e:
        log.warning(f"  [extract_text] {sel!r}: {e}")
        val = "" if not multi else []
    ctx.vars[save_as] = _cap_var_value(val)
    shown = str(ctx.vars[save_as])[:80] if isinstance(ctx.vars[save_as], str) \
            else f"list({len(ctx.vars[save_as])})"
    log.info(f"  [extract_text] {save_as} = {shown}")


# ── http_request ───────────────────────────────────────────────────

def _flow_http_request(step: dict, ctx: RunContext):
    """Fire a plain-HTTP request (NOT through the browser). Typical
    use: fire a webhook when something interesting happens.

    Goes DIRECT (not through the user's proxy) so the webhook target
    sees your actual server IP, not the rotating residential. That's
    usually what you want for webhooks — change later if you need
    proxy routing.
    """
    try:
        import requests
    except ImportError:
        log.warning("  [http_request] requests library not installed")
        return

    method = (step.get("method") or "GET").upper()
    url    = step.get("url") or ""
    if not url:
        log.warning("  [http_request] missing url")
        return

    # Audit #103 #5: validate URL — reject schemes other than http(s),
    # block localhost / loopback / link-local / RFC1918 internal IPs.
    # Without this, a user-authored script could exfiltrate data from
    # internal services (Redis on :6379, Elasticsearch on :9200, etc.)
    # accessible from the runner host. The flow runtime is essentially
    # arbitrary code from the user, but we still want this defensive
    # because scripts get IMPORTED from .json files, run on schedule,
    # and operators don't always read the bodies before importing.
    import urllib.parse as _up
    try:
        _parsed = _up.urlparse(url)
    except Exception:
        log.warning(f"  [http_request] url failed to parse: {url[:80]!r}")
        return
    if _parsed.scheme.lower() not in ("http", "https"):
        log.warning(
            f"  [http_request] rejected non-http(s) scheme "
            f"{_parsed.scheme!r} in {url[:80]!r}"
        )
        return
    _host = (_parsed.hostname or "").lower()
    _blocked_host_prefixes = (
        "localhost", "127.", "0.0.0.0", "::1",
        "169.254.",                                  # link-local / cloud metadata
        "10.", "192.168.",                           # RFC1918
    )
    _blocked_host_exact = {
        "localhost.localdomain", "broadcasthost",
    }
    if (_host in _blocked_host_exact or
            any(_host.startswith(p) for p in _blocked_host_prefixes) or
            (_host.startswith("172.") and _host.split(".")[1].isdigit() and
             16 <= int(_host.split(".")[1]) <= 31)):
        log.warning(
            f"  [http_request] rejected internal/loopback host "
            f"{_host!r} in {url[:80]!r} — http_request is for external "
            f"webhooks only, internal services would exfiltrate data"
        )
        return

    headers = step.get("headers") or {}
    # Body can be a dict (auto-JSON) or a raw string
    body = step.get("body")
    timeout = float(step.get("timeout", 15))
    save_as = step.get("save_as")
    # Audit #103 #6: cap response size at 1 MB. Without this, a
    # webhook target that returns a 500 MB JSON blob would pull the
    # whole thing into memory and store it in ctx.vars. Use
    # stream=True + Read up to a hard limit, fail loud if exceeded.
    MAX_RESP_BYTES = 1 * 1024 * 1024  # 1 MB

    try:
        kwargs = {"headers": headers, "timeout": timeout, "stream": True}
        if isinstance(body, dict):
            kwargs["json"] = body
        elif body is not None:
            kwargs["data"] = body

        with requests.request(method, url, **kwargs) as resp:
            log.info(f"  [http_request] {method} {url[:60]} → {resp.status_code}")

            # Streamed read with hard cap.
            chunks = []
            total = 0
            try:
                for chunk in resp.iter_content(chunk_size=8192,
                                                decode_unicode=False):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_RESP_BYTES:
                        log.warning(
                            f"  [http_request] response > {MAX_RESP_BYTES} "
                            f"bytes — truncating (use a smaller endpoint)"
                        )
                        break
                    chunks.append(chunk)
            except Exception as _re:
                log.debug(f"  [http_request] body read aborted: {_re}")

            raw = b"".join(chunks)
            text = ""
            try:
                text = raw.decode(resp.encoding or "utf-8", errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")

            if save_as:
                # Try JSON first only if it doesn't bloat ctx.vars too
                # much; cap text fallback at 10 KB as before.
                try:
                    import json as _json
                    parsed = _json.loads(text) if text else None
                    if parsed is not None:
                        ctx.vars[save_as] = parsed
                    else:
                        ctx.vars[save_as] = {
                            "status": resp.status_code,
                            "text":   text[:10000],
                        }
                except Exception:
                    ctx.vars[save_as] = {
                        "status": resp.status_code,
                        "text":   text[:10000],
                    }
    except Exception as e:
        log.warning(f"  [http_request] {type(e).__name__}: {e}")
        if step.get("abort_on_error"):
            raise


# ── rotate_ip (thin wrapper) ──────────────────────────────────────

def _flow_rotate_ip(step: dict, ctx: RunContext):
    rotate = ctx.loop_ctx.get("rotate_ip")
    if not rotate:
        log.debug("  [rotate_ip] no rotate callback in loop_ctx")
        return
    try:
        new_ip = rotate()
        if new_ip:
            log.info(f"  [rotate_ip] now on {new_ip}")
            ctx.vars["last_rotated_ip"] = new_ip
    except Exception as e:
        log.warning(f"  [rotate_ip] {type(e).__name__}: {e}")
    # Optional pause after
    wait = float(step.get("wait_after_sec", 0))
    if wait > 0:
        time.sleep(wait)


# ════════════════════════════════════════════════════════════════
# UNIFIED CATALOG — metadata for the new action types
# ════════════════════════════════════════════════════════════════

# Condition kinds exposed to the UI. Each entry knows which params
# the condition needs; the Scripts inspector renders a picker based
# on this. Keep labels short — they render inline in step summaries.
CONDITION_KINDS = [
    {"kind": "always",           "label": "Always run",
     "group": "simple"},
    {"kind": "ads_found",        "label": "Ads were found",
     "group": "ads"},
    {"kind": "no_ads",           "label": "No ads found",
     "group": "ads"},
    {"kind": "ads_count_gte",    "label": "Ads count ≥ N",
     "group": "ads", "fields": [
         {"name": "value", "type": "number", "default": 2,
          "label": "Minimum count"}
     ]},
    # Ad-classification predicates. NOTE the difference between
    # "competitor" (strict: not mine AND not target) and "external"
    # (loose: not mine — includes targets). Pick "external" if you
    # plan to use only_on_target / skip_on_target inside the branch
    # to fine-tune which ads to act on; pick "competitor" if you want
    # the gate to exclude targets up-front.
    {"kind": "ad_is_competitor", "label": "Ad is pure competitor (not mine, not target)",
     "group": "ads", "needs_ad": True,
     "hint": "Strict: excludes target-domain ads from this branch. "
             "If you intend to also act on target-domain ads here, "
             "use 'Ad is external' instead — that branch lets the "
             "click_ad's only_on_target / skip_on_target flags decide."},
    {"kind": "ad_is_external",   "label": "Ad is external (not mine — incl. targets)",
     "group": "ads", "needs_ad": True,
     "hint": "Loose: any ad not on your domain — covers competitors "
             "AND target-domain ads. Use this when you want the per-"
             "action only_on_target / skip_on_target flags to fine-"
             "tune which ads to click inside the branch."},
    {"kind": "ad_is_target",     "label": "Ad is target-domain",
     "group": "ads", "needs_ad": True},
    {"kind": "ad_is_mine",       "label": "Ad is my-domain",
     "group": "ads", "needs_ad": True},
    {"kind": "captcha_present",  "label": "Captcha on page",
     "group": "page"},
    {"kind": "url_contains",     "label": "URL contains…",
     "group": "page", "fields": [
         {"name": "value", "type": "text",
          "placeholder": "/checkout", "label": "Substring"}
     ]},
    {"kind": "element_exists",   "label": "Element exists (CSS)",
     "group": "page", "fields": [
         {"name": "selector", "type": "text",
          "placeholder": ".product-card", "label": "Selector"}
     ]},
    {"kind": "var_equals",       "label": "Variable equals…",
     "group": "vars", "fields": [
         {"name": "var",   "type": "text", "placeholder": "var.username"},
         {"name": "value", "type": "text", "placeholder": "anton"},
     ]},
    {"kind": "var_contains",     "label": "Variable contains…",
     "group": "vars", "fields": [
         {"name": "var",   "type": "text", "placeholder": "var.response.text"},
         {"name": "value", "type": "text", "placeholder": "success"},
     ]},
    {"kind": "var_matches",      "label": "Variable matches regex",
     "group": "vars", "fields": [
         {"name": "var",   "type": "text", "placeholder": "var.email"},
         {"name": "value", "type": "text", "placeholder": r"^[\w.]+@"},
     ]},
    {"kind": "var_empty",        "label": "Variable is empty",
     "group": "vars", "fields": [
         {"name": "var", "type": "text", "placeholder": "var.result"}
     ]},
]



# ════════════════════════════════════════════════════════════════
# EXTENSIONS automation (Phase 5)
#
# Drive Chrome extensions (crypto wallets, ad blockers, dev tools)
# from a script. Pattern: open the extension's popup HTML in a new
# tab, drive it with normal Selenium calls, then close the tab.
#
# Why "open in a new tab" rather than triggering the toolbar icon:
#   - Selenium can't click toolbar icons reliably (they're outside
#     the page's DOM)
#   - Toolbar popups (the ones that pop down from the icon) close
#     on focus loss — any Selenium call that switches tabs kills
#     the popup mid-flow
#   - The popup HTML itself works fine in a regular tab — wallets
#     check their own context, not "are you in a real popup"
# So: navigate to chrome-extension://<id>/popup.html in a new tab,
# do the work, close the tab. Persistent storage (seed phrase,
# unlock state) lives in the extension's IndexedDB which survives
# tab close.
# ════════════════════════════════════════════════════════════════

def _ext_resolve_id(step: dict, ctx: "RunContext") -> "str | None":
    """Resolve the extension id from a step. Accepts:
       - extension_id (32-char id directly, preferred)
       - extension_name (looks up DB by case-insensitive name match,
         picks the only match or warns if ambiguous)
    Returns the id or None (with a warning logged)."""
    eid = (step.get("extension_id") or "").strip().lower()
    if eid:
        return eid
    name = (step.get("extension_name") or "").strip()
    if not name:
        log.warning("  [ext] no extension_id or extension_name given")
        return None
    try:
        from ghost_shell.db import get_db
        rows = get_db().extension_list()
    except Exception as e:
        log.warning(f"  [ext] DB lookup failed: {e}")
        return None
    matches = [r for r in rows if (r.get("name") or "").lower() == name.lower()]
    if not matches:
        # Fallback: substring match
        matches = [r for r in rows if name.lower() in (r.get("name") or "").lower()]
    if not matches:
        log.warning(f"  [ext] no extension named '{name}' in pool")
        return None
    if len(matches) > 1:
        log.warning(f"  [ext] '{name}' is ambiguous ({len(matches)} matches); "
                    f"picking first ({matches[0].get('id')})")
    return matches[0].get("id")


def _ext_popup_url(ext_id: str, page: str = None) -> "str | None":
    """Build chrome-extension://<id>/<page>. If page is None, look
    up the manifest's default_popup. Returns None if the extension
    has no popup and no page is specified."""
    if page:
        return f"chrome-extension://{ext_id}/{page.lstrip('/')}"
    # Look up the manifest from the pool to find default_popup
    try:
        from ghost_shell.db import get_db
        row = get_db().extension_get(ext_id)
        if row and row.get("manifest_json"):
            import json as _j
            manifest = _j.loads(row["manifest_json"])
            action = manifest.get("action") or manifest.get("browser_action") or {}
            popup = action.get("default_popup")
            if popup:
                return f"chrome-extension://{ext_id}/{popup.lstrip('/')}"
    except Exception as e:
        log.debug(f"  [ext] manifest lookup failed: {e}")
    return None


def _ext_open_in_new_tab(ctx: "RunContext", url: str,
                         save_handle_as: str = None) -> "str | None":
    """Open `url` in a new tab and switch to it. Records the new tab
    handle in ctx.vars (default key "ext_tab", or `save_handle_as`)
    plus ctx.vars["_ext_origin_tab"] for cleanup."""
    drv = ctx.driver
    if not drv:
        log.warning("  [ext] no driver in context")
        return None
    origin = drv.current_window_handle
    drv.execute_script("window.open(arguments[0], '_blank');", url)
    # Wait briefly for the new tab and switch to it
    import time as _t
    deadline = _t.time() + 5
    while _t.time() < deadline:
        handles = drv.window_handles
        if len(handles) > 1 and handles[-1] != origin:
            drv.switch_to.window(handles[-1])
            ctx.vars["_ext_origin_tab"] = origin
            ctx.vars[save_handle_as or "ext_tab"] = handles[-1]
            return handles[-1]
        _t.sleep(0.1)
    log.warning("  [ext] new tab did not appear within 5s")
    return None


# ── open_extension_popup ──────────────────────────────────────────

def _flow_open_extension_popup(step: dict, ctx: "RunContext"):
    """Open the extension's default popup HTML in a new tab, then
    optionally wait for a selector to appear (typical: the unlock
    screen for a wallet)."""
    ext_id = _ext_resolve_id(step, ctx)
    if not ext_id:
        return
    url = _ext_popup_url(ext_id)
    if not url:
        log.warning(f"  [ext] {ext_id} has no default_popup in manifest — "
                    f"use open_extension_page with explicit `page` instead")
        return
    log.info(f"  [open_extension_popup] {ext_id} → {url}")
    _ext_open_in_new_tab(ctx, url, step.get("save_handle_as"))

    wait_sel = step.get("wait_for_selector")
    if wait_sel:
        timeout = float(step.get("timeout", 15))
        _ext_wait_for_selector(ctx, wait_sel, timeout)


# ── open_extension_page ───────────────────────────────────────────

def _flow_open_extension_page(step: dict, ctx: "RunContext"):
    """Open an arbitrary extension page (popup.html, options.html,
    home.html, sidepanel.html). Useful when the extension uses a
    non-standard popup name (MetaMask uses popup.html, OKX uses
    home.html, Phantom uses popup.html)."""
    ext_id = _ext_resolve_id(step, ctx)
    if not ext_id:
        return
    page = (step.get("page") or "popup.html").strip()
    url = _ext_popup_url(ext_id, page=page)
    log.info(f"  [open_extension_page] {ext_id} / {page}")
    _ext_open_in_new_tab(ctx, url, step.get("save_handle_as"))

    wait_sel = step.get("wait_for_selector")
    if wait_sel:
        timeout = float(step.get("timeout", 15))
        _ext_wait_for_selector(ctx, wait_sel, timeout)


# ── extension_eval ────────────────────────────────────────────────

def _flow_extension_eval(step: dict, ctx: "RunContext"):
    """Run JavaScript in the currently-active extension tab. Saves
    the return value to ctx.vars[save_as] if given.

    Distinct from a generic `eval` because it asserts we're actually
    inside a chrome-extension:// page — most wallets refuse to expose
    APIs to non-extension contexts."""
    drv = ctx.driver
    if not drv:
        return
    code = step.get("code") or ""
    if not code:
        log.warning("  [extension_eval] empty code")
        return
    cur_url = drv.current_url
    if not cur_url.startswith("chrome-extension://"):
        log.warning(f"  [extension_eval] not in an extension tab "
                    f"(current: {cur_url[:60]}). Run open_extension_popup first.")
        return
    try:
        result = drv.execute_script(code)
        save_as = step.get("save_as")
        if save_as:
            ctx.vars[save_as] = result
        log.info(f"  [extension_eval] OK ({len(code)} chars)")
    except Exception as e:
        log.warning(f"  [extension_eval] {type(e).__name__}: {e}")
        if step.get("abort_on_error"):
            raise


# ── extension_wait_for ────────────────────────────────────────────

def _ext_wait_for_selector(ctx: "RunContext", selector: str, timeout: float):
    """Poll for a selector to appear in the current tab. Returns the
    element or None on timeout."""
    drv = ctx.driver
    if not drv:
        return None
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    try:
        by = (By.CSS_SELECTOR, selector)
        return WebDriverWait(drv, timeout).until(EC.presence_of_element_located(by))
    except Exception as e:
        log.warning(f"  [ext] wait timeout '{selector[:50]}': {type(e).__name__}")
        return None


def _flow_extension_wait_for(step: dict, ctx: "RunContext"):
    sel = step.get("selector") or ""
    timeout = float(step.get("timeout", 15))
    if not sel:
        log.warning("  [extension_wait_for] missing selector")
        return
    el = _ext_wait_for_selector(ctx, sel, timeout)
    if el and step.get("save_as"):
        # Save the element's text content (most useful thing) as a var
        try:
            ctx.vars[step["save_as"]] = el.text or ""
        except Exception:
            pass


# ── extension_click ───────────────────────────────────────────────

def _flow_extension_click(step: dict, ctx: "RunContext"):
    """Click an element inside the open extension popup. Implicitly
    waits for the selector to appear (default 10s)."""
    sel = step.get("selector") or ""
    if not sel:
        log.warning("  [extension_click] missing selector")
        return
    timeout = float(step.get("timeout", 10))
    el = _ext_wait_for_selector(ctx, sel, timeout)
    if not el:
        return
    try:
        # Scroll into view first — extension popups are often small
        # and clickable elements may be partially off-screen.
        ctx.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        import time as _t; _t.sleep(0.15)
        el.click()
        log.info(f"  [extension_click] clicked '{sel[:50]}'")
    except Exception as e:
        log.warning(f"  [extension_click] {type(e).__name__}: {e}")
        if step.get("abort_on_error"):
            raise


# ── extension_fill ────────────────────────────────────────────────

def _flow_extension_fill(step: dict, ctx: "RunContext"):
    """Fill a text field in the open extension popup. Use {vault.x.y}
    placeholders for sensitive values — the runner pre-resolves these
    against the credential vault so secrets never appear in the
    saved script."""
    sel = step.get("selector") or ""
    val = step.get("value") or ""
    if not sel:
        log.warning("  [extension_fill] missing selector")
        return
    timeout = float(step.get("timeout", 10))
    el = _ext_wait_for_selector(ctx, sel, timeout)
    if not el:
        return
    try:
        if step.get("clear_first", True):
            try: el.clear()
            except Exception: pass
        # Prefer typing one key at a time so React-style listeners fire
        if step.get("typewriter", True):
            for ch in val:
                el.send_keys(ch)
                import time as _t; _t.sleep(0.02)
        else:
            el.send_keys(val)
        log.info(f"  [extension_fill] '{sel[:40]}' ← {len(val)} chars")
    except Exception as e:
        log.warning(f"  [extension_fill] {type(e).__name__}: {e}")
        if step.get("abort_on_error"):
            raise


# ── extension_close ───────────────────────────────────────────────

def _flow_extension_close(step: dict, ctx: "RunContext"):
    """Close the extension tab and switch back to the original tab.
    Looks up the saved handles from ctx.vars (set by
    open_extension_*). Safe no-op if the tab is already gone."""
    drv = ctx.driver
    if not drv:
        return
    target = ctx.vars.get(step.get("handle") or "ext_tab")
    origin = ctx.vars.get("_ext_origin_tab")
    try:
        if target and target in drv.window_handles:
            drv.switch_to.window(target)
            drv.close()
        if origin and origin in drv.window_handles:
            drv.switch_to.window(origin)
        log.info("  [extension_close] tab closed")
    except Exception as e:
        log.warning(f"  [extension_close] {type(e).__name__}: {e}")

def _unified_catalog() -> list[dict]:
    """Catalog entries for the new unified-flow actions. Merged into
    the main action_catalog() result. Every entry has `category` so
    the redesigned palette can group by function rather than scope."""
    return [
        # ── FLOW CONTROL ─────────────────────────────────────
        {
            "type":        "if",
            "label":       "If / Else",
            "category":    "flow",
            "scope":       "any",
            "description": "Conditional branching. Runs then-steps when "
                           "the condition is true, else-steps otherwise. "
                           "Conditions include ad/page/variable predicates.",
            "is_container": True,
            "params": [
                {"name": "condition", "type": "condition",
                 "default": {"kind": "always"},
                 "label": "Condition",
                 "hint": "Choose a predicate. Everything inside will run "
                         "only when it evaluates to true."},
                {"name": "then_steps", "type": "steps", "default": [],
                 "label": "Then (do these steps)"},
                {"name": "else_steps", "type": "steps", "default": [],
                 "label": "Else (optional — when condition is false)"},
            ],
        },
        {
            "type":        "foreach_ad",
            "label":       "For each advertisement",
            "category":    "flow",
            "scope":       "any",
            "description": "Iterate ads captured by the most recent "
                           "search_query or catch_ads. Nested steps see "
                           "the current ad via {ad.domain}, {ad.title}, "
                           "{ad.clean_url}.",
            "is_container": True,
            "params": [
                {"name": "shuffle", "type": "bool", "default": False,
                 "label": "Shuffle order",
                 "hint": "Randomize the iteration order. Useful to not "
                         "always click the #1 ad first."},
                {"name": "limit",   "type": "number", "default": "",
                 "label": "Limit", "placeholder": "all",
                 "hint": "Cap on how many ads to process. Leave blank "
                         "for all."},
                {"name": "steps", "type": "steps", "default": [],
                 "label": "Steps (run once per ad)"},
            ],
        },
        {
            "type":        "foreach",
            "label":       "For each item",
            "category":    "flow",
            "scope":       "any",
            "description": "Iterate a custom list (one per line, or a "
                           "{var.xxx} reference). Inside, use {item} — or "
                           "set a custom name in the inspector.",
            "is_container": True,
            "params": [
                {"name": "items",    "type": "textlist", "default": "",
                 "label": "Items (one per line)",
                 "hint": "Or reference a variable: {var.product_urls}"},
                {"name": "item_var", "type": "text", "default": "item",
                 "label": "Variable name",
                 "hint": "Use as {item} or {your_name}."},
                {"name": "shuffle",  "type": "bool", "default": True,
                 "label": "Shuffle order"},
                {"name": "steps",    "type": "steps", "default": [],
                 "label": "Steps"},
            ],
        },
        {
            "type":        "break",
            "label":       "Break loop",
            "category":    "flow",
            "scope":       "any",
            "description": "Stop the innermost foreach/loop immediately. "
                           "Use inside an if to bail out conditionally.",
            "params": [],
        },
        {
            "type":        "continue",
            "label":       "Skip to next iteration",
            "category":    "flow",
            "scope":       "any",
            "description": "Jump to the next iteration of the innermost "
                           "loop, skipping remaining steps.",
            "params": [],
        },

        # ── DATA ──────────────────────────────────────────────
        {
            "type":        "catch_ads",
            "label":       "Catch ads on current page",
            "category":    "data",
            "scope":       "any",
            "description": "Parse ads on whatever page we're on, save "
                           "them to the context as `ads`. Doesn't search "
                           "Google — use after visit_url on any SERP "
                           "or ad-containing page.",
            "params": [
                {"name": "query", "type": "text", "default": "",
                 "label": "Query label (for stats)",
                 "hint": "Optional — what to record in the DB as the "
                         "query this run. Leave blank to keep the "
                         "current query context."},
            ],
        },
        {
            "type":        "save_var",
            "label":       "Save variable",
            "category":    "data",
            "scope":       "any",
            "description": "Save a value (literal or interpolated) to "
                           "a named variable. Read it later as {var.name}.",
            "params": [
                {"name": "name",  "type": "text", "required": True,
                 "label": "Variable name",
                 "placeholder": "counter"},
                {"name": "value", "type": "text", "default": "",
                 "label": "Value",
                 "hint": "Can reference other variables: "
                         "'{ad.domain} at {query}'"},
            ],
        },
        {
            "type":        "extract_text",
            "label":       "Extract text from element",
            "category":    "data",
            "scope":       "any",
            "description": "Run a CSS selector and save the element's "
                           "text content to a variable.",
            "params": [
                {"name": "selector", "type": "text", "required": True,
                 "label": "CSS selector",
                 "placeholder": ".price-tag"},
                {"name": "save_as",  "type": "text", "default": "extracted",
                 "label": "Save as",
                 "hint": "Access later as {var.<name>}"},
                {"name": "all", "type": "bool", "default": False,
                 "label": "All matches",
                 "hint": "When on: saves a list of strings. "
                         "When off: saves only the first match."},
            ],
        },

        # ── EXTERNAL ─────────────────────────────────────────
        {
            "type":        "http_request",
            "label":       "HTTP request (webhook)",
            "category":    "external",
            "scope":       "any",
            "description": "Fire a HTTP request — typical use is a "
                           "webhook to notify your Slack/Discord/etc. "
                           "Does NOT route through the browser's proxy.",
            "params": [
                {"name": "method", "type": "select", "default": "POST",
                 "options": ["GET", "POST", "PUT", "DELETE"],
                 "label": "Method"},
                {"name": "url", "type": "text", "required": True,
                 "label": "URL",
                 "placeholder": "https://hooks.slack.com/services/..."},
                {"name": "body", "type": "textarea", "default": "",
                 "label": "Body (JSON)",
                 "hint": 'Plain text or JSON like {"text": "ad clicked: '
                         '{ad.domain}"}. Variables interpolate.'},
                {"name": "save_as", "type": "text", "default": "",
                 "label": "Save response as",
                 "hint": "Optional. The response JSON (or text) lands "
                         "in var.<save_as>."},
                {"name": "timeout", "type": "number", "default": 15,
                 "label": "Timeout (s)"},
            ],
        },
        # ── EXTENSIONS (Phase 5) ─────────────────────────────
        # All extension actions take an extension picker (the UI
        # populates an "extension_id" select from /api/extensions).
        # The "extension_name" fallback is for hand-edited scripts —
        # the runner does case-insensitive substring matching.
        {
            "type":        "open_extension_popup",
            "label":       "Open extension popup",
            "category":    "extensions",
            "scope":       "any",
            "description": "Open an installed extension's popup in a "
                           "new tab (the popup HTML, not the toolbar "
                           "icon). Toolbar popups close on focus loss; "
                           "the URL form stays open and is fully "
                           "scriptable. Required first step before any "
                           "other extension_* action.",
            "params": [
                {"name": "extension_id", "type": "extension", "required": False,
                 "label": "Extension",
                 "hint": "Pick from the pool. Or set extension_name "
                         "below if you prefer a name match."},
                {"name": "extension_name", "type": "text", "default": "",
                 "label": "…or by name",
                 "placeholder": "MetaMask, OKX Wallet, Phantom",
                 "hint": "Case-insensitive substring match against the "
                         "pool. Use only if extension_id is blank."},
                {"name": "wait_for_selector", "type": "text", "default": "",
                 "label": "Wait for selector",
                 "placeholder": ".unlock-page, [data-testid='unlock-button']",
                 "hint": "Wait for this CSS selector after opening so "
                         "the popup has finished its initial render."},
                {"name": "timeout", "type": "number", "default": 15,
                 "label": "Wait timeout (s)"},
                {"name": "save_handle_as", "type": "text", "default": "ext_tab",
                 "label": "Save tab handle as",
                 "hint": "Variable name for the tab handle. Default "
                         "ext_tab — extension_close uses this name."},
            ],
        },
        {
            "type":        "open_extension_page",
            "label":       "Open extension page (custom)",
            "category":    "extensions",
            "scope":       "any",
            "description": "Like open_extension_popup but lets you "
                           "pick a non-default page. OKX Wallet uses "
                           "home.html, Coinbase uses index.html, "
                           "MetaMask uses popup.html. Check the "
                           "extension's manifest to find the right one.",
            "params": [
                {"name": "extension_id", "type": "extension", "required": False,
                 "label": "Extension"},
                {"name": "extension_name", "type": "text", "default": "",
                 "label": "…or by name"},
                {"name": "page", "type": "text", "default": "popup.html",
                 "label": "Page",
                 "placeholder": "popup.html / options.html / home.html"},
                {"name": "wait_for_selector", "type": "text", "default": "",
                 "label": "Wait for selector"},
                {"name": "timeout", "type": "number", "default": 15,
                 "label": "Wait timeout (s)"},
                {"name": "save_handle_as", "type": "text", "default": "ext_tab",
                 "label": "Save tab handle as"},
            ],
        },
        {
            "type":        "extension_wait_for",
            "label":       "Wait for element in extension",
            "category":    "extensions",
            "scope":       "any",
            "description": "Pause until a CSS selector appears in the "
                           "open extension tab. Use between login steps "
                           "to handle the wallet's loading screens.",
            "params": [
                {"name": "selector", "type": "text", "required": True,
                 "label": "CSS selector",
                 "placeholder": "input[type='password']"},
                {"name": "timeout", "type": "number", "default": 15,
                 "label": "Timeout (s)"},
                {"name": "save_as", "type": "text", "default": "",
                 "label": "Save text as",
                 "hint": "Optional. If set, save the element's "
                         "textContent into var.<name> when found."},
            ],
        },
        {
            "type":        "extension_click",
            "label":       "Click in extension",
            "category":    "extensions",
            "scope":       "any",
            "description": "Click an element inside the open extension "
                           "tab. Auto-waits for the selector and "
                           "scrolls it into view first.",
            "params": [
                {"name": "selector", "type": "text", "required": True,
                 "label": "CSS selector",
                 "placeholder": "[data-testid='unlock-button']"},
                {"name": "timeout", "type": "number", "default": 10,
                 "label": "Wait timeout (s)"},
                {"name": "abort_on_error", "type": "bool", "default": False,
                 "label": "Abort run on error"},
            ],
        },
        {
            "type":        "extension_fill",
            "label":       "Fill input in extension",
            "category":    "extensions",
            "scope":       "any",
            "description": "Type into a text input inside the open "
                           "extension. Use {vault.<id>.password} or "
                           "{vault.<id>.seed} placeholders for "
                           "sensitive values — they're resolved from "
                           "the credential vault at run-time.",
            "params": [
                {"name": "selector", "type": "text", "required": True,
                 "label": "CSS selector",
                 "placeholder": "input#password"},
                {"name": "value", "type": "text", "required": True,
                 "label": "Value",
                 "placeholder": "{vault.metamask_main.password}",
                 "hint": "Plain text or {vault.x.y} reference. The "
                         "vault is decrypted before launch and pre-"
                         "loaded into the runner's environment."},
                {"name": "clear_first", "type": "bool", "default": True,
                 "label": "Clear field before typing"},
                {"name": "typewriter", "type": "bool", "default": True,
                 "label": "Type one character at a time",
                 "hint": "Slower but plays nicer with React-style "
                         "input listeners that some wallets use."},
                {"name": "timeout", "type": "number", "default": 10,
                 "label": "Wait timeout (s)"},
            ],
        },
        {
            "type":        "extension_eval",
            "label":       "Eval JS in extension",
            "category":    "extensions",
            "scope":       "any",
            "description": "Run arbitrary JavaScript in the open "
                           "extension tab. Asserts the current URL "
                           "is chrome-extension:// so it fails loud "
                           "if the popup isn't open. For reading "
                           "wallet state via the extension's exposed "
                           "API or for advanced scripting.",
            "params": [
                {"name": "code", "type": "textarea", "required": True,
                 "label": "JavaScript",
                 "placeholder": "return window.ethereum?.selectedAddress;",
                 "hint": "Use a 'return' statement to send a value "
                         "back into save_as."},
                {"name": "save_as", "type": "text", "default": "",
                 "label": "Save return value as"},
                {"name": "abort_on_error", "type": "bool", "default": False,
                 "label": "Abort run on error"},
            ],
        },
        {
            "type":        "extension_close",
            "label":       "Close extension tab",
            "category":    "extensions",
            "scope":       "any",
            "description": "Close the extension popup tab and switch "
                           "back to the originating tab. Pairs with "
                           "open_extension_popup. Safe no-op if the "
                           "tab is already gone.",
            "params": [
                {"name": "handle", "type": "text", "default": "ext_tab",
                 "label": "Tab handle variable",
                 "hint": "Defaults to the same name "
                         "open_extension_popup saved."},
            ],
        },
    ]

