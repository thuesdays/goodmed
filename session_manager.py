"""
session_manager.py — Управление cookies, localStorage и сессией

Позволяет:
- Экспортировать cookies из активного браузера в JSON
- Импортировать cookies из JSON, Netscape-формата или реального Chrome
- Сохранять/загружать localStorage и sessionStorage
- Быстро "оживлять" профиль cookies из настоящего браузера
"""

import os
import json
import sqlite3
import shutil
import time
import logging
import tempfile
from datetime import datetime


class SessionManager:
    """
    Использование:
        sm = SessionManager(browser.driver)

        # Экспорт текущей сессии
        sm.export_cookies("sessions/my_session.json")
        sm.export_storage("sessions/my_session_storage.json")

        # Импорт
        sm.import_cookies("sessions/my_session.json")
        sm.import_storage("sessions/my_session_storage.json")

        # Импорт из реального Chrome
        sm.import_from_chrome(domain_filter=["google.com", "youtube.com"])
    """

    def __init__(self, driver):
        self.driver = driver

    # ──────────────────────────────────────────────────────────
    # COOKIES — EXPORT / IMPORT
    # ──────────────────────────────────────────────────────────

    def export_cookies(self, filepath: str) -> int:
        """Экспортирует все cookies текущей сессии в JSON"""
        cookies = self.driver.get_cookies()
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        logging.info(f"[Session] Экспортировано {len(cookies)} cookies → {filepath}")
        return len(cookies)

    def import_cookies(self, filepath: str, domain_filter: list = None) -> int:
        """
        Импортирует cookies из JSON.
        domain_filter — если указан, импортирует только для этих доменов
        """
        if not os.path.exists(filepath):
            logging.warning(f"[Session] Файл не найден: {filepath}")
            return 0

        with open(filepath, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        # Нужно сначала зайти на домен чтобы установить его cookies
        domains = set()
        for cookie in cookies:
            domain = cookie.get("domain", "").lstrip(".")
            if domain_filter and not any(d in domain for d in domain_filter):
                continue
            domains.add(domain)

        imported = 0
        for domain in domains:
            try:
                # Заходим на домен чтобы иметь право ставить его cookies
                self.driver.get(f"https://{domain}")
                time.sleep(1)

                for cookie in cookies:
                    cookie_domain = cookie.get("domain", "").lstrip(".")
                    if cookie_domain != domain:
                        continue
                    if domain_filter and not any(d in cookie_domain for d in domain_filter):
                        continue

                    # Selenium не принимает некоторые поля
                    clean_cookie = {
                        k: v for k, v in cookie.items()
                        if k in ("name", "value", "domain", "path", "secure", "httpOnly", "expiry", "sameSite")
                    }
                    # expiry должен быть int
                    if "expiry" in clean_cookie:
                        clean_cookie["expiry"] = int(clean_cookie["expiry"])

                    try:
                        self.driver.add_cookie(clean_cookie)
                        imported += 1
                    except Exception as e:
                        logging.debug(f"[Session] Не удалось импортировать {cookie.get('name')}: {e}")

            except Exception as e:
                logging.debug(f"[Session] Ошибка импорта для {domain}: {e}")

        logging.info(f"[Session] Импортировано {imported} cookies")
        return imported

    # ──────────────────────────────────────────────────────────
    # LOCALSTORAGE / SESSIONSTORAGE
    # ──────────────────────────────────────────────────────────

    def export_storage(self, filepath: str) -> dict:
        """Экспортирует localStorage текущего домена"""
        try:
            local  = self.driver.execute_script("return Object.assign({}, localStorage);")
            session = self.driver.execute_script("return Object.assign({}, sessionStorage);")
        except Exception as e:
            logging.error(f"[Session] Ошибка чтения storage: {e}")
            return {}

        data = {
            "url":           self.driver.current_url,
            "localStorage":  local,
            "sessionStorage": session,
            "timestamp":     datetime.now().isoformat(),
        }
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logging.info(f"[Session] Storage экспортирован ({len(local)} local + {len(session)} session)")
        return data

    def import_storage(self, filepath: str, navigate_first: bool = True) -> int:
        """Импортирует localStorage/sessionStorage. Важно — на правильном домене."""
        if not os.path.exists(filepath):
            return 0
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if navigate_first and data.get("url"):
            self.driver.get(data["url"])
            time.sleep(1)

        count = 0
        for key, value in (data.get("localStorage") or {}).items():
            try:
                self.driver.execute_script(
                    "localStorage.setItem(arguments[0], arguments[1]);", key, value
                )
                count += 1
            except Exception:
                pass
        for key, value in (data.get("sessionStorage") or {}).items():
            try:
                self.driver.execute_script(
                    "sessionStorage.setItem(arguments[0], arguments[1]);", key, value
                )
                count += 1
            except Exception:
                pass
        logging.info(f"[Session] Импортировано {count} элементов storage")
        return count

    # ──────────────────────────────────────────────────────────
    # ИМПОРТ ИЗ РЕАЛЬНОГО CHROME
    # ──────────────────────────────────────────────────────────

    def import_from_chrome(
        self,
        domain_filter: list = None,
        chrome_profile_path: str = None,
    ) -> int:
        """
        Импортирует cookies из установленного Chrome на этом ПК.
        ВНИМАНИЕ: Chrome должен быть ЗАКРЫТ чтобы БД не была залочена.

        domain_filter — список доменов для импорта (напр. ["google.com", "youtube.com"])
        """
        if chrome_profile_path is None:
            # Стандартный путь Chrome на Windows
            appdata = os.environ.get("LOCALAPPDATA", "")
            chrome_profile_path = os.path.join(
                appdata, "Google", "Chrome", "User Data", "Default"
            )

        cookies_db = os.path.join(chrome_profile_path, "Network", "Cookies")
        if not os.path.exists(cookies_db):
            cookies_db = os.path.join(chrome_profile_path, "Cookies")  # старый путь

        if not os.path.exists(cookies_db):
            logging.error(f"[Session] БД cookies Chrome не найдена: {cookies_db}")
            return 0

        # Копируем БД во временный файл — не трогаем оригинал
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp_path = tmp.name
        try:
            shutil.copy2(cookies_db, tmp_path)
        except PermissionError:
            logging.error("[Session] Не удалось скопировать БД — Chrome открыт?")
            return 0

        cookies = []
        try:
            conn = sqlite3.connect(tmp_path)
            cur  = conn.cursor()
            query = """
                SELECT host_key, name, value, path, expires_utc, is_secure, is_httponly, samesite
                FROM cookies
            """
            for row in cur.execute(query):
                domain = row[0].lstrip(".")
                if domain_filter and not any(d in domain for d in domain_filter):
                    continue

                # Chrome хранит encrypted_value отдельно — в этом примере только незашифрованные
                # Для полного импорта нужна расшифровка через DPAPI (Windows)
                if not row[2]:
                    continue  # пропускаем зашифрованные

                # expires_utc — microseconds с 1601-01-01
                # Переводим в Unix timestamp
                expiry = None
                if row[4]:
                    expiry = int(row[4] / 1_000_000 - 11644473600)
                    if expiry <= 0:
                        expiry = None

                samesite_map = {0: "None", 1: "Lax", 2: "Strict"}
                cookie = {
                    "domain":   row[0],
                    "name":     row[1],
                    "value":    row[2],
                    "path":     row[3],
                    "secure":   bool(row[5]),
                    "httpOnly": bool(row[6]),
                    "sameSite": samesite_map.get(row[7], "Lax"),
                }
                if expiry:
                    cookie["expiry"] = expiry
                cookies.append(cookie)

            conn.close()
        finally:
            try: os.remove(tmp_path)
            except: pass

        if not cookies:
            logging.warning("[Session] Не нашли незашифрованных cookies для импорта")
            logging.warning("          Chrome шифрует cookies через DPAPI — нужна отдельная расшифровка")
            return 0

        # Сохраняем и импортируем
        tmp_json = tempfile.mktemp(suffix=".json")
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False)
        try:
            imported = self.import_cookies(tmp_json, domain_filter=domain_filter)
        finally:
            try: os.remove(tmp_json)
            except: pass
        return imported

    # ──────────────────────────────────────────────────────────
    # СОХРАНЕНИЕ ПОЛНОЙ СЕССИИ
    # ──────────────────────────────────────────────────────────

    def save_full_session(self, directory: str):
        """Сохраняет cookies + storage + текущий URL в папку"""
        os.makedirs(directory, exist_ok=True)
        self.export_cookies(os.path.join(directory, "cookies.json"))
        self.export_storage(os.path.join(directory, "storage.json"))
        info = {
            "url":       self.driver.current_url,
            "title":     self.driver.title,
            "timestamp": datetime.now().isoformat(),
        }
        with open(os.path.join(directory, "info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
        logging.info(f"[Session] Полная сессия сохранена: {directory}")

    def restore_full_session(self, directory: str):
        """Восстанавливает cookies + storage из папки"""
        if not os.path.exists(directory):
            raise ValueError(f"Папка сессии не найдена: {directory}")
        self.import_cookies(os.path.join(directory, "cookies.json"))
        self.import_storage(os.path.join(directory, "storage.json"))
        logging.info(f"[Session] Сессия восстановлена из {directory}")
