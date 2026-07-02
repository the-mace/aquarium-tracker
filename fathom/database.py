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
                related_inhabitant_id INTEGER,
                related_plant_id INTEGER,
                related_hardscape_id INTEGER,
                related_equipment_id INTEGER,
                source TEXT DEFAULT 'manual' CHECK(source IN ('auto','manual')),
                text TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (tank_id) REFERENCES tanks(id) ON DELETE CASCADE
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
            CREATE INDEX IF NOT EXISTS idx_observations_tank ON observations(tank_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_issues_tank_status ON issues(tank_id, status);
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
        """)

        # Migration: add schedule_id to events if not present
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if "schedule_id" not in cols:
            conn.execute(
                "ALTER TABLE events ADD COLUMN schedule_id INTEGER REFERENCES recurring_schedule(id) ON DELETE SET NULL"
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

        # Migration: add entity-linkage columns to observations if not present
        obs_cols = {row[1] for row in conn.execute("PRAGMA table_info(observations)").fetchall()}
        for col in ("related_inhabitant_id", "related_plant_id", "related_hardscape_id", "related_equipment_id"):
            if col not in obs_cols:
                conn.execute(f"ALTER TABLE observations ADD COLUMN {col} INTEGER")


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
