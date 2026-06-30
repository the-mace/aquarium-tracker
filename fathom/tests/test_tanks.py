import sqlite3
import database as _db


def test_list_tanks_empty(client):
    r = client.get("/tanks")
    assert r.status_code == 200


def test_create_tank_redirects_to_detail(client):
    r = client.post(
        "/tanks",
        data={"name": "Nano Tank", "water_type": "fresh", "volume_gallons": "5"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/tanks/" in r.headers["location"]


def test_create_tank_appears_in_list(client, make_tank):
    make_tank(name="Reef Tank")
    assert b"Reef Tank" in client.get("/tanks").content


def test_tank_detail_200(client, tank_id):
    assert client.get(f"/tanks/{tank_id}").status_code == 200


def test_tank_detail_404(client):
    assert client.get("/tanks/9999").status_code == 404


def test_edit_tank_form_200(client, tank_id):
    assert client.get(f"/tanks/{tank_id}/edit").status_code == 200


def test_edit_tank_redirects(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/edit",
        data={"name": "Renamed Tank", "water_type": "salt", "status": "active"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert f"/tanks/{tank_id}" in r.headers["location"]


def test_edit_tank_persists_change(client, make_tank):
    tid = make_tank(name="Old Name")
    client.post(
        f"/tanks/{tid}/edit",
        data={"name": "New Name", "water_type": "fresh", "status": "active"},
        follow_redirects=False,
    )
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT name FROM tanks WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row[0] == "New Name"


def test_delete_tank_wrong_confirmation_is_rejected(client, make_tank):
    tid = make_tank(name="Important Tank")
    r = client.post(f"/tanks/{tid}/delete", data={"confirmation": "wrong"})
    assert r.status_code == 400
    assert client.get(f"/tanks/{tid}").status_code == 200


def test_delete_tank_correct_confirmation(client, make_tank):
    tid = make_tank(name="Doomed Tank")
    r = client.post(
        f"/tanks/{tid}/delete",
        data={"confirmation": "Doomed Tank"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/tanks/{tid}").status_code == 404


def test_delete_nonexistent_tank_404(client):
    assert client.post("/tanks/9999/delete", data={"confirmation": "anything"}).status_code == 404


def test_delete_tank_cascades_to_child_tables(client, make_tank):
    tid = make_tank(name="Cascade Tank")
    client.post(f"/tanks/{tid}/tests", data={"ph": "7.0"}, headers={"Accept": "application/json"})
    client.post(f"/tanks/{tid}/events", data={"event_type": "feeding"}, headers={"Accept": "application/json"})
    client.post(f"/tanks/{tid}/delete", data={"confirmation": "Cascade Tank"}, follow_redirects=False)

    conn = sqlite3.connect(_db.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM test_results WHERE tank_id=?", (tid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE tank_id=?", (tid,)).fetchone()[0] == 0
    conn.close()


def test_reset_tank_clears_child_data(client, make_tank):
    tid = make_tank(name="Reset Tank")
    client.post(f"/tanks/{tid}/tests", data={"ph": "7.0"}, headers={"Accept": "application/json"})
    r = client.post(
        f"/tanks/{tid}/reset",
        data={"confirmation": "Reset Tank"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get(f"/tanks/{tid}").status_code == 200  # tank still exists

    conn = sqlite3.connect(_db.DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM test_results WHERE tank_id=?", (tid,)).fetchone()[0] == 0
    conn.close()


def test_reset_tank_wrong_confirmation_rejected(client, make_tank):
    tid = make_tank(name="Safe Tank")
    assert client.post(f"/tanks/{tid}/reset", data={"confirmation": "wrong"}).status_code == 400


def test_chart_water_params_empty(client, tank_id):
    r = client.get(f"/tanks/{tank_id}/charts/water-params")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_chart_water_params_with_data(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.2", "temp": "76"},
        headers={"Accept": "application/json"},
    )
    data = client.get(f"/tanks/{tank_id}/charts/water-params").json()["data"]
    assert len(data) == 1
    assert abs(data[0]["ph"] - 7.2) < 0.001
    assert abs(data[0]["temp"] - 76.0) < 0.001


def test_chart_water_params_limit(client, tank_id):
    for ph in range(5):
        client.post(
            f"/tanks/{tank_id}/tests",
            data={"ph": str(6.0 + ph * 0.1)},
            headers={"Accept": "application/json"},
        )
    data = client.get(f"/tanks/{tank_id}/charts/water-params?limit=3").json()["data"]
    assert len(data) == 3


def test_chart_population_shape(client, tank_id):
    body = client.get(f"/tanks/{tank_id}/charts/population").json()
    assert "events" in body and "current" in body


def test_chart_costs_shape(client, tank_id):
    body = client.get(f"/tanks/{tank_id}/charts/costs").json()
    assert "by_category" in body and "by_month" in body


def test_chart_costs_aggregates_by_category(client, tank_id):
    for item, cat, cost in [("Filter", "equipment", "25"), ("Food", "food", "10"), ("Heater", "equipment", "40")]:
        client.post(
            f"/tanks/{tank_id}/purchases",
            data={"item": item, "category": cat, "cost": cost},
            headers={"Accept": "application/json"},
        )
    rows = client.get(f"/tanks/{tank_id}/charts/costs").json()["by_category"]
    cats = {r["category"]: r["total"] for r in rows}
    assert abs(cats["equipment"] - 65.0) < 0.001
    assert abs(cats["food"] - 10.0) < 0.001
