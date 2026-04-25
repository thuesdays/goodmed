"""
proxy_forwarder.py — Локальный TCP-форвардер for авторизованных proxy

Решает проблему: Chrome не умеет работать с proxy-аутентификацией напрямую
without расширений, а расширения не всегда работают с undetected_chromedriver.

Как works:
1. Слушает на 127.0.0.1:<случайный_порт>
2. Принимает подключения от Chrome (without аутентификации)
3. Добавляет заголовок Proxy-Authorization: Basic ... в CONNECT и HTTP queryы
4. Пересылает everything via настоящий авторизованный proxy
5. После установки туннеля — просто proxyрует TCP-трафик в обе стороны

Поддерживает HTTPS (CONNECT tunneling) и plain HTTP.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import base64
import logging
import select
import socket
import threading
from urllib.parse import urlparse


class ProxyForwarder:
    """
    Usage:
        fwd = ProxyForwarder("user:pass@host:port")
        local_port = fwd.start()
        # Передать Chrome: --proxy-server=127.0.0.1:<local_port>
        # ...
        fwd.stop()
    """

    def __init__(self, upstream: str):
        """
        upstream: "user:pass@host:port" or "host:port"
        """
        # Нормализуем формат
        if "://" not in upstream:
            upstream = "http://" + upstream
        parsed = urlparse(upstream)

        self.up_host = parsed.hostname
        self.up_port = parsed.port or 8080
        self.up_user = parsed.username
        self.up_pass = parsed.password

        self._auth_header = b""
        if self.up_user and self.up_pass:
            token = base64.b64encode(
                f"{self.up_user}:{self.up_pass}".encode()
            ).decode()
            self._auth_header = f"Proxy-Authorization: Basic {token}".encode()

        self.local_port = None
        self._server   = None
        self._stop     = threading.Event()

        # ── Per-host traffic counters (authoritative) ──────────────
        # Populated by _handle() on each new connection and updated by
        # _forward() as bytes flow through. Readers (TrafficCollector)
        # call drain_counters() to read + reset. Keyed by the CONNECT
        # target host (for HTTPS) or the Host header (for plain HTTP).
        # We can't break CONNECT tunnels apart into individual requests
        # — they're encrypted — but per-host aggregation is exactly
        # what traffic_stats schema wants anyway.
        self._counters       = {}   # host -> {"bytes": N, "req_count": M}
        self._counters_lock  = threading.Lock()

    # ──────────────────────────────────────────────────────────

    def start(self) -> int:
        """Overпускает форвардер, возвращает локальный порт"""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))  # 0 = случайный свободный порт
        self._server.listen(128)
        self.local_port = self._server.getsockname()[1]

        threading.Thread(target=self._accept_loop, daemon=True).start()
        logging.info(
            f"[ProxyForwarder] 127.0.0.1:{self.local_port} → "
            f"{self.up_host}:{self.up_port}"
        )
        return self.local_port

    def stop(self):
        self._stop.set()
        try:
            self._server.close()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
                threading.Thread(
                    target=self._handle,
                    args=(client,),
                    daemon=True,
                ).start()
            except Exception:
                break

    def _handle(self, client: socket.socket):
        """Обрабатывает одно подключение от Chrome"""
        upstream = None
        try:
            # Читаем первые данные от Chrome — this CONNECT or HTTP-query
            client.settimeout(30)
            data = self._read_headers(client)
            if not data:
                return

            # Extract target host for byte accounting. Two cases:
            #   CONNECT target.example.com:443 HTTP/1.1  — HTTPS tunnel
            #   GET http://plain.example.com/x HTTP/1.1  — plain HTTP
            target_host = self._extract_target_host(data)

            # Коннектимся к апстрим-proxy
            upstream = socket.create_connection(
                (self.up_host, self.up_port), timeout=30
            )
            upstream.settimeout(30)

            # Вставляем Proxy-Authorization в заголовки
            modified = self._inject_auth(data)
            upstream.sendall(modified)
            # Count the headers byte stream too — proxy-level billing
            # includes them. Small (few hundred bytes) but accumulates
            # across thousands of requests.
            if target_host:
                self._add(target_host, len(modified), 1)

            # Дальше просто двусторонний TCP-форвардинг
            client.settimeout(None)
            upstream.settimeout(None)
            self._forward(client, upstream, target_host)

        except Exception as e:
            logging.debug(f"[ProxyForwarder] Error соединения: {e}")
        finally:
            for sock in (client, upstream):
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

    # ──────────────────────────────────────────────────────────────
    # Byte accounting
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_target_host(data: bytes) -> str:
        """Find the eventual destination host from the first HTTP
        request line / CONNECT line. Returns "" if unparseable so
        _handle can still forward without crashing."""
        try:
            first_line, _, rest = data.partition(b"\r\n")
            # CONNECT host:port HTTP/1.1  — HTTPS tunnel
            if first_line.startswith(b"CONNECT "):
                parts = first_line.split(b" ")
                if len(parts) >= 2:
                    host_port = parts[1].decode("latin-1", errors="replace")
                    return host_port.split(":")[0].lower()
            # GET http://host/path HTTP/1.1  — plain HTTP through proxy
            # Or any method with an absolute URL in the request line.
            parts = first_line.split(b" ")
            if len(parts) >= 2:
                target = parts[1].decode("latin-1", errors="replace")
                if target.startswith(("http://", "https://")):
                    return urlparse(target).hostname or ""
            # Last resort — look for Host: header
            for line in rest.split(b"\r\n"):
                low = line.lower()
                if low.startswith(b"host:"):
                    host = line.split(b":", 1)[1].strip()
                    return host.decode("latin-1", errors="replace").split(":")[0].lower()
        except Exception:
            pass
        return ""

    def _add(self, host: str, bytes_count: int, req_count: int = 0):
        """Atomically increment counters for a host."""
        if not host or (bytes_count <= 0 and req_count <= 0):
            return
        with self._counters_lock:
            slot = self._counters.setdefault(host, {"bytes": 0, "req_count": 0})
            slot["bytes"]     += bytes_count
            slot["req_count"] += req_count

    def drain_counters(self) -> dict:
        """Return a snapshot of counters and reset them to zero. Called
        by TrafficCollector on each flush — it takes what we've got,
        writes to DB, and we start accumulating fresh. Returns a dict
        shaped for traffic_record_batch's by_domain param."""
        with self._counters_lock:
            snapshot = self._counters
            self._counters = {}
        return snapshot

    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _read_headers(sock: socket.socket) -> bytes:
        """Читает из сокета до \\r\\n\\r\\n (конец HTTP-заголовков)"""
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 65536:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk
        return buf

    def _inject_auth(self, data: bytes) -> bytes:
        """Добавляет Proxy-Authorization в HTTP-query"""
        if not self._auth_header:
            return data

        # Находим конец первой строки (request-line)
        nl = data.find(b"\r\n")
        if nl == -1:
            return data

        request_line = data[:nl]
        rest         = data[nl + 2:]

        # Проверяем no ли already Proxy-Authorization
        if b"proxy-authorization:" in rest.lower():
            return data  # already is, не дублируем

        # Вставляем our заголовок сразу after request-line
        return request_line + b"\r\n" + self._auth_header + b"\r\n" + rest

    def _forward(self, sock1: socket.socket, sock2: socket.socket,
                 target_host: str = ""):
        """Двусторонний TCP-форвардинг между двумя сокетами. Bytes
        flowing in EITHER direction count toward target_host — the proxy
        bill sums both upload and download (asocks charges for total
        TCP throughput through the tunnel, not just downloads)."""
        sockets = [sock1, sock2]
        try:
            while True:
                readable, _, _ = select.select(sockets, [], sockets, 60)
                if not readable:
                    break
                for s in readable:
                    try:
                        data = s.recv(65536)
                    except Exception:
                        return
                    if not data:
                        return
                    target = sock2 if s is sock1 else sock1
                    try:
                        target.sendall(data)
                    except Exception:
                        return
                    # Byte accounting — independent of direction. For
                    # HTTPS tunnels this is the only visibility we have
                    # into bytes per host (payload is encrypted).
                    if target_host:
                        self._add(target_host, len(data), 0)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# CLI — fast тест
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) < 2:
        print("Usage: python proxy_forwarder.py user:pass@host:port")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO)
    fwd = ProxyForwarder(sys.argv[1])
    port = fwd.start()
    print(f"Локальный proxy: http://127.0.0.1:{port}")
    print("Теперь можешь check:")
    print(f"  curl -x http://127.0.0.1:{port} https://api.ipify.org")
    print("Ctrl+C тотбы остановить")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fwd.stop()
