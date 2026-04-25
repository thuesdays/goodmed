"""
db.py — Центральная SQLite база Ghost Shell

Один file ghost_shell.db contains everything: config, launchи, события,
конкурентов, IP tracker, fingerprint history, logs, selfcheck.

Usage:
    from db import DB
    db = DB()                     # открывает/creates ghost_shell.db
    db.config_set("proxy.url", "user:pass@host:port")
    run_id = db.run_start("profile_01", proxy_url="...")
    db.event_record(run_id, "profile_01", "search_ok", query="test")
    db.run_finish(run_id, exit_code=0)
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

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
    proxy_url    TEXT,             -- DEPRECATED: kept for back-compat. New code uses proxy_id.
    proxy_is_rotating    INTEGER,  -- 1 / 0 / NULL = inherit global
    rotation_api_url     TEXT,     -- per-profile rotation endpoint
    rotation_provider    TEXT,
    rotation_api_key     TEXT,
    notes        TEXT,             -- free-form user notes
    -- FK to scripts.id. NULL means "use the default script" (the one
    -- flagged scripts.is_default=1). Not a hard FK because we want
    -- deleting a script to fall through to default gracefully.
    script_id    INTEGER,
    -- FK to proxies.id. NULL means "use the default proxy" (proxies
    -- row with is_default=1). Same graceful-degradation story as
    -- script_id — deleting a proxy doesn't orphan profiles, they
    -- just fall through to default at run time.
    proxy_id     INTEGER,
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

-- ───────────────────────────────────────────────────────────────
-- scripts — saved flow definitions, each with a human name and
-- description. Replaces the single `actions.flow` config entry.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scripts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    description  TEXT    NOT NULL DEFAULT '',
    flow         TEXT    NOT NULL DEFAULT '[]',    -- JSON-encoded list of steps
    is_default   INTEGER NOT NULL DEFAULT 0,       -- one script is the fallback
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ───────────────────────────────────────────────────────────────
-- proxies — library of reusable proxy configurations. Replaces
-- the single `proxy.url` + `proxy.pool_urls` config entries with
-- a named, per-profile-assignable entity.
--
-- Cached diagnostic fields (last_*) hold the results of the most
-- recent /api/proxies/<id>/test call so the UI can render an
-- ACTIVE/ERROR badge + flag/ISP/latency without re-probing on
-- every page load. They're advisory — runtime proxy routing
-- doesn't depend on them.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS proxies (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT,                         -- human label; NULL → host:port
    url              TEXT    NOT NULL UNIQUE,      -- full proxy URL
    type             TEXT,                         -- http/https/socks5/socks4
    host             TEXT,
    port             INTEGER,
    login            TEXT,
    password         TEXT,
    is_rotating      INTEGER NOT NULL DEFAULT 0,
    rotation_api_url TEXT,
    rotation_provider TEXT,                        -- none|asocks|brightdata|generic
    rotation_api_key TEXT,
    is_default       INTEGER NOT NULL DEFAULT 0,   -- fallback when profile.proxy_id NULL
    notes            TEXT,
    -- Cached diagnostics (from last test)
    last_exit_ip     TEXT,
    last_country     TEXT,
    last_country_code TEXT,
    last_city        TEXT,
    last_timezone    TEXT,
    last_asn         TEXT,
    last_provider    TEXT,
    last_ip_type     TEXT,                         -- residential|datacenter|mobile|unknown
    last_detection_risk TEXT,                      -- low|medium|high|unknown
    last_latency_ms  INTEGER,
    last_status      TEXT,                         -- ok|error|untested
    last_error       TEXT,
    last_checked_at  TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_proxies_updated ON proxies(updated_at);

-- ───────────────────────────────────────────────────────────────
-- warmup_runs — log of every warmup invocation. Warmup is a
-- per-profile action that launches a real browser, visits a
-- handful of realistic sites (Wikipedia, news, weather…), accepts
-- cookie banners, dwells, closes cleanly — so Google/Meta see a
-- profile with organic history before the first ad-monitoring run.
--
-- One row per warmup attempt. Not tied to runs table (warmup is
-- not a run).
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS warmup_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name     TEXT    NOT NULL,
    started_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at      TEXT,
    preset           TEXT,                         -- medical|tech|news|general|custom
    sites_planned    INTEGER NOT NULL DEFAULT 0,
    sites_visited    INTEGER NOT NULL DEFAULT 0,
    sites_succeeded  INTEGER NOT NULL DEFAULT 0,
    duration_sec     REAL,
    status           TEXT    NOT NULL DEFAULT 'running',   -- running|ok|partial|failed
    trigger          TEXT    NOT NULL DEFAULT 'manual',    -- manual|scheduled|auto_before_run
    notes            TEXT,                         -- freeform detail / error
    sites_log        TEXT                          -- JSON array of per-site records
);
CREATE INDEX IF NOT EXISTS idx_warmup_profile    ON warmup_runs(profile_name);
CREATE INDEX IF NOT EXISTS idx_warmup_started_at ON warmup_runs(started_at);

-- ───────────────────────────────────────────────────────────────
-- cookie_snapshots — the cookie pool. After a run completes
-- without captcha we freeze the current cookie + localStorage
-- state and store it here. If a later run hits trouble we can
-- restore the last clean snapshot (session resurrection).
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cookie_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name     TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    run_id           INTEGER,
    trigger          TEXT    NOT NULL DEFAULT 'manual',
    cookies_json     TEXT    NOT NULL DEFAULT '[]',
    storage_json     TEXT    NOT NULL DEFAULT '{}',
    cookie_count     INTEGER NOT NULL DEFAULT 0,
    domain_count     INTEGER NOT NULL DEFAULT 0,
    bytes            INTEGER NOT NULL DEFAULT 0,
    reason           TEXT
);
CREATE INDEX IF NOT EXISTS idx_snapshot_profile   ON cookie_snapshots(profile_name);
CREATE INDEX IF NOT EXISTS idx_snapshot_created   ON cookie_snapshots(created_at);

-- ───────────────────────────────────────────────────────────────
-- vault_items — generic credential & secret vault
--
-- Stores ANY sensitive material the user wants protected: social-media
-- accounts, email logins, crypto wallet seeds, API keys, TOTP seeds,
-- arbitrary custom notes. A single schema covers all of it — variations
-- live in the `kind` column and the shape of the encrypted JSON blob.
--
-- Sensitive fields live in `secrets_enc` as a Fernet-encrypted JSON
-- object. The exact keys depend on `kind`, e.g.:
--   account       : {"password": "...", "totp_secret": "..."}
--   crypto_wallet : {"seed_phrase": "...", "wallet_password": "...",
--                    "private_key": "..."}
--   api_key       : {"key": "...", "secret": "..."}
--   custom        : whatever the user provided
--
-- `identifier` is the non-secret human-readable key shown in the table
-- (an email address, a wallet public address, an API client_id, …).
-- Splitting it out lets list views render without unlocking the vault.
-- ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vault_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL,            -- user-friendly label
    kind             TEXT    NOT NULL DEFAULT 'account',
                               -- account | crypto_wallet | email | social
                               -- api_key | totp_only | note | custom
    service          TEXT,                        -- google | binance | twitter | aws | ...
    identifier       TEXT,                        -- login / email / address / client_id
    secrets_enc      TEXT,                        -- Fernet ciphertext of a JSON object
    profile_name     TEXT,                        -- optional FK to profiles(name)
    status           TEXT    NOT NULL DEFAULT 'active',
                               -- active | banned | locked | needs_review | disabled
    tags_json        TEXT,                        -- JSON array of user tags
    notes            TEXT,
    last_used_at     TEXT,
    last_login_at    TEXT,
    last_login_status TEXT,                       -- ok | failed | captcha | 2fa_required
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vault_kind    ON vault_items(kind);
CREATE INDEX IF NOT EXISTS idx_vault_service ON vault_items(service);
CREATE INDEX IF NOT EXISTS idx_vault_profile ON vault_items(profile_name);
CREATE INDEX IF NOT EXISTS idx_vault_status  ON vault_items(status);
"""


# Дефолтные values configа — используются on первом launch.
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
    # Rotate once every N runs instead of every run. Set to 1 for
    # "rotate every run" (old behavior), 10 for "rotate every 10th run".
    # Rationale: rotating on every run makes the profile look like a
    # different user each time, which erases "returning visitor" signal
    # Google uses as a trust marker. Rotating less often lets a good
    # IP ripen into a trusted one. Detection risk is managed at run
    # time (captcha triggers unconditional rotation regardless of this
    # setting). Default 10 = about 5-15 min of ripening per IP in a
    # typical scheduler cadence.
    "proxy.rotate_every_n_runs":  10,
    # Run fingerprint self-check + proxy diagnostics once every N runs
    # (plus always after rotation, plus always on run #1). Checks are
    # deterministic per-profile — running 34 JS probes every run burns
    # 1-2s for no diagnostic gain. Default 10 = once every ~2 hrs at
    # 10 min cadence, which is enough to catch drift without wasting
    # time.
    "browser.selfcheck_every_n_runs": 10,
    # On fresh-profile first launch, automatically import a slice of
    # the host's real Chrome history (if present) so the profile looks
    # like a casual user rather than a factory-fresh browser. Each
    # profile gets a DIFFERENT random slice (seeded by profile name)
    # to avoid all Ghost Shell profiles looking identical to Google.
    # Idempotent — a sentinel file marks profiles that already ran this.
    # Users can still run Warm up from real Chrome manually via the
    # dashboard to force a fresh import or change the dose.
    "browser.auto_enrich_from_host_chrome": True,
    "browser.auto_enrich_max_days": 30,
    "browser.auto_enrich_max_urls": 500,
    # Explicit source path override. Empty string → auto-discover via
    # chrome_importer.discover_source() (finds %LOCALAPPDATA%\Google
    # \Chrome\User Data\Default on Windows). Set this to point at a
    # specific Chrome profile — e.g. "Profile 2" instead of "Default",
    # or an archived profile folder.
    "browser.auto_enrich_source_path": "",

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

    # ── SERP engagement behavior (serp_behavior.py) ──────────────
    # Post-ads-collection human-shaped behavior on the SERP. Without
    # this the monitor looks like a scraper — land, grab, leave in
    # under 2 seconds — and Google downgrades ad load on subsequent
    # queries (fewer ads → lower hit rate). Each step is independently
    # toggleable; the organic-click step is probabilistic (expensive
    # signal, don't want to do it on every query).
    "behavior.serp_scroll_enabled":      True,
    "behavior.serp_dwell_enabled":       True,
    "behavior.organic_click_enabled":    True,
    # 25% of queries end with a real organic-result click. Higher is
    # more engagement signal but slower runs; lower is closer to pure
    # scraping. 20-40% matches observed CTRs for commercial queries.
    "behavior.organic_click_probability": 0.25,
    # Time spent on the clicked organic result (new tab dwell).
    # 8-25s brackets p10-p75 of real commercial-query dwell times.
    "behavior.organic_dwell_min_sec":    8,
    "behavior.organic_dwell_max_sec":    25,

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
    Потокоwithoutопасная обёртка above SQLite.
    Использует локальные коннекции на поток + WAL for конкурентного доступа.
    """

    _local = threading.local()

    def __init__(self, path: str = None):
        self.path = path or DB_PATH
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Per-thread SQLite connection. WAL mode + busy_timeout together
        let us safely have N writers from different threads/processes
        without sporadic 'database is locked' errors.

        Thread model:
          - Each thread gets its own connection via threading.local
          - WAL journal mode allows concurrent read+write without blocking
          - busy_timeout=5000ms makes SQLite retry internally before raising
            OperationalError when another writer holds the lock"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            # 5 seconds of internal retry when lock is held by another
            # writer. Covers the worst-case burst: dashboard stats query
            # + main.py heartbeat + traffic flush firing in the same
            # instant. Without this we'd get "database is locked" at
            # random and individual calls would fail — noisy and requires
            # caller-side retry for every method in the class.
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _init_schema(self):
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        # Ensure newer columns exist on old DBs — SQLite has no
        # "ADD COLUMN IF NOT EXISTS", so we check PRAGMA first.
        self._ensure_column(conn, "runs", "pid",          "INTEGER")
        self._ensure_column(conn, "runs", "heartbeat_at", "TEXT")
        self._ensure_column(conn, "profiles", "script_id", "INTEGER")

        # Fingerprint coherence system (added in v4):
        #   template_id      — canonical device template used
        #   coherence_score  — 0-100 from validator run at save time
        #   coherence_report — JSON breakdown of checks
        #   locked_fields    — JSON list of dot-paths user chose to preserve
        #                      across regenerations
        #   source           — 'generated' / 'manual_edit' / 'runtime_observed'
        #   reason           — free text explaining why this FP was saved
        self._ensure_column(conn, "fingerprints", "template_id",      "TEXT")
        self._ensure_column(conn, "fingerprints", "coherence_score",  "INTEGER")
        self._ensure_column(conn, "fingerprints", "coherence_report", "TEXT")
        self._ensure_column(conn, "fingerprints", "locked_fields",    "TEXT")
        self._ensure_column(conn, "fingerprints", "source",           "TEXT")
        self._ensure_column(conn, "fingerprints", "reason",           "TEXT")

        # ── Seed the scripts table ─────────────────────────────
        # On first init (table empty), migrate any existing unified
        # flow (`actions.flow` config key) into a "Default" script,
        # so users keep working behavior across the upgrade. If
        # there's no prior flow, create an empty Default anyway so
        # profiles always resolve to something.
        existing = conn.execute("SELECT COUNT(*) FROM scripts").fetchone()[0]
        if existing == 0:
            flow_row = conn.execute(
                "SELECT value FROM config_kv WHERE key = 'actions.flow'"
            ).fetchone()
            legacy_flow = []
            if flow_row:
                try:
                    legacy_flow = json.loads(flow_row["value"])
                    if not isinstance(legacy_flow, list):
                        legacy_flow = []
                except Exception:
                    legacy_flow = []
            desc = ("Migrated from legacy `actions.flow` during the "
                    "scripts-library upgrade."
                    if legacy_flow else
                    "Default empty flow — edit and save, or create new "
                    "scripts via the Scripts page.")
            conn.execute(
                "INSERT INTO scripts (name, description, flow, is_default) "
                "VALUES (?, ?, ?, 1)",
                ("Default", desc,
                 json.dumps(legacy_flow, ensure_ascii=False)),
            )
            logging.info(
                f"[DB] Seeded scripts table — Default script with "
                f"{len(legacy_flow)} top-level step(s)"
            )

        # ── Back-fill profile.script_id → Default ──────────────
        # Any profile that existed before this upgrade should point
        # at the default script so its runs still use the same flow
        # they did before.
        default_row = conn.execute(
            "SELECT id FROM scripts WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if default_row:
            default_id = default_row["id"]
            conn.execute(
                "UPDATE profiles SET script_id = ? WHERE script_id IS NULL",
                (default_id,),
            )

        # ── Migrate legacy proxy config → proxies table ────────
        # Seed the proxies table on first init. We pull in:
        #   1. `proxy.url`       → one default proxy (marked is_default=1)
        #   2. `proxy.pool_urls` → one proxy each (not default)
        #   3. profiles.proxy_url → one proxy per unique URL, attached
        #      back to that profile via proxy_id
        # Idempotent — subsequent boots skip if proxies already has rows.
        self._ensure_column(conn, "profiles", "proxy_id", "INTEGER")
        existing_proxies = conn.execute(
            "SELECT COUNT(*) FROM proxies"
        ).fetchone()[0]
        if existing_proxies == 0:
            from urllib.parse import urlparse

            def _migrate_url_to_proxy_row(url: str, is_default: bool = False,
                                          name: str = None,
                                          rotation_api_url: str = None,
                                          rotation_provider: str = None,
                                          rotation_api_key: str = None,
                                          is_rotating: bool = False):
                """Insert (or get) a proxy row for this URL. Returns id."""
                if not url:
                    return None
                url = url.strip()
                # Normalize — bare host:port gets http:// prefix so URL
                # uniqueness works consistently.
                if not url.startswith(("http://", "https://",
                                        "socks5://", "socks4://")):
                    url = f"http://{url}"
                # Dedup by URL
                existing = conn.execute(
                    "SELECT id FROM proxies WHERE url = ?", (url,)
                ).fetchone()
                if existing:
                    return existing["id"]
                try:
                    p = urlparse(url)
                    ptype = p.scheme or "http"
                    host = p.hostname
                    port = p.port
                    login = p.username
                    password = p.password
                except Exception:
                    ptype, host, port, login, password = "http", None, None, None, None
                display_name = name or (f"{host}:{port}" if host else url)
                cur = conn.execute("""
                    INSERT INTO proxies
                      (name, url, type, host, port, login, password,
                       is_rotating, rotation_api_url, rotation_provider,
                       rotation_api_key, is_default, last_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'untested')
                """, (display_name, url, ptype, host, port, login, password,
                      1 if is_rotating else 0,
                      rotation_api_url, rotation_provider, rotation_api_key,
                      1 if is_default else 0))
                return cur.lastrowid

            # 1) Global proxy.url → default proxy
            def _cfg_val(key):
                row = conn.execute(
                    "SELECT value FROM config_kv WHERE key = ?", (key,)
                ).fetchone()
                if not row:
                    return None
                try:
                    return json.loads(row["value"])
                except Exception:
                    return row["value"]

            global_url = _cfg_val("proxy.url")
            rot_api    = _cfg_val("proxy.rotation_api_url")
            rot_prov   = _cfg_val("proxy.rotation_provider")
            rot_key    = _cfg_val("proxy.rotation_api_key")
            is_rot     = bool(rot_api)   # heuristic: if rotation endpoint set, assume rotating

            default_proxy_id = None
            if global_url:
                default_proxy_id = _migrate_url_to_proxy_row(
                    global_url, is_default=True, name="Default",
                    rotation_api_url=rot_api, rotation_provider=rot_prov,
                    rotation_api_key=rot_key, is_rotating=is_rot,
                )

            # 2) pool URLs → non-default proxies
            pool = _cfg_val("proxy.pool_urls") or []
            if isinstance(pool, list):
                for i, u in enumerate(pool):
                    _migrate_url_to_proxy_row(u, name=f"Pool {i+1}")

            # 3) Per-profile proxy_url overrides → dedicated rows
            for pr_row in conn.execute(
                "SELECT name, proxy_url, proxy_is_rotating, rotation_api_url, "
                "rotation_provider, rotation_api_key "
                "FROM profiles WHERE proxy_url IS NOT NULL "
                "AND proxy_url != ''"
            ).fetchall():
                pid = _migrate_url_to_proxy_row(
                    pr_row["proxy_url"],
                    name=f"{pr_row['name']} (custom)",
                    rotation_api_url=pr_row["rotation_api_url"],
                    rotation_provider=pr_row["rotation_provider"],
                    rotation_api_key=pr_row["rotation_api_key"],
                    is_rotating=bool(pr_row["proxy_is_rotating"]),
                )
                if pid:
                    conn.execute(
                        "UPDATE profiles SET proxy_id = ? WHERE name = ?",
                        (pid, pr_row["name"]),
                    )

            logging.info(
                f"[DB] Seeded proxies table from legacy config "
                f"(default_id={default_proxy_id})"
            )

        # Back-fill profile.proxy_id → default proxy for profiles
        # that still have NULL (no per-profile override)
        default_proxy_row = conn.execute(
            "SELECT id FROM proxies WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if default_proxy_row:
            conn.execute(
                "UPDATE profiles SET proxy_id = ? WHERE proxy_id IS NULL",
                (default_proxy_row["id"],),
            )

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
        """Возвращает all config as влонный dict"""
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
        """Массовое update — рекурсивно разворачивает nested dict"""
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
    # SCRIPTS — saved flow library + per-profile assignment
    # ──────────────────────────────────────────────────────────
    # A "script" is a named, saved unified flow. Profiles reference
    # scripts via profiles.script_id. Exactly one script is flagged
    # is_default=1 and acts as the fallback for profiles that have
    # no assignment yet (or whose script_id no longer exists).

    def scripts_list(self) -> list[dict]:
        """All scripts with summary metadata + usage count."""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT s.id, s.name, s.description, s.is_default,
                   s.created_at, s.updated_at,
                   (SELECT COUNT(*) FROM profiles p WHERE p.script_id = s.id)
                       AS profile_count
              FROM scripts s
             ORDER BY s.is_default DESC, s.updated_at DESC
        """).fetchall()
        # Don't return the full flow JSON here — the list view only
        # needs summary counts. `script_get` returns the full flow.
        result = []
        for r in rows:
            d = dict(r)
            # Compute step count by parsing flow JSON on demand
            flow_row = conn.execute(
                "SELECT flow FROM scripts WHERE id = ?", (r["id"],)
            ).fetchone()
            try:
                flow = json.loads(flow_row["flow"] or "[]")
                d["step_count"] = len(flow) if isinstance(flow, list) else 0
            except Exception:
                d["step_count"] = 0
            result.append(d)
        return result

    def script_get(self, script_id: int) -> dict | None:
        """Fetch one script including its full flow JSON."""
        row = self._get_conn().execute(
            "SELECT * FROM scripts WHERE id = ?", (script_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["flow"] = json.loads(d["flow"] or "[]")
        except Exception:
            d["flow"] = []
        return d

    def script_get_by_name(self, name: str) -> dict | None:
        row = self._get_conn().execute(
            "SELECT id FROM scripts WHERE name = ?", (name,)
        ).fetchone()
        return self.script_get(row["id"]) if row else None

    def script_get_default(self) -> dict | None:
        """Return the default (fallback) script, creating an empty
        one if somehow none is flagged default — this keeps runtime
        resolution always returning something."""
        row = self._get_conn().execute(
            "SELECT id FROM scripts WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if row:
            return self.script_get(row["id"])
        # Nothing flagged default — promote the oldest script, or
        # create a new empty Default if scripts table is empty.
        any_row = self._get_conn().execute(
            "SELECT id FROM scripts ORDER BY id LIMIT 1"
        ).fetchone()
        if any_row:
            self._get_conn().execute(
                "UPDATE scripts SET is_default = 1 WHERE id = ?",
                (any_row["id"],),
            )
            return self.script_get(any_row["id"])
        # Totally empty — seed one
        new_id = self.script_create("Default",
            "Default empty flow — auto-created.", [], is_default=True)
        return self.script_get(new_id)

    def script_create(self, name: str, description: str = "",
                      flow: list = None, is_default: bool = False) -> int:
        """Insert a new script. Raises if name already exists (UNIQUE)."""
        flow_json = json.dumps(flow or [], ensure_ascii=False)
        conn = self._get_conn()
        with conn:
            if is_default:
                # Only one script may be default at a time
                conn.execute("UPDATE scripts SET is_default = 0")
            cur = conn.execute("""
                INSERT INTO scripts (name, description, flow, is_default)
                VALUES (?, ?, ?, ?)
            """, (name, description or "", flow_json,
                  1 if is_default else 0))
            return cur.lastrowid

    def script_update(self, script_id: int, *, name: str = None,
                      description: str = None, flow: list = None,
                      is_default: bool | None = None) -> bool:
        """Partial update. Returns True if a row was affected."""
        conn = self._get_conn()
        updates = []
        params = []
        if name is not None:
            updates.append("name = ?"); params.append(name)
        if description is not None:
            updates.append("description = ?"); params.append(description)
        if flow is not None:
            updates.append("flow = ?")
            params.append(json.dumps(flow, ensure_ascii=False))
        if is_default is True:
            # Mark this one as default, unmark all others
            with conn:
                conn.execute("UPDATE scripts SET is_default = 0")
            updates.append("is_default = 1")
        elif is_default is False:
            updates.append("is_default = 0")
        if not updates:
            return False
        updates.append("updated_at = datetime('now')")
        params.append(script_id)
        with conn:
            cur = conn.execute(
                f"UPDATE scripts SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            return cur.rowcount > 0

    def script_delete(self, script_id: int) -> bool:
        """Delete a script. Profiles referencing it fall back to default.
        Default script cannot be deleted — caller must promote another
        first.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT is_default FROM scripts WHERE id = ?", (script_id,)
        ).fetchone()
        if not row:
            return False
        if row["is_default"]:
            raise ValueError(
                "Cannot delete the default script — set another as "
                "default first."
            )
        with conn:
            # Profiles using this script get their assignment cleared,
            # falling back to default at runtime resolution.
            conn.execute(
                "UPDATE profiles SET script_id = NULL WHERE script_id = ?",
                (script_id,),
            )
            conn.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
        return True

    def script_assign_to_profile(self, profile_name: str,
                                 script_id: int | None) -> bool:
        """Assign a script to a profile (None = clear, use default)."""
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                "UPDATE profiles SET script_id = ?, "
                "updated_at = datetime('now') WHERE name = ?",
                (script_id, profile_name),
            )
            if cur.rowcount == 0:
                # Profile doesn't have a row yet — insert minimal
                conn.execute(
                    "INSERT INTO profiles (name, script_id) VALUES (?, ?)",
                    (profile_name, script_id),
                )
        return True

    def script_profiles(self, script_id: int) -> list[str]:
        """Profile names that have this script assigned."""
        rows = self._get_conn().execute(
            "SELECT name FROM profiles WHERE script_id = ? ORDER BY name",
            (script_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def script_resolve_for_profile(self, profile_name: str) -> dict | None:
        """Runtime: which script should this profile use?
        - If profile has script_id and it exists → that script
        - Otherwise → default script (always exists)
        Returns full script dict (with flow parsed)."""
        row = self._get_conn().execute(
            "SELECT script_id FROM profiles WHERE name = ?",
            (profile_name,),
        ).fetchone()
        script_id = row["script_id"] if row else None
        if script_id:
            sc = self.script_get(script_id)
            if sc:
                return sc
        # Fall back to default
        return self.script_get_default()

    # ──────────────────────────────────────────────────────────
    # PROXIES — saved proxy library + per-profile assignment
    # ──────────────────────────────────────────────────────────
    # Mirrors the scripts API shape so the UI can treat both as
    # assignable resources. One proxy may be flagged is_default=1
    # — that's the fallback for profiles.proxy_id IS NULL.

    def proxies_list(self) -> list[dict]:
        """All proxies with summary + profile count."""
        rows = self._get_conn().execute("""
            SELECT p.*,
                   (SELECT COUNT(*) FROM profiles pr WHERE pr.proxy_id = p.id)
                       AS profile_count
              FROM proxies p
             ORDER BY p.is_default DESC, p.updated_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

    def proxy_get(self, proxy_id: int) -> dict | None:
        row = self._get_conn().execute(
            "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
        ).fetchone()
        return dict(row) if row else None

    def proxy_get_by_url(self, url: str) -> dict | None:
        row = self._get_conn().execute(
            "SELECT * FROM proxies WHERE url = ?", (url,)
        ).fetchone()
        return dict(row) if row else None

    def proxy_get_default(self) -> dict | None:
        row = self._get_conn().execute(
            "SELECT * FROM proxies WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)
        # Promote oldest if none flagged default
        any_row = self._get_conn().execute(
            "SELECT * FROM proxies ORDER BY id LIMIT 1"
        ).fetchone()
        if any_row:
            self._get_conn().execute(
                "UPDATE proxies SET is_default = 1 WHERE id = ?",
                (any_row["id"],),
            )
            return dict(any_row)
        return None

    def proxy_create(self, *, url: str, name: str = None, type: str = None,
                     host: str = None, port: int = None,
                     login: str = None, password: str = None,
                     is_rotating: bool = False,
                     rotation_api_url: str = None,
                     rotation_provider: str = None,
                     rotation_api_key: str = None,
                     is_default: bool = False,
                     notes: str = None) -> int:
        """Insert a new proxy. Parses URL into host/port/login/password
        if those aren't provided."""
        if not url:
            raise ValueError("url is required")
        url = url.strip()
        if not url.startswith(("http://", "https://",
                                "socks5://", "socks4://")):
            url = f"http://{url}"

        # Auto-parse URL parts if caller didn't supply them
        if not (host and port):
            from urllib.parse import urlparse
            try:
                p = urlparse(url)
                type = type or p.scheme or "http"
                host = host or p.hostname
                port = port or p.port
                login = login or p.username
                password = password or p.password
            except Exception:
                pass

        display_name = name or (f"{host}:{port}" if host else url)

        conn = self._get_conn()
        with conn:
            if is_default:
                conn.execute("UPDATE proxies SET is_default = 0")
            cur = conn.execute("""
                INSERT INTO proxies
                  (name, url, type, host, port, login, password,
                   is_rotating, rotation_api_url, rotation_provider,
                   rotation_api_key, is_default, notes, last_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'untested')
            """, (display_name, url, type, host, port, login, password,
                  1 if is_rotating else 0,
                  rotation_api_url, rotation_provider, rotation_api_key,
                  1 if is_default else 0, notes))
            return cur.lastrowid

    def proxy_update(self, proxy_id: int, **fields) -> bool:
        """Partial update. Allowed fields: name, url, type, host, port,
        login, password, is_rotating, rotation_api_url, rotation_provider,
        rotation_api_key, is_default, notes."""
        if not fields:
            return False
        allowed = {"name", "url", "type", "host", "port", "login", "password",
                   "is_rotating", "rotation_api_url", "rotation_provider",
                   "rotation_api_key", "is_default", "notes"}
        updates = []
        params = []
        conn = self._get_conn()
        if fields.get("is_default") is True:
            with conn:
                conn.execute("UPDATE proxies SET is_default = 0")
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "is_rotating":
                v = 1 if v else 0
            if k == "is_default":
                v = 1 if v else 0
            updates.append(f"{k} = ?")
            params.append(v)
        if not updates:
            return False
        updates.append("updated_at = datetime('now')")
        params.append(proxy_id)
        with conn:
            cur = conn.execute(
                f"UPDATE proxies SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            return cur.rowcount > 0

    def proxy_delete(self, proxy_id: int) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT is_default FROM proxies WHERE id = ?", (proxy_id,)
        ).fetchone()
        if not row:
            return False
        if row["is_default"]:
            raise ValueError(
                "Cannot delete the default proxy — set another as "
                "default first."
            )
        with conn:
            # Profiles referencing this proxy fall back to default
            conn.execute(
                "UPDATE profiles SET proxy_id = NULL WHERE proxy_id = ?",
                (proxy_id,),
            )
            conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
        return True

    def proxy_record_diagnostics(self, proxy_id: int, diag: dict) -> None:
        """Write the result of a test_proxy() call into the cached
        diagnostic columns. Used by /api/proxies/<id>/test."""
        from datetime import datetime as _dt
        now = _dt.now().isoformat(timespec="seconds")
        self._get_conn().execute("""
            UPDATE proxies SET
                last_exit_ip = ?,
                last_country = ?, last_country_code = ?,
                last_city = ?, last_timezone = ?,
                last_asn = ?, last_provider = ?,
                last_ip_type = ?, last_detection_risk = ?,
                last_latency_ms = ?, last_status = ?,
                last_error = ?, last_checked_at = ?,
                updated_at = ?
            WHERE id = ?
        """, (
            diag.get("ip"),
            diag.get("country"), diag.get("country_code"),
            diag.get("city"), diag.get("timezone"),
            diag.get("asn"), diag.get("provider"),
            diag.get("ip_type"), diag.get("detection_risk"),
            diag.get("latency_ms"),
            "ok" if diag.get("ok") else "error",
            diag.get("error"),
            now, now, proxy_id,
        ))
        self._get_conn().commit()

    def proxy_assign_to_profile(self, profile_name: str,
                                proxy_id: int | None) -> bool:
        conn = self._get_conn()
        with conn:
            cur = conn.execute(
                "UPDATE profiles SET proxy_id = ?, "
                "updated_at = datetime('now') WHERE name = ?",
                (proxy_id, profile_name),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO profiles (name, proxy_id) VALUES (?, ?)",
                    (profile_name, proxy_id),
                )
        return True

    def proxy_profiles(self, proxy_id: int) -> list[str]:
        rows = self._get_conn().execute(
            "SELECT name FROM profiles WHERE proxy_id = ? ORDER BY name",
            (proxy_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def proxy_resolve_for_profile(self, profile_name: str) -> dict | None:
        """Runtime: which proxy does this profile use?
        Priority: profile.proxy_id → default proxy → None."""
        row = self._get_conn().execute(
            "SELECT proxy_id FROM profiles WHERE name = ?",
            (profile_name,),
        ).fetchone()
        proxy_id = row["proxy_id"] if row else None
        if proxy_id:
            p = self.proxy_get(proxy_id)
            if p:
                return p
        return self.proxy_get_default()


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

    def runs_count_for_profile(self, profile_name: str) -> int:
        """Total number of runs ever recorded for a profile — used by
        main.py to decide whether the current run should rotate IP,
        given a "rotate every N runs" throttle. Cheap query (indexed
        on profile_name).

        Returns the count INCLUDING the currently-starting run, so on
        a profile's very first run this returns 1."""
        row = self._get_conn().execute(
            "SELECT COUNT(*) AS n FROM runs WHERE profile_name = ?",
            (profile_name,)
        ).fetchone()
        return row["n"] if row else 0

    # ──────────────────────────────────────────────────────────
    # EVENTS
    # ──────────────────────────────────────────────────────────

    def runs_totals(self, hours: int = None) -> dict:
        """Aggregate counters from the runs table itself. This is the
        AUTHORITATIVE source for headline stats (total searches, total
        ads, captchas) — the events table is optional telemetry that
        may or may not be populated by session_quality, so relying on
        it made the Overview look stuck at 3/3/0 even after many real
        runs completed.

        `runs.total_queries` / `runs.total_ads` / `runs.captchas` are
        written by run_finish() at the end of every run, so they always
        match reality.

        `hours=None` = all-time. Pass an int for a rolling window."""
        conn = self._get_conn()
        where, params = [], []
        if hours is not None:
            cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
            where.append("started_at >= ?")
            params.append(cutoff)
        sql = "SELECT COUNT(*) AS n, " \
              "COALESCE(SUM(total_queries), 0) AS queries, " \
              "COALESCE(SUM(total_ads), 0)     AS ads, " \
              "COALESCE(SUM(captchas), 0)      AS captchas, " \
              "SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) AS completed, " \
              "SUM(CASE WHEN exit_code IS NOT NULL AND exit_code != 0 THEN 1 ELSE 0 END) AS failed " \
              "FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = conn.execute(sql, params).fetchone()
        return {
            "runs":      row["n"] or 0,
            "searches":  row["queries"] or 0,
            "ads":       row["ads"] or 0,
            "captchas":  row["captchas"] or 0,
            "completed": row["completed"] or 0,
            "failed":    row["failed"] or 0,
        }

    def active_profiles_count(self, days: int = 7) -> int:
        """Distinct profiles that had at least one run in the last N days.
        'Active profiles' on Overview was showing 1 regardless of how
        many profiles actually ran; this makes the count honest."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = self._get_conn().execute(
            "SELECT COUNT(DISTINCT profile_name) AS n FROM runs WHERE started_at >= ?",
            (cutoff,)
        ).fetchone()
        return row["n"] if row else 0

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
        Returns {total_bytes, total_requests, profile_count, domain_count}.
        Timeseries is a separate method — call traffic_timeseries for
        per-bucket data. This used to also return `by_hour` but nothing
        in the frontend read it (wasted DB work).
        """
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

        return {
            "total_bytes":    totals["total_bytes"] or 0,
            "total_requests": totals["total_requests"] or 0,
            "profile_count":  totals["profile_count"] or 0,
            "domain_count":   totals["domain_count"] or 0,
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

    def competitors_by_domain(self, days: int = None,
                              search: str = None) -> list[dict]:
        """Aggregated competitor stats, one row per domain.

        Filters:
          days   — only competitor rows seen in the last N days (None = all time)
          search — substring match against domain or title (None = no filter)
        """
        where = []
        params: list = []
        if days and days > 0:
            where.append("timestamp >= datetime('now', ?)")
            params.append(f"-{int(days)} days")
        if search:
            where.append("(LOWER(domain) LIKE ? OR LOWER(COALESCE(title,'')) LIKE ?)")
            needle = f"%{search.lower()}%"
            params.extend([needle, needle])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        rows = self._get_conn().execute(f"""
            SELECT
                domain,
                COUNT(*) AS mentions,
                MIN(timestamp) AS first_seen,
                MAX(timestamp) AS last_seen,
                GROUP_CONCAT(DISTINCT query) AS queries
            FROM competitors{where_sql}
            GROUP BY domain
            ORDER BY mentions DESC
        """, tuple(params)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["queries"] = sorted(set((d["queries"] or "").split(","))) if d["queries"] else []
            result.append(d)
        return result

    def competitors_recent(self, limit: int = 100,
                           days: int = None,
                           search: str = None) -> list[dict]:
        where, params = [], []
        if days and days > 0:
            where.append("timestamp >= datetime('now', ?)")
            params.append(f"-{int(days)} days")
        if search:
            where.append("(LOWER(domain) LIKE ? OR LOWER(COALESCE(title,'')) LIKE ? OR LOWER(query) LIKE ?)")
            needle = f"%{search.lower()}%"
            params.extend([needle, needle, needle])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        rows = self._get_conn().execute(
            f"SELECT * FROM competitors{where_sql} ORDER BY timestamp DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    def competitors_count(self, days: int = None) -> tuple[int, int]:
        """(total_records, unique_domains) — optionally within last N days."""
        where = "WHERE timestamp >= datetime('now', ?)" if days else ""
        params = (f"-{int(days)} days",) if days else ()
        row = self._get_conn().execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT domain) AS d FROM competitors {where}",
            params,
        ).fetchone()
        return row["n"], row["d"]

    # ─── Analytics helpers (Phase: competitors redesign) ──────

    def competitors_trend(self, days: int = 7, top_n: int = 10) -> dict:
        """Daily mention counts for the top N domains over the last `days`.

        Returns {
          "dates":   ["2026-04-18", ..., "2026-04-24"],
          "series":  [{"domain": "x.com", "total": 42,
                       "counts": [0, 3, 5, 7, 10, 9, 8]}, ...]
        }

        Two queries — one to pick top N by total mentions in the window,
        one to pull daily buckets. Join in Python; SQL-side cross-join
        for an N×D grid would require sqlite-specific gymnastics.
        """
        days = max(1, int(days))
        top_n = max(1, int(top_n))
        conn = self._get_conn()

        # 1. Top domains in window
        top = conn.execute(f"""
            SELECT domain, COUNT(*) AS total
            FROM competitors
            WHERE timestamp >= datetime('now', ?)
            GROUP BY domain
            ORDER BY total DESC
            LIMIT ?
        """, (f"-{days} days", top_n)).fetchall()
        if not top:
            return {"dates": [], "series": []}
        top_domains = [r["domain"] for r in top]

        # 2. Daily grouping across those domains
        placeholders = ",".join("?" * len(top_domains))
        rows = conn.execute(f"""
            SELECT
              DATE(timestamp) AS d,
              domain,
              COUNT(*)        AS n
            FROM competitors
            WHERE timestamp >= datetime('now', ?)
              AND domain IN ({placeholders})
            GROUP BY d, domain
        """, (f"-{days} days", *top_domains)).fetchall()

        # Build the date axis (oldest → newest)
        from datetime import datetime as _dt, timedelta as _td
        today = _dt.now().date()
        date_axis = [(today - _td(days=days - 1 - i)).isoformat() for i in range(days)]

        # Rebuild sparse grid → dense per-domain series
        per_domain: dict[str, dict[str, int]] = {dom: {} for dom in top_domains}
        for r in rows:
            per_domain[r["domain"]][r["d"]] = r["n"]

        series = []
        for dom_row in top:
            d = dom_row["domain"]
            series.append({
                "domain": d,
                "total":  dom_row["total"],
                "counts": [per_domain[d].get(date, 0) for date in date_axis],
            })
        return {"dates": date_axis, "series": series}

    def competitors_sparklines(self, days: int = 7) -> dict:
        """Daily counts for ALL domains in one query — powers per-row
        mini-sparklines. Returns { domain: [counts by day] }.

        One query instead of N per-domain queries. Cheaper than calling
        competitors_trend() per row.
        """
        days = max(1, int(days))
        rows = self._get_conn().execute("""
            SELECT DATE(timestamp) AS d, domain, COUNT(*) AS n
            FROM competitors
            WHERE timestamp >= datetime('now', ?)
            GROUP BY d, domain
        """, (f"-{days} days",)).fetchall()

        from datetime import datetime as _dt, timedelta as _td
        today = _dt.now().date()
        date_axis = [(today - _td(days=days - 1 - i)).isoformat() for i in range(days)]

        by_domain: dict[str, dict[str, int]] = {}
        for r in rows:
            by_domain.setdefault(r["domain"], {})[r["d"]] = r["n"]
        return {d: [by_domain[d].get(date, 0) for date in date_axis]
                for d in by_domain}

    def competitor_detail(self, domain: str, days: int = 30) -> dict:
        """Per-domain drill-down — titles, display URLs, daily counts.

        Used by the expandable row in the UI.
        """
        days = max(1, int(days))
        conn = self._get_conn()

        titles = conn.execute("""
            SELECT title, COUNT(*) AS n, MAX(timestamp) AS last_seen
            FROM competitors
            WHERE domain = ? AND timestamp >= datetime('now', ?)
              AND title IS NOT NULL AND title != ''
            GROUP BY title
            ORDER BY n DESC
            LIMIT 8
        """, (domain, f"-{days} days")).fetchall()

        urls = conn.execute("""
            SELECT display_url, COUNT(*) AS n
            FROM competitors
            WHERE domain = ? AND timestamp >= datetime('now', ?)
              AND display_url IS NOT NULL AND display_url != ''
            GROUP BY display_url
            ORDER BY n DESC
            LIMIT 6
        """, (domain, f"-{days} days")).fetchall()

        queries = conn.execute("""
            SELECT query, COUNT(*) AS n
            FROM competitors
            WHERE domain = ? AND timestamp >= datetime('now', ?)
            GROUP BY query
            ORDER BY n DESC
        """, (domain, f"-{days} days")).fetchall()

        return {
            "domain":   domain,
            "titles":   [dict(r) for r in titles],
            "urls":     [dict(r) for r in urls],
            "queries":  [dict(r) for r in queries],
        }

    def competitors_by_query(self, days: int = 30,
                             per_query_top: int = 5) -> list[dict]:
        """Share-of-voice view: for each query, top advertisers seen.

        Returns list of {query, total, competitors: [{domain, mentions, pct}]}
        """
        days = max(1, int(days))
        rows = self._get_conn().execute("""
            SELECT query, domain, COUNT(*) AS mentions
            FROM competitors
            WHERE timestamp >= datetime('now', ?)
            GROUP BY query, domain
            ORDER BY query, mentions DESC
        """, (f"-{days} days",)).fetchall()

        by_query: dict[str, list] = {}
        for r in rows:
            by_query.setdefault(r["query"], []).append(
                {"domain": r["domain"], "mentions": r["mentions"]}
            )
        result = []
        for q, comps in by_query.items():
            total = sum(c["mentions"] for c in comps)
            top = comps[:per_query_top]
            for c in top:
                c["pct"] = round(100 * c["mentions"] / total, 1) if total else 0
            result.append({"query": q, "total": total, "competitors": top})
        # Sort queries by total mentions desc so busiest queries float up
        result.sort(key=lambda x: -x["total"])
        return result

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
    #
    # Each save creates a new row (immutable history). is_current=1
    # marks the active one. When saving a new fingerprint we first
    # clear is_current on all previous rows for this profile — so
    # there's always exactly one current fingerprint per profile.
    #
    # The `source` column distinguishes:
    #   'generated'        — from templates, high coherence by construction
    #   'manual_edit'      — user edited fields in dashboard UI
    #   'runtime_observed' — scan of real browser, used for drift detection
    #
    # `coherence_score` + `coherence_report` store the validator result
    # at save time. Cached so the UI doesn't re-run validation on every
    # page load — but we DO re-validate when the user edits fields.

    def fingerprint_save(self, profile_name: str, payload: dict,
                         *, coherence_score: int = None,
                         coherence_report: dict = None,
                         locked_fields: list = None,
                         source: str = "generated",
                         reason: str = None) -> int:
        """Save a new fingerprint snapshot. Previous ones stay in the
        table as history; only is_current flag moves."""
        conn = self._get_conn()
        with conn:
            # Demote previous current
            conn.execute(
                "UPDATE fingerprints SET is_current = 0 WHERE profile_name = ?",
                (profile_name,),
            )
            cur = conn.execute("""
                INSERT INTO fingerprints (
                    profile_name, timestamp, template_name, template_id,
                    payload_json, is_current,
                    coherence_score, coherence_report,
                    locked_fields, source, reason
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """, (
                profile_name,
                datetime.now().isoformat(timespec="seconds"),
                payload.get("template_label") or payload.get("template_name"),
                payload.get("template_id"),
                json.dumps(payload, ensure_ascii=False),
                coherence_score,
                json.dumps(coherence_report, ensure_ascii=False) if coherence_report else None,
                json.dumps(locked_fields, ensure_ascii=False) if locked_fields else None,
                source,
                reason,
            ))
            return cur.lastrowid

    def fingerprint_current(self, profile_name: str) -> Optional[dict]:
        """Get the currently-active fingerprint for a profile."""
        row = self._get_conn().execute("""
            SELECT * FROM fingerprints
            WHERE profile_name = ? AND is_current = 1
            ORDER BY timestamp DESC LIMIT 1
        """, (profile_name,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        if d.get("coherence_report"):
            try:
                d["coherence_report"] = json.loads(d["coherence_report"])
            except Exception:
                d["coherence_report"] = None
        if d.get("locked_fields"):
            try:
                d["locked_fields"] = json.loads(d["locked_fields"])
            except Exception:
                d["locked_fields"] = []
        return d

    def fingerprint_get(self, fingerprint_id: int) -> Optional[dict]:
        """Get a specific fingerprint by id (for history restoration)."""
        row = self._get_conn().execute(
            "SELECT * FROM fingerprints WHERE id = ?", (fingerprint_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        if d.get("coherence_report"):
            try:
                d["coherence_report"] = json.loads(d["coherence_report"])
            except Exception:
                d["coherence_report"] = None
        if d.get("locked_fields"):
            try:
                d["locked_fields"] = json.loads(d["locked_fields"])
            except Exception:
                d["locked_fields"] = []
        return d

    def fingerprints_history(self, profile_name: str, limit: int = 20) -> list[dict]:
        """List past fingerprints for a profile (newest first)."""
        rows = self._get_conn().execute("""
            SELECT id, timestamp, template_name, template_id,
                   is_current, coherence_score, source, reason
            FROM fingerprints WHERE profile_name = ?
            ORDER BY timestamp DESC LIMIT ?
        """, (profile_name, limit)).fetchall()
        return [dict(r) for r in rows]

    def fingerprint_activate(self, fingerprint_id: int) -> bool:
        """Mark a historical fingerprint as the current one.
        Used for 'restore to this version'."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT profile_name FROM fingerprints WHERE id = ?",
            (fingerprint_id,),
        ).fetchone()
        if not row:
            return False
        with conn:
            conn.execute(
                "UPDATE fingerprints SET is_current = 0 WHERE profile_name = ?",
                (row["profile_name"],),
            )
            conn.execute(
                "UPDATE fingerprints SET is_current = 1 WHERE id = ?",
                (fingerprint_id,),
            )
        return True

    def fingerprint_delete(self, fingerprint_id: int) -> bool:
        """Delete a historical fingerprint (can't delete current)."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT is_current FROM fingerprints WHERE id = ?",
            (fingerprint_id,),
        ).fetchone()
        if not row:
            return False
        if row["is_current"]:
            raise ValueError("Cannot delete the current fingerprint — "
                             "restore or generate another first.")
        with conn:
            conn.execute("DELETE FROM fingerprints WHERE id = ?",
                         (fingerprint_id,))
        return True

    def fingerprints_aggregate(self) -> list[dict]:
        """Per-profile summary: current score, template, last regen,
        # of historical snapshots. For Overview 'fingerprint health'
        widget."""
        rows = self._get_conn().execute("""
            SELECT
                p.name AS profile_name,
                fp.template_name,
                fp.template_id,
                fp.coherence_score,
                fp.timestamp AS current_ts,
                fp.source AS current_source,
                (SELECT COUNT(*) FROM fingerprints fh
                 WHERE fh.profile_name = p.name) AS history_count
            FROM profiles p
            LEFT JOIN fingerprints fp
                ON fp.profile_name = p.name AND fp.is_current = 1
            ORDER BY p.name
        """).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────
    # WARMUP RUNS
    # ──────────────────────────────────────────────────────────

    def warmup_start(self, profile_name: str, preset: str,
                     sites_planned: int, trigger: str = "manual") -> int:
        """Create a new warmup_runs row in 'running' state and return its id.
        The caller updates it via warmup_finish() once the browser closes."""
        conn = self._get_conn()
        with conn:
            cur = conn.execute("""
                INSERT INTO warmup_runs (
                    profile_name, started_at, preset, sites_planned,
                    trigger, status
                ) VALUES (?, ?, ?, ?, ?, 'running')
            """, (
                profile_name,
                datetime.now().isoformat(timespec="seconds"),
                preset, sites_planned, trigger,
            ))
            return cur.lastrowid

    def warmup_finish(self, warmup_id: int, *,
                      status: str,
                      sites_visited: int, sites_succeeded: int,
                      duration_sec: float,
                      notes: str = None,
                      sites_log: list = None) -> None:
        conn = self._get_conn()
        with conn:
            conn.execute("""
                UPDATE warmup_runs SET
                    finished_at     = ?,
                    status          = ?,
                    sites_visited   = ?,
                    sites_succeeded = ?,
                    duration_sec    = ?,
                    notes           = ?,
                    sites_log       = ?
                WHERE id = ?
            """, (
                datetime.now().isoformat(timespec="seconds"),
                status, sites_visited, sites_succeeded, duration_sec,
                notes,
                json.dumps(sites_log or [], ensure_ascii=False),
                warmup_id,
            ))

    def warmup_last(self, profile_name: str) -> Optional[dict]:
        """Most recent warmup row for a profile (any status)."""
        row = self._get_conn().execute("""
            SELECT * FROM warmup_runs
            WHERE profile_name = ?
            ORDER BY started_at DESC LIMIT 1
        """, (profile_name,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("sites_log"):
            try: d["sites_log"] = json.loads(d["sites_log"])
            except Exception: d["sites_log"] = []
        return d

    def warmup_history(self, profile_name: str, limit: int = 30) -> list[dict]:
        rows = self._get_conn().execute("""
            SELECT id, started_at, finished_at, preset, sites_planned,
                   sites_visited, sites_succeeded, duration_sec,
                   status, trigger, notes
            FROM warmup_runs
            WHERE profile_name = ?
            ORDER BY started_at DESC LIMIT ?
        """, (profile_name, limit)).fetchall()
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────
    # COOKIE SNAPSHOTS (the pool)
    # ──────────────────────────────────────────────────────────

    def snapshot_save(self, profile_name: str, cookies: list, storage: dict,
                      *, run_id: int = None, trigger: str = "manual",
                      reason: str = None) -> int:
        """Freeze the current cookie + storage state. Returns new row id."""
        cookies_json = json.dumps(cookies or [], ensure_ascii=False)
        storage_json = json.dumps(storage or {}, ensure_ascii=False)
        domains = {c.get("domain") for c in (cookies or []) if c.get("domain")}
        size = len(cookies_json) + len(storage_json)
        conn = self._get_conn()
        with conn:
            cur = conn.execute("""
                INSERT INTO cookie_snapshots (
                    profile_name, run_id, trigger, cookies_json, storage_json,
                    cookie_count, domain_count, bytes, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                profile_name, run_id, trigger, cookies_json, storage_json,
                len(cookies or []), len(domains), size, reason,
            ))
            return cur.lastrowid

    def snapshot_list(self, profile_name: str, limit: int = 50) -> list[dict]:
        """List snapshots for a profile (summary — no full cookie JSON)."""
        rows = self._get_conn().execute("""
            SELECT id, profile_name, created_at, run_id, trigger,
                   cookie_count, domain_count, bytes, reason
            FROM cookie_snapshots
            WHERE profile_name = ?
            ORDER BY created_at DESC LIMIT ?
        """, (profile_name, limit)).fetchall()
        return [dict(r) for r in rows]

    def snapshot_get(self, snapshot_id: int) -> Optional[dict]:
        """Full snapshot row INCLUDING cookies_json / storage_json parsed."""
        row = self._get_conn().execute(
            "SELECT * FROM cookie_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("cookies_json", "storage_json"):
            try: d[k.replace("_json", "")] = json.loads(d.pop(k))
            except Exception: d[k.replace("_json", "")] = [] if k == "cookies_json" else {}
        return d

    def snapshot_latest_clean(self, profile_name: str) -> Optional[dict]:
        """Most recent snapshot created by a clean run (auto_clean_run trigger)."""
        row = self._get_conn().execute("""
            SELECT id FROM cookie_snapshots
            WHERE profile_name = ? AND trigger = 'auto_clean_run'
            ORDER BY created_at DESC LIMIT 1
        """, (profile_name,)).fetchone()
        return self.snapshot_get(row["id"]) if row else None

    def snapshot_delete(self, snapshot_id: int) -> bool:
        conn = self._get_conn()
        with conn:
            cur = conn.execute("DELETE FROM cookie_snapshots WHERE id = ?",
                               (snapshot_id,))
            return cur.rowcount > 0

    def snapshot_stats(self, profile_name: str) -> dict:
        """Aggregate for the Session page header strip."""
        r = self._get_conn().execute("""
            SELECT
                COUNT(*) AS n,
                SUM(cookie_count) AS total_cookies,
                SUM(bytes) AS total_bytes,
                MAX(created_at) AS last_at
            FROM cookie_snapshots WHERE profile_name = ?
        """, (profile_name,)).fetchone()
        d = dict(r) if r else {}
        return {
            "n":             d.get("n") or 0,
            "total_cookies": d.get("total_cookies") or 0,
            "total_bytes":   d.get("total_bytes") or 0,
            "last_at":       d.get("last_at"),
        }

    # ──────────────────────────────────────────────────────────
    # VAULT ITEMS — credential / wallet / secret vault records
    # ──────────────────────────────────────────────────────────
    # The DB never decrypts — it stores and returns ciphertext. The
    # ghost_shell.accounts.manager module is the layer that calls vault.

    def vault_add(self, *, name: str, kind: str = "account",
                  service: str = None, identifier: str = None,
                  secrets_enc: str = None,
                  profile_name: str = None, status: str = "active",
                  tags: list = None, notes: str = None) -> int:
        tags_json = json.dumps(tags) if tags else None
        conn = self._get_conn()
        with conn:
            cur = conn.execute("""
                INSERT INTO vault_items (
                    name, kind, service, identifier, secrets_enc,
                    profile_name, status, tags_json, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, kind, service, identifier, secrets_enc,
                  profile_name, status, tags_json, notes))
            return cur.lastrowid

    def vault_update(self, item_id: int, **fields) -> bool:
        allowed = {"name", "kind", "service", "identifier", "secrets_enc",
                   "profile_name", "status", "notes",
                   "last_login_at", "last_login_status", "last_used_at"}
        if "tags" in fields:
            fields["tags_json"] = json.dumps(fields.pop("tags")) if fields.get("tags") is not None else None
            allowed.add("tags_json")
        sets = [f"{k} = ?" for k in fields if k in allowed]
        vals = [fields[k] for k in fields if k in allowed]
        if not sets:
            return False
        sets.append("updated_at = datetime('now')")
        sql = f"UPDATE vault_items SET {', '.join(sets)} WHERE id = ?"
        conn = self._get_conn()
        with conn:
            cur = conn.execute(sql, (*vals, item_id))
            return cur.rowcount > 0

    def vault_list(self, kind: str = None, service: str = None,
                   status: str = None, profile_name: str = None,
                   search: str = None) -> list[dict]:
        """Metadata-only listing — ciphertext is NOT included.
        Use vault_get() for a single record with ciphertext blob."""
        where, params = [], []
        if kind:         where.append("kind = ?");         params.append(kind)
        if service:      where.append("service = ?");      params.append(service)
        if status:       where.append("status = ?");       params.append(status)
        if profile_name: where.append("profile_name = ?"); params.append(profile_name)
        if search:
            needle = f"%{search.lower()}%"
            where.append("(LOWER(name) LIKE ? OR LOWER(COALESCE(identifier,'')) LIKE ? "
                         "OR LOWER(COALESCE(service,'')) LIKE ? "
                         "OR LOWER(COALESCE(tags_json,'')) LIKE ?)")
            params.extend([needle, needle, needle, needle])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._get_conn().execute(f"""
            SELECT
              id, name, kind, service, identifier, profile_name, status,
              tags_json, notes,
              last_login_at, last_login_status, last_used_at,
              created_at, updated_at,
              CASE WHEN secrets_enc IS NOT NULL THEN 1 ELSE 0 END AS has_secrets
            FROM vault_items{where_sql}
            ORDER BY kind, service, name
        """, tuple(params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["tags"] = json.loads(d.pop("tags_json") or "[]")
            except Exception:
                d["tags"] = []
            out.append(d)
        return out

    def vault_get(self, item_id: int) -> Optional[dict]:
        """Full row INCLUDING ciphertext. Caller needs the vault to decrypt."""
        row = self._get_conn().execute(
            "SELECT * FROM vault_items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["tags"] = json.loads(d.pop("tags_json") or "[]")
        except Exception:
            d["tags"] = []
        return d

    def vault_delete(self, item_id: int) -> bool:
        conn = self._get_conn()
        with conn:
            cur = conn.execute("DELETE FROM vault_items WHERE id = ?", (item_id,))
            return cur.rowcount > 0

    def vault_set_status(self, item_id: int, status: str,
                         login_status: str = None) -> bool:
        fields = {"status": status,
                  "last_used_at": datetime.now().isoformat(timespec="seconds")}
        if login_status:
            fields["last_login_status"] = login_status
            fields["last_login_at"]     = fields["last_used_at"]
        return self.vault_update(item_id, **fields)

    def vault_count_by_kind(self) -> dict:
        rows = self._get_conn().execute("""
            SELECT kind, COUNT(*) AS n FROM vault_items GROUP BY kind
        """).fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def vault_count_by_status(self) -> dict:
        rows = self._get_conn().execute("""
            SELECT status, COUNT(*) AS n FROM vault_items GROUP BY status
        """).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # Alias the new generic vault_* methods under the old account_*
    # names so scripts that already imported the account layer keep
    # working. Remove once all callers move to the new names.
    account_add          = vault_add
    account_update       = vault_update
    account_list         = vault_list
    account_get          = vault_get
    account_delete       = vault_delete
    account_set_status   = vault_set_status

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

        # Ротация — if больше MAX_LOGS, удаляем old
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
        """Return the list of "alive" profiles with their 24h stats.

        A profile is considered ALIVE if ANY of the following is true:
          - Its folder exists under profiles/
          - It has a row in the `profiles` table (per-profile metadata)
          - It has a fingerprint row
          - There's a default config value pointing at it

        A profile that exists ONLY in run history (no folder, no meta,
        no fingerprint) is a TOMBSTONE — it was previously deleted via
        api_profile_delete but we kept its runs for historical stats.
        Tombstones are filtered out here so the Profiles page and
        dropdown don't resurrect them.
        """
        conn = self._get_conn()

        # Alive sources
        alive_names = set()

        # 1. profiles-table rows
        for r in conn.execute("SELECT name FROM profiles").fetchall():
            alive_names.add(r["name"])

        # 2. fingerprints table
        for r in conn.execute(
                "SELECT DISTINCT profile_name FROM fingerprints").fetchall():
            alive_names.add(r["profile_name"])

        # 3. folders on disk
        if os.path.exists("profiles"):
            for name in os.listdir("profiles"):
                if os.path.isdir(os.path.join("profiles", name)):
                    alive_names.add(name)

        # 4. current active profile (config-declared)
        default = self.config_get("browser.profile_name", "profile_01")
        if default:
            alive_names.add(default)

        profiles = sorted(alive_names)

        # If nothing was found at all, still surface the default so the
        # UI has something to render on a fresh install.
        if not profiles:
            profiles = [default or "profile_01"]

        result = []
        for name in profiles:
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
        """Per-day rollup used by the Overview chart. Sourced from the
        runs table because it's the same source of truth as runs_totals().
        Previously we rolled up from events which meant the 7-day chart
        would show blank days while the Recent Activity below listed
        completed runs for those same days — very confusing.

        One row per DAY that has at least one run. Days with no runs are
        omitted (front-end can gap-fill with zeros if it wants a dense
        timeline)."""
        conn = self._get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

        rows = conn.execute("""
            SELECT
                substr(started_at, 1, 10) AS date,
                COUNT(*)                   AS runs,
                COALESCE(SUM(total_queries), 0) AS searches,
                COALESCE(SUM(total_ads),     0) AS ads,
                COALESCE(SUM(captchas),      0) AS captchas,
                SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN exit_code IS NOT NULL AND exit_code != 0 THEN 1 ELSE 0 END) AS failed
            FROM runs
            WHERE started_at >= ?
            GROUP BY date
            ORDER BY date
        """, (cutoff,)).fetchall()

        out = []
        for r in rows:
            searches = r["searches"] or 0
            empty    = max(0, (r["runs"] or 0) - (r["completed"] or 0))
            out.append({
                "date":      r["date"],
                "runs":      r["runs"] or 0,
                "searches":  searches,
                "ads":       r["ads"] or 0,       # matches front-end field name
                "captchas":  r["captchas"] or 0,
                "completed": r["completed"] or 0,
                "failed":    r["failed"] or 0,
                # Legacy key "empty" for backwards-compat with existing chart JS.
                # Now means "runs that didn't complete successfully".
                "empty":     empty,
            })
        return out

    # ──────────────────────────────────────────────────────────
    # МИГРАЦИЯ ИЗ СТАРЫХ ФАЙЛОВ
    # ──────────────────────────────────────────────────────────

    def migrate_from_files(self, verbose: bool = True):
        """Одноразовая migration из старых fileов in DB"""
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
    """Синглтон — один DB на everything onлоние"""
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
            print("✓ БД удалена")
        else:
            print("Добавь --yes for underтверждения")

    else:
        print("Команды: init | migrate | info | config | competitors | reset --yes")
