"""
creepjs_check.py — Автоматическая проверка через CreepJS

CreepJS — самый продвинутый open-source антидетект-сканер.
abrahamjuliot.github.io/creepjs — даёт trust score и детальный разбор
всех найденных противоречий в отпечатке.

Этот скрипт открывает CreepJS, ждёт пока он всё просканирует,
извлекает результаты и сохраняет в JSON + делает скриншот.

Использование:
    python creepjs_check.py [profile_name]

Выдаёт:
    - Trust score (0-100%)
    - Найденные противоречия (lies)
    - Проблемные области фингерпринта
"""

# ── sys.path bootstrap ───────────────────────────────────────────
# Make `python scripts/foo.py` work when the CWD is the project root.
# When run via `python -m scripts.foo` from project root, this is a
# no-op (the project root is already on sys.path). We do NOT touch the
# caller's path if ghost_shell already imports — avoids shadowing when
# the user installed the package with `pip install -e .`.
import os as _os, sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import os
import time
import json
import logging
from datetime import datetime

from nk_browser import NKBrowser


CREEPJS_URL = "https://abrahamjuliot.github.io/creepjs/"


def check_creepjs(profile_name: str = "profile_01", proxy_str: str = None, wait_sec: int = 20) -> dict:
    """
    Прогоняет браузер через CreepJS и возвращает результаты.
    """
    logging.info("[CreepJS] Запускаем проверку...")

    result = {
        "profile":    profile_name,
        "timestamp":  datetime.now().isoformat(timespec="seconds"),
        "trust_score": None,
        "lies":        [],
        "fingerprint_id": None,
        "screenshot":  None,
        "error":       None,
    }

    with NKBrowser(
        profile_name = profile_name,
        proxy_str    = proxy_str,
        auto_session = False,
    ) as browser:

        driver = browser.driver

        try:
            driver.get(CREEPJS_URL)
            logging.info(f"[CreepJS] Ждём {wait_sec}с пока CreepJS просканирует...")
            time.sleep(wait_sec)

            # Извлекаем результаты через JS
            js = r"""
            const result = {};

            // Trust score
            const trustEl = document.querySelector('.trusted-fingerprint, .unrustworthy-fingerprint');
            if (trustEl) {
                const m = trustEl.textContent.match(/([\d.]+)\s*%/);
                if (m) result.trust = parseFloat(m[1]);
            }

            // Fingerprint ID
            const fpIdEl = document.querySelector('.fingerprint-header .unblurred');
            if (fpIdEl) result.fpId = fpIdEl.textContent.trim();

            // Lies — список противоречий
            result.lies = [];
            document.querySelectorAll('.lies-detection').forEach(el => {
                result.lies.push(el.textContent.trim().substring(0, 200));
            });

            // Все блоки с ошибками
            result.errors = [];
            document.querySelectorAll('.unblurred.erratic, .warn, .perf').forEach(el => {
                const t = el.textContent.trim();
                if (t && t.length < 500) result.errors.push(t);
            });

            // Общий текст первого блока — для отладки
            const main = document.querySelector('.fingerprint-header');
            result.summary = main ? main.textContent.trim().substring(0, 1000) : '';

            return result;
            """

            data = driver.execute_script(js) or {}
            result["trust_score"]    = data.get("trust")
            result["fingerprint_id"] = data.get("fpId")
            result["lies"]           = data.get("lies", [])
            result["errors"]         = data.get("errors", [])
            result["summary"]        = data.get("summary", "")

            # Скриншот для визуальной проверки
            os.makedirs("reports/creepjs", exist_ok=True)
            ss_path = os.path.join(
                "reports/creepjs",
                f"creepjs_{profile_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            )

            try:
                # Разворачиваем страницу для полного скрина
                total_h = driver.execute_script("return document.body.scrollHeight")
                driver.set_window_size(1280, min(total_h + 100, 5000))
                time.sleep(1)
                driver.save_screenshot(ss_path)
                result["screenshot"] = ss_path
            except Exception as e:
                logging.debug(f"[CreepJS] screenshot: {e}")

        except Exception as e:
            result["error"] = str(e)
            logging.error(f"[CreepJS] {e}")

    return result


def print_report(result: dict):
    print("\n" + "═" * 70)
    print(f" CREEPJS TRUST REPORT — {result['profile']}")
    print("═" * 70)

    trust = result.get("trust_score")
    if trust is not None:
        if trust >= 70:
            icon = "✅"
            verdict = "ХОРОШО"
        elif trust >= 40:
            icon = "⚠️ "
            verdict = "СРЕДНЕ"
        else:
            icon = "❌"
            verdict = "ПЛОХО"
        print(f"\n  {icon} Trust Score: {trust}%  ({verdict})")
    else:
        print(f"\n  ⚠ Trust score не удалось извлечь")

    if result.get("fingerprint_id"):
        print(f"  Fingerprint ID: {result['fingerprint_id'][:40]}")

    lies = result.get("lies", [])
    if lies:
        print(f"\n  🚩 Найдено противоречий (lies): {len(lies)}")
        for lie in lies[:10]:
            print(f"     • {lie[:120]}")

    errors = result.get("errors", [])
    if errors:
        print(f"\n  ⚠ Другие замечания: {len(errors)}")
        for err in errors[:5]:
            print(f"     • {err[:100]}")

    if result.get("screenshot"):
        print(f"\n  📸 Скриншот: {result['screenshot']}")

    if result.get("error"):
        print(f"\n  ❌ Ошибка: {result['error']}")

    print("═" * 70 + "\n")


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    profile_name = sys.argv[1] if len(sys.argv) > 1 else "profile_01"

    # Пробуем взять прокси из config
    proxy = None
    try:
        from ghost_shell.config import Config
        cfg = Config.load()
        proxy = cfg.get("proxy.url") or None
    except Exception:
        pass

    result = check_creepjs(profile_name, proxy_str=proxy)
    print_report(result)

    # Сохраняем
    os.makedirs("reports/creepjs", exist_ok=True)
    report_file = os.path.join(
        "reports/creepjs",
        f"creepjs_{profile_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"JSON отчёт: {report_file}")
