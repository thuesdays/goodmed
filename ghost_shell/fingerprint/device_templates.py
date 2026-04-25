"""
NK Browser Core - Device Templates & Stealth Payload Builder (v3)
-------------------------------------------------------------------
Deterministic payload for the C++ core of Ghost Shell Chromium.

Covers all detection vectors (2026):
- Hardware (CPU, RAM, platform)
- Screen (dimensions, DPR, color depth, outer, screen_x/y, orientation)
- Graphics (WebGL vendor/renderer + WebGPU vendor/arch)
- Audio (sample rate, base latency)
- Fonts (full Windows set, a randomized subset)
- Navigator (UA, UA-CH, languages, plugins/mimeTypes, battery)
- Timezone (for V8 Intl override)
- Network connection (effective type, downlink, rtt)
- WebRTC (media devices -- full set with default + communications)
- Noise seeds (canvas shift, audio offset, clientrect offset)
- UserAgentMetadata (Sec-CH-UA-* headers)

Determinism: SHA256(profile_name) -> seed -> one profile = one fingerprint.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import base64
import random
import logging
import hashlib
from typing import Dict, Any, List, Optional

logger = logging.getLogger("DeviceTemplates")
logger.setLevel(logging.DEBUG)
# No own handler: records propagate to the root logger (configured in main.py
# via basicConfig). Adding a local StreamHandler here caused every message
# to be printed twice.


# =============================================================================
# CONSTANT POOLS -- values current as of Q2 2026
# =============================================================================

# The Chromium source we're building from. This is the actual engine —
# JS APIs available, WebGL/WebGPU feature set, etc. Shown in the
# dashboard header badge so the user knows which Chromium they compiled.
CHROMIUM_BUILD         = "149"
CHROMIUM_BUILD_FULL    = "149.0.7805.0"

# Chrome version pool we SPOOF to the outside world. These don't match
# CHROMIUM_BUILD — real Chrome stable lags ~2 major versions behind the
# bleeding-edge Chromium source. A bot detector comparing UA (147) vs
# engine features (149-specific) won't care because the delta is small
# and well within the normal propagation window for stable rollouts.
#
# Distribution as of April 22, 2026:
#   ~55% on current stable    (147)
#   ~25% on previous stable   (146)
#   ~12% on older stable      (145)
#   ~5%  on older             (144)
#   ~3%  on extended stable   (143)
CHROME_VERSIONS = [
    {"major": "147", "full": "147.0.7780.88",  "weight": 55},  # current stable (Apr 7, 2026)
    {"major": "146", "full": "146.0.7715.130", "weight": 25},  # prev stable (Mar 2026)
    {"major": "145", "full": "145.0.7665.162", "weight": 12},  # older (Feb 2026)
    {"major": "144", "full": "144.0.7615.185", "weight": 5},   # Jan 2026
    {"major": "143", "full": "143.0.7556.210", "weight": 3},   # Extended stable / enterprise laggers
]


def pick_chrome_version(rnd, bounds: tuple = None):
    """Weighted random pick from CHROME_VERSIONS — newer = more common.

    Args:
        rnd:    random.Random instance (seeded for deterministic fingerprints).
        bounds: optional (min_major, max_major) tuple. Versions outside
                this range are excluded from the pool. Both ends are
                inclusive; None means no bound on that side.

    Falls back to the full pool if bounds would filter everything out.
    """
    pool = CHROME_VERSIONS
    if bounds:
        lo, hi = bounds
        def _in_range(v):
            major = int(v["major"])
            if lo is not None and major < int(lo): return False
            if hi is not None and major > int(hi): return False
            return True
        filtered = [v for v in CHROME_VERSIONS if _in_range(v)]
        if filtered:
            pool = filtered
        # else: bounds exclude everything → silently fall back to full pool

    total = sum(v["weight"] for v in pool)
    pick = rnd.randint(1, total)
    running = 0
    for v in pool:
        running += v["weight"]
        if pick <= running:
            return v
    return pool[0]

PLATFORMS = [
    {
        "navigator_platform":  "Win32",
        "ch_platform":         "Windows",
        "ch_platform_version": "15.0.0",
        "ua_os_portion":       "Windows NT 10.0; Win64; x64",
        "ch_arch":             "x86",
        "ch_bitness":          "64",
        "ch_wow64":            False,
        "ch_model":            "",
    },
]

LANGUAGE_PROFILES = [
    {
        "navigator_languages":  ["uk-UA", "uk", "ru", "en-US", "en"],
        "navigator_language":   "uk-UA",
        "accept_language":      "uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6",
        "weight":               5,
    },
    {
        "navigator_languages":  ["ru-RU", "ru", "uk", "en-US", "en"],
        "navigator_language":   "ru-RU",
        "accept_language":      "ru-RU,ru;q=0.9,uk;q=0.8,en-US;q=0.7,en;q=0.6",
        "weight":               3,
    },
    {
        "navigator_languages":  ["en-US", "en", "uk", "ru"],
        "navigator_language":   "en-US",
        "accept_language":      "en-US,en;q=0.9,uk;q=0.8,ru;q=0.7",
        "weight":               2,
    },
]

DEVICE_TEMPLATES = [
    # ─── Mainstream desktop / laptop (original 6) ───
    {
        "name":    "office_desktop_intel",
        "cpu":     {"concurrency": 8, "memory": 8.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (Intel)",
            "gl_renderer":   "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "intel",
            "webgpu_arch":   "xe",
            "tier":          "integrated_modern",
        },
        "screen":  {"width": 1920, "height": 1080, "taskbar": 48, "dpr": 1.0},
        "battery": None,
        "weight":  5,
    },
    {
        "name":    "office_laptop_intel",
        "cpu":     {"concurrency": 8, "memory": 16.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (Intel)",
            "gl_renderer":   "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "intel",
            "webgpu_arch":   "xe",
            "tier":          "integrated_modern",
        },
        "screen":  {"width": 1920, "height": 1080, "taskbar": 48, "dpr": 1.0},
        "battery": {"charging": True, "level": None},
        "weight":  6,
    },
    {
        "name":    "gaming_nvidia_mid",
        "cpu":     {"concurrency": 12, "memory": 16.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (NVIDIA)",
            "gl_renderer":   "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "nvidia",
            "webgpu_arch":   "ada-lovelace",
            "tier":          "discrete_mid",
        },
        "screen":  {"width": 1920, "height": 1080, "taskbar": 48, "dpr": 1.0},
        "battery": None,
        "weight":  3,
    },
    {
        "name":    "gaming_nvidia_high",
        "cpu":     {"concurrency": 16, "memory": 32.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (NVIDIA)",
            "gl_renderer":   "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "nvidia",
            "webgpu_arch":   "ada-lovelace",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 2560, "height": 1440, "taskbar": 48, "dpr": 1.0},
        "battery": None,
        "weight":  2,
    },
    {
        "name":    "amd_desktop_mid",
        "cpu":     {"concurrency": 12, "memory": 16.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (AMD)",
            "gl_renderer":   "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "amd",
            "webgpu_arch":   "rdna-2",
            "tier":          "discrete_mid",
        },
        "screen":  {"width": 1920, "height": 1080, "taskbar": 48, "dpr": 1.0},
        "battery": None,
        "weight":  2,
    },
    {
        "name":    "budget_laptop",
        "cpu":     {"concurrency": 4, "memory": 8.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (Intel)",
            "gl_renderer":   "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "intel",
            "webgpu_arch":   "gen11",
            "tier":          "integrated_old",
        },
        "screen":  {"width": 1366, "height": 768, "taskbar": 40, "dpr": 1.0},
        "battery": {"charging": False, "level": None},
        "weight":  3,
    },

    # ─── High-end / gaming / workstation (added 2026-04) ───
    # Fills the gap where the catalog had nothing above 32GB RAM /
    # RTX 4070. Modern PC reality is 32-64GB DDR5 with RTX 40-series.
    {
        "name":    "gaming_nvidia_4070_super",
        "cpu":     {"concurrency": 20, "memory": 32.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (NVIDIA)",
            "gl_renderer":   "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "nvidia",
            "webgpu_arch":   "ada-lovelace",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 2560, "height": 1440, "taskbar": 48, "dpr": 1.0},
        "battery": None,
        "weight":  3,
    },
    {
        "name":    "gaming_nvidia_4080_super",
        "cpu":     {"concurrency": 24, "memory": 64.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (NVIDIA)",
            "gl_renderer":   "ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "nvidia",
            "webgpu_arch":   "ada-lovelace",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 2560, "height": 1440, "taskbar": 48, "dpr": 1.0},
        "battery": None,
        "weight":  2,
    },
    {
        "name":    "enthusiast_nvidia_4090_4k",
        "cpu":     {"concurrency": 32, "memory": 64.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (NVIDIA)",
            "gl_renderer":   "ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "nvidia",
            "webgpu_arch":   "ada-lovelace",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 3840, "height": 2160, "taskbar": 48, "dpr": 1.5},
        "battery": None,
        "weight":  1,
    },
    {
        "name":    "workstation_threadripper_a4000",
        "cpu":     {"concurrency": 48, "memory": 128.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (NVIDIA)",
            "gl_renderer":   "ANGLE (NVIDIA, NVIDIA RTX A4000 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "nvidia",
            "webgpu_arch":   "ampere",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 3840, "height": 2160, "taskbar": 48, "dpr": 1.5},
        "battery": None,
        "weight":  1,
    },
    {
        "name":    "amd_gaming_7900xt_2k",
        "cpu":     {"concurrency": 24, "memory": 32.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (AMD)",
            "gl_renderer":   "ANGLE (AMD, AMD Radeon RX 7900 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "amd",
            "webgpu_arch":   "rdna-3",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 2560, "height": 1440, "taskbar": 48, "dpr": 1.0},
        "battery": None,
        "weight":  2,
    },
    {
        "name":    "gaming_laptop_rtx4060_oled",
        "cpu":     {"concurrency": 16, "memory": 32.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (NVIDIA)",
            "gl_renderer":   "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "nvidia",
            "webgpu_arch":   "ada-lovelace",
            "tier":          "discrete_mid",
        },
        "screen":  {"width": 2880, "height": 1800, "taskbar": 48, "dpr": 1.5},
        "battery": {"charging": True, "level": None},
        "weight":  2,
    },
    {
        "name":    "ultrabook_4k_32gb",
        "cpu":     {"concurrency": 16, "memory": 32.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (Intel)",
            "gl_renderer":   "ANGLE (Intel, Intel(R) Arc(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "webgpu_vendor": "intel",
            "webgpu_arch":   "xe",
            "tier":          "integrated_modern",
        },
        "screen":  {"width": 3840, "height": 2400, "taskbar": 48, "dpr": 2.0},
        "battery": {"charging": True, "level": None},
        "weight":  2,
    },
    {
        "name":    "macbook_pro_16_m3_max",
        "cpu":     {"concurrency": 16, "memory": 64.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (Apple)",
            "gl_renderer":   "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Max, Unspecified Version)",
            "webgpu_vendor": "apple",
            "webgpu_arch":   "apple-9",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 1728, "height": 1117, "taskbar": 25, "dpr": 2.0},
        "battery": {"charging": True, "level": None},
        "weight":  2,
    },
    {
        "name":    "mac_studio_m2_ultra",
        "cpu":     {"concurrency": 24, "memory": 64.0},
        "gpu": {
            "gl_vendor":     "Google Inc. (Apple)",
            "gl_renderer":   "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Ultra, Unspecified Version)",
            "webgpu_vendor": "apple",
            "webgpu_arch":   "apple-8",
            "tier":          "discrete_high",
        },
        "screen":  {"width": 3008, "height": 1692, "taskbar": 25, "dpr": 2.0},
        "battery": None,
        "weight":  1,
    },
]

# ──────────────────────────────────────────────────────────────
# CODEC SUPPORT MATRIX — per GPU tier
#
# navigator.mediaCapabilities.decodingInfo() returns a triple
# {supported, smooth, powerEfficient} for each queried codec. Real
# hardware varies meaningfully here:
#
#   - Integrated GPUs from ~2018 (UHD Graphics) have no AV1 hwaccel —
#     they decode AV1 in software, so powerEfficient=false even though
#     supported=true.
#   - Modern Intel Arc / Iris Xe added AV1 HW decode (Gen12+).
#   - NVIDIA RTX 30/40 series have full HW decode for AV1/VP9/H264/H265.
#   - Budget GPUs of UHD Graphics era don't hwaccel VP9 Profile 2 (HDR).
#
# Detectors (creepjs, iphey) fingerprint the EXACT combination of these
# flags. All Ghost Shell profiles reporting the same matrix is a tell.
# Keying by GPU tier makes a profile's media-capabilities consistent
# with its WebGL renderer string.
# ──────────────────────────────────────────────────────────────
CODEC_MATRIX_BY_TIER: Dict[str, Dict[str, Dict[str, bool]]] = {
    # Old integrated (UHD Graphics 620/630 class) — no AV1 HW, VP9 soft
    "integrated_old": {
        "av1":  {"supported": True,  "smooth": False, "power_efficient": False},
        "vp9":  {"supported": True,  "smooth": True,  "power_efficient": False},
        "h264": {"supported": True,  "smooth": True,  "power_efficient": True},
        "h265": {"supported": False, "smooth": False, "power_efficient": False},
    },
    # Modern integrated (Iris Xe / UHD 770 / Arc A-series) — AV1 HW,
    # H265 partial support
    "integrated_modern": {
        "av1":  {"supported": True,  "smooth": True,  "power_efficient": True},
        "vp9":  {"supported": True,  "smooth": True,  "power_efficient": True},
        "h264": {"supported": True,  "smooth": True,  "power_efficient": True},
        "h265": {"supported": True,  "smooth": True,  "power_efficient": False},
    },
    # Discrete mid-range (RTX 30/40 60-class, RX 6600) — full HW, partial
    # power-efficiency advantages on lower codecs
    "discrete_mid": {
        "av1":  {"supported": True,  "smooth": True,  "power_efficient": True},
        "vp9":  {"supported": True,  "smooth": True,  "power_efficient": True},
        "h264": {"supported": True,  "smooth": True,  "power_efficient": True},
        "h265": {"supported": True,  "smooth": True,  "power_efficient": True},
    },
    # Discrete high (RTX 70/80/90-class) — everything full HW
    "discrete_high": {
        "av1":  {"supported": True,  "smooth": True,  "power_efficient": True},
        "vp9":  {"supported": True,  "smooth": True,  "power_efficient": True},
        "h264": {"supported": True,  "smooth": True,  "power_efficient": True},
        "h265": {"supported": True,  "smooth": True,  "power_efficient": True},
    },
}

# Windows 10/11 fonts -- the CORE set is always present
WINDOWS_FONTS_CORE = [
    "Arial", "Arial Black", "Calibri", "Cambria", "Cambria Math",
    "Candara", "Comic Sans MS", "Consolas", "Constantia", "Corbel",
    "Courier New", "Franklin Gothic Medium", "Georgia", "Impact",
    "Lucida Console", "Lucida Sans Unicode", "Microsoft Sans Serif",
    "Palatino Linotype", "Segoe UI", "Segoe UI Symbol", "Segoe UI Emoji",
    "Symbol", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana",
    "Webdings", "Wingdings",
]

WINDOWS_FONTS_EXTENDED = [
    "Bahnschrift", "Ebrima", "Gabriola", "Gadugi", "HoloLens MDL2 Assets",
    "Ink Free", "Javanese Text", "Leelawadee UI", "Malgun Gothic", "Marlett",
    "Microsoft Himalaya", "Microsoft JhengHei", "Microsoft New Tai Lue",
    "Microsoft PhagsPa", "Microsoft Tai Le", "Microsoft YaHei",
    "Microsoft Yi Baiti", "MingLiU-ExtB", "Mongolian Baiti", "MS Gothic",
    "MV Boli", "Myanmar Text", "Nirmala UI", "Segoe MDL2 Assets",
    "Segoe Print", "Segoe Script", "Segoe UI Historic", "SimSun", "Sitka",
    "Sylfaen", "Yu Gothic",
]

TIMEZONE_PROFILES = [
    {"id": "Europe/Kyiv", "offset_min": -180, "offset_str": "+03:00"},
]

# Standard PDF plugins, same on every Chrome 90+
STANDARD_CHROME_PLUGINS = [
    {
        "name":        "PDF Viewer",
        "description": "Portable Document Format",
        "filename":    "internal-pdf-viewer",
        "mime_types": [{"type": "application/pdf", "suffixes": "pdf"},
                       {"type": "text/pdf",        "suffixes": "pdf"}],
    },
    {
        "name":        "Chrome PDF Viewer",
        "description": "Portable Document Format",
        "filename":    "internal-pdf-viewer",
        "mime_types": [{"type": "application/pdf", "suffixes": "pdf"},
                       {"type": "text/pdf",        "suffixes": "pdf"}],
    },
    {
        "name":        "Chromium PDF Viewer",
        "description": "Portable Document Format",
        "filename":    "internal-pdf-viewer",
        "mime_types": [{"type": "application/pdf", "suffixes": "pdf"},
                       {"type": "text/pdf",        "suffixes": "pdf"}],
    },
    {
        "name":        "Microsoft Edge PDF Viewer",
        "description": "Portable Document Format",
        "filename":    "internal-pdf-viewer",
        "mime_types": [{"type": "application/pdf", "suffixes": "pdf"},
                       {"type": "text/pdf",        "suffixes": "pdf"}],
    },
    {
        "name":        "WebKit built-in PDF",
        "description": "Portable Document Format",
        "filename":    "internal-pdf-viewer",
        "mime_types": [{"type": "application/pdf", "suffixes": "pdf"},
                       {"type": "text/pdf",        "suffixes": "pdf"}],
    },
]

CONNECTION_PROFILES = [
    {"effective_type": "4g", "downlink": 10.0, "rtt": 50,  "save_data": False, "type": "wifi"},
    {"effective_type": "4g", "downlink": 7.5,  "rtt": 75,  "save_data": False, "type": "wifi"},
    {"effective_type": "4g", "downlink": 15.0, "rtt": 30,  "save_data": False, "type": "wifi"},
    {"effective_type": "4g", "downlink": 5.0,  "rtt": 100, "save_data": False, "type": "ethernet"},
]


def _weighted_choice(rnd: random.Random, items: list) -> dict:
    weights = [item.get("weight", 1) for item in items]
    return rnd.choices(items, weights=weights, k=1)[0]


# =============================================================================
# BUILDER
# =============================================================================

class DeviceTemplateBuilder:
    # 3.0.0 — original fingerprint schema
    # 3.1.0 — Feature #5/#8: extended noise (canvas/webgl/audio), per-tier
    #         codec matrix, unified GPU field (WebGL + WebGPU consistency)
    VERSION = "3.1.0"

    def __init__(self, profile_name: str, preferred_language: str = None,
                 force_template: str = None):
        self.profile_name = str(profile_name)
        self.preferred_language = preferred_language

        # Deterministic RNG derived from profile name
        seed_string = f"ghost_shell_v3_{self.profile_name}"
        seed_int = int(hashlib.sha256(seed_string.encode('utf-8')).hexdigest(), 16)
        self.rnd = random.Random(seed_int)

        # Template: either fixed by name (dashboard create form) or weighted-random
        if force_template:
            match = next((t for t in DEVICE_TEMPLATES
                          if t["name"] == force_template), None)
            self.template = match or _weighted_choice(self.rnd, DEVICE_TEMPLATES)
        else:
            self.template = _weighted_choice(self.rnd, DEVICE_TEMPLATES)

        self.platform = self.rnd.choice(PLATFORMS)

        # Chrome version: respect user-configured min/max bounds from
        # Settings → UA spoof range. Falls through gracefully if DB is
        # not reachable (e.g. unit-testing the builder standalone).
        chrome_bounds = None
        try:
            from ghost_shell.db.database import get_db
            _db = get_db()
            lo = _db.config_get("browser.spoof_chrome_min")
            hi = _db.config_get("browser.spoof_chrome_max")
            if lo or hi:
                chrome_bounds = (lo, hi)
        except Exception:
            pass
        self.chrome_v = pick_chrome_version(self.rnd, bounds=chrome_bounds)

        self.timezone = self.rnd.choice(TIMEZONE_PROFILES)

        if preferred_language:
            matching = [lp for lp in LANGUAGE_PROFILES if lp["navigator_language"] == preferred_language]
            self.lang = matching[0] if matching else _weighted_choice(self.rnd, LANGUAGE_PROFILES)
        else:
            self.lang = _weighted_choice(self.rnd, LANGUAGE_PROFILES)

        logger.debug(
            f"Template={self.template['name']} Chrome={self.chrome_v['full']} "
            f"Lang={self.lang['navigator_language']}"
        )

    def _device_id(self, prefix: str) -> str:
        raw = f"{self.profile_name}_{prefix}_{self.rnd.random()}"
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def _build_user_agent(self) -> str:
        return (
            f"Mozilla/5.0 ({self.platform['ua_os_portion']}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{self.chrome_v['full']} Safari/537.36"
        )

    # ──────────────────────────────────────────────────────────

    def _build_hardware(self) -> Dict[str, Any]:
        # W3C spec: navigator.deviceMemory is clamped to one of
        # {0.25, 0.5, 1, 2, 4, 8} — max 8 GB for privacy.
        # Pre-clamp here so the payload matches what the browser
        # will expose (even without our C++ clamp in the getter).
        raw_mem = self.template["cpu"]["memory"]
        if   raw_mem >= 8:   clamped_mem = 8
        elif raw_mem >= 4:   clamped_mem = 4
        elif raw_mem >= 2:   clamped_mem = 2
        elif raw_mem >= 1:   clamped_mem = 1
        elif raw_mem >= 0.5: clamped_mem = 0.5
        else:                clamped_mem = 0.25

        return {
            "user_agent":           self._build_user_agent(),
            "platform":             self.platform["navigator_platform"],
            "hardware_concurrency": self.template["cpu"]["concurrency"],
            "device_memory":        clamped_mem,
            "device_memory_raw":    raw_mem,   # for display / other logic
            "max_touch_points":     0,
            "do_not_track":         None,
            "pdf_viewer_enabled":   True,
        }

    def _build_languages(self) -> Dict[str, Any]:
        return {
            "language":         self.lang["navigator_language"],
            "languages":        self.lang["navigator_languages"],
            "accept_language":  self.lang["accept_language"],
        }

    def _build_screen(self) -> Dict[str, Any]:
        s = self.template["screen"]
        avail_height = s["height"] - s["taskbar"]
        return {
            "width":             s["width"],
            "height":            s["height"],
            "avail_width":       s["width"],
            "avail_height":      avail_height,
            "color_depth":       24,
            "pixel_depth":       24,
            "pixel_ratio":       s["dpr"],
            "outer_width":       s["width"],
            "outer_height":      avail_height,
            "screen_x":          self.rnd.randint(0, 50),
            "screen_y":          self.rnd.randint(0, 30),
            "orientation":       "landscape-primary",
            "orientation_angle": 0,
        }

    def _build_graphics(self) -> Dict[str, Any]:
        return {
            "gl_vendor":      self.template["gpu"]["gl_vendor"],
            "gl_renderer":    self.template["gpu"]["gl_renderer"],
            "webgpu_vendor":  self.template["gpu"]["webgpu_vendor"],
            "webgpu_arch":    self.template["gpu"]["webgpu_arch"],
            "webgpu_device":  "",
            "webgl_extensions": [
                "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
                "EXT_disjoint_timer_query", "EXT_float_blend", "EXT_frag_depth",
                "EXT_shader_texture_lod", "EXT_texture_compression_bptc",
                "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic",
                "EXT_sRGB", "OES_element_index_uint", "OES_fbo_render_mipmap",
                "OES_standard_derivatives", "OES_texture_float", "OES_texture_float_linear",
                "OES_texture_half_float", "OES_texture_half_float_linear",
                "OES_vertex_array_object", "WEBGL_color_buffer_float",
                "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb",
                "WEBGL_debug_renderer_info", "WEBGL_debug_shaders",
                "WEBGL_depth_texture", "WEBGL_draw_buffers", "WEBGL_lose_context",
                "WEBGL_multi_draw",
            ],
        }

    def _build_gpu(self) -> Dict[str, Any]:
        """
        Top-level 'gpu' dict consumed by the C++ patches for:
          - gl.getParameter(UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL)
          - navigator.gpu.requestAdapter().requestAdapterInfo() — the new
            WebGPU fingerprint vector added in Chrome 113

        IMPORTANT: the strings here MUST match what _build_graphics writes
        to gl_vendor/gl_renderer. Real hardware is identical across WebGL
        and WebGPU queries; any mismatch is an instant detector signal.
        We derive both from the same template data to guarantee coherence.
        """
        return {
            "unmasked_vendor":   self.template["gpu"]["gl_vendor"],
            "unmasked_renderer": self.template["gpu"]["gl_renderer"],
            # Ghost Shell tier tag, not surfaced to JS but useful in logs
            # and for the codec matrix lookup below.
            "tier":              self.template["gpu"].get("tier", "integrated_modern"),
        }

    def _build_codecs(self) -> Dict[str, Any]:
        """
        Per-codec capability matrix for navigator.mediaCapabilities.
        Keyed by GPU tier so the reported hardware decoding matches the
        reported GPU. A profile claiming an "integrated_old" UHD Graphics
        shouldn't also claim smooth+powerEfficient AV1 decode — that
        combination doesn't exist in real hardware.
        """
        tier = self.template["gpu"].get("tier", "integrated_modern")
        # Defensive fallback — if someone adds a tier to the template list
        # but forgets to extend CODEC_MATRIX_BY_TIER, we serve the safest
        # answer (modern integrated) rather than crashing.
        matrix = CODEC_MATRIX_BY_TIER.get(
            tier, CODEC_MATRIX_BY_TIER["integrated_modern"]
        )
        # Copy so caller can't mutate our module-level constant
        return {k: dict(v) for k, v in matrix.items()}

    def _build_audio(self) -> Dict[str, Any]:
        return {
            "sample_rate":       self.rnd.choice([44100, 48000]),
            "base_latency":      round(self.rnd.uniform(0.005, 0.015), 5),
            "output_latency":    0.0,
            "max_channel_count": 2,
            "number_of_inputs":  2,
            "number_of_outputs": 2,
        }

    def _build_timezone(self) -> Dict[str, Any]:
        return {
            "id":         self.timezone["id"],
            "offset_min": self.timezone["offset_min"],
            "offset_str": self.timezone["offset_str"],
            "locale_hl":  self.lang["navigator_language"].split("-")[0],
        }

    def _build_battery(self) -> Optional[Dict[str, Any]]:
        b = self.template.get("battery")
        if not b:
            return None
        level = b.get("level") or round(self.rnd.uniform(0.3, 0.95), 2)
        if b["charging"]:
            charging_time  = self.rnd.randint(1000, 5000)
            discharging_t  = "Infinity"
        else:
            charging_time  = "Infinity"
            discharging_t  = self.rnd.randint(3600, 18000)
        return {
            "charging":         b["charging"],
            "charging_time":    charging_time,
            "discharging_time": discharging_t,
            "level":            level,
        }

    def _build_connection(self) -> Dict[str, Any]:
        conn = self.rnd.choice(CONNECTION_PROFILES)
        return {
            "effective_type": conn["effective_type"],
            "downlink":       round(conn["downlink"] + self.rnd.uniform(-1, 1), 2),
            "rtt":            conn["rtt"] + self.rnd.randint(-10, 10),
            "save_data":      conn["save_data"],
            "type":           conn["type"],
        }

    def _build_media_devices(self) -> Dict[str, Any]:
        group_audio = self._device_id("group_audio")
        group_video = self._device_id("group_video")
        return {
            "video_inputs": [
                {
                    "deviceId": self._device_id("cam1"),
                    "kind":     "videoinput",
                    "label":    "Integrated Camera",
                    "groupId":  group_video,
                },
            ],
            "audio_inputs": [
                {"deviceId": "default",        "kind": "audioinput",
                 "label": "Default - Microphone (Realtek(R) Audio)",  "groupId": group_audio},
                {"deviceId": "communications", "kind": "audioinput",
                 "label": "Communications - Microphone (Realtek(R) Audio)", "groupId": group_audio},
                {"deviceId": self._device_id("mic1"), "kind": "audioinput",
                 "label": "Microphone (Realtek(R) Audio)", "groupId": group_audio},
            ],
            "audio_outputs": [
                {"deviceId": "default",        "kind": "audiooutput",
                 "label": "Default - Speakers (Realtek(R) Audio)",  "groupId": group_audio},
                {"deviceId": "communications", "kind": "audiooutput",
                 "label": "Communications - Speakers (Realtek(R) Audio)", "groupId": group_audio},
                {"deviceId": self._device_id("spk1"), "kind": "audiooutput",
                 "label": "Speakers (Realtek(R) Audio)", "groupId": group_audio},
            ],
        }

    def _build_noise(self) -> Dict[str, Any]:
        """
        Per-profile noise seeds — fed into the C++ patches to jitter
        fingerprint outputs in ways that mimic real-hardware variability.
        Each value stays *stable* for a given profile (so creep.js
        stability score still passes), but DIFFERS between profiles
        (so a pool of 50 profiles doesn't collide on one identical
        hash). The ranges below are tuned against Creep.js, f.vision.

        canvas_shift   int 1-7     pixel-level shift in Canvas 2D output
        canvas_noise   0..0.003    per-pixel RGB jitter in readback
        webgl_noise    0..0.002    float noise on WebGL precision probes
        webgl_params_mask  int bits  which WebGL params to jitter
        audio_offset   0..3e-4     floating-point offset in Audio samples
        audio_rate_jitter  0 or ±1  sampleRate drift ±1 Hz from 48000
        rect_offset    0.001..0.02 getBoundingClientRect noise
        font_width_off ±0.5..1.5   offsetWidth/Height for text metrics
        screen_avail_jitter int 0-12  availWidth/Height taskbar-sized variance
        timezone_offset_jitter 0 or ±1  minute drift in Date().getTimezoneOffset()
        """
        return {
            "seed":                    self.rnd.randint(1_000_000, 9_999_999),
            # Canvas — bumped range so jitter is *detectable* as variance,
            # but still well below the fingerprint-changing threshold
            # (a real GPU in a PC over an hour varies similarly).
            "canvas_shift":            self.rnd.randint(1, 7),
            "canvas_noise":            round(self.rnd.uniform(0.0005, 0.003), 5),
            # WebGL — new. Precision probes (RangeMin/Max/Precision) are
            # our top tell — closes that detector. Mask picks WHICH
            # params get jittered so not every profile jitters the same set.
            "webgl_noise":             round(self.rnd.uniform(0.0004, 0.002), 5),
            "webgl_params_mask":       self.rnd.randint(0x3, 0x3F),  # 6-bit
            # Audio — the original 0.00001-9 range was below detector
            # sensitivity. 3e-5..3e-4 sits in the zone real DAC jitter lives.
            "audio_offset":            round(self.rnd.uniform(0.00003, 0.00030), 6),
            "audio_rate_jitter":       self.rnd.choice([-1, 0, 0, 0, 1]),  # biased toward 0
            # Rects — bumped 10× so getBoundingClientRect fingerprinters
            # actually see a profile-specific offset.
            "rect_offset":             round(self.rnd.uniform(0.001, 0.020), 4),
            "font_width_offset":       round(self.rnd.uniform(-1.5, 1.5), 3),
            # Screen — simulates different taskbar/dock heights 0-12 px.
            # Handled in GetAvail*() getters; screen_*_ already set.
            "screen_avail_jitter":     self.rnd.randint(0, 12),
            # Timezone — extremely rare in practice (some users on custom
            # tz db builds), but enough variance that two profiles in the
            # same city don't have pixel-identical tz offsets.
            "timezone_offset_jitter":  self.rnd.choice([-1, 0, 0, 0, 0, 1]),
        }

    def _build_fonts(self) -> List[str]:
        fonts = list(WINDOWS_FONTS_CORE)
        extended_count = int(len(WINDOWS_FONTS_EXTENDED) * self.rnd.uniform(0.6, 0.85))
        extended_chosen = self.rnd.sample(WINDOWS_FONTS_EXTENDED, extended_count)
        fonts.extend(extended_chosen)
        fonts.sort()
        return fonts

    def _build_ua_metadata(self) -> Dict[str, Any]:
        major = self.chrome_v["major"]
        full  = self.chrome_v["full"]
        brands = [
            {"brand": "Not_A Brand",    "version": "8"},
            {"brand": "Chromium",       "version": major},
            {"brand": "Google Chrome",  "version": major},
        ]
        full_brands = [
            {"brand": "Not_A Brand",    "version": "8.0.0.0"},
            {"brand": "Chromium",       "version": full},
            {"brand": "Google Chrome",  "version": full},
        ]
        return {
            "brands":            brands,
            "full_version_list": full_brands,
            "full_version":      full,
            "major_version":     major,
            "platform":          self.platform["ch_platform"],
            "platform_version":  self.platform["ch_platform_version"],
            "architecture":      self.platform["ch_arch"],
            "bitness":           self.platform["ch_bitness"],
            "model":             self.platform["ch_model"],
            "wow64":             self.platform["ch_wow64"],
            "mobile":             False,
        }

    def _build_permissions(self) -> Dict[str, str]:
        """
        Default permission states for a realistic Chrome profile.
        Values match what a freshly-installed Chrome returns when JS
        queries navigator.permissions.query({name: X}).

        W3C PermissionState: "granted" | "denied" | "prompt"
        """
        return {
            # User-facing — always "prompt" until user interacts
            "geolocation":         "prompt",
            "notifications":       "prompt",
            "camera":              "prompt",
            "microphone":          "prompt",
            "midi":                "prompt",
            "clipboard-read":      "prompt",

            # Chrome grants these automatically in normal browsing
            "clipboard-write":     "granted",
            "accelerometer":       "granted",
            "gyroscope":           "granted",
            "magnetometer":        "granted",
            "ambient-light-sensor": "granted",
            "background-sync":     "granted",
            "payment-handler":     "granted",

            # Storage APIs
            "persistent-storage":  "prompt",
            "storage-access":      "prompt",

            # Push / background
            "push":                "prompt",
            "background-fetch":    "granted",

            # Screen capture — desktop-only
            "display-capture":     "prompt",
            "window-management":   "prompt",
        }

    # ──────────────────────────────────────────────────────────

    def generate_payload_dict(self) -> Dict[str, Any]:
        logger.info(f"Building payload for '{self.profile_name}' (template={self.template['name']})")
        return {
            "version":       self.VERSION,
            "profile_name":  self.profile_name,
            "template_name": self.template["name"],
            "hardware":      self._build_hardware(),
            "languages":     self._build_languages(),
            "screen":        self._build_screen(),
            "graphics":      self._build_graphics(),
            "audio":         self._build_audio(),
            "timezone":      self._build_timezone(),
            "battery":       self._build_battery(),
            "connection":    self._build_connection(),
            "media":         self._build_media_devices(),
            "noise":         self._build_noise(),
            "fonts":         self._build_fonts(),
            "ua_metadata":   self._build_ua_metadata(),
            "plugins":       STANDARD_CHROME_PLUGINS,
            "permissions":   self._build_permissions(),
            # New for Feature #8 — WebGL/WebGPU consistency + per-tier
            # codec matrix. Consumed by:
            #   - ghost_shell_config.cc GPU parser (unmasked_vendor/renderer)
            #   - ghost_shell_config.cc codecs parser (codec map)
            #   - patches 6 + 7 in CHROMIUM_PATCHES_4.md
            "gpu":           self._build_gpu(),
            "codecs":        self._build_codecs(),
        }

    def get_cli_flag(self) -> str:
        payload_dict = self.generate_payload_dict()
        json_str = json.dumps(payload_dict, separators=(',', ':'), ensure_ascii=False)
        b64_encoded = base64.b64encode(json_str.encode('utf-8')).decode('ascii')
        flag = f"--ghost-shell-payload={b64_encoded}"
        logger.info(f"CLI flag built (length={len(flag)} chars)")
        return flag


# =============================================================================
# STANDALONE TEST
# =============================================================================

if __name__ == "__main__":
    builder = DeviceTemplateBuilder("profile_01", preferred_language="uk-UA")

    print("\n=== PAYLOAD ===\n")
    payload = builder.generate_payload_dict()
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    flag = builder.get_cli_flag()
    print(f"\n=== CLI FLAG ===")
    print(f"Length: {len(flag)} chars")

    print("\n=== DETERMINISM CHECK ===")
    b2 = DeviceTemplateBuilder("profile_01", preferred_language="uk-UA")
    p2 = b2.generate_payload_dict()
    same = json.dumps(payload, sort_keys=True) == json.dumps(p2, sort_keys=True)
    print(f"Same profile → same payload: {same}")
