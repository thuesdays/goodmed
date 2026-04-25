"""Pre-flight profile validator.

Runs BEFORE Chrome launch and repairs known-bad state that would cause
FATAL crashes. Chrome 149 hard-validates several files on startup
(SQLite schemas, JSON prefs, session files) and EXITS the whole process
if anything is wrong — no recovery path exists once chrome.exe is up.
So we have to catch issues while we still can delete / rewrite them.

Every check is independent. A check that fails:
  - LEVEL 1 (recoverable): log + fix in-place → continue
  - LEVEL 2 (corrupt but rebuildable): log + delete the file → Chrome
    recreates on first launch → continue
  - LEVEL 3 (profile unsalvageable): log + rename to .quarantine → new
    empty profile created → run continues fresh

This module is conservative: when in doubt, delete. Chrome rebuilding a
file is always safe. Leaving a corrupt file that matches our best-guess
schema but not Chrome's actual schema is what kills the process.

Design principles:
  - NEVER raise. Every public method returns a status dict.
  - Idempotent. Running validate() twice in a row is fine.
  - Logs are LOUD — a profile crash is the #1 user-visible failure
    mode, so the validator announces everything it touches.
  - Schema comparisons are defensive. If we can't determine what Chrome
    wants, we err on the side of deletion rather than hoping.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from typing import Optional


# Chrome 149's known schemas — the columns Chrome's compiled SQL
# statements reference. We validate EXISTENCE of these columns. If
# Chrome adds new optional columns we'll tolerate that; if REQUIRED
# columns are missing, we delete the DB so Chrome recreates it with
# the correct schema.
#
# These lists are the MINIMUM required — extra columns (from older
# schemas that Chrome migrates on open) are fine. We only care that
# Chrome's INSERT/UPDATE statements won't hit missing columns.
EXPECTED_URLS_COLUMNS = {
    "id", "url", "title", "visit_count", "typed_count",
    "last_visit_time", "hidden",
}
EXPECTED_VISITS_COLUMNS = {
    "id", "url", "visit_time", "from_visit", "transition",
    "segment_id", "visit_duration",
}


class ProfileValidator:
    """One instance per profile. Call validate() before each launch."""

    def __init__(self, user_data_path: str):
        self.user_data_path = os.path.abspath(user_data_path)
        self.default_dir    = os.path.join(self.user_data_path, "Default")
        # Checks we performed — returned in the status dict so the
        # caller can surface "fixed 3 issues" in logs / dashboard.
        self._findings: list[dict] = []

    def _finding(self, level: str, what: str, action: str, detail: str = ""):
        self._findings.append({
            "level":  level,      # "ok" | "fixed" | "deleted" | "quarantined"
            "what":   what,
            "action": action,
            "detail": detail,
        })
        if level == "ok":
            return
        emoji = {"fixed": "🔧", "deleted": "🗑", "quarantined": "⚠"}.get(level, "ℹ")
        logging.info(f"[ProfileValidator] {emoji} {what}: {action}. {detail}")

    # ──────────────────────────────────────────────────────────
    # PUBLIC ENTRY
    # ──────────────────────────────────────────────────────────

    def validate(self) -> dict:
        """Run all checks. Returns summary dict; never raises.

        Safe to call on: non-existent profiles (no-op), freshly-created
        profiles (nothing to validate), existing profiles (main case).
        """
        self._findings = []

        if not os.path.exists(self.user_data_path):
            self._finding("ok", "profile_dir", "doesn't exist yet")
            return self._summary()

        # Default/ subfolder holds everything that matters — if it
        # doesn't exist, Chrome will create it; nothing for us to do.
        if not os.path.isdir(self.default_dir):
            self._finding("ok", "Default/", "not present, Chrome will create")
            return self._summary()

        # Run checks in order of cost (cheap first — JSON validation
        # before SQLite opens). Each is fully independent.
        self._check_history_db()
        self._check_top_sites_db()
        self._check_preferences_json()
        self._check_local_state_json()
        self._check_session_files()
        self._check_lock_files()
        self._check_stale_crash_markers()

        return self._summary()

    def _summary(self) -> dict:
        fixed        = sum(1 for f in self._findings if f["level"] == "fixed")
        deleted      = sum(1 for f in self._findings if f["level"] == "deleted")
        quarantined  = sum(1 for f in self._findings if f["level"] == "quarantined")
        return {
            "total_issues":  fixed + deleted + quarantined,
            "fixed":         fixed,
            "deleted":       deleted,
            "quarantined":   quarantined,
            "findings":      self._findings,
        }

    # ──────────────────────────────────────────────────────────
    # SQLite checks
    # ──────────────────────────────────────────────────────────

    def _columns_of(self, db_path: str, table: str) -> Optional[set]:
        """Return set of column names for a table, or None if we can't
        even open the DB / the table doesn't exist."""
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            try:
                rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            finally:
                conn.close()
            if not rows:
                return None
            return {r[1] for r in rows}
        except Exception:
            return None

    def _delete_with_wal(self, base_path: str):
        """Delete a SQLite main file + its WAL / SHM / journal siblings.
        Chrome's file journal mode can leave uncheckpointed writes in
        -wal; if we delete only the main file, Chrome could try to
        replay the WAL into a fresh DB and get confused. Take all
        three out together."""
        for suffix in ("", "-wal", "-shm", "-journal"):
            p = base_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError as e:
                    logging.warning(f"[ProfileValidator] couldn't remove {p}: {e}")

    def _check_history_db(self):
        """History is the single most common FATAL trigger. Chrome 149
        runs an integrity check + schema validation on open; if our
        seeded schema doesn't match, Chrome crashes in sql/statement.cc
        with 'Cannot call mutating statements on an invalid statement'.
        The only safe response is to delete the DB entirely and let
        Chrome create its own."""
        path = os.path.join(self.default_dir, "History")
        if not os.path.exists(path):
            self._finding("ok", "History DB", "not present (Chrome will create)")
            return

        # Check 1: file is a valid SQLite DB (not zero-length junk)
        if os.path.getsize(path) < 1024:
            self._delete_with_wal(path)
            self._finding("deleted", "History DB",
                          "file was suspiciously small",
                          "likely truncated / half-written")
            return

        # Check 2: integrity_check. Chrome runs this itself on open;
        # catching it here avoids the FATAL.
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
            if not result or result[0] != "ok":
                self._delete_with_wal(path)
                self._finding("deleted", "History DB",
                              "integrity_check failed",
                              f"sqlite reported: {result[0] if result else 'null'}")
                return
        except Exception as e:
            self._delete_with_wal(path)
            self._finding("deleted", "History DB",
                          "couldn't open for integrity check",
                          f"{type(e).__name__}: {str(e)[:80]}")
            return

        # Check 3: required columns present. This is the schema-drift
        # trap — our old enricher seeded urls/visits with a schema
        # that was one Chrome version out of date. Chrome's INSERT
        # referenced columns that didn't exist → FATAL.
        urls_cols   = self._columns_of(path, "urls")
        visits_cols = self._columns_of(path, "visits")

        if urls_cols is None or visits_cols is None:
            self._delete_with_wal(path)
            self._finding("deleted", "History DB",
                          "missing required tables (urls or visits)",
                          "Chrome will rebuild on open")
            return

        missing_urls   = EXPECTED_URLS_COLUMNS   - urls_cols
        missing_visits = EXPECTED_VISITS_COLUMNS - visits_cols
        if missing_urls or missing_visits:
            self._delete_with_wal(path)
            self._finding("deleted", "History DB",
                          "schema missing required columns",
                          f"urls missing: {missing_urls or '∅'}; "
                          f"visits missing: {missing_visits or '∅'}")
            return

        self._finding("ok", "History DB", "schema looks compatible")

    def _check_top_sites_db(self):
        """Top Sites used to be seeded by ProfileEnricher. Same
        schema-drift risk as History — if we created it with a stale
        schema, Chrome crashes. If we open a freshly-sandboxed copy
        and the schema looks bad, delete it."""
        path = os.path.join(self.default_dir, "Top Sites")
        if not os.path.exists(path):
            return   # normal — Chrome creates on first new-tab render

        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
            if not result or result[0] != "ok":
                self._delete_with_wal(path)
                self._finding("deleted", "Top Sites DB",
                              "integrity_check failed")
                return
        except Exception as e:
            self._delete_with_wal(path)
            self._finding("deleted", "Top Sites DB",
                          "couldn't open",
                          f"{type(e).__name__}")
            return

        self._finding("ok", "Top Sites DB", "healthy")

    # ──────────────────────────────────────────────────────────
    # JSON checks
    # ──────────────────────────────────────────────────────────

    def _check_preferences_json(self):
        """Preferences carries user prefs, permissions, extension list,
        and (critically) the exit_type / exited_cleanly flags Chrome
        uses to decide whether to show 'restore session?' on startup.

        Chrome refuses to start cleanly on invalid JSON here — it falls
        back to a prompt asking the user to reset preferences. We catch
        that by replacing broken Preferences with a minimal valid one.
        Chrome adds the rest on first launch.

        We also ensure exit_type=Normal so we don't accidentally leave
        the profile in "crashed" state, which triggers 9-tabs-restore
        behaviour (the separate bug fixed earlier)."""
        path = os.path.join(self.default_dir, "Preferences")
        if not os.path.exists(path):
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            if not isinstance(prefs, dict):
                raise ValueError(f"root is {type(prefs).__name__}, expected dict")
        except (json.JSONDecodeError, ValueError, OSError) as e:
            # Corrupt JSON. Can't be salvaged — write minimal default.
            minimal = {
                "profile": {
                    "exit_type":      "Normal",
                    "exited_cleanly": True,
                },
                "session": {
                    "restore_on_startup":          5,    # 5 = NTP
                    "restore_on_startup_migrated": True,
                    "startup_urls":                [],
                },
            }
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(minimal, f)
                os.replace(tmp, path)
                self._finding("fixed", "Preferences",
                              "corrupt JSON replaced with minimal valid prefs",
                              f"original error: {type(e).__name__}: {str(e)[:60]}")
                return
            except Exception as e2:
                self._finding("deleted", "Preferences",
                              "couldn't rewrite, deleted so Chrome recreates",
                              f"{type(e2).__name__}")
                try: os.remove(path)
                except OSError: pass
                return

        # JSON is valid. Check "crashed" markers — if previous run
        # exited uncleanly Chrome will try to restore session. We don't
        # want that (see the 9-tabs bug). Force clean-exit markers.
        profile = prefs.setdefault("profile", {})
        changed = False
        if profile.get("exit_type") != "Normal":
            profile["exit_type"] = "Normal"
            changed = True
        if profile.get("exited_cleanly") is not True:
            profile["exited_cleanly"] = True
            changed = True

        if changed:
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(prefs, f)
                os.replace(tmp, path)
                self._finding("fixed", "Preferences",
                              "stamped exited_cleanly=true",
                              "prev run left crash markers — would have triggered tab restore")
            except Exception as e:
                logging.warning(f"[ProfileValidator] couldn't write Preferences: {e}")

    def _check_local_state_json(self):
        """Local State is the parent-profile equivalent of Preferences —
        lives one level above Default/. Same corruption-handling as
        Preferences: invalid JSON → replace with minimal."""
        path = os.path.join(self.user_data_path, "Local State")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("root not a dict")
        except (json.JSONDecodeError, ValueError, OSError) as e:
            try:
                os.remove(path)
                self._finding("deleted", "Local State",
                              "corrupt JSON — Chrome will rebuild",
                              f"{type(e).__name__}")
            except OSError:
                pass

    # ──────────────────────────────────────────────────────────
    # Session files
    # ──────────────────────────────────────────────────────────

    def _check_session_files(self):
        """Current Session / Current Tabs / Last Session / Last Tabs
        are where Chrome stores 'open tabs' for the restore-on-startup
        feature. The 9-tabs-pile-up bug was caused by these files
        accumulating across crashed runs.

        Policy: delete them every run. Chrome treats their absence as
        'fresh profile, no tabs to restore' which is exactly what we
        want. If a user wants their previous tabs back they'd be using
        the actual browser, not Ghost Shell."""
        session_files = [
            os.path.join(self.default_dir, "Current Session"),
            os.path.join(self.default_dir, "Current Tabs"),
            os.path.join(self.default_dir, "Last Session"),
            os.path.join(self.default_dir, "Last Tabs"),
        ]
        deleted = 0
        for p in session_files:
            if os.path.exists(p):
                try:
                    os.remove(p)
                    deleted += 1
                except OSError:
                    pass
        # Sessions/ directory (Chrome 122+ moved tab state here)
        sessions_dir = os.path.join(self.default_dir, "Sessions")
        if os.path.isdir(sessions_dir):
            try:
                shutil.rmtree(sessions_dir, ignore_errors=True)
                deleted += 1
            except Exception:
                pass
        if deleted:
            self._finding("fixed", "Session files",
                          f"cleaned {deleted} stale session file(s)",
                          "prevents accumulated tab restore")

    def _check_lock_files(self):
        """SingletonLock / SingletonCookie / SingletonSocket are how
        Chrome prevents two instances running against the same
        user-data-dir. If a previous run was killed hard (SIGKILL,
        process reaper), these may linger and cause the new Chrome to
        refuse to start with 'profile in use'.

        Safe to delete as long as no Chrome is actually running. The
        outer start() has already ensured that by this point (it's
        called right before launching a fresh Chrome; if one was
        running, we'd have known from lockfiles anyway)."""
        singletons = [
            os.path.join(self.user_data_path, "SingletonLock"),
            os.path.join(self.user_data_path, "SingletonCookie"),
            os.path.join(self.user_data_path, "SingletonSocket"),
        ]
        removed = 0
        for p in singletons:
            # On Windows they're sometimes symlinks. Check with lexists.
            if os.path.lexists(p):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
        if removed:
            self._finding("fixed", "Singleton locks",
                          f"removed {removed} stale lock file(s)",
                          "prev run exited uncleanly")

    def _check_stale_crash_markers(self):
        """Crashpad directory can accumulate crash dumps if Chrome kept
        crashing. Chrome itself cleans these but slowly. If the dir is
        huge (>100 MB), delete it to free space + reduce startup
        scanning."""
        crashpad = os.path.join(self.user_data_path, "Crashpad")
        if not os.path.isdir(crashpad):
            return
        try:
            total = 0
            file_count = 0
            for root, dirs, files in os.walk(crashpad):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        total += os.path.getsize(fp)
                        file_count += 1
                    except OSError:
                        pass
            if total > 100 * 1024 * 1024:   # 100 MB
                shutil.rmtree(crashpad, ignore_errors=True)
                self._finding("fixed", "Crashpad",
                              f"cleared {total // 1024 // 1024} MB of crash dumps",
                              f"{file_count} files — previous crashes were piling up")
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # NUCLEAR OPTION — quarantine entire profile
    # ──────────────────────────────────────────────────────────

    def quarantine_profile(self, reason: str = "") -> str:
        """Rename the entire profile directory to <name>.quarantine-<ts>
        so a fresh one can be created. Used after Chrome crashes that
        our individual checks can't resolve.

        Returns the new path of the quarantined folder, or empty string
        on failure. Caller should let Chrome recreate user_data_path
        on next launch — profile name stays the same in config; just
        the on-disk folder is rotated."""
        if not os.path.exists(self.user_data_path):
            return ""

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        new_path = f"{self.user_data_path}.quarantine-{ts}"
        try:
            os.rename(self.user_data_path, new_path)
            logging.warning(
                f"[ProfileValidator] ⚠ QUARANTINED profile: {self.user_data_path} "
                f"→ {new_path}. Reason: {reason or 'unspecified'}. "
                f"Chrome will create a fresh empty profile on next launch."
            )
            return new_path
        except OSError as e:
            logging.error(
                f"[ProfileValidator] couldn't quarantine {self.user_data_path}: {e}. "
                f"Manual cleanup may be needed."
            )
            return ""


def preflight(user_data_path: str) -> dict:
    """Convenience one-shot. Returns status dict.
    Always safe to call, never raises."""
    try:
        return ProfileValidator(user_data_path).validate()
    except Exception as e:
        logging.warning(f"[ProfileValidator] validator itself crashed: {e}")
        return {"total_issues": 0, "fixed": 0, "deleted": 0,
                "quarantined": 0, "findings": []}
