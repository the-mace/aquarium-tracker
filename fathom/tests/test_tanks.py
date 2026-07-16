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


def test_create_tank_name_only(client):
    # Empty string fields for optional floats must not produce a 422
    r = client.post(
        "/tanks",
        data={"name": "Name Only Tank"},
        follow_redirects=False,
    )
    assert r.status_code == 303


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


def test_chart_population_current_includes_unknown_count_inhabitants(client, tank_id):
    # A species whose count later becomes "many"/unknown (count IS NULL) must still
    # appear in `current` — the frontend chart uses it as the authoritative "today"
    # value instead of a stale summed total. Previously `count > 0` filtered it out.
    client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={"common_name": "Bladder Snail", "count_unknown": "on"},
        headers={"Accept": "application/json"},
    )
    body = client.get(f"/tanks/{tank_id}/charts/population").json()
    names = {c["common_name"]: c["count"] for c in body["current"]}
    assert names.get("Bladder Snail") is None
    assert "Bladder Snail" in names


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


def _insert_pending_proposal(tank_id, proposed, reason="stale notes", prior="old notes"):
    conn = sqlite3.connect(_db.DB_PATH)
    cur = conn.execute(
        """INSERT INTO tank_notes_proposals
           (tank_id, proposed_notes, reason, prior_notes, status)
           VALUES (?, ?, ?, ?, 'pending')""",
        (tank_id, proposed, reason, prior),
    )
    conn.commit()
    pid = cur.lastrowid
    conn.close()
    return pid


def test_dashboard_shows_pending_notes_proposal(client, tank_id):
    _insert_pending_proposal(tank_id, "New notes with tap water", "Events use tap water")
    r = client.get(f"/tanks/{tank_id}")
    assert r.status_code == 200
    assert b"Update tank notes?" in r.content
    assert b"New notes with tap water" in r.content
    assert b"Events use tap water" in r.content


def test_accept_notes_proposal_updates_tank_notes(client, tank_id):
    pid = _insert_pending_proposal(tank_id, "Water source: home tap. Flourish weekly.")
    r = client.post(
        f"/tanks/{tank_id}/notes-proposal/{pid}/accept",
        data={},
        follow_redirects=False,
    )
    assert r.status_code == 303
    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    status = conn.execute(
        "SELECT status FROM tank_notes_proposals WHERE id=?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert notes == "Water source: home tap. Flourish weekly."
    assert status == "accepted"
    # Proposal banner gone
    assert b"Update tank notes?" not in client.get(f"/tanks/{tank_id}").content


def test_accept_notes_proposal_allows_user_edit(client, tank_id):
    pid = _insert_pending_proposal(tank_id, "AI draft notes")
    r = client.post(
        f"/tanks/{tank_id}/notes-proposal/{pid}/accept",
        data={"notes": "User-edited final notes"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    conn.close()
    assert notes == "User-edited final notes"


def test_dismiss_notes_proposal_leaves_notes_unchanged(client, tank_id):
    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute("UPDATE tanks SET notes=? WHERE id=?", ("Keep these notes", tank_id))
    conn.commit()
    conn.close()
    pid = _insert_pending_proposal(tank_id, "Would overwrite", prior="Keep these notes")
    r = client.post(
        f"/tanks/{tank_id}/notes-proposal/{pid}/dismiss",
        follow_redirects=False,
    )
    assert r.status_code == 303
    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    status = conn.execute(
        "SELECT status FROM tank_notes_proposals WHERE id=?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert notes == "Keep these notes"
    assert status == "dismissed"


def test_manual_edit_dismisses_pending_notes_proposal(client, tank_id):
    pid = _insert_pending_proposal(tank_id, "AI wants this")
    client.post(
        f"/tanks/{tank_id}/edit",
        data={
            "name": "Test Tank",
            "water_type": "fresh",
            "status": "active",
            "notes": "Manually set notes",
        },
        follow_redirects=False,
    )
    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    status = conn.execute(
        "SELECT status FROM tank_notes_proposals WHERE id=?", (pid,)
    ).fetchone()[0]
    conn.close()
    assert notes == "Manually set notes"
    assert status == "dismissed"


def test_accept_missing_notes_proposal_404(client, tank_id):
    assert client.post(f"/tanks/{tank_id}/notes-proposal/9999/accept").status_code == 404
