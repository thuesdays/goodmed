# Database Reference

Ghost Shell Anty uses a single SQLite file -- `ghost_shell.db` at the
project root. Every dashboard page and every run reads/writes through
`ghost_shell.db.database` (the post-refactor location of the layer
historically called `db.py`). There is no ORM -- just typed helper
methods on a `DB` class.

```powershell
sqlite3 ghost_shell.db ".tables"
sqlite3 ghost_shell.db ".schema runs"
sqlite3 ghost_shell.db "SELECT * FROM config_kv LIMIT 20"
```

---

## Table overview (18 tables)

| Table              | Purpose                                                  | Pruning                |
|--------------------|----------------------------------------------------------|------------------------|
| `config_kv`        | All dashboard settings (flat dotted-key store)           | Permanent              |
| `runs`             | One row per monitor run                                  | Manual only            |
| `events`           | Search events: ok / empty / captcha / blocked            | Manual only            |
| `selfchecks`       | Runtime network self-check snapshots (geo / WebRTC / TZ) | Last 20 / profile kept |
| `competitors`      | Every ad ever captured                                   | Manual only            |
| `action_events`    | Per-step log of the Script executor                      | Manual only            |
| `ip_history`       | Per-IP usage counter + burn status                       | Permanent              |
| `fingerprints`     | Phase 2 fingerprint snapshots + coherence reports        | Last 10 / profile kept |
| `profiles`         | Per-profile metadata (tags, proxy/script/group refs)     | Permanent              |
| `profile_groups`   | Named groups for batch monitoring                        | Permanent              |
| `profile_group_members` | Many-to-many between profiles and groups            | Permanent              |
| `traffic_stats`    | Per-profile per-host bandwidth counter                   | `traffic.retention_days` |
| `scripts`          | Saved per-ad pipelines (Scripts page library)            | Permanent              |
| `proxies`          | Proxy library with cached diagnostics                    | Permanent              |
| `warmup_runs`      | Each warmup invocation with per-site result log          | Manual only            |
| `cookie_snapshots` | Cookie-pool entries (auto + manual snapshots)            | Manual only            |
| `vault_items`      | Encrypted credential vault                               | Permanent              |
| `logs`             | Line-by-line monitor output                              | Rolling 10k tail       |

All schemas live in `ghost_shell/db/database.py` inside `SCHEMA_SQL`.
Migrations are idempotent -- `_ensure_column()` runs on every startup,
detects missing columns and `ALTER TABLE` adds them without disturbing
existing data.

---

## `config_kv` -- single source of truth for settings

Flat key/value store. Dotted keys like `search.queries`, `proxy.url`,
`scheduler.cron_expression`, `vault.salt`.

```sql
CREATE TABLE config_kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
```

### Key namespaces

| Namespace          | Examples                                                          |
|--------------------|-------------------------------------------------------------------|
| `search.*`         | `queries`, `my_domains`, `target_domains`, `block_domains`        |
| `browser.*`        | `binary_path`, `profile_name`, `preferred_language`, `auto_session` |
| `proxy.*`          | `auto_rotate_on_start`, `rotate_every_n_runs`, default settings   |
| `scheduler.*`      | `target_runs_per_day`, `active_hours`, `schedule_mode`, `cron_expression`, `interval_sec`, `active_days`, `profile_names`, `selection_mode`, `group_id`, `group_mode` |
| `behavior.*`       | typing/dwell/scroll/refresh timing                                |
| `traffic.*`        | `retention_days`, block-list patterns                             |
| `vault.*`          | `salt`, `verifier`, `initialized_at`                              |
| `session.*`        | `pending_restore.<profile>` -- queued snapshot for next launch    |

```python
from ghost_shell.db import get_db
db = get_db()
q = db.config_get("search.queries") or []
db.config_set("scheduler.cron_expression", "*/15 7-20 * * 1-5")
```

---

## `runs` -- monitor pass log

```sql
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name    TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    exit_code       INTEGER,
    error           TEXT,
    total_queries   INTEGER DEFAULT 0,
    total_ads       INTEGER DEFAULT 0,
    captchas        INTEGER DEFAULT 0,
    pid             INTEGER,
    heartbeat_at    TEXT
);
```

---

## `competitors` -- ad capture log

```sql
CREATE TABLE competitors (
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
CREATE INDEX idx_comp_domain ON competitors(domain);
CREATE INDEX idx_comp_query  ON competitors(query);
CREATE INDEX idx_comp_ts     ON competitors(timestamp DESC);
```

Used by the Competitors page:
- `competitors_by_domain(days, search)` -- aggregated table
- `competitors_trend(days, top_n)` -- daily bucket counts (line chart)
- `competitors_sparklines(days)` -- per-row 7-day mini-charts
- `competitor_detail(domain, days)` -- drill-down (titles/URLs/queries)
- `competitors_by_query(days, top_n)` -- share-of-voice tab

---

## `fingerprints` -- Phase 2 coherence snapshots

```sql
CREATE TABLE fingerprints (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name     TEXT NOT NULL,
    timestamp        TEXT NOT NULL,
    template_name    TEXT,
    template_id      TEXT,
    payload_json     TEXT NOT NULL,
    is_current       INTEGER NOT NULL DEFAULT 0,
    coherence_score  INTEGER,
    coherence_report TEXT,
    locked_fields    TEXT,
    source           TEXT,         -- generated | manual_edit | runtime_observed
    reason           TEXT
);
```

Only one row per profile has `is_current=1`. History preserved so the
Restore button on the Fingerprint editor can flip an old snapshot back
to current. Dual-mode toggle (`/api/fingerprint/<name>/mode`) finds
an existing desktop/mobile FP in this history first; falls back to
fresh generation when none matches.

---

## `warmup_runs` -- session warmup log

```sql
CREATE TABLE warmup_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name     TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    preset           TEXT,
    sites_planned    INTEGER NOT NULL DEFAULT 0,
    sites_visited    INTEGER NOT NULL DEFAULT 0,
    sites_succeeded  INTEGER NOT NULL DEFAULT 0,
    duration_sec     REAL,
    status           TEXT NOT NULL DEFAULT 'running',
    trigger          TEXT NOT NULL DEFAULT 'manual',
    notes            TEXT,
    sites_log        TEXT
);
```

`preset`: general | medical | tech | news | mobile | custom
`status`: running | ok | partial | failed
`trigger`: manual | scheduled | auto_before_run

---

## `cookie_snapshots` -- cookie pool

```sql
CREATE TABLE cookie_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name     TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    run_id           INTEGER,
    trigger          TEXT NOT NULL DEFAULT 'manual',
    cookies_json     TEXT NOT NULL DEFAULT '[]',
    storage_json     TEXT NOT NULL DEFAULT '{}',
    cookie_count     INTEGER NOT NULL DEFAULT 0,
    domain_count     INTEGER NOT NULL DEFAULT 0,
    bytes            INTEGER NOT NULL DEFAULT 0,
    reason           TEXT
);
```

`main.py` calls `snapshot_after_run()` at the end of every clean run
(`exit_code=0` and no captchas). Restore is manual via UI -- writes
`session.pending_restore.<profile>` to `config_kv`, next browser
launch reads it and injects via CDP.

---

## `vault_items` -- encrypted credential vault

```sql
CREATE TABLE vault_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'account',
    service          TEXT,
    identifier       TEXT,
    secrets_enc      TEXT,                  -- Fernet ciphertext of a JSON object
    profile_name     TEXT,
    status           TEXT NOT NULL DEFAULT 'active',
    tags_json        TEXT,
    notes            TEXT,
    last_used_at     TEXT,
    last_login_at    TEXT,
    last_login_status TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
```

Kind values: `account | crypto_wallet | email | social | api_key | totp_only | note | custom`

Encryption goes through `ghost_shell/accounts/vault.py`: master
password -> PBKDF2-HMAC-SHA256 (200k iterations) + salt -> 32-byte
Fernet key. Salt + verifier in `config_kv`. DB layer never sees plaintext.

---

## `proxies` -- proxy library

Stores named proxies plus cached diagnostics (geo / ASN / IP type /
latency / detection risk). See `ghost_shell/proxy/diagnostics.py` for
the test helper that fills the `last_*` columns.

`proxy.password` is currently plaintext in this table (legacy). Plan
is to migrate auth into `vault_items`. Keep the SQLite file out of
public dumps until then.

---

## `scripts` -- pipelines library

```sql
CREATE TABLE scripts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL DEFAULT '',
    flow         TEXT NOT NULL DEFAULT '[]',
    is_default   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
```

Each profile in `profiles` has optional `script_id` -- null = use the
script flagged `is_default=1`.

---

## `traffic_stats` -- bandwidth counter

```sql
CREATE TABLE traffic_stats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    profile_name  TEXT NOT NULL,
    host          TEXT,
    bytes_down    INTEGER NOT NULL DEFAULT 0,
    bytes_up      INTEGER NOT NULL DEFAULT 0,
    requests      INTEGER NOT NULL DEFAULT 0
);
```

Filled by `ghost_shell/browser/traffic.py` via CDP `Network.*` events.
Old rows pruned at startup per `traffic.retention_days` (default 90).

---

## Recommended cleanup queries

Reset historical data, keep config:

```sql
DELETE FROM events;
DELETE FROM competitors;
DELETE FROM action_events;
DELETE FROM logs;
DELETE FROM warmup_runs;
DELETE FROM cookie_snapshots WHERE trigger != 'manual';
VACUUM;
```

Drop one profile's history (keep its config row):

```sql
DELETE FROM events           WHERE profile_name = 'profile_01';
DELETE FROM competitors      WHERE run_id IN (SELECT id FROM runs WHERE profile_name='profile_01');
DELETE FROM action_events    WHERE profile_name = 'profile_01';
DELETE FROM warmup_runs      WHERE profile_name = 'profile_01';
DELETE FROM cookie_snapshots WHERE profile_name = 'profile_01';
DELETE FROM fingerprints     WHERE profile_name = 'profile_01';
DELETE FROM selfchecks       WHERE profile_name = 'profile_01';
DELETE FROM runs             WHERE profile_name = 'profile_01';
```

The dashboard's **Profiles -> row menu -> Delete profile** does this
plus deletes the on-disk user-data-dir.
