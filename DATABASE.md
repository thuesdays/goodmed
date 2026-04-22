# Ghost Shell — SQLite Data Layer

Один файл `ghost_shell.db` в корне проекта. Всё состояние приложения — там.
Кроме больших бинарников: скриншоты CreepJS остаются в `reports/creepjs/`,
но ссылки на них в БД.

## Таблицы

### 1. `runs` — каждый запуск мониторинга
Каждый клик "Запустить" или каждый вызов `python main.py` создаёт запись.

```sql
CREATE TABLE runs (
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
CREATE INDEX idx_runs_started ON runs(started_at DESC);
CREATE INDEX idx_runs_profile ON runs(profile_name);
```

### 2. `selfchecks` — история всех health_check
Каждый запуск браузера → одна запись. Видно как патчи меняются со временем.

```sql
CREATE TABLE selfchecks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    tests_json    TEXT NOT NULL,        -- {"ua_matches": true, ...}
    actual_json   TEXT,                  -- { userAgent: "...", ... }
    expected_json TEXT,                  -- из payload
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX idx_selfcheck_profile ON selfchecks(profile_name, timestamp DESC);
```

### 3. `events` — все SessionQuality-события
Вместо `profiles/<n>/session_quality.json` — одна таблица.

```sql
CREATE TABLE events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER,
    profile_name TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    event_type   TEXT NOT NULL,  -- search_ok | search_empty | captcha | blocked | ...
    query        TEXT,
    details      TEXT,
    duration_sec REAL,
    results_count INTEGER,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE SET NULL
);
CREATE INDEX idx_events_profile_ts ON events(profile_name, timestamp DESC);
CREATE INDEX idx_events_type ON events(event_type, timestamp DESC);
CREATE INDEX idx_events_run ON events(run_id);
```

### 4. `competitors` — найденные рекламные URL
Заменяет `competitor_urls.txt`.

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
CREATE INDEX idx_comp_query ON competitors(query);
CREATE INDEX idx_comp_ts ON competitors(timestamp DESC);
```

### 5. `ip_history` — rotating IP трекер
Заменяет `profiles/<n>/rotating_ips.json`.

```sql
CREATE TABLE ip_history (
    ip                TEXT PRIMARY KEY,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    total_uses        INTEGER DEFAULT 0,
    total_captchas    INTEGER DEFAULT 0,
    consecutive_capchas INTEGER DEFAULT 0,
    burned_at         TEXT,
    country           TEXT,
    city              TEXT,
    org               TEXT,
    asn               TEXT
);
CREATE INDEX idx_ip_burned ON ip_history(burned_at);
```

### 6. `fingerprints` — история payload_debug.json
Каждый раз при генерации → запись. Позволяет откатиться к предыдущему слепку.

```sql
CREATE TABLE fingerprints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    template_name TEXT,
    payload_json  TEXT NOT NULL,
    is_current    INTEGER DEFAULT 0
);
CREATE INDEX idx_fp_profile ON fingerprints(profile_name, timestamp DESC);
CREATE INDEX idx_fp_current ON fingerprints(profile_name, is_current);
```

### 7. `config_kv` — ключ-значение конфиг
Всё что было в `config.yaml` — тут. Точечное обновление одного поля = UPDATE одной строки.

```sql
CREATE TABLE config_kv (
    key       TEXT PRIMARY KEY,
    value     TEXT NOT NULL,        -- JSON-encoded value
    updated_at TEXT NOT NULL
);
```

Примеры ключей:
- `search.queries` → `["гудмедика", "goodmedika"]`
- `search.my_domains` → `["goodmedika.com.ua"]`
- `proxy.url` → `"user:pass@host:port"`
- `proxy.is_rotating` → `true`
- `browser.profile_name` → `"profile_01"`
- `captcha.twocaptcha_key` → `""`
- `behavior.open_background_tabs` → `true`
- etc.

### 8. `logs` — история живых логов (опционально, с ротацией)
```sql
CREATE TABLE logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER,
    timestamp TEXT NOT NULL,
    level     TEXT NOT NULL,
    message   TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX idx_logs_run ON logs(run_id, id);
```
Храним только последние 10000 строк, старые удаляем при вставке новой.

## Миграция из старых файлов

При первом запуске `db.py init`:
1. Если есть `config.yaml` → все поля → `config_kv`
2. Если есть `competitor_urls.txt` → каждая строка → `competitors`
3. Если есть `profiles/*/session_quality.json` → события → `events`
4. Если есть `profiles/*/rotating_ips.json` → IP → `ip_history`
5. Если есть `profiles/*/selfcheck.json` → последний → `selfchecks`
6. Если есть `profiles/*/payload_debug.json` → → `fingerprints` (is_current=1)

После миграции можно удалить старые файлы (или оставить для бэкапа).

## API модуля `db.py`

```python
from db import DB

db = DB()                                       # открывает ghost_shell.db

# Конфиг
db.config_get("proxy.url")                      # строка или None
db.config_set("proxy.url", "user:pass@...")
db.config_get_all()                              # dict {path: value}
db.config_set_all({...})                         # массово

# Runs
run_id = db.run_start(profile_name, proxy_url)
db.run_finish(run_id, exit_code=0, total_ads=5)
db.runs_list(limit=50)

# Events (для SessionQualityMonitor)
db.event_record(run_id, profile, "search_ok", query="...", duration_sec=4.2)
db.events_list(profile_name, since_hours=24)

# Selfchecks
db.selfcheck_save(run_id, profile, payload_check_result)
db.selfcheck_latest(profile_name)

# Competitors
db.competitor_add(run_id, query, domain, title, clean_url, google_click_url)
db.competitors_by_domain()
db.competitors_recent(limit=100)

# IP history
db.ip_report(ip, captcha=True)
db.ip_is_burned(ip)
db.ip_stats()

# Fingerprints
db.fingerprint_save(profile, payload_dict)
db.fingerprint_current(profile)
```
