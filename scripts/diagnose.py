"""
diagnose.py — Full Environment Diagnostics for Ghost Shell

Runs a battery of checks:
  - Python deps installed?
  - All critical Python modules present?
  - Is the custom-patched Chromium binary present for this OS?
  - Does SQLite DB exist and is it initialised?
  - Is a proxy URL configured?
  - Can we launch the browser without errors?
  - What does the selfcheck return (X/30 passed)?
  - Does the current proxy have WebRTC leak?
  - Is the exit IP in the expected country / timezone?

Run before first use or when troubleshooting:

    python diagnose.py
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
import sys
import time
import logging
import shutil
from datetime import datetime


# ──────────────────────────────────────────────────────────────
# DEPENDENCIES
# ──────────────────────────────────────────────────────────────

def check_dependencies() -> list[dict]:
    """Verify required Python modules are installed."""
    results = []
    deps = [
        ("selenium",     "selenium>=4.15"),
        ("requests",     "requests>=2.31"),
        ("flask",        "flask>=3.0"),
        ("flask_cors",   "flask-cors>=4.0"),
        ("psutil",       "psutil>=5.9"),
    ]
    for module_name, hint in deps:
        try:
            __import__(module_name)
            results.append({"check": module_name, "ok": True, "detail": "installed"})
        except ImportError:
            results.append({"check": module_name, "ok": False,
                            "detail": f"pip install {hint}"})
    return results


# ──────────────────────────────────────────────────────────────
# FILES
# ──────────────────────────────────────────────────────────────

def check_files() -> list[dict]:
    """Check presence of project files. Ghost Shell doesn't need config.yaml
    any more — everything lives in the SQLite DB (ghost_shell.db)."""
    results = []

    # Critical — the project can't run without these. Post-refactor
    # (v0.2.0) the package lives under ghost_shell/ — everything below
    # is checked relative to the project root.
    critical = [
        ("ghost_shell/main.py",                      "Monitor run entrypoint"),
        ("ghost_shell/dashboard/server.py",          "Dashboard Flask app"),
        ("ghost_shell/browser/runtime.py",           "Browser wrapper"),
        ("ghost_shell/fingerprint/device_templates.py", "Payload generator"),
        ("ghost_shell/db/database.py",               "SQLite layer"),
        ("ghost_shell/actions/runner.py",            "Post-click pipeline"),
        ("ghost_shell/core/platform_paths.py",       "OS-aware paths"),
    ]
    # Optional — nice-to-have, but project works without
    optional = [
        ("ghost_shell.db",                           "State database (created on first run)"),
        ("ghost_shell/proxy/diagnostics.py",         "Proxy / geo diagnostics"),
        ("scheduler.py",            "Cron-like runner"),
    ]
    # Chromium C++ source (only needed if user builds from source)
    source_hints = [
        ("ghost_shell_config.h",    "C++ header — for Chromium rebuilds"),
        ("ghost_shell_config.cc",   "C++ impl — for Chromium rebuilds"),
    ]

    for fname, desc in critical:
        exists = os.path.exists(fname)
        results.append({
            "check":  fname,
            "ok":     exists,
            "detail": desc if exists else f"MISSING — {desc}",
        })

    for fname, desc in optional:
        exists = os.path.exists(fname)
        results.append({
            "check":  fname,
            "ok":     True,
            "detail": desc if exists else f"Missing ({desc}) — non-critical",
        })

    for fname, desc in source_hints:
        exists = os.path.exists(fname)
        results.append({
            "check":  fname,
            "ok":     True,
            "detail": desc if exists else f"Missing ({desc}) — only required for rebuilds",
        })

    return results


# ──────────────────────────────────────────────────────────────
# CHROMIUM BINARY
# ──────────────────────────────────────────────────────────────

def check_chromium() -> dict:
    """Verify the patched Chromium binary is deployed for this platform."""
    try:
        from ghost_shell.core.platform_paths import find_chrome_binary, find_chromedriver, PLATFORM
    except Exception as e:
        return {"ok": False, "detail": f"platform_paths import failed: {e}"}

    chrome = find_chrome_binary()
    driver = find_chromedriver()

    if not chrome:
        return {
            "ok": False,
            "detail": (f"Chromium binary not found for {PLATFORM}. "
                       f"Run deploy-ghost-shell-flat.{'bat' if PLATFORM=='windows' else 'sh'} first.")
        }
    if not driver:
        return {
            "ok": False,
            "detail": f"chromedriver not found next to {chrome}. Rebuild with autoninja."
        }

    try:
        size_mb = os.path.getsize(chrome) / 1024 / 1024
    except Exception:
        size_mb = 0
    return {
        "ok": True,
        "detail": f"{chrome} ({size_mb:.0f} MB), driver at {driver}",
        "chrome": chrome,
        "driver": driver,
    }


# ──────────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────────

def check_database() -> dict:
    """Verify SQLite DB can be opened and has populated config."""
    try:
        from ghost_shell.db.database import get_db
        db = get_db()
        cfg_count = db._get_conn().execute(
            "SELECT COUNT(*) FROM config_kv"
        ).fetchone()[0]
        profile = db.config_get("browser.profile_name", "profile_01")
        return {
            "ok": cfg_count > 0,
            "detail": f"{cfg_count} config keys, active profile={profile}",
            "profile": profile,
        }
    except Exception as e:
        return {"ok": False, "detail": f"DB error: {e}"}


# ──────────────────────────────────────────────────────────────
# PROXY
# ──────────────────────────────────────────────────────────────

def check_proxy_setup() -> dict:
    """Verify proxy is configured in DB."""
    try:
        from ghost_shell.db.database import get_db
        db = get_db()
        proxy_url = db.config_get("proxy.url", "")
        if not proxy_url:
            return {
                "ok": False,
                "detail": "proxy.url is not set (configure in dashboard → Proxy page)"
            }
        # Hide credentials for display
        redacted = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        return {"ok": True, "detail": f"configured → {redacted[:50]}"}
    except Exception as e:
        return {"ok": False, "detail": f"Config read error: {e}"}


# ──────────────────────────────────────────────────────────────
# LIVE BROWSER LAUNCH
# ──────────────────────────────────────────────────────────────

def run_browser_check(profile_name: str = "diag_temp") -> dict:
    """Launch a temporary browser instance and run all health checks."""
    try:
        from ghost_shell.browser.runtime import GhostShellBrowser
        from ghost_shell.proxy.diagnostics import ProxyDiagnostics
        from ghost_shell.db.database import get_db

        db = get_db()
        proxy = db.config_get("proxy.url", "")
        expected_tz = db.config_get("browser.expected_timezone", "Europe/Kyiv")
        expected_country = db.config_get("browser.expected_country", "Ukraine")

        logging.info("Launching temporary browser for diagnostics...")

        with GhostShellBrowser(
            profile_name=profile_name,
            proxy_str=proxy,
            auto_session=False,
            enrich_on_create=False,
        ) as browser:
            # Self-check
            health = browser.health_check(verbose=False)
            h_passed = sum(1 for v in health.values() if v is True)
            h_total  = len(health)
            h_failed = [k for k, v in health.items() if v is not True]

            # Proxy diagnostics
            diag = ProxyDiagnostics(browser.driver)
            rep  = diag.full_check(
                expected_timezone=expected_tz,
                expected_country=expected_country,
            )

            return {
                "ok":            h_passed == h_total and not rep.get("webrtc_leak"),
                "health_score":  f"{h_passed}/{h_total}",
                "health_failed": h_failed,
                "ip":            rep.get("ip_info", {}).get("ip"),
                "country":       rep.get("ip_info", {}).get("country"),
                "risk":          rep.get("reputation", {}).get("risk"),
                "webrtc_leak":   rep.get("webrtc_leak", False),
                "timezone_ok":   rep.get("timezone", {}).get("ok", False),
                "geo_mismatch":  rep.get("geo_mismatch", False),
            }

    except Exception as e:
        logging.error(f"Browser check error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


def cleanup_temp_profile(profile_name: str = "diag_temp"):
    """Remove the temporary profile created for diagnostics."""
    path = os.path.join("profiles", profile_name)
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def run_diagnostic(verbose: bool = True) -> bool:
    print("\n" + "═" * 72)
    print("  GHOST SHELL — FULL DIAGNOSTICS")
    print("═" * 72)
    print(f"  Time:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python:   {sys.version.split()[0]}")
    print(f"  Platform: {sys.platform}")
    print("─" * 72)

    all_results = []

    # 1. Dependencies
    print("\n📦 DEPENDENCIES")
    for r in check_dependencies():
        icon = "✓" if r["ok"] else "✗"
        print(f"  {icon} {r['check']:<30} {r['detail']}")
        all_results.append(r)

    # 2. Files
    print("\n📁 FILES")
    for r in check_files():
        icon = "✓" if r["ok"] else "✗"
        print(f"  {icon} {r['check']:<30} {r['detail']}")
        all_results.append(r)

    # 3. Chromium binary
    print("\n🧬 CHROMIUM BUILD")
    cr = check_chromium()
    icon = "✓" if cr["ok"] else "✗"
    print(f"  {icon} binary                        {cr['detail']}")

    # 4. Database
    print("\n💾 DATABASE")
    dr = check_database()
    icon = "✓" if dr["ok"] else "✗"
    print(f"  {icon} ghost_shell.db                {dr['detail']}")

    # 5. Proxy config
    print("\n🌐 PROXY CONFIG")
    proxy_setup = check_proxy_setup()
    icon = "✓" if proxy_setup["ok"] else "✗"
    print(f"  {icon} proxy.url                     {proxy_setup['detail']}")

    # If any critical piece is missing, skip live run
    critical_missing = not cr["ok"] or not dr["ok"] or not proxy_setup["ok"]
    if critical_missing:
        print("\n⚠ Skipping live browser check — fix the above issues first")
        print("═" * 72 + "\n")
        return False

    # 6. Live browser launch
    print("\n🚀 LIVE BROWSER LAUNCH & SELFCHECK")
    print("   (this takes ~60-90 seconds)")
    bc = run_browser_check()

    if bc.get("error"):
        print(f"  ✗ Launch error: {bc['error']}")
        print("═" * 72 + "\n")
        return False

    # Selfcheck
    score = bc.get("health_score", "?/?")
    passed, total = (score.split("/") + ["0", "0"])[:2]
    perfect = passed == total and total != "0"
    health_icon = "✓" if perfect else "⚠"
    print(f"  {health_icon} Selfcheck:                    {score} passed")
    if bc.get("health_failed"):
        print(f"    Failed: {', '.join(bc['health_failed'])}")

    # IP & geo
    ip      = bc.get("ip") or "?"
    country = bc.get("country") or "?"
    print(f"  ✓ External IP:                 {ip} ({country})")

    # Reputation
    risk = bc.get("risk", "unknown")
    risk_icon = {"low": "✓", "medium": "⚠", "high": "✗"}.get(risk, "?")
    print(f"  {risk_icon} IP Reputation:                {risk}")

    # WebRTC leak
    leak_icon = "✓" if not bc.get("webrtc_leak") else "✗"
    leak_status = "DETECTED" if bc.get("webrtc_leak") else "none"
    print(f"  {leak_icon} WebRTC leak:                  {leak_status}")

    # Timezone
    tz_icon = "✓" if bc.get("timezone_ok") else "⚠"
    print(f"  {tz_icon} Timezone match:               {bc.get('timezone_ok')}")

    # Geo mismatch
    geo_icon = "✓" if not bc.get("geo_mismatch") else "⚠"
    print(f"  {geo_icon} Country match:                {not bc.get('geo_mismatch')}")

    # Cleanup
    cleanup_temp_profile()

    # Summary
    print("\n" + "═" * 72)
    critical_failed = [r for r in all_results
                       if not r["ok"] and "MISSING" in r.get("detail", "")]

    if critical_failed:
        print("  ❌ DIAGNOSTICS FAILED — Critical files missing")
        for r in critical_failed:
            print(f"       - {r['check']}: {r['detail']}")
    elif bc.get("webrtc_leak"):
        print("  ❌ DIAGNOSTICS FAILED — WebRTC leak detected")
    elif not perfect:
        print(f"  ⚠ DIAGNOSTICS PASSED WITH WARNINGS — {score} selfcheck")
    elif not bc.get("ok"):
        print("  ⚠ DIAGNOSTICS PASSED WITH WARNINGS")
    else:
        print("  ✅ DIAGNOSTICS PASSED — All systems operational")
    print("═" * 72 + "\n")

    return bc.get("ok", False)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )
    is_ok = run_diagnostic()
    sys.exit(0 if is_ok else 1)
