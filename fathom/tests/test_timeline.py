def test_timeline_empty(client, tank_id):
    r = client.get(f"/tanks/{tank_id}/timeline")
    assert r.status_code == 200
    assert "No history yet" in r.text


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
