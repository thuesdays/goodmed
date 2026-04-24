"""
config.py — Совместимость со старым API Config via БД

Новый code использует db.get_db().config_get() напрямую.
Этот модуль нalreadyн thatбы старые импорты `from config import Config` продолжали работать.
"""

import logging
from ghost_shell.db.database import get_db


class Config:
    """Обёртка поверх БД — for backward compatibility со старым codeом"""

    def __init__(self):
        self._db = get_db()

    @classmethod
    def load(cls, path: str = None) -> "Config":
        """Старый API — игнорирует path, reads from DB. Авто-migrates старые файлы."""
        cfg = cls()
        cfg._db.migrate_from_files(verbose=False)
        return cfg

    def get(self, path: str, default=None):
        """Доступ по точечному пути: cfg.get('search.queries')"""
        return self._db.config_get(path, default)

    def set(self, path: str, value):
        self._db.config_set(path, value)

    def __getitem__(self, key):
        return self.get(key)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = Config.load()
    import json
    print(json.dumps(cfg._db.config_get_all(), indent=2, ensure_ascii=False))
