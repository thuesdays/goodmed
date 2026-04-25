"""
config.py — Compat shim for the old `Config` API, backed by the DB.

New code calls `db.get_db().config_get()` directly. This module exists
only so legacy imports like `from config import Config` keep working
while we migrate callers over.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import logging
from ghost_shell.db.database import get_db


class Config:
    """Thin wrapper around the DB — kept for backward compatibility
    with the pre-refactor flat-file config layer."""

    def __init__(self):
        self._db = get_db()

    @classmethod
    def load(cls, path: str = None) -> "Config":
        """Old API — ignores `path`, reads from DB. Auto-migrates the
        legacy on-disk files on first use."""
        cfg = cls()
        cfg._db.migrate_from_files(verbose=False)
        return cfg

    def get(self, path: str, default=None):
        """Dotted-path read: `cfg.get('search.queries')`."""
        return self._db.config_get(path, default)

    def set(self, path: str, value):
        self._db.config_set(path, value)

    def __getitem__(self, key):
        return self.get(key)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.basicConfig(level=logging.INFO)
    cfg = Config.load()
    import json
    print(json.dumps(cfg._db.config_get_all(), indent=2, ensure_ascii=False))
