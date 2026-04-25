"""
session_manager.py — Cookie and storage I/O for a Selenium driver.

Main responsibilities (kept intentionally small):
  - export_cookies() / import_cookies() — session portability via JSON
  - export_storage() / import_storage() — localStorage + sessionStorage
  - import_from_chrome() — one-shot copy of cookies from the installed
    real Chrome profile (useful to warm a fresh Ghost Shell profile with
    a known-good Google session)

Note: The full round-trip save/restore on browser open/close is handled
by GhostShellBrowser._auto_save_session / _restore_session. This module
is useful for manual operations and the "seed from real Chrome" flow.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import shutil
import time
import logging
import tempfile
import sqlite3
from datetime import datetime


class SessionManager:
    """
    Usage:
        sm = SessionManager(browser.driver)

        # Export current session
        sm.export_cookies("sessions/my_session.json")
        sm.export_storage("sessions/my_session_storage.json")

        # Import later
        sm.import_cookies("sessions/my_session.json")
        sm.import_storage("sessions/my_session_storage.json")

        # Seed from real Chrome (Chrome MUST be closed — DB is locked otherwise)
        sm.import_from_chrome(domain_filter=["google.com", "youtube.com"])
    """

    def __init__(self, driver):
        self.driver = driver

    # ─── Cookies ───────────────────────────────────────────

    def export_cookies(self, filepath: str) -> int:
        cookies = self.driver.get_cookies()
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2, ensure_ascii=False)
        logging.info(f"[Session] Exported {len(cookies)} cookies → {filepath}")
        return len(cookies)

    def import_cookies(self, filepath: str, domain_filter: list = None) -> int:
        """Import cookies from JSON into Chrome via CDP Network.setCookie.
        No navigation required — writes directly to the cookie jar.
        Zero proxy traffic, zero flake from per-domain page loads."""
        if not os.path.exists(filepath):
            logging.warning(f"[Session] File not found: {filepath}")
            return 0

        with open(filepath, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        imported = 0
        # STRATEGY: use CDP Network.setCookie instead of Selenium's
        # driver.add_cookie(). Selenium requires the browser to be
        # currently navigated to the cookie's domain before add_cookie()
        # is accepted — which means we were doing 1 full navigation per
        # domain (6 cookies → 6 page loads through the proxy). Those
        # navigations sometimes hang (proxy flake, CAPTCHA, nav timeout)
        # and if ANY of them kills the session via navigation error,
        # the rest of the run dies with "invalid session id".
        #
        # CDP Network.setCookie doesn't need the browser to be ON the
        # domain — it writes directly into Chrome's cookie jar. Also
        # it's free: zero network traffic, zero proxy usage, zero risk
        # of triggering anti-bot heuristics on cookie-restore.
        try:
            # Make sure Network domain is enabled — it usually is (we
            # call Network.enable() earlier in start()), but setting
            # again is cheap and idempotent.
            self.driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass

        for c in cookies:
            if domain_filter and not any(f in (c.get("domain") or "") for f in domain_filter):
                continue

            # Build a CDP Network.setCookie param shape. Differs from
            # Selenium's add_cookie shape — notably `expires` (not
            # expiry), and sameSite must be one of Strict/Lax/None
            # exactly, or omitted.
            cdp_params = {
                "name":   c.get("name"),
                "value":  c.get("value", ""),
                "domain": (c.get("domain") or "").lstrip("."),
                "path":   c.get("path") or "/",
            }
            if c.get("secure"):    cdp_params["secure"]   = True
            if c.get("httpOnly"): cdp_params["httpOnly"]  = True
            # Expiry: Selenium uses Unix epoch seconds. CDP too.
            exp = c.get("expiry") or c.get("expires")
            if exp:
                try:
                    cdp_params["expires"] = int(exp)
                except (ValueError, TypeError):
                    pass
            ss = c.get("sameSite")
            if ss in ("Strict", "Lax", "None"):
                cdp_params["sameSite"] = ss

            if not cdp_params["name"] or not cdp_params["domain"]:
                continue

            try:
                self.driver.execute_cdp_cmd("Network.setCookie", cdp_params)
                imported += 1
            except Exception as e:
                logging.debug(f"[Session] CDP setCookie failed for {c.get('name')}: {e}")

        logging.info(f"[Session] Imported {imported} cookies (via CDP, no navigation)")
        return imported

    # ─── localStorage / sessionStorage ────────────────────

    def export_storage(self, filepath: str) -> dict:
        try:
            local   = self.driver.execute_script("return Object.assign({}, localStorage);")
            session = self.driver.execute_script("return Object.assign({}, sessionStorage);")
        except Exception as e:
            logging.error(f"[Session] storage read error: {e}")
            return {}

        data = {
            "url":            self.driver.current_url,
            "localStorage":   local,
            "sessionStorage": session,
            "timestamp":      datetime.now().isoformat(timespec="seconds"),
        }
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logging.info(
            f"[Session] Storage exported ({len(local)} local + {len(session)} session)"
        )
        return data

    def import_storage(self, filepath: str, navigate_first: bool = True) -> int:
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
                    "localStorage.setItem(arguments[0], arguments[1]);",
                    key, value,
                )
                count += 1
            except Exception:
                pass
        for key, value in (data.get("sessionStorage") or {}).items():
            try:
                self.driver.execute_script(
                    "sessionStorage.setItem(arguments[0], arguments[1]);",
                    key, value,
                )
                count += 1
            except Exception:
                pass
        logging.info(f"[Session] Imported {count} storage entries")
        return count

    # ─── Seed from real Chrome (one-time) ─────────────────

    def import_from_chrome(
        self,
        domain_filter: list = None,
        chrome_profile_path: str = None,
    ) -> int:
        """
        Import cookies from the user's installed Chrome (Default profile
        unless otherwise specified). CHROME MUST BE CLOSED — otherwise
        the Cookies SQLite file is locked by the OS and the copy fails.

        Note: Chrome encrypts most cookies via DPAPI on Windows. This
        method does NOT decrypt them — it copies only plaintext `value`
        rows. This is still useful for non-sensitive cookies (e.g. locale,
        consent, cf_clearance). For full DPAPI decryption you need a
        separate tool.
        """
        if chrome_profile_path is None:
            appdata = os.environ.get("LOCALAPPDATA", "")
            chrome_profile_path = os.path.join(
                appdata, "Google", "Chrome", "User Data", "Default"
            )

        # Newer Chrome stores cookies in <Profile>/Network/Cookies
        cookies_db = os.path.join(chrome_profile_path, "Network", "Cookies")
        if not os.path.exists(cookies_db):
            cookies_db = os.path.join(chrome_profile_path, "Cookies")
        if not os.path.exists(cookies_db):
            logging.error(f"[Session] Chrome cookies DB not found: {cookies_db}")
            return 0

        # Copy to temp so we don't risk corrupting the real DB
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
            tmp_path = tmp.name
        try:
            shutil.copy2(cookies_db, tmp_path)
        except PermissionError:
            logging.error("[Session] Could not copy Chrome DB — is Chrome running?")
            try: os.remove(tmp_path)
            except OSError: pass
            return 0

        cookies = []
        try:
            conn = sqlite3.connect(tmp_path)
            cur  = conn.cursor()
            query = """
                SELECT host_key, name, value, path, expires_utc,
                       is_secure, is_httponly, samesite
                FROM cookies
            """
            for row in cur.execute(query):
                domain = row[0].lstrip(".")
                if domain_filter and not any(f in domain for f in domain_filter):
                    continue
                if not row[2]:       # skip rows with empty plaintext value (encrypted)
                    continue

                # expires_utc = microseconds since 1601-01-01 → Unix timestamp
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
            except OSError: pass

        if not cookies:
            logging.warning(
                "[Session] No plaintext cookies found to import. "
                "Chrome encrypts most cookies via DPAPI — you'd need a "
                "decryption tool for the rest."
            )
            return 0

        # Write to a temp JSON and reuse import_cookies()
        tmp_json = tempfile.mktemp(suffix=".json")
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False)
        try:
            return self.import_cookies(tmp_json, domain_filter=domain_filter)
        finally:
            try: os.remove(tmp_json)
            except OSError: pass

    # ─── Full-session bundle helpers ──────────────────────

    def save_full_session(self, directory: str):
        """Save cookies + storage + URL/title metadata to a directory."""
        os.makedirs(directory, exist_ok=True)
        self.export_cookies(os.path.join(directory, "cookies.json"))
        self.export_storage(os.path.join(directory, "storage.json"))
        info = {
            "url":       self.driver.current_url,
            "title":     self.driver.title,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        with open(os.path.join(directory, "info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
        logging.info(f"[Session] Full session saved → {directory}")

    def restore_full_session(self, directory: str):
        """Restore cookies + storage from a directory (reverse of save_full_session)."""
        if not os.path.exists(directory):
            raise ValueError(f"Session directory not found: {directory}")
        self.import_cookies(os.path.join(directory, "cookies.json"))
        self.import_storage(os.path.join(directory, "storage.json"))
        logging.info(f"[Session] Session restored from {directory}")
