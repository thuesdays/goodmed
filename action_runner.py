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
    """Actually click the ad element (Ctrl+Click → new tab)."""
    ad = ctx.get("ad") or {}

    # Find the ad's anchor element by URL or by known SERP selectors
    url    = ad.get("google_click_url") or ad.get("clean_url") or ""
    anchor = None

    # Strategy 1: find an <a> whose href matches the ad we parsed
    if url:
        try:
            anchor = driver.find_element(
                By.CSS_SELECTOR,
                f"a[href*='{url[:80]}']"
            )
        except Exception:
            pass

    # Strategy 2: find any ad anchor (first Sponsored card)
    if anchor is None:
        for sel in ("[data-text-ad] a.sVXRqc",
                    "[data-text-ad] a[href*='aclk']",
                    "div[data-rw] a[href*='aclk']",
                    ".uEierd a",
                    "a[data-rw][href*='aclk']"):
            try:
                anchor = driver.find_element(By.CSS_SELECTOR, sel)
                break
            except Exception:
                continue

    if anchor is None:
        log.warning("    click_ad: couldn't locate ad anchor")
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
        log.info(f"    → clicked ad, now on: {driver.current_url[:80]}")

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
    """Navigate directly to a URL."""
    ad  = ctx.get("ad") or {}
    url = action.get("url") or ad.get("google_click_url") or ad.get("clean_url")
    if not url:
        return
    new_tab = bool(action.get("new_tab", True))
    log.info(f"    → visit {url[:80]}")

    if new_tab:
        original = driver.current_window_handle
        driver.execute_script(f"window.open('{url}', '_blank');")
        _random_sleep(1.0, 2.0)
        new_handles = [h for h in driver.window_handles if h != original]
        if new_handles:
            driver.switch_to.window(new_handles[-1])

        dwell_lo = float(action.get("dwell_min", 5))
        dwell_hi = float(action.get("dwell_max", 15))
        _random_sleep(dwell_lo, dwell_hi)

        if action.get("close_after", True):
            try: driver.close()
            finally: driver.switch_to.window(original)
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

ACTION_HANDLERS: dict[str, Callable] = {
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
}


def run_pipeline(browser, pipeline: list, context: dict = None):
    """
    Execute a full pipeline of actions. Each action dict has at minimum
    `type`. Common params:
      - enabled            (bool, default true)
      - probability        (0.0..1.0, default 1.0)
      - skip_on_my_domain  (bool) — skip this step if ad's domain is
                            one of the user's own domains (search.my_domains)
      - skip_on_target     (bool) — skip this step if ad's domain is a
                            target domain (from context)
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

    for i, action in enumerate(pipeline, 1):
        if not action.get("enabled", True):
            continue
        act_type = action.get("type")
        handler = ACTION_HANDLERS.get(act_type)
        if handler is None:
            log.warning(f"  unknown action type: {act_type}")
            continue

        # Per-step domain filters — "do nothing if ad is my own domain"
        if is_mine and action.get("skip_on_my_domain"):
            log.info(f"  [{i}] skip {act_type} (ad is on my_domain: {ad_domain})")
            continue
        if is_target and action.get("skip_on_target"):
            log.info(f"  [{i}] skip {act_type} (ad is on target domain)")
            continue

        probability = float(action.get("probability", 1.0))
        if random.random() > probability:
            log.info(f"  [{i}] skip {act_type} (probability {probability:.2f})")
            continue

        try:
            log.info(f"  [{i}] running {act_type}")
            handler(driver, action, ctx)
        except Exception as e:
            log.warning(f"  [{i}] {act_type} errored: {type(e).__name__}: {e}")
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
                    "Domains. Inverse of the on-target-domain pipeline.",
        },
    ]


def action_catalog() -> list[dict]:
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
            "params": [
                {"name": "selector",    "type": "text", "required": True},
                {"name": "timeout_sec", "type": "number", "default": 10},
            ],
        },
    ]
