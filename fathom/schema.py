"""Main-database schema and versioned upgrades.

SCHEMA_VERSION is the latest migration. A new database runs CANONICAL_SCHEMA
and is stamped at version 1. A database last opened by the pre-version
startup (tables present, no schema_migrations) is checked for the current
columns, then stamped at version 1 after the one leftover step: move
reference_info into the cache database and drop it here.

Add the next change as a function in _apply_pending and bump SCHEMA_VERSION.
Recorded versions are not run again.
"""

SCHEMA_VERSION = 1

# Columns the previous startup migrations left on every database that has
# been opened by current code. A legacy file missing one of these is older
# than that and must not be stamped as current.
_SENTINELS = (
    ("events", "schedule_id"),
    ("chat_conversations", "culture_id"),
    ("culture_log", "held"),
    ("culture_log_vessels", "temp_f"),
    ("goals", "progress_summary"),
    ("home_water_tests", "water_blend"),
    ("culture_vessels", "heater_set_f"),
    ("recurring_schedule", "time_of_day"),
)

CANONICAL_SCHEMA = """
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
                schedule_id INTEGER REFERENCES recurring_schedule(id) ON DELETE SET NULL,
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
                source TEXT DEFAULT 'manual' CHECK(source IN ('auto','manual','import')),
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
            CREATE INDEX IF NOT EXISTS idx_chat_conversations_tank
                ON chat_conversations(tank_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_conversations_culture
                ON chat_conversations(culture_id, updated_at DESC);

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
"""


def ensure_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    current = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    has_tanks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tanks'"
    ).fetchone()
    if current == 0 and has_tanks:
        _assert_legacy_is_current(conn)
        _move_reference_info_to_cache(conn)
        _normalize_goal_status(conn)
        _stamp(conn, 1)
        current = 1
    elif current == 0:
        conn.executescript(CANONICAL_SCHEMA)
        _stamp(conn, 1)
        current = 1
    _apply_pending(conn, current)


def _apply_pending(conn, current):
    """Migrations after the version-1 baseline. Each bumps current."""
    return


def _stamp(conn, version):
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
        (version,),
    )


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _assert_legacy_is_current(conn):
    missing = []
    for table, col in _SENTINELS:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not present:
            missing.append(f"table {table}")
            continue
        if col not in _columns(conn, table):
            missing.append(f"{table}.{col}")
    obs = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='observations'"
    ).fetchone()
    if not obs or "'import'" not in (obs[0] or ""):
        missing.append("observations.source import")
    if missing:
        raise RuntimeError(
            "Database is missing current columns ("
            + ", ".join(missing)
            + "). It was not fully migrated by the previous startup schema, "
            "so it was not stamped. Open it once with the last release from "
            "before schema versions, then upgrade again."
        )


def _normalize_goal_status(conn):
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'"
    ).fetchone():
        conn.execute(
            "UPDATE goals SET status = 'in_progress' WHERE status = 'open'"
        )


def _move_reference_info_to_cache(conn):
    """Copy fetched species/plant/hardscape cards off the main database."""
    from database import get_ref_db, init_ref_cache_db, rows_to_list

    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reference_info'"
    ).fetchone()
    if not exists:
        return
    init_ref_cache_db()
    rows = rows_to_list(conn.execute(
        "SELECT * FROM reference_info WHERE fetched_at IS NOT NULL"
    ).fetchall())
    if rows:
        with get_ref_db() as ref_conn:
            for row in rows:
                ref_conn.execute(
                    """INSERT OR IGNORE INTO reference_info
                       (entity_type, entity_name, common_name, description, care_notes,
                        image_url, image_source, image_attribution, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        row["entity_type"], row["entity_name"], row.get("common_name"),
                        row.get("description"), row.get("care_notes"), row.get("image_url"),
                        row.get("image_source"), row.get("image_attribution"),
                        row["fetched_at"],
                    ),
                )
    conn.execute("DROP TABLE reference_info")
