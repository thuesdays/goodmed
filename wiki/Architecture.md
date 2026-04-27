# Architecture

## Code layout

```
ghost_shell/
├── core/         platform paths, log banners, process reaper
├── db/           SQLite layer; idempotent migrations
├── fingerprint/  device templates · generator · validator · selftest
├── proxy/        diagnostics · forwarder · pool · rotation provider
├── profile/      manager · enricher · validator · pool
├── session/      cookies · cookie pool (resurrection) · warmup robot
├── browser/      runtime (CDP-driven) · watchdog · traffic collector
├── extensions/   pool (CWS install, CRX unpack, manifest normalize)
├── actions/      script flow runner · query expander
├── scheduler/    pure-Python cron parser + 3 schedule modes
├── dashboard/    Flask server (5000+ LOC, 80+ endpoints)
├── accounts/     Fernet-encrypted vault
├── __main__.py   runpy dispatcher for monitor|dashboard|scheduler
└── main.py       monitor entrypoint
```

Three top-level CLI entrypoints, all routed through `__main__.py`:

- `python -m ghost_shell monitor` — one ad-monitoring pass for the
  configured active profile
- `python -m ghost_shell dashboard` — Flask server on `:5000`
- `python -m ghost_shell scheduler` — background loop that fires
  `monitor` runs on a schedule

## Data flow for a single run

```
        ┌────────────────────────────────────────┐
        │  Scheduler / dashboard "▶ Start"       │
        └─────────────────┬──────────────────────┘
                          │
                          ▼
        ┌────────────────────────────────────────┐
        │  main.py monitor entrypoint            │
        │  - resolve profile config              │
        │  - resolve script flow                 │
        │  - resolve assigned extensions         │
        │  - prep user-data-dir + Preferences    │
        │  - inject pending cookie restore       │
        └─────────────────┬──────────────────────┘
                          │
                          ▼
        ┌────────────────────────────────────────┐
        │  browser/runtime — Chromium launch     │
        │  --user-data-dir=<profile_dir>         │
        │  --load-extension=<pool_id1,id2,...>   │
        │  CDP attached for traffic + emulation  │
        └─────────────────┬──────────────────────┘
                          │
                          ▼
        ┌────────────────────────────────────────┐
        │  actions/runner — execute script flow  │
        │  click_ad / fill_form / extension_*    │
        │  with human-like timing                │
        └─────────────────┬──────────────────────┘
                          │
                          ▼
        ┌────────────────────────────────────────┐
        │  on clean exit:                        │
        │  - snapshot cookies (if exit_code=0    │
        │    and no captchas)                    │
        │  - record run in `runs` table          │
        │  - persist competitor captures, logs   │
        └────────────────────────────────────────┘
```

## Data layer

Single SQLite file `ghost_shell.db` at the project root. 18 tables.
Every dashboard page and every run reads/writes through
`ghost_shell.db.database`. No ORM — typed helpers on a `DB` class.

Schemas live in `ghost_shell/db/database.py` inside `SCHEMA_SQL`.
`_ensure_column()` runs on every startup, detects missing columns,
and `ALTER TABLE` adds them without touching existing data — so
upgrades across versions never need a manual migration.

Full table-by-table reference:
[`DATABASE.md`](https://github.com/thuesdays/ghost_shell_browser/blob/main/DATABASE.md).

## Anti-detection stack

The README's
[Anti-detection layers](https://github.com/thuesdays/ghost_shell_browser#anti-detection-layers)
section has the full list. Short version: each profile gets one
**coherent** fingerprint (UA, GPU, fonts, timezone all from one
device template) plus C++ patches in the binary, CDP runtime
emulation for mobile, behaviour timing, session warmup, cookie pool,
and proxy intelligence — stacked top-to-bottom, each layer hides a
different detection signal.

## Extension model

Why one shared pool instead of per-profile copies:

- **Disk space** — a serious extension is 5-30 MB; 50 profiles =
  250 MB of duplicated bytes vs ~10 MB shared.
- **Updates** — re-download once, every profile gets the new version
  on next launch.
- **Permissions / consistency** — all profiles see the exact same
  code surface, no risk of drift after a manual edit.

Per-profile extension **data** (cookies, IndexedDB, login state,
settings) lives inside the user-data-dir at:

```
<profile_user_data_dir>/Default/Local Extension Settings/<id>/
<profile_user_data_dir>/Default/IndexedDB/chrome-extension_<id>_0.indexeddb.leveldb/
<profile_user_data_dir>/Default/Storage/ext/<id>/
```

This is fully isolated per-profile and survives across launches —
Chrome handles persistence as long as we keep pointing at the same
source dir on subsequent launches.

See [Extensions](Extensions.md) for the install pipeline,
ID-derivation quirks, and automation steps.
