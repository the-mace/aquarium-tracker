import sqlite3
import database as _db
import routers.reference_info as _ref


def _seed_ref(entity_type, entity_name, **kwargs):
    conn = sqlite3.connect(_db.REFERENCE_CACHE_DB_PATH)
    conn.execute(
        """INSERT INTO reference_info (entity_type, entity_name, common_name, description, care_notes, image_url, fetched_at)
           VALUES (?,?,?,?,?,?,datetime('now'))
           ON CONFLICT(entity_type, entity_name) DO UPDATE SET
             common_name = excluded.common_name,
             description = excluded.description,
             care_notes  = excluded.care_notes,
             image_url   = excluded.image_url,
             fetched_at  = excluded.fetched_at""",
        (entity_type, entity_name,
         kwargs.get("common_name"), kwargs.get("description"), kwargs.get("care_notes"), kwargs.get("image_url")),
    )
    conn.commit()
    conn.close()


# ── GET /reference-info ────────────────────────────────────────────────────

def test_get_reference_info_404_when_missing(client):
    r = client.get("/reference-info?entity_type=species&entity_name=fakefish")
    assert r.status_code == 404


def test_get_reference_info_returns_row(client, tank_id):
    _seed_ref("species", "betta splendens",
              common_name="Betta", description="Small labyrinth fish")
    r = client.get("/reference-info?entity_type=species&entity_name=betta splendens")
    assert r.status_code == 200
    data = r.json()
    assert data["entity_name"] == "betta splendens"
    assert data["description"] == "Small labyrinth fish"


# ── POST /reference-info/refresh ──────────────────────────────────────────

def test_refresh_queues_task(client):
    r = client.post(
        "/reference-info/refresh",
        json={"entity_type": "species", "entity_name": "betta splendens", "display_name": "Betta"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "refresh_queued"


def test_refresh_clears_fetched_at(client, tank_id):
    _seed_ref("plant", "java moss", description="Hardy moss")
    client.post(
        "/reference-info/refresh",
        json={"entity_type": "plant", "entity_name": "java moss", "display_name": "Java Moss"},
    )
    conn = sqlite3.connect(_db.REFERENCE_CACHE_DB_PATH)
    row = conn.execute("SELECT fetched_at FROM reference_info WHERE entity_name='java moss'").fetchone()
    conn.close()
    assert row[0] is None


def test_refresh_missing_entity_type_returns_400(client):
    r = client.post("/reference-info/refresh", json={"entity_name": "betta"})
    assert r.status_code == 400


# ── maybe_fetch_reference_info inserts placeholder ────────────────────────

def test_maybe_fetch_inserts_placeholder(client, tank_id):
    from unittest.mock import MagicMock
    bt = MagicMock()
    _ref.maybe_fetch_reference_info(bt, "species", "corydoras paleatus", "Peppered Cory")
    conn = sqlite3.connect(_db.REFERENCE_CACHE_DB_PATH)
    row = conn.execute(
        "SELECT entity_type, entity_name, fetched_at FROM reference_info WHERE entity_name='corydoras paleatus'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "species"
    assert row[2] is None  # fetched_at not set yet
    bt.add_task.assert_called_once()


def test_maybe_fetch_skips_if_already_fetched(client, tank_id):
    from unittest.mock import MagicMock
    _seed_ref("species", "neon tetra")  # fetched_at set by _seed_ref
    bt = MagicMock()
    _ref.maybe_fetch_reference_info(bt, "species", "neon tetra", "Neon Tetra")
    bt.add_task.assert_not_called()


def test_maybe_fetch_requeues_stuck_placeholder(client, tank_id):
    """A placeholder row with fetched_at=NULL (e.g. after server restart) must be re-queued."""
    from unittest.mock import MagicMock
    conn = __import__("sqlite3").connect(_db.REFERENCE_CACHE_DB_PATH)
    conn.execute(
        "INSERT INTO reference_info (entity_type, entity_name) VALUES ('species', 'cherry shrimp')"
    )
    conn.commit(); conn.close()
    bt = MagicMock()
    _ref.maybe_fetch_reference_info(bt, "species", "cherry shrimp", "Cherry Shrimp")
    bt.add_task.assert_called_once()  # should re-queue despite row existing


# ── Inhabitants list joins reference_info ─────────────────────────────────

def test_inhabitants_list_shows_ref_data(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Betta", "species": "Betta splendens", "count": "1"},
    )
    _seed_ref("species", "betta splendens",
              common_name="Betta", description="Labyrinth fish",
              image_url="https://upload.wikimedia.org/test.jpg")
    r = client.get(f"/tanks/{tank_id}/inhabitants", follow_redirects=True)
    assert r.status_code == 200
    assert "upload.wikimedia.org" in r.text


# ── Plants list joins reference_info ─────────────────────────────────────

def test_plants_list_shows_ref_data(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/plants",
        data={"common_name": "Java Moss", "species": "Taxiphyllum barbieri"},
    )
    _seed_ref("plant", "taxiphyllum barbieri",
              description="Hardy carpet moss",
              image_url="https://upload.wikimedia.org/moss.jpg")
    r = client.get(f"/tanks/{tank_id}/plants", follow_redirects=True)
    assert r.status_code == 200
    assert "upload.wikimedia.org" in r.text
