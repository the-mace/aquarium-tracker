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


def test_add_observation_with_link_ref_sets_related_id(client, tank_id):
    inh = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Amano Shrimp", "species": "Caridina multidentata", "count": "5"},
        headers={"Accept": "application/json"},
    ).json()
    r = client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Very active today", "link_ref": f"inhabitant:{inh['id']}"},
        headers={"Accept": "application/json"},
    )
    obs_id = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT related_inhabitant_id, related_plant_id, related_hardscape_id, related_equipment_id"
        " FROM observations WHERE id=?", (obs_id,),
    ).fetchone()
    conn.close()
    assert row == (inh["id"], None, None, None)


def test_observations_filtered_by_link_type_and_id(client, tank_id):
    inh = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Amano Shrimp", "count": "5"},
        headers={"Accept": "application/json"},
    ).json()
    client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Linked note", "link_ref": f"inhabitant:{inh['id']}"},
    )
    client.post(f"/tanks/{tank_id}/observations", data={"text": "Unrelated note"})

    r = client.get(f"/tanks/{tank_id}/observations?link_type=inhabitant&link_id={inh['id']}")
    assert r.status_code == 200
    assert "Linked note" in r.text
    assert "Unrelated note" not in r.text
    assert "Showing notes for" in r.text


def test_observations_invalid_link_id_falls_back_to_unfiltered(client, tank_id):
    client.post(f"/tanks/{tank_id}/observations", data={"text": "General note"})
    r = client.get(f"/tanks/{tank_id}/observations?link_type=inhabitant&link_id=999999")
    assert r.status_code == 200
    assert "General note" in r.text
    assert "Showing notes for" not in r.text


def test_observations_filtered_by_link_ref(client, tank_id):
    inh = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Amano Shrimp", "count": "5"},
        headers={"Accept": "application/json"},
    ).json()
    client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Linked note", "link_ref": f"inhabitant:{inh['id']}"},
    )
    client.post(f"/tanks/{tank_id}/observations", data={"text": "Unrelated note"})

    r = client.get(f"/tanks/{tank_id}/observations?link_ref=inhabitant:{inh['id']}")
    assert "Linked note" in r.text
    assert "Unrelated note" not in r.text


def test_observations_filtered_by_link_ref_any(client, tank_id):
    inh = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Amano Shrimp", "count": "5"},
        headers={"Accept": "application/json"},
    ).json()
    client.post(f"/tanks/{tank_id}/observations", data={"text": "Linked note", "link_ref": f"inhabitant:{inh['id']}"})
    client.post(f"/tanks/{tank_id}/observations", data={"text": "Unrelated note"})

    r = client.get(f"/tanks/{tank_id}/observations?link_ref=any")
    assert "Linked note" in r.text
    assert "Unrelated note" not in r.text


def test_observations_filtered_by_link_ref_none(client, tank_id):
    inh = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Amano Shrimp", "count": "5"},
        headers={"Accept": "application/json"},
    ).json()
    client.post(f"/tanks/{tank_id}/observations", data={"text": "Linked note", "link_ref": f"inhabitant:{inh['id']}"})
    client.post(f"/tanks/{tank_id}/observations", data={"text": "Unrelated note"})

    r = client.get(f"/tanks/{tank_id}/observations?link_ref=none")
    assert "Unrelated note" in r.text
    assert "Linked note" not in r.text


def test_set_observation_link_on_existing(client, tank_id):
    inh = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Amano Shrimp", "count": "5"},
        headers={"Accept": "application/json"},
    ).json()
    obs = client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "General note"},
        headers={"Accept": "application/json"},
    ).json()

    r = client.post(f"/tanks/{tank_id}/observations/{obs['id']}/link", data={"link_ref": f"inhabitant:{inh['id']}"})
    assert r.status_code == 200

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT related_inhabitant_id FROM observations WHERE id=?", (obs["id"],)).fetchone()
    conn.close()
    assert row[0] == inh["id"]


def test_clear_observation_link(client, tank_id):
    inh = client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Amano Shrimp", "count": "5"},
        headers={"Accept": "application/json"},
    ).json()
    obs = client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Linked note", "link_ref": f"inhabitant:{inh['id']}"},
        headers={"Accept": "application/json"},
    ).json()

    r = client.post(f"/tanks/{tank_id}/observations/{obs['id']}/link", data={"link_ref": ""})
    assert r.status_code == 200

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT related_inhabitant_id FROM observations WHERE id=?", (obs["id"],)).fetchone()
    conn.close()
    assert row[0] is None


def test_set_observation_link_404_for_unknown_observation(client, tank_id):
    r = client.post(f"/tanks/{tank_id}/observations/999999/link", data={"link_ref": ""})
    assert r.status_code == 404


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
