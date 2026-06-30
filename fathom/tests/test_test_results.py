import sqlite3
import database as _db


def test_add_test_result_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.2", "gh": "8", "kh": "5", "ammonia": "0",
              "nitrite": "0", "nitrate": "10", "temp": "76"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    body = r.json()
    assert "id" in body
    assert body["status"] == "created"


def test_add_test_result_persisted_to_db(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "6.8", "nitrate": "20"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT ph, nitrate FROM test_results WHERE id=?", (result_id,)).fetchone()
    conn.close()
    assert abs(row[0] - 6.8) < 0.001
    assert abs(row[1] - 20.0) < 0.001


def test_add_test_result_with_explicit_timestamp(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.0", "timestamp": "2026-01-15 12:00:00"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT timestamp FROM test_results WHERE id=?", (r.json()["id"],)).fetchone()
    conn.close()
    assert row[0] == "2026-01-15 12:00:00"


def test_add_test_result_redirects_without_json_accept(client, tank_id):
    r = client.post(f"/tanks/{tank_id}/tests", data={"ph": "7.0"}, follow_redirects=False)
    assert r.status_code == 303


def test_list_tests_page(client, tank_id):
    assert client.get(f"/tanks/{tank_id}/tests").status_code == 200


def test_delete_test_result(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.5"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]
    r2 = client.delete(f"/tanks/{tank_id}/tests/{result_id}")
    assert r2.json()["status"] == "deleted"

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_delete_test_result_wrong_tank_leaves_row(client, make_tank):
    tid1 = make_tank(name="Tank A")
    tid2 = make_tank(name="Tank B")
    r = client.post(
        f"/tanks/{tid1}/tests",
        data={"ph": "7.0"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]
    client.delete(f"/tanks/{tid2}/tests/{result_id}")  # wrong tank_id

    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert count == 1  # row untouched


def test_multiple_test_results_ordered_by_chart(client, tank_id):
    for ph in [7.0, 7.2, 7.4]:
        client.post(
            f"/tanks/{tank_id}/tests",
            data={"ph": str(ph), "timestamp": f"2026-0{int(ph*10-69)}-01 00:00:00"},
            headers={"Accept": "application/json"},
        )
    data = client.get(f"/tanks/{tank_id}/charts/water-params").json()["data"]
    phs = [row["ph"] for row in data]
    assert phs == sorted(phs)  # chart returns chronological order (oldest first)
