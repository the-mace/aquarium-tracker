def test_timeline_empty(client, tank_id):
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "No history yet" in r.text


def test_timeline_shows_delete_buttons_and_endpoint_map(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "water_change", "amount": "2.5", "timestamp": "2026-01-15 10:00:00"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "deleteTimelineItem('event'" in r.text
    assert "TL_ENDPOINTS" in r.text
    assert f"/tanks/{tank_id}/events/" in r.text  # event delete endpoint referenced in JS map
    assert f"/tanks/{tank_id}/inhabitants/population-events/" in r.text  # population delete endpoint referenced


def test_timeline_shows_event(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "water_change", "amount": "2.5", "timestamp": "2026-01-15 10:00:00"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "water change" in r.text.lower()
    assert "2026-01-15" in r.text


def test_timeline_shows_issue_open_and_resolve(client, tank_id):
    ri = client.post(
        f"/tanks/{tank_id}/issues",
        data={"title": "Algae outbreak", "description": "Green algae on glass"},
        headers={"Accept": "application/json"},
    )
    issue_id = ri.json()["id"]
    client.post(
        f"/tanks/{tank_id}/issues/{issue_id}/update",
        data={"title": "Algae outbreak", "status": "resolved"},
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "Algae outbreak" in r.text
    assert "Issue opened" in r.text
    assert "Issue resolved" in r.text


def test_timeline_shows_equipment_install_and_remove(client, tank_id):
    req = client.post(
        f"/tanks/{tank_id}/equipment",
        data={
            "category": "filter",
            "brand": "Fluval",
            "model": "Spec V",
            "installed_date": "2025-06-01",
        },
        headers={"Accept": "application/json"},
    )
    eq_id = req.json()["id"]
    client.post(
        f"/tanks/{tank_id}/equipment/{eq_id}/update",
        data={
            "category": "filter",
            "brand": "Fluval",
            "model": "Spec V",
            "installed_date": "2025-06-01",
            "removed_date": "2026-03-01",
            "is_active": "0",
        },
        follow_redirects=False,
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "Fluval Spec V" in r.text
    assert "filter installed" in r.text.lower()
    assert "filter removed" in r.text.lower()
    assert "2025-06-01" in r.text
    assert "2026-03-01" in r.text


def test_timeline_shows_population_event(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/inhabitants",
        data={
            "species": "Neocaridina davidi",
            "common_name": "Cherry Shrimp",
            "count": "6",
            "added_date": "2026-02-01",
        },
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "Cherry Shrimp" in r.text
    assert "added" in r.text.lower()


def test_timeline_shows_water_test(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"timestamp": "2026-04-01 08:00:00", "ph": "7.2", "ammonia": "0", "notes": "Looking good"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "water test" in r.text.lower()
    assert "pH 7.2" in r.text
    assert "Looking good" in r.text
    assert "2026-04-01" in r.text


def test_timeline_shows_test_notes_once_when_mirrored_to_observation(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"timestamp": "2026-04-01 08:00:00", "ph": "7.2", "notes": "Looking good"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    # The note is now a manual observation, linked back to the test — should not
    # also be repeated as the water-test entry's own detail text.
    assert r.text.count("Looking good") == 1
    assert "manual note" in r.text.lower()


def test_timeline_still_shows_notes_inline_for_test_without_linked_observation(client, tank_id):
    import sqlite3
    import database as _db

    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute(
        "INSERT INTO test_results (tank_id, timestamp, ph, notes) VALUES (?,?,?,?)",
        (tank_id, "2026-04-01 08:00:00", 7.2, "Legacy note, no observation row"),
    )
    conn.commit()
    conn.close()
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "Legacy note, no observation row" in r.text


def test_timeline_flags_out_of_range_test_params(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/tests",
        data={
            "timestamp": "2026-04-01 08:00:00",
            "ph": "7.2", "ammonia": "0.5", "nitrite": "0.2", "nitrate": "50",
        },
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert '<span class="tl-param tl-param-danger">NH3 0.5</span>' in r.text
    assert '<span class="tl-param tl-param-danger">NO2 0.2</span>' in r.text
    assert '<span class="tl-param tl-param-warn">NO3 50.0</span>' in r.text
    assert '<span class="tl-param">pH 7.2</span>' in r.text


def test_timeline_shows_observation(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Shrimp molting normally"},
        headers={"Accept": "application/json"},
    )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "manual note" in r.text.lower()
    assert "Shrimp molting normally" in r.text


def test_timeline_kind_filter_tests_and_observations(client, tank_id):
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"timestamp": "2026-04-01 08:00:00", "ph": "7.2"},
        headers={"Accept": "application/json"},
    )
    client.post(
        f"/tanks/{tank_id}/observations",
        data={"text": "Shrimp molting normally"},
        headers={"Accept": "application/json"},
    )
    client.post(
        f"/tanks/{tank_id}/events",
        data={"event_type": "water_change", "amount": "2.5", "timestamp": "2026-01-15 10:00:00"},
        headers={"Accept": "application/json"},
    )

    r = client.get(f"/tanks/{tank_id}/timeline?kind=tests")
    assert "pH 7.2" in r.text
    assert "water change" not in r.text.lower()

    r = client.get(f"/tanks/{tank_id}/timeline?kind=observations")
    assert "Shrimp molting normally" in r.text
    assert "pH 7.2" not in r.text


def test_timeline_404_unknown_tank(client):
    r = client.get("/tanks/99999/timeline")
    assert r.status_code == 404


def test_timeline_date_grouping(client, tank_id):
    for event_type in ("water_change", "feeding", "maintenance"):
        client.post(
            f"/tanks/{tank_id}/events",
            data={"event_type": event_type, "timestamp": "2026-03-10 09:00:00"},
            headers={"Accept": "application/json"},
        )
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert r.text.count("2026-03-10") == 1
