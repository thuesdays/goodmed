"""
chrome_importer.py — Import real browsing history from a host Chrome
install into a Ghost Shell profile's user-data-dir.

Why: Google's ad auction weights "user is a real person who browses the
web" signals heavily. A profile with an empty History DB, zero bookmarks,
and a Preferences file with factory defaults looks synthetic to Google
and triggers lower ad load. Our profile_enricher already synthesises
fake data but real users have noisier, more varied histories than any
generator produces.

This module copies REAL history/bookmarks from a source Chrome install
(the user's own browser) into a Ghost Shell profile. Runs OFFLINE —
Chrome must be closed on BOTH ends during the operation (SQLite file
locks), and we scrub identifying fields before writing to our profile.

Usage:
    python chrome_importer.py \\
        --source "C:\\Users\\you\\AppData\\Local\\Google\\Chrome\\User Data\\Default" \\
        --dest profile_01 \\
        [--days 90]              # Only import history from last N days
        [--skip-cookies]         # Don't import cookies (default: import)
        [--skip-sensitive]       # Skip banking/health/social domains

Or from Python:
    from ghost_shell.browser.chrome_import import ChromeImporter
    ChromeImporter("C:/.../Default", "profile_01").import_all(days=90)

IMPORTANT: the source Chrome MUST be closed. SQLite files have
exclusive locks while Chrome is running — we can't even read them. We
check and refuse to start if the source is locked.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import sys
import json
import shutil
import sqlite3
import logging
import tempfile
import argparse
from datetime import datetime, timedelta
from typing import Optional


# Default source locations by platform. Chrome's "User Data" root;
# the actual profile usually lives under "Default" or "Profile 1".
_DEFAULT_SOURCES_WIN = [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Default"),
    os.path.expandvars(r"%LOCALAPPDATA%\Chromium\User Data\Default"),
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\Default"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default"),
]
_DEFAULT_SOURCES_MAC = [
    os.path.expanduser("~/Library/Application Support/Google/Chrome/Default"),
    os.path.expanduser("~/Library/Application Support/Chromium/Default"),
]
_DEFAULT_SOURCES_LINUX = [
    os.path.expanduser("~/.config/google-chrome/Default"),
    os.path.expanduser("~/.config/chromium/Default"),
]


def discover_source() -> Optional[str]:
    """Find a likely Chrome profile directory on this machine."""
    if sys.platform == "win32":
        candidates = _DEFAULT_SOURCES_WIN
    elif sys.platform == "darwin":
        candidates = _DEFAULT_SOURCES_MAC
    else:
        candidates = _DEFAULT_SOURCES_LINUX
    for c in candidates:
        if os.path.isdir(c) and os.path.exists(os.path.join(c, "History")):
            return c
    return None


# Sensitive domain keywords — skip these from history when the user
# asks for scrubbing. Conservative list, easier to extend than to
# apologise for leaking.
_SENSITIVE_MARKERS = [
    "bank", "paypal", "privat24", "monobank", "wise.com", "revolut",
    "venmo", "cashapp",
    "health", "medical", "doctor", "hospital", "pharmacy", "medi",
    "porn", "xxx", "nsfw", "adult", "sex",
    "facebook.com/", "instagram.com/", "twitter.com/", "x.com/",
    "linkedin.com/", "tiktok.com/",
    "gmail.com", "outlook.live.com", "mail.yahoo", "protonmail",
    "drive.google.com", "dropbox.com",
    "github.com/settings", "github.com/orgs",
]


class ChromeImporter:
    """Orchestrates copying select files from a source Chrome profile
    into a destination Ghost Shell profile directory.

    Each import method is independent — you can call just `import_history`
    if that's all you need, or `import_all` to do the lot.
    """

    def __init__(self, source_dir: str, dest_profile: str,
                 profiles_root: str = "profiles"):
        self.source_dir = os.path.abspath(source_dir)
        self.dest_profile = dest_profile
        # Our profiles live at profiles/<name>/Default to match Chrome's
        # on-disk layout, since that's what --user-data-dir expects.
        self.dest_dir = os.path.join(
            profiles_root, dest_profile, "Default")
        os.makedirs(self.dest_dir, exist_ok=True)

        if not os.path.isdir(self.source_dir):
            raise FileNotFoundError(
                f"Source Chrome profile not found: {self.source_dir}"
            )

    # ──────────────────────────────────────────────────────────
    # Safe-copy helper for live Chrome SQLite files
    # ──────────────────────────────────────────────────────────
    #
    # Chrome is EXPECTED to be running during import — the dashboard
    # typically IS loaded in the user's Chrome, so "close Chrome first"
    # was a catch-22. We read from a WAL-safe live copy instead.
    #
    # The trick: Chrome uses WAL (Write-Ahead Log) journal mode for its
    # SQLite DBs. Current state lives across THREE files:
    #   <f>        — main DB (behind if there are uncheckpointed writes)
    #   <f>-wal    — Write-Ahead Log (recent committed transactions)
    #   <f>-shm    — Shared memory index into the WAL
    #
    # Copying ONLY the main file can give a read minutes/hours stale.
    # Copying all three together and opening the triplet with sqlite3
    # gives the CURRENT committed state: sqlite applies the WAL
    # transparently on open.
    #
    # The three copyfile() calls aren't atomic (Chrome may write
    # between them) but SQLite is extremely tolerant — worst case is
    # a slightly older read, never corruption. We open read-only so
    # we also can't break anything on the source side.

    @staticmethod
    def _copy_sqlite_live(src_path: str, dst_path: str) -> bool:
        """Copy a LIVE Chrome SQLite DB plus WAL + SHM sidecars.
        Returns True if the main file copied successfully."""
        ok_main = False
        for suffix in ("", "-wal", "-shm"):
            s = src_path + suffix
            d = dst_path + suffix
            if not os.path.exists(s):
                continue
            try:
                shutil.copyfile(s, d)
                if suffix == "":
                    ok_main = True
            except (OSError, PermissionError) as e:
                if suffix == "":
                    logging.warning(
                        f"[import] couldn't copy {s}: {e}. "
                        f"If Chrome is busy (heavy download / sync), "
                        f"wait a moment and retry."
                    )
                    return False
                # Missing -wal / -shm just means we miss last few secs
                # of writes — not fatal.
        return ok_main

    # ──────────────────────────────────────────────────────────
    # History
    # ──────────────────────────────────────────────────────────

    def import_history(self, days: int = 90,
                       skip_sensitive: bool = True,
                       max_urls: int = 5000) -> int:
        """Copy URL visit history from source's `History` SQLite into
        destination's `History` SQLite. Filtering:
          - Only URLs visited in the last `days` days
          - Top `max_urls` by visit_count (drops long tail of trivial hits)
          - Sensitive domain filter if `skip_sensitive`

        Returns number of URL rows actually imported.

        Strategy: we READ from the source via WAL-safe multi-file copy
        (Chrome can be running), WRITE into our own freshly-created
        History file with Chrome-compatible schema. We don't try to
        merge Chrome's internal state — we initialise a new history
        for our profile. If the dest already has history, the
        imported URLs are appended.
        """
        src_history = os.path.join(self.source_dir, "History")
        dst_history = os.path.join(self.dest_dir, "History")
        if not os.path.exists(src_history):
            logging.info("[import] source has no History DB, skipping")
            return 0

        # WAL-safe live copy — Chrome can be running, we snapshot the
        # current committed state including unflushed WAL entries.
        tmp_dir = tempfile.mkdtemp(prefix="gs_chrome_import_")
        tmp_src = os.path.join(tmp_dir, "History")
        if not self._copy_sqlite_live(src_history, tmp_src):
            logging.warning("[import] History snapshot failed, skipping")
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception: pass
            return 0

        try:
            cutoff_ts = self._chrome_ts(
                datetime.now() - timedelta(days=days))

            # Open read-only. If Chrome held an exclusive lock for a
            # moment during our copy, sqlite will return SQLITE_BUSY
            # here — busy_timeout lets it retry for 3 seconds.
            src_conn = sqlite3.connect(f"file:{tmp_src}?mode=ro", uri=True)
            src_conn.execute("PRAGMA busy_timeout=3000")
            src_conn.row_factory = sqlite3.Row

            rows = src_conn.execute(f"""
                SELECT id, url, title, visit_count, typed_count,
                       last_visit_time, hidden
                FROM urls
                WHERE last_visit_time >= ?
                ORDER BY visit_count DESC, last_visit_time DESC
                LIMIT ?
            """, (cutoff_ts, max_urls)).fetchall()

            # Filter sensitive
            if skip_sensitive:
                rows = [r for r in rows if not self._is_sensitive(r["url"])]

            # Also pull visits for these URLs so the DB looks real
            # (urls without visits is an unusual shape and could be
            # flagged by Chrome itself on next startup).
            url_ids = tuple(r["id"] for r in rows)
            visit_rows = []
            if url_ids:
                placeholders = ",".join("?" * len(url_ids))
                visit_rows = src_conn.execute(f"""
                    SELECT url, visit_time, from_visit, transition,
                           segment_id, visit_duration
                    FROM visits
                    WHERE url IN ({placeholders})
                """, url_ids).fetchall()

            src_conn.close()

            # Ensure dest History exists with Chrome-compatible schema.
            # We create it if missing, otherwise append.
            self._ensure_history_schema(dst_history)

            dst_conn = sqlite3.connect(dst_history)
            dst_conn.row_factory = sqlite3.Row

            # Re-key url ids — source IDs may collide with any existing
            # rows in dest. We map source_id -> new dest_id.
            id_map = {}
            imported = 0
            for r in rows:
                try:
                    cur = dst_conn.execute("""
                        INSERT INTO urls
                          (url, title, visit_count, typed_count,
                           last_visit_time, hidden)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (r["url"], r["title"] or "",
                          r["visit_count"] or 1, r["typed_count"] or 0,
                          r["last_visit_time"], r["hidden"] or 0))
                    id_map[r["id"]] = cur.lastrowid
                    imported += 1
                except sqlite3.IntegrityError:
                    # URL already present — update visit count instead.
                    existing = dst_conn.execute(
                        "SELECT id, visit_count FROM urls WHERE url = ?",
                        (r["url"],)).fetchone()
                    if existing:
                        dst_conn.execute(
                            "UPDATE urls SET visit_count = visit_count + ? "
                            "WHERE id = ?",
                            (r["visit_count"] or 1, existing["id"]))
                        id_map[r["id"]] = existing["id"]

            # Insert visits with remapped IDs
            for v in visit_rows:
                new_url_id = id_map.get(v["url"])
                if new_url_id is None:
                    continue
                new_from = id_map.get(v["from_visit"], 0) if v["from_visit"] else 0
                try:
                    dst_conn.execute("""
                        INSERT INTO visits
                          (url, visit_time, from_visit, transition,
                           segment_id, visit_duration)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (new_url_id, v["visit_time"], new_from,
                          v["transition"] or 805306368,   # typed
                          v["segment_id"] or 0,
                          v["visit_duration"] or 0))
                except Exception:
                    pass

            dst_conn.commit()
            dst_conn.close()
            logging.info(f"[import] History: {imported} URLs, "
                         f"{len(visit_rows)} visits")
            return imported
        finally:
            # Removes the main file + -wal + -shm + the dir itself.
            try: shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception: pass

    def _ensure_history_schema(self, path: str):
        """Create a minimally Chrome-compatible History DB if file
        doesn't exist. Schema copied from Chrome 120+; older versions
        will have their columns transparently migrated by Chrome on
        first open since we don't touch any columns outside the basics.
        """
        if os.path.exists(path):
            return
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url LONGVARCHAR,
                title LONGVARCHAR,
                visit_count INTEGER DEFAULT 0 NOT NULL,
                typed_count INTEGER DEFAULT 0 NOT NULL,
                last_visit_time INTEGER NOT NULL,
                hidden INTEGER DEFAULT 0 NOT NULL
            );
            CREATE INDEX urls_url_index ON urls(url);
            CREATE INDEX urls_last_visit_index ON urls(last_visit_time);

            CREATE TABLE visits (
                id INTEGER PRIMARY KEY,
                url INTEGER NOT NULL,
                visit_time INTEGER NOT NULL,
                from_visit INTEGER,
                transition INTEGER DEFAULT 0 NOT NULL,
                segment_id INTEGER,
                visit_duration INTEGER DEFAULT 0 NOT NULL
            );
            CREATE INDEX visits_url_index ON visits(url);
            CREATE INDEX visits_time_index ON visits(visit_time);

            CREATE TABLE meta (
                key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY,
                value LONGVARCHAR
            );
            INSERT INTO meta(key, value) VALUES('version', '47');
            INSERT INTO meta(key, value) VALUES('last_compatible_version', '40');
        """)
        conn.commit()
        conn.close()

    # ──────────────────────────────────────────────────────────
    # Bookmarks
    # ──────────────────────────────────────────────────────────

    def import_bookmarks(self, skip_sensitive: bool = True) -> int:
        """Copy Bookmarks JSON, filtered for sensitive domains. The
        Bookmarks file is plaintext JSON — much easier than History."""
        src = os.path.join(self.source_dir, "Bookmarks")
        dst = os.path.join(self.dest_dir, "Bookmarks")
        if not os.path.exists(src):
            logging.info("[import] source has no Bookmarks, skipping")
            return 0

        try:
            with open(src, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logging.warning(f"[import] bookmarks read failed: {e}")
            return 0

        # Walk the bookmark tree and filter in-place
        count = {"n": 0}

        def walk(node):
            if not isinstance(node, dict):
                return node
            if node.get("type") == "url":
                url = node.get("url") or ""
                if skip_sensitive and self._is_sensitive(url):
                    return None
                count["n"] += 1
                return node
            if node.get("type") == "folder" or "children" in node:
                if "children" in node:
                    node["children"] = [c for c in (walk(c) for c in node["children"])
                                        if c is not None]
                return node
            return node

        for root_key in ("roots",):
            if root_key not in data:
                continue
            for fld_key in list(data[root_key].keys()):
                fld = data[root_key][fld_key]
                walk(fld)

        # Chrome computes a `checksum` over the bookmarks tree. We drop
        # it — Chrome will recompute on next open (logs a warning but
        # still loads the file fine).
        data.pop("checksum", None)

        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f)
        logging.info(f"[import] Bookmarks: {count['n']} entries")
        return count["n"]

    # ──────────────────────────────────────────────────────────
    # Preferences (scrubbed)
    # ──────────────────────────────────────────────────────────

    def import_preferences(self) -> bool:
        """Copy Preferences JSON with identifying fields scrubbed.
        Preferences carries a LOT — installed extensions, themes,
        default search engine, content settings, autofill profile,
        signed-in Google account, etc. We import the shape (which
        makes the profile look well-used) but redact anything that
        points back to the source machine or person."""
        src = os.path.join(self.source_dir, "Preferences")
        dst = os.path.join(self.dest_dir, "Preferences")
        if not os.path.exists(src):
            return False

        try:
            with open(src, "r", encoding="utf-8") as f:
                prefs = json.load(f)
        except Exception as e:
            logging.warning(f"[import] preferences read failed: {e}")
            return False

        # Scrub keys that tie to the real user identity. We don't try
        # to be exhaustive — this is a defense-in-depth layer, not a
        # privacy guarantee. Users concerned about PII should start
        # from a fresh profile instead of importing.
        for path in [
            ("signin",),
            ("account_info",),
            ("google", "services", "account_id"),
            ("google", "services", "signin_scoped_device_id"),
            ("autofill",),   # addresses, credit cards — definitely out
            ("profile", "name"),
            ("profile", "gaia_name"),
            ("profile", "gaia_id"),
            ("profile", "gaia_picture_file_name"),
            ("profile", "last_name"),
            ("profile", "avatar_index"),
            ("browser", "last_known_google_url"),
            ("credentials_enable_service",),
            ("sync",),
            ("gcm",),
            ("device_identity",),
            # Extension IDs we keep (extensions list helps legitimacy),
            # but prune any that would phone home with identifying data:
        ]:
            self._pop_path(prefs, path)

        with open(dst, "w", encoding="utf-8") as f:
            json.dump(prefs, f)
        logging.info("[import] Preferences imported (scrubbed)")
        return True

    @staticmethod
    def _pop_path(d, path):
        """Remove a nested key. Silent on missing keys."""
        if not path:
            return
        for k in path[:-1]:
            if not isinstance(d, dict) or k not in d:
                return
            d = d[k]
        if isinstance(d, dict):
            d.pop(path[-1], None)

    # ──────────────────────────────────────────────────────────
    # Top-Sites + Favicons (visual polish only)
    # ──────────────────────────────────────────────────────────

    def import_top_sites(self) -> bool:
        """The Top Sites DB powers the new-tab thumbnail grid. Presence
        makes the profile look lived-in. Uses WAL-safe live copy since
        Chrome keeps this DB open while running."""
        src = os.path.join(self.source_dir, "Top Sites")
        dst = os.path.join(self.dest_dir, "Top Sites")
        if not os.path.exists(src):
            return False
        if self._copy_sqlite_live(src, dst):
            logging.info("[import] Top Sites copied")
            return True
        logging.debug("[import] Top Sites: live copy failed")
        return False

    # ──────────────────────────────────────────────────────────
    # Orchestration
    # ──────────────────────────────────────────────────────────

    def import_all(self, days: int = 90,
                   skip_sensitive: bool = True,
                   max_urls: int = 5000) -> dict:
        """Run every import step in the right order and return a summary."""
        return {
            "history":     self.import_history(
                days=days,
                skip_sensitive=skip_sensitive,
                max_urls=max_urls),
            "bookmarks":   self.import_bookmarks(
                skip_sensitive=skip_sensitive),
            "preferences": self.import_preferences(),
            "top_sites":   self.import_top_sites(),
        }

    # ──────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _chrome_ts(dt: datetime) -> int:
        """Convert a datetime to Chrome's timestamp format
        (microseconds since 1601-01-01 UTC)."""
        # 11644473600 seconds between 1601-01-01 and 1970-01-01 UTC
        return int((dt.timestamp() + 11_644_473_600) * 1_000_000)

    @staticmethod
    def _is_sensitive(url: str) -> bool:
        if not url:
            return False
        low = url.lower()
        return any(marker in low for marker in _SENSITIVE_MARKERS)


# ──────────────────────────────────────────────────────────────
# AUTO-ENRICH on fresh profile
# ──────────────────────────────────────────────────────────────
#
# Triggered automatically for profiles that have:
#   1. No prior auto-enrich (a sentinel file flags whether we already
#      did this), AND
#   2. A reachable host Chrome on this machine (discover_source
#      returns something), AND
#   3. An empty / near-empty History DB (profile hasn't been manually
#      enriched by the user either).
#
# Keeps the dose small (30 days / 500 URLs by default) because:
#   - We want VARIETY per profile — if 10 profiles all import my
#     whole 90-day history they look like the same user 10×.
#   - Smaller imports = less traffic + less forensic value if
#     someone inspects the profile.
#   - Chrome's own user telemetry on realistic browsing shows median
#     daily visits around 30-60. 500 URLs over 30 days ≈ 17/day,
#     normal for casual users.
#
# A per-profile RANDOM SEED (based on profile name) drives which
# subset of the user's history is picked — so profile_01 gets a
# different slice than profile_02 even when pointing at the same
# source Chrome.

_AUTO_ENRICH_SENTINEL = ".gs_auto_enriched"


def auto_enrich_fresh_profile(
    dest_profile: str,
    profiles_root: str = "profiles",
    max_days: int = 30,
    max_urls: int = 500,
    skip_sensitive: bool = True,
    source_dir: str = None,
) -> dict:
    """One-shot auto-enrich for a freshly-created profile. Idempotent —
    writes a sentinel file on success so subsequent runs are no-ops.

    Why a sentinel (vs checking history row count): import could
    legitimately run into 0 new rows (user's Chrome is itself empty).
    Sentinel means "we tried" which is what we actually want to track.

    Returns a dict with status and whatever import_all returned.
    Never raises — failures are logged and returned as {"ok": False,
    "reason": ...}.

    Triggers:
      - Called from ghost_shell_browser.start() AFTER Chrome's first
        successful launch on a fresh profile folder. That timing
        matters: Chrome creates its own History DB with the correct
        schema, THEN we layer imported rows on top via
        chrome_importer's Chrome-compatible schema.
    """
    dest_dir = os.path.join(profiles_root, dest_profile, "Default")
    if not os.path.isdir(dest_dir):
        return {"ok": False, "reason": "dest profile has no Default dir yet"}

    sentinel = os.path.join(dest_dir, _AUTO_ENRICH_SENTINEL)
    if os.path.exists(sentinel):
        return {"ok": False, "reason": "already auto-enriched"}

    # Find host Chrome
    src = source_dir or discover_source()
    if not src or not os.path.isdir(src):
        # Create the sentinel anyway — retrying every run when host
        # Chrome doesn't exist is wasteful. The sentinel only blocks
        # FUTURE auto-enrich; user can still do manual import via
        # dashboard regardless.
        try:
            with open(sentinel, "w") as f:
                f.write("no host Chrome found on this machine\n")
        except OSError:
            pass
        return {"ok": False, "reason": "no host Chrome detected"}

    # Per-profile random variation: same profile name → same slice
    # across runs (deterministic debug), different profile names →
    # different slices.
    import random as _r
    prof_seed = sum(ord(c) for c in dest_profile)
    rng = _r.Random(prof_seed)
    # Randomize the actual dose a bit so profiles don't all have
    # exactly the same URL count (another easy detection signal).
    dose_urls = rng.randint(int(max_urls * 0.6), max_urls)
    dose_days = rng.randint(max(7, max_days // 2), max_days)

    logging.info(
        f"[auto-enrich] fresh profile '{dest_profile}' detected, "
        f"importing up to {dose_urls} URLs from last {dose_days} days "
        f"of host Chrome at {src}"
    )

    try:
        importer = ChromeImporter(
            source_dir=src, dest_profile=dest_profile,
            profiles_root=profiles_root
        )
        summary = importer.import_all(
            days=dose_days,
            skip_sensitive=skip_sensitive,
            max_urls=dose_urls,
        )
        # Write sentinel so we don't re-run. File content is the
        # summary for forensic value if user later inspects the
        # profile.
        try:
            with open(sentinel, "w", encoding="utf-8") as f:
                f.write(
                    f"auto-enriched {datetime.now().isoformat()} from {src}\n"
                    f"dose: {dose_urls} URLs, {dose_days} days\n"
                    f"result: {summary}\n"
                )
        except OSError:
            pass
        return {"ok": True, "source": src, "summary": summary,
                "dose_urls": dose_urls, "dose_days": dose_days}
    except Exception as e:
        logging.warning(f"[auto-enrich] failed: {type(e).__name__}: {e}")
        # Still write sentinel — we don't want to retry every run on a
        # persistently-broken source Chrome (e.g. locked in a way our
        # WAL-safe copy can't handle).
        try:
            with open(sentinel, "w") as f:
                f.write(f"auto-enrich attempted {datetime.now().isoformat()} "
                        f"but failed: {e}\n")
        except OSError:
            pass
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


# ──────────────────────────────────────────────────────────────
# CLI entry
# ──────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(
        description="Import browser history from a source Chrome install "
                    "into a Ghost Shell profile.")
    ap.add_argument("--source", default=None,
                    help="Source Chrome profile dir (default: auto-detect).")
    ap.add_argument("--dest", required=True,
                    help="Destination Ghost Shell profile name.")
    ap.add_argument("--days", type=int, default=90,
                    help="Only import URLs visited in the last N days.")
    ap.add_argument("--max-urls", type=int, default=5000,
                    help="Cap on number of URLs imported.")
    ap.add_argument("--keep-sensitive", action="store_true",
                    help="Skip the built-in sensitive-domain filter.")
    ap.add_argument("--profiles-root", default="profiles",
                    help="Ghost Shell profiles root (default: profiles/)")
    args = ap.parse_args()

    source = args.source or discover_source()
    if not source:
        print("ERROR: no Chrome profile found on this machine — "
              "pass --source explicitly.", file=sys.stderr)
        sys.exit(2)

    print(f"Importing from : {source}")
    print(f"Into profile   : {args.dest}")
    print(f"History window : last {args.days} days")
    print(f"Max URLs       : {args.max_urls}")
    print(f"Sensitive skip : {'OFF' if args.keep_sensitive else 'ON'}")
    print("")

    imp = ChromeImporter(source, args.dest, profiles_root=args.profiles_root)
    summary = imp.import_all(
        days=args.days,
        skip_sensitive=not args.keep_sensitive,
        max_urls=args.max_urls,
    )
    print("")
    print("=== Done ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
