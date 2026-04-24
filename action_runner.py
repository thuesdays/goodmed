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

    from action_runner import run_pipeline
    run_pipeline(browser, pipeline, context={"ad": ad})

Each action is a dict like:
    {
      "type": "click_ad",
      "enabled": true,
      "probability": 0.8,    # chance this step runs (0..1)
      # type-specific params below…
    }
"""

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
        if not href:
            return False
        h = href.lower()
        return any(d in h for d in own_domains)

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

    # ── FALLBACK: URL fragment match with own-domain guard ──────
    # Only reached if the stamped anchor vanished (page re-rendered,
    # Google dynamically swapped the ad block, etc). Still requires
    # owndomain verification on whatever we find.
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
    try:
        ac = ActionChains(driver)
        ac.key_down(Keys.CONTROL).click(anchor).key_up(Keys.CONTROL).perform()
    except Exception as e:
        log.warning(f"    click_ad: ctrl-click failed ({e}), falling back to plain click")
        try:
            anchor.click()
        except Exception:
            log.warning("    click_ad: click failed entirely")
            return

    _random_sleep(1.0, 2.5)
    # Switch to the new tab if one opened
    tabs = [h for h in driver.window_handles if h != original]
    if tabs:
        driver.switch_to.window(tabs[-1])
        landed_url = driver.current_url or ""
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

    # Dwell
    dwell_lo = float(action.get("dwell_min", 6))
    dwell_hi = float(action.get("dwell_max", 18))
    _random_sleep(dwell_lo, dwell_hi)

    # Optional post-click scroll
    if action.get("scroll_after_click", True):
        try:
            _human_scroll(driver)
        except Exception:
            pass
        _random_sleep(1, 3)

    # Close tab and return to SERP
    if action.get("close_after", True) and tabs:
        try:
            driver.close()
        finally:
            driver.switch_to.window(original)


def _act_click_selector(driver, action: dict, ctx: dict):
    """Click any element by CSS selector with human mouse movement."""
    sel = action.get("selector")
    if not sel:
        log.warning("    click_selector: no selector given")
        return

    try:
        el = driver.find_element(By.CSS_SELECTOR, sel)
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
    try:
        el = driver.find_element(By.CSS_SELECTOR, sel)
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
    timeout = float(action.get("timeout_sec", 10))
    if not sel: return
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, sel))
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
    Alias of `visit` but with a clearer name for non-ads scripts."""
    url = _subst(action.get("url", ""), ctx)
    if not url:
        log.warning("    open_url: no url given")
        return
    wait_sec = float(action.get("wait_after", 1.0))
    log.info(f"    → open_url: {url}")
    driver.get(url)
    _random_sleep(wait_sec, wait_sec + 0.8)


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
    Useful after OAuth redirects, form submits, SPA route changes."""
    target = _subst(action.get("contains") or action.get("regex") or "", ctx)
    if not target:
        log.warning("    wait_for_url: no pattern given")
        return
    timeout = float(action.get("timeout", 15.0))
    use_regex = bool(action.get("regex"))

    pattern = re.compile(target) if use_regex else None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cur = driver.current_url or ""
        except Exception:
            cur = ""
        if use_regex and pattern.search(cur):
            log.info(f"    → url matched: {cur}")
            return
        if not use_regex and target in cur:
            log.info(f"    → url matched: {cur}")
            return
        time.sleep(0.4)
    log.warning(f"    wait_for_url timed out after {timeout}s (wanted {target!r})")


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
    "wait_for_url":       _act_wait_for_url,
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
        from db import get_db
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
            log.debug(f"action_event_add failed: {e}")

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
            "label": "Click ad (real click on SERP)",
            "description": "Finds the ad anchor, moves mouse along a curve, "
                          "Ctrl+Clicks it to open in new tab. The most "
                          "realistic ad-click signal.",
            "params": [
                {"name": "dwell_min", "type": "number", "default": 6,  "label": "Min dwell (s)"},
                {"name": "dwell_max", "type": "number", "default": 18, "label": "Max dwell (s)"},
                {"name": "scroll_after_click", "type": "bool", "default": True,
                 "label": "Scroll page after click"},
                {"name": "close_after", "type": "bool", "default": True,
                 "label": "Close tab afterward"},
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
            "label": "Run one search query",
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
    """Top-level catalog — wraps _action_catalog_raw() and ensures every
    entry has a `scope` ('per_ad' | 'loop') set, so the dashboard's
    script builder can group them properly. Called by the dashboard
    GET /api/actions/catalog endpoint.
    """
    catalog = _action_catalog_raw()
    for entry in catalog:
        if "scope" not in entry:
            entry["scope"] = "per_ad"
    return catalog
