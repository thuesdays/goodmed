"""
cookie_warmer.py — Мгновенный прогрев via готовые cookies

Вместо real посещений сайтов (2-5 минут) устанавливает via CDP
правдоподобные cookies that характерны for активного browserа:

- Consent cookies Google/YouTube (as будто юзер onнял согласие earlier)
- NID cookie Google (идентификатор sessions)
- Preference cookies (языки, регион)
- localStorage for крупных сайтов

Это даёт тот же сигнал "я here already бывал" without траты времени на real прогрев.

ВАЖНО: cookies here сгенерированы по correctlyму формату но со случайными
значениями — они не валидны for авторизации. Они работают as "onсутствие",
а не "авторизация". Google видит that browser имеет историю настроек.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import time
import random
import logging
import string
from datetime import datetime, timezone


def _random_string(length: int, alphabet: str = None) -> str:
    alphabet = alphabet or string.ascii_letters + string.digits + "-_"
    return "".join(random.choices(alphabet, k=length))


def _future_timestamp(days: int) -> int:
    """Unix timestamp via N дней"""
    return int(time.time() + days * 86400)


# ──────────────────────────────────────────────────────────────
# ШАБЛОНЫ COOKIES ДЛЯ КРУПНЫХ САЙТОВ
# ──────────────────────────────────────────────────────────────

def google_cookies() -> list[dict]:
    """
    Cookies that устанавливает Google for активного юзера.
    Имитируем профиль that already earlier заходил на google.com.
    """
    return [
        # Consent — был onнят asое-то время назад
        {
            "name":   "CONSENT",
            "value":  f"YES+cb.{datetime.now().strftime('%Y%m%d')}-{random.randint(10,17)}-p0.uk+FX+{random.randint(100,999)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365 * 2),
        },
        {
            "name":   "SOCS",
            "value":  f"CAISHAgCEhJnd3NfMjAyN{_random_string(10)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365),
        },
        # NID — основной session cookie Google
        {
            "name":   "NID",
            "value":  f"511={_random_string(180)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            "expiry": _future_timestamp(180),
        },
        # 1P_JAR — один из трекинговых
        {
            "name":   "1P_JAR",
            "value":  f"{datetime.now().strftime('%Y-%m-%d')}-{random.randint(0,23)}",
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "sameSite": "None",
            "expiry": _future_timestamp(30),
        },
        # AEC — still один consent-related
        {
            "name":   "AEC",
            "value":  _random_string(80),
            "domain": ".google.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
            "expiry": _future_timestamp(180),
        },
    ]


def youtube_cookies() -> list[dict]:
    """YouTube consent + preferences"""
    return [
        {
            "name":   "CONSENT",
            "value":  f"YES+cb.{datetime.now().strftime('%Y%m%d')}-{random.randint(10,17)}-p0.uk+FX+{random.randint(100,999)}",
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365 * 2),
        },
        {
            "name":   "VISITOR_INFO1_LIVE",
            "value":  _random_string(22),
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            "expiry": _future_timestamp(180),
        },
        {
            "name":   "YSC",
            "value":  _random_string(16),
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "httpOnly": True,
            # Session cookie — without expiry
        },
        {
            "name":   "PREF",
            "value":  f"tz=Europe.Kiev&f6=400&hl=uk",
            "domain": ".youtube.com",
            "path":   "/",
            "secure": True,
            "expiry": _future_timestamp(365 * 2),
        },
    ]


def common_analytics_cookies() -> list[dict]:
    """Google Analytics + общие трекеры — these is почти у each"""
    ga_id = f"GA1.2.{random.randint(1000000000, 9999999999)}.{int(time.time()) - random.randint(86400*7, 86400*60)}"
    return [
        {
            "name":   "_ga",
            "value":  ga_id,
            "domain": ".google.com",
            "path":   "/",
            "expiry": _future_timestamp(365 * 2),
        },
        {
            "name":   "_gid",
            "value":  f"GA1.2.{random.randint(1000000000, 9999999999)}.{int(time.time()) - random.randint(0, 86400)}",
            "domain": ".google.com",
            "path":   "/",
            "expiry": _future_timestamp(1),
        },
    ]


# ──────────────────────────────────────────────────────────────
# ИНЖЕКТОР
# ──────────────────────────────────────────────────────────────

class CookieWarmer:
    """
    Usage:
        warmer = CookieWarmer(browser.driver)
        warmer.fast_warmup()   # 5-10 секунд instead of 2-5 минут
    """

    def __init__(self, driver):
        self.driver = driver

    def _inject_cookies_via_cdp(self, cookies: list[dict]):
        """Устанавливает cookies via CDP Network.setCookie — without посещения страницы"""
        # Включаем Network domain
        try:
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

        injected = 0
        for c in cookies:
            try:
                params = {
                    "name":   c["name"],
                    "value":  c["value"],
                    "domain": c["domain"],
                    "path":   c.get("path", "/"),
                    "secure": c.get("secure", False),
                    "httpOnly": c.get("httpOnly", False),
                }
                if "sameSite" in c:
                    params["sameSite"] = c["sameSite"]
                if "expiry" in c:
                    params["expires"] = c["expiry"]

                self.driver.execute_cdp_cmd("Network.setCookie", params)
                injected += 1
            except Exception as e:
                logging.debug(f"[CookieWarmer] Не удалось установить {c['name']}: {e}")
        return injected

    def fast_warmup(self):
        """
        Быстрый прогрев: устанавливаем cookies without real посещений.
        Overнимает 3-5 секунд instead of 2-5 минут.
        """
        logging.info("[CookieWarmer] ⚡ Быстрый прогрев via cookies...")
        started = time.time()

        all_cookies = (
            google_cookies() +
            youtube_cookies() +
            common_analytics_cookies()
        )

        count = self._inject_cookies_via_cdp(all_cookies)

        # Также добавляем записи в localStorage for Google/YouTube
        # Это делается via посещение — но очень короткое
        self._seed_local_storage()

        duration = time.time() - started
        logging.info(f"[CookieWarmer] ✓ Installed {count} cookies in {duration:.1f}с")

    def _seed_local_storage(self):
        """Overсеиваем localStorage — only на текущей странице (without переходов)"""
        try:
            # Мы не будем прыгать по доменам, просто посеем базовые вещи
            # if мы already на google.com
            if "google.com" in self.driver.current_url:
                data = {
                    "_grecaptcha":            _random_string(30),
                    "google_experiment_mod":  str(random.randint(1000, 9999)),
                }
                for key, value in data.items():
                    self.driver.execute_script(
                        "try { localStorage.setItem(arguments[0], arguments[1]); } catch(e) {}",
                        key, value
                    )
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # ГИБРИДНЫЙ ПРОГРЕВ — fast + короткие посещения
    # ──────────────────────────────────────────────────────────

    def hybrid_warmup(self, short_visits: bool = True):
        """
        Гибридный: сначала cookies, then 1-2 коротких real посещения
        for максимальной достоверности. Overнимает 20-30 секунд.
        """
        self.fast_warmup()

        if not short_visits:
            return

        logging.info("[CookieWarmer] Supplementing with short visits...")

        # Первым делом идём на google.com — он самый "тёплый" (cookies already is)
        # и с него проверяем that сеть вообще works
        quick_sites = [
            "https://www.google.com/",
            "https://www.youtube.com/",
        ]

        for url in quick_sites[:2]:
            try:
                self.driver.get(url)
                # Waiting loading document (не networkidle — слишком строго)
                self._wait_page_ready(timeout=15)

                # Проверяем that не whileзалась офлайн-страница
                if self._is_offline_page():
                    logging.warning(f"[CookieWarmer] {url} whileзал офлайн — пропускаем визиты")
                    # Возвращаемся на blank тотбы не оставлять офлайн-страницу
                    try:
                        self.driver.get("about:blank")
                    except Exception:
                        pass
                    return

                time.sleep(random.uniform(3, 5))
                # Небольшой скролл
                try:
                    self.driver.execute_script(f"window.scrollBy(0, {random.randint(200, 500)});")
                except Exception:
                    pass
                time.sleep(random.uniform(1, 2))
            except Exception as e:
                logging.debug(f"[CookieWarmer] {url}: {e}")

    def _wait_page_ready(self, timeout: int = 15):
        """Waiting document.readyState === complete"""
        started = time.time()
        while time.time() - started < timeout:
            try:
                state = self.driver.execute_script("return document.readyState;")
                if state == "complete":
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _is_offline_page(self) -> bool:
        """Проверяет that Chrome не whileзал офлайн-страницу"""
        try:
            # Офлайн-страницы Chrome имеют специфичный title or текст
            title = (self.driver.title or "").lower()
            if any(marker in title for marker in ("офлайн", "offline", "недоступно")):
                return True
            # Или специфичный class на body
            body_text = self.driver.execute_script(
                "return (document.body && document.body.innerText || '').substring(0, 200).toLowerCase();"
            )
            offline_markers = [
                "підключіться до інтернеу", "connect to the internet",
                "в режимі офлайн", "you're offline", "you are offline",
                "no соединения", "подключитесь к интернеу",
            ]
            return any(m in body_text for m in offline_markers)
        except Exception:
            return False
