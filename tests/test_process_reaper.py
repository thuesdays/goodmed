"""Unit tests for process_reaper helpers — orphan kill + DB liveness."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ── kill_chrome_for_user_data_dir — cmdline matching ──────────

def test_kill_chrome_returns_zero_without_psutil(monkeypatch):
    from ghost_shell.core import process_reaper as pr
    monkeypatch.setattr(pr, "HAVE_PSUTIL", False)
    assert pr.kill_chrome_for_user_data_dir("/some/profile") == 0


def test_kill_chrome_returns_zero_for_empty_path(fake_psutil):
    from ghost_shell.core import process_reaper as pr
    assert pr.kill_chrome_for_user_data_dir("") == 0
    assert pr.kill_chrome_for_user_data_dir(None) == 0


def test_kill_chrome_matches_chrome_by_user_data_dir(fake_psutil, make_proc, monkeypatch):
    from ghost_shell.core import process_reaper as pr
    target = r"C:\profiles\me"
    fake_psutil.processes = [
        make_proc(100, "chrome.exe", cmdline=[
            "chrome.exe",
            f"--user-data-dir={target}",
            "--no-sandbox",
        ]),
        make_proc(200, "firefox.exe", cmdline=["firefox.exe"]),  # different browser, ignore
        make_proc(300, "chrome.exe", cmdline=[
            "chrome.exe",
            "--user-data-dir=C:/other/profile",
        ]),  # different profile, ignore
    ]
    killed_pids = []
    def fake_kill_tree(pid, reason="cleanup"):
        killed_pids.append(pid)
        return True
    monkeypatch.setattr(pr, "kill_process_tree", fake_kill_tree)
    n = pr.kill_chrome_for_user_data_dir(target)
    assert n == 1
    assert killed_pids == [100]


def test_kill_chrome_matches_forward_and_backslash(fake_psutil, make_proc, monkeypatch):
    """Cmdline may have forward-slash form even when our path is
    Windows-native; matcher must be slash-agnostic."""
    from ghost_shell.core import process_reaper as pr
    fake_psutil.processes = [
        make_proc(101, "chrome.exe", cmdline=[
            "chrome",
            "--user-data-dir=C:/Users/n/profiles/p1",
        ]),
    ]
    killed = []
    monkeypatch.setattr(pr, "kill_process_tree",
                        lambda pid, reason="x": killed.append(pid) or True)
    n = pr.kill_chrome_for_user_data_dir(r"C:\Users\n\profiles\p1")
    assert n == 1


def test_kill_chrome_skips_processes_with_empty_cmdline(fake_psutil, make_proc, monkeypatch):
    from ghost_shell.core import process_reaper as pr
    fake_psutil.processes = [
        make_proc(100, "chrome.exe", cmdline=[]),
        make_proc(101, "chrome.exe", cmdline=None),
    ]
    monkeypatch.setattr(pr, "kill_process_tree", lambda *a, **kw: True)
    n = pr.kill_chrome_for_user_data_dir(r"C:\foo")
    assert n == 0


# ── is_profile_actually_running (DB-level liveness) ───────────

def _mk_run_row(pid, hb_offset_sec=0, run_id=1):
    """Build a runs table row with heartbeat at NOW + offset."""
    hb = (datetime.now() + timedelta(seconds=hb_offset_sec)).isoformat(timespec="seconds")
    return {"id": run_id, "pid": pid, "started_at": hb, "heartbeat_at": hb}


def test_liveness_no_runs_returns_false():
    from ghost_shell.core import process_reaper as pr
    db = MagicMock()
    db.runs_live_for_profile.return_value = []
    assert pr.is_profile_actually_running(db, "p1") is False


def test_liveness_fresh_heartbeat_returns_true(monkeypatch):
    """Sprint 10.2: classify_run_liveness Case C tries
    ``psutil.pid_exists(123)``. On a real machine PID 123 almost
    certainly doesn't exist, so the assertion would fail. Force
    HAVE_PSUTIL=False so Case B (heartbeat-only) handles it — that
    branch returns alive purely from the fresh heartbeat."""
    from ghost_shell.core import process_reaper as pr
    monkeypatch.setattr(pr, "HAVE_PSUTIL", False)
    db = MagicMock()
    db.runs_live_for_profile.return_value = [_mk_run_row(123, hb_offset_sec=-30)]
    assert pr.is_profile_actually_running(db, "p1") is True


def test_liveness_stale_heartbeat_dead_pid_returns_false(monkeypatch):
    """RC-33 + PR-31: stale heartbeat AND dead PID → not running."""
    from ghost_shell.core import process_reaper as pr
    db = MagicMock()
    db.runs_live_for_profile.return_value = [_mk_run_row(99999, hb_offset_sec=-300)]
    monkeypatch.setattr(pr, "HAVE_PSUTIL", True)
    fake = MagicMock()
    fake.pid_exists = lambda pid: False
    monkeypatch.setattr(pr, "psutil", fake)
    assert pr.is_profile_actually_running(db, "p1") is False


def test_liveness_stale_heartbeat_alive_pid_returns_true(monkeypatch):
    """Heartbeat stale but PID alive (and ours) — still running, but
    presumed hung. is_profile_actually_running treats as RUNNING (the
    delete-protection caller wants this; the launch guard separately
    handles hung detection via _is_lock_live in runtime.py)."""
    from ghost_shell.core import process_reaper as pr
    db = MagicMock()
    db.runs_live_for_profile.return_value = [_mk_run_row(12345, hb_offset_sec=-500)]
    monkeypatch.setattr(pr, "HAVE_PSUTIL", True)
    fake = MagicMock()
    fake.pid_exists = lambda pid: True
    monkeypatch.setattr(pr, "psutil", fake)
    monkeypatch.setattr(pr, "pid_looks_like_ghost_shell", lambda pid: True)
    assert pr.is_profile_actually_running(db, "p1") is True


def test_liveness_db_error_returns_true_conservative():
    """If DB call raises, return True (refuse destructive op) rather
    than risking data loss on a transient DB issue."""
    from ghost_shell.core import process_reaper as pr
    db = MagicMock()
    db.runs_live_for_profile.side_effect = RuntimeError("DB locked")
    assert pr.is_profile_actually_running(db, "p1") is True


def test_liveness_corrupt_heartbeat_falls_back_to_pid_check(monkeypatch):
    from ghost_shell.core import process_reaper as pr
    db = MagicMock()
    bad_row = {"id": 1, "pid": 12345, "heartbeat_at": "not-a-timestamp"}
    db.runs_live_for_profile.return_value = [bad_row]
    monkeypatch.setattr(pr, "HAVE_PSUTIL", True)
    fake = MagicMock()
    fake.pid_exists = lambda pid: True
    monkeypatch.setattr(pr, "psutil", fake)
    monkeypatch.setattr(pr, "pid_looks_like_ghost_shell", lambda pid: True)
    assert pr.is_profile_actually_running(db, "p1") is True


# ── pid_looks_like_ghost_shell ────────────────────────────────

def test_pid_looks_like_ghost_shell_no_psutil(monkeypatch):
    from ghost_shell.core import process_reaper as pr
    monkeypatch.setattr(pr, "HAVE_PSUTIL", False)
    assert pr.pid_looks_like_ghost_shell(123) is False


# ── ensure_profile_ready_to_launch ────────────────────────────

def test_ensure_ready_returns_error_when_not_ready():
    from ghost_shell.core import process_reaper as pr
    db = MagicMock()
    db.profile_is_ready = MagicMock(return_value=False)
    err = pr.ensure_profile_ready_to_launch(db, "p1")
    assert err is not None
    assert "not ready" in err.lower()


def test_ensure_ready_delegates_to_liveness_when_ready():
    from ghost_shell.core import process_reaper as pr
    db = MagicMock()
    db.profile_is_ready = MagicMock(return_value=True)
    db.runs_live_for_profile = MagicMock(return_value=[])
    err = pr.ensure_profile_ready_to_launch(db, "p1")
    assert err is None  # ready + no live runs → free to launch


def test_ensure_ready_no_profile_is_ready_method_proceeds():
    """Legacy DB without profile_is_ready — should fall through to
    liveness check, not raise AttributeError."""
    from ghost_shell.core import process_reaper as pr
    db = MagicMock(spec=["runs_live_for_profile"])
    db.runs_live_for_profile.return_value = []
    err = pr.ensure_profile_ready_to_launch(db, "p1")
    assert err is None
