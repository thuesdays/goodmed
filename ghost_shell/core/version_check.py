"""
version_check.py — Detect Chrome / chromedriver version mismatch.

The most reliable way to crash a Chrome session before it even runs a
single CDP command is to pair a chrome.exe of one major version with a
chromedriver.exe of another. Selenium's chromedriver enforces protocol
compatibility per major version: 149.x.y can drive 149.a.b.c, but a 148
chromedriver against 149 chrome will yield SessionNotCreatedException
2-3s after launch with no useful error inline (just "Chrome instance
exited" — the same symptom we hit with locked profile dirs).

This module probes both binaries on demand and exposes a structured
verdict the dashboard surfaces as a startup banner. Cached for
``_CACHE_TTL_SEC`` because the binaries don't change without a
deploy/upgrade.

Public API
──────────
  get_chrome_version(path: str) -> str | None
  get_chromedriver_version(path: str) -> str | None
  check_compatibility(chrome=None, driver=None) -> dict
  invalidate_cache() -> None

The check_compatibility() return shape (always a dict, never raises):

    {
        "ok":               bool,            # False when mismatch detected
        "level":            "ok"|"warn"|"critical",
        "chrome_version":   "149.0.7805.0" | None,
        "chrome_major":     149 | None,
        "driver_version":   "149.0.7805.0" | None,
        "driver_major":     149 | None,
        "chrome_path":      str | None,
        "driver_path":      str | None,
        "reason":           str,             # human-readable summary
        "checked_at":       ISO timestamp,
    }
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

from ghost_shell.core.platform_paths import (
    find_chrome_binary, find_chromedriver, popen_flags_no_console,
)


# ──────────────────────────────────────────────────────────────
# Cache — version probe is one subprocess.run() per binary, cheap
# but not free; ~50-150ms each. Skip the cost on hot paths.
# ──────────────────────────────────────────────────────────────

_CACHE_TTL_SEC = 60 * 60        # 1 hour — re-probe roughly hourly
_CACHE: dict = {                 # keyed by absolute path
    # path: (version_str | None, mtime_when_probed, probed_at_ts)
}
_CACHE_LOCK = threading.Lock()
_VERDICT_CACHE: Optional[dict] = None
_VERDICT_CACHE_AT: float = 0


def invalidate_cache() -> None:
    """Drop all cached probes. Call after a deploy / chromium swap."""
    global _VERDICT_CACHE, _VERDICT_CACHE_AT
    with _CACHE_LOCK:
        _CACHE.clear()
        _VERDICT_CACHE = None
        _VERDICT_CACHE_AT = 0


# ──────────────────────────────────────────────────────────────
# Version regex — both chrome.exe --product-version and
# chromedriver.exe --version emit "<major>.<minor>.<build>.<patch>"
# possibly preceded by a label and followed by a hash. Examples:
#   chrome:        "149.0.7805.0\n"
#   chromedriver:  "ChromeDriver 149.0.7805.0 (abc...) refs/branch-heads/...\n"
# ──────────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\.(\d+)\b")


def _parse_version_string(text: str) -> Optional[str]:
    """Extract a four-part version from an arbitrary line. Returns the
    canonical 'X.Y.Z.W' form, or None if no match."""
    if not text:
        return None
    m = _VERSION_RE.search(text)
    if not m:
        return None
    return ".".join(m.groups())


def _major_of(version: Optional[str]) -> Optional[int]:
    if not version:
        return None
    try:
        return int(version.split(".", 1)[0])
    except (ValueError, IndexError):
        return None


# ──────────────────────────────────────────────────────────────
# Probe helpers — run the binary briefly with a version flag,
# parse stdout. ALL exceptions caught; the worst this module
# does is return None.
# ──────────────────────────────────────────────────────────────

def _run_version_probe(path: str, args: list[str], timeout: float = 5.0) -> Optional[str]:
    """Spawn ``path`` with ``args``, capture stdout, parse a version
    string. Returns the version or None on any failure (binary missing,
    timeout, parse failure, OS-level denial)."""
    if not path or not os.path.exists(path):
        return None
    try:
        flags = popen_flags_no_console()
        result = subprocess.run(
            [path] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **flags,
        )
        # Most builds print to stdout; some to stderr — try both.
        for stream in (result.stdout, result.stderr):
            v = _parse_version_string(stream or "")
            if v:
                return v
        return None
    except subprocess.TimeoutExpired:
        logging.debug(f"[version_check] timeout probing {path}")
        return None
    except OSError as e:
        logging.debug(f"[version_check] OS error probing {path}: {e}")
        return None
    except Exception as e:
        logging.debug(f"[version_check] unexpected probing {path}: {e}")
        return None


def _cached_probe(path: str, args: list[str]) -> Optional[str]:
    """Wrap _run_version_probe with mtime-aware caching. If the file's
    mtime has changed since we last probed, we re-probe — that's the
    "after a deploy" detection. Otherwise honor TTL."""
    if not path or not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    now = time.time()

    with _CACHE_LOCK:
        cached = _CACHE.get(path)
        if cached is not None:
            cached_v, cached_mtime, probed_at = cached
            if cached_mtime == mtime and (now - probed_at) < _CACHE_TTL_SEC:
                return cached_v

    # Cache miss / stale — actually probe (outside the lock, can be slow)
    version = _run_version_probe(path, args)

    with _CACHE_LOCK:
        _CACHE[path] = (version, mtime, now)
    return version


def get_chrome_version(path: Optional[str] = None) -> Optional[str]:
    """Return the four-part version of the Chrome/Chromium binary at
    ``path`` (e.g. '149.0.7805.0'), or None if unresolvable. If path
    is None, falls back to ``find_chrome_binary()``."""
    if path is None:
        path = find_chrome_binary()
    return _cached_probe(path, ["--product-version"])


def get_chromedriver_version(path: Optional[str] = None) -> Optional[str]:
    """Return the four-part version of chromedriver at ``path``, or
    None if unresolvable. Tries ``--version`` (the documented flag).
    Falls back to ``-v`` for very old builds."""
    if path is None:
        path = find_chromedriver()
    if not path:
        return None
    v = _cached_probe(path, ["--version"])
    if not v:
        v = _cached_probe(path, ["-v"])
    return v


# ──────────────────────────────────────────────────────────────
# Compatibility verdict
# ──────────────────────────────────────────────────────────────

def check_compatibility(
    chrome: Optional[str] = None,
    driver: Optional[str] = None,
    use_cache: bool = True,
) -> dict:
    """Run both probes and return a structured verdict.

    Verdict levels:
      * ``"ok"``       — versions match at the major level
      * ``"warn"``     — one of the binaries is missing (we can't decide)
      * ``"critical"`` — major versions differ; sessions WILL fail

    Cached for 60s by default — repeated calls in a tight loop don't
    stress process_iter.

    Args:
        chrome: Optional override path. Defaults to ``find_chrome_binary()``.
        driver: Optional override path. Defaults to ``find_chromedriver()``.
        use_cache: When True (default), reuses the last verdict if it
            was computed less than 60s ago. Bypass for forced refresh.
    """
    global _VERDICT_CACHE, _VERDICT_CACHE_AT
    now = time.time()

    if use_cache and _VERDICT_CACHE is not None and (now - _VERDICT_CACHE_AT) < 60:
        # Return a copy so callers can't mutate our cache
        return dict(_VERDICT_CACHE)

    chrome_path = chrome if chrome else find_chrome_binary()
    driver_path = driver if driver else find_chromedriver()

    chrome_ver = get_chrome_version(chrome_path) if chrome_path else None
    driver_ver = get_chromedriver_version(driver_path) if driver_path else None

    chrome_major = _major_of(chrome_ver)
    driver_major = _major_of(driver_ver)

    verdict = {
        "ok":              False,
        "level":           "warn",
        "chrome_version":  chrome_ver,
        "chrome_major":    chrome_major,
        "driver_version":  driver_ver,
        "driver_major":    driver_major,
        "chrome_path":     chrome_path,
        "driver_path":     driver_path,
        "reason":          "",
        "checked_at":      datetime.now().isoformat(timespec="seconds"),
    }

    if not chrome_path:
        verdict["reason"] = ("Chromium binary not found — set "
                             "browser.binary_path in dashboard settings, "
                             "or run scripts/download_chromium to pull it.")
        verdict["level"] = "warn"
    elif not driver_path:
        verdict["reason"] = (f"chromedriver not found next to "
                             f"{chrome_path}. The matching driver must "
                             f"live alongside the chrome binary; without "
                             f"it Selenium can't drive Chrome.")
        verdict["level"] = "critical"
    elif chrome_ver is None or driver_ver is None:
        # Binaries exist but didn't print a version we could parse —
        # treat as warn rather than fail-closed. Could be a stripped
        # build, an old-format --version output, or missing perms.
        missing = []
        if chrome_ver is None: missing.append("chrome")
        if driver_ver is None: missing.append("chromedriver")
        verdict["reason"] = (f"could not read version from "
                             f"{', '.join(missing)} — version probe "
                             f"failed. Compatibility unknown.")
        verdict["level"] = "warn"
    elif chrome_major != driver_major:
        verdict["reason"] = (
            f"VERSION MISMATCH: Chrome major={chrome_major} "
            f"({chrome_ver}) vs chromedriver major={driver_major} "
            f"({driver_ver}). Sessions WILL fail with "
            f"SessionNotCreatedException 2-3 seconds after launch. "
            f"Re-deploy matching binaries — both must come from the "
            f"same Chromium build."
        )
        verdict["level"] = "critical"
    else:
        verdict["ok"]     = True
        verdict["level"]  = "ok"
        verdict["reason"] = (f"Chrome {chrome_ver} ↔ chromedriver "
                             f"{driver_ver} — major versions match.")

    if verdict["level"] == "critical":
        logging.error(f"[version_check] {verdict['reason']}")
    elif verdict["level"] == "warn":
        logging.warning(f"[version_check] {verdict['reason']}")
    else:
        logging.info(f"[version_check] {verdict['reason']}")

    with _CACHE_LOCK:
        _VERDICT_CACHE = dict(verdict)
        _VERDICT_CACHE_AT = now

    return verdict


# ──────────────────────────────────────────────────────────────
# CLI / smoke test
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    print(json.dumps(check_compatibility(use_cache=False), indent=2))
