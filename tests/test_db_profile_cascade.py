"""Tests for the profile lifecycle DB helpers — ready_at + cascade."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Spin up an isolated DB instance bound to tmp_path. Inserts a
    skeletal profile row so the cascade tests have something to clean."""
    db_path = tmp_path / "test.db"

    # Force the DB module to use this path. The DB picks file location
    # via PROJECT_ROOT/ghost_shell.db, so monkeypatch PROJECT_ROOT for
    # the duration of the test.
    monkeypatch.setattr(
        "ghost_shell.core.platform_paths.PROJECT_ROOT", str(tmp_path)
    )
    # Sprint 10.2: same gotcha as test_backup.py / conftest.py:
    # the actual singleton is _db_instance (lowercase); DB_PATH is
    # read at module-import-time; DB._local is class-level
    # threading.local(). All three need explicit reset for a true
    # fresh DB per test.
    import ghost_shell.db.database as db_mod
    monkeypatch.setenv("GHOST_SHELL_DB", str(db_path))
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))
    db_mod._db_instance = None
    if hasattr(db_mod, "_DB_INSTANCE"):
        db_mod._DB_INSTANCE = None
    if hasattr(db_mod, "_DB"):
        db_mod._DB = None
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
    return db


def _insert_profile(db, name: str = "p1"):
    """Insert a minimal profiles row without going through the
    high-level helpers (faster + isolates the cascade test)."""
    conn = db._get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO profiles (name, created_at) VALUES (?, datetime('now'))",
        (name,)
    )
    conn.commit()


# ── ready_at column + helpers ──────────────────────────────

def test_profile_mark_ready_stamps_timestamp(fresh_db):
    _insert_profile(fresh_db, "p_ready")
    ok = fresh_db.profile_mark_ready("p_ready")
    assert ok is True
    row = fresh_db._get_conn().execute(
        "SELECT ready_at FROM profiles WHERE name = ?", ("p_ready",)
    ).fetchone()
    assert row["ready_at"] is not None


def test_profile_mark_ready_returns_false_for_unknown(fresh_db):
    assert fresh_db.profile_mark_ready("nope") is False


def test_profile_is_ready_false_when_null(fresh_db):
    _insert_profile(fresh_db, "p_unready")
    # Reset ready_at to NULL (mid-bulk-create state)
    fresh_db._get_conn().execute(
        "UPDATE profiles SET ready_at = NULL WHERE name = ?", ("p_unready",)
    )
    fresh_db._get_conn().commit()
    assert fresh_db.profile_is_ready("p_unready") is False


def test_profile_is_ready_true_after_mark(fresh_db):
    _insert_profile(fresh_db, "p_marked")
    fresh_db.profile_mark_ready("p_marked")
    assert fresh_db.profile_is_ready("p_marked") is True


def test_profile_is_ready_false_for_missing_profile(fresh_db):
    assert fresh_db.profile_is_ready("does_not_exist") is False


# ── profile_delete_cascade ──────────────────────────────────

def test_cascade_removes_profile_row(fresh_db):
    _insert_profile(fresh_db, "p_del")
    counts = fresh_db.profile_delete_cascade("p_del")
    assert counts.get("profiles", 0) >= 1
    row = fresh_db._get_conn().execute(
        "SELECT 1 FROM profiles WHERE name = ?", ("p_del",)
    ).fetchone()
    assert row is None


def test_cascade_removes_profile_extensions(fresh_db):
    _insert_profile(fresh_db, "p_ext")
    # Insert into extensions pool first (FK)
    # Schema: id, name, description, version, source, source_url,
    # pool_path, manifest_json, icon_b64, permissions_summary,
    # is_enabled, auto_install_for_new, created_at, updated_at
    # (no manifest_version column — manifest_version lives inside the
    # cached manifest_json blob, not as its own column)
    fresh_db._get_conn().execute(
        "INSERT INTO extensions (id, name, version, source, pool_path) "
        "VALUES ('ext1', 'Test', '1.0', 'manual_unpacked', '/tmp/x')"
    )
    fresh_db._get_conn().execute(
        "INSERT INTO profile_extensions (profile_name, extension_id) "
        "VALUES (?, 'ext1')", ("p_ext",)
    )
    fresh_db._get_conn().commit()
    counts = fresh_db.profile_delete_cascade("p_ext")
    assert counts.get("profile_extensions", 0) >= 1
    remaining = fresh_db._get_conn().execute(
        "SELECT 1 FROM profile_extensions WHERE profile_name = ?",
        ("p_ext",)
    ).fetchone()
    assert remaining is None


def test_cascade_removes_vault_items(fresh_db):
    _insert_profile(fresh_db, "p_vault")
    fresh_db._get_conn().execute(
        "INSERT INTO vault_items (name, kind, profile_name, created_at, updated_at) "
        "VALUES ('mywallet', 'crypto_wallet', ?, datetime('now'), datetime('now'))",
        ("p_vault",)
    )
    fresh_db._get_conn().commit()
    counts = fresh_db.profile_delete_cascade("p_vault")
    assert counts.get("vault_items", 0) == 1
    assert fresh_db._get_conn().execute(
        "SELECT 1 FROM vault_items WHERE profile_name = ?", ("p_vault",)
    ).fetchone() is None


def test_cascade_removes_pending_restore_config_kv(fresh_db):
    _insert_profile(fresh_db, "p_pend")
    fresh_db.config_set("session.pending_restore.p_pend", 42)
    fresh_db.config_set("profile.p_pend.template_name", "desktop")
    counts = fresh_db.profile_delete_cascade("p_pend")
    assert counts.get("config_kv", 0) >= 2
    assert fresh_db.config_get("session.pending_restore.p_pend") is None
    assert fresh_db.config_get("profile.p_pend.template_name") is None


def test_cascade_idempotent_on_missing_profile(fresh_db):
    # No profile inserted — cascade should still run cleanly
    counts = fresh_db.profile_delete_cascade("never_existed")
    assert counts["profiles"] == 0


def test_cascade_returns_per_table_counts(fresh_db):
    _insert_profile(fresh_db, "p_counts")
    counts = fresh_db.profile_delete_cascade("p_counts")
    # All tracked tables should appear in the result dict
    expected_tables = {
        "profile_extensions", "vault_items", "cookie_snapshots",
        "events", "selfchecks", "fingerprints", "warmup_runs",
        "action_events", "traffic_stats", "profile_health",
        "profile_group_members", "profiles", "config_kv",
    }
    for t in expected_tables:
        assert t in counts, f"missing {t} in cascade summary"


# ── profile_health helpers (Sprint 4) ────────────────────────

def test_profile_health_save_and_recent(fresh_db):
    """Save a few canary rows + read them back via profile_health_recent."""
    _insert_profile(fresh_db, "p_health")
    fresh_db.profile_health_save(
        profile_name="p_health", site="sannysoft",
        score=85, raw_score="12/14", passed=12, total=14,
        details={"check_details": []},
    )
    fresh_db.profile_health_save(
        profile_name="p_health", site="creepjs",
        score=70, raw_score="70.0%",
        details={"fingerprint_hash": "abc"},
    )
    fresh_db.profile_health_save(
        profile_name="p_health", site="pixelscan", error="probe timed out"
    )

    rows = fresh_db.profile_health_recent("p_health", days=30)
    assert len(rows) == 3
    sites = {r["site"] for r in rows}
    assert sites == {"sannysoft", "creepjs", "pixelscan"}


def test_profile_health_recent_filters_by_site(fresh_db):
    _insert_profile(fresh_db, "p_filter")
    fresh_db.profile_health_save(profile_name="p_filter", site="sannysoft", score=80)
    fresh_db.profile_health_save(profile_name="p_filter", site="creepjs",   score=70)
    rows = fresh_db.profile_health_recent("p_filter", days=30, site="sannysoft")
    assert len(rows) == 1
    assert rows[0]["site"] == "sannysoft"


def test_profile_health_summary_trend_improving(fresh_db):
    _insert_profile(fresh_db, "p_trend")
    # Five increasing scores → improving
    for s in (40, 50, 60, 70, 85):
        fresh_db.profile_health_save(
            profile_name="p_trend", site="sannysoft", score=s
        )
    summary = fresh_db.profile_health_summary("p_trend", days=7)
    assert "sannysoft" in summary["trend"]
    # Latest row is 85 → improving slope
    assert summary["trend"]["sannysoft"] == "improving"
    assert summary["latest"]["sannysoft"]["score"] == 85


def test_profile_health_summary_trend_degrading(fresh_db):
    _insert_profile(fresh_db, "p_degrade")
    for s in (90, 80, 70, 60, 45):
        fresh_db.profile_health_save(
            profile_name="p_degrade", site="creepjs", score=s
        )
    summary = fresh_db.profile_health_summary("p_degrade", days=7)
    assert summary["trend"]["creepjs"] == "degrading"


def test_profile_health_summary_trend_flat_for_single_point(fresh_db):
    _insert_profile(fresh_db, "p_lonely")
    fresh_db.profile_health_save(
        profile_name="p_lonely", site="creepjs", score=72
    )
    summary = fresh_db.profile_health_summary("p_lonely", days=7)
    assert summary["trend"]["creepjs"] == "flat"


def test_cascade_removes_profile_health(fresh_db):
    _insert_profile(fresh_db, "p_cascade_h")
    fresh_db.profile_health_save(
        profile_name="p_cascade_h", site="sannysoft", score=80
    )
    counts = fresh_db.profile_delete_cascade("p_cascade_h")
    assert counts.get("profile_health", 0) == 1
    assert fresh_db._get_conn().execute(
        "SELECT 1 FROM profile_health WHERE profile_name = ?",
        ("p_cascade_h",),
    ).fetchone() is None
