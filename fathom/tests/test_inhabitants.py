import sqlite3
import database as _db


def _add(client, tank_id, name="Neon Tetra", count=6, **kwargs):
    data = {"common_name": name, "count": str(count), **kwargs}
    r = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data=data,
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_add_inhabitant_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Betta", "species": "Betta splendens", "count": "1"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    assert "id" in r.json()


def test_add_inhabitant_creates_population_event(client, tank_id):
    inh_id = _add(client, tank_id, name="Corydoras", count=4)
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT event_type, count FROM population_events WHERE inhabitant_id=?", (inh_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "added"
    assert row[1] == 4


def test_add_inhabitant_count_unknown_stores_null(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "MTS Snail", "count_unknown": "on"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT count FROM inhabitants WHERE id=?", (r.json()["id"],)).fetchone()
    conn.close()
    assert row[0] is None


def test_population_event_died_decrements_count(client, tank_id):
    inh_id = _add(client, tank_id, name="Guppy", count=5)
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/event",
        data={"event_type": "died", "count": "2"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["new_count"] == 3


def test_population_event_removed_decrements_count(client, tank_id):
    inh_id = _add(client, tank_id, name="Ember Tetra", count=6)
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/event",
        data={"event_type": "removed", "count": "2"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["new_count"] == 4


def test_population_event_added_increments_count(client, tank_id):
    inh_id = _add(client, tank_id, name="Ember Tetra", count=3)
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/event",
        data={"event_type": "added", "count": "3"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["new_count"] == 6


def test_population_event_born_increments_count(client, tank_id):
    inh_id = _add(client, tank_id, name="Guppy", count=4)
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/event",
        data={"event_type": "born", "count": "2"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["new_count"] == 6


def test_population_event_count_floored_at_zero(client, tank_id):
    inh_id = _add(client, tank_id, name="Shrimp", count=2)
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/event",
        data={"event_type": "died", "count": "10"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["new_count"] == 0


def test_update_inhabitant_count_up_creates_added_event(client, tank_id):
    inh_id = _add(client, tank_id, name="Otocinclus", count=3)
    client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/update",
        data={"common_name": "Otocinclus", "count": "5"},
        follow_redirects=False,
    )
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT event_type, count FROM population_events WHERE inhabitant_id=? ORDER BY id DESC LIMIT 1",
        (inh_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "added"
    assert row[1] == 2


def test_update_inhabitant_count_down_creates_died_event(client, tank_id):
    inh_id = _add(client, tank_id, name="Otocinclus", count=5)
    client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/update",
        data={"common_name": "Otocinclus", "count": "3"},
        follow_redirects=False,
    )
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT event_type, count FROM population_events WHERE inhabitant_id=? ORDER BY id DESC LIMIT 1",
        (inh_id,),
    ).fetchone()
    conn.close()
    assert row[0] == "died"
    assert row[1] == 2


def test_update_inhabitant_no_count_change_no_extra_event(client, tank_id):
    inh_id = _add(client, tank_id, name="Corydoras", count=4)
    client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/update",
        data={"common_name": "Corydoras", "count": "4"},  # same count
        follow_redirects=False,
    )
    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM population_events WHERE inhabitant_id=?", (inh_id,)
    ).fetchone()[0]
    conn.close()
    assert count == 1  # only the original "added" event


def test_delete_inhabitant(client, tank_id):
    inh_id = _add(client, tank_id, name="Guppy", count=3)
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/{inh_id}/delete",
        follow_redirects=False,
    )
    assert r.status_code == 303
    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM inhabitants WHERE id=?", (inh_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_delete_population_event(client, tank_id):
    inh_id = _add(client, tank_id, name="Guppy", count=3)
    conn = sqlite3.connect(_db.DB_PATH)
    pe_id = conn.execute(
        "SELECT id FROM population_events WHERE inhabitant_id=? ORDER BY id DESC LIMIT 1", (inh_id,)
    ).fetchone()[0]
    conn.close()

    r = client.post(f"/tanks/{tank_id}/inhabitants/population-events/{pe_id}/delete")
    assert r.json()["status"] == "deleted"

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM population_events WHERE id=?", (pe_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_population_event_nonexistent_inhabitant_404(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/9999/event",
        data={"event_type": "died", "count": "1"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 404


def test_update_nonexistent_inhabitant_404(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/inhabitants/9999/update",
        data={"common_name": "Ghost", "count": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 404
