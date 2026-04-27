"""Shared pytest fixtures for the ghost_shell test suite."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make the project root importable so `import ghost_shell` works
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_profile_dir(tmp_path: Path) -> Path:
    """Realistic mock of a profile's user-data-dir."""
    p = tmp_path / "profile_test"
    (p / "Default").mkdir(parents=True)
    return p


@pytest.fixture
def fake_psutil(monkeypatch):
    """Mock the psutil module imported across ghost_shell. Returns a
    namespace with controllable PID-existence + process-iter behaviour.

    Usage:
        fake_psutil.alive_pids = {1234, 5678}
        fake_psutil.processes = [MagicMock(...), ...]
    """
    fake = MagicMock(name="fake_psutil")
    fake.alive_pids = set()
    fake.processes = []

    def _pid_exists(pid):
        return pid in fake.alive_pids

    def _process_iter(attrs=None):
        return iter(fake.processes)

    class FakeNoSuchProcess(Exception):
        pass

    class FakeAccessDenied(Exception):
        pass

    fake.pid_exists = _pid_exists
    fake.process_iter = _process_iter
    fake.NoSuchProcess = FakeNoSuchProcess
    fake.AccessDenied = FakeAccessDenied
    fake.wait_procs = MagicMock(return_value=([], []))

    # Patch the symbol everywhere it's imported lazily inside functions
    monkeypatch.setattr("ghost_shell.core.process_reaper.psutil", fake)
    monkeypatch.setattr("ghost_shell.core.process_reaper.HAVE_PSUTIL", True)
    return fake


@pytest.fixture
def make_proc():
    """Factory for mock psutil.Process objects with cmdline + name."""
    def _make(pid: int, name: str, cmdline=None, ppid: int = 1):
        p = MagicMock()
        p.info = {
            "pid":     pid,
            "name":    name,
            "cmdline": cmdline or [],
            "ppid":    ppid,
        }
        p.pid     = pid
        p.children = MagicMock(return_value=[])
        return p
    return _make


@pytest.fixture
def in_memory_db(monkeypatch, tmp_path):
    """Fresh ghost_shell DB pointed at a tmp file. Returns the DB instance.
    Schema is initialised; callers can insert via the standard helpers.

    Lifecycle quirk to know about: ``DB._local`` is a CLASS-LEVEL
    ``threading.local()`` shared across every DB instance in the
    process. Without explicit cleanup, the cached (now-closed)
    connection from the previous test leaks into the next test and
    raises ``sqlite3.ProgrammingError: Cannot operate on a closed
    database`` the moment a seed helper calls ``_get_conn()``. We
    nuke ``DB._local.conn`` on both setup and teardown so each test
    starts with a guaranteed-fresh thread-local cache."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("GHOST_SHELL_DB", str(db_path))
    import ghost_shell.db.database as db_mod
    # IMPORTANT: db_mod.DB_PATH is computed at module import from
    # the env var, so a late monkeypatch.setenv doesn't change it.
    # We have to overwrite the module-level constant directly so
    # DB.__init__ sees the tmp_path-scoped DB file.
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))
    # IMPORTANT: the actual module-level singleton is named
    # ``_db_instance`` (lowercase). Resetting ``_DB_INSTANCE``
    # (uppercase) is a no-op and leaks the previous test's cached DB
    # into the new test, causing UNIQUE-name collisions on shared
    # profile names like "p1". Reset both for forward-compat.
    db_mod._db_instance = None
    if hasattr(db_mod, "_DB_INSTANCE"):
        db_mod._DB_INSTANCE = None
    if hasattr(db_mod, "DB") and hasattr(db_mod.DB, "_local"):
        if hasattr(db_mod.DB._local, "conn"):
            try:
                db_mod.DB._local.conn.close()
            except Exception:
                pass
            try:
                delattr(db_mod.DB._local, "conn")
            except Exception:
                pass
    db = db_mod.get_db()
    yield db
    try:
        if hasattr(db, "_get_conn"):
            db._get_conn().close()
    except Exception:
        pass
    if hasattr(db_mod, "DB") and hasattr(db_mod.DB, "_local"):
        try:
            delattr(db_mod.DB._local, "conn")
        except Exception:
            pass
    db_mod._db_instance = None
    if hasattr(db_mod, "_DB_INSTANCE"):
        db_mod._DB_INSTANCE = None

# Sprint 10.2: pytest 9 dropped collect_ignore_glob from .ini files;
# the equivalent must live in conftest.py. These three files use the
# test_ prefix but have function args (host, port, proxy_url) that
# pytest tries to resolve as fixtures. They're standalone integration
# scripts meant to be run as `python tests/test_proxy_live.py`.
collect_ignore = [
    "test_chromedriver.py",
    "test_proxy_live.py",
    "test_proxy_rotation.py",
]
