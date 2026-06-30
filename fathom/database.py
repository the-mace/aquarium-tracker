import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "data", "fathom.db"),
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
                INSERT INTO observations_new SELECT * FROM observations;
                DROP TABLE observations;
                ALTER TABLE observations_new RENAME TO observations;
                CREATE INDEX IF NOT EXISTS idx_observations_tank ON observations(tank_id, created_at);
            """)

        seed_schedule_data(conn)


def seed_schedule_data(conn):
    """One-time seed of schedule entries for known production tanks."""
    tank_5g = conn.execute("SELECT id FROM tanks WHERE name='Fish Tank 5G'").fetchone()
    if tank_5g and conn.execute(
        "SELECT COUNT(*) FROM recurring_schedule WHERE tank_id=?", (tank_5g[0],)
    ).fetchone()[0] == 0:
        tid = tank_5g[0]
        conn.executemany(
            """INSERT INTO recurring_schedule
               (tank_id, category, tracking_mode, day_of_week, description,
                interval_type, interval_days, last_done, next_due, is_active, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (tid, 'feeding', 'reference_only', 'mon', 'Snowflake — tiny fragment',           None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'tue', 'No feeding',                           None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'wed', 'Shrimp Cuisine 1-2 pellets',           None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'wed', 'Bacter AE 1/16 tsp',                  None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'wed', 'Zucchini',                             None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'thu', 'No feeding (test day)',                None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'fri', 'No feeding',                           None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'sat', 'Algae wafer 1/2 mini + Zucchini',     None, None, None, None, 1, None),
                (tid, 'feeding', 'reference_only', 'sun', 'Bacter AE 1/16 tsp',                  None, None, None, None, 1, None),
                (tid, 'dosing',  'reference_only', 'thu', '5ml Flourish weekly',                  None, None, None, None, 1, None),
            ],
        )

    # 40G tank — seeded by volume; update name lookup once tank exists in dev DB
    tank_40g = conn.execute(
        "SELECT id FROM tanks WHERE CAST(volume_gallons AS INTEGER) BETWEEN 38 AND 45 LIMIT 1"
    ).fetchone()
    if tank_40g and conn.execute(
        "SELECT COUNT(*) FROM recurring_schedule WHERE tank_id=?", (tank_40g[0],)
    ).fetchone()[0] == 0:
        tid = tank_40g[0]
        conn.executemany(
            """INSERT INTO recurring_schedule
               (tank_id, category, tracking_mode, day_of_week, description,
                interval_type, interval_days, last_done, next_due, is_active, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (tid, 'feeding',     'reference_only', 'mon', 'Vibra Bites',                                    None,           None, None,         None,         1, None),
                (tid, 'feeding',     'reference_only', 'tue', 'Vibra Bites',                                    None,           None, None,         None,         1, None),
                (tid, 'feeding',     'reference_only', 'wed', 'Vibra Bites + Zucchini',                         None,           None, None,         None,         1, None),
                (tid, 'feeding',     'reference_only', 'thu', 'Vibra Bites',                                    None,           None, None,         None,         1, None),
                (tid, 'feeding',     'reference_only', 'fri', 'Vibra Bites',                                    None,           None, None,         None,         1, None),
                (tid, 'feeding',     'reference_only', 'sat', 'Vibra Bites + Zucchini',                         None,           None, None,         None,         1, None),
                (tid, 'feeding',     'reference_only', 'sun', 'Vibra Bites',                                    None,           None, None,         None,         1, None),
                (tid, 'dosing',      'reference_only', 'thu', '2/3 cap Flourish + 1/2 cap Potassium + 1/2 cap Iron, weekly', None, None, None,   None,         1, None),
                (tid, 'maintenance', 'logged',         None,  'Clean pre-filter',                               'interval_days', 30, '2026-05-29', '2026-07-02', 1, None),
                (tid, 'maintenance', 'logged',         None,  'Clean all stages',                               'interval_days', 75, '2026-05-29', '2026-08-13', 1, None),
                (tid, 'maintenance', 'logged',         None,  'Squeeze out filter intake sponge',               None,           None, None,         None,         1, 'Frequency unclear — no auto-tracking, manual reminder only'),
            ],
        )


def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    return [dict(r) for r in rows]
