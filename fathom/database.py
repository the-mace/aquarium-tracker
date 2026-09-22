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
    _attach_reference_cache(conn)
    try:
        yield conn
    finally:
        conn.close()


def _attach_reference_cache(conn):
    """Expose the reference-cache table as reference_info on a read-only connection.

    Species/plant/hardscape cards live in reference_cache.db. A temp view of that
    name lets Ask AI SELECT them without a second database in the query text.
    """
    path = REFERENCE_CACHE_DB_PATH
    if not path or not os.path.isfile(path):
        return
    uri = "file:" + path + "?mode=ro"
    quoted = uri.replace("'", "''")
    conn.execute(f"ATTACH DATABASE '{quoted}' AS refcache")
    conn.execute(
        "CREATE TEMP VIEW IF NOT EXISTS reference_info AS "
        "SELECT * FROM refcache.reference_info"
    )


def get_schema_text(table_names=None):
    """Table/column listing for Ask AI.

    Main-database tables come from sqlite_master. reference_info is appended
    from the reference cache (it is not a main-database table). schema_migrations
    is omitted. If table_names is given, only those tables are included.
    """
    wanted = set(table_names) if table_names else None
    with get_db_readonly() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        lines = []
        for row in tables:
            name = row[0]
            if name == "schema_migrations":
                continue
            if wanted is not None and name not in wanted:
                continue
            cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
            col_desc = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            lines.append(f"{name}({col_desc})")
        if wanted is None or "reference_info" in wanted:
            if not any(line.startswith("reference_info(") for line in lines):
                try:
                    cols = conn.execute("PRAGMA refcache.table_info(reference_info)").fetchall()
                except sqlite3.Error:
                    cols = []
                if cols:
                    col_desc = ", ".join(f"{c[1]} {c[2]}" for c in cols)
                    lines.append(f"reference_info({col_desc})")
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
    """Create or upgrade the main database.

    Fresh databases get the canonical schema. Databases created before
    schema versions are stamped once they match the current columns.
    See schema.py.
    """
    from schema import ensure_schema
    with get_db() as conn:
        ensure_schema(conn)


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



def row_to_dict(row):
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    return [dict(r) for r in rows]
