"""
proxy_forwarder.py — Локальный TCP-форвардер для авторизованных прокси

Решает проблему: Chrome не умеет работать с прокси-аутентификацией напрямую
без расширений, а расширения не всегда работают с undetected_chromedriver.

Как работает:
1. Слушает на 127.0.0.1:<случайный_порт>
2. Принимает подключения от Chrome (без аутентификации)
3. Добавляет заголовок Proxy-Authorization: Basic ... в CONNECT и HTTP запросы
4. Пересылает всё через настоящий авторизованный прокси
5. После установки туннеля — просто проксирует TCP-трафик в обе стороны

Поддерживает HTTPS (CONNECT tunneling) и plain HTTP.
"""

import base64
import logging
import select
import socket
import threading
from urllib.parse import urlparse


class ProxyForwarder:
    """
    Использование:
        fwd = ProxyForwarder("user:pass@host:port")
        local_port = fwd.start()
        # Передать Chrome: --proxy-server=127.0.0.1:<local_port>
        # ...
        fwd.stop()
    """

    def __init__(self, upstream: str):
        """
        upstream: "user:pass@host:port" или "host:port"
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

    # ──────────────────────────────────────────────────────────

    def start(self) -> int:
        """Запускает форвардер, возвращает локальный порт"""
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
            # Читаем первые данные от Chrome — это CONNECT или HTTP-запрос
            client.settimeout(30)
            data = self._read_headers(client)
            if not data:
                return

            # Коннектимся к апстрим-прокси
            upstream = socket.create_connection(
                (self.up_host, self.up_port), timeout=30
            )
            upstream.settimeout(30)

            # Вставляем Proxy-Authorization в заголовки
            modified = self._inject_auth(data)
            upstream.sendall(modified)

            # Дальше просто двусторонний TCP-форвардинг
            client.settimeout(None)
            upstream.settimeout(None)
            self._forward(client, upstream)

        except Exception as e:
            logging.debug(f"[ProxyForwarder] Ошибка соединения: {e}")
        finally:
            for sock in (client, upstream):
                try:
                    if sock:
                        sock.close()
                except Exception:
                    pass

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
        """Добавляет Proxy-Authorization в HTTP-запрос"""
        if not self._auth_header:
            return data

        # Находим конец первой строки (request-line)
        nl = data.find(b"\r\n")
        if nl == -1:
            return data

        request_line = data[:nl]
        rest         = data[nl + 2:]

        # Проверяем нет ли уже Proxy-Authorization
        if b"proxy-authorization:" in rest.lower():
            return data  # уже есть, не дублируем

        # Вставляем наш заголовок сразу после request-line
        return request_line + b"\r\n" + self._auth_header + b"\r\n" + rest

    @staticmethod
    def _forward(sock1: socket.socket, sock2: socket.socket):
        """Двусторонний TCP-форвардинг между двумя сокетами"""
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
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# CLI — быстрый тест
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) < 2:
        print("Использование: python proxy_forwarder.py user:pass@host:port")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO)
    fwd = ProxyForwarder(sys.argv[1])
    port = fwd.start()
    print(f"Локальный прокси: http://127.0.0.1:{port}")
    print("Теперь можешь проверить:")
    print(f"  curl -x http://127.0.0.1:{port} https://api.ipify.org")
    print("Ctrl+C чтобы остановить")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        fwd.stop()
