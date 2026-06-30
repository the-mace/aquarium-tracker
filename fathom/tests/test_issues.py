import sqlite3
import database as _db


def _add_issue(client, tank_id, title="Algae bloom", status="open"):
    r = client.post(
        f"/tanks/{tank_id}/issues",
        data={"title": title, "description": "Test description", "status": status},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_add_issue_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/issues",
        data={"title": "High nitrates", "status": "open"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    assert "id" in r.json()


def test_add_issue_persisted(client, tank_id):
    issue_id = _add_issue(client, tank_id, title="pH instability")
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT title, status FROM issues WHERE id=?", (issue_id,)).fetchone()
    conn.close()
    assert row[0] == "pH instability"
    assert row[1] == "open"


def test_list_issues_page(client, tank_id):
    assert client.get(f"/tanks/{tank_id}/issues").status_code == 200


def test_update_issue_to_resolved_sets_resolved_at(client, tank_id):
    issue_id = _add_issue(client, tank_id, title="Algae bloom")
    r = client.post(
        f"/tanks/{tank_id}/issues/{issue_id}/update",
        data={"title": "Algae bloom", "status": "resolved"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["status"] == "updated"

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT status, resolved_at FROM issues WHERE id=?", (issue_id,)).fetchone()
    conn.close()
    assert row[0] == "resolved"
    assert row[1] is not None


def test_update_already_resolved_does_not_overwrite_resolved_at(client, tank_id):
    issue_id = _add_issue(client, tank_id, title="Old Issue")
    # First resolution
    client.post(
        f"/tanks/{tank_id}/issues/{issue_id}/update",
        data={"title": "Old Issue", "status": "resolved"},
        headers={"Accept": "application/json"},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    first_ts = conn.execute("SELECT resolved_at FROM issues WHERE id=?", (issue_id,)).fetchone()[0]

    # Re-resolve — timestamp must not change
    client.post(
        f"/tanks/{tank_id}/issues/{issue_id}/update",
        data={"title": "Old Issue", "status": "resolved"},
        headers={"Accept": "application/json"},
    )
    second_ts = conn.execute("SELECT resolved_at FROM issues WHERE id=?", (issue_id,)).fetchone()[0]
    conn.close()
    assert first_ts == second_ts


def test_update_issue_to_monitoring_no_resolved_at(client, tank_id):
    issue_id = _add_issue(client, tank_id, title="High GH")
    r = client.post(
        f"/tanks/{tank_id}/issues/{issue_id}/update",
        data={"title": "High GH", "status": "monitoring"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["status"] == "updated"

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT status, resolved_at FROM issues WHERE id=?", (issue_id,)).fetchone()
    conn.close()
    assert row[0] == "monitoring"
    assert row[1] is None


def test_delete_issue(client, tank_id):
    issue_id = _add_issue(client, tank_id, title="Temp issue")
    r = client.post(
        f"/tanks/{tank_id}/issues/{issue_id}/delete",
        follow_redirects=False,
    )
    assert r.status_code == 303
    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM issues WHERE id=?", (issue_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_update_nonexistent_issue_404(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/issues/9999/update",
        data={"title": "Ghost", "status": "open"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 404


def test_add_issue_redirects_without_json_accept(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/issues",
        data={"title": "Silent issue"},
        follow_redirects=False,
    )
    assert r.status_code == 303
