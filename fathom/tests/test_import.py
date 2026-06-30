"""Tests for import_data: _strip_html unit tests + import/confirm endpoint."""
import json
import sqlite3
import database as _db
from routers.import_data import _strip_html


# ── _strip_html unit tests ──────────────────────────────────────────────────

def test_strip_html_removes_tags():
    result = _strip_html("<p>Hello <b>world</b></p>")
    assert "<" not in result
    assert "Hello" in result
    assert "world" in result


def test_strip_html_decodes_amp():
    assert "&" in _strip_html("fish &amp; plants")


def test_strip_html_decodes_gt_lt():
    result = _strip_html("pH &gt; 7.0 &amp; KH &lt; 10")
    assert ">" in result
    assert "<" in result


def test_strip_html_decodes_quot():
    assert '"' in _strip_html("&quot;test&quot;")


def test_strip_html_nbsp_becomes_space():
    assert "hello world" in _strip_html("hello&nbsp;world")


def test_strip_html_br_becomes_newline():
    result = _strip_html("line1<br/>line2<br>line3")
    assert "\n" in result
    assert "line1" in result
    assert "line2" in result


def test_strip_html_p_becomes_newline():
    result = _strip_html("<p>Para one</p><p>Para two</p>")
    assert "\n" in result
    assert "Para one" in result
    assert "Para two" in result


def test_strip_html_collapses_excess_newlines():
    result = _strip_html("<p>A</p>\n\n\n\n<p>B</p>")
    assert "\n\n\n" not in result


def test_strip_html_plain_text_unchanged():
    plain = "pH 7.2, temp 76F, all normal"
    assert _strip_html(plain) == plain


# ── import/confirm endpoint tests ───────────────────────────────────────────

def test_import_confirm_empty_preview(client, tank_id):
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {}})
    assert r.status_code == 200
    assert r.json()["status"] == "imported"


def test_import_confirm_test_results(client, tank_id):
    preview = {
        "test_results": [{"timestamp": "2026-01-01 00:00:00", "ph": 7.0, "temp": 76.0, "nitrate": 10.0}]
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.status_code == 200
    assert r.json()["inserted"]["test_results"] == 1

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT ph, temp FROM test_results WHERE tank_id=?", (tank_id,)).fetchone()
    conn.close()
    assert abs(row[0] - 7.0) < 0.001
    assert abs(row[1] - 76.0) < 0.001


def test_import_confirm_tank_specs_updates_tank(client, tank_id):
    preview = {"tank_specs": {"volume_gallons": 5.5, "manufacturer": "Fluval", "model": "Spec V"}}
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.status_code == 200

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT volume_gallons, manufacturer, model FROM tanks WHERE id=?", (tank_id,)).fetchone()
    conn.close()
    assert abs(row[0] - 5.5) < 0.001
    assert row[1] == "Fluval"
    assert row[2] == "Spec V"


def test_import_confirm_tank_specs_skips_null_fields(client, tank_id):
    """Null fields in tank_specs must not overwrite existing values."""
    # Set manufacturer first
    client.post(
        f"/tanks/{tank_id}/import/confirm",
        json={"preview": {"tank_specs": {"manufacturer": "Fluval"}}},
    )
    # Second import with null manufacturer
    client.post(
        f"/tanks/{tank_id}/import/confirm",
        json={"preview": {"tank_specs": {"model": "Spec V", "manufacturer": None}}},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT manufacturer, model FROM tanks WHERE id=?", (tank_id,)).fetchone()
    conn.close()
    assert row[0] == "Fluval"   # preserved
    assert row[1] == "Spec V"   # updated


def test_import_confirm_inhabitants_count_unknown(client, tank_id):
    preview = {"inhabitants": [{"common_name": "MTS Snail", "count_unknown": True, "count": None}]}
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT count FROM inhabitants WHERE tank_id=?", (tank_id,)).fetchone()
    conn.close()
    assert row[0] is None


def test_import_confirm_inhabitants_creates_population_event(client, tank_id):
    preview = {"inhabitants": [{"common_name": "Ember Tetra", "count": 8}]}
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT event_type, count FROM population_events WHERE tank_id=?", (tank_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "added"
    assert row[1] == 8


def test_import_confirm_equipment_specs_dict_serialized(client, tank_id):
    preview = {
        "equipment": [{"category": "filter", "brand": "AquaClear", "model": "20",
                        "specs": {"flow_rate": "100gph"}}]
    }
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT specs FROM tank_equipment WHERE tank_id=?", (tank_id,)).fetchone()
    conn.close()
    assert json.loads(row[0])["flow_rate"] == "100gph"


def test_import_confirm_issues_stored(client, tank_id):
    preview = {
        "issues": [{"title": "Algae outbreak", "description": "Green algae on glass",
                    "status": "resolved", "opened_at": "2026-01-01", "resolved_at": "2026-01-15"}]
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.json()["inserted"]["issues"] == 1

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT title, status, resolved_at FROM issues WHERE tank_id=?", (tank_id,)).fetchone()
    conn.close()
    assert row[0] == "Algae outbreak"
    assert row[1] == "resolved"
    assert row[2] == "2026-01-15"


def test_import_confirm_observations_with_timestamp(client, tank_id):
    preview = {"observations": [{"text": "Shrimp active", "created_at": "2026-01-10 14:00:00"}]}
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT text, source, created_at FROM observations WHERE tank_id=?", (tank_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "Shrimp active"
    assert row[1] == "manual"
    assert row[2] == "2026-01-10 14:00:00"


def test_import_confirm_plants_stored(client, tank_id):
    preview = {"plants": [{"common_name": "Java Fern", "species": "Microsorum pteropus", "status": "active"}]}
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.json()["inserted"]["plants"] == 1


def test_import_confirm_hardscape_stored(client, tank_id):
    preview = {"hardscape": [{"item": "Driftwood", "quantity": 2}]}
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.json()["inserted"]["hardscape"] == 1

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT item, quantity FROM hardscape WHERE tank_id=?", (tank_id,)).fetchone()
    conn.close()
    assert row[0] == "Driftwood"
    assert row[1] == 2


def test_import_confirm_all_sections(client, tank_id):
    preview = {
        "test_results": [{"timestamp": "2026-01-01 00:00:00", "ph": 7.0}],
        "events": [{"event_type": "water_change", "timestamp": "2026-01-02 00:00:00"}],
        "purchases": [{"item": "Filter media", "category": "equipment", "cost": 8.0}],
        "inhabitants": [{"common_name": "Neon Tetra", "count": 6}],
        "plants": [{"common_name": "Java Fern", "status": "active"}],
        "equipment": [{"category": "filter", "brand": "AquaClear"}],
        "hardscape": [{"item": "Driftwood", "quantity": 1}],
        "issues": [{"title": "Algae growth", "status": "open"}],
        "observations": [{"text": "Tank looking healthy", "created_at": "2026-01-01 12:00:00"}],
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.status_code == 200
    inserted = r.json()["inserted"]
    for section in ("test_results", "events", "purchases", "inhabitants", "plants",
                    "equipment", "hardscape", "issues", "observations"):
        assert inserted.get(section) == 1, f"section {section!r} not inserted"


def test_import_confirm_zero_count_sections_excluded_from_inserted(client, tank_id):
    preview = {"test_results": [], "events": []}
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    inserted = r.json()["inserted"]
    assert "test_results" not in inserted
    assert "events" not in inserted


def test_import_confirm_404_for_unknown_tank(client):
    r = client.post("/tanks/9999/import/confirm", json={"preview": {}})
    assert r.status_code == 404


def test_import_page_404_for_unknown_tank(client):
    assert client.get("/tanks/9999/import-page").status_code == 404
