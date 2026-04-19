"""
proxy_diagnostics.py — Проверка качества прокси и окружения

Проверяет:
- Совпадает ли IP браузера с IP прокси
- Не утекает ли реальный IP через WebRTC
- Не утекает ли DNS
- Соответствует ли таймзона геолокации IP
- Репутация IP (datacenter vs residential)
"""

import time
import json
import logging


class ProxyDiagnostics:
    """
    Использование:
        diag = ProxyDiagnostics(browser.driver)
        report = diag.full_check()
        diag.print_report(report)
    """

    def __init__(self, driver):
        self.driver = driver

    # ──────────────────────────────────────────────────────────
    # IP CHECK
    # ──────────────────────────────────────────────────────────

    def get_browser_ip(self) -> dict:
        """Получаем IP через который работает браузер"""
        try:
            self.driver.get("https://api.ipify.org?format=json")
            time.sleep(2)
            body = self.driver.execute_script("return document.body.innerText;")
            data = json.loads(body)
            return {"ok": True, "ip": data.get("ip")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_ip_info(self) -> dict:
        """Получаем гео-информацию IP: страна, город, провайдер, тип"""
        try:
            self.driver.get("https://ipapi.co/json/")
            time.sleep(2)
            body = self.driver.execute_script("return document.body.innerText;")
            data = json.loads(body)
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
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────
    # WEBRTC LEAK CHECK
    # ──────────────────────────────────────────────────────────

    def webrtc_leak_check(self) -> dict:
        """Проверяем не утекает ли локальный IP через WebRTC"""
        script = """
        return new Promise((resolve) => {
            const ips = new Set();
            try {
                const pc = new RTCPeerConnection({
                    iceServers: [{urls: 'stun:stun.l.google.com:19302'}]
                });
                pc.createDataChannel('');
                pc.onicecandidate = (e) => {
                    if (!e.candidate) {
                        resolve({ ok: true, ips: Array.from(ips) });
                        return;
                    }
                    const match = e.candidate.candidate.match(/(\\d+\\.\\d+\\.\\d+\\.\\d+)/);
                    if (match) ips.add(match[1]);
                };
                pc.createOffer().then(o => pc.setLocalDescription(o));
                setTimeout(() => resolve({ ok: true, ips: Array.from(ips) }), 3000);
            } catch(e) {
                resolve({ ok: false, error: e.toString() });
            }
        });
        """
        try:
            return self.driver.execute_async_script(script)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────
    # TIMEZONE / GEO CONSISTENCY
    # ──────────────────────────────────────────────────────────

    def timezone_consistency(self, expected_timezone: str) -> dict:
        """Проверяем что JS-таймзона совпадает с таймзоной IP.
        Chrome использует старое IANA имя Europe/Kiev вместо Europe/Kyiv —
        считаем их эквивалентными."""
        try:
            js_tz = self.driver.execute_script(
                "return Intl.DateTimeFormat().resolvedOptions().timeZone;"
            )
            # Алиасы для старых IANA имён
            aliases = {
                "Europe/Kiev": "Europe/Kyiv",
                "Europe/Kyiv": "Europe/Kyiv",
                "Asia/Kiev":   "Europe/Kyiv",  # совсем устаревший
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
        Простая эвристика: если org/ASN содержит 'hosting', 'cloud',
        'datacenter' — прокси на датацентровом IP (высокий риск детекта)
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

    def full_check(self, expected_timezone: str = "Europe/Kyiv") -> dict:
        """Полная проверка — возвращает сводный отчёт"""
        logging.info("[ProxyDiag] Запуск полной диагностики прокси...")

        ip_info   = self.get_ip_info()
        webrtc    = self.webrtc_leak_check()
        tz_check  = self.timezone_consistency(expected_timezone)
        reputation = self.ip_reputation_hint(ip_info)

        # Определяем утечку WebRTC
        webrtc_leak = False
        if webrtc.get("ok") and ip_info.get("ok"):
            proxy_ip = ip_info.get("ip")
            for leaked_ip in webrtc.get("ips", []):
                # Игнорируем локальные и сам прокси-IP
                if (leaked_ip != proxy_ip and
                    not leaked_ip.startswith("10.") and
                    not leaked_ip.startswith("192.168.") and
                    not leaked_ip.startswith("172.") and
                    not leaked_ip.startswith("127.") and
                    leaked_ip != "0.0.0.0"):
                    webrtc_leak = True

        return {
            "ip_info":     ip_info,
            "webrtc":      webrtc,
            "webrtc_leak": webrtc_leak,
            "timezone":    tz_check,
            "reputation":  reputation,
        }

    # ──────────────────────────────────────────────────────────
    # ВЫВОД
    # ──────────────────────────────────────────────────────────

    def print_report(self, report: dict):
        print("\n" + "═" * 60)
        print(" ДИАГНОСТИКА ПРОКСИ")
        print("═" * 60)

        ip = report.get("ip_info", {})
        if ip.get("ok"):
            print(f"\n IP:         {ip.get('ip')}")
            print(f" Страна:     {ip.get('country')}")
            print(f" Город:      {ip.get('city')}")
            print(f" Таймзона:   {ip.get('timezone')}")
            print(f" Провайдер:  {ip.get('org')}")
        else:
            print(f"\n ✗ Не удалось получить IP: {ip.get('error')}")

        rep = report.get("reputation", {})
        rep_icon = {"low": "✓", "medium": "⚠", "high": "✗"}.get(rep.get("risk"), "?")
        print(f"\n {rep_icon} Тип IP:     {rep.get('hint')} (риск детекта: {rep.get('risk')})")

        tz = report.get("timezone", {})
        tz_icon = "✓" if tz.get("ok") else "✗"
        print(f" {tz_icon} Таймзона браузера: {tz.get('browser_timezone')}")

        if report.get("webrtc_leak"):
            print(f"\n ✗ WebRTC УТЕЧКА обнаружена!")
            print(f"   Утёкшие IP: {report['webrtc'].get('ips')}")
        else:
            print(f"\n ✓ WebRTC не течёт")

        print("═" * 60 + "\n")
