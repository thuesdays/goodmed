"""
quality_manager.py -- Programmatic profile quality assessment + auto-warmup gate.

Combines four signals into a single ready/warmup/burned verdict:

  1. Fingerprint coherence_score (from fingerprints.coherence_score, written by
     runtime.py via validator.validate())
  2. Session quality (captcha rate 24h, consecutive blocks)
     -- read from SessionQualityMonitor JSON file
  3. IP burn state (any IP this profile recently used in cooldown?)
  4. Warmup freshness -- when was the last successful warmup_runs row?

Verdict:

  ready   -- all signals green; safe to do real Google searches
  warmup  -- at least one yellow signal; a quick warmup will help
             (visit news/weather/shopping for 2-5 min before next real run)
  burned  -- multiple red signals; profile should be paused or recreated

The manager does NOT itself launch a warmup run -- that's the dashboard's
or scheduler's job. It only delivers the verdict + machine-readable reasons.
This keeps the manager pure and side-effect-free, easy to unit-test.

Usage:
    from ghost_shell.profile.quality_manager import assess_profile

    verdict = assess_profile("profile_01")
    if verdict["status"] == "burned":
        return  # don't waste a run
    if verdict["status"] == "warmup":
        run_warmup_then_search()
    else:
        run_search_directly()
"""

__author__ = "Mykola Kovhanko"
__email__  = "thuesdays@gmail.com"

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional


# Thresholds. Tuned conservatively -- preference is for *over*-warmup
# rather than burning a real run on a half-cooked profile. Each one is
# documented so users tweaking these in the dashboard later know what
# they buy.

THRESHOLDS = {
    # Fingerprint coherence score below which we recommend warmup. The
    # validator's "warning" grade starts at 75; below that we flag.
    "score_warmup":        75,
    # Below this, the profile is genuinely incoherent and warmup won't
    # save it -- recommend regenerate.
    "score_burned":        50,

    # Captcha rate in last 24h. The session quality monitor already
    # exposes this -- we just compare to our own thresholds (which may
    # be stricter than its built-in critical/warning).
    "captcha_rate_warmup": 0.30,   # 30% captchas -> warmup
    "captcha_rate_burned": 0.70,   # 70% captchas -> burn

    # Consecutive blocks read from the session monitor.
    "blocks_burned":       5,

    # Hours since the last successful warmup. After a long idle period
    # the cookie state drifts (Google rotates session tokens) so it's
    # worth a quick refresh.
    "warmup_stale_hours":  72,

    # If the profile has fewer than N total runs, we treat it as fresh
    # and recommend a warmup before the first real Google search. This
    # prevents the "first ever run on a brand-new profile is a Google
    # query" pattern, which is one of the strongest auto-bot signals.
    "fresh_profile_runs":  2,
}


def _safe_get_db():
    """Local import + tolerant -- the manager must not crash callers."""
    try:
        from ghost_shell.db.database import get_db
        return get_db()
    except Exception as e:
        logging.debug(f"[QualityManager] DB unavailable: {e}")
        return None


def _read_session_health(profile_name: str) -> Optional[dict]:
    """Read session_quality.json next to the profile dir, return health."""
    try:
        from ghost_shell.session.quality import SessionQualityMonitor
        # Profile dir resolution -- try the standard layout. If it
        # fails, the monitor returns its default (in-memory) state and
        # we just get all-zeros, which is fine for a fresh profile.
        from ghost_shell.profile.manager import resolve_profile_dir
        profile_dir = resolve_profile_dir(profile_name)
        sqm = SessionQualityMonitor(profile_dir=profile_dir)
        return sqm.get_health()
    except Exception as e:
        logging.debug(f"[QualityManager] session-quality read failed: {e}")
        return None


def _hours_since(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return (datetime.now() - dt).total_seconds() / 3600.0
    except Exception:
        return None


def assess_profile(profile_name: str,
                   thresholds: Optional[dict] = None) -> dict:
    """Return a quality verdict for the given profile.

    Output schema:
      {
        "profile_name": str,
        "status":       "ready" | "warmup" | "burned",
        "score":        int,            # 0-100, derived
        "fingerprint":  {"score": int|None, "grade": str|None},
        "session":      {...}|None,     # raw session-quality dict
        "warmup":       {"hours_since_last": float|None, "stale": bool},
        "ip":           {"burned": bool, "last_ip": str|None},
        "reasons":      [str, ...],     # human-readable bullet points
        "recommendation": str,          # short tip, like "run a 3-min warmup"
        "checked_at":   ISO timestamp,
      }

    Errors degrade to {"status": "ready", "reasons": ["assessment failed: ..."]}
    so a bad assessment never blocks a run.
    """
    th = {**THRESHOLDS, **(thresholds or {})}
    out = {
        "profile_name": profile_name,
        "status":       "ready",
        "score":        100,
        "fingerprint":  {"score": None, "grade": None},
        "session":      None,
        "warmup":       {"hours_since_last": None, "stale": False},
        "ip":           {"burned": False, "last_ip": None},
        "reasons":      [],
        "recommendation": "",
        "checked_at":   datetime.now().isoformat(timespec="seconds"),
    }

    db = _safe_get_db()
    if db is None:
        out["reasons"].append("DB unavailable -- assessment skipped, treating as ready")
        return out

    yellow = 0   # bumps -> warmup
    red    = 0   # bumps -> burned

    # ── 1. Fingerprint score ─────────────────────────────────────
    try:
        fp = db.fingerprint_current(profile_name) if hasattr(db, "fingerprint_current") else None
        if fp:
            score = fp.get("coherence_score")
            grade = None
            try:
                rep = fp.get("coherence_report")
                if isinstance(rep, str):
                    rep = json.loads(rep)
                grade = (rep or {}).get("grade")
            except Exception:
                pass
            out["fingerprint"]["score"] = score
            out["fingerprint"]["grade"] = grade
            if score is not None:
                if score < th["score_burned"]:
                    red += 1
                    out["reasons"].append(
                        f"fingerprint coherence score is {score} "
                        f"(below burn threshold {th['score_burned']}) "
                        f"-- regenerate fingerprint"
                    )
                elif score < th["score_warmup"]:
                    yellow += 1
                    out["reasons"].append(
                        f"fingerprint coherence score is {score} "
                        f"(below warmup threshold {th['score_warmup']})"
                    )
            else:
                # No score yet -- the validator wasn't run on this
                # profile's fingerprint. Not a strike against the
                # profile, just a note. New runtime saves DO score
                # the fingerprint at launch (see runtime.py).
                out["reasons"].append("fingerprint not yet scored -- next launch will score it")
    except Exception as e:
        logging.debug(f"[QualityManager] fingerprint check skipped: {e}")

    # ── 2. Session quality (captchas + blocks) ───────────────────
    try:
        health = _read_session_health(profile_name)
        out["session"] = health
        if health:
            cr = health.get("captcha_rate_24h", 0) or 0
            cb = health.get("consecutive_blocks", 0) or 0
            if cb >= th["blocks_burned"]:
                red += 1
                out["reasons"].append(
                    f"{cb} consecutive blocks -- IP/profile combo is burned"
                )
            elif cr >= th["captcha_rate_burned"]:
                red += 1
                out["reasons"].append(
                    f"captcha rate {cr:.0%} (24h) -- profile is burned"
                )
            elif cr >= th["captcha_rate_warmup"]:
                yellow += 1
                out["reasons"].append(
                    f"elevated captcha rate {cr:.0%} (24h) -- warmup before next real run"
                )
    except Exception as e:
        logging.debug(f"[QualityManager] session check skipped: {e}")

    # ── 3. IP burn state (last IP this profile used) ─────────────
    try:
        # Use the runs table to find the most recent IP this profile
        # used; ip_history then tells us if it's burned.
        conn = db._get_conn()
        row = conn.execute(
            "SELECT ip FROM runs WHERE profile_name = ? AND ip IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (profile_name,),
        ).fetchone()
        last_ip = row["ip"] if row and "ip" in row.keys() else None
        out["ip"]["last_ip"] = last_ip
        if last_ip:
            ip_row = db.ip_get(last_ip) if hasattr(db, "ip_get") else None
            burned_at = (ip_row or {}).get("burned_at")
            if burned_at:
                out["ip"]["burned"] = True
                yellow += 1
                out["reasons"].append(
                    f"last IP used by this profile ({last_ip}) is in cooldown "
                    f"since {burned_at} -- next run will need rotation"
                )
    except Exception as e:
        logging.debug(f"[QualityManager] ip-burn check skipped: {e}")

    # ── 4. Warmup freshness + freshness vs total runs ────────────
    try:
        conn = db._get_conn()
        # Last successful warmup (status='ok')
        row = conn.execute(
            "SELECT finished_at FROM warmup_runs "
            "WHERE profile_name = ? AND status = 'ok' "
            "ORDER BY id DESC LIMIT 1",
            (profile_name,),
        ).fetchone()
        last_warmup_at = row["finished_at"] if row and "finished_at" in row.keys() else None
        hrs = _hours_since(last_warmup_at)
        out["warmup"]["hours_since_last"] = hrs
        if last_warmup_at and hrs is not None and hrs > th["warmup_stale_hours"]:
            yellow += 1
            out["warmup"]["stale"] = True
            out["reasons"].append(
                f"last warmup was {hrs:.0f}h ago "
                f"(stale after {th['warmup_stale_hours']}h)"
            )

        # Total runs to detect a fresh profile that has never been warmed up
        total_runs = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE profile_name = ?",
            (profile_name,),
        ).fetchone()
        n = total_runs["n"] if total_runs else 0
        if n < th["fresh_profile_runs"] and not last_warmup_at:
            yellow += 1
            out["reasons"].append(
                f"profile has only {n} prior run(s) and no warmup history -- "
                f"warm up before first Google query"
            )
    except Exception as e:
        logging.debug(f"[QualityManager] warmup check skipped: {e}")

    # ── Verdict ──────────────────────────────────────────────────
    if red > 0:
        out["status"]         = "burned"
        out["recommendation"] = (
            "skip this profile or regenerate it; if hosted on a fresh IP "
            "from a different /24, you can also try one full warmup pass "
            "and only then attempt a real run"
        )
        # Score: cap at 30 when burned so the badge is unambiguous
        out["score"] = max(0, 30 - 5 * red)
    elif yellow > 0:
        out["status"]         = "warmup"
        out["recommendation"] = (
            "run a 2-3 min warmup (news/weather/shopping) before the next "
            "real Google query"
        )
        out["score"] = max(40, 80 - 10 * yellow)
    else:
        out["status"]         = "ready"
        out["recommendation"] = "profile is healthy; safe for real runs"
        out["score"]          = 100

    return out


def should_auto_warmup(profile_name: str,
                       thresholds: Optional[dict] = None) -> tuple[bool, str]:
    """Convenience predicate for the scheduler / runtime.

    Returns (should_warmup, reason). Does NOT fire the warmup itself --
    callers (scheduler, dashboard, main.py) decide what to do.
    """
    v = assess_profile(profile_name, thresholds)
    if v["status"] == "warmup":
        return True, "; ".join(v["reasons"]) or "profile needs warmup"
    if v["status"] == "burned":
        # Burned profiles benefit MORE from warmup than ready ones, but
        # the caller should also rotate IP first; we still return True
        # so the warmup pipeline runs. The caller can override.
        return True, "burned: " + ("; ".join(v["reasons"]) or "many fail signals")
    return False, "profile is ready"
