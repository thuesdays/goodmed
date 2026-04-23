# Database Reference

Ghost Shell uses a single SQLite file — `ghost_shell.db` at the project
root (`F:\projects\ghost_shell_browser\ghost_shell.db` on Windows,
equivalent on other platforms).

Every dashboard page and every run reads/writes through `db.py`.
There is no ORM — just typed helper methods on a `DB` class.

To inspect it directly:

```powershell
sqlite3 ghost_shell.db ".tables"
sqlite3 ghost_shell.db ".schema runs"
sqlite3 ghost_shell.db "SELECT * FROM config_kv LIMIT 20"
```

---

## Table overview

| Table          | Purpose                                          | Pruning         |
|----------------|--------------------------------------------------|-----------------|
| `config_kv`    | All dashboard settings (flat dotted-key store)   | Permanent       |
| `runs`         | One row per monitor run                          | Manual only     |
| `events`       | Search events: ok / empty / captcha / blocked    | Manual only     |
| `selfchecks`   | Fingerprint self-check snapshots                 | Last 20 / profile kept |
| `competitors`  | Every ad ever captured                           | Manual only     |
| `action_events`| Per-step log of the Script executor              | Manual only     |
| `ip_history`   | Per-IP usage counter + burn status               | Permanent       |
| `fingerprints` | Payload snapshot per profile                     | Last 10 / profile kept |
| `logs`         | Line-by-line monitor output                      | Rolling tail    |
| `meta`         | Schema version + migration markers               | Permanent       |

---

## `config_kv` — the single source of truth for settings

Flat key/value store. Every dashboard page uses dotted keys like
`search.queries`, `proxy.url`, `actions.main_script`, etc.

```sql
CREATE TABLE config_kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,           -- JSON-encoded (string/int/bool/list/dict)
    updated_at TEXT
);
```

### Key namespaces

| Prefix        | Example keys                                    |
|---------------|-------------------------------------------------|
| `search.*`    | `queries`, `my_domains`, `target_domains`, `block_domains`, `refresh_max_sec` |
| `proxy.*`     | `url`, `is_rotating`, `rotation_provider`, `auto_rotate_on_start`, `total_rotations` |
| `behavior.*`  | 14 timing ranges (`initial_load_min/max`, `between_queries_min/max`, …) + naturalness toggles |
| `browser.*`   | `profile_name`, `expected_country`, `expected_timezone` |
| `actions.*`   | `main_script`, `post_ad_actions`, `on_target_domain_actions` — each a list of step dicts. `main_script` supports nested `loop` actions with their own inner `steps[]` arrays and `{item}` placeholders. |
| `scheduler.*` | `enabled`, `cron_expr`, `last_run_at`           |
| `captcha.*`   | `twocaptcha_key`                                |
| `watchdog.*`  | `max_stall_sec`, `check_interval_sec`           |
| `system.*`    | `first_run_at` (machine-local, never exported)  |
| `profile.<n>.*` | Per-profile overrides (template, language)    |

Keys are **always dotted**. If you ever see a row like `key="proxy"` in
config_kv, that's a leftover from the v1 import bug — the current
`dashboard_server.py` cleans them up on startup.

### Access pattern

```python
from db import get_db
db = get_db()

# Single value
proxy_url = db.config_get("proxy.url")
# Write
db.config_set("search.my_domains", ["example.com", "example.org"])
# Whole dict (nested representation — for dashboard convenience)
cfg = db.config_get_all()   # → {"proxy": {"url": "..."}, "search": {...}}
```

### `actions.main_script` schema

The Main script is a list of top-level action steps. One special action
type — `loop` — has its own nested `steps[]` list that runs per item.
Inside nested steps, string params are substituted with the current
item via `{item}` (or a custom `item_var`).

```json
[
  {
    "type": "loop",
    "enabled": true,
    "items": ["term one", "term two", "term three"],
    "item_var": "query",
    "shuffle": true,
    "steps": [
      {"type": "pause",        "min_sec": 2, "max_sec": 5},
      {"type": "search_query", "query": "{query}"},
      {"type": "rotate_ip",    "wait_after_sec": 3}
    ]
  },
  {
    "type": "visit_url",
    "url": "https://example.com",
    "dwell_min": 5,
    "dwell_max": 12
  }
]
```

Substitution is recursive through dicts and lists but only swaps
bare-identifier placeholders (`{query}`, `{item}`, `{index}`,
`{total}`). Legacy `search_all_queries` steps are auto-upgraded to
the `loop` shape on read — see
`dashboard_server.py::api_actions_pipelines_get()`.

---

## `runs`

One row per invocation of `main.py`.

```sql
CREATE TABLE runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    profile_name  TEXT NOT NULL,
    proxy_url     TEXT,
    status        TEXT,    -- "running" | "ok" | "error" | "aborted"
    error_message TEXT,
    ads_total     INTEGER DEFAULT 0,
    competitors   INTEGER DEFAULT 0,
    duration_sec  REAL
);
```

Every run starts with `status='running'`. On exit the finalizer writes
`finished_at`, totals, and final status. If Python crashes hard and
can't finalize, the next dashboard startup calls `cleanup_stale_runs()`
which marks any orphaned `running` entries as `error`.

---

## `events`

Individual timeline events within a run.

```sql
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    event_type    TEXT NOT NULL,  -- search_ok / search_empty / captcha / blocked / …
    query         TEXT,
    details       TEXT,          -- JSON string
    duration_sec  REAL,
    results_count INTEGER
);
```

Aggregated for the Overview page's 7-day chart via `db.daily_stats()`.

---

## `action_events` — per-step script log

One row for every step execution inside the Scripts page's pipelines.

```sql
CREATE TABLE action_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    query         TEXT,
    ad_domain     TEXT,
    ad_class      TEXT,   -- "target" | "my_domain" | "competitor" | "unknown"
    action_type   TEXT NOT NULL,
    outcome       TEXT NOT NULL,  -- "ran" | "skipped" | "error"
    skip_reason   TEXT,           -- "my_domain"/"target"/"not_target"/"not_my_domain"/"probability"/"disabled"
    duration_sec  REAL,
    error         TEXT
);
```

This table powers:

- Overview → **Actions ran (24h)** hero stat with breakdown by type
- Overview → **Total actions performed** card
- Competitors → **Actions** column per domain

Query patterns:

```python
db.action_events_summary(hours=24)      # {actions_ran, actions_skipped,
                                         # actions_errored, by_type, by_ad_class}
db.action_events_by_domain()             # {domain: {ran, skipped, errored, last_action_at}}
db.action_events_recent(limit=50)        # Raw rows for debugging
```

---

## `competitors`

Every ad observed on a SERP.

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
    google_click_url TEXT
);
```

The Competitors dashboard aggregates by `domain`, joins with
`action_events` on `domain = ad_domain` to produce the "Actions"
column showing how many times we interacted with each advertiser.

---

## `ip_history` — proxy exit IP tracking

```sql
CREATE TABLE ip_history (
    ip                   TEXT PRIMARY KEY,
    first_seen           TEXT NOT NULL,
    last_seen            TEXT NOT NULL,
    total_uses           INTEGER DEFAULT 0,
    total_captchas       INTEGER DEFAULT 0,
    consecutive_capchas  INTEGER DEFAULT 0,
    burned_at            TEXT,       -- ISO timestamp when 3rd captcha hit
    country              TEXT,
    city                 TEXT,
    org                  TEXT,
    asn                  TEXT
);
```

Populated at **two points**:

1. `ip_record_start()` at the beginning of every run, right after
   proxy diagnostics — catches static (non-rotating) proxy IPs that
   would otherwise never trigger `ip_report()`.
2. `ip_report()` after every search: success increments `total_uses`,
   captcha increments `total_captchas` + `consecutive_capchas`. After
   3 consecutive captchas `burned_at` is set, which bans the IP from
   re-use for 12 hours.

Displayed on Proxy → IP Statistics.

---

## `fingerprints`

Snapshot of every generated fingerprint payload per profile.

```sql
CREATE TABLE fingerprints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    template_name TEXT,
    chrome_major  TEXT,
    payload_json  TEXT       -- full payload (base64-wrapped into the --ghost-shell-payload flag)
);
```

The dashboard shows the latest fingerprint under Profile → Detail and
lets you regenerate it (🎲 button).

---

## `selfchecks`

Results of the 29-test fingerprint self-check that runs at the start
of every monitor run.

```sql
CREATE TABLE selfchecks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    tests_json    TEXT NOT NULL,       -- {"webdriver_hidden": true, …}
    actual_json   TEXT,                -- navigator.* snapshot
    expected_json TEXT                 -- payload values from DB
);
```

Kept for the last 20 runs per profile.

---

## `logs`

Rolling per-line log tail.

```sql
CREATE TABLE logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    level         TEXT,
    profile_name  TEXT,
    message       TEXT
);
```

Consumed by the Logs page's live stream endpoint.

---

## `meta`

Schema versioning and migration markers.

```sql
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

---

## Export / Import

The Settings page exports all of `config_kv` plus profile metadata
and the three action pipelines into one portable JSON file. It does
**not** include `runs`, `events`, `competitors`, `action_events`,
`selfchecks`, `fingerprints`, `logs`, or `ip_history` — those are
installation-specific history.

Skipped machine-local keys (never exported, preserved on import):

- `proxy.total_rotations`
- `proxy.last_rotation_at`
- `system.first_run_at`

See `dashboard_server.py::api_export_config()` for the exact contract.

---

## Resetting

To wipe everything and start fresh:

```powershell
# Stop the dashboard first
del ghost_shell.db
rmdir /s /q profiles
python dashboard_server.py   # rebuilds DB from DEFAULT_CONFIG
```

To clear just the history (keep config, profiles, pipelines) — use the
"Clear history" button on the Runs page, or:

```python
from db import get_db
db = get_db()
conn = db._get_conn()
for tbl in ("runs", "events", "action_events", "competitors", "logs"):
    conn.execute(f"DELETE FROM {tbl}")
conn.commit()
```
