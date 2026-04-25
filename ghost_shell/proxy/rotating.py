"""
rotating_proxy.py — Universal rotating-proxy health tracker and rotation caller.

For providers like asocks, bright data, smart proxy — a single endpoint
with a changing exit IP. We track per-IP health in the DB (ip_history
table) and:

  - Detect when an IP is "burned" (too many captchas, too recent use)
  - Trigger rotation via the provider's HTTP API (if configured)
  - Enrich IP metadata (country, ASN, org) for smart filtering
  - Surface stats to the dashboard (/api/ips endpoint)

Supported rotation providers:

    "asocks"      — GET to the configured rotation URL
                    (you paste full URL from asocks dashboard, e.g.
                     https://api.asocks.com/v2/proxy/refresh/<port_id>)

    "brightdata"  — GET with Authorization: Bearer <api_key>

    "generic"     — GET/POST to any URL you give us; if api_key is set,
                    sent as "X-API-Key" header

    "none"        — no forced rotation, we just wait for provider to
                    rotate on its own (many providers auto-rotate every
                    N requests or every M minutes)

Usage:
    from ghost_shell.proxy.rotating import RotatingProxyTracker

    tracker = RotatingProxyTracker(
        proxy_url        = "user:pass@host:port",
        rotation_provider= "asocks",
        rotation_api_url = "https://api.asocks.com/v2/proxy/refresh/12345",
        rotation_api_key = None,   # asocks puts auth in the URL itself
    )

    # Discover current IP
    ip = tracker.get_current_ip(driver)
    tracker.enrich_ip(ip)  # one-time geo lookup

    # Before a search session — if the IP is burned, rotate
    if tracker.is_ip_burned(ip):
        tracker.force_rotate()
        ip = tracker.wait_for_rotation(driver, old_ip=ip, timeout=60)

    # After each search — log outcome
    tracker.report(ip, success=True,  captcha=False)
    tracker.report(ip, success=False, captcha=True)
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import time
import logging
import requests
from datetime import datetime, timedelta
from typing import Optional

from ghost_shell.db.database import get_db


# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────

BURN_AFTER_CAPTCHAS = 3        # 3 captchas in a row → IP is burned
COOLDOWN_HOURS      = 12       # Burned IPs come back online after N hours
IP_CHECK_URL        = "https://api.ipify.org?format=json"
IP_INFO_URL         = "https://ipapi.co/json/"
ROTATION_TIMEOUT    = 15       # HTTP timeout for rotation API calls

SUPPORTED_PROVIDERS = {"asocks", "brightdata", "generic", "none"}


# ──────────────────────────────────────────────────────────────
# Class
# ──────────────────────────────────────────────────────────────

class RotatingProxyTracker:
    """
    Per-IP health tracker for a rotating-proxy endpoint.

    State lives entirely in the SQLite `ip_history` table managed by db.py.
    No JSON files, no local caching — the dashboard always sees live state.
    """

    def __init__(
        self,
        proxy_url:          str,
        rotation_provider:  str = "none",
        rotation_api_url:   Optional[str] = None,
        rotation_api_key:   Optional[str] = None,
        rotation_method:    str = "GET",
    ):
        if rotation_provider not in SUPPORTED_PROVIDERS:
            logging.warning(
                f"[RotatingProxy] Unknown provider '{rotation_provider}' — "
                f"falling back to 'none'. Supported: {SUPPORTED_PROVIDERS}"
            )
            rotation_provider = "none"

        self.proxy_url         = proxy_url
        self.rotation_provider = rotation_provider
        self.rotation_api_url  = rotation_api_url
        self.rotation_api_key  = rotation_api_key
        self.rotation_method   = rotation_method.upper()

        self.db = get_db()

    # ──────────────────────────────────────────────────────────
    # IP discovery
    # ──────────────────────────────────────────────────────────

    def get_current_ip(self, driver=None) -> Optional[str]:
        """Return the outgoing IP as observed through the proxy.

        Uses `requests` through the proxy — NEVER routes through the
        browser. This was a deliberate change: navigating Chrome to
        ipify on every run created an unmistakable bot fingerprint
        (real users never visit ipify.org first thing in a session),
        and dumped that domain into Chrome's history/cookies for
        Google to observe. Using requests keeps the IP check entirely
        invisible to the browser — goes directly through our local
        proxy_forwarder to the same exit, just on a separate TCP
        connection.

        The `driver` parameter is kept for backwards compatibility
        with older callers but is now ignored.
        """
        if not self.proxy_url:
            return None
        try:
            proxies = {
                "http":  f"http://{self.proxy_url}",
                "https": f"http://{self.proxy_url}",
            }
            r = requests.get(IP_CHECK_URL, proxies=proxies, timeout=15)
            return r.json().get("ip")
        except Exception as e:
            logging.debug(f"[RotatingProxy] get_current_ip: {e}")
            return None

    def enrich_ip(self, ip: str, driver=None):
        """Look up country / city / ASN / ISP and store them in
        ip_history. Called once per IP — subsequent calls are a no-op.

        Like get_current_ip, we NEVER route through the browser. Using
        driver.get(ipapi) would surface the meta-service in Chrome's
        history and add a unique fingerprint (real users never hit
        ipapi first thing). `driver` kept for backwards compat but
        ignored.
        """
        if not ip:
            return

        existing = self.db.ip_get(ip)
        if existing and existing.get("country"):
            return

        if not self.proxy_url:
            return

        try:
            proxies = {
                "http":  f"http://{self.proxy_url}",
                "https": f"http://{self.proxy_url}",
            }
            r = requests.get(IP_INFO_URL, proxies=proxies, timeout=15)
            data = r.json()

            self.db.ip_update_meta(
                ip       = ip,
                country  = data.get("country_name") or data.get("country"),
                city     = data.get("city"),
                org      = data.get("org"),
                asn      = data.get("asn"),
            )
            logging.info(
                f"[RotatingProxy] IP {ip} enriched: "
                f"{data.get('country_name')} / {data.get('org')}"
            )
        except Exception as e:
            logging.debug(f"[RotatingProxy] enrich {ip}: {e}")

    # ──────────────────────────────────────────────────────────
    # Health checks (all from DB)
    # ──────────────────────────────────────────────────────────

    def is_ip_burned(self, ip: str) -> bool:
        """
        True if this IP is in cooldown (had too many captchas recently).
        After COOLDOWN_HOURS the IP gets un-burned automatically (next call
        returns False and the row is reset).
        """
        if not ip:
            return False

        row = self.db.ip_get(ip)
        if not row:
            return False

        burned_at = row.get("burned_at")
        if not burned_at:
            return False

        try:
            burned_time = datetime.fromisoformat(burned_at)
            if datetime.now() - burned_time < timedelta(hours=COOLDOWN_HOURS):
                return True
            # Cooldown passed — un-burn
            self.db.ip_unburn(ip)
            logging.info(f"[RotatingProxy] IP {ip} cooldown over, back in rotation")
            return False
        except Exception:
            return False

    def is_ip_fresh(self, ip: str) -> bool:
        """True if we've never seen this IP before."""
        return self.db.ip_get(ip) is None

    # ──────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────

    def report(self, ip: str, success: bool = True, captcha: bool = False):
        """
        Record the outcome of one session's use of this IP.

        On BURN_AFTER_CAPTCHAS consecutive captchas the IP is marked burned,
        and future is_ip_burned() checks return True until COOLDOWN_HOURS
        have passed.
        """
        if not ip:
            return

        self.db.ip_report(
            ip            = ip,
            success       = success,
            captcha       = captcha,
            burn_after    = BURN_AFTER_CAPTCHAS,
        )

    # ──────────────────────────────────────────────────────────
    # Rotation
    # ──────────────────────────────────────────────────────────

    def force_rotate(self) -> bool:
        """
        Call the provider's rotation API. Returns True on HTTP 2xx.

        If no API URL is configured, logs a note and returns False so
        the caller knows we can't force-rotate and should fall back to
        wait_for_rotation() or reconnect the proxy.
        """
        if self.rotation_provider == "none" or not self.rotation_api_url:
            logging.warning(
                "[RotatingProxy] ⚠ Rotation API is NOT configured "
                f"(provider={self.rotation_provider!r}, url={self.rotation_api_url!r}). "
                "Cannot force-rotate — IP will only change when the proxy "
                "provider rotates it on its own schedule. "
                "Fix: Proxy page → Rotation section → paste your provider's "
                "rotation URL and set provider to 'asocks'/'brightdata'/'generic'."
            )
            return False

        headers = {}
        if self.rotation_api_key:
            if self.rotation_provider == "brightdata":
                headers["Authorization"] = f"Bearer {self.rotation_api_key}"
            elif self.rotation_provider == "asocks":
                # asocks uses ?apiKey=... in the URL — no header needed.
                pass
            else:
                headers["X-API-Key"] = self.rotation_api_key

        try:
            if self.rotation_method == "POST":
                r = requests.post(
                    self.rotation_api_url, headers=headers,
                    timeout=ROTATION_TIMEOUT,
                )
            else:
                r = requests.get(
                    self.rotation_api_url, headers=headers,
                    timeout=ROTATION_TIMEOUT,
                )

            if 200 <= r.status_code < 300:
                self.db.ip_log_rotation(self.rotation_provider)
                logging.info(
                    f"[RotatingProxy] ✓ rotation triggered via "
                    f"{self.rotation_provider} ({r.status_code})"
                )
                return True

            logging.warning(
                f"[RotatingProxy] rotation API returned HTTP {r.status_code}: "
                f"{r.text[:200]}"
            )
        except Exception as e:
            logging.error(f"[RotatingProxy] rotation failed: {e}")
        return False

    def wait_for_rotation(
        self, driver, old_ip: str, timeout: int = 60
    ) -> Optional[str]:
        """Poll the exit IP until it changes or the timeout hits.

        Used after force_rotate() (providers may be async) OR when no
        rotation API is available and we're verifying auto-rotate on
        TCP reconnect.

        `driver` is unused — we check IP via requests to keep ipify
        invisible to Chrome (see get_current_ip for rationale). Kept
        in the signature for backwards compatibility.

        Polling cadence: start fast (500ms) since asocks typically
        rotates in 1-3s, then back off to avoid hammering ipify if
        the provider is genuinely slow. Total hits for a fast rotate:
        2-4 requests instead of the previous 5-8.
        """
        logging.info(f"[RotatingProxy] waiting for IP to change from {old_ip}...")
        started = time.time()
        # Exponential-ish backoff: 0.5s, 1s, 2s, 3s, 5s, 5s, ...
        intervals = [0.5, 1.0, 2.0, 3.0, 5.0]
        i = 0

        while time.time() - started < timeout:
            wait = intervals[min(i, len(intervals) - 1)]
            time.sleep(wait)
            current = self.get_current_ip()
            if current and current != old_ip:
                logging.info(f"[RotatingProxy] ✓ IP changed: {old_ip} → {current} "
                             f"(in {time.time() - started:.1f}s)")
                return current
            i += 1

        logging.warning(f"[RotatingProxy] IP did not change within {timeout}s")
        return None

    # ──────────────────────────────────────────────────────────
    # Stats (for dashboard)
    # ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Aggregate stats pulled from the ip_history table."""
        return self.db.ip_summary()


# ──────────────────────────────────────────────────────────────
# CLI — quick ops without running the whole dashboard
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python rotating_proxy.py stats     — print current IP stats")
        print("  python rotating_proxy.py rotate    — force-rotate using config")
        print("  python rotating_proxy.py whoami    — show current outgoing IP")
        sys.exit(0)

    # Load settings from DB so we match dashboard
    db = get_db()
    proxy_url = db.config_get("proxy.url") or ""
    tracker = RotatingProxyTracker(
        proxy_url         = proxy_url,
        rotation_provider = db.config_get("proxy.rotation_provider") or "none",
        rotation_api_url  = db.config_get("proxy.rotation_api_url"),
        rotation_api_key  = db.config_get("proxy.rotation_api_key"),
        rotation_method   = db.config_get("proxy.rotation_method") or "GET",
    )

    cmd = sys.argv[1].lower()
    if cmd == "stats":
        s = tracker.get_stats()
        print("═" * 60)
        print(" ROTATING PROXY STATS")
        print("═" * 60)
        for k, v in s.items():
            print(f"  {k:<24} {v}")
        print("═" * 60)

    elif cmd == "rotate":
        ok = tracker.force_rotate()
        print("✓ rotation triggered" if ok else "✗ rotation failed / not configured")

    elif cmd == "whoami":
        ip = tracker.get_current_ip()
        print(f"Current IP: {ip or 'unknown'}")

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
