"""
cookie_manager.py — Dashboard-side cookie storage for profiles.

Reads / writes cookies for a profile WITHOUT launching Chrome. Each
profile has its session stored at:

    profiles/<name>/ghostshell_session/cookies.json

Format is the Selenium cookie dict shape (what driver.get_cookies()
returns), which maps 1:1 to the most common browser-extension export
formats (EditThisCookie, Cookie-Editor, Cookie-Quick-Manager). We also
support Netscape `cookies.txt` because curl/wget users have it everywhere.

This module is READ/WRITE to disk only. When a profile is actively
running, Chrome's own SQLite DB is authoritative — changes here take
effect on the NEXT start. The dashboard should warn the user about this.
"""

import os
import json
import time
import logging
from typing import Optional


# ──────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────

def profile_session_dir(profile_name: str, base_dir: str = None) -> str:
    """Where session artifacts (cookies, storage) live for a profile."""
    base = base_dir or "profiles"
    return os.path.join(base, profile_name, "ghostshell_session")


def cookies_path(profile_name: str, base_dir: str = None) -> str:
    return os.path.join(profile_session_dir(profile_name, base_dir), "cookies.json")


def storage_path(profile_name: str, base_dir: str = None) -> str:
    return os.path.join(profile_session_dir(profile_name, base_dir), "storage.json")


def chrome_cookies_db_path(profile_name: str, base_dir: str = None) -> str:
    """Path to Chrome's own Cookies SQLite DB for a profile. Exists
    after the profile has been run at least once.

    Chrome 96+ stores cookies at:
      profiles/<name>/Default/Network/Cookies
    Older versions used Default/Cookies directly.
    """
    base = base_dir or "profiles"
    new_path = os.path.join(base, profile_name, "Default", "Network", "Cookies")
    if os.path.exists(new_path):
        return new_path
    old_path = os.path.join(base, profile_name, "Default", "Cookies")
    return old_path  # caller checks existence


def list_chrome_live_cookies(profile_name: str, base_dir: str = None) -> list:
    """Read cookies directly from Chrome's SQLite DB for the profile.

    Returns Selenium-shape dicts so the result merges cleanly with
    list_cookies(). Returns [] if Chrome hasn't run for this profile
    yet OR if Chrome is currently running (DB will be locked).

    We query the readable columns only — `encrypted_value` we CAN'T
    decrypt without Chrome's keychain integration, so for those entries
    we just expose name/domain/path/expiry/flags. The dashboard UI
    shows "(encrypted)" for the value in that case, which is still
    useful for "what domains has this profile authenticated against".

    This is strictly READ-ONLY. Never write back — Chrome's encrypted_value
    column uses OS-keychain-derived keys we don't have.
    """
    db_path = chrome_cookies_db_path(profile_name, base_dir)
    if not os.path.exists(db_path):
        return []

    import sqlite3
    import tempfile
    import shutil

    # Copy to a temp file first — if Chrome is running, the DB is
    # locked. Copying bypasses the lock for read-only use.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        shutil.copyfile(db_path, tmp_path)
    except Exception as e:
        logging.debug(f"[cookies] copy of Chrome DB failed: {e}")
        try: os.remove(tmp_path)
        except OSError: pass
        return []

    results = []
    try:
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        # Chrome's schema has evolved — try modern first, fall back.
        rows = None
        try:
            rows = conn.execute("""
                SELECT host_key, name, value, path, is_secure, is_httponly,
                       expires_utc, samesite, encrypted_value
                FROM cookies
            """).fetchall()
        except sqlite3.OperationalError:
            # Very old Chrome might have `secure`/`httponly` without prefix
            rows = conn.execute("""
                SELECT host_key, name, value, path, secure AS is_secure,
                       httponly AS is_httponly, expires_utc,
                       NULL AS samesite, NULL AS encrypted_value
                FROM cookies
            """).fetchall()
        conn.close()

        for r in rows or []:
            value = r["value"]
            # If value column is empty and encrypted_value has content,
            # Chrome stored it encrypted (OS-keychain-keyed). We can't
            # decrypt so we surface a placeholder so the UI shows the
            # cookie exists.
            if (not value) and r["encrypted_value"]:
                value = "(encrypted — run profile to decrypt)"
            # Chrome stores expires_utc as microseconds since 1601-01-01.
            # Convert to Unix epoch (seconds) for Selenium-style `expiry`.
            expiry = None
            try:
                eu = int(r["expires_utc"] or 0)
                if eu > 0:
                    # 11644473600 seconds between 1601-01-01 and 1970-01-01
                    expiry = int(eu / 1_000_000 - 11_644_473_600)
                    if expiry < 0:
                        expiry = None
            except Exception:
                pass
            # host_key is like ".google.com" — Selenium prefers no leading dot
            # for the domain field, but keeps it for "non-host-only" cookies.
            # Preserve it as-is; front-end can display verbatim.
            samesite_raw = r["samesite"]
            samesite_map = {0: "None", 1: "Lax", 2: "Strict"}
            samesite = samesite_map.get(samesite_raw, "Lax") if samesite_raw is not None else None

            results.append({
                "name":     r["name"],
                "value":    value,
                "domain":   r["host_key"],
                "path":     r["path"],
                "secure":   bool(r["is_secure"]),
                "httpOnly": bool(r["is_httponly"]),
                "expiry":   expiry,
                "sameSite": samesite,
                "_source":  "chrome_live",
            })
    except Exception as e:
        logging.warning(f"[cookies] read Chrome DB failed: {e}")
    finally:
        try: os.remove(tmp_path)
        except OSError: pass

    return results


def list_cookies_merged(profile_name: str, base_dir: str = None) -> list:
    """Union of persisted cookies.json + Chrome's live SQLite cookies.

    When both sources have a cookie with the same (domain, path, name),
    Chrome's live copy wins — it's the most recent state. Ghost Shell
    session JSON is older (snapshot from last run's shutdown).

    Each returned entry has an `_source` field: "session" / "chrome_live" /
    "both" so the UI can indicate provenance.
    """
    session_cookies = list_cookies(profile_name, base_dir)
    live_cookies    = list_chrome_live_cookies(profile_name, base_dir)

    # Key by (domain, path, name). Chrome's list overwrites session's.
    by_key = {}
    for c in session_cookies:
        k = (c.get("domain"), c.get("path"), c.get("name"))
        c["_source"] = "session"
        by_key[k] = c
    for c in live_cookies:
        k = (c.get("domain"), c.get("path"), c.get("name"))
        if k in by_key:
            c["_source"] = "both"
        by_key[k] = c
    return list(by_key.values())


# ──────────────────────────────────────────────────────────────
# Read
# ──────────────────────────────────────────────────────────────

def list_cookies(profile_name: str, base_dir: str = None) -> list:
    """Return the list of stored cookies as Selenium-shape dicts.
    Returns [] if no cookies.json exists yet."""
    path = cookies_path(profile_name, base_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # Some older formats wrap in {cookies: [...]}
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            return data["cookies"]
        return []
    except Exception as e:
        logging.warning(f"[cookies] Failed to read {path}: {e}")
        return []


def list_storage(profile_name: str, base_dir: str = None) -> dict:
    """Return the stored localStorage / sessionStorage map, or {}."""
    path = storage_path(profile_name, base_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        logging.warning(f"[storage] Failed to read {path}: {e}")
        return {}


# ──────────────────────────────────────────────────────────────
# Write
# ──────────────────────────────────────────────────────────────

def save_cookies(profile_name: str, cookies: list, base_dir: str = None) -> None:
    """Overwrite the cookies file with the given list.

    The list entries should be Selenium-shape dicts:
      {name, value, domain, path, secure, httpOnly, expiry, sameSite}

    We don't validate heavily here — if the caller passes garbage,
    the worker will just skip those entries at import time. But we DO
    drop obviously-malformed records (missing name or domain) because
    those would break add_cookie() on the worker side.
    """
    cleaned = []
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        if not c.get("name") or not c.get("domain"):
            continue
        cleaned.append(_normalize_cookie(c))

    session_dir = profile_session_dir(profile_name, base_dir)
    os.makedirs(session_dir, exist_ok=True)
    path = cookies_path(profile_name, base_dir)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def clear_cookies(profile_name: str, base_dir: str = None) -> None:
    """Remove all cookies for this profile. Chrome's own DB is NOT
    touched — only the dashboard-facing JSON. On next worker start the
    worker will load this empty file → profile browses as logged-out."""
    save_cookies(profile_name, [], base_dir)


# ──────────────────────────────────────────────────────────────
# Import format converters — accept lots of common formats
# ──────────────────────────────────────────────────────────────

def _normalize_cookie(raw: dict) -> dict:
    """Convert various extension-specific cookie shapes into the
    Selenium dict our worker expects.

    Handles:
      * Selenium / WebDriver (pass-through)
      * EditThisCookie / Cookie-Editor:
          {name, value, domain, path, secure, httpOnly, hostOnly,
           session, expirationDate, sameSite, storeId}
      * Puppeteer/Playwright:
          {name, value, domain, path, secure, httpOnly, expires, sameSite}
    """
    out = {
        "name":   raw.get("name"),
        "value":  raw.get("value", ""),
        "domain": raw.get("domain"),
        "path":   raw.get("path", "/"),
    }

    # Secure / httpOnly — direct copy, default False
    out["secure"]   = bool(raw.get("secure", False))
    out["httpOnly"] = bool(raw.get("httpOnly", raw.get("httponly", False)))

    # Expiry — unify various key names to Selenium's "expiry" (unix seconds, int)
    exp = (
        raw.get("expiry")
        or raw.get("expires")
        or raw.get("expirationDate")
        or raw.get("Expires")
    )
    if exp is not None:
        try:
            # Some exporters use floats (fractional seconds); coerce to int.
            # Negative or 0 = session cookie → just omit the key.
            iexp = int(float(exp))
            if iexp > 0:
                out["expiry"] = iexp
        except Exception:
            pass

    # sameSite — normalize case. Selenium wants "Strict" / "Lax" / "None".
    ss = raw.get("sameSite") or raw.get("samesite")
    if ss:
        ss = str(ss).strip().lower()
        if   ss in ("strict", "s"):      out["sameSite"] = "Strict"
        elif ss in ("lax",):              out["sameSite"] = "Lax"
        elif ss in ("none", "no_restriction", "unspecified"):
            out["sameSite"] = "None"

    return out


def parse_import(blob: str) -> list:
    """Parse a cookie import payload — detects JSON vs Netscape.

    JSON: a list of cookie objects OR {cookies: [...]}.
    Netscape: tab-separated lines per cookie (the classic cookies.txt).

    Returns a list of normalized Selenium-shape dicts. Raises ValueError
    if the payload is unparseable.
    """
    blob = (blob or "").strip()
    if not blob:
        raise ValueError("Empty import")

    # Try JSON first — covers ~90% of cases (browser extensions all use JSON)
    if blob[0] in "[{":
        try:
            data = json.loads(blob)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of cookies or {cookies: [...]}")
        return [_normalize_cookie(c) for c in data if isinstance(c, dict)]

    # Netscape cookies.txt — `# Netscape HTTP Cookie File` header or tab-delimited rows
    rows = []
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, include_subs, path, secure, expiry, name, value = parts[:7]
        rows.append(_normalize_cookie({
            "domain":  domain,
            "path":    path or "/",
            "secure":  secure.upper() == "TRUE",
            "expiry":  expiry,
            "name":    name,
            "value":   value,
        }))
    if not rows:
        raise ValueError(
            "Could not parse as JSON or Netscape cookies.txt. "
            "Expected either a JSON array of cookie objects, or "
            "tab-separated lines per the Netscape spec."
        )
    return rows


# ──────────────────────────────────────────────────────────────
# Export format converters
# ──────────────────────────────────────────────────────────────

def to_netscape(cookies: list) -> str:
    """Convert our Selenium-shape list back to a Netscape cookies.txt
    string. Useful for curl/wget users."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Exported by Ghost Shell",
        "",
    ]
    now_ts = int(time.time())
    for c in cookies:
        domain = c.get("domain", "")
        # Netscape's "include subdomains" flag — convention is leading dot means yes
        include_subs = "TRUE" if domain.startswith(".") else "FALSE"
        path    = c.get("path", "/")
        secure  = "TRUE" if c.get("secure") else "FALSE"
        # Session cookies in Netscape format use 0 for expiry
        expiry  = c.get("expiry") or 0
        name    = c.get("name", "")
        value   = c.get("value", "")
        lines.append("\t".join([
            domain, include_subs, path, secure, str(expiry), name, value,
        ]))
    return "\n".join(lines) + "\n"
