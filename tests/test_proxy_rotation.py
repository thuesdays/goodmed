"""
test_proxy_rotation.py — Direct test of the asocks proxy rotation.

Runs 10 requests through the proxy and shows the exit IP + country for
each. If all 10 are in Ukraine → country filter works.
If they're scattered across countries → we need to configure filter in
the proxy URL or in the provider's dashboard.

Usage:
    python test_proxy_rotation.py

Configurable via env vars PROXY or reads from DB.
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
import json
import time
import requests

# ── Read proxy from DB (same place main.py reads) ─────────────
try:
    from ghost_shell.db.database import get_db
    db = get_db()
    PROXY = (db.config_get("proxy.url")
             or db.config_get("proxy.string")
             or None)
except Exception:
    PROXY = None

PROXY = os.environ.get("PROXY") or PROXY
if not PROXY:
    print("[ERROR] No proxy configured. Set PROXY env var or proxy.url in DB.")
    sys.exit(1)

if not PROXY.startswith("http"):
    PROXY = "http://" + PROXY

proxies = {"http": PROXY, "https": PROXY}


def safe_get(url, timeout=15):
    try:
        r = requests.get(url, proxies=proxies, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)[:100]}


def get_exit_info():
    """Fetch current exit IP + country. Tries multiple services."""
    # Primary
    data = safe_get("https://ipapi.co/json/")
    if "_error" not in data and data.get("ip"):
        return {
            "ip":      data.get("ip"),
            "country": data.get("country_name"),
            "city":    data.get("city"),
            "org":     data.get("org"),
        }
    # Fallback
    data = safe_get("https://ipwho.is/")
    if "_error" not in data and data.get("ip"):
        return {
            "ip":      data.get("ip"),
            "country": data.get("country"),
            "city":    data.get("city"),
            "org":     (data.get("connection") or {}).get("org"),
        }
    return None


print("=" * 75)
print(" ASOCKS ROTATION TEST")
print("=" * 75)
print(f" Proxy: {PROXY.split('@')[-1] if '@' in PROXY else PROXY}")
print("=" * 75)
print()

seen_countries = {}
seen_ips       = set()

N_REQUESTS = 10
for i in range(1, N_REQUESTS + 1):
    info = get_exit_info()
    if not info:
        print(f"  [{i:2d}/{N_REQUESTS}]  ✗ all IP services failed")
        time.sleep(2)
        continue

    ip      = info.get("ip") or "?"
    country = info.get("country") or "?"
    city    = info.get("city") or "?"
    org     = (info.get("org") or "?")[:40]

    flag = "🇺🇦" if country.lower() in ("ukraine", "україна") else "🌐"
    print(f"  [{i:2d}/{N_REQUESTS}]  {flag}  {ip:<16}  "
          f"{country:<20}  {city:<15}  {org}")

    seen_countries[country] = seen_countries.get(country, 0) + 1
    seen_ips.add(ip)

    # Small delay. If asocks rotates per-request no delay needed, but some
    # providers use "sticky sessions" that last N seconds.
    time.sleep(2)

print()
print("=" * 75)
print(" SUMMARY")
print("=" * 75)
print(f"  Unique IPs:        {len(seen_ips)}")
print(f"  Unique countries:  {len(seen_countries)}")
for country, count in sorted(seen_countries.items(),
                              key=lambda kv: -kv[1]):
    bar = "█" * count
    print(f"  {country:<25}  {bar}  ({count})")

print()
if len(seen_countries) == 1 and list(seen_countries.keys())[0].lower() in (
        "ukraine", "україна"):
    print("  ✓ Country filter is working — all exits are Ukraine.")
elif "Ukraine" in seen_countries or "Україна" in seen_countries:
    pct = 100 * (seen_countries.get("Ukraine", 0)
                 + seen_countries.get("Україна", 0)) / N_REQUESTS
    print(f"  ⚠ Only {pct:.0f}% of exits are Ukraine.")
    print("    Country filter is NOT applied — configure it in asocks "
          "dashboard or modify the proxy URL.")
else:
    print("  ✗ Zero exits from Ukraine — country filter is definitely not "
          "applied.")
    print("    Action: contact asocks support for country-filter syntax, "
          "OR switch to a Ukraine-only plan.")
print("=" * 75)
