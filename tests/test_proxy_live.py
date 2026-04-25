"""
test_proxy_live.py — diagnose WHY Chrome sees ERR_PROXY_CONNECTION_FAILED.

Runs 4 checks in order, stops at the first failure:

    1. TCP reachability       — can we even open a socket to upstream?
    2. CONNECT response       — does upstream answer HTTP CONNECT correctly?
    3. IP identification      — what IP does upstream present us as?
    4. Rotation API health    — if configured, does the rotate URL work?

Usage:
    python test_proxy_live.py
    python test_proxy_live.py --proxy http://user:pass@host:port
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
import argparse
import base64
import socket
import ssl
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ghost_shell.db.database import get_db


def resolve_proxy_url() -> str:
    """Read the same DB keys ghost_shell_browser reads, so this tool
    matches what the real launcher will use."""
    db = get_db()
    url = db.config_get("proxy.url") or db.config_get("browser.proxy_url")
    if not url:
        raise SystemExit(
            "No proxy configured. Set proxy.url in Dashboard → Proxy, "
            "or pass --proxy flag."
        )
    return url


def test_tcp_reachable(host: str, port: int) -> bool:
    print(f"  [1/4] TCP handshake to {host}:{port} ...", end=" ")
    sys.stdout.flush()
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=10)
        s.close()
        print(f"OK ({(time.time() - t0) * 1000:.0f}ms)")
        return True
    except socket.timeout:
        print(f"TIMEOUT after 10s")
        print(f"       → Provider's server is blocked from your IP, or firewall drops packets.")
        print(f"       → Check if {host} resolves: nslookup {host}")
        return False
    except ConnectionRefusedError:
        print("REFUSED")
        print(f"       → Port {port} is closed on {host}. Provider probably changed ports.")
        return False
    except socket.gaierror as e:
        print(f"DNS FAIL: {e}")
        print(f"       → {host} doesn't resolve. Check URL typo or provider downtime.")
        return False
    except Exception as e:
        print(f"FAIL: {e}")
        return False


def test_connect_auth(proxy_url: str) -> bool:
    """Send a raw CONNECT request with Proxy-Authorization and parse the
    response. Catches auth failures that TCP reachability would miss."""
    parsed = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    host, port = parsed.hostname, parsed.port or 8080

    print(f"  [2/4] CONNECT api.ipify.org:443 via {host}:{port} ...", end=" ")
    sys.stdout.flush()

    req = (
        f"CONNECT api.ipify.org:443 HTTP/1.1\r\n"
        f"Host: api.ipify.org:443\r\n"
    )
    if parsed.username:
        auth = f"{parsed.username}:{parsed.password or ''}"
        token = base64.b64encode(auth.encode()).decode()
        req += f"Proxy-Authorization: Basic {token}\r\n"
    req += "\r\n"

    try:
        sock = socket.create_connection((host, port), timeout=15)
        sock.settimeout(15)
        sock.sendall(req.encode())
        resp = b""
        # Read until end-of-headers or socket close
        while b"\r\n\r\n" not in resp and len(resp) < 4096:
            chunk = sock.recv(1024)
            if not chunk:
                break
            resp += chunk
        sock.close()

        first_line = resp.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        code = first_line.split()[1] if len(first_line.split()) >= 2 else "?"

        if code == "200":
            print(f"OK (200 Connection Established)")
            return True
        elif code == "407":
            print(f"AUTH REQUIRED (407)")
            print(f"       → Username/password rejected. Check credentials.")
            print(f"       → For asocks: your session might be expired. Re-generate creds.")
            return False
        elif code == "403":
            print(f"FORBIDDEN (403)")
            print(f"       → Your source IP isn't whitelisted, or target host is blocked.")
            return False
        else:
            print(f"UNEXPECTED: {first_line}")
            print(f"       → Raw response head: {resp[:200]!r}")
            return False
    except Exception as e:
        print(f"FAIL: {e}")
        return False


def test_ip_seen(proxy_url: str) -> bool:
    """What external IP does the proxy present us as?"""
    print(f"  [3/4] Fetching api.ipify.org through proxy ...", end=" ")
    sys.stdout.flush()

    import urllib.request
    handler = urllib.request.ProxyHandler({
        "http":  proxy_url,
        "https": proxy_url,
    })
    opener = urllib.request.build_opener(handler)
    try:
        t0 = time.time()
        resp = opener.open("https://api.ipify.org?format=text", timeout=20)
        ip = resp.read().decode().strip()
        print(f"OK — external IP: {ip} ({(time.time() - t0) * 1000:.0f}ms)")
        return True
    except Exception as e:
        print(f"FAIL: {e}")
        print(f"       → TCP + CONNECT worked but no HTTP traffic flows.")
        print(f"       → Provider silently drops packets after handshake.")
        return False


def test_rotation_api() -> bool:
    """If rotation is configured, verify the API endpoint works."""
    db = get_db()
    rot_url = (db.config_get("proxy.rotation_api_url")
               or db.config_get("rotation.api_url"))
    if not rot_url:
        print(f"  [4/4] Rotation API: SKIP (not configured)")
        return True

    print(f"  [4/4] Rotation API GET {rot_url[:60]} ...", end=" ")
    sys.stdout.flush()
    try:
        t0 = time.time()
        resp = urlopen(rot_url, timeout=15)
        body = resp.read().decode("utf-8", errors="replace")[:200]
        print(f"HTTP {resp.status} ({(time.time() - t0) * 1000:.0f}ms)")
        print(f"       → body: {body!r}")
        return resp.status == 200
    except Exception as e:
        print(f"FAIL: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proxy", help="Override proxy URL (otherwise read from DB)")
    args = ap.parse_args()

    print("═" * 64)
    print(" Ghost Shell — Proxy connectivity check")
    print("═" * 64)
    proxy_url = args.proxy or resolve_proxy_url()
    parsed = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
    print(f"  Upstream: {parsed.scheme}://{parsed.hostname}:{parsed.port or 8080}")
    print(f"  Auth:     {'yes' if parsed.username else 'none'}")
    print()

    ok = test_tcp_reachable(parsed.hostname, parsed.port or 8080)
    if not ok:
        return 1

    ok = test_connect_auth(proxy_url)
    if not ok:
        return 1

    ok = test_ip_seen(proxy_url)
    if not ok:
        return 1

    test_rotation_api()   # not a showstopper — just informational

    print()
    print("✓ All checks passed. The proxy itself is healthy.")
    print("  If Chrome STILL sees ERR_PROXY_CONNECTION_FAILED, check:")
    print("  • proxy_forwarder.py is listening on localhost (check its log line)")
    print("  • Chrome's --proxy-server flag matches the forwarder's port")
    return 0


if __name__ == "__main__":
    sys.exit(main())
