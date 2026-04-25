"""
session_quality.py — Profile session health monitor

Tracks metrics that show whether a profile is "burned":
- Captcha frequency
- Search success rate (finds results or empty)
- Time to results
- Consecutive failures

Writes events to BOTH legacy JSON file AND the SQLite database (events table).

Based on these metrics, a profile can be marked as "degraded" and
recreated. This is what makes Dolphin-style profiles long-lived — they
monitor health and swap fingerprint before it's too late.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict


@dataclass
class SessionMetric:
    """Single metric record"""
    timestamp:     str
    event:         str   # search_ok | search_empty | captcha | captcha_solved | blocked | search_fail
    query:         str = ""
    results_count: int = 0
    duration_sec:  float = 0.0
    details:       str = ""


class SessionQualityMonitor:
    """
    Usage:
        sqm = SessionQualityMonitor(browser.user_data_path)

        # In search loop
        sqm.record("search_ok", query=query, results_count=len(results))
        sqm.record("captcha", query=query)

        # Before next run
        should_abort, reason = sqm.should_abort()
        if should_abort:
            # recreate profile or switch IP
            ...

    Thresholds (softer than before to avoid premature blocking):
        WARNING_CAPTCHA_RATE   = 0.30  (30% captcha in 24h → warning)
        CRITICAL_CAPTCHA_RATE  = 0.70  (70% captcha in 24h → critical)
        CRITICAL_BLOCKED_IN_ROW = 5    (5 consecutive blocks → critical)
        MIN_SEARCHES_FOR_JUDGE  = 10   (need at least 10 searches before judging)
    """

    # Thresholds
    CRITICAL_CAPTCHA_RATE   = 0.70   # 70%+ captcha in 24h → critical
    WARNING_CAPTCHA_RATE    = 0.30   # 30%+ captcha → warning
    CRITICAL_BLOCKED_IN_ROW = 5      # 5 blocks in a row → critical
    MIN_SEARCHES_FOR_JUDGE  = 10     # need this many searches before we judge

    def __init__(self, profile_path: str):
        self.profile_path = profile_path
        self.metrics_file = os.path.join(profile_path, "session_quality.json")
        self._metrics: list[dict] = []
        self._load()

    # ──────────────────────────────────────────────────────────
    # PERSISTENCE
    # ──────────────────────────────────────────────────────────

    def _load(self):
        if os.path.exists(self.metrics_file):
            try:
                with open(self.metrics_file, "r", encoding="utf-8") as f:
                    self._metrics = json.load(f)
            except Exception as e:
                logging.warning(f"[SessionQuality] Load error: {e}")
                self._metrics = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.metrics_file), exist_ok=True)
            with open(self.metrics_file, "w", encoding="utf-8") as f:
                json.dump(self._metrics, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.warning(f"[SessionQuality] Save error: {e}")

    # ──────────────────────────────────────────────────────────
    # RECORDING EVENTS
    # ──────────────────────────────────────────────────────────

    def record(self, event: str, **kwargs):
        """
        Register event. Supported types:
        - search_ok:      successful search with results
        - search_empty:   search returned no ads
        - captcha:        captcha page appeared
        - captcha_solved: captcha was solved successfully
        - blocked:        IP/profile blocked by Google (hard)
        - search_fail:    search failed (unrelated error)

        Writes to BOTH legacy JSON file AND SQLite DB.
        """
        metric = SessionMetric(
            timestamp     = datetime.now().isoformat(timespec="seconds"),
            event         = event,
            query         = kwargs.get("query", ""),
            results_count = kwargs.get("results_count", 0),
            duration_sec  = kwargs.get("duration_sec", 0.0),
            details       = kwargs.get("details", ""),
        )
        self._metrics.append(asdict(metric))
        self._save()

        # Also write to SQLite (events table) — primary store for dashboard
        try:
            from ghost_shell.db.database import get_db
            run_id = None
            try:
                run_id = int(os.environ.get("GHOST_SHELL_RUN_ID", "0")) or None
            except Exception:
                pass

            profile_name = os.path.basename(self.profile_path.rstrip(os.sep))
            get_db().event_record(
                run_id        = run_id,
                profile_name  = profile_name,
                event_type    = event,
                query         = metric.query or None,
                details       = metric.details or None,
                duration_sec  = metric.duration_sec,
                results_count = metric.results_count,
            )
        except Exception as e:
            logging.debug(f"[SessionQuality] DB write failed: {e}")

    # ──────────────────────────────────────────────────────────
    # HEALTH ANALYSIS
    # ──────────────────────────────────────────────────────────

    def _metrics_within(self, hours: int) -> list[dict]:
        """Return metrics from last N hours"""
        threshold = datetime.now() - timedelta(hours=hours)
        result = []
        for m in self._metrics:
            try:
                ts = datetime.fromisoformat(m["timestamp"])
                if ts >= threshold:
                    result.append(m)
            except Exception:
                continue
        return result

    def get_health(self) -> dict:
        """
        Returns profile health:
        - status: healthy | warning | critical
        - various rates and counts
        - recommendations list
        """
        recent_24h = self._metrics_within(24)
        recent_1h  = self._metrics_within(1)

        searches = sum(1 for m in recent_24h if m["event"] in ("search_ok", "search_empty"))
        captchas = sum(1 for m in recent_24h if m["event"] == "captcha")
        empty    = sum(1 for m in recent_24h if m["event"] == "search_empty")
        total    = searches + captchas

        captcha_rate = captchas / total if total > 0 else 0

        # Consecutive blocks (from the end of metrics list)
        consecutive_blocks = 0
        for m in reversed(self._metrics):
            if m["event"] == "blocked":
                consecutive_blocks += 1
            elif m["event"] in ("search_ok",):
                break

        # Captcha rate in last hour
        recent_searches_1h = sum(1 for m in recent_1h if m["event"] in ("search_ok", "search_empty"))
        recent_captchas_1h = sum(1 for m in recent_1h if m["event"] == "captcha")
        recent_total_1h    = recent_searches_1h + recent_captchas_1h
        captcha_rate_1h    = recent_captchas_1h / recent_total_1h if recent_total_1h > 0 else 0

        # Determine status
        status = "healthy"
        recommendations = []

        if consecutive_blocks >= self.CRITICAL_BLOCKED_IN_ROW:
            status = "critical"
            recommendations.append(
                f"{consecutive_blocks} consecutive blocks — recreate profile or switch IP"
            )
        elif total >= self.MIN_SEARCHES_FOR_JUDGE and captcha_rate >= self.CRITICAL_CAPTCHA_RATE:
            status = "critical"
            recommendations.append(
                f"Captcha in {captcha_rate:.0%} of searches (24h) — profile burned"
            )
        elif recent_total_1h >= 10 and captcha_rate_1h >= self.CRITICAL_CAPTCHA_RATE:
            status = "critical"
            recommendations.append(
                f"Sudden captcha spike: {captcha_rate_1h:.0%} in the last hour"
            )
        elif total >= self.MIN_SEARCHES_FOR_JUDGE and captcha_rate >= self.WARNING_CAPTCHA_RATE:
            status = "warning"
            recommendations.append(
                f"Elevated captcha rate {captcha_rate:.0%} — consider a 30-minute break"
            )

        # Empty results — possible soft-block
        if searches >= 10 and empty / searches >= 0.9:
            if status == "healthy":
                status = "warning"
            recommendations.append(
                f"{empty}/{searches} searches returned empty — possible soft-block"
            )

        return {
            "status":              status,
            "captcha_rate_24h":    round(captcha_rate, 3),
            "captcha_rate_1h":     round(captcha_rate_1h, 3),
            "consecutive_blocks":  consecutive_blocks,
            "total_searches_24h":  searches,
            "total_captchas_24h":  captchas,
            "empty_results_24h":   empty,
            "total_in_log":        len(self._metrics),
            "recommendations":     recommendations,
        }

    def print_report(self):
        health = self.get_health()

        icons = {"healthy": "OK", "warning": "WARN", "critical": "CRIT"}
        icon  = icons.get(health["status"], "?")

        print("\n" + "=" * 60)
        print(f" PROFILE HEALTH  [{icon}]  {health['status'].upper()}")
        print("=" * 60)
        print(f" Searches in 24h:     {health['total_searches_24h']}")
        print(f" Captchas in 24h:     {health['total_captchas_24h']}")
        print(f" Captcha rate 24h:    {health['captcha_rate_24h']:.1%}")
        print(f" Captcha rate 1h:     {health['captcha_rate_1h']:.1%}")
        print(f" Empty results 24h:   {health['empty_results_24h']}")
        print(f" Consecutive blocks:  {health['consecutive_blocks']}")
        print(f" Total records:       {health['total_in_log']}")

        if health["recommendations"]:
            print("\n Recommendations:")
            for rec in health["recommendations"]:
                print(f"   - {rec}")
        print("=" * 60 + "\n")

    # ──────────────────────────────────────────────────────────
    # CONTROL
    # ──────────────────────────────────────────────────────────

    def should_abort(self) -> tuple[bool, str]:
        """
        Returns (should_abort, reason).
        True only if profile is DEFINITELY burned — be conservative to avoid
        false positives on the first runs (when statistics are tiny).

        Rules (conservative):
        - Need at least MIN_SEARCHES_FOR_JUDGE searches before we can judge
          captcha rate
        - Consecutive blocks need to reach CRITICAL_BLOCKED_IN_ROW (5 now)
        - "blocked" event is created only when captcha is NOT SOLVED, so
          3-5 such events in a row is genuinely bad
        """
        health = self.get_health()

        # Only abort on hard critical
        if health["status"] != "critical":
            return False, ""

        # If it's the very first session — no data yet, never abort
        if health["total_in_log"] < 3:
            return False, ""

        return True, health["recommendations"][0] if health["recommendations"] else "critical status"

    def clear(self):
        """Reset history — after profile recreation"""
        self._metrics = []
        self._save()
        logging.info("[SessionQuality] History cleared")

    def reset_consecutive_blocks(self):
        """
        Adds a synthetic 'search_ok' marker at the end to break the
        chain of consecutive blocks. Useful when you know the root cause
        (e.g. forgot to configure proxy) has been fixed.
        """
        self._metrics.append({
            "timestamp":     datetime.now().isoformat(timespec="seconds"),
            "event":         "search_ok",
            "query":         "",
            "results_count": 0,
            "duration_sec":  0.0,
            "details":       "manual_reset_marker",
        })
        self._save()
        logging.info("[SessionQuality] Consecutive blocks reset")
