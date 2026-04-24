"""
fingerprint_templates.py — Canonical device templates for coherent
fingerprint generation.

Design principle: instead of picking UA, screen, GPU from independent
pools (which can produce incoherent combos like "iPhone UA + Windows
GPU"), we pick from curated *device templates* that represent real
products with real characteristics. Sampling within a template is
guaranteed coherent by construction.

Each template describes one physical device family. All values in it
are plausible to appear together in the wild — extracted from public
fingerprint datasets (amIunique, FingerprintHub, device-tested Chrome
profiles). Adding a template? Verify on a real device first.

Template structure:
    id                : unique slug ("macbook_pro_14_m2_2023")
    label             : human label ("MacBook Pro 14\" M2 (2023)")
    category          : "desktop" / "laptop" / "tablet" / "phone"
    market_share_pct  : approximate 2024 share of THIS device among
                        desktop fingerprints (for weighted sampling)
    os                : "Windows" / "macOS" / "Linux" / ...
    os_version_range  : [min, max] — e.g. ["10.0", "11.26200"]
    ua_platform_token : what goes inside the UA parens:
                        "Windows NT 10.0; Win64; x64"
    chrome_version_range : [min_major, max_major]
    navigator_platform: navigator.platform value
    screen.options    : list of {width, height, dpr, avail_delta} —
                        one is sampled
    viewport_ratio    : fraction of screen width used as inner width
                        (typical 0.88 windowed, 1.0 fullscreen)
    gpu               : {vendor, renderer_templates[]}
    hardware_concurrency_options : list — sampled
    device_memory_options        : list — sampled (Windows only exposes)
    max_touch_points : 0 for desktop, 5 for touch
    fonts_core        : must-have set for this OS
    fonts_optional    : commonly-present; 40-80% sampled subset
    fonts_forbidden   : NOT allowed (Mac fonts on Windows, etc)
    webgl_extensions_typical : common extension list
    audio_sample_rate : typical value (48000 on most modern devices)
    color_gamut       : "srgb" / "p3" / "rec2020"
    prefers_color_scheme : "light" default, can flip
    languages_common  : languages plausibly configured
    timezone_cities_common : typical timezone by region
"""

# Reference: OS version -> UA platform token mapping.
# Chrome freezes Windows UA at 10.0 for privacy (reduced UA-CH).
# macOS freezes at 10_15_7 since Chrome 95.
_WINDOWS_UA_TOKEN = "Windows NT 10.0; Win64; x64"
_MACOS_UA_TOKEN   = "Macintosh; Intel Mac OS X 10_15_7"
_LINUX_UA_TOKEN   = "X11; Linux x86_64"


DEVICE_TEMPLATES = {

    # ═══════════════════════════════════════════════════════════
    # WINDOWS DESKTOPS & LAPTOPS
    # ═══════════════════════════════════════════════════════════
    # Windows is ~72% of desktop fingerprints in our target markets.
    # We cover the bulk: Dell/HP/Lenovo laptops + custom-built PCs
    # with common Intel/NVIDIA combinations.

    "win11_laptop_intel_iris_1920": {
        "id": "win11_laptop_intel_iris_1920",
        "label": "Windows 11 Laptop · Intel Iris Xe · 1920×1080",
        "category": "laptop",
        "market_share_pct": 18.0,
        "os": "Windows",
        "os_version_range": ["10.0", "10.0"],
        "ua_platform_token": _WINDOWS_UA_TOKEN,
        "chrome_version_range": [130, 148],
        "navigator_platform": "Win32",
        "screen_options": [
            {"width": 1920, "height": 1080, "dpr": 1.0, "avail_delta_h": 40},
            {"width": 1920, "height": 1080, "dpr": 1.25, "avail_delta_h": 40},
        ],
        "viewport_ratio": 0.92,
        "gpu": {
            "vendor": "Google Inc. (Intel)",
            "renderer_templates": [
                "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics (0x0000{:04X}) Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ],
        },
        "hardware_concurrency_options": [4, 8, 12, 16],
        "device_memory_options": [8, 16],
        "max_touch_points": 0,
        "fonts_core": [
            "Segoe UI", "Segoe UI Symbol", "Segoe UI Emoji",
            "Calibri", "Cambria", "Consolas", "Arial", "Courier New",
            "Georgia", "Tahoma", "Times New Roman", "Verdana",
            "Microsoft Sans Serif", "Lucida Console",
        ],
        "fonts_optional": [
            "Segoe Print", "Segoe Script", "Microsoft YaHei",
            "Cambria Math", "Franklin Gothic", "MS Gothic",
            "Malgun Gothic", "Leelawadee UI", "Nirmala UI",
            "Palatino Linotype", "Lucida Sans Unicode", "Impact",
            "Comic Sans MS", "Book Antiqua", "Trebuchet MS",
        ],
        "fonts_forbidden": [
            "San Francisco", "SF Pro", "SF Pro Display", "SF Pro Text",
            "Helvetica Neue", "Monaco", "Menlo", "-apple-system",
            "Apple Color Emoji", "Ubuntu", "DejaVu Sans",
            "Liberation Sans", "Noto Color Emoji",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
            "EXT_disjoint_timer_query", "EXT_float_blend", "EXT_frag_depth",
            "EXT_shader_texture_lod", "EXT_texture_compression_bptc",
            "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic",
            "WEBKIT_EXT_texture_filter_anisotropic", "EXT_sRGB",
            "KHR_parallel_shader_compile", "OES_element_index_uint",
            "OES_fbo_render_mipmap", "OES_standard_derivatives",
            "OES_texture_float", "OES_texture_float_linear",
            "OES_texture_half_float", "OES_texture_half_float_linear",
            "OES_vertex_array_object", "WEBGL_color_buffer_float",
            "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb",
            "WEBGL_debug_renderer_info", "WEBGL_debug_shaders",
            "WEBGL_depth_texture", "WEBGL_draw_buffers",
            "WEBGL_lose_context", "WEBGL_multi_draw",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "srgb",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "en-GB", "uk-UA", "pl-PL", "de-DE", "ru-RU"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Warsaw", "Europe/Berlin",
            "Europe/London", "America/New_York", "America/Chicago",
        ],
    },

    "win11_desktop_nvidia_1440": {
        "id": "win11_desktop_nvidia_1440",
        "label": "Windows 11 Desktop · NVIDIA RTX · 2560×1440",
        "category": "desktop",
        "market_share_pct": 12.0,
        "os": "Windows",
        "os_version_range": ["10.0", "10.0"],
        "ua_platform_token": _WINDOWS_UA_TOKEN,
        "chrome_version_range": [130, 148],
        "navigator_platform": "Win32",
        "screen_options": [
            {"width": 2560, "height": 1440, "dpr": 1.0, "avail_delta_h": 40},
            {"width": 1920, "height": 1080, "dpr": 1.0, "avail_delta_h": 40},
        ],
        "viewport_ratio": 0.92,
        "gpu": {
            "vendor": "Google Inc. (NVIDIA)",
            "renderer_templates": [
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ],
        },
        "hardware_concurrency_options": [8, 12, 16, 20, 24, 32],
        "device_memory_options": [8, 16, 32],
        "max_touch_points": 0,
        "fonts_core": [
            "Segoe UI", "Segoe UI Symbol", "Segoe UI Emoji",
            "Calibri", "Cambria", "Consolas", "Arial", "Courier New",
            "Georgia", "Tahoma", "Times New Roman", "Verdana",
        ],
        "fonts_optional": [
            "Segoe Print", "Microsoft YaHei", "Franklin Gothic",
            "MS Gothic", "Malgun Gothic", "Palatino Linotype",
            "Lucida Console", "Impact", "Trebuchet MS",
        ],
        "fonts_forbidden": [
            "San Francisco", "SF Pro", "Helvetica Neue", "Monaco",
            "Menlo", "-apple-system", "Apple Color Emoji",
            "Ubuntu", "DejaVu Sans",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_color_buffer_float", "EXT_color_buffer_half_float",
            "EXT_disjoint_timer_query", "EXT_float_blend",
            "EXT_texture_filter_anisotropic",
            "WEBGL_compressed_texture_s3tc", "WEBGL_debug_renderer_info",
            "WEBGL_draw_buffers", "OES_texture_float_linear",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "srgb",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "en-GB", "uk-UA", "pl-PL", "de-DE", "ru-RU"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Warsaw", "Europe/Berlin",
            "Europe/Moscow", "America/New_York",
        ],
    },

    "win10_laptop_amd_radeon_1920": {
        "id": "win10_laptop_amd_radeon_1920",
        "label": "Windows 10 Laptop · AMD Radeon · 1920×1080",
        "category": "laptop",
        "market_share_pct": 9.0,
        "os": "Windows",
        "os_version_range": ["10.0", "10.0"],
        "ua_platform_token": _WINDOWS_UA_TOKEN,
        "chrome_version_range": [128, 148],
        "navigator_platform": "Win32",
        "screen_options": [
            {"width": 1920, "height": 1080, "dpr": 1.0, "avail_delta_h": 40},
            {"width": 1366, "height": 768, "dpr": 1.0, "avail_delta_h": 40},
        ],
        "viewport_ratio": 0.92,
        "gpu": {
            "vendor": "Google Inc. (AMD)",
            "renderer_templates": [
                "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            ],
        },
        "hardware_concurrency_options": [6, 8, 12, 16],
        "device_memory_options": [8, 16],
        "max_touch_points": 0,
        "fonts_core": [
            "Segoe UI", "Calibri", "Cambria", "Consolas", "Arial",
            "Courier New", "Georgia", "Tahoma", "Times New Roman",
            "Verdana",
        ],
        "fonts_optional": [
            "MS Gothic", "Malgun Gothic", "Franklin Gothic",
            "Palatino Linotype", "Impact", "Trebuchet MS",
        ],
        "fonts_forbidden": [
            "San Francisco", "SF Pro", "Helvetica Neue", "Monaco",
            "-apple-system", "Apple Color Emoji", "Ubuntu",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_texture_filter_anisotropic",
            "WEBGL_compressed_texture_s3tc", "WEBGL_debug_renderer_info",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "srgb",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "uk-UA", "pl-PL", "ru-RU"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Warsaw", "Europe/Moscow",
        ],
    },

    # ═══════════════════════════════════════════════════════════
    # macOS
    # ═══════════════════════════════════════════════════════════
    # macOS is ~17% of desktop fingerprints. Apple Silicon dominates
    # new units; Intel Macs still significant (>30% of macOS base).

    "macbook_pro_14_m2_2023": {
        "id": "macbook_pro_14_m2_2023",
        "label": "MacBook Pro 14\" M2 (2023)",
        "category": "laptop",
        "market_share_pct": 4.0,
        "os": "macOS",
        "os_version_range": ["10.15.7", "10.15.7"],  # frozen by Chrome
        "ua_platform_token": _MACOS_UA_TOKEN,
        "chrome_version_range": [130, 148],
        "navigator_platform": "MacIntel",
        "screen_options": [
            # Native res 3024×1964 @ 2x, but Chrome reports logical
            {"width": 1512, "height": 982, "dpr": 2.0, "avail_delta_h": 25},
            # Scaled modes users often pick:
            {"width": 1440, "height": 900, "dpr": 2.0, "avail_delta_h": 25},
            {"width": 1680, "height": 1050, "dpr": 2.0, "avail_delta_h": 25},
        ],
        "viewport_ratio": 0.95,
        "gpu": {
            "vendor": "Google Inc. (Apple)",
            "renderer_templates": [
                "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
                "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Pro, Unspecified Version)",
            ],
        },
        "hardware_concurrency_options": [8, 10, 12],
        "device_memory_options": [8],  # macOS doesn't expose — always 8
        "max_touch_points": 0,
        "fonts_core": [
            "-apple-system", "San Francisco", "SF Pro", "SF Pro Display",
            "SF Pro Text", "Helvetica Neue", "Helvetica", "Lucida Grande",
            "Geneva", "Monaco", "Menlo", "Courier", "Times", "Arial",
            "Courier New", "Georgia", "Tahoma", "Times New Roman",
            "Verdana", "Apple Color Emoji",
        ],
        "fonts_optional": [
            "Avenir", "Avenir Next", "Baskerville", "Big Caslon",
            "Bodoni 72", "Cochin", "Didot", "Futura", "Gill Sans",
            "Hoefler Text", "Optima", "Palatino", "Papyrus",
            "Trebuchet MS", "Impact", "Comic Sans MS",
            "Hiragino Sans", "Hiragino Kaku Gothic Pro",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "Cambria", "Consolas",
            "Microsoft YaHei", "MS Gothic", "Ubuntu", "DejaVu Sans",
            "Liberation Sans", "Noto Color Emoji",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_color_buffer_half_float", "EXT_float_blend",
            "EXT_texture_filter_anisotropic",
            "WEBGL_debug_renderer_info", "WEBGL_draw_buffers",
            "OES_texture_float_linear", "OES_texture_half_float_linear",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "p3",  # Apple displays support P3
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "en-GB", "de-DE", "fr-FR", "es-ES", "uk-UA"],
        "timezone_cities_common": [
            "America/New_York", "America/Los_Angeles", "Europe/London",
            "Europe/Berlin", "Europe/Kyiv",
        ],
    },

    "macbook_air_m1_2020": {
        "id": "macbook_air_m1_2020",
        "label": "MacBook Air M1 (2020)",
        "category": "laptop",
        "market_share_pct": 5.0,
        "os": "macOS",
        "os_version_range": ["10.15.7", "10.15.7"],
        "ua_platform_token": _MACOS_UA_TOKEN,
        "chrome_version_range": [128, 148],
        "navigator_platform": "MacIntel",
        "screen_options": [
            {"width": 1440, "height": 900, "dpr": 2.0, "avail_delta_h": 25},
            {"width": 1280, "height": 800, "dpr": 2.0, "avail_delta_h": 25},
            {"width": 1680, "height": 1050, "dpr": 2.0, "avail_delta_h": 25},
        ],
        "viewport_ratio": 0.95,
        "gpu": {
            "vendor": "Google Inc. (Apple)",
            "renderer_templates": [
                "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
            ],
        },
        "hardware_concurrency_options": [8],
        "device_memory_options": [8],
        "max_touch_points": 0,
        "fonts_core": [
            "-apple-system", "San Francisco", "SF Pro", "SF Pro Display",
            "SF Pro Text", "Helvetica Neue", "Helvetica", "Lucida Grande",
            "Geneva", "Monaco", "Menlo", "Courier", "Times", "Arial",
            "Courier New", "Georgia", "Tahoma", "Times New Roman",
            "Verdana", "Apple Color Emoji",
        ],
        "fonts_optional": [
            "Avenir", "Avenir Next", "Baskerville", "Futura",
            "Gill Sans", "Optima", "Palatino", "Trebuchet MS",
            "Impact", "Hiragino Sans",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "Cambria", "Consolas",
            "Microsoft YaHei", "Ubuntu",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_texture_filter_anisotropic",
            "WEBGL_debug_renderer_info",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "p3",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "en-GB", "de-DE", "fr-FR", "uk-UA"],
        "timezone_cities_common": [
            "America/New_York", "America/Los_Angeles", "Europe/London",
            "Europe/Kyiv",
        ],
    },

    "imac_intel_retina_2019": {
        "id": "imac_intel_retina_2019",
        "label": "iMac Intel Retina (2019)",
        "category": "desktop",
        "market_share_pct": 2.0,
        "os": "macOS",
        "os_version_range": ["10.15.7", "10.15.7"],
        "ua_platform_token": _MACOS_UA_TOKEN,
        "chrome_version_range": [128, 148],
        "navigator_platform": "MacIntel",
        "screen_options": [
            {"width": 2560, "height": 1440, "dpr": 2.0, "avail_delta_h": 25},
            {"width": 1920, "height": 1080, "dpr": 2.0, "avail_delta_h": 25},
        ],
        "viewport_ratio": 0.95,
        "gpu": {
            "vendor": "Google Inc. (AMD)",
            "renderer_templates": [
                "ANGLE (AMD, AMD Radeon Pro 5500M OpenGL Engine, OpenGL 4.1)",
                "ANGLE (AMD, AMD Radeon Pro 5700 OpenGL Engine, OpenGL 4.1)",
            ],
        },
        "hardware_concurrency_options": [8, 12],
        "device_memory_options": [8],
        "max_touch_points": 0,
        "fonts_core": [
            "-apple-system", "San Francisco", "SF Pro", "Helvetica Neue",
            "Helvetica", "Lucida Grande", "Monaco", "Menlo", "Arial",
            "Courier New", "Georgia", "Times New Roman", "Verdana",
            "Apple Color Emoji",
        ],
        "fonts_optional": [
            "Avenir", "Baskerville", "Futura", "Gill Sans", "Optima",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "Consolas", "Microsoft YaHei",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_texture_filter_anisotropic",
            "WEBGL_debug_renderer_info",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "p3",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "en-GB", "de-DE", "fr-FR"],
        "timezone_cities_common": [
            "America/New_York", "America/Los_Angeles", "Europe/London",
        ],
    },

    # ═══════════════════════════════════════════════════════════
    # LINUX — small but vocal minority, mostly Ubuntu/Fedora
    # ═══════════════════════════════════════════════════════════

    "linux_ubuntu_intel_1920": {
        "id": "linux_ubuntu_intel_1920",
        "label": "Linux Ubuntu · Intel · 1920×1080",
        "category": "desktop",
        "market_share_pct": 3.0,
        "os": "Linux",
        "os_version_range": ["5.15", "6.8"],
        "ua_platform_token": _LINUX_UA_TOKEN,
        "chrome_version_range": [128, 148],
        "navigator_platform": "Linux x86_64",
        "screen_options": [
            {"width": 1920, "height": 1080, "dpr": 1.0, "avail_delta_h": 60},
            {"width": 2560, "height": 1440, "dpr": 1.0, "avail_delta_h": 60},
        ],
        "viewport_ratio": 0.92,
        "gpu": {
            "vendor": "Google Inc. (Intel)",
            "renderer_templates": [
                "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
                "ANGLE (Intel, Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2), OpenGL 4.6)",
            ],
        },
        "hardware_concurrency_options": [4, 8, 12, 16],
        "device_memory_options": [8, 16],
        "max_touch_points": 0,
        "fonts_core": [
            "Ubuntu", "Ubuntu Condensed", "Ubuntu Mono",
            "DejaVu Sans", "DejaVu Sans Mono", "DejaVu Serif",
            "Liberation Sans", "Liberation Serif", "Liberation Mono",
            "Noto Sans", "Noto Serif", "Noto Color Emoji",
            "Arial", "Courier New", "Times New Roman",
        ],
        "fonts_optional": [
            "Droid Sans", "Droid Sans Mono", "Cantarell",
            "Open Sans", "Roboto", "Lato", "FreeSans", "FreeSerif",
            "FreeMono",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "Cambria", "Consolas",
            "San Francisco", "SF Pro", "Helvetica Neue", "Monaco",
            "-apple-system", "Apple Color Emoji", "Microsoft YaHei",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_texture_filter_anisotropic",
            "WEBGL_debug_renderer_info",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "srgb",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "en-GB", "de-DE", "uk-UA"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Berlin", "America/New_York",
        ],
    },

    # ═══════════════════════════════════════════════════════════
    # ANDROID PHONES
    # ═══════════════════════════════════════════════════════════
    # Mobile templates are structurally similar to desktop but carry
    # the is_mobile flag which the runtime uses to enable CDP touch +
    # viewport emulation. UA is Chrome-on-Android format. We do NOT
    # ship iOS templates — iOS Chrome is a Safari/WKWebView shim, so
    # emulating it from a Chromium binary is inherently incoherent.
    # Stick to Android where Chrome is native. The mobile UA format:
    #
    #   Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36
    #     (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36
    #
    # Key differences from desktop:
    #   is_mobile = True        → CDP Emulation.setDeviceMetricsOverride mobile:true
    #   max_touch_points = 5    → navigator.maxTouchPoints reports touch-capable
    #   viewport_ratio = 1.0    → mobile Chrome is full-screen (no chrome bars at inner)
    #   screens are PORTRAIT    → heights > widths, DPR 2.0-3.5
    #   Chrome on Android usually FREEZES hardware_concurrency at 8
    #   fonts_core is the Android system set (Roboto dominant)

    "pixel_8_android14": {
        "id": "pixel_8_android14",
        "label": "Google Pixel 8 · Android 14",
        "category": "phone",
        "is_mobile": True,
        "market_share_pct": 3.0,
        "os": "Android",
        "os_version_range": ["14", "14"],
        "ua_platform_token": "Linux; Android 14; Pixel 8",
        "chrome_version_range": [130, 148],
        "navigator_platform": "Linux armv81",
        "screen_options": [
            # Portrait CSS px (DPR-independent); DPR 2.625 on Pixel 8 for 1080-wide physical
            {"width": 412, "height": 915, "dpr": 2.625, "avail_delta_h": 24},
        ],
        "viewport_ratio": 1.0,
        "gpu": {
            "vendor": "Google Inc. (Qualcomm)",
            "renderer_templates": [
                "ANGLE (Qualcomm, Adreno (TM) 740, OpenGL ES 3.2 V@0760.0)",
                "ANGLE (Qualcomm, Mali-G710 MC10, OpenGL ES 3.2)",
            ],
        },
        "hardware_concurrency_options": [8],
        "device_memory_options": [4, 8],
        "max_touch_points": 5,
        "fonts_core": [
            "Roboto", "Noto Sans", "Noto Serif", "Noto Color Emoji",
            "Google Sans", "Droid Sans Mono", "Roboto Mono",
        ],
        "fonts_optional": [
            "Noto Sans CJK", "Noto Sans Arabic", "Noto Sans Devanagari",
            "Noto Naskh Arabic", "Google Sans Text",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "Cambria", "Consolas", "Tahoma",
            "San Francisco", "SF Pro", "Helvetica Neue", "Monaco",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
            "EXT_float_blend", "EXT_frag_depth", "EXT_shader_texture_lod",
            "EXT_texture_filter_anisotropic", "KHR_parallel_shader_compile",
            "OES_element_index_uint", "OES_standard_derivatives",
            "OES_texture_float", "OES_texture_float_linear",
            "OES_texture_half_float", "OES_texture_half_float_linear",
            "OES_vertex_array_object", "WEBGL_compressed_texture_astc",
            "WEBGL_compressed_texture_etc", "WEBGL_compressed_texture_etc1",
            "WEBGL_debug_renderer_info", "WEBGL_debug_shaders",
            "WEBGL_depth_texture", "WEBGL_draw_buffers", "WEBGL_lose_context",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "srgb",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "uk-UA", "pl-PL", "de-DE", "ru-RU"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Warsaw", "Europe/Berlin",
            "America/New_York", "Asia/Dubai",
        ],
    },

    "pixel_7_android14": {
        "id": "pixel_7_android14",
        "label": "Google Pixel 7 · Android 14",
        "category": "phone",
        "is_mobile": True,
        "market_share_pct": 2.0,
        "os": "Android",
        "os_version_range": ["14", "14"],
        "ua_platform_token": "Linux; Android 14; Pixel 7",
        "chrome_version_range": [128, 148],
        "navigator_platform": "Linux armv81",
        "screen_options": [
            {"width": 412, "height": 915, "dpr": 2.625, "avail_delta_h": 24},
            {"width": 393, "height": 851, "dpr": 2.75,  "avail_delta_h": 24},
        ],
        "viewport_ratio": 1.0,
        "gpu": {
            "vendor": "Google Inc. (Qualcomm)",
            "renderer_templates": [
                "ANGLE (Qualcomm, Adreno (TM) 730, OpenGL ES 3.2 V@0720.0)",
                "ANGLE (ARM, Mali-G710 MP7, OpenGL ES 3.2)",
            ],
        },
        "hardware_concurrency_options": [8],
        "device_memory_options": [4, 8],
        "max_touch_points": 5,
        "fonts_core": [
            "Roboto", "Noto Sans", "Noto Serif", "Noto Color Emoji",
            "Google Sans", "Droid Sans Mono",
        ],
        "fonts_optional": [
            "Noto Sans CJK", "Noto Sans Arabic", "Noto Naskh Arabic",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "Consolas", "San Francisco", "SF Pro",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
            "EXT_float_blend", "EXT_frag_depth", "EXT_texture_filter_anisotropic",
            "KHR_parallel_shader_compile", "OES_element_index_uint",
            "OES_texture_float", "OES_texture_float_linear",
            "OES_vertex_array_object", "WEBGL_compressed_texture_astc",
            "WEBGL_compressed_texture_etc", "WEBGL_debug_renderer_info",
            "WEBGL_depth_texture", "WEBGL_draw_buffers", "WEBGL_lose_context",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "srgb",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "uk-UA", "pl-PL", "de-DE"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Warsaw", "America/New_York",
        ],
    },

    "samsung_s24_android14": {
        "id": "samsung_s24_android14",
        "label": "Samsung Galaxy S24 · Android 14",
        "category": "phone",
        "is_mobile": True,
        "market_share_pct": 4.0,
        "os": "Android",
        "os_version_range": ["14", "14"],
        "ua_platform_token": "Linux; Android 14; SM-S921B",
        "chrome_version_range": [130, 148],
        "navigator_platform": "Linux armv81",
        "screen_options": [
            {"width": 360, "height": 780, "dpr": 3.0, "avail_delta_h": 24},
            {"width": 384, "height": 832, "dpr": 2.8125, "avail_delta_h": 24},
        ],
        "viewport_ratio": 1.0,
        "gpu": {
            "vendor": "Google Inc. (Qualcomm)",
            "renderer_templates": [
                "ANGLE (Qualcomm, Adreno (TM) 750, OpenGL ES 3.2 V@0770.0)",
                "ANGLE (Samsung Electronics Co.,LTD., Xclipse 940, OpenGL ES 3.2)",
            ],
        },
        "hardware_concurrency_options": [8],
        "device_memory_options": [8, 12],
        "max_touch_points": 10,
        "fonts_core": [
            "Roboto", "Noto Sans", "Noto Color Emoji",
            "SEC CJK", "SamsungOne", "SamsungSans",
        ],
        "fonts_optional": [
            "Noto Sans CJK", "Noto Sans Arabic", "Droid Sans Mono",
            "Roboto Mono", "SamsungOne 400",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "Consolas", "San Francisco", "SF Pro",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax", "EXT_color_buffer_half_float",
            "EXT_float_blend", "EXT_frag_depth", "EXT_texture_filter_anisotropic",
            "KHR_parallel_shader_compile", "OES_element_index_uint",
            "OES_texture_float", "OES_vertex_array_object",
            "WEBGL_compressed_texture_astc", "WEBGL_compressed_texture_etc",
            "WEBGL_debug_renderer_info", "WEBGL_depth_texture",
            "WEBGL_draw_buffers", "WEBGL_lose_context", "WEBGL_multi_draw",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "p3",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "uk-UA", "pl-PL", "de-DE", "fr-FR", "es-ES"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Warsaw", "Europe/Berlin",
            "America/New_York", "America/Chicago", "Asia/Seoul",
        ],
    },

    "samsung_a54_android13": {
        "id": "samsung_a54_android13",
        "label": "Samsung Galaxy A54 · Android 13 (mid-range)",
        "category": "phone",
        "is_mobile": True,
        "market_share_pct": 2.5,
        "os": "Android",
        "os_version_range": ["13", "14"],
        "ua_platform_token": "Linux; Android 13; SM-A546B",
        "chrome_version_range": [126, 146],
        "navigator_platform": "Linux armv81",
        "screen_options": [
            {"width": 360, "height": 780, "dpr": 2.625, "avail_delta_h": 24},
        ],
        "viewport_ratio": 1.0,
        "gpu": {
            "vendor": "Google Inc. (ARM)",
            "renderer_templates": [
                "ANGLE (ARM, Mali-G68 MC4, OpenGL ES 3.2)",
                "ANGLE (ARM, Mali-G68 MP4, OpenGL ES 3.2)",
            ],
        },
        "hardware_concurrency_options": [8],
        "device_memory_options": [6, 8],
        "max_touch_points": 10,
        "fonts_core": [
            "Roboto", "Noto Sans", "Noto Color Emoji", "SamsungOne",
        ],
        "fonts_optional": [
            "Noto Sans CJK", "Noto Sans Arabic", "Droid Sans Mono",
        ],
        "fonts_forbidden": [
            "Segoe UI", "Calibri", "San Francisco", "SF Pro",
        ],
        "webgl_extensions_typical": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_color_buffer_half_float", "EXT_frag_depth",
            "EXT_texture_filter_anisotropic", "KHR_parallel_shader_compile",
            "OES_element_index_uint", "OES_texture_float",
            "OES_vertex_array_object", "WEBGL_compressed_texture_astc",
            "WEBGL_compressed_texture_etc", "WEBGL_debug_renderer_info",
            "WEBGL_depth_texture", "WEBGL_draw_buffers", "WEBGL_lose_context",
        ],
        "audio_sample_rate": 48000,
        "color_gamut": "srgb",
        "prefers_color_scheme": "light",
        "languages_common": ["en-US", "uk-UA", "pl-PL", "tr-TR"],
        "timezone_cities_common": [
            "Europe/Kyiv", "Europe/Warsaw", "Europe/Istanbul", "Asia/Dubai",
        ],
    },
}


def all_templates() -> list[dict]:
    """List all templates in a stable order (by market share desc)."""
    return sorted(
        DEVICE_TEMPLATES.values(),
        key=lambda t: -t.get("market_share_pct", 0),
    )


def get_template(template_id: str) -> dict | None:
    return DEVICE_TEMPLATES.get(template_id)


def weighted_pick_template(seed: str = None) -> dict:
    """Pick a template weighted by market share. Deterministic if seed
    provided — so same profile always gets same template on re-seed."""
    import random
    import hashlib
    rng = random.Random(
        int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16)
        if seed else None
    )
    pool = list(DEVICE_TEMPLATES.values())
    weights = [t.get("market_share_pct", 1.0) for t in pool]
    return rng.choices(pool, weights=weights, k=1)[0]
