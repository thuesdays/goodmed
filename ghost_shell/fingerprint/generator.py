"""
fingerprint_generator.py — Generate coherent fingerprints from
device templates.

Input:
    - profile_name (for deterministic seeding, or None for random)
    - template_id (optional — if None, weighted-random pick)
    - locked_fields (optional — dict of {path: value} to preserve)

Output: full fingerprint dict ready to feed into Chrome launch params.
Guaranteed to score 95+ on validator (if template data is correct).

Design: sampling is deterministic given (profile_name + template_id).
This means the SAME profile ALWAYS generates the SAME fingerprint —
important because Google fingerprint-tracks profiles across sessions.
If a profile's fingerprint changed randomly every run, it would
instantly look like a bot rotating identities.

Locked fields override sampled values. Use case: user on profile page
wants to keep Timezone=Europe/Kyiv after regeneration → lock it, all
other fields regenerate coherently around it.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import hashlib
import random
import re

from ghost_shell.fingerprint.templates import (
    DEVICE_TEMPLATES,
    get_template,
    weighted_pick_template,
)


def _seeded_random(seed: str) -> random.Random:
    """Deterministic RNG from a string seed."""
    h = hashlib.sha256(seed.encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def _pick(rng, options):
    """rng.choice wrapper that accepts single value as fallback."""
    if isinstance(options, (list, tuple)):
        return rng.choice(options)
    return options


def _apply_locks(fp: dict, locks: dict) -> dict:
    """Overlay locked field values onto a generated fingerprint.

    `locks` uses dot-notation paths: {"navigator.platform": "Win32",
    "timezone.intl": "Europe/Kyiv"}. Missing intermediate keys are
    created as empty dicts.
    """
    if not locks:
        return fp
    for path, value in locks.items():
        keys = path.split(".")
        cur = fp
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value
    return fp


def generate(profile_name: str = None,
             template_id: str = None,
             locked_fields: dict = None) -> dict:
    """Main entry point. Returns a full coherent fingerprint."""
    # Seed — deterministic if profile_name given
    seed = profile_name or "random"
    rng = _seeded_random(seed + "::" + (template_id or "auto"))

    # Pick template — explicit id, or weighted random
    if template_id:
        template = get_template(template_id)
        if not template:
            raise ValueError(f"Unknown template_id: {template_id}")
    else:
        template = weighted_pick_template(seed=seed)

    # Sample Chrome version — weighted toward the top of the range
    # (stable channel is newest). Use triangular distribution to bias.
    v_low, v_high = template["chrome_version_range"]
    chrome_major = int(rng.triangular(v_low, v_high, v_high))
    chrome_build = rng.randint(7100, 7300)
    chrome_patch = rng.randint(30, 120)
    chrome_version_str = f"{chrome_major}.0.{chrome_build}.{chrome_patch}"

    # Sample screen — pick one of the template's options
    screen_opt = rng.choice(template["screen_options"])
    screen = {
        "width":       screen_opt["width"],
        "height":      screen_opt["height"],
        "availWidth":  screen_opt["width"],
        "availHeight": screen_opt["height"] - screen_opt["avail_delta_h"],
        "colorDepth":  24,
        "pixelDepth":  24,
    }
    dpr = screen_opt["dpr"]

    # Viewport — fraction of screen width typical for windowed browser
    viewport_ratio = template["viewport_ratio"]
    inner_w = int(screen["width"] * viewport_ratio)
    inner_h = int(screen["height"] * viewport_ratio * 0.85)

    # Build user-agent string. Chrome formula:
    #   Mozilla/5.0 (<platform>) AppleWebKit/537.36 (KHTML, like Gecko)
    #     Chrome/<version> Safari/537.36
    # Mobile Chrome appends " Mobile" to the Safari token; desktop
    # omits it. Ad-intel sites look for this suffix to decide mobile
    # vs desktop even when UA-CH says otherwise.
    is_mobile = bool(template.get("is_mobile"))
    mobile_marker = "Mobile " if is_mobile else ""
    ua = (f"Mozilla/5.0 ({template['ua_platform_token']}) "
          f"AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{chrome_version_str} {mobile_marker}Safari/537.36")

    # GPU — pick one of the renderer templates
    renderer_tmpl = rng.choice(template["gpu"]["renderer_templates"])
    # Replace any {:04X} with a plausible device id
    if "{:04X}" in renderer_tmpl:
        renderer = renderer_tmpl.format(rng.randint(0x4000, 0xFFFF))
    else:
        renderer = renderer_tmpl

    # Hardware
    hw_concurrency = _pick(rng, template["hardware_concurrency_options"])
    device_memory = _pick(rng, template["device_memory_options"])

    # Language — pick from plausible for template; primary + ordered list
    lang_primary = _pick(rng, template["languages_common"])
    # Build navigator.languages — primary first, then 1-2 fallbacks
    other_langs = [l for l in template["languages_common"] if l != lang_primary]
    rng.shuffle(other_langs)
    languages = [lang_primary] + other_langs[:rng.randint(1, 2)]

    # Timezone
    timezone = _pick(rng, template["timezone_cities_common"])

    # Fonts — all core + random subset of optional
    fonts = list(template["fonts_core"])
    optional = list(template["fonts_optional"])
    rng.shuffle(optional)
    fraction = rng.uniform(0.4, 0.8)
    fonts.extend(optional[:int(len(optional) * fraction)])
    fonts.sort()

    # Build full fingerprint dict
    fp = {
        # Metadata
        "template_id":       template["id"],
        "template_label":    template["label"],
        "generated_for":     profile_name,
        "schema_version":    "v1",

        # Browser identity
        "user_agent":          ua,
        "platform":            template["navigator_platform"],
        "vendor":              "Google Inc.",
        "chrome_major":        chrome_major,
        "chrome_full_version": chrome_version_str,

        # navigator.* values (the flat "configured" form for Chrome)
        "hardware_concurrency": hw_concurrency,
        "device_memory":        device_memory,
        "max_touch_points":     template["max_touch_points"],
        "language":             lang_primary,
        "languages":            languages,
        "webdriver":            False,   # always false by policy

        # Screen / window
        "screen":               screen,
        "dpr":                  dpr,
        "viewport_hint":        {"width": inner_w, "height": inner_h},

        # Timezone + locale
        "timezone":             timezone,
        "prefers_color_scheme": template.get("prefers_color_scheme", "light"),
        "color_gamut":          template.get("color_gamut", "srgb"),

        # WebGL / GPU
        "webgl": {
            "vendor":     template["gpu"]["vendor"],
            "renderer":   renderer,
            "extensions": template["webgl_extensions_typical"],
        },

        # Device category flags — consumed by the browser runtime to
        # enable CDP touch emulation + mobile viewport overrides.
        "is_mobile":  is_mobile,
        "category":   template.get("category", "desktop"),

        # Audio
        "audio_sample_rate": template["audio_sample_rate"],

        # Fonts
        "fonts": fonts,

        # UA-CH (Client Hints) — mirrors UA but in modern form
        "ua_client_hints": _build_ua_ch(template, chrome_major,
                                         chrome_version_str),
    }

    # Apply locked fields last — user-chosen values override sampled
    fp = _apply_locks(fp, locked_fields or {})

    return fp


def _build_ua_ch(template: dict, major: int, full_version: str) -> dict:
    """Build navigator.userAgentData.getHighEntropyValues() response
    matching the UA string. These must be coherent or detection
    systems will flag the mismatch."""
    os_map = {
        "Windows":     ("Windows",  "15.0.0"),
        "macOS":       ("macOS",    "14.3.1"),
        "Linux":       ("Linux",    ""),
        "Android":     ("Android",  "14.0.0"),
    }
    os_name, os_version = os_map.get(template["os"], ("Unknown", ""))
    is_mobile = bool(template.get("is_mobile"))

    # Platform arch/bitness differs for mobile. Android phones are
    # ARM 64-bit since API 21 (5.0) basically universally; leaving
    # "x86/64" there is the #1 UA-CH red flag for detection suites.
    if is_mobile or template["os"] == "Android":
        arch, bits = "arm", "64"
    else:
        arch, bits = "x86", "64"

    # model — only populated for mobile (iPhone/Pixel/SM-Sxxx).
    # For desktop Chrome leaves it empty.
    model = ""
    if is_mobile:
        tok = template.get("ua_platform_token", "")
        # "Linux; Android 14; Pixel 8" → "Pixel 8"
        parts = [p.strip() for p in tok.split(";")]
        if len(parts) >= 3:
            model = parts[-1]

    return {
        "brands": [
            {"brand": "Not)A;Brand",         "version": "99"},
            {"brand": "Google Chrome",       "version": str(major)},
            {"brand": "Chromium",            "version": str(major)},
        ],
        "fullVersionList": [
            {"brand": "Not)A;Brand",         "version": "99.0.0.0"},
            {"brand": "Google Chrome",       "version": full_version},
            {"brand": "Chromium",            "version": full_version},
        ],
        "mobile":          is_mobile or template["category"] in ("phone", "tablet"),
        "platform":        os_name,
        "platformVersion": os_version,
        "architecture":    arch,
        "bitness":         bits,
        "model":           model,
        "wow64":           False,
    }


def regenerate_preserving_locks(current_fp: dict,
                                locked_paths: list,
                                new_template_id: str = None) -> dict:
    """Regenerate a fingerprint keeping specific fields locked.

    current_fp: the existing fingerprint dict
    locked_paths: ["timezone", "language", ...] — dot-paths to preserve
    new_template_id: if set, switch to this template; else keep current

    Returns a fresh fingerprint with the locked values carried over.
    """
    if not locked_paths:
        locked_paths = []

    # Extract locked values from current fingerprint
    locks = {}
    for path in locked_paths:
        keys = path.split(".")
        cur = current_fp
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                cur = None
                break
        if cur is not None:
            locks[path] = cur

    template_id = new_template_id or current_fp.get("template_id")
    profile_name = current_fp.get("generated_for")

    # Force RNG to give DIFFERENT result on regenerate — append timestamp
    import time
    regen_seed = f"{profile_name}::regen::{int(time.time())}"
    return generate(
        profile_name=regen_seed,
        template_id=template_id,
        locked_fields=locks,
    )
