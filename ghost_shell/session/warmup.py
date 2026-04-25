"""
warmup.py — Per-profile session warmup robot.

Launches a real Chromium instance with the profile's user-data-dir,
cycles through a preset list of realistic destinations, accepts
cookie-consent banners where present, dwells with mild scrolling,
and closes cleanly. The result: cookies + localStorage + history
that make the profile look like a human has been using this browser.

Runs in the SAME sandboxed Chromium as normal monitor runs — never
touches the user's own Chrome. The profile's user-data-dir is
isolated under profiles/<profile_name>/ next to the project root.

Called from:
  - Dashboard "Run warmup" button        (trigger="manual")
  - Scheduler daily job                  (trigger="scheduled")
  - Future: auto-before-first-run hook   (trigger="auto_before_run")

Output: a warmup_runs row + per-site log in its sites_log field,
plus (indirectly) populated cookies in the profile's user-data-dir.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
import random
import time
from typing import Callable, Optional

from ghost_shell.db import get_db
from ghost_shell.session.site_presets import pick_sites, roll_dwell, get_preset


# Cookie-consent banner patterns we know how to click away.
# The strategy: we DON'T click "Reject all" — we want the cookies.
# We click "Accept" / "I agree" / "Accept all" to populate consent.
# CSS-selector variants first, then a broad xpath by text.
CONSENT_SELECTORS_CSS = [
    # Google / YouTube consent dialog
    'button[aria-label="Accept all"]',
    'button[aria-label="Agree to all"]',
    'button[aria-label="Принять все"]',
    'form[action*="consent.google"] button[type="submit"]',

    # Common CMP (OneTrust, Quantcast, Cookiebot, TrustArc)
    '#onetrust-accept-btn-handler',
    '#cookieAcceptAllButton',
    '.qc-cmp2-summary-buttons button[mode="primary"]',
    'button[data-testid="uc-accept-all-button"]',
    'button[id*="accept"][id*="ookie" i]',
    'button.fc-cta-consent',
    'button.css-47sehv',                         # Didomi
    'button[data-action="accept-all"]',

    # News sites
    'button.button--accept',
    '.cookie-banner button.accept',

    # Wikipedia has no banner. Reddit uses its own modal:
    'button[class*="cookie-consent"][class*="accept"]',
]

# NOTE -- INTENTIONAL CYRILLIC: the text matchers below include
# Russian and Ukrainian phrases ("Принять все", "Прийняти", etc.).
# These are the visible labels on cookie-consent buttons of news
# sites in RU / UA locales. Translating them to English would
# break consent dismissal on those sites — leave them as-is.
# Fall-back XPath by visible text (case-insensitive via translate()).
CONSENT_XPATH_TEXTS = [
    "Accept all", "Accept All", "Accept", "I agree", "Agree",
    "Allow all", "Got it", "OK, got it",
    "Принять все", "Принять", "Согласен",
    "Прийняти", "Погоджуюсь",
]


class WarmupEngine:
    """One warmup session. Not thread-safe — instantiate per-run."""

    def __init__(self, profile_name: str, preset_id: str = "general",
                 site_count: int = 7, trigger: str = "manual",
                 *, proxy_url: Optional[str] = None):
        self.profile_name = profile_name
        self.preset_id    = preset_id
        self.site_count   = site_count
        self.trigger      = trigger
        self.proxy_url    = proxy_url
        self.db           = get_db()
        self._warmup_id: Optional[int] = None
        self._started_at: Optional[float] = None
        self._sites_log: list[dict] = []

    # ──────────────────────────────────────────────────────────
    # Main entry
    # ──────────────────────────────────────────────────────────
    def run(self, progress_cb: Callable[[dict], None] | None = None) -> dict:
        """Execute the warmup. Returns the final sites_log + status.

        progress_cb(event) is called on each state transition so SSE
        streams or polling UIs can render live progress. Event shape:
            {"kind": "start",    "total": N}
            {"kind": "visiting", "idx": i, "url": "..."}
            {"kind": "visited",  "idx": i, "url": "...", "ok": bool, "duration_ms": int}
            {"kind": "finish",   "status": "ok|partial|failed"}
        """
        # Auto-resolve mobile-vs-desktop preset. Two flavours of "auto":
        #   preset_id == "auto"   → pick based on profile's active fp
        #   preset_id set AND the profile is mobile but preset has no
        #   mobile tilt → swap in "mobile" as a courtesy (we don't want
        #   to warmup a mobile profile by visiting desktop-only sites).
        self._is_mobile = self._profile_is_mobile()
        if self.preset_id == "auto":
            self.preset_id = "mobile" if self._is_mobile else "general"
            logging.info(f"[warmup] auto-selected preset: {self.preset_id}")
        elif self._is_mobile and self.preset_id not in ("mobile",):
            logging.info(
                f"[warmup] profile is mobile but preset={self.preset_id!r} — "
                "keeping as chosen; consider preset=mobile for better realism"
            )

        if get_preset(self.preset_id) is None:
            raise ValueError(f"unknown preset: {self.preset_id}")

        sites = pick_sites(self.preset_id, self.site_count,
                           seed=f"{self.profile_name}:{int(time.time())}")
        planned = len(sites)

        self._warmup_id = self.db.warmup_start(
            self.profile_name, self.preset_id, planned, self.trigger,
        )
        self._started_at = time.time()
        if progress_cb: progress_cb({"kind": "start", "total": planned})

        # Lazy import — avoids pulling selenium / chromium into modules
        # that don't need the full browser (e.g. the /api endpoint that
        # just reads warmup_last() for the UI rollup).
        try:
            from ghost_shell.browser.runtime import GhostShellBrowser
        except ImportError as e:
            return self._finish("failed",
                                notes=f"browser import error: {e}",
                                progress_cb=progress_cb)

        status = "failed"
        try:
            with GhostShellBrowser(
                profile_name       = self.profile_name,
                proxy_str          = self.proxy_url,
                auto_session       = False,   # don't mix with existing sessions
                is_rotating_proxy  = False,
                enrich_on_create   = False,   # caller decides, usually pre-enriched
            ) as browser:
                driver = browser.driver
                succeeded = 0
                for i, site in enumerate(sites, 1):
                    if progress_cb:
                        progress_cb({"kind": "visiting", "idx": i,
                                     "url": site["url"]})
                    result = self._visit(driver, site, index=i, total=planned)
                    self._sites_log.append(result)
                    if result["ok"]:
                        succeeded += 1
                    if progress_cb:
                        progress_cb({"kind": "visited", "idx": i,
                                     "url": site["url"],
                                     "ok": result["ok"],
                                     "duration_ms": result["duration_ms"]})

                # Status calculus — all ok → "ok", some ok → "partial",
                # zero ok → "failed" (engine ran but nothing worked).
                if succeeded == planned:   status = "ok"
                elif succeeded > 0:        status = "partial"
                else:                      status = "failed"
        except Exception as e:
            logging.error(f"[warmup] engine crash: {e}", exc_info=True)
            return self._finish("failed", notes=f"engine crash: {e}",
                                progress_cb=progress_cb)

        return self._finish(status, progress_cb=progress_cb)

    # ──────────────────────────────────────────────────────────
    # Per-site logic
    # ──────────────────────────────────────────────────────────
    def _visit(self, driver, site: dict, *, index: int, total: int) -> dict:
        """Visit one site: navigate, consent, dwell, optional scroll."""
        t0 = time.time()
        url = site["url"]
        topic = site.get("topic", "")
        entry = {"url": url, "topic": topic,
                 "ok": False, "duration_ms": 0,
                 "cookies_before": 0, "cookies_after": 0,
                 "consent_clicked": False, "error": None}

        try:
            # Count cookies BEFORE so we can report "cookies_added"
            try:    entry["cookies_before"] = len(driver.get_cookies())
            except Exception: pass

            driver.set_page_load_timeout(25)
            driver.get(url)
            time.sleep(random.uniform(1.0, 2.0))   # initial render settle

            # Try consent banners — non-fatal if none present
            if self._try_consent(driver):
                entry["consent_clicked"] = True
                time.sleep(random.uniform(0.4, 0.9))

            # Dwell with optional scroll
            dwell = roll_dwell(site.get("dwell_sec", (5, 8)))
            if site.get("scroll", False):
                self._gentle_scroll(driver, dwell)
            else:
                time.sleep(dwell)

            try:    entry["cookies_after"] = len(driver.get_cookies())
            except Exception: pass

            entry["ok"] = True
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            entry["duration_ms"] = int((time.time() - t0) * 1000)
        return entry

    def _try_consent(self, driver) -> bool:
        """Best-effort cookie-consent dismissal. Returns True if clicked."""
        # CSS selectors first — faster + more precise.
        for sel in CONSENT_SELECTORS_CSS:
            try:
                els = driver.find_elements("css selector", sel)
                for el in els:
                    if el.is_displayed() and el.is_enabled():
                        el.click()
                        logging.debug(f"[warmup] consent clicked via {sel}")
                        return True
            except Exception:
                continue
        # XPath-by-text fallback — slower, catches anything bespoke.
        for text in CONSENT_XPATH_TEXTS:
            try:
                xpath = (
                    f"//button[normalize-space(.)='{text}' or "
                    f"contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    f"'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]"
                )
                els = driver.find_elements("xpath", xpath)
                for el in els:
                    if el.is_displayed() and el.is_enabled():
                        el.click()
                        logging.debug(f"[warmup] consent clicked via text={text!r}")
                        return True
            except Exception:
                continue
        return False

    def _profile_is_mobile(self) -> bool:
        """Peek at the profile's current fingerprint to decide if we
        should run in mobile mode. No fingerprint or no is_mobile flag
        → treat as desktop."""
        try:
            fp = self.db.fingerprint_current(self.profile_name)
            return bool(fp and (fp.get("payload") or {}).get("is_mobile"))
        except Exception:
            return False

    def _gentle_scroll(self, driver, total_seconds: float):
        """Burn `total_seconds` with scroll activity. Mobile profiles
        use CDP touch swipe events (real finger pattern), desktop profiles
        use window.scrollBy. Both end with a small return-scroll to mimic
        a reader who over-scrolled and came back."""
        if getattr(self, "_is_mobile", False):
            self._swipe_scroll(driver, total_seconds)
        else:
            self._wheel_scroll(driver, total_seconds)

    def _wheel_scroll(self, driver, total_seconds: float):
        end_at = time.time() + total_seconds
        step = 0
        while time.time() < end_at:
            try:
                delta = random.randint(300, 700)
                driver.execute_script(f"window.scrollBy({{top: {delta}, left: 0, behavior: 'smooth'}});")
            except Exception:
                pass
            time.sleep(random.uniform(1.5, 3.0))
            step += 1
            if step > 8:
                break
        try:
            driver.execute_script("window.scrollBy({top: -200, behavior: 'smooth'});")
        except Exception:
            pass

    def _swipe_scroll(self, driver, total_seconds: float):
        """Mobile-style scroll via CDP touch. Emits touchStart →
        touchMove × N → touchEnd in an upward swipe, which the mobile
        Chromium path handles differently than wheel events (and which
        touch-aware analytics scripts can see)."""
        try:
            size = driver.execute_script(
                "return {w: window.innerWidth, h: window.innerHeight};"
            )
            W, H = size["w"], size["h"]
        except Exception:
            W, H = 400, 800

        end_at = time.time() + total_seconds
        step = 0
        while time.time() < end_at:
            try:
                # 70-90% of viewport height as swipe distance
                distance = H * random.uniform(0.5, 0.8)
                fy = H * random.uniform(0.7, 0.85)
                ty = fy - distance
                fx = tx = W * random.uniform(0.4, 0.6)
                steps_n = random.randint(10, 18)
                driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
                    "type": "touchStart",
                    "touchPoints": [{"x": fx, "y": fy}],
                })
                for i in range(1, steps_n):
                    t = i / steps_n
                    eased = 1 - (1 - t) * (1 - t)
                    x = fx + (tx - fx) * eased
                    y = fy + (ty - fy) * eased
                    driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
                        "type": "touchMove",
                        "touchPoints": [{"x": x, "y": y}],
                    })
                    time.sleep(random.uniform(0.015, 0.03))
                driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
                    "type": "touchEnd",
                    "touchPoints": [],
                })
            except Exception as e:
                logging.debug(f"[warmup] touch swipe failed: {e}")
            time.sleep(random.uniform(1.5, 3.0))
            step += 1
            if step > 6:
                break
        # Small pull-down swipe at the end — reader scrolling back up
        try:
            driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": [{"x": W/2, "y": H*0.4}],
            })
            driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [{"x": W/2, "y": H*0.55}],
            })
            driver.execute_cdp_cmd("Input.dispatchTouchEvent", {
                "type": "touchEnd",
                "touchPoints": [],
            })
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # Finish bookkeeping
    # ──────────────────────────────────────────────────────────
    def _finish(self, status: str, *, notes: str = None,
                progress_cb=None) -> dict:
        duration = (time.time() - (self._started_at or time.time()))
        visited = len(self._sites_log)
        succeeded = sum(1 for s in self._sites_log if s["ok"])
        try:
            self.db.warmup_finish(
                self._warmup_id,
                status=status,
                sites_visited=visited,
                sites_succeeded=succeeded,
                duration_sec=duration,
                notes=notes,
                sites_log=self._sites_log,
            )
        except Exception as e:
            logging.error(f"[warmup] DB finish failed: {e}")

        if progress_cb:
            progress_cb({"kind": "finish", "status": status})

        return {
            "warmup_id":       self._warmup_id,
            "status":          status,
            "sites_visited":   visited,
            "sites_succeeded": succeeded,
            "duration_sec":    duration,
            "notes":           notes,
            "sites_log":       self._sites_log,
        }


def run_warmup(profile_name: str, preset: str = "general", sites: int = 7,
               trigger: str = "manual",
               progress_cb=None) -> dict:
    """Module-level convenience wrapper — most callers want this."""
    return WarmupEngine(profile_name, preset, sites, trigger).run(progress_cb)
