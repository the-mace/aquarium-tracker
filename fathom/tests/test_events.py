import sqlite3
import database as _db


def test_add_event_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "water_change", "amount": "3.0", "notes": "30% change"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "created"
    assert "id" in r.json()


def test_add_event_persisted(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "treatment"},
        headers={"Accept": "application/json"},
    )
    eid = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT event_type FROM events WHERE id=?", (eid,)).fetchone()
    conn.close()
    assert row[0] == "treatment"


def test_add_event_with_explicit_timestamp(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "feeding", "timestamp": "2026-02-01 08:00:00"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT timestamp FROM events WHERE id=?", (r.json()["id"],)).fetchone()
    conn.close()
    assert row[0] == "2026-02-01 08:00:00"


def test_add_event_redirects_without_json_accept(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "other"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_list_events_returns_json(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "feeding"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/events")
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    assert len(body["events"]) >= 1


def test_list_events_limit_param(client, tank_id):
    for _ in range(5):
        client.post(
            f"/tanks/{tank_id}/events",
            data={"event_type": "feeding"},
            headers={"Accept": "application/json"},
        )
    body = client.get(f"/tanks/{tank_id}/events?limit=3").json()
    assert len(body["events"]) == 3


def test_delete_event(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "maintenance"},
        headers={"Accept": "application/json"},
    )
    eid = r.json()["id"]
    r2 = client.delete(f"/tanks/{tank_id}/events/{eid}")
    assert r2.json()["status"] == "deleted"

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM events WHERE id=?", (eid,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_delete_event_wrong_tank_leaves_row(client, make_tank):
    tid1 = make_tank(name="Tank A")
    tid2 = make_tank(name="Tank B")
    r = client.post(
        f"/tanks/{tid1}/events",
        data={"event_type": "feeding"},
        headers={"Accept": "application/json"},
    )
    eid = r.json()["id"]
    client.delete(f"/tanks/{tid2}/events/{eid}")  # wrong tank

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM events WHERE id=?", (eid,)).fetchone()[0]
    conn.close()
    assert count == 1
