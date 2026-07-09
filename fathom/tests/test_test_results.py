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


def test_add_test_result_with_empty_string_fields(client, tank_id):
    # Real browser form submits every field, including untouched ones, as "".
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"timestamp": "", "ph": "7.2", "gh": "", "kh": "", "ammonia": "0",
              "nitrite": "0", "nitrate": "10", "tds": "", "temp": "", "notes": ""},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    result_id = r.json()["id"]
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT ph, gh, tds FROM test_results WHERE id=?", (result_id,)).fetchone()
    conn.close()
    assert abs(row[0] - 7.2) < 0.001
    assert row[1] is None
    assert row[2] is None


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


def test_add_test_result_redirects_to_dashboard_with_saved_flag(client, tank_id):
    r = client.post(f"/tanks/{tank_id}/tests", data={"ph": "7.0"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/tanks/{tank_id}?saved=test"


def test_add_test_result_return_to_tests_still_honored(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.0", "return_to": "tests"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/tanks/{tank_id}/tests"


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


def test_update_test_result(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.0", "nitrate": "10"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]
    r2 = client.post(
        f"/tanks/{tank_id}/tests/{result_id}/update",
        data={"ph": "7.5", "nitrate": "20", "notes": "corrected reading"},
    )
    assert r2.json()["status"] == "updated"

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT ph, nitrate, notes FROM test_results WHERE id=?", (result_id,)).fetchone()
    conn.close()
    assert abs(row[0] - 7.5) < 0.001
    assert abs(row[1] - 20.0) < 0.001
    assert row[2] == "corrected reading"


def test_update_test_result_timestamp(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.0", "timestamp": "2026-01-01 00:00:00"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]
    client.post(
        f"/tanks/{tank_id}/tests/{result_id}/update",
        data={"ph": "7.0", "timestamp": "2026-02-15 08:30:00"},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT timestamp FROM test_results WHERE id=?", (result_id,)).fetchone()
    conn.close()
    assert row[0] == "2026-02-15 08:30:00"


def test_update_test_result_wrong_tank_404s(client, make_tank):
    tid1 = make_tank(name="Tank A")
    tid2 = make_tank(name="Tank B")
    r = client.post(
        f"/tanks/{tid1}/tests",
        data={"ph": "7.0"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]
    r2 = client.post(f"/tanks/{tid2}/tests/{result_id}/update", data={"ph": "9.0"})
    assert r2.status_code == 404

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT ph FROM test_results WHERE id=?", (result_id,)).fetchone()
    conn.close()
    assert abs(row[0] - 7.0) < 0.001


def test_new_test_form_no_prior_tests(client, tank_id):
    r = client.get(f"/tanks/{tank_id}/tests/new")
    assert r.status_code == 200
    assert 'name="ph"' in r.text


def test_new_test_form_prefills_from_latest(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.3", "nitrate": "15"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/tests/new")
    assert r.status_code == 200
    assert 'value="7.3"' in r.text
    assert 'value="15.0"' in r.text


def test_new_test_form_prefills_from_most_recent_of_several(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "6.5", "timestamp": "2026-01-01 00:00:00"},
        headers={"Accept": "application/json"},
    )
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.9", "timestamp": "2026-02-01 00:00:00"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/tests/new")
    assert 'value="7.9"' in r.text
    assert 'value="6.5"' not in r.text


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
