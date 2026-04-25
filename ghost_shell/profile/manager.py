"""
profile_manager.py — Управление профилями NK Browser
Создание, листинг, клонирование, удаление профилей
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import shutil
import logging
from datetime import datetime


class ProfileManager:
    """
    Менеджер профилей. Управляет папкой profiles/ и их фингерпринтами.

    Пример использования:
        pm = ProfileManager()
        pm.list()                         # показать все профили
        pm.create("worker_01")            # создать пустой
        pm.clone("worker_01", "worker_02")# клонировать с новым фингерпринтом
        pm.delete("worker_02")            # удалить
        pm.info("worker_01")              # информация о профиле
    """

    def __init__(self, base_dir: str = "profiles"):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    # ──────────────────────────────────────────────────────────
    # СПИСОК
    # ──────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        """Возвращает список всех профилей с краткой информацией"""
        profiles = []
        if not os.path.exists(self.base_dir):
            return profiles

        for name in sorted(os.listdir(self.base_dir)):
            path = os.path.join(self.base_dir, name)
            if not os.path.isdir(path):
                continue

            fp_path = os.path.join(path, "fingerprint.json")
            info = {
                "name":       name,
                "path":       path,
                "has_fp":     os.path.exists(fp_path),
                "size_mb":    round(self._dir_size(path) / 1024 / 1024, 1),
                "created":    datetime.fromtimestamp(os.path.getctime(path)).strftime("%Y-%m-%d %H:%M"),
            }

            if info["has_fp"]:
                try:
                    with open(fp_path, "r", encoding="utf-8") as f:
                        fp = json.load(f)
                    info["chrome"]   = fp.get("chrome_version_major", "?")
                    info["screen"]   = f"{fp.get('screen_width')}x{fp.get('screen_height')}"
                    info["lang"]     = fp.get("languages", ["?"])[0]
                    info["webgl"]    = fp.get("webgl_renderer", "")[:40]
                except Exception:
                    pass

            profiles.append(info)

        return profiles

    def print_list(self):
        """Вывод списка в консоль"""
        profiles = self.list()
        if not profiles:
            print("Профилей нет.")
            return

        print(f"\n{'Имя':<20} {'Chrome':<8} {'Экран':<12} {'Язык':<10} {'Размер':<10} {'Создан'}")
        print("─" * 85)
        for p in profiles:
            print(
                f"{p['name']:<20} "
                f"{p.get('chrome', '?'):<8} "
                f"{p.get('screen', '?'):<12} "
                f"{p.get('lang', '?'):<10} "
                f"{p['size_mb']} MB    "
                f"{p['created']}"
            )
        print()

    # ──────────────────────────────────────────────────────────
    # СОЗДАНИЕ
    # ──────────────────────────────────────────────────────────

    def create(self, name: str) -> str:
        """Создаёт пустой профиль. Фингерпринт создастся при первом запуске NKBrowser"""
        path = os.path.join(self.base_dir, name)
        if os.path.exists(path):
            raise ValueError(f"Профиль '{name}' уже существует")
        os.makedirs(path)
        logging.info(f"[ProfileManager] Создан профиль: {name}")
        return path

    # ──────────────────────────────────────────────────────────
    # КЛОНИРОВАНИЕ
    # ──────────────────────────────────────────────────────────

    def clone(self, source: str, target: str, new_fingerprint: bool = True) -> str:
        """
        Клонирует профиль.
        new_fingerprint=True — сгенерирует новый fingerprint.json (по умолчанию)
        new_fingerprint=False — скопирует всё как есть (опасно — одинаковый отпечаток)
        """
        src_path = os.path.join(self.base_dir, source)
        dst_path = os.path.join(self.base_dir, target)

        if not os.path.exists(src_path):
            raise ValueError(f"Исходный профиль '{source}' не найден")
        if os.path.exists(dst_path):
            raise ValueError(f"Целевой профиль '{target}' уже существует")

        shutil.copytree(src_path, dst_path)

        if new_fingerprint:
            fp_path = os.path.join(dst_path, "fingerprint.json")
            if os.path.exists(fp_path):
                os.remove(fp_path)

        logging.info(f"[ProfileManager] Клонирован: {source} → {target}")
        return dst_path

    # ──────────────────────────────────────────────────────────
    # УДАЛЕНИЕ
    # ──────────────────────────────────────────────────────────

    def delete(self, name: str, confirm: bool = False):
        """Удаляет профиль. confirm=True обязателен для подтверждения"""
        path = os.path.join(self.base_dir, name)
        if not os.path.exists(path):
            raise ValueError(f"Профиль '{name}' не найден")
        if not confirm:
            raise ValueError(f"Для удаления передайте confirm=True")

        shutil.rmtree(path)
        logging.info(f"[ProfileManager] Удалён: {name}")

    def reset_fingerprint(self, name: str):
        """Удаляет только fingerprint.json — при следующем запуске создастся новый"""
        fp_path = os.path.join(self.base_dir, name, "fingerprint.json")
        if os.path.exists(fp_path):
            os.remove(fp_path)
            logging.info(f"[ProfileManager] Фингерпринт сброшен: {name}")

    # ──────────────────────────────────────────────────────────
    # ИНФОРМАЦИЯ
    # ──────────────────────────────────────────────────────────

    def info(self, name: str) -> dict:
        """Полная информация о профиле"""
        path = os.path.join(self.base_dir, name)
        if not os.path.exists(path):
            raise ValueError(f"Профиль '{name}' не найден")

        fp_path = os.path.join(path, "fingerprint.json")
        result = {
            "name":    name,
            "path":    path,
            "size_mb": round(self._dir_size(path) / 1024 / 1024, 1),
        }
        if os.path.exists(fp_path):
            with open(fp_path, "r", encoding="utf-8") as f:
                result["fingerprint"] = json.load(f)
        return result

    # ──────────────────────────────────────────────────────────
    # СЛУЖЕБНОЕ
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _dir_size(path: str) -> int:
        total = 0
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total


# ──────────────────────────────────────────────────────────────
# CLI — быстрое управление из командной строки
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    pm = ProfileManager()
    if len(sys.argv) < 2:
        print("Использование:")
        print("  python profile_manager.py list")
        print("  python profile_manager.py create <name>")
        print("  python profile_manager.py clone <source> <target>")
        print("  python profile_manager.py delete <name>")
        print("  python profile_manager.py reset <name>")
        print("  python profile_manager.py info <name>")
        sys.exit(0)

    cmd = sys.argv[1]
    try:
        if cmd == "list":
            pm.print_list()
        elif cmd == "create":
            pm.create(sys.argv[2])
            print(f"✓ Профиль '{sys.argv[2]}' создан")
        elif cmd == "clone":
            pm.clone(sys.argv[2], sys.argv[3])
            print(f"✓ Клонирован: {sys.argv[2]} → {sys.argv[3]}")
        elif cmd == "delete":
            resp = input(f"Удалить '{sys.argv[2]}'? [y/N]: ")
            if resp.lower() == "y":
                pm.delete(sys.argv[2], confirm=True)
                print(f"✓ Удалён: {sys.argv[2]}")
        elif cmd == "reset":
            pm.reset_fingerprint(sys.argv[2])
            print(f"✓ Фингерпринт сброшен: {sys.argv[2]}")
        elif cmd == "info":
            info = pm.info(sys.argv[2])
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print(f"Неизвестная команда: {cmd}")
    except Exception as e:
        print(f"✗ Ошибка: {e}")
