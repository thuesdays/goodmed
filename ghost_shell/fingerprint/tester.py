"""
fingerprint_tester.py — Проверка стабильности фингерпринта

Запускает браузер несколько раз подряд, каждый раз снимает полный
отпечаток через JS, сравнивает. Если что-то меняется между запусками —
это баг, Google это точно заметит.

Проверяет:
- Canvas hash (рендеринг canvas)
- WebGL vendor/renderer
- AudioContext signature
- Navigator properties
- Screen properties
- Client hints

Использование:
    python fingerprint_tester.py <profile_name>

    # В коде
    tester = FingerprintTester("profile_01")
    report = tester.run(iterations=3)
    tester.print_report(report)
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import time
import json
import hashlib
import logging
from datetime import datetime

from nk_browser import NKBrowser


# ──────────────────────────────────────────────────────────────
# JS-СКРИПТ ДЛЯ ПОЛУЧЕНИЯ ПОЛНОГО ОТПЕЧАТКА
# ──────────────────────────────────────────────────────────────

FINGERPRINT_SCRIPT = r"""
return (async () => {
    const result = {};

    // Navigator
    result.navigator = {
        userAgent:            navigator.userAgent,
        platform:             navigator.platform,
        language:             navigator.language,
        languages:            Array.from(navigator.languages || []),
        hardwareConcurrency:  navigator.hardwareConcurrency,
        deviceMemory:         navigator.deviceMemory,
        webdriver:            navigator.webdriver,
        vendor:               navigator.vendor,
        maxTouchPoints:       navigator.maxTouchPoints,
        doNotTrack:           navigator.doNotTrack,
    };

    // Screen
    result.screen = {
        width:       screen.width,
        height:      screen.height,
        availWidth:  screen.availWidth,
        availHeight: screen.availHeight,
        colorDepth:  screen.colorDepth,
        pixelDepth:  screen.pixelDepth,
    };

    // Window
    result.window = {
        innerWidth:  window.innerWidth,
        innerHeight: window.innerHeight,
        outerWidth:  window.outerWidth,
        outerHeight: window.outerHeight,
        screenX:     window.screenX,
        screenY:     window.screenY,
        devicePixelRatio: window.devicePixelRatio,
    };

    // Timezone
    result.timezone = {
        intl:       Intl.DateTimeFormat().resolvedOptions().timeZone,
        offset:     new Date().getTimezoneOffset(),
    };

    // WebGL
    try {
        const c = document.createElement('canvas');
        const gl = c.getContext('webgl');
        result.webgl = {
            vendor:   gl.getParameter(37445),
            renderer: gl.getParameter(37446),
            version:  gl.getParameter(gl.VERSION),
        };
    } catch(e) { result.webgl = { error: e.message }; }

    // Canvas hash (рендерим фигуру, хэшируем результат)
    try {
        const cvs = document.createElement('canvas');
        cvs.width = 240; cvs.height = 60;
        const ctx = cvs.getContext('2d');
        ctx.textBaseline = "top";
        ctx.font = "14px 'Arial'";
        ctx.fillStyle = "#f60";
        ctx.fillRect(125, 1, 62, 20);
        ctx.fillStyle = "#069";
        ctx.fillText("Fingerprint test! \u{1F600}", 2, 15);
        ctx.fillStyle = "rgba(102, 204, 0, 0.7)";
        ctx.fillText("Fingerprint test! \u{1F600}", 4, 17);
        result.canvas = cvs.toDataURL();
    } catch(e) { result.canvas = { error: e.message }; }

    // Audio hash
    try {
        const AC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
        if (AC) {
            const ctx = new AC(1, 44100, 44100);
            const osc = ctx.createOscillator();
            osc.type = 'triangle';
            osc.frequency.value = 10000;
            const compressor = ctx.createDynamicsCompressor();
            compressor.threshold.value = -50;
            compressor.knee.value = 40;
            compressor.ratio.value = 12;
            compressor.attack.value = 0;
            compressor.release.value = 0.25;
            osc.connect(compressor);
            compressor.connect(ctx.destination);
            osc.start(0);
            ctx.startRendering();
            const buf = await new Promise(resolve => {
                ctx.oncomplete = (e) => resolve(e.renderedBuffer);
                setTimeout(() => resolve(null), 2000);
            });
            if (buf) {
                const data = buf.getChannelData(0);
                let sum = 0;
                for (let i = 0; i < data.length; i++) sum += Math.abs(data[i]);
                result.audio = sum.toFixed(10);
            }
        }
    } catch(e) { result.audio = { error: e.message }; }

    // Fonts (подмножество)
    result.fonts = [];
    const testFonts = ['Arial', 'Calibri', 'Segoe UI', 'Tahoma', 'Verdana',
                       'Courier New', 'Times New Roman', 'Consolas', 'Comic Sans MS'];
    for (const font of testFonts) {
        try {
            if (document.fonts.check('12px "' + font + '"')) {
                result.fonts.push(font);
            }
        } catch(e) {}
    }

    // Plugins
    result.plugins = Array.from(navigator.plugins || []).map(p => p.name);

    // MediaDevices
    try {
        if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
            const devs = await navigator.mediaDevices.enumerateDevices();
            result.mediaDevices = devs.map(d => ({kind: d.kind, deviceId: d.deviceId.slice(0, 20)}));
        }
    } catch(e) {}

    return result;
})();
"""


# ──────────────────────────────────────────────────────────────
# TESTER
# ──────────────────────────────────────────────────────────────

class FingerprintTester:
    """Проверяет стабильность фингерпринта между запусками"""

    def __init__(self, profile_name: str, proxy_str: str = None):
        self.profile_name = profile_name
        self.proxy_str    = proxy_str

    def capture(self) -> dict:
        """Запускает браузер, снимает отпечаток, закрывает"""
        with NKBrowser(
            profile_name = self.profile_name,
            proxy_str    = self.proxy_str,
            auto_session = False,   # не трогаем сессию во время теста
        ) as browser:
            browser.driver.get("about:blank")
            time.sleep(2)
            fp = browser.driver.execute_script(FINGERPRINT_SCRIPT)
            return fp

    def run(self, iterations: int = 3) -> dict:
        """
        Снимает отпечаток N раз подряд, сравнивает.
        Возвращает отчёт с различиями.
        """
        logging.info(f"[FpTester] Запускаем {iterations} итераций...")
        captures = []

        for i in range(iterations):
            logging.info(f"[FpTester] Итерация {i+1}/{iterations}")
            fp = self.capture()
            captures.append(fp)
            time.sleep(2)  # пауза между запусками

        # Сравниваем
        return self._compare(captures)

    def _compare(self, captures: list[dict]) -> dict:
        """Сравнивает список отпечатков и возвращает различия"""
        if len(captures) < 2:
            return {"error": "Нужно минимум 2 отпечатка"}

        baseline  = captures[0]
        diffs     = []
        stable    = []

        def flatten(d: dict, prefix: str = "") -> dict:
            """Разворачивает вложенный dict в плоский"""
            result = {}
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    result.update(flatten(v, key))
                elif isinstance(v, list):
                    result[key] = json.dumps(v, sort_keys=True)
                else:
                    result[key] = v
            return result

        base_flat = flatten(baseline)

        for key, base_val in base_flat.items():
            values_across = [base_val]
            for other in captures[1:]:
                other_flat = flatten(other)
                values_across.append(other_flat.get(key))

            if len(set(str(v) for v in values_across)) == 1:
                stable.append(key)
            else:
                # Для canvas/audio хэшируем для компактности
                if key in ("canvas", "audio"):
                    hashes = [
                        hashlib.md5(str(v).encode()).hexdigest()[:12] if v else "null"
                        for v in values_across
                    ]
                    diffs.append({"key": key, "values": hashes})
                else:
                    diffs.append({"key": key, "values": values_across})

        return {
            "iterations":     len(captures),
            "stable_count":   len(stable),
            "unstable_count": len(diffs),
            "stable_fields":  stable,
            "diffs":          diffs,
            "passed":         len(diffs) == 0,
        }

    def print_report(self, report: dict):
        print("\n" + "═" * 70)
        print(" FINGERPRINT PERSISTENCE TEST")
        print("═" * 70)
        print(f" Итераций:         {report.get('iterations')}")
        print(f" Стабильных полей: {report.get('stable_count')}")
        print(f" Нестабильных:     {report.get('unstable_count')}")

        if report.get("passed"):
            print(f"\n ✅ ТЕСТ ПРОЙДЕН — все параметры идентичны между запусками")
        else:
            print(f"\n ❌ ТЕСТ НЕ ПРОЙДЕН — найдены различия:")
            for diff in report.get("diffs", []):
                print(f"\n   {diff['key']}:")
                for i, v in enumerate(diff["values"]):
                    short = str(v)[:100] if v else "null"
                    print(f"     [{i+1}] {short}")

        print("═" * 70 + "\n")


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    profile_name = sys.argv[1] if len(sys.argv) > 1 else "profile_01"
    iterations   = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    tester = FingerprintTester(profile_name)
    report = tester.run(iterations=iterations)
    tester.print_report(report)

    # Сохраняем отчёт
    report_file = f"fp_test_{profile_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"Отчёт: {report_file}")
