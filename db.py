"""
db.py — Центральная SQLite база Ghost Shell

Один файл ghost_shell.db contains everything: config, запуски, события,
конкурентов, IP tracker, fingerprint history, логи, selfcheck.

Usage:
    from db import DB
    db = DB()                     # открывает/creates ghost_shell.db
    db.config_set("proxy.url", "user:pass@host:port")
    run_id = db.run_start("profile_01", proxy_url="...")
    db.event_record(run_id, "profile_01", "search_ok", query="test")
    db.run_finish(run_id, exit_code=0)
"""

import os
import json
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Optional


DB_PATH = os.environ.get("GHOST_SHELL_DB", "ghost_shell.db")


# ──────────────────────────────────────────────────────────────
# СХЕМА
# ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    profile_name  TEXT NOT NULL,
    proxy_url     TEXT,
    exit_code     INTEGER,
    error         TEXT,
    total_queries INTEGER DEFAULT 0,
    total_ads     INTEGER DEFAULT 0,
    captchas      INTEGER DEFAULT 0,
    ip_used       TEXT,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_profile ON runs(profile_name);

CREATE TABLE IF NOT EXISTS selfchecks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    tests_json    TEXT NOT NULL,
    actual_json   TEXT,
    expected_json TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_selfcheck_profile ON selfchecks(profile_name, timestamp DESC);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    query         TEXT,
    details       TEXT,
    duration_sec  REAL,
    results_count INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_events_profile_ts ON events(profile_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS competitors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER,
    timestamp        TEXT NOT NULL,
    query            TEXT NOT NULL,
    domain           TEXT NOT NULL,
    title            TEXT,
    display_url      TEXT,
    clean_url        TEXT,
    google_click_url TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_comp_domain ON competitors(domain);
CREATE INDEX IF NOT EXISTS idx_comp_query ON competitors(query);
CREATE INDEX IF NOT EXISTS idx_comp_ts ON competitors(timestamp DESC);

-- Per-step execution log for the action pipeline.
-- One row per (ad × pipeline-step × outcome). Powers Overview/Competitor
-- stats that count "how many ads did we click / interact with".
--   ad_class: "target", "my_domain", "competitor", "unknown"
--   outcome:  "ran", "skipped", "error"
--   skip_reason (only when outcome=skipped): "my_domain" | "target" |
--                                            "not_target" | "not_my_domain" |
--                                            "probability" | "disabled"
CREATE TABLE IF NOT EXISTS action_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    query         TEXT,
    ad_domain     TEXT,
    ad_class      TEXT,
    action_type   TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    skip_reason   TEXT,
    duration_sec  REAL,
    error         TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_aev_run       ON action_events(run_id);
CREATE INDEX IF NOT EXISTS idx_aev_domain_ts ON action_events(ad_domain, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_aev_profile_ts ON action_events(profile_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_aev_outcome   ON action_events(outcome, timestamp DESC);

CREATE TABLE IF NOT EXISTS ip_history (
    ip                  TEXT PRIMARY KEY,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    total_uses          INTEGER DEFAULT 0,
    total_captchas      INTEGER DEFAULT 0,
    consecutive_capchas INTEGER DEFAULT 0,
    burned_at           TEXT,
    country             TEXT,
    city                TEXT,
    org                 TEXT,
    asn                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_ip_burned ON ip_history(burned_at);

CREATE TABLE IF NOT EXISTS fingerprints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    template_name TEXT,
    payload_json  TEXT NOT NULL,
    is_current    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fp_profile ON fingerprints(profile_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_fp_current ON fingerprints(profile_name, is_current);

CREATE TABLE IF NOT EXISTS config_kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER,
    timestamp TEXT NOT NULL,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(run_id, id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ───────────────────────────────────────────────────────────────
-- Profile metadata. The filesystem folder profiles/<name>/ still
-- owns the Chrome user-data-dir and session files; this table
-- tracks _dashboard-level_ state: tags, per-profile proxy override,
-- group membership, notes.
--
-- A profile exists in this table as soon as the user customises it
-- away from defaults. Profiles that only live on disk are still
-- listed by profiles_list() as "implicit" — those use global
-- config values until the user overrides something.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS profiles (
    name         TEXT PRIMARY KEY,
    tags         TEXT,             -- JSON array of strings
    proxy_url    TEXT,             -- overrides global proxy.url when set
    proxy_is_rotating    INTEGER,  -- 1 / 0 / NULL = inherit global
    rotation_api_url     TEXT,     -- per-profile rotation endpoint
    rotation_provider    TEXT,
    rotation_api_key     TEXT,
    notes        TEXT,             -- free-form user notes
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_profiles_updated ON profiles(updated_at);

-- Profile groups — a named bag of profiles + shared settings
-- (typically a group-wide script and scheduler entry). Deleting a
-- group doesn't delete its profiles; profiles can belong to many
-- groups through profile_group_members.
CREATE TABLE IF NOT EXISTS profile_groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT,
    -- Shared script/pipeline snapshot (JSON) applied when this group
    -- is run as a batch. NULL means "use each profile's own pipelines".
    script       TEXT,
    -- Concurrency cap specific to this group (NULL = use global default)
    max_parallel INTEGER,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile_group_members (
    group_id    INTEGER NOT NULL,
    profile_name TEXT   NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, profile_name),
    FOREIGN KEY (group_id) REFERENCES profile_groups(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_group_members_profile
    ON profile_group_members(profile_name);

-- ───────────────────────────────────────────────────────────────
-- Traffic stats — AGGREGATED per (profile, domain, hour_bucket).
--
-- We record TOTALS per bucket, not individual requests. A 4-hour run
-- that hits google.com 10,000 times creates at most 4 rows, not 10,000.
-- Hour granularity gives us useful time-series charts without blowing
-- up DB size. Rough sizing:
--
--   ~30 active hours/day * 50 unique domains * 10 profiles = 15,000 rows/day
--   90 days retention = ~1.4M rows ≈ 50 MB of SQLite.
--
-- Cleanup policy: rows older than traffic.retention_days (default 90)
-- are deleted by traffic_cleanup() which dashboard_server runs at
-- startup and once per day.
--
-- We intentionally DO NOT store full URLs or paths. Only (domain, bytes)
-- — enough for cost attribution, granular enough for "who's eating my
-- bandwidth" questions, narrow enough to never log sensitive query params.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS traffic_stats (
    profile_name  TEXT    NOT NULL,
    domain        TEXT    NOT NULL,
    hour_bucket   TEXT    NOT NULL,   -- 'YYYY-MM-DD HH' (local time, hour resolution)
    bytes         INTEGER NOT NULL DEFAULT 0,   -- cumulative in this bucket
    req_count     INTEGER NOT NULL DEFAULT 0,   -- cumulative request count
    run_id        INTEGER,                       -- first run that contributed (nullable)
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (profile_name, domain, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_traffic_profile_time
    ON traffic_stats(profile_name, hour_bucket DESC);
CREATE INDEX IF NOT EXISTS idx_traffic_bucket
    ON traffic_stats(hour_bucket);
CREATE INDEX IF NOT EXISTS idx_traffic_domain
    ON traffic_stats(domain);
"""


# Дефолтные значения configа — используются on первом запуске.
# Platform-aware chrome path — default subdir and binary differ per OS.
def _default_chrome_binary_path() -> str:
    try:
        from platform_paths import default_chrome_subdir, PLATFORM
        subdir = default_chrome_subdir()
        if PLATFORM == "windows":
            return f"{subdir}/chrome.exe"
        elif PLATFORM == "darwin":
            return f"{subdir}/Chromium.app/Contents/MacOS/Chromium"
        else:  # linux
            return f"{subdir}/chrome"
    except Exception:
        return "chrome_win64/chrome.exe"   # fallback


DEFAULT_CONFIG = {
    "search.queries":             ["гудмедика", "гудмедіка", "goodmedika"],
    "search.my_domains":          ["goodmedika.com.ua", "goodmedika.ua", "goodmedika.com"],
    "search.target_domains":      [],
    "search.block_domains":       [],
    "search.refresh_min_sec":     10,
    "search.refresh_max_sec":     15,
    "search.refresh_max_attempts": 4,

    "proxy.url":                  "",
    # None = auto-derive from rotation_api_url presence (recommended).
    # Explicit True = force rotation on even without API (rare).
    # Explicit False = disable rotation even if API configured (debug).
    # See main.py::_resolve_rotation() for the full logic.
    "proxy.is_rotating":          None,
    "proxy.rotation_provider":    "none",    # none | asocks | brightdata | generic
    "proxy.rotation_api_url":     None,
    # asocks uses a path + query-param URL shape. We store the two parts
    # separately and assemble at save time so the user doesn't have to
    # escape/concatenate by hand. For non-asocks providers these are
    # ignored and rotation_api_url is used verbatim.
    "proxy.asocks_port_id":       None,
    "proxy.asocks_api_key":       None,
    "proxy.rotation_api_key":     None,
    "proxy.rotation_method":      "GET",
    "proxy.pool_urls":            [],
    "proxy.use_pool":             False,

    "browser.profile_name":       "profile_01",
    # Relative path resolved against current working directory.
    # Layout depends on platform:
    #   Windows  chrome_win64/chrome.exe
    #   macOS    chrome_mac/Chromium.app/Contents/MacOS/Chromium
    #   Linux    chrome_linux/chrome
    # deploy-ghost-shell-flat.{bat|sh} creates the layout automatically.
    "browser.binary_path":        _default_chrome_binary_path(),
    "browser.auto_session":       True,
    # enrich_on_create: seed History/Bookmarks/Top Sites on fresh profile.
    # Makes the browser look like a real user's aged profile instead of
    # a sterile brand-new one (which is a detection signal).
    "browser.enrich_on_create":   True,
    "browser.preferred_language": "uk-UA",
    # Geo-matching: we reject the run if the exit IP country doesn't match
    # these expectations (unless geo_mismatch_mode is "warn" or "rotate").
    "browser.expected_country":   "Ukraine",
    "browser.expected_timezone":  "Europe/Kyiv",
    "browser.geo_mismatch_mode":  "rotate",   # abort | rotate | warn
    # UA spoof range — bounds for Chrome major version in the fingerprint.
    # Configurable on Settings page. Defaults match the current pool
    # (Chrome 143–147 as of Apr 2026).
    "browser.spoof_chrome_min":   143,
    "browser.spoof_chrome_max":   147,

    # Resource-blocking via CDP Network.setBlockedURLs. Every toggle
    # here is OFF by default to stay conservative — a non-googler who
    # changes the defaults will see the same result as the previous
    # build. Flip these on in Settings page to trade realism for speed.
    #
    # Each bucket is an English category — dashboard_server maps it to
    # URL patterns at runtime. Patterns stay in code, not config, so
    # users can't accidentally break Google by blocking it.
    # Default-on because they burn MASSIVE proxy traffic without affecting
    # ad detection at all — YouTube video blobs alone can be 5-20 MB each
    # if a SERP result happens to contain an embedded video preview.
    # Users on unlimited/free proxies can turn these off in Settings.
    "browser.block_youtube_video":      True,   # *.ytimg.com, *.youtube.com/*.mp4, *.googlevideo.com
    "browser.block_google_images":      False,  # off — thumbnails sometimes matter for context
    "browser.block_google_maps_tiles":  True,   # *mt*.google.com/vt/* — huge when "map pack" in SERP
    "browser.block_fonts":              False,  # off — affects page rendering
    "browser.block_analytics":          True,   # google-analytics, doubleclick beacons — no ad-parse impact
    "browser.block_social_widgets":     True,   # facebook/twitter/linkedin embeds — unrelated to ads
    "browser.block_video_everywhere":   False,  # off — too aggressive, can break some sites
    "browser.block_custom_patterns":    [],     # user-supplied URL patterns (CDP wildcard syntax)

    # ── Runner pool ────────────────────────────────────────────
    # Maximum number of profiles that can run simultaneously.
    # Each running profile spawns a full Chrome instance plus a
    # Python main.py subprocess, so this is a rough memory/CPU cap.
    # 4 is safe on most desktops (8 GB RAM, 4 cores); scale up on
    # beefier machines. UI shows a warning when the user is about
    # to exceed this.
    "runner.max_parallel":              4,
    "runner.warn_at_parallel":          3,    # show amber UI warning at this count

    # ── Traffic accounting ─────────────────────────────────────
    # We aggregate bytes per (profile, domain, hour) via CDP events.
    # Keeping enabled costs ~1% CPU per browser and 50 MB of DB over
    # 90 days. Disable if running on a box where the disk is tight or
    # you don't care about who's eating bandwidth.
    "traffic.enabled":                  True,
    # How long to keep traffic rows. Older rows are deleted at startup
    # and once per day. Set 0 to disable cleanup (keep forever).
    "traffic.retention_days":           90,
    # How often (seconds) the in-browser aggregator flushes pending
    # buckets to SQLite. Lower = fresher dashboard numbers but more
    # write volume. 30s is a good tradeoff.
    "traffic.flush_interval_sec":       30,
    # Auto-rotate exit IP at start of each run. Forces a fresh TCP connection
    # to the rotating proxy so we get a new random Ukrainian IP each time.
    # Without this, subsequent runs in the same session can reuse the same IP
    # which is a detection signal for Google.
    "proxy.auto_rotate_on_start": True,

    "captcha.twocaptcha_key":     "",

    # Post-ad actions — what to do after we detected an ad on the SERP
    # Each action is {type: visit|dwell|scroll|click_result, ...params}
    # Legacy keys (kept for compat with older imports):
    "actions.post_ad":            [],
    "actions.on_target_domain":   [],
    # Modern keys used by Scripts page + main.py:
    "actions.main_script":              [],
    "actions.post_ad_actions":          [],
    "actions.on_target_domain_actions": [],

    "behavior.open_background_tabs": False,
    "behavior.bg_tabs_count":     [2, 4],
    "behavior.idle_pauses":       False,
    "behavior.pre_target_warmup": False,

    # ── Behavior timing (seconds) — configured from dashboard ───
    # Delay after initial page load before we start reading the SERP
    "behavior.initial_load_min":     2.0,
    "behavior.initial_load_max":     4.0,
    # Delay after WebDriverWait confirms SERP DOM is ready
    "behavior.serp_settle_min":      1.5,
    "behavior.serp_settle_max":      3.0,
    # Delay after driver.refresh() before rechecking page state
    "behavior.post_refresh_min":     2.0,
    "behavior.post_refresh_max":     4.0,
    # Delay after force_rotate_ip() before re-running geo check
    "behavior.post_rotate_min":      2.0,
    "behavior.post_rotate_max":      4.0,
    # Delay after first-ever visit to google.com on a fresh profile
    "behavior.fresh_google_min":     3.0,
    "behavior.fresh_google_max":     5.0,
    # Delay after clicking "Accept all" cookies consent
    "behavior.post_consent_min":     2.0,
    "behavior.post_consent_max":     4.0,
    # Gap between consecutive queries in a single run
    "behavior.between_queries_min":  6.0,
    "behavior.between_queries_max": 12.0,

    "scheduler.target_runs_per_day":     30,
    "scheduler.active_hours":            [7, 20],
    "scheduler.min_interval_sec":        180,
    "scheduler.max_interval_sec":        1200,
    "scheduler.max_consecutive_fails":   5,
    "scheduler.fail_pause_sec":          1800,
    # Profiles to cycle through — empty = use browser.profile_name only
    "scheduler.profile_names":           [],
    "scheduler.selection_mode":          "random",   # random | round-robin
    # Group-based trigger — if set, each scheduler iteration launches
    # the whole group's members instead of picking one profile at a time.
    # Takes precedence over profile_names. Leave null to use the old
    # "pick one profile" flow.
    "scheduler.group_id":                None,       # int | null
    "scheduler.group_mode":              "parallel", # parallel | serial
    # Extra jitter % applied on top of the base random spacing (0..100)
    "scheduler.jitter_percent":          25,

    # Watchdog — browser hang protection
    "watchdog.max_stall_sec":     180,
    "watchdog.check_interval_sec": 15,
}


# ──────────────────────────────────────────────────────────────
# DB CLASS
# ──────────────────────────────────────────────────────────────

class DB:
    """
    Потокоwithoutопасная обёртка над SQLite.
    Использует локальные коннекции на поток + WAL for конкурентного доступа.
    """

    _local = threading.local()

    def __init__(self, path: str = None):
        self.path = path or DB_PATH
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Коннекция for текущего потока"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self):
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        # Ensure newer columns exist on old DBs — SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so we check PRAGMA first.
        self._ensure_column(conn, "runs", "pid",          "INTEGER")
        self._ensure_column(conn, "runs", "heartbeat_at", "TEXT")
        # Check how full config is. On first init (empty), bulk-insert all
        # defaults. On subsequent starts after an upgrade, only insert keys
        # that are missing — so user customisations stay and new features
        # get their default values.
        cursor = conn.execute("SELECT COUNT(*) FROM config_kv")
        row_count = cursor.fetchone()[0]
        now = datetime.now().isoformat(timespec="seconds")
        if row_count == 0:
            logging.info("[DB] Config пуст — onменяем defaults")
            for key, value in DEFAULT_CONFIG.items():
                conn.execute(
                    "INSERT INTO config_kv (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now)
                )
        else:
            # Idempotent backfill — only adds keys that don't exist yet.
            # INSERT OR IGNORE leaves existing rows alone so users keep
            # their customised values through upgrades.
            added = 0
            for key, value in DEFAULT_CONFIG.items():
                cur = conn.execute(
                    "INSERT OR IGNORE INTO config_kv (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
                added += cur.rowcount
            if added:
                logging.info(f"[DB] Backfilled {added} new default config keys")

    # ──────────────────────────────────────────────────────────
    # CONFIG
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _ensure_column(conn, table: str, col: str, type_sql: str):
        """Idempotent ALTER TABLE ADD COLUMN for schema migrations.
        SQLite has no `ADD COLUMN IF NOT EXISTS`, so we probe via
        PRAGMA first. Safe to call repeatedly at startup."""
        existing = {r["name"] for r in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_sql}")
            logging.info(f"[DB] Migrated: added {table}.{col} ({type_sql})")

    def config_get(self, key: str, default: Any = None) -> Any:
        row = self._get_conn().execute(
            "SELECT value FROM config_kv WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def config_set(self, key: str, value: Any):
        self._get_conn().execute("""
            INSERT INTO config_kv (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, json.dumps(value, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))

    def config_get_all(self) -> dict:
        """Возвращает all config as вложенный dict"""
        rows = self._get_conn().execute("SELECT key, value FROM config_kv").fetchall()
        flat = {}
        for row in rows:
            try:
                flat[row["key"]] = json.loads(row["value"])
            except Exception:
                flat[row["key"]] = row["value"]
        # Преобразуем flat { "proxy.url": "..." } в nested { proxy: { url: "..." } }
        nested: dict = {}
        for key, value in flat.items():
            parts = key.split(".")
            cur = nested
            for p in parts[:-1]:
                if p not in cur:
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = value
        return nested

    def config_set_all(self, nested: dict):
        """Массовое обновление — рекурсивно разворачивает nested dict"""
        def flatten(d: dict, prefix: str = "") -> dict:
            result = {}
            for k, v in d.items():
                full_key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    result.update(flatten(v, full_key))
                else:
                    result[full_key] = v
            return result

        flat = flatten(nested)
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._get_conn()
        with conn:
            for key, value in flat.items():
                conn.execute("""
                    INSERT INTO config_kv (key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """, (key, json.dumps(value, ensure_ascii=False), now))

    # ──────────────────────────────────────────────────────────
    # RUNS
    # ──────────────────────────────────────────────────────────

    def run_start(self, profile_name: str, proxy_url: str = None) -> int:
        cur = self._get_conn().execute("""
            INSERT INTO runs (started_at, profile_name, proxy_url, heartbeat_at)
            VALUES (?, ?, ?, ?)
        """, (datetime.now().isoformat(timespec="seconds"),
              profile_name, proxy_url,
              datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid

    def run_set_pid(self, run_id: int, pid: int):
        """Called by dashboard_server after spawning main.py so we can
        kill stale subprocesses on next dashboard restart."""
        self._get_conn().execute(
            "UPDATE runs SET pid = ? WHERE id = ?", (pid, run_id)
        )

    def run_heartbeat(self, run_id: int):
        """Pinged by main.py every ~15 seconds while a run is alive.
        If no heartbeat for 3 min, stale-detector treats the run as
        hung and triggers forced cleanup."""
        self._get_conn().execute(
            "UPDATE runs SET heartbeat_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), run_id)
        )

    def runs_find_unfinished_with_pid(self) -> list:
        """Returns runs that never wrote finished_at. Each has a PID we
        can check against the OS — if the PID is dead, mark the run as
        stale; if the PID is alive but heartbeat is old, kill it.
        Called by dashboard_server.cleanup_stale_runs() at startup."""
        rows = self._get_conn().execute("""
            SELECT id, profile_name, pid, started_at, heartbeat_at
            FROM runs
            WHERE finished_at IS NULL
            ORDER BY started_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def runs_live_for_profile(self, profile_name: str) -> list:
        """All runs for this profile that are still marked as running
        (finished_at IS NULL). Pre-spawn guards use this — if ANY match,
        we refuse to start a new run unless we can prove the old one is
        really dead. Returns newest first."""
        rows = self._get_conn().execute("""
            SELECT id, pid, started_at, heartbeat_at
            FROM runs
            WHERE profile_name = ? AND finished_at IS NULL
            ORDER BY started_at DESC
        """, (profile_name,)).fetchall()
        return [dict(r) for r in rows]

    def run_finish(self, run_id: int, exit_code: int = 0, error: str = None,
                   total_queries: int = None, total_ads: int = None,
                   captchas: int = None, ip_used: str = None):
        fields = ["finished_at = ?", "exit_code = ?"]
        values = [datetime.now().isoformat(timespec="seconds"), exit_code]
        if error is not None:
            fields.append("error = ?");         values.append(error)
        if total_queries is not None:
            fields.append("total_queries = ?"); values.append(total_queries)
        if total_ads is not None:
            fields.append("total_ads = ?");     values.append(total_ads)
        if captchas is not None:
            fields.append("captchas = ?");      values.append(captchas)
        if ip_used is not None:
            fields.append("ip_used = ?");       values.append(ip_used)
        values.append(run_id)

        self._get_conn().execute(
            f"UPDATE runs SET {', '.join(fields)} WHERE id = ?",
            values
        )

    def runs_list(self, limit: int = 50, profile_name: str = None) -> list[dict]:
        conn = self._get_conn()
        if profile_name:
            rows = conn.execute("""
                SELECT * FROM runs WHERE profile_name = ?
                ORDER BY started_at DESC LIMIT ?
            """, (profile_name, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM runs ORDER BY started_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def run_latest(self, profile_name: str = None) -> Optional[dict]:
        runs = self.runs_list(limit=1, profile_name=profile_name)
        return runs[0] if runs else None

    # ──────────────────────────────────────────────────────────
    # EVENTS
    # ──────────────────────────────────────────────────────────

    def event_record(self, run_id: Optional[int], profile_name: str, event_type: str,
                     query: str = None, details: str = None,
                     duration_sec: float = None, results_count: int = None):
        self._get_conn().execute("""
            INSERT INTO events (run_id, profile_name, timestamp, event_type,
                                query, details, duration_sec, results_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, profile_name, datetime.now().isoformat(timespec="seconds"),
              event_type, query, details, duration_sec, results_count))

    def events_list(self, profile_name: str = None, since_hours: int = None,
                    event_type: str = None, limit: int = 500) -> list[dict]:
        conn = self._get_conn()
        where, params = [], []
        if profile_name:
            where.append("profile_name = ?"); params.append(profile_name)
        if since_hours:
            cutoff = (datetime.now() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
            where.append("timestamp >= ?"); params.append(cutoff)
        if event_type:
            where.append("event_type = ?"); params.append(event_type)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def events_summary(self, profile_name: str = None, hours: int = 24) -> dict:
        """Сводка по типам событий за N часов"""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        params = [cutoff]
        sql = """
            SELECT event_type, COUNT(*) as n FROM events WHERE timestamp >= ?
        """
        if profile_name:
            sql += " AND profile_name = ?"
            params.append(profile_name)
        sql += " GROUP BY event_type"
        rows = conn.execute(sql, params).fetchall()
        return {r["event_type"]: r["n"] for r in rows}

    # ──────────────────────────────────────────────────────────
    # SELFCHECKS
    # ──────────────────────────────────────────────────────────

    def selfcheck_save(self, run_id: Optional[int], profile_name: str,
                       passed: int, total: int, tests: dict,
                       actual: dict = None, expected: dict = None) -> int:
        cur = self._get_conn().execute("""
            INSERT INTO selfchecks (run_id, profile_name, timestamp, passed, total,
                                    tests_json, actual_json, expected_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, profile_name, datetime.now().isoformat(timespec="seconds"),
              passed, total,
              json.dumps(tests, ensure_ascii=False),
              json.dumps(actual, ensure_ascii=False) if actual else None,
              json.dumps(expected, ensure_ascii=False) if expected else None))
        return cur.lastrowid

    def selfcheck_latest(self, profile_name: str) -> Optional[dict]:
        row = self._get_conn().execute("""
            SELECT * FROM selfchecks WHERE profile_name = ?
            ORDER BY timestamp DESC LIMIT 1
        """, (profile_name,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tests"]    = json.loads(d.pop("tests_json") or "{}")
        d["actual"]   = json.loads(d.pop("actual_json") or "{}") if d.get("actual_json") else None
        d["expected"] = json.loads(d.pop("expected_json") or "{}") if d.get("expected_json") else None
        return d

    def selfchecks_history(self, profile_name: str, limit: int = 20) -> list[dict]:
        rows = self._get_conn().execute("""
            SELECT id, timestamp, passed, total FROM selfchecks
            WHERE profile_name = ? ORDER BY timestamp DESC LIMIT ?
        """, (profile_name, limit)).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────
    # TRAFFIC STATS (aggregated per profile × domain × hour)
    # ──────────────────────────────────────────────────────────
    #
    # We never store individual HTTP requests — that would bloat the DB
    # and record sensitive URL paths. Instead, the browser-side collector
    # aggregates request sizes in memory and flushes buckets here every
    # 30 seconds via traffic_record_batch().
    #
    # Each bucket key is (profile_name, domain, hour_bucket). Two flushes
    # targeting the same bucket MERGE rather than conflict — we use
    # INSERT ... ON CONFLICT DO UPDATE with accumulating arithmetic.

    @staticmethod
    def _hour_bucket(ts: datetime = None) -> str:
        """Format a datetime as 'YYYY-MM-DD HH' for bucket keying.
        Uses local time — matches what users see in their dashboard
        timezone. Hour is enough resolution for traffic charts without
        making the table 60× bigger than it needs to be."""
        ts = ts or datetime.now()
        return ts.strftime("%Y-%m-%d %H")

    def traffic_record_batch(self, profile_name: str, run_id: Optional[int],
                             by_domain: dict, when: datetime = None):
        """Flush a batch of (domain -> {bytes, req_count}) into the DB.

        Called by GhostShellBrowser's in-memory aggregator every ~30 seconds
        while a run is live. `by_domain` is a dict like:
            {"google.com": {"bytes": 1234567, "req_count": 42},
             "dl.google.com": {"bytes": 2345678, "req_count": 8}}

        All pairs are bucketed into the SAME hour — the caller doesn't
        flush across hour boundaries.
        """
        if not by_domain or not profile_name:
            return
        bucket = self._hour_bucket(when)
        conn = self._get_conn()
        # Batch upsert — one statement per domain, but all under a single
        # transaction so 50-domain flushes stay fast.
        with conn:
            for domain, stats in by_domain.items():
                if not domain:
                    continue
                b = int(stats.get("bytes") or 0)
                c = int(stats.get("req_count") or 0)
                if b <= 0 and c <= 0:
                    continue
                # SQLite UPSERT syntax — atomic and locks-free at row level.
                conn.execute("""
                    INSERT INTO traffic_stats
                        (profile_name, domain, hour_bucket, bytes, req_count, run_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(profile_name, domain, hour_bucket) DO UPDATE SET
                        bytes      = bytes + excluded.bytes,
                        req_count  = req_count + excluded.req_count,
                        updated_at = datetime('now')
                """, (profile_name, domain, bucket, b, c, run_id))

    def traffic_summary(self, hours: int = 24) -> dict:
        """Total bytes + requests across all profiles in the last N hours.
        Returns {total_bytes, total_requests, profile_count, domain_count,
                 by_hour: [{hour, bytes, requests}, ...]}"""
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H")
        conn = self._get_conn()
        totals = conn.execute("""
            SELECT
                COALESCE(SUM(bytes), 0)     AS total_bytes,
                COALESCE(SUM(req_count), 0) AS total_requests,
                COUNT(DISTINCT profile_name) AS profile_count,
                COUNT(DISTINCT domain)       AS domain_count
            FROM traffic_stats
            WHERE hour_bucket >= ?
        """, (cutoff,)).fetchone()

        by_hour = conn.execute("""
            SELECT
                hour_bucket                    AS hour,
                COALESCE(SUM(bytes), 0)        AS bytes,
                COALESCE(SUM(req_count), 0)    AS requests
            FROM traffic_stats
            WHERE hour_bucket >= ?
            GROUP BY hour_bucket
            ORDER BY hour_bucket ASC
        """, (cutoff,)).fetchall()

        return {
            "total_bytes":    totals["total_bytes"] or 0,
            "total_requests": totals["total_requests"] or 0,
            "profile_count":  totals["profile_count"] or 0,
            "domain_count":   totals["domain_count"] or 0,
            "by_hour":        [dict(r) for r in by_hour],
        }

    def traffic_by_profile(self, hours: int = 24) -> list:
        """Per-profile traffic totals for the last N hours.
        Returns [{profile_name, bytes, requests, domain_count}, ...]
        sorted by bytes desc."""
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H")
        rows = self._get_conn().execute("""
            SELECT
                profile_name,
                COALESCE(SUM(bytes), 0)       AS bytes,
                COALESCE(SUM(req_count), 0)   AS requests,
                COUNT(DISTINCT domain)        AS domain_count
            FROM traffic_stats
            WHERE hour_bucket >= ?
            GROUP BY profile_name
            ORDER BY bytes DESC
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    def traffic_by_domain(self, hours: int = 24, limit: int = 50,
                          profile_name: Optional[str] = None) -> list:
        """Top domains by bytes in the last N hours. Optionally filter
        to a single profile — useful to answer 'what's eating my
        profile_03's bandwidth'."""
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H")
        conn = self._get_conn()
        if profile_name:
            rows = conn.execute("""
                SELECT domain,
                       SUM(bytes)     AS bytes,
                       SUM(req_count) AS requests
                FROM traffic_stats
                WHERE hour_bucket >= ? AND profile_name = ?
                GROUP BY domain
                ORDER BY bytes DESC
                LIMIT ?
            """, (cutoff, profile_name, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT domain,
                       SUM(bytes)     AS bytes,
                       SUM(req_count) AS requests,
                       COUNT(DISTINCT profile_name) AS profiles
                FROM traffic_stats
                WHERE hour_bucket >= ?
                GROUP BY domain
                ORDER BY bytes DESC
                LIMIT ?
            """, (cutoff, limit)).fetchall()
        return [dict(r) for r in rows]

    def traffic_timeseries(self, profile_name: Optional[str] = None,
                           hours: int = 24, bucket: str = "hour") -> list:
        """Time series for the traffic chart.

        bucket = 'hour' (default, finest — use for hours <= 48)
        bucket = 'day'  (use for hours > 48, aggregates each day)

        Returns [{time: 'YYYY-MM-DD HH' or 'YYYY-MM-DD', bytes, requests}].
        """
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H")
        # Day-bucketing: derive 'YYYY-MM-DD' from 'YYYY-MM-DD HH' via SUBSTR(,1,10)
        time_expr = "hour_bucket" if bucket == "hour" else "SUBSTR(hour_bucket, 1, 10)"
        where_clause = "hour_bucket >= ?"
        params = [cutoff]
        if profile_name:
            where_clause += " AND profile_name = ?"
            params.append(profile_name)
        rows = self._get_conn().execute(f"""
            SELECT {time_expr}                   AS time,
                   COALESCE(SUM(bytes), 0)       AS bytes,
                   COALESCE(SUM(req_count), 0)   AS requests
            FROM traffic_stats
            WHERE {where_clause}
            GROUP BY time
            ORDER BY time ASC
        """, params).fetchall()
        return [dict(r) for r in rows]

    def traffic_cleanup(self, retention_days: int = 90) -> int:
        """Delete traffic rows older than `retention_days`. Returns row
        count removed. Called by dashboard_server at startup + once/day."""
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H")
        cur = self._get_conn().execute(
            "DELETE FROM traffic_stats WHERE hour_bucket < ?", (cutoff,)
        )
        return cur.rowcount or 0

    # ──────────────────────────────────────────────────────────
    # COMPETITORS
    # ──────────────────────────────────────────────────────────

    def competitor_add(self, run_id: Optional[int], query: str, domain: str,
                       title: str = None, display_url: str = None,
                       clean_url: str = None, google_click_url: str = None):
        self._get_conn().execute("""
            INSERT INTO competitors (run_id, timestamp, query, domain,
                                     title, display_url, clean_url, google_click_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, datetime.now().isoformat(timespec="seconds"),
              query, domain, title, display_url, clean_url, google_click_url))

    def competitors_by_domain(self) -> list[dict]:
        rows = self._get_conn().execute("""
            SELECT
                domain,
                COUNT(*) as mentions,
                MIN(timestamp) as first_seen,
                MAX(timestamp) as last_seen,
                GROUP_CONCAT(DISTINCT query) as queries
            FROM competitors
            GROUP BY domain
            ORDER BY mentions DESC
        """).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["queries"] = (d["queries"] or "").split(",")
            result.append(d)
        return result

    def competitors_recent(self, limit: int = 100) -> list[dict]:
        rows = self._get_conn().execute("""
            SELECT * FROM competitors ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def competitors_count(self) -> tuple[int, int]:
        """Возвращает (total_records, unique_domains)"""
        row = self._get_conn().execute("""
            SELECT COUNT(*) as n, COUNT(DISTINCT domain) as d FROM competitors
        """).fetchone()
        return row["n"], row["d"]

    # ──────────────────────────────────────────────────────────
    # ACTION EVENTS — per-step pipeline execution log
    # ──────────────────────────────────────────────────────────

    def action_event_add(self, run_id: Optional[int], profile_name: str,
                         query: str, ad_domain: str, ad_class: str,
                         action_type: str, outcome: str,
                         skip_reason: str = None, duration_sec: float = None,
                         error: str = None):
        """Record one step execution.

        outcome: "ran" | "skipped" | "error"
        ad_class: "target" | "my_domain" | "competitor" | "unknown"
        """
        self._get_conn().execute("""
            INSERT INTO action_events
                (run_id, profile_name, timestamp, query, ad_domain, ad_class,
                 action_type, outcome, skip_reason, duration_sec, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, profile_name,
            datetime.now().isoformat(timespec="seconds"),
            query, ad_domain, ad_class,
            action_type, outcome, skip_reason, duration_sec, error,
        ))
        self._get_conn().commit()

    def action_events_summary(self, hours: int = 24) -> dict:
        """Roll-up counters for the Overview page.

        Returns:
            {
              "actions_ran":     int,   # outcomes = "ran"
              "actions_skipped": int,   # outcomes = "skipped"
              "actions_errored": int,   # outcomes = "error"
              "by_type":         {"click_ad": 12, "visit_link": 3, ...},
              "by_ad_class":     {"target": 8, "competitor": 40, ...},
            }
        """
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        conn = self._get_conn()

        totals = conn.execute("""
            SELECT outcome, COUNT(*) as n
            FROM action_events WHERE timestamp >= ?
            GROUP BY outcome
        """, (cutoff,)).fetchall()
        out = {
            "actions_ran":     0,
            "actions_skipped": 0,
            "actions_errored": 0,
        }
        for r in totals:
            if r["outcome"] == "ran":     out["actions_ran"]     = r["n"]
            elif r["outcome"] == "skipped": out["actions_skipped"] = r["n"]
            elif r["outcome"] == "error":   out["actions_errored"] = r["n"]

        by_type = conn.execute("""
            SELECT action_type, COUNT(*) as n
            FROM action_events
            WHERE timestamp >= ? AND outcome = 'ran'
            GROUP BY action_type
            ORDER BY n DESC
        """, (cutoff,)).fetchall()
        out["by_type"] = {r["action_type"]: r["n"] for r in by_type}

        by_class = conn.execute("""
            SELECT ad_class, COUNT(*) as n
            FROM action_events
            WHERE timestamp >= ? AND outcome = 'ran'
            GROUP BY ad_class
        """, (cutoff,)).fetchall()
        out["by_ad_class"] = {r["ad_class"]: r["n"] for r in by_class}

        return out

    def action_events_by_domain(self, hours: int = None) -> dict:
        """Returns {domain: {"ran": N, "skipped": N, "last_action_at": iso}}

        Used by Competitors page to show "Actions" column alongside mentions.
        """
        query = """
            SELECT ad_domain,
                   SUM(CASE WHEN outcome='ran'     THEN 1 ELSE 0 END) as ran,
                   SUM(CASE WHEN outcome='skipped' THEN 1 ELSE 0 END) as skipped,
                   SUM(CASE WHEN outcome='error'   THEN 1 ELSE 0 END) as errored,
                   MAX(timestamp) as last_action_at
            FROM action_events
            WHERE ad_domain IS NOT NULL AND ad_domain != ''
        """
        params = ()
        if hours:
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
            query += " AND timestamp >= ?"
            params = (cutoff,)
        query += " GROUP BY ad_domain"

        rows = self._get_conn().execute(query, params).fetchall()
        return {
            r["ad_domain"]: {
                "ran":            r["ran"]     or 0,
                "skipped":        r["skipped"] or 0,
                "errored":        r["errored"] or 0,
                "last_action_at": r["last_action_at"],
            }
            for r in rows
        }

    def action_events_recent(self, limit: int = 50) -> list[dict]:
        rows = self._get_conn().execute("""
            SELECT * FROM action_events ORDER BY timestamp DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────
    # IP HISTORY
    # ──────────────────────────────────────────────────────────

    def ip_report(self, ip: str, success: bool = True, captcha: bool = False,
                  country: str = None, city: str = None, org: str = None, asn: str = None,
                  burn_after: int = 3):
        if not ip:
            return
        conn = self._get_conn()
        now = datetime.now().isoformat(timespec="seconds")
        row = conn.execute("SELECT * FROM ip_history WHERE ip = ?", (ip,)).fetchone()

        if row is None:
            conn.execute("""
                INSERT INTO ip_history (ip, first_seen, last_seen, total_uses,
                    total_captchas, consecutive_capchas, country, city, org, asn)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """, (ip, now, now, 1 if captcha else 0,
                  1 if captcha else 0, country, city, org, asn))
            return

        total_uses = row["total_uses"] + 1
        total_capt = row["total_captchas"] + (1 if captcha else 0)
        cons = row["consecutive_capchas"] + 1 if captcha else (0 if success else row["consecutive_capchas"])

        burned_at = row["burned_at"]
        if cons >= burn_after and not burned_at:
            burned_at = now

        conn.execute("""
            UPDATE ip_history
            SET last_seen = ?, total_uses = ?, total_captchas = ?,
                consecutive_capchas = ?, burned_at = ?,
                country = COALESCE(?, country), city = COALESCE(?, city),
                org = COALESCE(?, org), asn = COALESCE(?, asn)
            WHERE ip = ?
        """, (now, total_uses, total_capt, cons, burned_at,
              country, city, org, asn, ip))

    def ip_get(self, ip: str) -> Optional[dict]:
        """Return the single ip_history row as dict, or None."""
        row = self._get_conn().execute(
            "SELECT * FROM ip_history WHERE ip = ?", (ip,)
        ).fetchone()
        return dict(row) if row else None

    def ip_unburn(self, ip: str):
        """Clear burned_at and reset consecutive captchas (cooldown expired)."""
        self._get_conn().execute("""
            UPDATE ip_history
            SET burned_at = NULL, consecutive_capchas = 0
            WHERE ip = ?
        """, (ip,))

    def ip_update_meta(self, ip: str, country: str = None, city: str = None,
                       org: str = None, asn: str = None):
        """Update geo metadata for an IP (called after enrichment)."""
        if not ip:
            return
        conn = self._get_conn()
        row = conn.execute("SELECT 1 FROM ip_history WHERE ip = ?", (ip,)).fetchone()
        if not row:
            # No history yet — create a minimal row so metadata isn't lost
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute("""
                INSERT INTO ip_history (ip, first_seen, last_seen, total_uses,
                    total_captchas, consecutive_capchas, country, city, org, asn)
                VALUES (?, ?, ?, 0, 0, 0, ?, ?, ?, ?)
            """, (ip, now, now, country, city, org, asn))
            return
        conn.execute("""
            UPDATE ip_history
            SET country = COALESCE(?, country), city = COALESCE(?, city),
                org = COALESCE(?, org), asn = COALESCE(?, asn)
            WHERE ip = ?
        """, (country, city, org, asn, ip))

    def ip_record_start(self, ip: str, country: str = None, city: str = None,
                        org: str = None, asn: str = None):
        """
        Register that `ip` is being used RIGHT NOW (bump total_uses and
        last_seen). Called at session start for every IP — including
        static-proxy setups where no subsequent `ip_report(captcha=...)`
        ever fires. Without this, Proxy → IP statistics stays empty for
        non-rotating setups.

        For rotating proxies, this is called in addition to report() so
        we capture the IP even if the first request is instant-cut and
        never reaches report().
        """
        if not ip:
            return
        conn = self._get_conn()
        now = datetime.now().isoformat(timespec="seconds")
        row = conn.execute("SELECT total_uses FROM ip_history WHERE ip = ?",
                           (ip,)).fetchone()
        if row is None:
            conn.execute("""
                INSERT INTO ip_history
                    (ip, first_seen, last_seen, total_uses, total_captchas,
                     consecutive_capchas, country, city, org, asn)
                VALUES (?, ?, ?, 1, 0, 0, ?, ?, ?, ?)
            """, (ip, now, now, country, city, org, asn))
        else:
            conn.execute("""
                UPDATE ip_history
                SET last_seen = ?, total_uses = total_uses + 1,
                    country = COALESCE(?, country), city = COALESCE(?, city),
                    org = COALESCE(?, org), asn = COALESCE(?, asn)
                WHERE ip = ?
            """, (now, country, city, org, asn, ip))

    def ip_log_rotation(self, provider: str = "unknown"):
        """Record a rotation call (for stats + dashboard)."""
        self.config_set(
            "proxy.last_rotation_at",
            datetime.now().isoformat(timespec="seconds"),
        )
        current = self.config_get("proxy.total_rotations") or 0
        self.config_set("proxy.total_rotations", current + 1)

    def ip_summary(self) -> dict:
        """Aggregate stats across all IPs for the dashboard."""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT
                COUNT(*)                           AS total_unique_ips,
                SUM(CASE WHEN burned_at IS NOT NULL THEN 1 ELSE 0 END) AS burned_count,
                SUM(total_uses)                    AS total_requests,
                SUM(total_captchas)                AS total_captchas
            FROM ip_history
        """).fetchone()
        total_uses = row["total_requests"] or 0
        total_capt = row["total_captchas"] or 0
        return {
            "total_unique_ips":   row["total_unique_ips"] or 0,
            "burned_count":       row["burned_count"] or 0,
            "healthy_count":      (row["total_unique_ips"] or 0) - (row["burned_count"] or 0),
            "total_requests":     total_uses,
            "total_captchas":     total_capt,
            "overall_captcha_rate": (total_capt / total_uses) if total_uses else 0,
            "total_rotations":    self.config_get("proxy.total_rotations") or 0,
            "last_rotation_at":   self.config_get("proxy.last_rotation_at"),
        }

    def ip_is_burned(self, ip: str, cooldown_hours: int = 12) -> bool:
        row = self._get_conn().execute(
            "SELECT burned_at FROM ip_history WHERE ip = ?", (ip,)
        ).fetchone()
        if not row or not row["burned_at"]:
            return False
        burned = datetime.fromisoformat(row["burned_at"])
        return (datetime.now() - burned) < timedelta(hours=cooldown_hours)

    def ip_stats(self, limit: int = 100) -> list[dict]:
        rows = self._get_conn().execute("""
            SELECT * FROM ip_history ORDER BY total_uses DESC LIMIT ?
        """, (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            rate = (d["total_captchas"] / d["total_uses"]) if d["total_uses"] else 0
            d["captcha_rate"] = rate
            if d["burned_at"]:
                d["status"] = "burned"
            elif rate > 0.3:
                d["status"] = "warning"
            elif d["total_uses"] == 0:
                d["status"] = "unused"
            else:
                d["status"] = "healthy"
            result.append(d)
        return result

    # ──────────────────────────────────────────────────────────
    # FINGERPRINTS
    # ──────────────────────────────────────────────────────────

    def fingerprint_save(self, profile_name: str, payload: dict) -> int:
        conn = self._get_conn()
        # Сбрасываем is_current у предыдущих
        conn.execute(
            "UPDATE fingerprints SET is_current = 0 WHERE profile_name = ?",
            (profile_name,)
        )
        cur = conn.execute("""
            INSERT INTO fingerprints (profile_name, timestamp, template_name,
                                      payload_json, is_current)
            VALUES (?, ?, ?, ?, 1)
        """, (profile_name, datetime.now().isoformat(timespec="seconds"),
              payload.get("template_name"),
              json.dumps(payload, ensure_ascii=False)))
        return cur.lastrowid

    def fingerprint_current(self, profile_name: str) -> Optional[dict]:
        row = self._get_conn().execute("""
            SELECT * FROM fingerprints WHERE profile_name = ? AND is_current = 1
            ORDER BY timestamp DESC LIMIT 1
        """, (profile_name,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def fingerprints_history(self, profile_name: str, limit: int = 20) -> list[dict]:
        rows = self._get_conn().execute("""
            SELECT id, timestamp, template_name, is_current
            FROM fingerprints WHERE profile_name = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (profile_name, limit)).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────
    # LOGS
    # ──────────────────────────────────────────────────────────

    MAX_LOGS = 10000  # лимит строк в таблице

    def log_add(self, run_id: Optional[int], level: str, message: str):
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO logs (run_id, timestamp, level, message)
            VALUES (?, ?, ?, ?)
        """, (run_id, datetime.now().isoformat(timespec="seconds"), level, message))

        # Ротация — if больше MAX_LOGS, уyesляем старые
        count_row = conn.execute("SELECT COUNT(*) as n FROM logs").fetchone()
        if count_row["n"] > self.MAX_LOGS:
            excess = count_row["n"] - self.MAX_LOGS
            conn.execute(
                "DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY id ASC LIMIT ?)",
                (excess,)
            )

    def logs_list(self, run_id: int = None, limit: int = 500) -> list[dict]:
        conn = self._get_conn()
        if run_id:
            rows = conn.execute(
                "SELECT * FROM logs WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────
    # PROFILES
    # ──────────────────────────────────────────────────────────

    def profiles_list(self) -> list[dict]:
        """
        Список all профилей that появлялись в runs + их статистика.
        """
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT DISTINCT profile_name FROM runs
            UNION
            SELECT DISTINCT profile_name FROM events
            UNION
            SELECT DISTINCT profile_name FROM fingerprints
        """).fetchall()
        profiles = [r["profile_name"] for r in rows]

        # Если БД пустая — добавим дефолтный профиль из configа
        if not profiles:
            default = self.config_get("browser.profile_name", "profile_01")
            profiles = [default]

        # Также добавим профor из папки profiles/ if is
        if os.path.exists("profiles"):
            for name in os.listdir("profiles"):
                if os.path.isdir(os.path.join("profiles", name)) and name not in profiles:
                    profiles.append(name)

        result = []
        for name in sorted(profiles):
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
            summary = conn.execute("""
                SELECT event_type, COUNT(*) as n FROM events
                WHERE profile_name = ? AND timestamp >= ?
                GROUP BY event_type
            """, (name, cutoff)).fetchall()
            events_24h = {r["event_type"]: r["n"] for r in summary}

            searches = events_24h.get("search_ok", 0)
            captchas = events_24h.get("captcha", 0)
            total    = searches + events_24h.get("search_empty", 0) + captchas

            if total == 0:
                status = "idle"
            elif captchas / total >= 0.5:
                status = "critical"
            elif captchas / total >= 0.2:
                status = "warning"
            else:
                status = "healthy"

            fp = self.fingerprint_current(name)
            sc = self.selfcheck_latest(name)

            # Last run timestamp — any run for this profile, completed or not
            last_run = conn.execute("""
                SELECT started_at, finished_at, exit_code
                  FROM runs
                 WHERE profile_name = ?
              ORDER BY id DESC
                 LIMIT 1
            """, (name,)).fetchone()

            result.append({
                "name":         name,
                "status":       status,
                "searches_24h": searches,
                "captchas_24h": captchas,
                "total_24h":    total,
                "fingerprint":  {
                    "template":  (fp or {}).get("template_name"),
                    "timestamp": (fp or {}).get("timestamp"),
                } if fp else None,
                "selfcheck":    {
                    "passed":    sc["passed"],
                    "total":     sc["total"],
                    "timestamp": sc["timestamp"],
                } if sc else None,
                # Last run: use finished_at if the run ended, else started_at
                # so "currently running" profiles show their start time.
                "last_run_at": (
                    (last_run["finished_at"] or last_run["started_at"])
                    if last_run else None
                ),
                "last_run_status": (
                    ("success" if last_run["exit_code"] == 0
                     else "failed" if last_run["exit_code"] is not None
                     else "running")
                    if last_run else None
                ),
            })

        # ── Enrichment pass: tags, proxy override, group membership ──
        # Fetch all metadata in one query per table and merge by name.
        # Profiles that only exist on disk / in runs still show up with
        # empty tags and no proxy override (fall back to global config).
        profile_meta_rows = conn.execute(
            "SELECT * FROM profiles"
        ).fetchall()
        meta_by_name = {r["name"]: dict(r) for r in profile_meta_rows}

        # Group membership — one row per (group, profile), collect group names
        group_rows = conn.execute("""
            SELECT m.profile_name, g.id AS group_id, g.name AS group_name
              FROM profile_group_members m
              JOIN profile_groups g ON g.id = m.group_id
        """).fetchall()
        groups_by_profile = {}
        for r in group_rows:
            groups_by_profile.setdefault(r["profile_name"], []).append({
                "id":   r["group_id"],
                "name": r["group_name"],
            })

        for row in result:
            name = row["name"]
            meta = meta_by_name.get(name, {})
            tags_raw = meta.get("tags")
            try:
                row["tags"] = json.loads(tags_raw) if tags_raw else []
            except Exception:
                row["tags"] = []
            row["proxy_url"]          = meta.get("proxy_url")
            row["proxy_is_rotating"]  = meta.get("proxy_is_rotating")
            row["rotation_api_url"]   = meta.get("rotation_api_url")
            row["rotation_provider"]  = meta.get("rotation_provider")
            row["notes"]              = meta.get("notes")
            row["groups"]             = groups_by_profile.get(name, [])

        return result

    # ──────────────────────────────────────────────────────────
    # PROFILE METADATA — tags, per-profile proxy override, notes
    # ──────────────────────────────────────────────────────────

    def profile_meta_get(self, name: str) -> dict:
        """Return the profiles row as a dict, or {} if no row exists yet.

        Callers should use profile_effective_proxy() rather than reading
        proxy_url directly — it handles the "inherit from global" case.
        """
        row = self._get_conn().execute(
            "SELECT * FROM profiles WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            return {}
        out = dict(row)
        try:
            out["tags"] = json.loads(out.get("tags") or "[]")
        except Exception:
            out["tags"] = []
        return out

    def profile_meta_upsert(self, name: str, **fields) -> None:
        """Create-or-update a profiles row. Unknown columns are ignored
        silently so callers don't have to care about schema drift.

        Accepts: tags (list), proxy_url, proxy_is_rotating,
        rotation_api_url, rotation_provider, rotation_api_key, notes.
        """
        allowed = {
            "tags", "proxy_url", "proxy_is_rotating",
            "rotation_api_url", "rotation_provider", "rotation_api_key",
            "notes",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return

        if "tags" in updates and isinstance(updates["tags"], list):
            updates["tags"] = json.dumps(updates["tags"], ensure_ascii=False)

        conn = self._get_conn()
        # INSERT OR IGNORE then UPDATE — sqlite3 < 3.24 doesn't support
        # ON CONFLICT gracefully, and we need to keep created_at untouched.
        conn.execute(
            "INSERT OR IGNORE INTO profiles(name) VALUES(?)", (name,)
        )
        cols = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [name]
        conn.execute(
            f"UPDATE profiles SET {cols}, updated_at = datetime('now') "
            f"WHERE name = ?",
            vals,
        )
        conn.commit()

    def profile_meta_delete(self, name: str) -> None:
        """Remove a profile's metadata row. Called when the whole
        profile is being deleted — group memberships cascade via FK."""
        conn = self._get_conn()
        conn.execute("DELETE FROM profiles WHERE name = ?", (name,))
        conn.execute(
            "DELETE FROM profile_group_members WHERE profile_name = ?",
            (name,),
        )
        conn.commit()

    def profile_effective_proxy(self, name: str) -> dict:
        """Return the proxy settings a run would actually use for this
        profile. Per-profile overrides win over global config values.

        Returns dict with keys: url, is_rotating, rotation_api_url,
        rotation_provider, rotation_api_key.
        """
        meta = self.profile_meta_get(name)
        def _pick(meta_key: str, cfg_key: str, default=None):
            v = meta.get(meta_key)
            if v is not None and v != "":
                return v
            cfg_v = self.config_get(cfg_key)
            return cfg_v if cfg_v is not None else default

        return {
            "url":              _pick("proxy_url",        "proxy.url", ""),
            "is_rotating":      bool(_pick("proxy_is_rotating", "proxy.is_rotating", True)),
            "rotation_api_url": _pick("rotation_api_url", "proxy.rotation_api_url"),
            "rotation_provider":_pick("rotation_provider","proxy.rotation_provider", "none"),
            "rotation_api_key": _pick("rotation_api_key", "proxy.rotation_api_key"),
        }

    # ──────────────────────────────────────────────────────────
    # PROFILE GROUPS
    # ──────────────────────────────────────────────────────────

    def group_list(self) -> list[dict]:
        """All groups + profile-count per group. Ordered by most recently updated."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT g.id, g.name, g.description, g.script, g.max_parallel,
                   g.created_at, g.updated_at,
                   (SELECT COUNT(*) FROM profile_group_members m
                     WHERE m.group_id = g.id) AS member_count
              FROM profile_groups g
          ORDER BY g.updated_at DESC
        """).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["script"] = json.loads(item["script"]) if item["script"] else None
            except Exception:
                item["script"] = None
            out.append(item)
        return out

    def group_get(self, group_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM profile_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        members = conn.execute("""
            SELECT profile_name, position
              FROM profile_group_members
             WHERE group_id = ?
          ORDER BY position, profile_name
        """, (group_id,)).fetchall()
        item["members"] = [r["profile_name"] for r in members]
        try:
            item["script"] = json.loads(item["script"]) if item["script"] else None
        except Exception:
            item["script"] = None
        return item

    def group_create(self, name: str, description: str = None,
                     script: dict = None, max_parallel: int = None) -> int:
        conn = self._get_conn()
        script_json = json.dumps(script, ensure_ascii=False) if script else None
        cur = conn.execute("""
            INSERT INTO profile_groups(name, description, script, max_parallel)
            VALUES(?, ?, ?, ?)
        """, (name, description, script_json, max_parallel))
        conn.commit()
        return cur.lastrowid

    def group_update(self, group_id: int, **fields) -> None:
        allowed = {"name", "description", "script", "max_parallel"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates: return
        if "script" in updates and updates["script"] is not None \
                               and not isinstance(updates["script"], str):
            updates["script"] = json.dumps(updates["script"], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [group_id]
        conn = self._get_conn()
        conn.execute(
            f"UPDATE profile_groups SET {cols}, updated_at = datetime('now') "
            f"WHERE id = ?",
            vals,
        )
        conn.commit()

    def group_delete(self, group_id: int) -> None:
        conn = self._get_conn()
        conn.execute("DELETE FROM profile_groups WHERE id = ?", (group_id,))
        conn.commit()

    def group_set_members(self, group_id: int, profile_names: list[str]) -> None:
        """Replace the group's membership list wholesale."""
        conn = self._get_conn()
        conn.execute(
            "DELETE FROM profile_group_members WHERE group_id = ?", (group_id,)
        )
        for i, name in enumerate(profile_names):
            conn.execute("""
                INSERT INTO profile_group_members(group_id, profile_name, position)
                VALUES(?, ?, ?)
            """, (group_id, name, i))
        conn.execute(
            "UPDATE profile_groups SET updated_at = datetime('now') WHERE id = ?",
            (group_id,),
        )
        conn.commit()

    def group_add_member(self, group_id: int, profile_name: str) -> None:
        conn = self._get_conn()
        conn.execute("""
            INSERT OR IGNORE INTO profile_group_members(group_id, profile_name, position)
            VALUES(?, ?, (SELECT COALESCE(MAX(position), -1) + 1
                            FROM profile_group_members WHERE group_id = ?))
        """, (group_id, profile_name, group_id))
        conn.commit()

    def group_remove_member(self, group_id: int, profile_name: str) -> None:
        conn = self._get_conn()
        conn.execute("""
            DELETE FROM profile_group_members
             WHERE group_id = ? AND profile_name = ?
        """, (group_id, profile_name))
        conn.commit()

    # ──────────────────────────────────────────────────────────
    # DAILY STATS
    # ──────────────────────────────────────────────────────────

    def daily_stats(self, days: int = 14) -> list[dict]:
        """Статистика по дням for графика"""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = conn.execute("""
            SELECT
                substr(timestamp, 1, 10) as date,
                event_type,
                COUNT(*) as n
            FROM events
            WHERE timestamp >= ?
            GROUP BY date, event_type
            ORDER BY date
        """, (cutoff,)).fetchall()

        by_day = {}
        for r in rows:
            d = r["date"]
            if d not in by_day:
                by_day[d] = {"date": d, "searches": 0, "captchas": 0, "empty": 0, "blocks": 0}
            et = r["event_type"]
            if et == "search_ok":
                by_day[d]["searches"] = r["n"]
            elif et == "search_empty":
                by_day[d]["empty"] = r["n"]
            elif et == "captcha":
                by_day[d]["captchas"] = r["n"]
            elif et == "blocked":
                by_day[d]["blocks"] = r["n"]

        return list(by_day.values())

    # ──────────────────────────────────────────────────────────
    # МИГРАЦИЯ ИЗ СТАРЫХ ФАЙЛОВ
    # ──────────────────────────────────────────────────────────

    def migrate_from_files(self, verbose: bool = True):
        """Одноразовая migration из старых файлов in DB"""
        meta_key = "migrated_from_files"
        existing = self._get_conn().execute(
            "SELECT value FROM meta WHERE key = ?", (meta_key,)
        ).fetchone()
        if existing:
            if verbose:
                logging.info("[DB] Миграция already выполнена ранее")
            return

        imported = {
            "config":       0, "competitors": 0, "events":      0,
            "ips":          0, "selfchecks":  0, "fingerprints": 0,
        }

        # 1. config.yaml
        if os.path.exists("config.yaml"):
            try:
                import yaml
                with open("config.yaml", "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self.config_set_all(data)
                imported["config"] = 1
            except Exception as e:
                logging.warning(f"[DB] config.yaml migrate: {e}")

        # 2. competitor_urls.txt
        if os.path.exists("competitor_urls.txt"):
            try:
                with open("competitor_urls.txt", "r", encoding="utf-8") as f:
                    for line in f:
                        parts = line.strip().split("\t")
                        if len(parts) >= 4:
                            self._get_conn().execute("""
                                INSERT INTO competitors (run_id, timestamp, query, domain, google_click_url)
                                VALUES (NULL, ?, ?, ?, ?)
                            """, (parts[0], parts[1], parts[2], parts[3]))
                            imported["competitors"] += 1
            except Exception as e:
                logging.warning(f"[DB] competitor_urls.txt migrate: {e}")

        # 3. profiles/*/session_quality.json
        import glob
        for sq_file in glob.glob("profiles/*/session_quality.json"):
            try:
                profile_name = os.path.basename(os.path.dirname(sq_file))
                with open(sq_file, "r", encoding="utf-8") as f:
                    metrics = json.load(f)
                for m in metrics:
                    self._get_conn().execute("""
                        INSERT INTO events (run_id, profile_name, timestamp, event_type,
                                            query, details, duration_sec, results_count)
                        VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)
                    """, (profile_name, m.get("timestamp", ""), m.get("event", ""),
                          m.get("query"), m.get("details"),
                          m.get("duration_sec"), m.get("results_count")))
                    imported["events"] += 1
            except Exception as e:
                logging.warning(f"[DB] {sq_file} migrate: {e}")

        # 4. profiles/*/rotating_ips.json
        for ips_file in glob.glob("profiles/*/rotating_ips.json"):
            try:
                with open(ips_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for ip, s in data.get("ips", {}).items():
                    self._get_conn().execute("""
                        INSERT OR REPLACE INTO ip_history
                        (ip, first_seen, last_seen, total_uses, total_captchas,
                         consecutive_capchas, burned_at, country, city, org, asn)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (ip, s.get("first_seen", ""), s.get("last_seen", ""),
                          s.get("total_uses", 0), s.get("total_captchas", 0),
                          s.get("consecutive_capchas", 0), s.get("burned_at"),
                          s.get("country"), s.get("city"), s.get("org"), s.get("asn")))
                    imported["ips"] += 1
            except Exception as e:
                logging.warning(f"[DB] {ips_file} migrate: {e}")

        # 5. profiles/*/selfcheck.json (only afterдний)
        for sc_file in glob.glob("profiles/*/selfcheck.json"):
            try:
                profile_name = os.path.basename(os.path.dirname(sc_file))
                with open(sc_file, "r", encoding="utf-8") as f:
                    sc = json.load(f)
                self.selfcheck_save(
                    run_id=None, profile_name=profile_name,
                    passed=sc.get("passed", 0), total=sc.get("total", 0),
                    tests=sc.get("tests", {}),
                    actual=sc.get("actual_values"),
                    expected=sc.get("expected_values"),
                )
                imported["selfchecks"] += 1
            except Exception as e:
                logging.warning(f"[DB] {sc_file} migrate: {e}")

        # 6. profiles/*/payload_debug.json
        for fp_file in glob.glob("profiles/*/payload_debug.json"):
            try:
                profile_name = os.path.basename(os.path.dirname(fp_file))
                with open(fp_file, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                self.fingerprint_save(profile_name, payload)
                imported["fingerprints"] += 1
            except Exception as e:
                logging.warning(f"[DB] {fp_file} migrate: {e}")

        # Помечаем that migration сделана
        self._get_conn().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (meta_key, datetime.now().isoformat(timespec="seconds"))
        )

        if verbose:
            logging.info(f"[DB] Миграция завершена: {imported}")

    # ──────────────────────────────────────────────────────────
    # PROFILE METADATA HELPERS (used by dashboard create / edit)
    # ──────────────────────────────────────────────────────────

    def profile_get(self, name: str) -> dict | None:
        """Return profile metadata from the most recent fingerprint + events."""
        row = self._get_conn().execute(
            "SELECT profile_name, template_name, payload_json, timestamp "
            "FROM fingerprints WHERE profile_name = ? AND is_current = 1 "
            "LIMIT 1",
            (name,)
        ).fetchone()
        if not row:
            # Fall back to latest non-current row
            row = self._get_conn().execute(
                "SELECT profile_name, template_name, payload_json, timestamp "
                "FROM fingerprints WHERE profile_name = ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (name,)
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        return {
            "name":               row["profile_name"],
            "template_name":      row["template_name"],
            "preferred_language": (payload.get("languages") or {}).get("language"),
            "created_at":         row["timestamp"],
            "payload":            payload,
        }

    def profile_save(self, name: str, meta: dict):
        """Persist lightweight profile metadata as config_kv entries."""
        prefix = f"profile.{name}."
        for k, v in meta.items():
            self.config_set(prefix + k, v)

    def reset_profile_health(self, name: str):
        """
        Clear cached self-check health state so the next run re-evaluates it.
        Deletes all selfcheck rows for this profile.
        """
        self._get_conn().execute(
            "DELETE FROM selfchecks WHERE profile_name = ?", (name,)
        )
        self._get_conn().commit()

    def clear_profile_history(self, name: str,
                              scope: str = "events") -> dict:
        """
        Bulk-delete history data for a profile. `scope` selects what to clear:
          "events"     — events table only (searches, captchas, etc.)
          "runs"       — run records
          "logs"       — log lines
          "selfchecks" — self-check results
          "all"        — everything profile-related except the fingerprint itself
        Returns a dict with deletion counts.
        """
        c = self._get_conn()
        out = {}
        if scope in ("events", "all"):
            out["events"] = c.execute(
                "DELETE FROM events WHERE profile_name = ?", (name,)
            ).rowcount
        if scope in ("runs", "all"):
            out["runs"] = c.execute(
                "DELETE FROM runs WHERE profile_name = ?", (name,)
            ).rowcount
        if scope in ("logs", "all"):
            out["logs"] = c.execute(
                "DELETE FROM logs WHERE profile_name = ?", (name,)
            ).rowcount
        if scope in ("selfchecks", "all"):
            out["selfchecks"] = c.execute(
                "DELETE FROM selfchecks WHERE profile_name = ?", (name,)
            ).rowcount
        c.commit()
        return out

    def clear_all_runs(self, older_than_days: int = None) -> int:
        """Delete run records (optionally only older than N days)."""
        c = self._get_conn()
        if older_than_days is None:
            cur = c.execute("DELETE FROM runs")
        else:
            cutoff = (datetime.now() - timedelta(days=older_than_days))\
                .isoformat(timespec="seconds")
            cur = c.execute(
                "DELETE FROM runs WHERE started_at < ?", (cutoff,)
            )
        c.commit()
        return cur.rowcount


# ──────────────────────────────────────────────────────────────
# SINGLETON INSTANCE
# ──────────────────────────────────────────────────────────────

_db_instance: Optional[DB] = None

def get_db() -> DB:
    """Синглтон — один DB на everything onложение"""
    global _db_instance
    if _db_instance is None:
        _db_instance = DB()
    return _db_instance


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    db = DB()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"

    if cmd == "init":
        print("✓ БД инициализирована")

    elif cmd == "migrate":
        db.migrate_from_files(verbose=True)

    elif cmd == "info":
        print("\n── BASE INFO ──")
        print(f"DB path: {db.path}")
        conn = db._get_conn()
        for table in ("runs", "events", "competitors", "ip_history",
                      "fingerprints", "selfchecks", "config_kv", "logs"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:20} {n:>8}")

    elif cmd == "config":
        print(json.dumps(db.config_get_all(), indent=2, ensure_ascii=False))

    elif cmd == "competitors":
        for c in db.competitors_by_domain()[:30]:
            print(f"  {c['mentions']:>4}  {c['domain']}")

    elif cmd == "reset":
        if "--yes" in sys.argv:
            os.remove(db.path)
            print("✓ БД уyesлена")
        else:
            print("Добавь --yes for подтверждения")

    else:
        print("Команды: init | migrate | info | config | competitors | reset --yes")
