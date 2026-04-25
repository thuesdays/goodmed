"""
proxy_pool.py — Пул прокси с ротацией по здоровью

Управляет несколькими прокси — выбирает самый "здоровый" для каждой сессии,
помечает сгоревшие, делает cooldown. Это основа долгоживущих антидетектов.

Формат proxies.json:
[
  {"id": "proxy_1", "url": "user:pass@host:port", "label": "asocks-kyiv"},
  {"id": "proxy_2", "url": "user:pass@host:port", "label": "asocks-kyiv-2"}
]
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import random
import logging
from datetime import datetime, timedelta


class ProxyPool:
    """
    Использование:
        pool = ProxyPool("proxies.json")
        proxy = pool.acquire()         # получить свободный прокси
        # ... работа ...
        pool.report(proxy["id"], success=True)   # отчитаться
        pool.release(proxy["id"])                # вернуть в пул
    """

    # Статусы прокси
    HEALTHY   = "healthy"
    WARNING   = "warning"   # есть капчи, но работает
    COOLDOWN  = "cooldown"  # временная пауза
    BURNED    = "burned"    # сгорел, не использовать

    # Параметры
    COOLDOWN_MIN       = 30    # минут отдыха при warning
    COOLDOWN_MAX       = 180   # минут отдыха при серии капч
    BURN_AFTER_FAILS   = 10    # после скольких подряд капч считаем сгоревшим
    WARN_CAPTCHA_RATE  = 0.3
    CRIT_CAPTCHA_RATE  = 0.6

    def __init__(self, proxies_file: str = "proxies.json", state_file: str = "proxy_pool_state.json"):
        self.proxies_file = proxies_file
        self.state_file   = state_file
        self.proxies      = self._load_proxies()
        self.state        = self._load_state()

    # ──────────────────────────────────────────────────────────
    # ЗАГРУЗКА / СОХРАНЕНИЕ
    # ──────────────────────────────────────────────────────────

    def _load_proxies(self) -> list[dict]:
        if not os.path.exists(self.proxies_file):
            raise FileNotFoundError(
                f"Файл {self.proxies_file} не найден. "
                f"Создай его в формате [{{\"id\":\"p1\",\"url\":\"user:pass@host:port\"}}]"
            )
        with open(self.proxies_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.warning(f"[ProxyPool] save_state: {e}")

    def _proxy_state(self, proxy_id: str) -> dict:
        """Получить (или создать) состояние конкретного прокси"""
        if proxy_id not in self.state:
            self.state[proxy_id] = {
                "status":             self.HEALTHY,
                "total_uses":         0,
                "total_success":      0,
                "total_captchas":     0,
                "consecutive_fails":  0,
                "cooldown_until":     None,
                "last_used":          None,
                "in_use":             False,
                "burned_at":          None,
            }
        return self.state[proxy_id]

    # ──────────────────────────────────────────────────────────
    # ПОЛУЧЕНИЕ ПРОКСИ
    # ──────────────────────────────────────────────────────────

    def acquire(self, strategy: str = "best_health") -> dict | None:
        """
        Получить свободный прокси.
        strategy:
          "best_health" — самый здоровый первый
          "round_robin" — по кругу (тоже фильтрует cooldown и burned)
          "random"      — случайный из здоровых
        """
        now = datetime.now()
        available = []

        for proxy in self.proxies:
            state = self._proxy_state(proxy["id"])

            if state["in_use"]:
                continue
            if state["status"] == self.BURNED:
                continue
            if state["status"] == self.COOLDOWN:
                if state["cooldown_until"]:
                    until = datetime.fromisoformat(state["cooldown_until"])
                    if now < until:
                        continue
                    # Cooldown закончился — возвращаем в warning
                    state["status"]         = self.WARNING
                    state["cooldown_until"] = None

            available.append(proxy)

        if not available:
            logging.warning("[ProxyPool] Нет доступных прокси!")
            # Покажем статус каждого для отладки
            self.print_status()
            return None

        # Стратегия выбора
        if strategy == "random":
            chosen = random.choice(available)
        elif strategy == "round_robin":
            # Выбираем тот что дольше не использовался
            available.sort(
                key=lambda p: self._proxy_state(p["id"]).get("last_used") or "0"
            )
            chosen = available[0]
        else:  # best_health
            # Сортируем по убыванию: сначала healthy, потом warning
            # Внутри группы — по captcha rate (меньше лучше)
            def health_score(p):
                s       = self._proxy_state(p["id"])
                captcha_rate = (s["total_captchas"] / s["total_uses"]) if s["total_uses"] else 0
                status_score = {self.HEALTHY: 0, self.WARNING: 1}.get(s["status"], 99)
                return (status_score, captcha_rate, s["total_uses"])
            available.sort(key=health_score)
            chosen = available[0]

        # Помечаем как используемый
        state               = self._proxy_state(chosen["id"])
        state["in_use"]     = True
        state["last_used"]  = now.isoformat(timespec="seconds")
        state["total_uses"] += 1
        self._save_state()

        logging.info(
            f"[ProxyPool] ✓ Выдан {chosen['id']} "
            f"({chosen.get('label', '?')}, status={state['status']})"
        )
        return chosen

    def release(self, proxy_id: str):
        """Возвращает прокси в пул"""
        state = self._proxy_state(proxy_id)
        state["in_use"] = False
        self._save_state()

    # ──────────────────────────────────────────────────────────
    # ОТЧЁТНОСТЬ
    # ──────────────────────────────────────────────────────────

    def report(self, proxy_id: str, success: bool = True, captcha: bool = False,
               blocked: bool = False):
        """
        Сообщить результат использования прокси.
        """
        state = self._proxy_state(proxy_id)

        if captcha:
            state["total_captchas"]    += 1
            state["consecutive_fails"] += 1
        elif blocked:
            state["consecutive_fails"] += 1
        elif success:
            state["total_success"]    += 1
            state["consecutive_fails"] = 0

        # Пересчёт статуса
        self._recalc_status(proxy_id)
        self._save_state()

    def _recalc_status(self, proxy_id: str):
        """Пересчитывает статус на основе метрик"""
        state = self._proxy_state(proxy_id)

        # Серия провалов → burned
        if state["consecutive_fails"] >= self.BURN_AFTER_FAILS:
            state["status"]    = self.BURNED
            state["burned_at"] = datetime.now().isoformat(timespec="seconds")
            logging.error(f"[ProxyPool] 🔥 {proxy_id} помечен как BURNED")
            return

        # Capcha rate
        if state["total_uses"] > 5:
            captcha_rate = state["total_captchas"] / state["total_uses"]

            if captcha_rate >= self.CRIT_CAPTCHA_RATE:
                # Серьёзный cooldown
                until = datetime.now() + timedelta(minutes=self.COOLDOWN_MAX)
                state["status"]         = self.COOLDOWN
                state["cooldown_until"] = until.isoformat(timespec="seconds")
                logging.warning(f"[ProxyPool] ⏸ {proxy_id} → cooldown до {until.strftime('%H:%M')}")
                return
            elif captcha_rate >= self.WARN_CAPTCHA_RATE:
                state["status"] = self.WARNING
                return

        # Consecutive fails но мало — короткий cooldown
        if state["consecutive_fails"] >= 3:
            until = datetime.now() + timedelta(minutes=self.COOLDOWN_MIN)
            state["status"]         = self.COOLDOWN
            state["cooldown_until"] = until.isoformat(timespec="seconds")
            logging.warning(f"[ProxyPool] ⏸ {proxy_id} → short cooldown")
            return

        state["status"] = self.HEALTHY

    # ──────────────────────────────────────────────────────────
    # КОНТЕКСТ-МЕНЕДЖЕР
    # ──────────────────────────────────────────────────────────

    def checkout(self, strategy: str = "best_health"):
        """
        Использование:
            with pool.checkout() as proxy:
                if not proxy: return
                # ... работаем ...
                pool.report(proxy["id"], success=True)
        """
        return _ProxyCheckout(self, strategy)

    # ──────────────────────────────────────────────────────────
    # УПРАВЛЕНИЕ
    # ──────────────────────────────────────────────────────────

    def unburn(self, proxy_id: str):
        """Вручную разжечь (отметить как healthy) сгоревший прокси"""
        state = self._proxy_state(proxy_id)
        state["status"]            = self.HEALTHY
        state["consecutive_fails"] = 0
        state["cooldown_until"]    = None
        state["burned_at"]         = None
        self._save_state()
        logging.info(f"[ProxyPool] {proxy_id} → healthy (ручной сброс)")

    def reset_all(self):
        """Сбрасывает статусы всех прокси"""
        for proxy in self.proxies:
            self.unburn(proxy["id"])

    def print_status(self):
        """Красивый вывод состояния пула"""
        print("\n" + "═" * 80)
        print(f" ПУЛ ПРОКСИ ({len(self.proxies)} шт.)")
        print("═" * 80)
        print(f" {'ID':<15} {'Label':<20} {'Status':<10} {'Uses':>6} {'Captcha':>8} {'Rate':>7}")
        print(" " + "─" * 78)

        for proxy in self.proxies:
            s = self._proxy_state(proxy["id"])
            rate = (s["total_captchas"] / s["total_uses"]) if s["total_uses"] else 0
            status_icon = {
                self.HEALTHY: "🟢",
                self.WARNING: "🟡",
                self.COOLDOWN: "🟠",
                self.BURNED: "🔴",
            }.get(s["status"], "?")

            print(
                f" {proxy['id']:<15} "
                f"{proxy.get('label', '-')[:20]:<20} "
                f"{status_icon} {s['status']:<8} "
                f"{s['total_uses']:>6} "
                f"{s['total_captchas']:>8} "
                f"{rate:>6.1%}"
            )

            if s.get("cooldown_until"):
                until = datetime.fromisoformat(s["cooldown_until"])
                remaining = (until - datetime.now()).total_seconds() / 60
                if remaining > 0:
                    print(f"   cooldown ещё {remaining:.0f} мин")
        print("═" * 80 + "\n")


class _ProxyCheckout:
    """Контекст-менеджер для acquire/release"""
    def __init__(self, pool: ProxyPool, strategy: str):
        self.pool     = pool
        self.strategy = strategy
        self.proxy    = None

    def __enter__(self):
        self.proxy = self.pool.acquire(self.strategy)
        return self.proxy

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.proxy:
            self.pool.release(self.proxy["id"])
        return False


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if len(sys.argv) < 2:
        print("Команды:")
        print("  python proxy_pool.py status      — статус пула")
        print("  python proxy_pool.py reset       — сбросить все burned")
        print("  python proxy_pool.py unburn <id> — разжечь один прокси")
        sys.exit(0)

    pool = ProxyPool()
    cmd = sys.argv[1]

    if cmd == "status":
        pool.print_status()
    elif cmd == "reset":
        pool.reset_all()
        print("✓ Все прокси сброшены")
    elif cmd == "unburn" and len(sys.argv) > 2:
        pool.unburn(sys.argv[2])
    else:
        print("Неизвестная команда")
