"""
fingerprint_validator.py — Coherence rules engine + scoring.

Takes a fingerprint dict (either configured expectations OR runtime-
observed values) and a device template. Checks all fields vs the rules
encoded in the template. Returns:

    {
        "score":       0-100 (higher = more coherent),
        "grade":       "excellent" | "good" | "warning" | "critical",
        "checks":      [ {name, category, status, detail}, ... ],
        "summary":     "72/100 — 3 warnings, 1 critical",
        "critical":    [list of critical failures],
        "warnings":    [list of warnings],
    }

Philosophy: scoring is multiplicative — each critical failure
significantly drops score; warnings subtract small amounts. We avoid
linear summing because missing one critical signal (like Mac fonts on
a Windows UA) is not "90% OK" — it's a bot-killer.

Categories of checks:
    critical  — bot-killer if wrong (OS ↔ UA, GPU ↔ OS, forbidden fonts)
    important — strong signal (screen ratios, hardware concurrency)
    warning   — mild signal (timezone matches language, DPR matches screen)
    info      — nice to match, low impact

Check function signature:
    check(fp: dict, template: dict) -> (status, detail) | None
        status: "pass" | "fail" | "warn" | "n/a"
        detail: human-readable explanation
        None   : check skipped (field not present in fp)
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import re


# ═══════════════════════════════════════════════════════════════
# CHECK HELPERS
# ═══════════════════════════════════════════════════════════════

def _get(fp: dict, *path, default=None):
    """Safe nested dict access: _get(fp, 'navigator', 'platform')."""
    cur = fp
    for key in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
    return cur if cur is not None else default


# ═══════════════════════════════════════════════════════════════
# CHECK DEFINITIONS
# Each check receives (fp, template) and returns (status, detail).
# Checks return None to skip (field missing — can't validate).
# ═══════════════════════════════════════════════════════════════

def check_ua_platform_matches_os(fp, template):
    """UA string must contain the OS token of the template."""
    ua = _get(fp, "navigator", "userAgent") or _get(fp, "user_agent")
    if not ua:
        return None
    expected_token = template["ua_platform_token"]
    if expected_token in ua:
        return ("pass", f"UA contains '{expected_token}' ✓")
    # Partial match allowed — Chrome sometimes omits minor parts
    core = expected_token.split(";")[0].strip()  # "Windows NT 10.0"
    if core in ua:
        return ("warn", f"UA contains OS core '{core}' but "
                        f"differs from expected token")
    return ("fail", f"UA does not contain '{core}' — OS mismatch. "
                    f"UA starts with: {ua[:120]}")


def check_navigator_platform(fp, template):
    """navigator.platform matches template expectation."""
    plat = _get(fp, "navigator", "platform") or _get(fp, "platform")
    if plat is None:
        return None
    expected = template["navigator_platform"]
    if plat == expected:
        return ("pass", f"navigator.platform = '{plat}' ✓")
    return ("fail", f"navigator.platform = '{plat}', "
                    f"expected '{expected}'")


def check_gpu_vendor(fp, template):
    """GPU vendor must match template's expected vendor family."""
    vendor = (_get(fp, "webgl", "vendor") or
              _get(fp, "gpu", "vendor") or
              _get(fp, "gl_vendor"))
    if vendor is None:
        return None
    expected = template["gpu"]["vendor"]
    # Permissive match — full vendor string changes between Chrome versions
    expected_key = expected.split("(")[-1].rstrip(")").strip().lower()
    # e.g. "Google Inc. (Apple)" → key "apple"
    if expected_key.lower() in vendor.lower():
        return ("pass", f"GPU vendor '{vendor}' matches '{expected_key}' ✓")
    return ("fail", f"GPU vendor '{vendor}' does not contain "
                    f"'{expected_key}' — OS/GPU mismatch")


def check_gpu_renderer(fp, template):
    """GPU renderer plausibility — must look like one of the templates."""
    renderer = (_get(fp, "webgl", "renderer") or
                _get(fp, "gpu", "renderer") or
                _get(fp, "gl_renderer"))
    if renderer is None:
        return None
    # Extract key tokens from template renderers
    templates = template["gpu"]["renderer_templates"]
    # Each template is like "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, ...)"
    # We check that at least one template's "core" brand token appears
    for t in templates:
        # Pick 2-3 distinctive tokens from each template
        tokens = re.findall(r"\b(Apple M\d[^,]*|Intel\(R\)[^,]*|"
                            r"NVIDIA[^,]*|AMD Radeon[^,]*|Mesa[^,]*)",
                            t)
        for token in tokens:
            if token in renderer:
                return ("pass", f"GPU renderer matches '{token}' ✓")
    return ("warn", f"GPU renderer '{renderer[:80]}' doesn't match "
                    f"known templates — may still be valid")


def check_screen_dimensions(fp, template):
    """Screen width/height must be one of the template's options."""
    w = _get(fp, "screen", "width")
    h = _get(fp, "screen", "height")
    if w is None or h is None:
        return None
    options = template["screen_options"]
    for opt in options:
        if opt["width"] == w and opt["height"] == h:
            return ("pass", f"Screen {w}×{h} matches template option ✓")
    offered = ", ".join(f"{o['width']}×{o['height']}" for o in options)
    return ("fail", f"Screen {w}×{h} not in template options: {offered}")


def check_dpr(fp, template):
    """Device pixel ratio must match one of the template's options."""
    dpr = _get(fp, "window", "devicePixelRatio") or _get(fp, "dpr")
    if dpr is None:
        return None
    options = template["screen_options"]
    allowed_dprs = [o["dpr"] for o in options]
    # Accept within 0.05 tolerance (Chrome rounds sometimes)
    if any(abs(dpr - d) < 0.05 for d in allowed_dprs):
        return ("pass", f"devicePixelRatio {dpr} matches ✓")
    return ("warn", f"devicePixelRatio {dpr} not in {allowed_dprs}")


def check_hardware_concurrency(fp, template):
    hc = _get(fp, "navigator", "hardwareConcurrency") or _get(fp, "hardware_concurrency")
    if hc is None:
        return None
    options = template["hardware_concurrency_options"]
    if hc in options:
        return ("pass", f"hardwareConcurrency {hc} ✓")
    # Close values acceptable — just warn
    if any(abs(hc - o) <= 2 for o in options):
        return ("warn", f"hardwareConcurrency {hc} close to "
                        f"template options {options}")
    return ("fail", f"hardwareConcurrency {hc} not in {options}")


def check_device_memory(fp, template):
    """navigator.deviceMemory. Only exposed on certain platforms."""
    mem = _get(fp, "navigator", "deviceMemory") or _get(fp, "device_memory")
    options = template["device_memory_options"]
    if mem is None:
        return None
    if mem in options:
        return ("pass", f"deviceMemory {mem} ✓")
    return ("warn", f"deviceMemory {mem} not in template {options}")


def check_max_touch_points(fp, template):
    tp = _get(fp, "navigator", "maxTouchPoints") or _get(fp, "max_touch_points")
    if tp is None:
        return None
    expected = template["max_touch_points"]
    if tp == expected:
        return ("pass", f"maxTouchPoints = {tp} ✓")
    return ("fail", f"maxTouchPoints {tp}, expected {expected} — "
                    f"desktop profile should not expose touch")


def check_mobile_ua_consistency(fp, template):
    """Mobile templates must have "Mobile" in the UA; desktop must not.
    A mobile template that produces a desktop-looking UA is the classic
    sign of a half-applied emulation (viewport switched but UA forgot)."""
    ua = _get(fp, "navigator", "userAgent") or _get(fp, "user_agent")
    if not ua:
        return None
    is_mobile = bool(template.get("is_mobile"))
    has_mobile = "Mobile" in ua
    if is_mobile and not has_mobile:
        return ("fail",
                "UA missing 'Mobile' suffix — mobile template produced a "
                "desktop-shaped UA")
    if not is_mobile and has_mobile:
        return ("fail",
                "UA contains 'Mobile' — desktop template should not")
    return ("pass", "UA mobile marker matches template ✓")


def check_mobile_viewport(fp, template):
    """Mobile templates should have portrait-ish dimensions (height > width)."""
    if not template.get("is_mobile"):
        return None
    w = _get(fp, "screen", "width")
    h = _get(fp, "screen", "height")
    if w is None or h is None:
        return None
    if h <= w:
        return ("fail",
                f"mobile template has landscape screen {w}x{h} — "
                f"phone fingerprints should be portrait by default")
    if w > 600:
        return ("warn",
                f"mobile screen width {w}px is unusually wide for a phone")
    return ("pass", f"mobile portrait viewport {w}x{h} ✓")


def check_fonts_no_forbidden(fp, template):
    """No forbidden fonts must be installed (Mac fonts on Windows, etc)."""
    fonts = _get(fp, "fonts") or _get(fp, "available_fonts") or []
    if not fonts:
        return None
    if isinstance(fonts, dict):
        fonts = list(fonts.keys())
    forbidden = template["fonts_forbidden"]
    caught = [f for f in forbidden if f in fonts]
    if caught:
        return ("fail", f"Forbidden fonts present: {caught} — "
                        f"these should not exist on {template['os']}")
    return ("pass", f"No forbidden fonts ✓ ({len(forbidden)} rules checked)")


def check_fonts_core_present(fp, template):
    """All core fonts of the OS must be present."""
    fonts = _get(fp, "fonts") or _get(fp, "available_fonts") or []
    if not fonts:
        return None
    if isinstance(fonts, dict):
        fonts = list(fonts.keys())
    core = template["fonts_core"]
    missing = [f for f in core if f not in fonts]
    if not missing:
        return ("pass", f"All {len(core)} core fonts present ✓")
    if len(missing) > len(core) * 0.3:
        return ("fail", f"Missing {len(missing)}/{len(core)} core "
                        f"fonts (e.g. {missing[:3]}) — fingerprint "
                        f"looks like wrong OS")
    return ("warn", f"Missing {len(missing)} core fonts: {missing[:5]}")


def check_chrome_version_in_range(fp, template):
    """Chrome version parsed from UA should be in template range."""
    ua = _get(fp, "navigator", "userAgent") or _get(fp, "user_agent")
    if not ua:
        return None
    m = re.search(r"Chrome/(\d+)", ua)
    if not m:
        return ("warn", "Could not parse Chrome version from UA")
    version = int(m.group(1))
    low, high = template["chrome_version_range"]
    if low <= version <= high:
        return ("pass", f"Chrome {version} in template range {low}-{high} ✓")
    if version < low:
        return ("warn", f"Chrome {version} below template range "
                        f"{low}-{high} — outdated spoof")
    return ("warn", f"Chrome {version} above template range — "
                    f"ahead of actual rollout")


def check_timezone_plausibility(fp, template):
    """Timezone should be in the template's common list (soft check)."""
    tz = _get(fp, "timezone", "intl") or _get(fp, "timezone")
    if not tz:
        return None
    common = template["timezone_cities_common"]
    if tz in common:
        return ("pass", f"Timezone {tz} in template's common list ✓")
    return ("warn", f"Timezone {tz} is unusual for this template — "
                    f"not necessarily wrong, but rare combo")


def check_language_plausibility(fp, template):
    lang = _get(fp, "navigator", "language") or _get(fp, "language")
    if not lang:
        return None
    common = template["languages_common"]
    if lang in common:
        return ("pass", f"Language {lang} plausible ✓")
    # Permit language-matches-timezone rule (e.g. fr-CA + Montreal)
    return ("warn", f"Language {lang} not in template's common list")


def check_webdriver_false(fp, template):
    """CRITICAL — navigator.webdriver must be false/undefined."""
    wd = _get(fp, "navigator", "webdriver")
    if wd is None:
        return ("pass", "navigator.webdriver = undefined ✓")
    if wd is False:
        return ("pass", "navigator.webdriver = false ✓")
    return ("fail", f"navigator.webdriver = {wd} — BOT SIGNAL, "
                    f"stealth patch failed!")


def check_vendor_chrome(fp, template):
    """navigator.vendor should be 'Google Inc.' on Chrome."""
    vendor = _get(fp, "navigator", "vendor")
    if vendor is None:
        return None
    if vendor == "Google Inc.":
        return ("pass", "navigator.vendor = 'Google Inc.' ✓")
    return ("fail", f"navigator.vendor = '{vendor}' — expected "
                    f"'Google Inc.' for Chrome")


def check_audio_sample_rate(fp, template):
    """AudioContext sampleRate commonly 48000 on desktop."""
    rate = _get(fp, "audio", "sampleRate") or _get(fp, "audio_sample_rate")
    if rate is None:
        return None
    expected = template["audio_sample_rate"]
    if rate == expected:
        return ("pass", f"Audio sample rate {rate} ✓")
    # 44100 is plausible on some older hardware
    if rate in (44100, 48000, 96000):
        return ("warn", f"Audio sample rate {rate} not template-typical "
                        f"({expected}) but plausible")
    return ("fail", f"Audio sample rate {rate} is unusual")


# ═══════════════════════════════════════════════════════════════
# CHECK REGISTRY — with category + weight
# ═══════════════════════════════════════════════════════════════
# Weight is the score impact of a FAILURE.
# Critical checks: high weight (25-35) — bot-killers.
# Important checks: medium weight (10-15).
# Warnings: low weight (3-7).

CHECKS = [
    # (name, category, weight_fail, weight_warn, check_fn)
    ("UA platform matches OS",     "critical",  30, 10, check_ua_platform_matches_os),
    ("navigator.platform",          "critical",  25,  8, check_navigator_platform),
    # webdriver is a special case: if it's exposed, game over.
    # Weight is artificially huge so score collapses immediately.
    ("navigator.webdriver = false", "critical",  60,  0, check_webdriver_false),
    ("navigator.vendor = Google",   "critical",  15,  5, check_vendor_chrome),
    ("GPU vendor matches OS",       "critical",  25,  8, check_gpu_vendor),
    ("GPU renderer plausibility",   "important", 10,  3, check_gpu_renderer),
    ("Screen dimensions",           "important", 12,  4, check_screen_dimensions),
    ("Device pixel ratio",          "important",  8,  2, check_dpr),
    ("Hardware concurrency",        "important",  7,  2, check_hardware_concurrency),
    ("Device memory",               "warning",    4,  1, check_device_memory),
    ("maxTouchPoints",              "important", 10,  3, check_max_touch_points),
    ("mobile UA marker",            "critical",  20,  5, check_mobile_ua_consistency),
    ("mobile viewport",             "important", 10,  3, check_mobile_viewport),
    ("No forbidden fonts",          "critical",  30, 10, check_fonts_no_forbidden),
    ("Core fonts present",          "important", 15,  4, check_fonts_core_present),
    ("Chrome version in range",     "warning",    5,  1, check_chrome_version_in_range),
    ("Timezone plausibility",       "warning",    3,  1, check_timezone_plausibility),
    ("Language plausibility",       "warning",    3,  1, check_language_plausibility),
    ("Audio sample rate",           "warning",    4,  1, check_audio_sample_rate),
]


def validate(fp: dict, template: dict) -> dict:
    """Run every check against fingerprint dict. Return full report."""
    results = []
    total_penalty = 0
    max_possible_penalty = 0
    critical_fails = []
    warnings = []

    for name, category, w_fail, w_warn, fn in CHECKS:
        max_possible_penalty += w_fail   # worst-case scoring baseline

        try:
            result = fn(fp, template)
        except Exception as e:
            result = ("warn", f"check error: {type(e).__name__}: {e}")

        if result is None:
            # Field missing — check skipped. We don't penalize for missing
            # data: a runtime scan might be partial. But note it.
            results.append({
                "name": name, "category": category,
                "status": "skip",
                "detail": "field not present in fingerprint",
            })
            continue

        status, detail = result
        entry = {
            "name": name, "category": category,
            "status": status, "detail": detail,
        }
        results.append(entry)

        if status == "fail":
            total_penalty += w_fail
            if category == "critical":
                critical_fails.append(f"{name}: {detail}")
            else:
                warnings.append(f"{name}: {detail}")
        elif status == "warn":
            total_penalty += w_warn
            warnings.append(f"{name}: {detail}")

    # Score — bounded 0-100, scaled against max possible penalty
    # We clamp because max_possible is theoretical worst, almost never hit
    # Use a softer denominator so that hitting all warnings ≈ 80 score
    denom = max_possible_penalty * 0.5
    score = max(0, min(100, round(100 - (total_penalty / denom * 100)
                                   if denom else 100)))

    # Grade mapping — user-friendly labels
    if score >= 90:
        grade = "excellent"
    elif score >= 75:
        grade = "good"
    elif score >= 55:
        grade = "warning"
    else:
        grade = "critical"

    summary = (
        f"{score}/100 — "
        f"{len(critical_fails)} critical, "
        f"{len(warnings) - len(critical_fails) if len(warnings) > len(critical_fails) else len(warnings)} warnings"
    )

    return {
        "score":     score,
        "grade":     grade,
        "summary":   summary,
        "checks":    results,
        "critical":  critical_fails,
        "warnings":  warnings,
        "template_id":   template.get("id"),
        "template_label": template.get("label"),
    }


# ═══════════════════════════════════════════════════════════════
# Cross-validation: configured fingerprint vs actual runtime
# ═══════════════════════════════════════════════════════════════
# Separate concern: does what Chrome actually reports match what we
# configured? Reveals stealth-patch failures (webdriver exposed,
# platform mismatched, UA not taking effect). Used after the runtime
# tester runs a live browser.

def compare_configured_vs_actual(configured: dict, actual: dict) -> dict:
    """Side-by-side diff. Returns list of mismatches with severity.

    configured: fingerprint we wanted Chrome to report
    actual: fingerprint Chrome actually reports at runtime
    """
    mismatches = []
    checks = [
        # (label, configured_path, actual_path, severity)
        ("User Agent",       ["user_agent"],        ["navigator", "userAgent"],  "critical"),
        ("Platform",         ["platform"],          ["navigator", "platform"],    "critical"),
        ("Screen width",     ["screen", "width"],   ["screen", "width"],          "critical"),
        ("Screen height",    ["screen", "height"],  ["screen", "height"],         "critical"),
        ("Hardware cores",   ["hardware_concurrency"],
                              ["navigator", "hardwareConcurrency"], "important"),
        ("Device memory",    ["device_memory"],     ["navigator", "deviceMemory"], "warning"),
        ("Max touch points", ["max_touch_points"],  ["navigator", "maxTouchPoints"], "important"),
        ("Timezone",         ["timezone"],          ["timezone", "intl"],         "important"),
        ("Language",         ["language"],          ["navigator", "language"],     "important"),
        ("Vendor",           ["vendor"],            ["navigator", "vendor"],       "important"),
        ("WebGL vendor",     ["webgl", "vendor"],   ["webgl", "vendor"],          "critical"),
        ("WebGL renderer",   ["webgl", "renderer"], ["webgl", "renderer"],        "critical"),
    ]

    for label, c_path, a_path, severity in checks:
        c_val = _get(configured, *c_path)
        a_val = _get(actual, *a_path)
        if c_val is None and a_val is None:
            continue
        if c_val == a_val:
            continue
        mismatches.append({
            "field": label,
            "configured": c_val,
            "actual": a_val,
            "severity": severity,
        })

    # Sort critical first
    severity_rank = {"critical": 0, "important": 1, "warning": 2}
    mismatches.sort(key=lambda m: severity_rank.get(m["severity"], 3))

    return {
        "total_mismatches": len(mismatches),
        "critical_mismatches": sum(1 for m in mismatches if m["severity"] == "critical"),
        "mismatches": mismatches,
    }
