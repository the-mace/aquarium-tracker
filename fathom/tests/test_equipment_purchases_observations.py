"""Coverage for equipment, purchases, and observations routers."""
import json
import sqlite3
import database as _db


# ── Equipment ──────────────────────────────────────────────────────────────

def test_add_equipment_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/equipment",
        data={"category": "filter", "brand": "AquaClear", "model": "20"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "created"
    assert "id" in r.json()


def test_add_equipment_free_text_specs_wrapped_as_json(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/equipment",
        data={"category": "heater", "specs": "50W adjustable"},
        headers={"Accept": "application/json"},
    )
    eq_id = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT specs FROM tank_equipment WHERE id=?", (eq_id,)).fetchone()
    conn.close()
    parsed = json.loads(row[0])
    assert "50W adjustable" in parsed.get("description", "")


def test_add_equipment_valid_json_specs_stored_verbatim(client, tank_id):
    specs = json.dumps({"flow_rate": "200gph", "media": "sponge"})
    r = client.post(
        f"/tanks/{tank_id}/equipment",
        data={"category": "filter", "specs": specs},
        headers={"Accept": "application/json"},
    )
    eq_id = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT specs FROM tank_equipment WHERE id=?", (eq_id,)).fetchone()
    conn.close()
    assert json.loads(row[0])["flow_rate"] == "200gph"


def test_equipment_list_page(client, tank_id):
    assert client.get(f"/tanks/{tank_id}/equipment").status_code == 200


def test_delete_equipment(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/equipment",
        data={"category": "light"},
        headers={"Accept": "application/json"},
    )
    eq_id = r.json()["id"]
    r2 = client.post(f"/tanks/{tank_id}/equipment/{eq_id}/delete", follow_redirects=False)
    assert r2.status_code == 303

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM tank_equipment WHERE id=?", (eq_id,)).fetchone()[0]
    conn.close()
    assert count == 0


# ── Observations ──────────────────────────────────────────────────────────

def test_add_observation_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Fish are active"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    assert "id" in r.json()


def test_add_observation_stored_as_manual(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Water crystal clear"},
        headers={"Accept": "application/json"},
    )
    obs_id = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT source, text FROM observations WHERE id=?", (obs_id,)).fetchone()
    conn.close()
    assert row[0] == "manual"
    assert row[1] == "Water crystal clear"


def test_list_observations_json(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Planted new stems"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/observations/json")
    assert r.status_code == 200
    body = r.json()
    assert "observations" in body
    assert len(body["observations"]) >= 1


def test_list_observations_json_limit(client, tank_id):
    for i in range(5):
        client.post(
            f"/tanks/{tank_id}/observations",
            data={"text": f"Note {i}"},
            headers={"Accept": "application/json"},
        )
    body = client.get(f"/tanks/{tank_id}/observations/json?limit=3").json()
    assert len(body["observations"]) == 3


def test_delete_observation(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Temporary note"},
        headers={"Accept": "application/json"},
    )
    obs_id = r.json()["id"]
    r2 = client.post(f"/tanks/{tank_id}/observations/{obs_id}/delete")
    assert r2.json()["status"] == "deleted"

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM observations WHERE id=?", (obs_id,)).fetchone()[0]
    conn.close()
    assert count == 0


# ── Purchases ─────────────────────────────────────────────────────────────

def test_add_purchase_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/purchases",
        data={"item": "Filter media", "category": "consumables", "cost": "12.50"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    assert r.json()["status"] == "created"


def test_add_purchase_persisted(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/purchases",
        data={"item": "Substrate", "category": "other", "cost": "18.00"},
        headers={"Accept": "application/json"},
    )
    pid = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT item, cost FROM purchases WHERE id=?", (pid,)).fetchone()
    conn.close()
    assert row[0] == "Substrate"
    assert abs(row[1] - 18.0) < 0.001


def test_list_purchases_page(client, tank_id):
    assert client.get(f"/tanks/{tank_id}/purchases").status_code == 200


def test_delete_purchase(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/purchases",
        data={"item": "Old item", "category": "other"},
        headers={"Accept": "application/json"},
    )
    pid = r.json()["id"]
    r2 = client.post(f"/purchases/{pid}/delete", follow_redirects=False)
    assert r2.status_code == 303

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM purchases WHERE id=?", (pid,)).fetchone()[0]
    conn.close()
    assert count == 0
