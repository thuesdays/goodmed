"""
proxy_diagnostics.py — Проверка качества proxy и окрalreadyния

Проверяет:
- Совпаyesет ли IP browserа с IP proxy
- Не утекает ли real IP via WebRTC
- Не утекает ли DNS
- Соответствует ли таймзона геолокации IP
- Репутация IP (datacenter vs residential)
"""

import time
import json
import logging


class ProxyDiagnostics:
    """
    Usage:
        diag = ProxyDiagnostics(browser.driver)
        report = diag.full_check()
        diag.print_report(report)
    """

    def __init__(self, driver, proxy_url: str = None):
        self.driver = driver
        self.proxy_url = proxy_url

    # ──────────────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────
    # IP CHECK
    # ──────────────────────────────────────────────────────────
    #
    # NOTE: We used to have a separate `get_browser_ip()` method that
    # hit api.ipify.org to just get the IP. It was dead code — no
    # caller used it — and each call was a wasted proxy roundtrip.
    # Deleted. `get_ip_info()` below returns IP + geo in one shot
    # via ipapi, which is what full_check() actually needs.

    def get_ip_info(self) -> dict:
        """Fetch geo info via proxy. Tries ipapi.co then ipwhois.app as fallback."""
        import requests
        p_url = self.proxy_url
        if p_url and not p_url.startswith("http"):
            p_url = f"http://{p_url}"
        proxies = {"http": p_url, "https": p_url} if p_url else None

        # Primary: ipapi.co
        try:
            r = requests.get("https://ipapi.co/json/", proxies=proxies, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("ip"):
                return {
                    "ok":       True,
                    "ip":       data.get("ip"),
                    "country":  data.get("country_name"),
                    "city":     data.get("city"),
                    "region":   data.get("region"),
                    "timezone": data.get("timezone"),
                    "org":      data.get("org"),
                    "asn":      data.get("asn"),
                }
        except Exception as e:
            import logging
            logging.debug(f"[ProxyDiag] ipapi.co failed: {e}")

        # Fallback: ipwhois.app (free tier, no API key, no rate limit for small usage)
        try:
            r = requests.get("https://ipwho.is/", proxies=proxies, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("success", True) and data.get("ip"):
                return {
                    "ok":       True,
                    "ip":       data.get("ip"),
                    "country":  data.get("country"),
                    "city":     data.get("city"),
                    "region":   data.get("region"),
                    "timezone": (data.get("timezone") or {}).get("id"),
                    "org":      (data.get("connection") or {}).get("org"),
                    "asn":      (data.get("connection") or {}).get("asn"),
                }
        except Exception as e:
            import logging
            logging.debug(f"[ProxyDiag] ipwho.is failed: {e}")

        return {"ok": False, "error": "all geo services failed"}

    # ──────────────────────────────────────────────────────────
    # WEBRTC LEAK CHECK
    # ──────────────────────────────────────────────────────────

    def webrtc_leak_check(self) -> dict:
        """Checking if leaking локальный IP via WebRTC"""
        # В execute_async_script afterдний аргумент — this callback, that need вызвать
        script = r"""
        const callback = arguments[arguments.length - 1];
        const ips = new Set();
        try {
            const pc = new RTCPeerConnection({
                iceServers: [{urls: 'stun:stun.l.google.com:19302'}]
            });
            pc.createDataChannel('');
            pc.onicecandidate = (e) => {
                if (!e.candidate) {
                    callback({ ok: true, ips: Array.from(ips) });
                    return;
                }
                const match = e.candidate.candidate.match(/(\d+\.\d+\.\d+\.\d+)/);
                if (match) ips.add(match[1]);
            };
            pc.createOffer().then(o => pc.setLocalDescription(o), e => callback({ok: false, error: e.toString()}));
            // Таймаут на случай if stun не ответит
            setTimeout(() => callback({ ok: true, ips: Array.from(ips) }), 5000);
        } catch(e) {
            callback({ ok: false, error: e.toString() });
        }
        """
        try:
            res = self.driver.execute_async_script(script)
            return res if res else {"ok": False, "error": "Empty script result"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────
    # TIMEZONE / GEO CONSISTENCY
    # ──────────────────────────────────────────────────────────

    def timezone_consistency(self, expected_timezone: str) -> dict:
        """Проверяем that JS-таймзона совпаyesет с таймзоной IP.
        Chrome uses legacy IANA name Europe/Kiev instead of Europe/Kyiv —
        сreading их эквивалентными."""
        try:
            js_tz = self.driver.execute_script(
                "return Intl.DateTimeFormat().resolvedOptions().timeZone;"
            )
            # Алиасы for старых IANA имён
            aliases = {
                "Europe/Kiev": "Europe/Kyiv",
                "Europe/Kyiv": "Europe/Kyiv",
                "Asia/Kiev":   "Europe/Kyiv",  # соallм устаревший
            }
            normalized_js       = aliases.get(js_tz, js_tz)
            normalized_expected = aliases.get(expected_timezone, expected_timezone)
            return {
                "ok":               normalized_js == normalized_expected,
                "browser_timezone": js_tz,
                "expected":         expected_timezone,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────
    # IP REPUTATION (datacenter vs residential)
    # ──────────────────────────────────────────────────────────

    def ip_reputation_hint(self, ip_info: dict) -> dict:
        """
        Simple heuristic: if org/ASN contains 'hosting', 'cloud',
        'datacenter' — proxy на yesтацентровом IP (high detection risk)
        """
        if not ip_info.get("ok"):
            return {"hint": "unknown", "risk": "unknown"}

        org = (ip_info.get("org") or "").lower()
        datacenter_markers = [
            "hosting", "cloud", "datacenter", "data center", "dedicated",
            "server", "vps", "ovh", "amazon", "aws", "digitalocean",
            "vultr", "linode", "hetzner", "leaseweb",
        ]
        residential_markers = [
            "telecom", "broadband", "communications", "mobile", "cable",
            "kyivstar", "lifecell", "vodafone", "ukrtelecom", "localnet",
            "triolan", "volia", "fregat", "maxnet", "inet", "datagroup",
            "intertelecom", "megaphone", "mts", "rostelecom", "beeline",
        ]

        if any(m in org for m in datacenter_markers):
            return {"hint": "datacenter", "risk": "high", "org": ip_info.get("org")}
        if any(m in org for m in residential_markers):
            return {"hint": "residential", "risk": "low", "org": ip_info.get("org")}
        return {"hint": "unknown", "risk": "medium", "org": ip_info.get("org")}

    # ──────────────────────────────────────────────────────────
    # FULL CHECK
    # ──────────────────────────────────────────────────────────

    def full_check(self,
                   expected_timezone: str = "Europe/Kyiv",
                   expected_country: str  = None) -> dict:
        """
        Full proxy diagnostics. If expected_country is given (e.g. "Ukraine"),
        the report includes a `geo_mismatch` flag which main.py can use to
        abort the run before wasting a search slot.
        """
        logging.info("[ProxyDiag] Running full proxy diagnostics...")

        ip_info    = self.get_ip_info()
        webrtc     = self.webrtc_leak_check()
        tz_check   = self.timezone_consistency(expected_timezone)
        reputation = self.ip_reputation_hint(ip_info)

        # WebRTC leak detection
        webrtc_leak = False
        if webrtc and webrtc.get("ok") and ip_info and ip_info.get("ok"):
            proxy_ip = ip_info.get("ip")
            for leaked_ip in webrtc.get("ips", []):
                if (leaked_ip != proxy_ip and
                    not leaked_ip.startswith("10.") and
                    not leaked_ip.startswith("192.168.") and
                    not leaked_ip.startswith("172.") and
                    not leaked_ip.startswith("127.") and
                    leaked_ip != "0.0.0.0"):
                    webrtc_leak = True

        # Country mismatch detection — the biggest red flag after IP blacklist.
        # If exit country != expected, Google serves localized SERP for the
        # wrong region AND anti-bot heuristics flag the locale/IP mismatch.
        geo_mismatch = False
        actual_country = (ip_info.get("country") or "").strip()
        if expected_country and actual_country:
            # Case-insensitive, strip common prefixes ("Republic of", etc.)
            exp_lc = expected_country.strip().lower()
            act_lc = actual_country.lower()
            geo_mismatch = (exp_lc not in act_lc and act_lc not in exp_lc)

        return {
            "ip_info":          ip_info,
            "webrtc":           webrtc,
            "webrtc_leak":      webrtc_leak,
            "timezone":         tz_check,
            "reputation":       reputation,
            "expected_country": expected_country,
            "actual_country":   actual_country or None,
            "geo_mismatch":     geo_mismatch,
        }

    # ──────────────────────────────────────────────────────────
    # ВЫВОД
    # ──────────────────────────────────────────────────────────

    def print_report(self, report: dict):
        print("\n" + "═" * 60)
        print(" PROXY DIAGNOSTICS")
        print("═" * 60)

        ip = report.get("ip_info", {})
        if ip.get("ok"):
            print(f"\n IP:         {ip.get('ip')}")
            print(f" Country:     {ip.get('country')}")
            print(f" City:      {ip.get('city')}")
            print(f" Timezone:   {ip.get('timezone')}")
            print(f" Provider:  {ip.get('org')}")
        else:
            print(f"\n ✗ Не уyesлось получить IP: {ip.get('error')}")

        rep = report.get("reputation", {})
        rep_icon = {"low": "✓", "medium": "⚠", "high": "✗"}.get(rep.get("risk"), "?")
        print(f"\n {rep_icon} IP Type:     {rep.get('hint')} (detection risk: {rep.get('risk')})")

        tz = report.get("timezone", {})
        tz_icon = "✓" if tz.get("ok") else "✗"
        print(f" {tz_icon} Timezone browserа: {tz.get('browser_timezone')}")

        if report.get("webrtc_leak"):
            print(f"\n ✗ WebRTC УТЕЧКА обнарalreadyна!")
            print(f"   Leaked IPs: {report['webrtc'].get('ips')}")
        else:
            print(f"\n ✓ No WebRTC leak")

        print("═" * 60 + "\n")


# ════════════════════════════════════════════════════════════════
# Standalone proxy testing (no Chrome needed)
# ════════════════════════════════════════════════════════════════
#
# The class above assumes we already have a Selenium driver. For the
# proxy library page we need to test an arbitrary proxy URL *without*
# launching Chrome — user clicks "Test" on a row, we want a result
# in <5 seconds. This does plain HTTP requests through the proxy.
#
# Data source: ip-api.com free tier (45 req/min, no key). Returns
# country/city/timezone/isp/org/asn plus detection flags:
#   `proxy: true`    → the IP is a known proxy/VPN
#   `hosting: true`  → datacenter IP
#   `mobile: true`   → mobile carrier IP
# We turn those into a residential/datacenter/mobile label + a
# detection-risk rating the UI can display with a colored badge.

def test_proxy(proxy_url: str, timeout: float = 10.0) -> dict:
    """Probe an arbitrary proxy URL and return diagnostics.

    Returns a dict with keys:
        ok (bool), status ('ok'|'error'),
        ip, country, country_code, city, timezone,
        asn, provider, ip_type ('residential'|'datacenter'|'mobile'|'unknown'),
        detection_risk ('low'|'medium'|'high'),
        latency_ms, error (str if ok=False)

    Never raises — errors come back in the dict. Callers are dashboard
    endpoints that want to show a red badge, not crash.
    """
    import requests
    import time as _time

    result = {
        "ok": False, "status": "error",
        "ip": None, "country": None, "country_code": None,
        "city": None, "timezone": None,
        "asn": None, "provider": None,
        "ip_type": "unknown", "detection_risk": "unknown",
        "latency_ms": None, "error": None,
    }

    if not proxy_url:
        result["error"] = "empty proxy url"
        return result

    # Accept bare "user:pass@host:port" by prefixing http://
    pu = proxy_url if proxy_url.startswith(("http://", "https://",
                                             "socks5://", "socks4://"))\
        else f"http://{proxy_url}"
    proxies = {"http": pu, "https": pu}

    started = _time.time()
    try:
        # ip-api.com — everything in one request. fields= trims payload.
        # `proxy`, `hosting`, `mobile` are the detection booleans.
        r = requests.get(
            "http://ip-api.com/json/"
            "?fields=status,message,country,countryCode,city,timezone,"
            "isp,org,as,query,mobile,proxy,hosting",
            proxies=proxies, timeout=timeout,
        )
        latency = int((_time.time() - started) * 1000)
        result["latency_ms"] = latency

        data = r.json()
        if data.get("status") != "success":
            result["error"] = data.get("message") or "ip-api returned no data"
            return result

        # Classify IP type. Priority order matters — a mobile IP might
        # ALSO be flagged proxy=true for some carrier NATs, but it's
        # really mobile in user terms.
        if data.get("mobile"):
            ip_type = "mobile"
        elif data.get("hosting"):
            ip_type = "datacenter"
        elif data.get("proxy"):
            # Known proxy/VPN service but not a hosting range —
            # residential-proxy services land here (Bright Data, IPRoyal).
            ip_type = "residential"
        else:
            ip_type = "residential"

        # Detection risk heuristic — consumers see this as a traffic
        # light. Datacenter or known-proxy = high risk for stealth work;
        # mobile = lowest; plain residential = low.
        if ip_type == "datacenter":
            risk = "high"
        elif data.get("proxy"):
            risk = "medium"
        elif ip_type == "mobile":
            risk = "low"
        else:
            risk = "low"

        # `as` field is "AS12345 Some Org LLC" — split into asn + provider
        as_raw = data.get("as") or ""
        asn = None
        provider = data.get("isp") or data.get("org") or ""
        if as_raw.startswith("AS"):
            parts = as_raw.split(" ", 1)
            asn = parts[0]
            if len(parts) > 1 and not provider:
                provider = parts[1]

        result.update({
            "ok":             True,
            "status":         "ok",
            "ip":             data.get("query"),
            "country":        data.get("country"),
            "country_code":   data.get("countryCode"),
            "city":           data.get("city"),
            "timezone":       data.get("timezone"),
            "asn":            asn or as_raw or None,
            "provider":       provider or None,
            "ip_type":        ip_type,
            "detection_risk": risk,
            "error":          None,
        })
        return result
    except requests.exceptions.Timeout:
        result["error"] = f"timed out after {timeout}s"
        return result
    except requests.exceptions.ProxyError as e:
        # Strip the tedious "HTTPSConnectionPool(...): " prefix that
        # requests wraps around every proxy error — users just want to
        # see "401 Unauthorized" or "Connection refused".
        msg = str(e)
        if ":" in msg:
            msg = msg.rsplit(":", 1)[-1].strip(" ')")
        result["error"] = f"proxy error: {msg}"
        return result
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return result


def parse_proxy_url(proxy_url: str) -> dict:
    """Split a proxy URL into parts for display. Input accepts
       user:pass@host:port  (bare; treated as http)
       http://user:pass@host:port
       socks5://user:pass@host:port
    Returns {type, host, port, login, password}. Missing parts → None.
    """
    from urllib.parse import urlparse
    if not proxy_url:
        return {"type": None, "host": None, "port": None,
                "login": None, "password": None}
    url = proxy_url if proxy_url.startswith(
        ("http://", "https://", "socks5://", "socks4://")) \
        else f"http://{proxy_url}"
    try:
        p = urlparse(url)
        return {
            "type":     p.scheme or "http",
            "host":     p.hostname,
            "port":     p.port,
            "login":    p.username,
            "password": p.password,
        }
    except Exception:
        return {"type": "http", "host": None, "port": None,
                "login": None, "password": None}


# ════════════════════════════════════════════════════════════════
# Multi-format bulk parser
# ════════════════════════════════════════════════════════════════
#
# Proxy providers export in half a dozen different conventions. The
# user pastes a list — we figure out which format (per line, since
# providers sometimes mix) and normalize to a canonical URL.
#
# Supported shapes, checked in order:
#
#   1. Already-canonical URL:
#        http://u:p@host:port  /  socks5://u:p@host:port  /  http://host:port
#
#   2. IPv6 in brackets:
#        [2001:db8::1]:8080  /  user:pass@[2001:db8::1]:8080
#
#   3. Colon-delimited (the "IPRoyal / Webshare" CSV shape):
#        host:port:user:pass    (4 parts, numeric 2nd field)
#        host:port              (2 parts)
#
#   4. user:pass@host:port  (credentials prefix, @ delimiter)
#
#   5. host:port@user:pass  (reversed — rare but some tools export this)
#
#   6. user:pass:host:port  (4 parts, numeric 4th field — harder to
#                             disambiguate from #3, so we require
#                             that the 2nd field isn't a port)
#
# Each line may be preceded/followed by whitespace, and may have a
# trailing "# comment" — we strip both before parsing.
#
# An optional `default_scheme` on the row (e.g. "http" vs "socks5")
# lets the UI offer a checkbox "assume SOCKS5 for lines without
# scheme" for users pasting SOCKS-only lists.

import re as _re_parse

# An IP:port token we can match at the START of a line. Handles v4
# and [v6].
_RX_HOST_PORT = _re_parse.compile(
    r"^(?:\[([0-9a-fA-F:]+)\]|([^\s:@]+)):([0-9]{1,5})"
)


def parse_proxy_line(line: str, default_scheme: str = "http") -> dict | None:
    """Parse ONE line from a bulk-import paste. Returns a dict:
        {
          "ok": True,
          "url": "http://user:pass@host:port",
          "type": "http",
          "host": "1.2.3.4",
          "port": 8080,
          "login": "user",
          "password": "pass",
          "raw": "<original line>",
          "format": "<which format matched>",
        }
    or {ok: False, raw, error} for junk.
    Caller iterates lines, collects oks, shows errors to user as
    "these N lines skipped".
    """
    if line is None:
        return None
    raw = line
    # Trim comments (anything after `#`) and surrounding whitespace.
    # Must strip comments BEFORE looking for `@` because a comment like
    # "# test@example" could confuse the credentials detector.
    hash_pos = line.find("#")
    if hash_pos >= 0:
        line = line[:hash_pos]
    line = line.strip()
    if not line:
        return None

    # 1. Canonical URL (has scheme://)
    m = _re_parse.match(r"^(https?|socks[45])://(.+)$", line, _re_parse.I)
    if m:
        scheme = m.group(1).lower()
        rest = m.group(2)
        parsed = _split_authority(rest)
        if parsed:
            host, port, login, password = parsed
            return _ok(raw, "canonical", scheme, host, port, login, password)

    # 2-6. No scheme → must infer. Check credential patterns first
    # because `user:pass@host:port` includes colons that would
    # mis-parse as the 4-part host:port:user:pass shape.

    # 4. user:pass@host:port
    if "@" in line and line.count("@") == 1:
        left, right = line.split("@", 1)
        # Either half could be host:port — disambiguate by presence
        # of a plausible port number.
        p_right = _parse_host_port(right)
        p_left  = _parse_host_port(left)
        if p_right:
            # Standard: creds@host:port
            host, port = p_right
            if ":" in left:
                login, password = left.split(":", 1)
            else:
                login, password = left, ""
            return _ok(raw, "creds_at_host_port", default_scheme,
                       host, port, login, password)
        if p_left:
            # Reversed: host:port@creds (format #5)
            host, port = p_left
            if ":" in right:
                login, password = right.split(":", 1)
            else:
                login, password = right, ""
            return _ok(raw, "host_port_at_creds", default_scheme,
                       host, port, login, password)

    # 3 + 6. Colon-separated. Count the colons to decide.
    # Bracketed IPv6 complicates — extract it first if present.
    m = _re_parse.match(r"^\[([0-9a-fA-F:]+)\]:([0-9]{1,5})(:(.+))?$", line)
    if m:
        host = m.group(1)
        port = int(m.group(2))
        tail = m.group(4) or ""
        login, password = ("", "")
        if tail:
            if ":" in tail:
                login, password = tail.split(":", 1)
            else:
                login = tail
        return _ok(raw, "ipv6_colon", default_scheme,
                   host, port, login, password)

    parts = line.split(":")
    if len(parts) == 2:
        # host:port
        host, port_s = parts
        port = _parse_port(port_s)
        if port:
            return _ok(raw, "host_port", default_scheme,
                       host, port, "", "")
    elif len(parts) == 4:
        # Two candidates:
        #   (a) host:port:user:pass   — parts[1] is port
        #   (b) user:pass:host:port   — parts[3] is port
        port_a = _parse_port(parts[1])
        port_b = _parse_port(parts[3])
        # If only one of them parses as a port, we have a winner.
        if port_a and not port_b:
            return _ok(raw, "host_port_user_pass", default_scheme,
                       parts[0], port_a, parts[2], parts[3])
        if port_b and not port_a:
            return _ok(raw, "user_pass_host_port", default_scheme,
                       parts[2], port_b, parts[0], parts[1])
        if port_a and port_b:
            # Both look like ports. Tie-break by checking which side
            # looks like a hostname (contains a dot or is numeric-IP).
            if _looks_like_host(parts[0]):
                return _ok(raw, "host_port_user_pass", default_scheme,
                           parts[0], port_a, parts[2], parts[3])
            if _looks_like_host(parts[2]):
                return _ok(raw, "user_pass_host_port", default_scheme,
                           parts[2], port_b, parts[0], parts[1])
            # Still ambiguous — assume host:port:user:pass (the
            # IPRoyal/Webshare convention, which is more common).
            return _ok(raw, "host_port_user_pass_ambiguous",
                       default_scheme, parts[0], port_a,
                       parts[2], parts[3])
    elif len(parts) == 3:
        # Unusual. host:port:user   (no pass) or user:pass:host  (no port)
        port_a = _parse_port(parts[1])
        if port_a:
            return _ok(raw, "host_port_user", default_scheme,
                       parts[0], port_a, parts[2], "")
        # user:pass:host — no explicit port; reject since we need one
        return _err(raw, f"ambiguous 3-field line (need port): {line!r}")

    return _err(raw, f"could not parse as a proxy line: {line!r}")


def _split_authority(s: str):
    """Take the part after `scheme://` and split into host/port/login/pw.
    Returns (host, port, login, password) or None if unparseable."""
    login = password = ""
    if "@" in s:
        creds, s = s.rsplit("@", 1)
        if ":" in creds:
            login, password = creds.split(":", 1)
        else:
            login = creds
    p = _parse_host_port(s)
    if not p:
        return None
    host, port = p
    return host, port, login, password


def _parse_host_port(s: str):
    """Return (host, port) or None. Handles [v6]:port and v4:port."""
    s = s.strip().rstrip("/")
    m = _re_parse.match(r"^\[([0-9a-fA-F:]+)\]:([0-9]{1,5})$", s)
    if m:
        return m.group(1), int(m.group(2))
    if ":" not in s:
        return None
    host, _, port_s = s.rpartition(":")
    port = _parse_port(port_s)
    if not host or not port:
        return None
    return host, port


def _parse_port(s: str):
    s = (s or "").strip()
    if not s.isdigit():
        return None
    n = int(s)
    return n if 1 <= n <= 65535 else None


def _looks_like_host(s: str) -> bool:
    """True if s looks more like a hostname than like credentials.
    A token with a dot or one that parses as an integer-only (IPv4
    component) is probably a host."""
    if "." in s:
        return True
    return s.isdigit() and len(s) <= 3   # single IPv4 octet

def _ok(raw, fmt, scheme, host, port, login, password):
    scheme = scheme or "http"
    if login or password:
        authority = f"{login}:{password}@{host}:{port}" if password \
                    else f"{login}@{host}:{port}"
    else:
        authority = f"{host}:{port}"
    # IPv6 hosts must be bracketed in the URL form
    if ":" in host and not host.startswith("["):
        authority = authority.replace(host, f"[{host}]", 1)
    url = f"{scheme}://{authority}"
    return {
        "ok": True, "raw": raw, "format": fmt,
        "url": url, "type": scheme,
        "host": host, "port": port,
        "login": login or "", "password": password or "",
    }


def _err(raw, msg):
    return {"ok": False, "raw": raw, "error": msg}


def parse_proxy_list(text: str, default_scheme: str = "http") -> dict:
    """Parse a multi-line bulk paste. Returns:
        {
          "valid":  [ <proxy dict>, ... ],
          "errors": [ {"line": N, "raw": ..., "error": ...}, ... ],
          "total":  total lines (excluding blanks/comments),
        }
    """
    valid = []
    errors = []
    total = 0
    lines = (text or "").splitlines()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        total += 1
        result = parse_proxy_line(line, default_scheme=default_scheme)
        if result is None:
            continue
        if result.get("ok"):
            valid.append(result)
        else:
            errors.append({
                "line":  i,
                "raw":   result["raw"],
                "error": result["error"],
            })
    return {"valid": valid, "errors": errors, "total": total}
