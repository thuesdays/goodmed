"""
profile_enricher.py — Обогащение профиля Chrome реалистичными данными

Настоящий Chrome хранит в профиле:
- History — SQLite база со всеми посstillнными URL
- Bookmarks — JSON с закладками
- Login Data — логины (зашифрованы, их мы не трогаем)
- Preferences — настройки (already заполнены в nk_browser)
- Top Sites — топ сайтов на new tab page
- Favicons — SQLite с иконками сайтов

У пустого профиля all these базы либо не существуют, либо пусты —
this очень подозрительно. Мы заполняем их до первого запуска browserа
правдоподобными данными.

ВАЖНО: запускать ТОЛЬКО on закрытом browserе. Для существующего
профиля withoutопасно — добавляем данные не стирая существующие.
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import os
import json
import time
import random
import sqlite3
import logging
from datetime import datetime, timedelta


class ProfileEnricher:
    """
    Usage:
        enricher = ProfileEnricher(profile_path="profiles/profile_01")
        enricher.enrich_all()
    """

    # Популярные сайты that посещает средний юзер Украины
    COMMON_SITES = [
        ("https://www.google.com/",           "Google"),
        ("https://www.youtube.com/",          "YouTube"),
        ("https://www.youtube.com/watch?v=",  "YouTube видео"),
        ("https://mail.google.com/mail/u/0/", "Gmail"),
        ("https://www.rozetka.com.ua/",       "Rozetka"),
        ("https://www.rozetka.com.ua/ua/",    "Rozetka — Інтерне-магазин"),
        ("https://www.olx.ua/",               "OLX.ua"),
        ("https://uk.wikipedia.org/",         "Вікіпедія"),
        ("https://ru.wikipedia.org/",         "Википедия"),
        ("https://www.pravda.com.ua/",        "Українська правда"),
        ("https://www.ukr.net/",              "ukr.net"),
        ("https://www.bbc.com/",              "BBC"),
        ("https://www.google.com/maps",       "Google Maps"),
        ("https://translate.google.com/",     "Google Translate"),
        ("https://www.instagram.com/",        "Instagram"),
        ("https://www.facebook.com/",         "Facebook"),
        ("https://www.reddit.com/",           "reddit"),
        ("https://github.com/",               "GitHub"),
        ("https://stackoverflow.com/",        "Stack Overflow"),
        ("https://www.booking.com/",          "Booking.com"),
        ("https://www.aliexpress.com/",       "AliExpress"),
        ("https://prom.ua/",                  "Prom.ua"),
        ("https://zakupki.prom.ua/",          "Zakupki Prom"),
        ("https://novaposhta.ua/",            "Нова Пошта"),
        ("https://privat24.ua/",              "Приват24"),
        ("https://monobank.ua/",              "monobank"),
    ]

    # Поисковые queryы that средний юзер делал за afterдний месяц
    COMMON_SEARCHES = [
        "погода", "курс доллара", "новости",
        "as сделать скриншот", "that посмотреть",
        "рецепт борща", "время work почты",
        "as доехать до", "адрес", "телефон",
        "youtube", "переводчик", "google maps",
        "rozetka знижки", "olx робота",
    ]

    def __init__(self, profile_path: str):
        self.profile_path = profile_path
        self.default_dir  = os.path.join(profile_path, "Default")
        os.makedirs(self.default_dir, exist_ok=True)

    # ──────────────────────────────────────────────────────────
    # CHROME TIMESTAMP CONVERSION
    # Chrome использует microseconds с 1601-01-01
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _chrome_time(dt: datetime) -> int:
        """Конвертирует datetime в Chrome timestamp (microseconds since 1601)"""
        epoch_start = datetime(1601, 1, 1)
        delta = dt - epoch_start
        return int(delta.total_seconds() * 1_000_000)

    # ──────────────────────────────────────────────────────────
    # HISTORY — основная SQLite с посещениями
    # ──────────────────────────────────────────────────────────

    def seed_history(self, days_back: int = 30, visits_per_day_range: tuple = (5, 25)):
        """
        Overполняет History SQLite базу реалистичными посещениями
        за afterдние N дней.
        """
        db_path = os.path.join(self.default_dir, "History")

        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        # Create tables if they don't exist (Chrome 149 schema).
        # Must exactly match the bundled Chromium's expected columns —
        # see _ensure_history_schema() for the full spec and WHY this
        # matters (Chrome FATAL-crashes on schema mismatch).
        self._ensure_history_schema(cur)

        now = datetime.now()
        url_id_counter   = 1
        visit_id_counter = 1

        # Получаем максимальные ID тотбы не конфликтовать с существующими
        try:
            cur.execute("SELECT MAX(id) FROM urls")
            max_url_id = cur.fetchone()[0]
            if max_url_id:
                url_id_counter = max_url_id + 1
            cur.execute("SELECT MAX(id) FROM visits")
            max_visit_id = cur.fetchone()[0]
            if max_visit_id:
                visit_id_counter = max_visit_id + 1
        except Exception:
            pass

        url_cache = {}   # url → id for дедупликации
        total_visits = 0

        # Для each дня генерируем посещения
        for day_offset in range(days_back, 0, -1):
            day_start = now - timedelta(days=day_offset)
            visits_today = random.randint(*visits_per_day_range)

            for _ in range(visits_today):
                # Случайный сайт из списка
                url, title = random.choice(self.COMMON_SITES)

                # Добавляем случайный путь for неwhich сайтов (реалистичнее)
                if random.random() < 0.4 and "?" not in url:
                    paths = ["search?q=test", "about", "contact", "news", "login"]
                    url = url + random.choice(paths)

                # Случайное время в течение дня
                visit_time = day_start + timedelta(
                    hours   = random.randint(7, 23),
                    minutes = random.randint(0, 59),
                    seconds = random.randint(0, 59),
                )

                # URL запись
                if url in url_cache:
                    url_id = url_cache[url]
                    cur.execute(
                        "UPDATE urls SET visit_count = visit_count + 1, "
                        "last_visit_time = ? WHERE id = ?",
                        (self._chrome_time(visit_time), url_id)
                    )
                else:
                    url_id = url_id_counter
                    url_id_counter += 1
                    url_cache[url] = url_id

                    try:
                        cur.execute("""
                            INSERT INTO urls (id, url, title, visit_count, typed_count,
                                              last_visit_time, hidden)
                            VALUES (?, ?, ?, 1, ?, ?, 0)
                        """, (
                            url_id, url, title,
                            1 if random.random() < 0.2 else 0,  # typed — 20% ввели в адресную строку
                            self._chrome_time(visit_time),
                        ))
                    except sqlite3.IntegrityError:
                        # URL already is
                        pass

                # Visit запись
                try:
                    cur.execute("""
                        INSERT INTO visits (id, url, visit_time, from_visit, external_referrer_url,
                                            transition, segment_id, visit_duration, incremented_omnibox_typed_score)
                        VALUES (?, ?, ?, 0, '', ?, 0, ?, 0)
                    """, (
                        visit_id_counter, url_id,
                        self._chrome_time(visit_time),
                        805306376 if random.random() < 0.3 else 805306368,  # link / typed
                        random.randint(3000000, 180000000),  # длительность в микросекундах
                    ))
                    visit_id_counter += 1
                    total_visits += 1
                except sqlite3.IntegrityError:
                    pass

        conn.commit()
        conn.close()
        logging.info(f"[ProfileEnricher] History: +{total_visits} посещений за {days_back} дней")

    def _ensure_history_schema(self, cur):
        """Creates History tables with the full Chrome 149 schema.

        Chrome validates the exact column set on first open. Any missing
        column causes `FATAL: Cannot call mutating statements on an invalid
        statement` during profile initialization, because Chrome's own
        compiled SQL INSERT/UPDATE statements reference columns that
        don't exist in our seeded DB.

        This schema matches Chrome 149.0.7805 exactly. When bumping the
        bundled Chromium version, cross-check this against
        chrome/browser/history/history_database.cc — look for the
        CreateURLTable() and CreateMainTable() SQL strings.

        Also important: the `meta` table with a correct `version` value
        is REQUIRED. Without it Chrome assumes schema is legacy or
        corrupted and either migrates (succeeds silently) or throws.
        """
        cur.executescript("""
            -- urls: the URL dictionary (one row per unique URL visited)
            CREATE TABLE IF NOT EXISTS urls (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                url              LONGVARCHAR,
                title            LONGVARCHAR,
                visit_count      INTEGER DEFAULT 0 NOT NULL,
                typed_count      INTEGER DEFAULT 0 NOT NULL,
                last_visit_time  INTEGER NOT NULL,
                hidden           INTEGER DEFAULT 0 NOT NULL
            );
            CREATE INDEX IF NOT EXISTS urls_url_index ON urls(url);

            -- visits: per-visit audit log. Chrome 120+ added all the
            -- originator_* columns for cross-device sync, plus
            -- visited_link_id (Chrome 140+ :visited selector privacy)
            -- and consider_for_ntp_most_visited (Chrome 116+).
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY,
                url INTEGER NOT NULL,
                visit_time INTEGER NOT NULL,
                from_visit INTEGER,
                external_referrer_url TEXT,
                transition INTEGER DEFAULT 0 NOT NULL,
                segment_id INTEGER,
                visit_duration INTEGER DEFAULT 0 NOT NULL,
                incremented_omnibox_typed_score BOOLEAN DEFAULT FALSE NOT NULL,
                opener_visit INTEGER,
                originator_cache_guid TEXT DEFAULT '',
                originator_visit_id INTEGER DEFAULT 0 NOT NULL,
                originator_from_visit INTEGER DEFAULT 0 NOT NULL,
                originator_opener_visit INTEGER DEFAULT 0 NOT NULL,
                is_known_to_sync BOOLEAN DEFAULT FALSE NOT NULL,
                consider_for_ntp_most_visited BOOLEAN DEFAULT FALSE NOT NULL,
                visited_link_id INTEGER DEFAULT 0 NOT NULL,
                app_id TEXT
            );
            CREATE INDEX IF NOT EXISTS visits_url_index ON visits(url);
            CREATE INDEX IF NOT EXISTS visits_from_index ON visits(from_visit);
            CREATE INDEX IF NOT EXISTS visits_time_index ON visits(visit_time);

            CREATE TABLE IF NOT EXISTS keyword_search_terms (
                keyword_id       INTEGER NOT NULL,
                url_id           INTEGER NOT NULL,
                term             LONGVARCHAR NOT NULL,
                normalized_term  LONGVARCHAR NOT NULL
            );
            CREATE INDEX IF NOT EXISTS keyword_search_terms_index1
                ON keyword_search_terms (keyword_id, normalized_term);
            CREATE INDEX IF NOT EXISTS keyword_search_terms_index2
                ON keyword_search_terms (url_id);
            CREATE INDEX IF NOT EXISTS keyword_search_terms_index3
                ON keyword_search_terms (term);

            -- visit_source: Chrome tracks WHERE each visit came from
            -- (sync, import, extensions, browsed, firefox_imported…).
            -- Its absence caused History DB corruption warnings in Chrome 118+.
            CREATE TABLE IF NOT EXISTS visit_source (
                id      INTEGER PRIMARY KEY,
                source  INTEGER NOT NULL
            );

            -- download tables exist in a stock fresh profile even when
            -- empty — Chrome creates them on first open, but having
            -- them pre-populated avoids the write-on-read race.
            CREATE TABLE IF NOT EXISTS downloads (
                id                    INTEGER PRIMARY KEY,
                guid                  VARCHAR NOT NULL,
                current_path          LONGVARCHAR NOT NULL,
                target_path           LONGVARCHAR NOT NULL,
                start_time            INTEGER NOT NULL,
                received_bytes        INTEGER NOT NULL,
                total_bytes           INTEGER NOT NULL,
                state                 INTEGER NOT NULL,
                danger_type           INTEGER NOT NULL,
                interrupt_reason      INTEGER NOT NULL,
                hash                  BLOB NOT NULL,
                end_time              INTEGER NOT NULL,
                opened                INTEGER NOT NULL,
                last_access_time      INTEGER NOT NULL,
                transient             INTEGER NOT NULL,
                referrer              VARCHAR NOT NULL,
                site_url              VARCHAR NOT NULL,
                embedder_download_data VARCHAR NOT NULL DEFAULT '',
                tab_url               VARCHAR NOT NULL,
                tab_referrer_url      VARCHAR NOT NULL,
                http_method           VARCHAR NOT NULL,
                by_ext_id             VARCHAR NOT NULL,
                by_ext_name           VARCHAR NOT NULL,
                by_web_app_id         VARCHAR NOT NULL DEFAULT '',
                etag                  VARCHAR NOT NULL,
                last_modified         VARCHAR NOT NULL,
                mime_type             VARCHAR(255) NOT NULL,
                original_mime_type    VARCHAR(255) NOT NULL
            );

            -- segments — used for typed-count ranking in the omnibox.
            CREATE TABLE IF NOT EXISTS segments (
                id      INTEGER PRIMARY KEY,
                name    VARCHAR,
                url_id  INTEGER NON NULL
            );
            CREATE INDEX IF NOT EXISTS segments_name ON segments(name);
            CREATE INDEX IF NOT EXISTS segments_url_id ON segments(url_id);

            CREATE TABLE IF NOT EXISTS segment_usage (
                id          INTEGER PRIMARY KEY,
                segment_id  INTEGER NOT NULL,
                time_slot   INTEGER NOT NULL,
                visit_count INTEGER DEFAULT 0 NOT NULL
            );
            CREATE INDEX IF NOT EXISTS segment_usage_time_slot_segment_id
                ON segment_usage (time_slot, segment_id);
            CREATE INDEX IF NOT EXISTS segments_usage_seg_id
                ON segment_usage (segment_id);

            -- meta: schema version. Chrome hard-checks this on open —
            -- absence OR wrong version triggers migration path which
            -- calls mutating SQL against partial schema → FATAL.
            -- Version 66 is Chrome 149's History DB schema version.
            CREATE TABLE IF NOT EXISTS meta (
                key    LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY,
                value  LONGVARCHAR
            );
            INSERT OR REPLACE INTO meta (key, value) VALUES ('version', '66');
            INSERT OR REPLACE INTO meta (key, value) VALUES ('last_compatible_version', '62');
            INSERT OR REPLACE INTO meta (key, value) VALUES ('early_expiration_threshold', '0');
        """)

    # ──────────────────────────────────────────────────────────
    # BOOKMARKS — JSON файл
    # ──────────────────────────────────────────────────────────

    def seed_bookmarks(self, count_range: tuple = (5, 15)):
        """Создаёт/дополняет файл Bookmarks реалистичными закладками"""
        bookmarks_path = os.path.join(self.default_dir, "Bookmarks")

        # Если already существуют — не трогаем
        if os.path.exists(bookmarks_path):
            logging.info("[ProfileEnricher] Bookmarks already существуют — пропускаем")
            return

        count = random.randint(*count_range)
        chosen = random.sample(self.COMMON_SITES, min(count, len(self.COMMON_SITES)))

        # Chrome bookmarks format
        now_chrome = self._chrome_time(datetime.now() - timedelta(days=random.randint(30, 180)))

        children = []
        for i, (url, title) in enumerate(chosen):
            children.append({
                "date_added":     str(now_chrome + i * 10000),
                "guid":           self._generate_guid(),
                "id":             str(100 + i),
                "meta_info":      {},
                "name":           title,
                "type":           "url",
                "url":            url,
            })

        bookmarks = {
            "checksum":  "",
            "roots": {
                "bookmark_bar": {
                    "children":     children[:min(5, len(children))],  # первые 5 на панели
                    "date_added":   str(now_chrome),
                    "date_modified": str(now_chrome),
                    "guid":         self._generate_guid(),
                    "id":           "1",
                    "name":         "Bookmarks bar",
                    "type":         "folder",
                },
                "other": {
                    "children":     children[5:],  # остальные в "other"
                    "date_added":   str(now_chrome),
                    "date_modified": str(now_chrome),
                    "guid":         self._generate_guid(),
                    "id":           "2",
                    "name":         "Other bookmarks",
                    "type":         "folder",
                },
                "synced": {
                    "children":     [],
                    "date_added":   str(now_chrome),
                    "date_modified": "0",
                    "guid":         self._generate_guid(),
                    "id":           "3",
                    "name":         "Mobile bookmarks",
                    "type":         "folder",
                },
            },
            "version": 1,
        }

        with open(bookmarks_path, "w", encoding="utf-8") as f:
            json.dump(bookmarks, f, indent=3, ensure_ascii=False)

        logging.info(f"[ProfileEnricher] Bookmarks: добавлено {count} закладок")

    @staticmethod
    def _generate_guid() -> str:
        """Генерирует Chrome-совместимый GUID"""
        import uuid
        return str(uuid.uuid4()).upper()

    # ──────────────────────────────────────────────────────────
    # TOP SITES — SQLite с топом for new tab page
    # ──────────────────────────────────────────────────────────

    def seed_top_sites(self):
        """Populates Top Sites DB — what's shown on the new-tab page.

        Same schema-mismatch FATAL trap as History (see
        _ensure_history_schema). Chrome 149's Top Sites schema adds a
        required `meta` table and the thumbnail column with NOT NULL
        constraints. We create the full schema and a minimal meta row.
        """
        db_path = os.path.join(self.default_dir, "Top Sites")

        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        cur.executescript("""
            CREATE TABLE IF NOT EXISTS top_sites (
                url LONGVARCHAR NOT NULL,
                url_rank INTEGER NOT NULL,
                title LONGVARCHAR NOT NULL
            );
            -- Required meta table — same schema-version gate as History DB.
            -- Top Sites schema version 5 matches Chrome 149.
            CREATE TABLE IF NOT EXISTS meta (
                key    LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY,
                value  LONGVARCHAR
            );
            INSERT OR REPLACE INTO meta (key, value) VALUES ('version', '5');
            INSERT OR REPLACE INTO meta (key, value) VALUES ('last_compatible_version', '4');
        """)

        # Pick a random top-8 from the common-sites pool.
        top = random.sample(self.COMMON_SITES, min(8, len(self.COMMON_SITES)))
        cur.execute("DELETE FROM top_sites")  # clear before reseed
        for rank, (url, title) in enumerate(top):
            cur.execute(
                "INSERT INTO top_sites (url, url_rank, title) VALUES (?, ?, ?)",
                (url, rank, title)
            )

        conn.commit()
        conn.close()
        logging.info(f"[ProfileEnricher] Top Sites: {len(top)} сайтов")

    # ──────────────────────────────────────────────────────────
    # LAST SESSION / LAST TABS — "вкладки с прошлого раза"
    # ──────────────────────────────────────────────────────────

    def seed_last_session(self):
        """Создаёт пустые Current Session / Current Tabs тотбы Chrome не
        жаловался на "свежий" профиль"""
        for filename in ("Current Session", "Current Tabs", "Last Session", "Last Tabs"):
            path = os.path.join(self.default_dir, filename)
            if not os.path.exists(path):
                # Минимальный бинарный заголовок Session файла
                # Chrome создаст нормальный on первом запуске, просто need тотбы
                # файл был — for реальности
                with open(path, "wb") as f:
                    f.write(b"SNSS")  # magic bytes

    # ──────────────────────────────────────────────────────────
    # ОБЩАЯ ФУНКЦИЯ
    # ──────────────────────────────────────────────────────────

    def enrich_all(self, history_days: int = 30, seed_history_enabled: bool = False):
        """Enrich a freshly-created profile so it doesn't look synthetic.

        seed_history_enabled=False by default since Chrome 149's History
        schema evolves frequently and a mismatched INSERT triggers
        FATAL: Cannot call mutating statements on an invalid statement
        the moment Chrome opens the DB. User-reported crashes traced to
        this: seed_history wrote `urls`/`visits` with our best-guess
        schema, Chrome validated on open, found columns missing or
        extra, and hard-exited.

        Bookmarks and Top Sites are JSON files (Bookmarks) or small
        fixed-schema SQLite DBs (Top Sites) that Chrome tolerates
        wider version drift on — those stay enabled.

        For realistic History, users should run the Chrome History
        Import feature (dashboard → Edit Profile → "Warm up from real
        Chrome") which COPIES the host Chrome's actual History DB with
        exactly matching schema, guaranteed compatible."""
        logging.info(f"[ProfileEnricher] 🌱 Обогащаем профиль: {self.profile_path}")

        if seed_history_enabled:
            try:
                self.seed_history(days_back=history_days)
            except Exception as e:
                logging.warning(f"[ProfileEnricher] history: {e}")
        else:
            logging.info(
                "[ProfileEnricher] skipping synthetic history seed "
                "(use Dashboard → Edit Profile → Warm up from real Chrome "
                "for real history with guaranteed-compatible schema)"
            )

        try:
            self.seed_bookmarks()
        except Exception as e:
            logging.warning(f"[ProfileEnricher] bookmarks: {e}")

        # Top Sites and Last Session are also SQLite/binary files
        # Chrome hard-validates on open. Same FATAL risk as History.
        # Skipped by default. Real Top Sites arrive through
        # chrome_importer's WAL-safe copy when users opt in.
        try:
            self.seed_last_session()   # writes only "SNSS" magic — safe
        except Exception as e:
            logging.warning(f"[ProfileEnricher] last_session: {e}")

        logging.info("[ProfileEnricher] ✓ Обогащение завершено")
