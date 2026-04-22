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
    # IP CHECK
    # ──────────────────────────────────────────────────────────

    def get_browser_ip(self) -> dict:
        """Получаем IP via requests (бесшумно)"""
        import requests
        # Добавляем протокол if его no (need for requests)
        p_url = self.proxy_url
        if p_url and not p_url.startswith("http"):
            p_url = f"http://{p_url}"
        proxies = {"http": p_url, "https": p_url} if p_url else None
        # Try multiple IP services — any single one may be blocked / rate-limited
        # through datacenter proxies (very common for asocks-style endpoints).
        services = [
            "https://api.ipify.org?format=json",
            "https://ifconfig.co/json",
            "https://api.myip.com",
        ]
        last_err = None
        for url in services:
            try:
                r = requests.get(url, proxies=proxies, timeout=10)
                r.raise_for_status()
                data = r.json()
                ip = data.get("ip") or data.get("IP")
                if ip:
                    return {"ok": True, "ip": ip}
            except Exception as e:
                last_err = str(e)
                continue
        import logging
        logging.debug(f"[ProxyDiag] all IP services failed: {last_err}")
        return {"ok": False, "error": last_err or "no service responded"}

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
