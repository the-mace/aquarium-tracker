import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "fathom.db"),
)

REFERENCE_CACHE_DB_PATH = os.environ.get(
    "REFERENCE_CACHE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "reference_cache.db"),
)


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_db_readonly():
    """Read-only connection (SQLite URI mode=ro) for untrusted/AI-generated queries.

    Guarantees no write can succeed even if SQL-text filtering upstream is imperfect.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_schema_text(table_names=None):
    """Table/column listing for the main DB, used to give the chat AI query-tool context.

    If table_names is given, only those tables are included.
    """
    wanted = set(table_names) if table_names else None
    with get_db_readonly() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        lines = []
        for row in tables:
            name = row[0]
            if wanted is not None and name not in wanted:
                continue
            cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
            col_desc = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            lines.append(f"{name}({col_desc})")
        return "\n".join(lines)


@contextmanager
def get_ref_db():
    """Context manager for the reference cache DB (separate from main DB so it survives resets)."""
    os.makedirs(os.path.dirname(REFERENCE_CACHE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(REFERENCE_CACHE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tanks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                water_type TEXT CHECK(water_type IN ('fresh', 'salt', 'brackish')),
                volume_gallons REAL,
                dimensions_l REAL,
                dimensions_w REAL,
                dimensions_h REAL,
                shape TEXT,
                manufacturer TEXT,
                model TEXT,
                substrate_type TEXT,
                substrate_brand TEXT,
                substrate_depth_inches REAL,
                setup_date TEXT,
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'inactive', 'archived')),
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tank_equipment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                category TEXT CHECK(category IN ('filter','heater','light','uv','pump','co2','other')),
                brand TEXT,
                model TEXT,
                specs TEXT,
                installed_date TEXT,
                removed_date TEXT,
                is_active INTEGER DEFAULT 1,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                timestamp TEXT DEFAULT (datetime('now')),
                ph REAL,
                gh REAL,
                kh REAL,
                ammonia REAL,
                nitrite REAL,
                nitrate REAL,
                tds REAL,
                temp REAL,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS inhabitants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                species TEXT,
                common_name TEXT,
                count INTEGER DEFAULT 0,
                added_date TEXT,
                source TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS population_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                inhabitant_id INTEGER,
                event_type TEXT CHECK(event_type IN ('added','died','removed','born')),
                count INTEGER,
                timestamp TEXT DEFAULT (datetime('now')),
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE,
                FOREIGN KEY (inhabitant_id) REFERENCES inhabitants(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER,
                item TEXT,
                category TEXT CHECK(category IN ('equipment','livestock','plants','hardscape','consumables','food','decor','other')),
                vendor TEXT,
                cost REAL,
                purchase_date TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                timestamp TEXT DEFAULT (datetime('now')),
                event_type TEXT CHECK(event_type IN ('water_change','feeding','purchase','observation','treatment','maintenance','other')),
                notes TEXT,
                amount REAL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                title TEXT,
                description TEXT,
                status TEXT DEFAULT 'open' CHECK(status IN ('open','monitoring','resolved')),
                opened_at TEXT DEFAULT (datetime('now')),
                monitoring_at TEXT,
                resolved_at TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                related_event_id INTEGER,
                related_test_id INTEGER,
                source TEXT DEFAULT 'manual' CHECK(source IN ('auto','manual')),
                text TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS observation_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('inhabitant','plant','hardscape','equipment')),
                entity_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE,
                UNIQUE(observation_id, entity_type, entity_id)
            );

            CREATE TABLE IF NOT EXISTS tank_state_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL UNIQUE,
                summary_text TEXT,
                generated_at TEXT DEFAULT (datetime('now')),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS plants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                species TEXT,
                common_name TEXT,
                added_date TEXT,
                source TEXT,
                notes TEXT,
                status TEXT DEFAULT 'active' CHECK(status IN ('active', 'removed')),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS hardscape (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                item TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                source TEXT,
                cost REAL,
                added_date TEXT,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_test_results_tank_ts ON test_results(tank_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_tank_ts ON events(tank_id, timestamp);

            -- Shared home/source water readings (not tank-scoped). Used as incoming
            -- water context for water-change analysis across all tanks.
            CREATE TABLE IF NOT EXISTS home_water_tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                ph REAL,
                gh REAL,
                kh REAL,
                ammonia REAL,
                nitrite REAL,
                nitrate REAL,
                tds REAL,
                temp REAL,
                sample_point TEXT DEFAULT 'tap'
                    CHECK(sample_point IN (
                        'tap','bottled_spring','bottled_distilled','bottled',
                        'raw','post_neutralizer','post_softener','hose','other'
                    )),
                -- Softener/blend context for well systems that mix hard + soft water.
                -- null = not specified; 'as_used' = normal WC blend; hard/soft/mixed explicit.
                water_blend TEXT
                    CHECK(water_blend IS NULL OR water_blend IN (
                        'as_used','hard','soft','mixed','unknown'
                    )),
                is_lab_test INTEGER DEFAULT 0,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_home_water_tests_ts
                ON home_water_tests(timestamp DESC);

            -- Singleton AI suitability summary for home/source water.
            -- based_on_timestamp = latest WC-source/tap reading (not global max).
            -- based_on_raw_timestamp = latest raw reading (horse section).
            -- Regenerated when either basis drifts from those latest rows.
            CREATE TABLE IF NOT EXISTS home_water_summary (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                summary_text TEXT NOT NULL,
                raw_outdoor_text TEXT,
                based_on_timestamp TEXT,
                based_on_raw_timestamp TEXT,
                generated_at TEXT DEFAULT (datetime('now')),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                target TEXT,
                status TEXT DEFAULT 'in_progress'
                    CHECK(status IN ('open','in_progress','paused','achieved','abandoned')),
                notes TEXT,
                progress_summary TEXT,
                progress_summary_at TEXT,
                sort_order INTEGER DEFAULT 0,
                opened_at TEXT DEFAULT (datetime('now')),
                achieved_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS goal_dependencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                depends_on_goal_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (goal_id) REFERENCES goals(id) ON DELETE CASCADE,
                FOREIGN KEY (depends_on_goal_id) REFERENCES goals(id) ON DELETE CASCADE,
                UNIQUE(goal_id, depends_on_goal_id),
                CHECK(goal_id != depends_on_goal_id)
            );

            CREATE INDEX IF NOT EXISTS idx_observations_tank ON observations(tank_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_issues_tank_status ON issues(tank_id, status);
            CREATE INDEX IF NOT EXISTS idx_goals_tank_status ON goals(tank_id, status);
            CREATE INDEX IF NOT EXISTS idx_goal_deps_goal ON goal_dependencies(goal_id);
            CREATE INDEX IF NOT EXISTS idx_goal_deps_depends ON goal_dependencies(depends_on_goal_id);
            CREATE INDEX IF NOT EXISTS idx_inhabitants_tank ON inhabitants(tank_id);
            CREATE INDEX IF NOT EXISTS idx_population_events_tank ON population_events(tank_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_plants_tank ON plants(tank_id);
            CREATE INDEX IF NOT EXISTS idx_hardscape_tank ON hardscape(tank_id);

            CREATE TABLE IF NOT EXISTS recurring_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                category TEXT NOT NULL CHECK(category IN ('feeding','dosing','maintenance')),
                tracking_mode TEXT NOT NULL DEFAULT 'reference_only' CHECK(tracking_mode IN ('reference_only','logged')),
                day_of_week TEXT CHECK(day_of_week IN ('mon','tue','wed','thu','fri','sat','sun')),
                time_of_day TEXT CHECK(time_of_day IS NULL OR time_of_day IN ('am','pm')),
                description TEXT NOT NULL,
                interval_type TEXT CHECK(interval_type IN ('weekly','monthly','interval_days')),
                interval_days INTEGER,
                last_done TEXT,
                next_due TEXT,
                is_active INTEGER DEFAULT 1,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_schedule_tank ON recurring_schedule(tank_id, is_active, tracking_mode);

            CREATE TABLE IF NOT EXISTS reference_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('species','plant','hardscape')),
                entity_name TEXT NOT NULL,
                common_name TEXT,
                description TEXT,
                care_notes TEXT,
                image_url TEXT,
                image_source TEXT,
                image_attribution TEXT,
                fetched_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(entity_type, entity_name)
            );

            CREATE INDEX IF NOT EXISTS idx_reference_info_lookup ON reference_info(entity_type, entity_name);
            CREATE INDEX IF NOT EXISTS idx_observation_links_obs ON observation_links(observation_id);
            CREATE INDEX IF NOT EXISTS idx_observation_links_entity ON observation_links(entity_type, entity_id);

            -- Pending AI-proposed tank notes updates (user must accept before notes change)
            CREATE TABLE IF NOT EXISTS tank_notes_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                proposed_notes TEXT NOT NULL,
                reason TEXT NOT NULL,
                prior_notes TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','accepted','dismissed')),
                created_at TEXT DEFAULT (datetime('now')),
                resolved_at TEXT,
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_notes_proposals_tank_status
                ON tank_notes_proposals(tank_id, status);

            CREATE TABLE IF NOT EXISTS chat_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tank_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'New conversation',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_conversations_tank
                ON chat_conversations(tank_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (conversation_id) REFERENCES chat_conversations(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
                ON chat_messages(conversation_id, id);

            -- Live-food cultures (not tanks). One purpose per culture
            -- (Daphnia *or* green water, not mixed): green water isn't fed,
            -- and harvest goes to a destination tank, culture, or bin.
            CREATE TABLE IF NOT EXISTS cultures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT DEFAULT 'other' CHECK(kind IN ('daphnia','green_water','other')),
                consumer_tank_id INTEGER,
                destination_culture_id INTEGER,
                destination_vessel_id INTEGER,
                isolation_notes TEXT,
                notes TEXT,
                harvest_status TEXT DEFAULT 'not_ready'
                    CHECK(harvest_status IN ('not_ready','ready')),
                next_action TEXT,
                next_action_date TEXT,
                status TEXT DEFAULT 'active' CHECK(status IN ('active','archived')),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (consumer_tank_id) REFERENCES tanks(id) ON DELETE SET NULL,
                FOREIGN KEY (destination_culture_id) REFERENCES cultures(id) ON DELETE SET NULL,
                FOREIGN KEY (destination_vessel_id) REFERENCES culture_vessels(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS culture_vessels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                culture_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('daphnia','green_water','other')),
                volume_gallons REAL,
                is_lit INTEGER DEFAULT 0,
                is_heated INTEGER DEFAULT 0,
                heater_set_f INTEGER,
                status TEXT DEFAULT 'active' CHECK(status IN ('active','crashed','archived')),
                sort_order INTEGER DEFAULT 0,
                notes TEXT,
                hitchhikers TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (culture_id) REFERENCES cultures(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_culture_vessels_culture
                ON culture_vessels(culture_id, sort_order, id);

            CREATE TABLE IF NOT EXISTS culture_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                culture_id INTEGER NOT NULL,
                timestamp TEXT DEFAULT (datetime('now')),
                kind TEXT NOT NULL CHECK(kind IN (
                    'feed','look','harvest','seed','crash','temp','other'
                )),
                food TEXT CHECK(food IS NULL OR food IN (
                    'spirulina','green_water','yeast','none'
                )),
                amount_text TEXT,
                notes TEXT,
                tint TEXT CHECK(tint IS NULL OR tint IN (
                    'clear','faint','green','soup','milky'
                )),
                density TEXT CHECK(density IS NULL OR density IN (
                    'thin','ok','dense','crash'
                )),
                guts TEXT CHECK(guts IS NULL OR guts IN (
                    'empty_pink','darker','mixed'
                )),
                temp_f REAL,
                temp_kind TEXT CHECK(temp_kind IS NULL OR temp_kind IN ('water','air')),
                rh REAL,
                rh_low REAL,
                rh_high REAL,
                temp_low REAL,
                temp_high REAL,
                held INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (culture_id) REFERENCES cultures(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_culture_log_culture_ts
                ON culture_log(culture_id, timestamp DESC);

            CREATE TABLE IF NOT EXISTS culture_log_vessels (
                log_id INTEGER NOT NULL,
                vessel_id INTEGER NOT NULL,
                tint TEXT,
                density TEXT,
                guts TEXT,
                amount_text TEXT,
                notes TEXT,
                temp_f REAL,
                PRIMARY KEY (log_id, vessel_id),
                FOREIGN KEY (log_id) REFERENCES culture_log(id) ON DELETE CASCADE,
                FOREIGN KEY (vessel_id) REFERENCES culture_vessels(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_culture_log_vessels_vessel
                ON culture_log_vessels(vessel_id);

            CREATE TABLE IF NOT EXISTS culture_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                culture_id INTEGER NOT NULL,
                vessel_id INTEGER,
                category TEXT NOT NULL CHECK(category IN ('feeding','look','maintenance')),
                tracking_mode TEXT NOT NULL DEFAULT 'logged'
                    CHECK(tracking_mode IN ('reference_only','logged')),
                description TEXT NOT NULL,
                interval_days INTEGER,
                last_done TEXT,
                next_due TEXT,
                is_active INTEGER DEFAULT 1,
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (culture_id) REFERENCES cultures(id) ON DELETE CASCADE,
                FOREIGN KEY (vessel_id) REFERENCES culture_vessels(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_culture_schedule_culture
                ON culture_schedule(culture_id, is_active, tracking_mode);
        """)

        # Cultures: kind + harvest destination (tank / other culture / bin)
        cultures_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cultures'"
        ).fetchone()
        if cultures_exists:
            cult_cols = {row[1] for row in conn.execute("PRAGMA table_info(cultures)").fetchall()}
            if "kind" not in cult_cols:
                conn.execute("ALTER TABLE cultures ADD COLUMN kind TEXT DEFAULT 'other'")
            if "destination_culture_id" not in cult_cols:
                conn.execute("ALTER TABLE cultures ADD COLUMN destination_culture_id INTEGER REFERENCES cultures(id) ON DELETE SET NULL")
            if "destination_vessel_id" not in cult_cols:
                conn.execute("ALTER TABLE cultures ADD COLUMN destination_vessel_id INTEGER REFERENCES culture_vessels(id) ON DELETE SET NULL")
            if "harvest_status" not in cult_cols:
                conn.execute("ALTER TABLE cultures ADD COLUMN harvest_status TEXT DEFAULT 'not_ready'")
            if "next_action" not in cult_cols:
                conn.execute("ALTER TABLE cultures ADD COLUMN next_action TEXT")
            if "next_action_date" not in cult_cols:
                conn.execute("ALTER TABLE cultures ADD COLUMN next_action_date TEXT")
            vess_cols = {row[1] for row in conn.execute("PRAGMA table_info(culture_vessels)").fetchall()}
            if vess_cols and "hitchhikers" not in vess_cols:
                conn.execute("ALTER TABLE culture_vessels ADD COLUMN hitchhikers TEXT")
            if vess_cols and "is_heated" not in vess_cols:
                conn.execute("ALTER TABLE culture_vessels ADD COLUMN is_heated INTEGER DEFAULT 0")
            if vess_cols and "heater_set_f" not in vess_cols:
                conn.execute("ALTER TABLE culture_vessels ADD COLUMN heater_set_f INTEGER")
            log_cols = {row[1] for row in conn.execute("PRAGMA table_info(culture_log)").fetchall()}
            if log_cols:
                if "temp_kind" not in log_cols:
                    conn.execute("ALTER TABLE culture_log ADD COLUMN temp_kind TEXT")
                if "rh" not in log_cols:
                    conn.execute("ALTER TABLE culture_log ADD COLUMN rh REAL")
                if "rh_low" not in log_cols:
                    conn.execute("ALTER TABLE culture_log ADD COLUMN rh_low REAL")
                if "rh_high" not in log_cols:
                    conn.execute("ALTER TABLE culture_log ADD COLUMN rh_high REAL")
                if "temp_low" not in log_cols:
                    conn.execute("ALTER TABLE culture_log ADD COLUMN temp_low REAL")
                if "temp_high" not in log_cols:
                    conn.execute("ALTER TABLE culture_log ADD COLUMN temp_high REAL")
                if "held" not in log_cols:
                    conn.execute("ALTER TABLE culture_log ADD COLUMN held INTEGER DEFAULT 0")
            lv_cols = {row[1] for row in conn.execute("PRAGMA table_info(culture_log_vessels)").fetchall()}
            if lv_cols:
                if "tint" not in lv_cols:
                    conn.execute("ALTER TABLE culture_log_vessels ADD COLUMN tint TEXT")
                if "density" not in lv_cols:
                    conn.execute("ALTER TABLE culture_log_vessels ADD COLUMN density TEXT")
                if "guts" not in lv_cols:
                    conn.execute("ALTER TABLE culture_log_vessels ADD COLUMN guts TEXT")
                if "amount_text" not in lv_cols:
                    conn.execute("ALTER TABLE culture_log_vessels ADD COLUMN amount_text TEXT")
                if "notes" not in lv_cols:
                    conn.execute("ALTER TABLE culture_log_vessels ADD COLUMN notes TEXT")
                if "temp_f" not in lv_cols:
                    conn.execute("ALTER TABLE culture_log_vessels ADD COLUMN temp_f REAL")
                    # Looks (and water-temp logs) used to store one station-wide
                    # reading on culture_log. Copy it onto each tagged bin so
                    # heated vs unheated history stays per-bin going forward.
                    conn.execute(
                        """UPDATE culture_log_vessels
                           SET temp_f = (
                               SELECT l.temp_f FROM culture_log l
                               WHERE l.id = culture_log_vessels.log_id
                                 AND l.temp_f IS NOT NULL
                                 AND (l.kind = 'look'
                                      OR COALESCE(l.temp_kind, '') = 'water')
                           )
                           WHERE temp_f IS NULL"""
                    )
                    conn.execute(
                        """UPDATE culture_log
                           SET temp_f = NULL, temp_kind = NULL
                           WHERE kind = 'look' AND temp_f IS NOT NULL
                             AND EXISTS (
                                 SELECT 1 FROM culture_log_vessels lv
                                 WHERE lv.log_id = culture_log.id
                                   AND lv.temp_f IS NOT NULL
                             )"""
                    )

        # Migration: water_blend on home_water_tests (softener mix context for wells)
        hw_cols = {row[1] for row in conn.execute("PRAGMA table_info(home_water_tests)").fetchall()}
        if hw_cols and "water_blend" not in hw_cols:
            conn.execute("ALTER TABLE home_water_tests ADD COLUMN water_blend TEXT")

        # Migration: expand home_water_tests.sample_point CHECK to allow bottled fill sources.
        # SQLite cannot ALTER a CHECK constraint in place — rebuild the table when needed.
        hw_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='home_water_tests'"
        ).fetchone()
        hw_sql = (hw_sql_row[0] or "") if hw_sql_row else ""
        if hw_sql and "bottled_spring" not in hw_sql:
            conn.executescript("""
                CREATE TABLE home_water_tests_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT DEFAULT (datetime('now')),
                    ph REAL,
                    gh REAL,
                    kh REAL,
                    ammonia REAL,
                    nitrite REAL,
                    nitrate REAL,
                    tds REAL,
                    temp REAL,
                    sample_point TEXT DEFAULT 'tap'
                        CHECK(sample_point IN (
                            'tap','bottled_spring','bottled_distilled','bottled',
                            'raw','post_neutralizer','post_softener','hose','other'
                        )),
                    water_blend TEXT
                        CHECK(water_blend IS NULL OR water_blend IN (
                            'as_used','hard','soft','mixed','unknown'
                        )),
                    is_lab_test INTEGER DEFAULT 0,
                    notes TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                INSERT INTO home_water_tests_new
                    (id, timestamp, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp,
                     sample_point, water_blend, is_lab_test, notes, created_at, updated_at)
                SELECT id, timestamp, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp,
                       sample_point, water_blend, is_lab_test, notes, created_at, updated_at
                FROM home_water_tests;
                DROP TABLE home_water_tests;
                ALTER TABLE home_water_tests_new RENAME TO home_water_tests;
                CREATE INDEX IF NOT EXISTS idx_home_water_tests_ts
                    ON home_water_tests(timestamp DESC);
            """)

        # Migration: add schedule_id to events if not present
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "schedule_id" not in cols:
            conn.execute(
                "ALTER TABLE events ADD COLUMN schedule_id INTEGER REFERENCES recurring_schedule(id) ON DELETE SET NULL"
            )

        # Migration: AM/PM tag on recurring feedings (and optional for dosing)
        sched_cols = {row[1] for row in conn.execute("PRAGMA table_info(recurring_schedule)").fetchall()}
        if sched_cols and "time_of_day" not in sched_cols:
            conn.execute(
                "ALTER TABLE recurring_schedule ADD COLUMN time_of_day TEXT "
                "CHECK(time_of_day IS NULL OR time_of_day IN ('am','pm'))"
            )
            # One-shot backfill from wording already in description/notes
            # (e.g. prod notes "Morning feeding" / "Evening feeding").
            text_expr = "lower(coalesce(description,'') || ' ' || coalesce(notes,''))"
            conn.execute(
                f"""UPDATE recurring_schedule SET time_of_day = 'am'
                   WHERE time_of_day IS NULL
                     AND ({text_expr} LIKE '%morning%'
                          OR {text_expr} LIKE '% a.m.%'
                          OR {text_expr} LIKE 'am %'
                          OR {text_expr} LIKE '% am'
                          OR {text_expr} LIKE '% am %')"""
            )
            conn.execute(
                f"""UPDATE recurring_schedule SET time_of_day = 'pm'
                   WHERE time_of_day IS NULL
                     AND ({text_expr} LIKE '%evening%'
                          OR {text_expr} LIKE '%night%'
                          OR {text_expr} LIKE '% p.m.%'
                          OR {text_expr} LIKE 'pm %'
                          OR {text_expr} LIKE '% pm'
                          OR {text_expr} LIKE '% pm %')"""
            )

        # Migration: add monitoring_at to issues if not present
        cols = {row[1] for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
        if "monitoring_at" not in cols:
            conn.execute("ALTER TABLE issues ADD COLUMN monitoring_at TEXT")

        # Migration: goals.progress_summary columns (table may predate them —
        # CREATE TABLE IF NOT EXISTS does not add columns to an existing table)
        goals_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'"
        ).fetchone()
        if goals_exists:
            goal_cols = {row[1] for row in conn.execute("PRAGMA table_info(goals)").fetchall()}
            if "progress_summary" not in goal_cols:
                conn.execute("ALTER TABLE goals ADD COLUMN progress_summary TEXT")
            if "progress_summary_at" not in goal_cols:
                conn.execute("ALTER TABLE goals ADD COLUMN progress_summary_at TEXT")

            # Allow status='paused' (and keep legacy 'open') via CHECK rebuild when needed
            goals_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='goals'"
            ).fetchone()
            goals_sql = (goals_sql_row[0] or "") if goals_sql_row else ""
            if goals_sql and "paused" not in goals_sql:
                conn.executescript("""
                    DROP TABLE IF EXISTS goals_new;
                    CREATE TABLE goals_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tank_id INTEGER NOT NULL,
                        title TEXT NOT NULL,
                        description TEXT,
                        target TEXT,
                        status TEXT DEFAULT 'in_progress'
                            CHECK(status IN ('open','in_progress','paused','achieved','abandoned')),
                        notes TEXT,
                        progress_summary TEXT,
                        progress_summary_at TEXT,
                        sort_order INTEGER DEFAULT 0,
                        opened_at TEXT DEFAULT (datetime('now')),
                        achieved_at TEXT,
                        created_at TEXT DEFAULT (datetime('now')),
                        updated_at TEXT DEFAULT (datetime('now')),
                        FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
                    );
                    INSERT INTO goals_new (
                        id, tank_id, title, description, target, status, notes,
                        progress_summary, progress_summary_at, sort_order,
                        opened_at, achieved_at, created_at, updated_at
                    )
                    SELECT id, tank_id, title, description, target, status, notes,
                           progress_summary, progress_summary_at, sort_order,
                           opened_at, achieved_at, created_at, updated_at
                    FROM goals;
                    DROP TABLE goals;
                    ALTER TABLE goals_new RENAME TO goals;
                    CREATE INDEX IF NOT EXISTS idx_goals_tank_status ON goals(tank_id, status);
                """)

            # Legacy 'open' was the old default; new goals start as in_progress.
            conn.execute(
                "UPDATE goals SET status = 'in_progress' WHERE status = 'open'"
            )

        # Migration: move per-entity observation links (previously 4 nullable FK columns) into
        # the observation_links junction table so one observation can link to multiple entities
        # (e.g. "pruned frogbit, ramshorn snails died, UV light back on" -> 3 links).
        obs_cols = {row[1] for row in conn.execute("PRAGMA table_info(observations)").fetchall()}
        legacy_link_cols = ["related_inhabitant_id", "related_plant_id", "related_hardscape_id", "related_equipment_id"]
        if any(col in obs_cols for col in legacy_link_cols):
            # Read the legacy links into memory first: observation_links has an ON DELETE
            # CASCADE FK to observations, so populating it before the DROP TABLE below would
            # have SQLite cascade-delete everything we just inserted.
            present_cols = [c for c in legacy_link_cols if c in obs_cols]
            legacy_rows = conn.execute(
                f"SELECT id, {', '.join(present_cols)} FROM observations"
            ).fetchall()

            conn.executescript("""
                DROP TABLE IF EXISTS observations_new2;
                CREATE TABLE observations_new2 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tank_id INTEGER NOT NULL,
                    related_event_id INTEGER,
                    related_test_id INTEGER,
                    source TEXT DEFAULT 'manual' CHECK(source IN ('auto','manual','import')),
                    text TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
                );
                INSERT INTO observations_new2 (id, tank_id, related_event_id, related_test_id, source, text, created_at, updated_at)
                    SELECT id, tank_id, related_event_id, related_test_id, source, text, created_at, updated_at FROM observations;
                DROP TABLE observations;
                ALTER TABLE observations_new2 RENAME TO observations;
                CREATE INDEX IF NOT EXISTS idx_observations_tank ON observations(tank_id, created_at);
            """)

            etype_by_col = dict(zip(legacy_link_cols, ["inhabitant", "plant", "hardscape", "equipment"]))
            for row in legacy_rows:
                obs_id = row[0]
                for col, entity_id in zip(present_cols, row[1:]):
                    if entity_id is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO observation_links (observation_id, entity_type, entity_id) VALUES (?,?,?)",
                            (obs_id, etype_by_col[col], entity_id),
                        )

        # Migration: add 'import' to observations.source CHECK constraint
        obs_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='observations'"
        ).fetchone()
        if obs_sql and "'import'" not in obs_sql[0]:
            conn.executescript("""
                DROP TABLE IF EXISTS observations_new;
                CREATE TABLE observations_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tank_id INTEGER NOT NULL,
                    related_event_id INTEGER,
                    related_test_id INTEGER,
                    source TEXT DEFAULT 'manual' CHECK(source IN ('auto','manual','import')),
                    text TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
                );
                INSERT INTO observations_new (id, tank_id, related_event_id, related_test_id, source, text, created_at, updated_at)
                    SELECT id, tank_id, related_event_id, related_test_id, source, text, created_at, updated_at FROM observations;
                DROP TABLE observations;
                ALTER TABLE observations_new RENAME TO observations;
                CREATE INDEX IF NOT EXISTS idx_observations_tank ON observations(tank_id, created_at);
            """)

        # Migration: culture Ask AI — conversations may belong to a tank XOR a culture.
        chat_cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_conversations)").fetchall()}
        if chat_cols and "culture_id" not in chat_cols:
            # Rebuild both chat tables. Drop the child first so ON DELETE CASCADE
            # on chat_messages does not wipe copied rows.
            conn.executescript("""
                DROP TABLE IF EXISTS chat_messages_new;
                DROP TABLE IF EXISTS chat_conversations_new;
                CREATE TABLE chat_conversations_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tank_id INTEGER,
                    culture_id INTEGER,
                    title TEXT NOT NULL DEFAULT 'New conversation',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE,
                    FOREIGN KEY (culture_id) REFERENCES cultures(id) ON DELETE CASCADE,
                    CHECK (
                        (tank_id IS NOT NULL AND culture_id IS NULL)
                        OR (tank_id IS NULL AND culture_id IS NOT NULL)
                    )
                );
                INSERT INTO chat_conversations_new (id, tank_id, culture_id, title, created_at, updated_at)
                    SELECT id, tank_id, NULL, title, created_at, updated_at FROM chat_conversations;
                CREATE TABLE chat_messages_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (conversation_id) REFERENCES chat_conversations_new(id) ON DELETE CASCADE
                );
                INSERT INTO chat_messages_new (id, conversation_id, role, content, created_at)
                    SELECT id, conversation_id, role, content, created_at FROM chat_messages;
                DROP TABLE chat_messages;
                DROP TABLE chat_conversations;
                ALTER TABLE chat_conversations_new RENAME TO chat_conversations;
                ALTER TABLE chat_messages_new RENAME TO chat_messages;
                CREATE INDEX IF NOT EXISTS idx_chat_conversations_tank
                    ON chat_conversations(tank_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_chat_conversations_culture
                    ON chat_conversations(culture_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
                    ON chat_messages(conversation_id, id);
            """)


def init_ref_cache_db():
    """Create (or migrate to) the persistent reference cache DB."""
    with get_ref_db() as ref_conn:
        ref_conn.executescript("""
            CREATE TABLE IF NOT EXISTS reference_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('species','plant','hardscape')),
                entity_name TEXT NOT NULL,
                common_name TEXT,
                scientific_name TEXT,
                description TEXT,
                care_notes TEXT,
                image_url TEXT,
                image_source TEXT,
                image_attribution TEXT,
                fetched_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(entity_type, entity_name)
            );
            CREATE INDEX IF NOT EXISTS idx_reference_info_lookup ON reference_info(entity_type, entity_name);
        """)

        # Migration: add scientific_name column if not present
        ref_cols = {row[1] for row in ref_conn.execute("PRAGMA table_info(reference_info)").fetchall()}
        if "scientific_name" not in ref_cols:
            ref_conn.execute("ALTER TABLE reference_info ADD COLUMN scientific_name TEXT")

        # One-time migration: copy already-fetched rows from main DB into the cache
        try:
            with get_db() as main_conn:
                existing = rows_to_list(main_conn.execute(
                    "SELECT * FROM reference_info WHERE fetched_at IS NOT NULL"
                ).fetchall())
            for row in existing:
                ref_conn.execute(
                    """INSERT OR IGNORE INTO reference_info
                       (entity_type, entity_name, common_name, description, care_notes,
                        image_url, image_source, image_attribution, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (row["entity_type"], row["entity_name"], row.get("common_name"),
                     row.get("description"), row.get("care_notes"), row.get("image_url"),
                     row.get("image_source"), row.get("image_attribution"), row["fetched_at"]),
                )
        except Exception:
            pass  # Main DB may not have reference_info yet; migration is best-effort


def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    return [dict(r) for r in rows]
