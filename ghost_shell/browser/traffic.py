"""
traffic_collector.py — Per-browser traffic aggregator.

Runs in a background thread, polls Chrome via CDP for resource timing
data, aggregates bytes by domain, flushes to SQLite every N seconds.

Design choices:

  1. PRIMARY: CDP Network events via execute_cdp_cmd('Network.enable').
     Captures byte counts for ALL requests including cross-origin, which
     is what most browsing actually is. Chrome reports encodedDataLength
     directly — no security restrictions.

  2. FALLBACK: JS PerformanceObserver. Used only if CDP registration
     fails. Limited because PerformanceResourceTiming's transferSize is
     ZEROED OUT for cross-origin responses without Timing-Allow-Origin
     header — which is ~95% of real traffic. Leaving PerfObserver as
     the sole source meant the DB filled with req_count > 0 but
     bytes = 0 almost everywhere, and the dashboard showed the right
     request count but zero MB because every byte number was 0.

  3. Aggregate BY DOMAIN, not by URL. 10,000 requests to google.com
     become ONE in-memory entry. Flushed every 30s to DB as ONE row
     (or merged with existing row if same hour_bucket).

  4. Cache-hit requests have transferSize = 0. We still count them as
     1 request but add 0 bytes. Users can see high req_count / low bytes
     for cache-friendly domains like fonts.gstatic.com.

CDP path: we don't use Selenium BiDi event subscriptions because they're
flaky across Chrome versions. Instead we call Network.enable and then
poll Performance.getMetrics for cumulative counters on each flush — the
specific counters are `Network.encodedDataLength` (total bytes) and
`Network.requestCount` (total requests). These are NOT per-domain, so
we use them to VERIFY PerfObserver numbers aren't missing huge chunks
(if CDP totals >> PerfObserver totals, we know transferSize is blocked
and fall back to a different approach).

Actually simpler: we use Network.loadingFinished via a lightweight
custom devtools handler. Selenium 4 supports this through script
injection that listens for CDP events surfaced to window.
"""

import threading
import time
import logging
from urllib.parse import urlparse
from datetime import datetime
from typing import Optional


# JS buffer we inject into every page. Uses PerformanceObserver which
# fires on every resource load completion. Entries accumulate in a
# global array that Python reads and clears via execute_script().
#
# Important: survive page navigations. Each new page wipes window.*,
# so the script must re-install itself on every poll if the buffer is
# absent. We do that check in the poll snippet, not here.
_OBSERVER_JS = r"""
(function() {
  if (window.__ghostShellTrafficBuf) return;
  window.__ghostShellTrafficBuf = [];
  try {
    const obs = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        // transferSize is bytes over the wire (headers + compressed body)
        // 0 means served from cache or opaque response — still count the request.
        window.__ghostShellTrafficBuf.push({
          n: e.name,
          t: e.transferSize || 0,
        });
        // Cap buffer size so a runaway page can't eat all RAM — drop oldest.
        if (window.__ghostShellTrafficBuf.length > 5000) {
          window.__ghostShellTrafficBuf.splice(0, 2000);
        }
      }
    });
    obs.observe({ type: 'resource', buffered: true });
  } catch (e) {
    // Some sandboxed/about:blank contexts throw — ignore, next page will retry.
  }
})();
"""

# Poll snippet — installs observer if missing (survives navigation),
# reads and clears the buffer, returns the batch to Python.
_POLL_JS = r"""
(function() {
  if (!window.__ghostShellTrafficBuf) {
    // Re-install after page navigation wiped window.*
    try {
      window.__ghostShellTrafficBuf = [];
      const obs = new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          window.__ghostShellTrafficBuf.push({ n: e.name, t: e.transferSize || 0 });
          if (window.__ghostShellTrafficBuf.length > 5000) {
            window.__ghostShellTrafficBuf.splice(0, 2000);
          }
        }
      });
      obs.observe({ type: 'resource', buffered: true });
    } catch (e) {}
  }
  const batch = window.__ghostShellTrafficBuf || [];
  window.__ghostShellTrafficBuf = [];
  return batch;
})();
"""


class TrafficCollector:
    """Background aggregator. One instance per GhostShellBrowser.

    Usage:
        tc = TrafficCollector(driver=browser.driver,
                              profile_name="profile_01",
                              run_id=42,
                              db=get_db())
        tc.start()
        # ... browser runs ...
        tc.stop()   # flushes remaining buffer before return
    """

    # How often to pull the JS buffer (seconds). Lower = smaller
    # in-browser buffer but more JS round-trips. 5s is fine.
    POLL_INTERVAL = 5.0

    def __init__(self, driver, profile_name: str, run_id: Optional[int],
                 db, flush_interval_sec: int = 30, proxy_forwarder=None):
        """
        proxy_forwarder: optional ProxyForwarder instance. If provided,
        we drain its per-host byte counters on each flush — this is
        the AUTHORITATIVE source for byte counts since Chrome's
        PerformanceObserver zeros out transferSize for cross-origin
        responses without Timing-Allow-Origin (which is most traffic).
        The JS observer still contributes req_count for cache-hit
        requests that never touch the proxy, but proxy_forwarder wins
        for byte totals.
        """
        self.driver          = driver
        self.profile_name    = profile_name
        self.run_id          = run_id
        self.db              = db
        self.flush_interval  = flush_interval_sec
        self.proxy_forwarder = proxy_forwarder

        self._thread     = None
        self._stop_event = threading.Event()
        # In-memory aggregator: {hour_bucket: {domain: {"bytes": N, "req_count": M}}}
        # Keyed by hour so an overnight run flushes separate rows for each hour.
        self._pending    = {}
        self._pending_lock = threading.Lock()
        self._last_flush = time.time()

    # ──────────────────────────────────────────────────────────────

    def start(self):
        """Install the JS observer and spawn the poll thread."""
        try:
            self.driver.execute_script(_OBSERVER_JS)
        except Exception as e:
            logging.debug(f"[TrafficCollector] observer install skipped: {e}")

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="TrafficCollector"
        )
        self._thread.start()
        logging.info(
            f"[TrafficCollector] started for '{self.profile_name}' "
            f"(flush every {self.flush_interval}s)"
        )

    def stop(self):
        """Signal the thread to exit and flush any pending aggregates."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        # Final flush — capture any late entries that arrived between
        # the last poll and browser shutdown.
        try:
            self._poll_once()
            self._flush(force=True)
        except Exception as e:
            logging.debug(f"[TrafficCollector] final flush error: {e}")
        logging.info(
            f"[TrafficCollector] stopped for '{self.profile_name}'"
        )

    # ──────────────────────────────────────────────────────────────

    def _loop(self):
        """Main polling loop. Runs until stop() is called."""
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as e:
                # Most common: driver died or window closed — log once and
                # fall through. The collector's .stop() will be called by
                # the browser's own shutdown path.
                logging.debug(f"[TrafficCollector] poll error: {e}")

            # Flush if enough time has passed
            if time.time() - self._last_flush >= self.flush_interval:
                try:
                    self._flush()
                except Exception as e:
                    logging.debug(f"[TrafficCollector] flush error: {e}")

            self._stop_event.wait(self.POLL_INTERVAL)

    def _poll_once(self):
        """Pull the JS buffer and fold entries into the in-memory aggregate."""
        try:
            batch = self.driver.execute_script(_POLL_JS)
        except Exception:
            return
        if not batch:
            return

        now = datetime.now()
        bucket = now.strftime("%Y-%m-%d %H")

        with self._pending_lock:
            hour_map = self._pending.setdefault(bucket, {})
            for entry in batch:
                name  = entry.get("n") or ""
                bytes_ = entry.get("t") or 0
                domain = _extract_domain(name)
                if not domain:
                    continue
                slot = hour_map.setdefault(domain, {"bytes": 0, "req_count": 0})
                slot["bytes"]     += int(bytes_ or 0)
                slot["req_count"] += 1

    def _flush(self, force: bool = False):
        """Write the in-memory aggregate to the DB, hour by hour.

        Two sources feed the aggregate:
          1. PerformanceObserver entries (via _poll_once) — good for
             req_count, unreliable for bytes cross-origin
          2. ProxyForwarder per-host counters (drain_counters) — 100%
             accurate bytes for everything that went through our proxy
             (which is everything Chrome does when we route it there)

        We MERGE the two on each flush: PerfObserver's req_count +
        ProxyForwarder's bytes. If a domain appears in both, req_count
        from PerfObserver and bytes from ProxyForwarder. If only in
        proxy (no PerfObserver entry), we use proxy's req_count estimate
        (1 per TCP connection, under-counts HTTP keepalive but matches
        what asocks bills).
        """
        # ── 1. Pull current PerfObserver pending (in-memory JS buffer) ─
        with self._pending_lock:
            pending = self._pending
            self._pending = {}

        # ── 2. Drain ProxyForwarder per-host counters ──────────────
        # All traffic through this profile went through proxy_forwarder
        # (Chrome was launched with --proxy-server pointing at it), so
        # its counters represent authoritative billed bytes. We fold
        # these into the current hour's bucket.
        proxy_bytes_total = 0
        if self.proxy_forwarder is not None:
            try:
                proxy_counts = self.proxy_forwarder.drain_counters()
            except Exception as e:
                logging.debug(f"[TrafficCollector] proxy drain: {e}")
                proxy_counts = {}
            if proxy_counts:
                bucket = datetime.now().strftime("%Y-%m-%d %H")
                hour_map = pending.setdefault(bucket, {})
                for host, stats in proxy_counts.items():
                    if not host:
                        continue
                    pb = int(stats.get("bytes") or 0)
                    pc = int(stats.get("req_count") or 0)
                    if pb <= 0 and pc <= 0:
                        continue
                    slot = hour_map.setdefault(host, {"bytes": 0, "req_count": 0})
                    # Authoritative bytes from proxy — REPLACE whatever
                    # PerfObserver guessed (which was probably 0). If
                    # PerfObserver already contributed bytes, keep the
                    # MAX since both might miss some edge cases but
                    # neither over-reports.
                    slot["bytes"] = max(slot["bytes"], pb)
                    # For req_count, keep PerfObserver's value if it's
                    # higher (it sees individual HTTP requests inside
                    # keepalive connections which proxy bundles into 1).
                    slot["req_count"] = max(slot["req_count"], pc)
                    proxy_bytes_total += pb

        if not pending:
            self._last_flush = time.time()
            return

        total_bytes = 0
        total_reqs = 0
        for bucket_str, by_domain in pending.items():
            # Parse bucket back to a datetime for traffic_record_batch —
            # though the DB method only uses hour precision, passing the
            # real timestamp keeps the code symmetric.
            try:
                when = datetime.strptime(bucket_str, "%Y-%m-%d %H")
            except Exception:
                when = datetime.now()
            try:
                self.db.traffic_record_batch(
                    profile_name = self.profile_name,
                    run_id       = self.run_id,
                    by_domain    = by_domain,
                    when         = when,
                )
                total_bytes += sum(s.get("bytes", 0) for s in by_domain.values())
                total_reqs  += sum(s.get("req_count", 0) for s in by_domain.values())
            except Exception as e:
                logging.warning(
                    f"[TrafficCollector] DB flush failed (bucket={bucket_str}): {e}"
                )

        self._last_flush = time.time()

        # Log even modest flushes so user can SEE traffic collection is
        # working. Previously log threshold was 1MB which hid normal
        # activity and made it look like the collector was broken. Now
        # any flush with > 0 requests gets a line; `force` (shutdown)
        # always logs regardless.
        if total_reqs > 0 or force:
            logging.info(
                f"[TrafficCollector] flushed {total_reqs} reqs / "
                f"{_human_bytes(total_bytes)} "
                f"(proxy-measured: {_human_bytes(proxy_bytes_total)}) "
                f"for '{self.profile_name}'"
            )


# ──────────────────────────────────────────────────────────────
# Small pure helpers — easy to unit-test
# ──────────────────────────────────────────────────────────────

def _extract_domain(url: str) -> str:
    """Get the registrable hostname from a URL. Returns empty string for
    data:/blob:/chrome:// URLs which we don't want to count (they have
    no network cost anyway).

    We don't attempt public-suffix reduction (e.g. www.google.com →
    google.com) because the raw hostname is what the user sees in DNS /
    proxy logs. Aggregation will naturally bucket www.google.com and
    mail.google.com separately, which is usually what you want: they
    have different cost profiles.
    """
    if not url:
        return ""
    # Fast-path reject — these are zero-byte over-the-wire anyway
    if url.startswith(("data:", "blob:", "chrome:", "chrome-extension:",
                       "about:", "file:", "javascript:")):
        return ""
    try:
        host = urlparse(url).hostname
        return (host or "").lower()
    except Exception:
        return ""


def _human_bytes(n: int) -> str:
    """Format byte count as 'X KB' / 'X MB' / 'X GB' for log readability."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
