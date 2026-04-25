"""
fingerprint_selftest.py — Runtime fingerprint verifier.

Launches Chromium with a profile's configured fingerprint, navigates
to about:blank, executes a JS probe to collect the ACTUAL reported
values, then compares vs what we configured. This is the truth
test — it reveals stealth-patch failures (webdriver exposed, UA not
applied, platform mismatch).

Why it's separate from the existing fingerprint_tester.py:
    - That tester checks stability (run 3x, do values change?).
    - This one checks fidelity (do the values match what we told
      Chrome to report?) + coherence (are the returned values
      self-consistent?).

Design: headless short-lived Chrome via the existing nk_browser
infrastructure. Does NOT run the full monitoring pipeline — just
opens about:blank, runs the probe script, closes. Target duration
< 8 seconds per test.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import logging
import time
import traceback

from ghost_shell.fingerprint.validator import validate, compare_configured_vs_actual
from ghost_shell.fingerprint.templates import get_template


# ═══════════════════════════════════════════════════════════════
# JS probe — collects everything we need to validate.
# Runs inside the target browser via CDP Runtime.evaluate.
# ═══════════════════════════════════════════════════════════════

PROBE_SCRIPT = r"""
(async function collectFingerprint() {
    const result = {};
    const errors = [];

    // ---- Navigator ----
    try {
        result.navigator = {
            userAgent:           navigator.userAgent,
            platform:            navigator.platform,
            language:            navigator.language,
            languages:           Array.from(navigator.languages || []),
            hardwareConcurrency: navigator.hardwareConcurrency,
            deviceMemory:        navigator.deviceMemory,
            maxTouchPoints:      navigator.maxTouchPoints,
            vendor:              navigator.vendor,
            doNotTrack:          navigator.doNotTrack,
            webdriver:           navigator.webdriver,
            cookieEnabled:       navigator.cookieEnabled,
            onLine:              navigator.onLine,
        };
    } catch (e) { errors.push("navigator: " + e.message); }

    // ---- UA Client Hints (entropy reveal) ----
    try {
        if (navigator.userAgentData) {
            const hints = await navigator.userAgentData.getHighEntropyValues([
                "architecture", "bitness", "brands", "fullVersionList",
                "mobile", "model", "platform", "platformVersion", "wow64"
            ]);
            result.ua_client_hints = hints;
        }
    } catch (e) { errors.push("ua-ch: " + e.message); }

    // ---- Screen / window ----
    try {
        result.screen = {
            width:       screen.width,
            height:      screen.height,
            availWidth:  screen.availWidth,
            availHeight: screen.availHeight,
            colorDepth:  screen.colorDepth,
            pixelDepth:  screen.pixelDepth,
        };
        result.window = {
            innerWidth:       window.innerWidth,
            innerHeight:      window.innerHeight,
            outerWidth:       window.outerWidth,
            outerHeight:      window.outerHeight,
            devicePixelRatio: window.devicePixelRatio,
        };
    } catch (e) { errors.push("screen: " + e.message); }

    // ---- Timezone / locale ----
    try {
        const intl = Intl.DateTimeFormat().resolvedOptions();
        result.timezone = {
            intl:   intl.timeZone,
            offset: new Date().getTimezoneOffset(),
            locale: intl.locale,
        };
    } catch (e) { errors.push("timezone: " + e.message); }

    // ---- WebGL (GPU vendor/renderer) ----
    try {
        const canvas = document.createElement("canvas");
        const gl = canvas.getContext("webgl2") || canvas.getContext("webgl");
        if (gl) {
            const dbg = gl.getExtension("WEBGL_debug_renderer_info");
            result.webgl = {
                vendor:    dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)   : gl.getParameter(gl.VENDOR),
                renderer:  dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
                version:   gl.getParameter(gl.VERSION),
                shading:   gl.getParameter(gl.SHADING_LANGUAGE_VERSION),
                extensions: gl.getSupportedExtensions() || [],
            };
        }
    } catch (e) { errors.push("webgl: " + e.message); }

    // ---- Canvas fingerprint (stable hash of rendering) ----
    try {
        const c = document.createElement("canvas");
        c.width = 300; c.height = 80;
        const ctx = c.getContext("2d");
        ctx.textBaseline = "top";
        ctx.font = "14px Arial";
        ctx.fillStyle = "#f60";
        ctx.fillRect(125, 1, 62, 20);
        ctx.fillStyle = "#069";
        ctx.fillText("Ghost Shell fp-test 🕵", 2, 15);
        ctx.fillStyle = "rgba(102, 204, 0, 0.7)";
        ctx.fillText("Ghost Shell fp-test 🕵", 4, 17);
        const dataUrl = c.toDataURL();
        // Tiny hash without crypto for speed
        let h = 0;
        for (let i = 0; i < dataUrl.length; i++) {
            h = ((h << 5) - h) + dataUrl.charCodeAt(i);
            h |= 0;
        }
        result.canvas_hash = h.toString(16);
    } catch (e) { errors.push("canvas: " + e.message); }

    // ---- Audio fingerprint ----
    try {
        const audioCtx = new (window.OfflineAudioContext ||
                              window.webkitOfflineAudioContext)(1, 44100, 44100);
        result.audio = {
            sampleRate:    audioCtx.sampleRate,
            maxChannelCount: audioCtx.destination.maxChannelCount,
            numberOfInputs:  audioCtx.destination.numberOfInputs,
            numberOfOutputs: audioCtx.destination.numberOfOutputs,
            channelCount:    audioCtx.destination.channelCount,
            channelCountMode: audioCtx.destination.channelCountMode,
            channelInterpretation: audioCtx.destination.channelInterpretation,
        };
    } catch (e) { errors.push("audio: " + e.message); }

    // ---- Fonts probe — measure width of test string across known fonts ----
    // Comprehensive check: which of our probe fonts are actually installed.
    // Method: measure text width with font stack — if different from
    // default, font is present.
    try {
        const probeFonts = [
            // Windows
            "Segoe UI", "Calibri", "Consolas", "Microsoft Sans Serif",
            "Cambria", "Tahoma", "Verdana",
            // macOS
            "San Francisco", "SF Pro", "Helvetica Neue", "Monaco", "Menlo",
            "-apple-system", "Apple Color Emoji",
            // Linux
            "Ubuntu", "DejaVu Sans", "Liberation Sans", "Noto Sans",
            // Universal
            "Arial", "Times New Roman", "Courier New", "Georgia",
        ];
        const baseFonts = ["monospace", "sans-serif", "serif"];
        const testString = "mmmmmmmmmmlli";
        const testSize = "72px";

        const span = document.createElement("span");
        span.style.position = "absolute";
        span.style.left = "-9999px";
        span.style.fontSize = testSize;
        span.innerHTML = testString;
        document.body.appendChild(span);

        // Baseline widths for the 3 generic families
        const baselines = {};
        for (const base of baseFonts) {
            span.style.fontFamily = base;
            baselines[base] = {
                w: span.offsetWidth,
                h: span.offsetHeight,
            };
        }

        const detected = [];
        for (const f of probeFonts) {
            let present = false;
            for (const base of baseFonts) {
                span.style.fontFamily = `"${f}",${base}`;
                if (span.offsetWidth  !== baselines[base].w ||
                    span.offsetHeight !== baselines[base].h) {
                    present = true;
                    break;
                }
            }
            if (present) detected.push(f);
        }
        document.body.removeChild(span);
        result.fonts = detected;
    } catch (e) { errors.push("fonts: " + e.message); }

    // ---- Misc ----
    try {
        result.plugins = Array.from(navigator.plugins || []).map(p => p.name);
        result.mimeTypes = Array.from(navigator.mimeTypes || []).map(m => m.type);
    } catch (e) { errors.push("plugins: " + e.message); }

    result._errors = errors;
    result._timestamp = Date.now();
    return result;
})();
"""


# ═══════════════════════════════════════════════════════════════
# Python entry point — orchestrates Chrome launch + probe + compare
# ═══════════════════════════════════════════════════════════════

def run_selftest(profile_name: str, configured_fp: dict,
                 timeout: float = 30.0) -> dict:
    """Launch a lightweight browser, collect the actual fingerprint,
    compare vs configured, return full report.

    Returns dict with:
        {
            "ok":                bool,
            "error":             str | None,
            "observed":          dict (actual JS result),
            "configured":        dict (input — for diff),
            "coherence":         dict (validator.validate() result),
            "comparison":        dict (configured_vs_actual result),
            "duration_ms":       int,
        }

    This function does NOT modify any DB state — callers decide whether
    to persist the result.
    """
    t0 = time.time()
    result = {
        "ok":          False,
        "error":       None,
        "observed":    None,
        "configured":  configured_fp,
        "coherence":   None,
        "comparison":  None,
        "duration_ms": 0,
    }

    try:
        # Lazy import — avoids pulling in Selenium for pure-Python callers
        # (e.g. tests or one-off scripts).
        from nk_browser import NKBrowser
    except ImportError as e:
        result["error"] = (f"nk_browser module unavailable: {e}. "
                           "Runtime self-test requires the full browser "
                           "stack.")
        result["duration_ms"] = int((time.time() - t0) * 1000)
        return result

    browser = None
    try:
        # Launch browser with this profile's fingerprint applied.
        # NKBrowser reads the fingerprint from DB (current row), so the
        # caller must have saved it before running selftest.
        browser = NKBrowser(
            profile_name=profile_name,
            proxy_url=None,            # don't use proxy for selftest —
                                        # we test the browser, not the network
            headless=True,
            skip_enrich=True,           # no history injection, short-lived
        )
        browser.start()

        # about:blank is faster than loading a real page and gives us a
        # clean environment to probe. Document.body exists so the fonts
        # probe works.
        browser.driver.get("about:blank")

        # Execute the probe and decode the JSON result
        raw = browser.driver.execute_script("return " + PROBE_SCRIPT)
        if isinstance(raw, str):
            observed = json.loads(raw)
        else:
            observed = raw

        result["observed"] = observed

        # ── Coherence validation: does the observed fingerprint
        # make sense as a whole? (regardless of what we configured)
        template_id = configured_fp.get("template_id")
        if template_id:
            template = get_template(template_id)
            if template:
                result["coherence"] = validate(observed, template)

        # ── Configured-vs-actual comparison: did stealth patches work?
        # Flat config is what we passed to Chrome; observed is what
        # Chrome actually reports back to JS.
        result["comparison"] = compare_configured_vs_actual(
            configured_fp, observed
        )

        result["ok"] = True

    except Exception as e:
        logging.exception("selftest failed")
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
    finally:
        if browser:
            try:
                browser.stop()
            except Exception:
                pass
        result["duration_ms"] = int((time.time() - t0) * 1000)

    return result
