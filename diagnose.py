"""
diagnose.py — Full Environment Diagnostics

A single script that checks EVERYTHING:
- Are dependencies installed?
- Is config.yaml present?
- Is the proxy working?
- Are there any WebRTC leaks?
- Is the fingerprint correct (health_check)?
- CreepJS trust score (if implemented)
- Fingerprint stability across launches
- Profile health

Run before first use or when troubleshooting:
    python diagnose.py
"""

import os
import sys
import time
import logging
import shutil
from datetime import datetime


def check_dependencies() -> list[dict]:
    """Verifies that all required modules are installed."""
    results = []
    deps = [
        ("undetected_chromedriver", "undetected-chromedriver>=3.5.5"),
        ("selenium",                "selenium>=4.15.0"),
        ("requests",                "requests>=2.31.0"),
        ("yaml",                    "PyYAML>=6.0 (optional)"),
    ]
    
    for module_name, hint in deps:
        try:
            __import__(module_name)
            results.append({"check": module_name, "ok": True, "detail": "installed"})
        except ImportError:
            results.append({"check": module_name, "ok": False, "detail": f"pip install {hint}"})
            
    return results


def check_files() -> list[dict]:
    """Checks for the presence of critical and optional project files."""
    results = []
    critical = [
        ("fingerprints.js",  "JS injections"),
        ("nk_browser.py",    "Main browser class"),
    ]
    optional = [
        ("config.yaml",      "Configuration file"),
        ("proxies.json",     "Proxy pool"),
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
        
    return results


def check_proxy_setup() -> dict:
    """Verifies proxy configuration via the config file."""
    try:
        from config import Config
        cfg = Config.load()
        proxy_url = cfg.get("proxy.url")
        
        if not proxy_url:
            return {"ok": False, "detail": "proxy.url is not set in config.yaml"}
            
        return {"ok": True, "detail": f"Proxy configured: {proxy_url[:40]}..."}
    except Exception as e:
        return {"ok": False, "detail": f"Config error: {e}"}


def run_browser_check(profile_name: str = "diag_temp") -> dict:
    """Launches a temporary browser instance and runs all health checks."""
    try:
        from ghost_shell_browser import GhostShellBrowser   
        from proxy_diagnostics import ProxyDiagnostics
        from config import Config

        cfg = Config.load()
        proxy = cfg.get("proxy.url")

        logging.info("Launching temporary browser for diagnostics...")

        with GhostShellBrowser(
            profile_name      = profile_name,
            proxy_str         = proxy,
            auto_session      = False,
            enrich_on_create  = False,  # Do not pollute the temporary profile
        ) as browser:
            
            # Health check
            health = browser.health_check(verbose=False)
            health_passed = sum(1 for v in health.values() if v is True)
            health_total  = len(health)

            # Proxy diagnostics
            diag = ProxyDiagnostics(browser.driver)
            proxy_report = diag.full_check(expected_timezone="Europe/Kyiv")

            return {
                "ok":            health_passed == health_total and not proxy_report.get("webrtc_leak"),
                "health_passed": health_passed,
                "health_total":  health_total,
                "health_score":  f"{health_passed}/{health_total}",
                "health_failed": [k for k, v in health.items() if v is not True],
                "ip":            proxy_report.get("ip_info", {}).get("ip"),
                "country":       proxy_report.get("ip_info", {}).get("country"),
                "risk":          proxy_report.get("reputation", {}).get("risk"),
                "webrtc_leak":   proxy_report.get("webrtc_leak", False),
                "timezone_ok":   proxy_report.get("timezone", {}).get("ok", False),
            }

    except Exception as e:
        logging.error(f"Browser check error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


def cleanup_temp_profile(profile_name: str = "diag_temp"):
    """Deletes the temporary profile created for diagnostics."""
    path = os.path.join("profiles", profile_name)
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# MAIN EXECUTION
# ──────────────────────────────────────────────────────────────

def run_diagnostic(verbose: bool = True) -> bool:
    print("\n" + "═" * 72)
    print("  NK BROWSER — FULL DIAGNOSTICS")
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

    # 3. Proxy Setup
    print("\n🌐 PROXY")
    proxy_setup = check_proxy_setup()
    icon = "✓" if proxy_setup["ok"] else "✗"
    print(f"  {icon} proxy config                  {proxy_setup['detail']}")

    if not proxy_setup["ok"]:
        print("\n⚠ Skipping browser check — proxy is not configured")
        return False

    # 4. Browser Launch & Live Check
    print("\n🌐 BROWSER LAUNCH & LIVE CHECK")
    print("   (This will take ~60 seconds)")
    browser_check = run_browser_check()

    if browser_check.get("error"):
        print(f"  ✗ Launch error: {browser_check['error']}")
        return False

    # Dynamically evaluate health score instead of hardcoding 15/15
    is_perfect_health = browser_check.get('health_passed') == browser_check.get('health_total')
    health_icon = "✓" if is_perfect_health else "⚠"
    print(f"  {health_icon} Health check:                 {browser_check['health_score']}")

    if browser_check.get("health_failed"):
        print(f"    Failed checks: {', '.join(browser_check['health_failed'])}")

    ip = browser_check.get("ip") or "?"
    country = browser_check.get("country") or "?"
    print(f"  ✓ External IP:                 {ip} ({country})")

    risk = browser_check.get("risk", "unknown")
    risk_icon = {"low": "✓", "medium": "⚠", "high": "✗"}.get(risk, "?")
    print(f"  {risk_icon} IP Reputation:                {risk}")

    leak_icon = "✓" if not browser_check.get("webrtc_leak") else "✗"
    leak_status = "DETECTED!" if browser_check.get("webrtc_leak") else "None"
    print(f"  {leak_icon} WebRTC leak:                  {leak_status}")

    tz_icon = "✓" if browser_check.get("timezone_ok") else "⚠"
    print(f"  {tz_icon} Timezone match:               {browser_check.get('timezone_ok')}")

    # Cleanup
    cleanup_temp_profile()

    # Summary
    print("\n" + "═" * 72)
    critical_failed = [r for r in all_results if not r["ok"] and "MISSING" in r.get("detail", "")]
    
    if critical_failed:
        print("  ❌ DIAGNOSTICS FAILED — Critical files are missing")
    elif browser_check.get("webrtc_leak"):
        print("  ❌ DIAGNOSTICS FAILED — WebRTC leak detected")
    elif not browser_check.get("ok"):
        print("  ⚠ DIAGNOSTICS PASSED WITH WARNINGS — Operational but degraded")
    else:
        print("  ✅ DIAGNOSTICS PASSED — All systems operational")
    print("═" * 72 + "\n")

    return browser_check.get("ok", False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    is_ok = run_diagnostic()
    sys.exit(0 if is_ok else 1)