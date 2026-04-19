"""
main.py — Мониторинг контекстной рекламы конкурентов по брендовым запросам

Что делает:
1. Открывает Google через антидетект-браузер
2. Ищет брендовые запросы goodmedika по очереди
3. На странице результатов извлекает все рекламные блоки
4. Собирает список уникальных конкурентов (не goodmedika.com.ua)
5. Выводит итоговый отчёт со ссылками
"""

import os
import re
import time
import random
import logging
import json
import requests
from datetime import datetime
from urllib.parse import urlparse, parse_qs, unquote
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from nk_browser import NKBrowser
from proxy_diagnostics import ProxyDiagnostics

# ──────────────────────────────────────────────────────────────
# НАСТРОЙКИ
# ──────────────────────────────────────────────────────────────

SEARCH_QUERIES     = ["гудмедика", "гудмедіка", "goodmedika"]
MY_DOMAINS         = ["goodmedika.com.ua", "goodmedika.ua", "goodmedika.com"]
TWOCAPTCHA_API_KEY = "6304d6e97726df713271b9de0ca2d653"
PROXY              = "01kpjw4p1mrn74xw7eq1sd843q:nfoN0DTTFaoUizWj@109.236.84.23:16720"
PROFILE_NAME       = "profile_01"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ──────────────────────────────────────────────────────────────
# СОСТОЯНИЕ СТРАНИЦЫ
# ──────────────────────────────────────────────────────────────

def page_state(driver) -> str:
    try:
        url = driver.current_url
    except Exception:
        return "dead"
    if "sorry/index" in url or "/sorry/" in url:
        return "captcha"
    if "consent.google.com" in url:
        return "consent"
    if "/search" in url and "q=" in url:
        return "search_results"
    if url.startswith("https://www.google.") or url == "about:blank":
        return "home"
    return "other"


def bypass_consent(driver):
    if page_state(driver) != "consent":
        return
    logging.info("🍪 Принимаем куки...")
    try:
        btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//*[contains(text(),'Принять все') or contains(text(),'Accept all') or contains(text(),'Прийняти все')]"
            ))
        )
        btn.click()
        time.sleep(random.uniform(2, 4))
    except Exception:
        pass


def solve_captcha(driver) -> bool:
    if page_state(driver) != "captcha":
        return True
    if TWOCAPTCHA_API_KEY == "ВАШ_КЛЮЧ":
        logging.warning("2Captcha API ключ не задан")
        return False
    logging.info("⚠️ Капча — решаем через 2Captcha...")
    try:
        wait = WebDriverWait(driver, 10)
        el = wait.until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "div.g-recaptcha, div[data-sitekey]"
        )))
        sitekey = el.get_attribute("data-sitekey") or el.get_attribute("data-s")
        if not sitekey:
            return False
        create = requests.get(
            f"https://2captcha.com/in.php?key={TWOCAPTCHA_API_KEY}"
            f"&method=userrecaptcha&googlekey={sitekey}&pageurl={driver.current_url}&json=1"
        ).json()
        if create.get("status") != 1:
            return False
        task_id = create["request"]
        time.sleep(20)
        for _ in range(24):
            poll = requests.get(
                f"https://2captcha.com/res.php?key={TWOCAPTCHA_API_KEY}"
                f"&action=get&id={task_id}&json=1"
            ).json()
            if poll.get("status") == 1:
                token = poll["request"]
                driver.execute_script(
                    "document.getElementById('g-recaptcha-response').value = arguments[0];",
                    token
                )
                time.sleep(5)
                return True
            time.sleep(5)
    except Exception as e:
        logging.error(f"Капча: {e}")
    return False


# ──────────────────────────────────────────────────────────────
# ИЗВЛЕЧЕНИЕ РЕКЛАМНЫХ БЛОКОВ
# ──────────────────────────────────────────────────────────────

def extract_real_url(href: str) -> str:
    """
    Google оборачивает рекламные ссылки в редиректы вида
    https://www.googleadservices.com/pagead/aclk?adurl=...&url=https%3A%2F%2Freal.com
    Достаём реальный URL.
    """
    if not href:
        return ""
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        # Разные варианты названий параметра с реальным URL
        for key in ("adurl", "url", "q"):
            if key in qs:
                real = unquote(qs[key][0])
                if real.startswith("http"):
                    return real
    except Exception:
        pass
    return href


def extract_domain(url: str) -> str:
    """Извлекает домен из URL в чистом виде"""
    if not url:
        return ""
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def parse_ads(driver, query: str) -> list[dict]:
    """
    Извлекает ТОЛЬКО рекламные блоки со страницы результатов.

    Стратегия поиска (от самой надёжной к менее):
    1. Элементы с текстом 'Sponsored' / 'Реклама' / 'Спонсировано' / 'Спонсоване'
       — Google обязан помечать рекламу по закону, это 100% маркер
    2. Блоки с атрибутом data-text-ad="1" — если Google не переименует
    3. Ссылки через googleadservices.com/aclk — классические редиректы рекламы
    """
    state = page_state(driver)
    if state != "search_results":
        logging.warning(f"  Не на странице результатов: state={state}")
        return []

    # Собираем рекламные блоки через JS
    # Ищем элементы содержащие метку "Sponsored" / "Реклама" и поднимаемся
    # вверх по DOM пока не найдём блок с ссылкой
    js_script = """
    const SPONSORED_MARKERS = [
        'Sponsored', 'Реклама', 'Спонсировано', 'Спонсоване',
        'Anuncio', 'Annonce', 'Werbung', 'Annuncio'
    ];

    const adBlocks = new Set();

    // Способ 1: ищем по тексту метки "Sponsored" / "Реклама"
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
    let node;
    while (node = walker.nextNode()) {
        const text = (node.textContent || '').trim();
        // Текст элемента должен быть ТОЛЬКО меткой, не длиннее 20 символов
        if (text.length < 20 && SPONSORED_MARKERS.some(m => text === m || text.startsWith(m))) {
            // Поднимаемся вверх пока не найдём блок с ссылкой вне googleadservices
            let parent = node;
            for (let i = 0; i < 8 && parent; i++) {
                parent = parent.parentElement;
                if (!parent) break;
                const link = parent.querySelector('a[href]');
                if (link && link.href && link.href.startsWith('http')) {
                    adBlocks.add(parent);
                    break;
                }
            }
        }
    }

    // Способ 2: data-text-ad атрибут (дополнительно)
    document.querySelectorAll('div[data-text-ad]').forEach(el => adBlocks.add(el));

    // Собираем данные из каждого блока
    const results = [];
    adBlocks.forEach(block => {
        // Заголовок — role=heading или первый div с большим текстом
        let title = '';
        const heading = block.querySelector('[role="heading"], h3');
        if (heading) title = heading.textContent.trim();

        // Видимый URL — cite или специальный span
        let displayUrl = '';
        const cite = block.querySelector('cite, span.VuuXrf, span.x2VHCd, span[role="text"]');
        if (cite) displayUrl = cite.textContent.trim();

        // Ссылка — ищем первую HTTP-ссылку которая не на google
        let href = '';
        for (const link of block.querySelectorAll('a[href]')) {
            const h = link.href;
            if (h && h.startsWith('http')) {
                href = h;
                break;
            }
        }

        if (href || displayUrl) {
            results.push({
                title: title,
                displayUrl: displayUrl,
                href: href,
                html: block.outerHTML.slice(0, 500)  // для отладки
            });
        }
    });

    return results;
    """

    try:
        raw_ads = driver.execute_script(js_script) or []
    except Exception as e:
        logging.warning(f"  Ошибка JS-парсинга: {e}")
        return []

    logging.info(f"  Рекламных блоков найдено: {len(raw_ads)}")

    ads = []
    seen_domains = set()

    for raw in raw_ads:
        try:
            href        = raw.get("href", "")
            display_url = raw.get("displayUrl", "")
            title       = raw.get("title", "")

            real_url = extract_real_url(href)
            domain   = extract_domain(real_url) or extract_domain(display_url)

            if not domain:
                continue

            # Пропускаем внутренние домены Google (если метка "Реклама" случайно
            # попалась рядом с ссылкой на Google Maps и т.п.)
            if any(g in domain for g in ("google.com", "google.ua", "googleusercontent.com")):
                continue

            # Пропускаем наш собственный домен
            if any(my in domain for my in MY_DOMAINS):
                logging.info(f"  · [наш] {domain} — {title[:50]}")
                continue

            # Дедупликация в рамках одного запроса
            if domain in seen_domains:
                continue
            seen_domains.add(domain)

            ads.append({
                "query":       query,
                "title":       title,
                "display_url": display_url,
                "real_url":    real_url,
                "domain":      domain,
                "found_at":    datetime.now().isoformat(timespec="seconds"),
            })
            logging.info(f"  ✓ {domain} — {title[:60]}")

        except Exception as e:
            logging.debug(f"  Ошибка обработки блока: {e}")

    return ads


# ──────────────────────────────────────────────────────────────
# ОСНОВНАЯ ЛОГИКА
# ──────────────────────────────────────────────────────────────

def run_monitor():
    # Глобальный словарь всех найденных конкурентов: domain → {data}
    competitors: dict[str, dict] = {}

    with NKBrowser(
        profile_name    = PROFILE_NAME,
        proxy_str       = PROXY,
        device_template = "office_laptop",
        auto_session    = True,
    ) as browser:

        driver = browser.driver
        browser.setup_profile_logging()

        # ── 1. Проверки ──────────────────────────────
        browser.health_check(verbose=True)

        diag   = ProxyDiagnostics(driver)
        report = diag.full_check(expected_timezone="Europe/Kyiv")
        diag.print_report(report)

        if report["webrtc_leak"]:
            logging.error("✗ WebRTC УТЕЧКА — останавливаемся")
            return

        browser.enable_request_blocking()

        # ── 2. Прогрев только если профиль новый ─────
        fp_exists = os.path.exists(os.path.join(browser.user_data_path, "fingerprint.json"))
        session_exists = os.path.exists(browser.session_dir)

        if not session_exists:
            logging.info("📥 Новый профиль — прогреваем один раз")
            browser.warmup_profile(depth="medium")
        else:
            logging.info("✓ Сессия восстановлена — прогрев пропущен")

        # ── 3. Идём на Google ───────────────────────
        browser.stealth_get("https://www.google.com")
        time.sleep(random.uniform(3, 6))
        bypass_consent(driver)

        if not solve_captcha(driver):
            logging.warning("Капча на входе не решена")

        # ── 4. ЦИКЛ ПОИСКА И СБОРА РЕКЛАМЫ ─────────
        for i, query in enumerate(SEARCH_QUERIES):
            if not browser.is_alive():
                logging.error("Окно закрыто — выходим")
                break

            logging.info("")
            logging.info(f"{'='*60}")
            logging.info(f"🔎 Запрос {i+1}/{len(SEARCH_QUERIES)}: {query}")
            logging.info(f"{'='*60}")

            # Возвращаемся на главную для чистого поиска
            if i > 0:
                try:
                    driver.get("https://www.google.com")
                    time.sleep(random.uniform(2, 4))
                    bypass_consent(driver)
                except Exception:
                    if not browser.is_alive():
                        break
                    continue

            # Проверяем не подсунули ли капчу
            bypass_consent(driver)
            if page_state(driver) == "captcha":
                if not solve_captcha(driver):
                    logging.warning(f"Запрос '{query}' пропущен (капча)")
                    time.sleep(random.uniform(30, 60))
                    continue

            # Вводим запрос
            def do_search():
                search_box = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.NAME, "q"))
                )
                WebDriverWait(driver, 5).until(EC.visibility_of(search_box))

                # Клик на поле
                browser.bezier_move_to(search_box)
                time.sleep(random.uniform(0.4, 0.9))

                # Подстраховка — проверяем что поле в фокусе
                focused = driver.execute_script(
                    "return document.activeElement === arguments[0];",
                    search_box
                )
                if not focused:
                    logging.warning("  Поле не в фокусе — делаем focus через JS")
                    driver.execute_script("arguments[0].focus();", search_box)
                    time.sleep(0.3)

                # Очищаем поле
                search_box.send_keys(Keys.CONTROL + "a")
                search_box.send_keys(Keys.BACKSPACE)
                time.sleep(random.uniform(0.3, 0.7))

                # Печатаем запрос
                browser.human_type(search_box, query)
                time.sleep(random.uniform(0.5, 1.2))

                # Проверка что текст реально попал в поле
                typed_value = driver.execute_script(
                    "return arguments[0].value;", search_box
                )
                if query not in typed_value:
                    logging.warning(f"  Текст не попал в поле (в поле: '{typed_value}'). Пробуем через JS")
                    driver.execute_script(
                        "arguments[0].value = arguments[1]; "
                        "arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                        search_box, query
                    )
                    time.sleep(0.5)

                search_box.send_keys(Keys.RETURN)

            try:
                browser.safe_execute(
                    do_search,
                    description=f"search_{query}",
                    retries=2,
                    screenshot_on_fail=browser.is_alive()
                )
            except Exception as e:
                logging.error(f"Поиск '{query}' провален: {type(e).__name__}")
                if not browser.is_alive():
                    break
                continue

            time.sleep(random.uniform(4, 7))

            # Капча после поиска
            if page_state(driver) == "captcha":
                logging.warning("Капча после поиска")
                if not solve_captcha(driver):
                    time.sleep(random.uniform(30, 60))
                    continue

            # Небольшой скролл — имитация просмотра
            browser.human_scroll(1, 2)
            time.sleep(random.uniform(1, 2))

            # Извлекаем рекламу
            ads = parse_ads(driver, query)

            for ad in ads:
                domain = ad["domain"]
                if domain in competitors:
                    # Уже видели — добавляем запрос в список
                    if query not in competitors[domain]["queries"]:
                        competitors[domain]["queries"].append(query)
                else:
                    competitors[domain] = {
                        "domain":      domain,
                        "title":       ad["title"],
                        "display_url": ad["display_url"],
                        "real_url":    ad["real_url"],
                        "queries":     [query],
                        "first_seen":  ad["found_at"],
                    }

                browser.stealth_get(ad["real_url"])
                time.sleep(random.uniform(12, 20))
                bypass_consent(driver)

            if not ads:
                logging.info("  (реклама не найдена)")

            time.sleep(random.uniform(5, 10))


        # ── 5. ИТОГОВЫЙ ОТЧЁТ ─────────────────────────
        print_report(competitors)
        save_report(competitors)


# ──────────────────────────────────────────────────────────────
# ОТЧЁТ
# ──────────────────────────────────────────────────────────────

def print_report(competitors: dict):
    logging.info("")
    logging.info("╔" + "═" * 68 + "╗")
    logging.info("║" + f" ИТОГОВЫЙ ОТЧЁТ — КОНКУРЕНТЫ В КОНТЕКСТНОЙ РЕКЛАМЕ ".center(68) + "║")
    logging.info("╚" + "═" * 68 + "╝")

    if not competitors:
        logging.info("Конкурентов не обнаружено.")
        return

    logging.info(f"Найдено уникальных рекламодателей: {len(competitors)}")
    logging.info("")

    # Сортируем по количеству запросов (чаще встречающиеся наверху)
    sorted_items = sorted(
        competitors.values(),
        key=lambda c: (-len(c["queries"]), c["domain"])
    )

    for i, c in enumerate(sorted_items, 1):
        logging.info(f"[{i}] {c['domain']}")
        if c["title"]:
            logging.info(f"    Заголовок: {c['title']}")
        if c["display_url"]:
            logging.info(f"    Display:   {c['display_url']}")
        if c["real_url"]:
            logging.info(f"    URL:       {c['real_url']}")
        logging.info(f"    Запросы:   {', '.join(c['queries'])}")
        logging.info("")


def save_report(competitors: dict):
    """Сохраняет отчёт в JSON и CSV"""
    if not competitors:
        return

    reports_dir = "reports"
    os.makedirs(reports_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON
    json_path = os.path.join(reports_dir, f"competitors_{timestamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(list(competitors.values()), f, indent=2, ensure_ascii=False)
    logging.info(f"📄 JSON отчёт: {json_path}")

    # CSV
    csv_path = os.path.join(reports_dir, f"competitors_{timestamp}.csv")
    with open(csv_path, "w", encoding="utf-8-sig") as f:  # BOM для Excel
        f.write("Домен;Заголовок;Display URL;Real URL;Запросы;Впервые замечен\n")
        for c in competitors.values():
            row = [
                c["domain"],
                (c["title"] or "").replace(";", ","),
                (c["display_url"] or "").replace(";", ","),
                (c["real_url"] or "").replace(";", ","),
                "|".join(c["queries"]),
                c["first_seen"],
            ]
            f.write(";".join(row) + "\n")
    logging.info(f"📊 CSV отчёт: {csv_path}")


if __name__ == "__main__":
    run_monitor()
