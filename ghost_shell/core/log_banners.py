"""
log_banners.py — Pretty log banners for monitor runs.

Turns raw log spam into structured banners you can skim at a glance:

    ═══════════════════════════════════════════════════════════════════
     RUN #7 STARTED
    ═══════════════════════════════════════════════════════════════════
     Profile    : profile_01
     Template   : office_laptop_intel (Chrome 132.0.6834.210)
     Locale     : uk-UA (Europe/Kyiv, UTC+03:00)
     Screen     : 1920x1080 @ 1.0x
     Hardware   : 4 CPU cores, 8 GB RAM
     GPU        : Intel HD Graphics 620
     UA         : Mozilla/5.0 (Windows NT 10.0;...) Chrome/132.0.0.0
     Proxy      : 109.236.84.23:16720 (asocks, rotating)
     Exit IP    : 193.32.154.239 [Ukraine / DataWeb]
     Queries    : goodmedika (Latin and brand spellings)
     Targets    : goodmedika.com.ua, goodmedika.ua
    ═══════════════════════════════════════════════════════════════════

Usage:
    from ghost_shell.core.log_banners import log_run_start, log_run_end, log_query_result

    log_run_start(run_id, profile_name, payload, proxy_url,
                  exit_ip, queries, target_domains)

    log_query_result(idx, total, query, ads_found, competitors)

    log_run_end(run_id, duration_sec, stats_dict)
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import logging
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Box drawing
# ──────────────────────────────────────────────────────────────

WIDTH = 75
LINE  = "═" * WIDTH
THIN  = "─" * WIDTH


def _box(title: str, rows: list, emoji: str = ""):
    """Render a boxed block: title line + key/value rows."""
    out = ["", LINE, f" {emoji + ' ' if emoji else ''}{title}", LINE]
    for label, value in rows:
        # Multi-line values: indent continuation lines
        value_str = str(value) if value is not None else "—"
        first, *rest = value_str.split("\n")
        out.append(f" {label:<11}: {first}")
        for cont in rest:
            out.append(f" {'':<11}  {cont}")
    out.append(LINE)
    return "\n".join(out)


def _truncate(s: Optional[str], limit: int) -> str:
    if not s:
        return "—"
    s = str(s)
    return s if len(s) <= limit else s[:limit - 1] + "…"


# ──────────────────────────────────────────────────────────────
# Banners
# ──────────────────────────────────────────────────────────────

def log_run_start(
    run_id: int,
    profile_name: str,
    payload: Optional[dict] = None,
    proxy_url: str = None,
    exit_ip: str = None,
    exit_ip_geo: dict = None,
    queries: list = None,
    target_domains: list = None,
    chrome_path: str = None,
    rotating: bool = False,
    rotation_provider: str = None,
):
    """Render a big pretty banner at the start of a run."""
    rows = [("Profile", profile_name)]

    if payload:
        tmpl = payload.get("template_name") or "?"
        chr_ver = (payload.get("ua_metadata") or {}).get("full_version", "?")
        rows.append(("Template", f"{tmpl} (Chrome {chr_ver})"))

        langs = payload.get("languages") or {}
        tz    = payload.get("timezone") or {}
        off_m = tz.get("offset_min", 0)
        # In payload, offset_min follows JS Date.getTimezoneOffset() convention:
        # positive = behind UTC, negative = ahead of UTC. Flip for display so
        # Kyiv (-180 in JS) shows as "UTC+03:00" which is how humans read it.
        real_off_m = -off_m
        sign  = "+" if real_off_m >= 0 else "-"
        off_s = f"UTC{sign}{abs(real_off_m)//60:02d}:{abs(real_off_m)%60:02d}"
        rows.append(("Locale",
            f"{langs.get('language', '?')} "
            f"({tz.get('id', '?')}, {off_s})"))

        scr = payload.get("screen") or {}
        if scr:
            rows.append(("Screen",
                f"{scr.get('width')}x{scr.get('height')} "
                f"@ {scr.get('pixel_ratio', 1):.1f}x"))

        hw = payload.get("hardware") or {}
        if hw:
            rows.append(("Hardware",
                f"{hw.get('hardware_concurrency')} CPU cores, "
                f"{hw.get('device_memory')} GB RAM"))

        gfx = payload.get("graphics") or {}
        if gfx.get("gl_renderer"):
            rows.append(("GPU", _truncate(gfx["gl_renderer"], 55)))

        ua = (payload.get("hardware") or {}).get("user_agent")
        if ua:
            rows.append(("UA", _truncate(ua, 55)))

    if proxy_url:
        proxy_display = proxy_url
        if "@" in proxy_url:
            proxy_display = "***@" + proxy_url.split("@", 1)[1]
        proxy_note = ""
        if rotating:
            proxy_note = f" (rotating via {rotation_provider or 'auto'})"
        rows.append(("Proxy", f"{proxy_display}{proxy_note}"))

    if exit_ip:
        geo_str = ""
        if exit_ip_geo:
            geo_str = (f" [{exit_ip_geo.get('country', '?')}"
                       f" / {exit_ip_geo.get('org', '?')}]")
        rows.append(("Exit IP", f"{exit_ip}{geo_str}"))

    if queries:
        rows.append(("Queries", ", ".join(queries)))

    if target_domains:
        rows.append(("Targets", ", ".join(target_domains)))

    if chrome_path:
        rows.append(("Chrome bin", chrome_path))

    logging.info(_box(f"RUN #{run_id} STARTED", rows, emoji="▶"))


def log_run_end(
    run_id: int,
    duration_sec: float,
    exit_code: int = 0,
    stats: Optional[dict] = None,
    error: Optional[str] = None,
):
    """Banner at the end of a run with summary stats."""
    rows = [("Duration", _format_duration(duration_sec)),
            ("Exit code", exit_code)]

    if stats:
        if "total_ads" in stats:
            rows.append(("Total ads", stats["total_ads"]))
        if "competitors" in stats:
            rows.append(("Competitors",
                f"{stats['competitors']} unique "
                f"({stats.get('competitors_new', 0)} new)"))
        if "queries_done" in stats:
            rows.append(("Queries",
                f"{stats['queries_done']}/{stats.get('queries_total', '?')}"))
        if "captchas" in stats:
            rows.append(("Captchas", stats["captchas"]))
        if "empty_results" in stats:
            rows.append(("Empty results", stats["empty_results"]))
        if "actions_done" in stats:
            rows.append(("Actions done", stats["actions_done"]))
        if "health_status" in stats:
            rows.append(("Health", stats["health_status"]))

    if error:
        rows.append(("Error", _truncate(error, 60)))

    title = f"RUN #{run_id} " + ("COMPLETED" if exit_code == 0 else "FAILED")
    emoji = "✓" if exit_code == 0 else "✗"
    logging.info(_box(title, rows, emoji=emoji))


def log_query_result(
    idx: int,
    total: int,
    query: str,
    ads_found: int,
    competitors_found: int,
    duration_sec: float = None,
    my_domain_matched: bool = False,
):
    """Compact one-liner plus hints for each query processed."""
    dur = f" in {duration_sec:.1f}s" if duration_sec is not None else ""
    badge = ""
    if ads_found == 0:
        badge = "  (no ads — possible soft-block)"
    elif my_domain_matched:
        badge = "  ✓ your domain present"

    logging.info(
        f" [{idx}/{total}] \"{query}\" → "
        f"{ads_found} ads, {competitors_found} competitors{dur}{badge}"
    )


def log_payload_summary(payload: dict, level: int = logging.DEBUG):
    """
    Short structured summary of the generated fingerprint payload —
    enough to correlate against observed detection without dumping the
    whole 7800-char JSON.

    At DEBUG level by default — use logging.INFO to see it in normal runs.
    """
    hw   = payload.get("hardware")   or {}
    gfx  = payload.get("graphics")   or {}
    scr  = payload.get("screen")     or {}
    lang = payload.get("languages")  or {}
    tz   = payload.get("timezone")   or {}
    uam  = payload.get("ua_metadata") or {}

    summary = {
        "template":       payload.get("template_name"),
        "chrome":         uam.get("full_version"),
        "platform":       hw.get("platform"),
        "cpu_cores":      hw.get("hardware_concurrency"),
        "ram_gb":         hw.get("device_memory"),
        "screen":         f"{scr.get('width')}x{scr.get('height')}",
        "pixel_ratio":    scr.get("pixel_ratio"),
        "language":       lang.get("language"),
        "languages":      lang.get("languages"),
        "timezone":       tz.get("id"),
        "tz_offset_min":  tz.get("offset_min"),
        "gpu_vendor":     gfx.get("gl_vendor"),
        "gpu_renderer":   _truncate(gfx.get("gl_renderer"), 50),
        "has_battery":    payload.get("battery") is not None,
        "fonts_count":    len(payload.get("fonts") or []),
        "webgl_exts":     len(gfx.get("webgl_extensions") or []),
        "plugins_count":  len(payload.get("plugins") or []),
    }

    logging.log(level, "Payload summary:\n" +
                json.dumps(summary, indent=2, ensure_ascii=False))


def log_step(name: str, extra: str = ""):
    """Short inline step marker: ▶ step_name — some extra info."""
    msg = f" ▶ {name}"
    if extra:
        msg += f" — {extra}"
    logging.info(msg)


def log_error_banner(title: str, details: str):
    """Compact error banner — less noisy than a full Python traceback."""
    out = ["", THIN, f" ✗ {title}", THIN, f" {details}", THIN]
    logging.error("\n".join(out))


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _format_duration(seconds: float) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"
