"""
profile_pool.py — Оркестратор пула профилей

Управляет комбинацией несколько профилей × несколько прокси.
При запуске выбирает здоровую пару (profile, proxy), работает с ней,
отчитывается о результатах.

Это главный интерфейс для масштабной автоматизации — именно так
работают коммерческие антидетект-системы типа Dolphin Teams.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import random
import logging
from datetime import datetime

from ghost_shell.session.quality import SessionQualityMonitor
from ghost_shell.proxy.pool import ProxyPool


class ProfilePool:
    """
    Использование:
        pool = ProfilePool(
            profiles_dir="profiles",
            min_profiles=5,   # авто-создание до этого числа
        )

        # Получаем здоровую пару профиль+прокси
        profile, proxy = pool.acquire_pair()

        with NKBrowser(profile_name=profile, proxy_str=proxy["url"]) as browser:
            # ... работаем ...
            pass

        pool.release_pair(profile, proxy["id"])
    """

    def __init__(
        self,
        profiles_dir:  str = "profiles",
        proxy_pool:    ProxyPool = None,
        min_profiles:  int = 3,
        device_templates: list = None,
    ):
        self.profiles_dir     = profiles_dir
        self.proxy_pool       = proxy_pool or ProxyPool()
        self.min_profiles     = min_profiles
        self.device_templates = device_templates or [
            "office_laptop", "office_desktop", "gaming_mid", "amd_desktop_mid"
        ]

        os.makedirs(profiles_dir, exist_ok=True)
        self._ensure_minimum_profiles()

    # ──────────────────────────────────────────────────────────
    # СПИСОК ПРОФИЛЕЙ
    # ──────────────────────────────────────────────────────────

    def list_profiles(self) -> list[str]:
        """Возвращает имена всех профилей в папке"""
        if not os.path.exists(self.profiles_dir):
            return []
        return sorted([
            name for name in os.listdir(self.profiles_dir)
            if os.path.isdir(os.path.join(self.profiles_dir, name))
        ])

    def _ensure_minimum_profiles(self):
        """Если профилей меньше min_profiles — создаём пустые папки"""
        profiles = self.list_profiles()
        if len(profiles) >= self.min_profiles:
            return

        needed = self.min_profiles - len(profiles)
        logging.info(f"[ProfilePool] Создаём {needed} профилей до минимума")

        for i in range(needed):
            # Находим свободный номер
            n = 1
            while f"profile_{n:02d}" in profiles:
                n += 1
            name = f"profile_{n:02d}"
            os.makedirs(os.path.join(self.profiles_dir, name), exist_ok=True)
            profiles.append(name)
            logging.info(f"[ProfilePool] + {name}")

    # ──────────────────────────────────────────────────────────
    # ЗДОРОВЬЕ ПРОФИЛЕЙ
    # ──────────────────────────────────────────────────────────

    def get_profile_health(self, profile_name: str) -> dict:
        """Возвращает здоровье конкретного профиля"""
        profile_path = os.path.join(self.profiles_dir, profile_name)
        sqm = SessionQualityMonitor(profile_path)
        return sqm.get_health()

    def get_all_health(self) -> dict[str, dict]:
        """Здоровье всех профилей"""
        return {
            name: self.get_profile_health(name)
            for name in self.list_profiles()
        }

    # ──────────────────────────────────────────────────────────
    # ВЫБОР ПРОФИЛЯ
    # ──────────────────────────────────────────────────────────

    def acquire_profile(self) -> str | None:
        """
        Выбирает лучший доступный профиль.
        Приоритет: healthy > warning > critical не выбираем.
        Между одинаковыми — у кого давно не было капчи.
        """
        profiles = self.list_profiles()
        if not profiles:
            return None

        # Фильтруем критичные + собираем метрики
        candidates = []
        for name in profiles:
            # Проверяем занятость
            lock_file = os.path.join(self.profiles_dir, name, ".in_use")
            if os.path.exists(lock_file):
                continue

            health = self.get_profile_health(name)
            if health["status"] == "critical":
                logging.warning(f"[ProfilePool] ⛔ {name} пропущен (critical)")
                continue

            candidates.append((name, health))

        if not candidates:
            logging.error("[ProfilePool] Нет доступных здоровых профилей!")
            return None

        # Сортируем: healthy → warning, внутри группы — по возрастанию capcha_rate
        status_priority = {"healthy": 0, "warning": 1}
        candidates.sort(key=lambda x: (
            status_priority.get(x[1]["status"], 99),
            x[1]["captcha_rate_24h"],
            x[1]["total_searches_24h"],  # меньше использованный — лучше
        ))

        chosen_name = candidates[0][0]

        # Ставим лок
        lock_file = os.path.join(self.profiles_dir, chosen_name, ".in_use")
        with open(lock_file, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds"))

        logging.info(f"[ProfilePool] ✓ Выдан профиль: {chosen_name}")
        return chosen_name

    def release_profile(self, profile_name: str):
        """Освобождает профиль"""
        lock_file = os.path.join(self.profiles_dir, profile_name, ".in_use")
        if os.path.exists(lock_file):
            os.remove(lock_file)

    # ──────────────────────────────────────────────────────────
    # ПАРЫ ПРОФИЛЬ + ПРОКСИ
    # ──────────────────────────────────────────────────────────

    def acquire_pair(self) -> tuple[str | None, dict | None]:
        """
        Выдаёт здоровую пару (profile_name, proxy_dict).
        """
        profile = self.acquire_profile()
        if not profile:
            return None, None

        proxy = self.proxy_pool.acquire(strategy="best_health")
        if not proxy:
            # Если прокси нет — освобождаем профиль обратно
            self.release_profile(profile)
            return None, None

        return profile, proxy

    def release_pair(self, profile_name: str, proxy_id: str,
                     success: bool = True, captcha: bool = False, blocked: bool = False):
        """Освобождает пару и отчитывается"""
        if profile_name:
            self.release_profile(profile_name)
        if proxy_id:
            self.proxy_pool.report(proxy_id, success=success, captcha=captcha, blocked=blocked)
            self.proxy_pool.release(proxy_id)

    # ──────────────────────────────────────────────────────────
    # АВТОМАТИЧЕСКОЕ ОЗДОРОВЛЕНИЕ
    # ──────────────────────────────────────────────────────────

    def nuke_critical_profiles(self, dry_run: bool = True) -> list[str]:
        """
        Удаляет fingerprint и session у критических профилей чтобы они
        пересоздались с нуля при следующем запуске.
        dry_run=True — только показывает список, не удаляет
        """
        import shutil
        nuked = []

        for name in self.list_profiles():
            health = self.get_profile_health(name)
            if health["status"] != "critical":
                continue

            profile_path = os.path.join(self.profiles_dir, name)
            if dry_run:
                logging.info(f"[ProfilePool] [dry-run] {name} был бы очищен ({health['recommendations']})")
                nuked.append(name)
                continue

            # Удаляем fingerprint + session + activity
            for filename in ("fingerprint.json", "session_quality.json", "activity.json"):
                f = os.path.join(profile_path, filename)
                if os.path.exists(f):
                    os.remove(f)

            session_dir = os.path.join(profile_path, "nk_session")
            if os.path.exists(session_dir):
                shutil.rmtree(session_dir)

            logging.warning(f"[ProfilePool] ☢ {name} очищен")
            nuked.append(name)

        return nuked

    # ──────────────────────────────────────────────────────────
    # СТАТУС
    # ──────────────────────────────────────────────────────────

    def print_status(self):
        profiles = self.list_profiles()
        print("\n" + "═" * 80)
        print(f" ПУЛ ПРОФИЛЕЙ ({len(profiles)} шт.)")
        print("═" * 80)
        print(f" {'Name':<20} {'Status':<12} {'Uses 24h':>10} {'Captcha':>8} {'In Use':>8}")
        print(" " + "─" * 78)

        for name in profiles:
            health = self.get_profile_health(name)
            lock_file = os.path.join(self.profiles_dir, name, ".in_use")
            in_use    = "YES" if os.path.exists(lock_file) else "-"

            icon = {
                "healthy":  "🟢",
                "warning":  "🟡",
                "critical": "🔴",
            }.get(health["status"], "?")

            print(
                f" {name:<20} "
                f"{icon} {health['status']:<10} "
                f"{health['total_searches_24h']:>10} "
                f"{health['captcha_rate_24h']:>7.1%} "
                f"{in_use:>8}"
            )
        print("═" * 80 + "\n")
        self.proxy_pool.print_status()


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    pool = ProfilePool()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        pool.print_status()
    elif cmd == "nuke":
        dry = "--yes" not in sys.argv
        nuked = pool.nuke_critical_profiles(dry_run=dry)
        if dry:
            print(f"Были бы очищены: {nuked}. Добавь --yes чтобы реально удалить")
        else:
            print(f"Очищено: {nuked}")
    else:
        print("Команды: status | nuke [--yes]")
