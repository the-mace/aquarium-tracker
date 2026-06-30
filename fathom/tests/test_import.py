"""Tests for import_data: _strip_html unit tests + import/confirm + check-duplicates endpoints."""
import json
import sqlite3
import database as _db
from routers.import_data import _strip_html, _find_duplicates, _merge_results, _split_chunks


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


# ── _find_duplicates unit tests ──────────────────────────────────────────────

def test_find_duplicates_no_existing_data(tank_id):
    preview = {
        "test_results": [{"timestamp": "2026-01-01 00:00:00", "ph": 7.0, "ammonia": 0.0, "nitrate": 10.0}],
        "events": [{"timestamp": "2026-01-02 00:00:00", "event_type": "water_change"}],
    }
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert dups == []


def test_find_duplicates_test_result_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "test_results": [{"timestamp": "2026-03-10 00:00:00", "ph": 7.2, "ammonia": 0.0, "nitrate": 15.0}]
    }})
    preview = {"test_results": [{"timestamp": "2026-03-10 00:00:00", "ph": 7.2, "ammonia": 0.0}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "test_results"
    assert dups[0]["index"] == 0


def test_find_duplicates_test_result_different_date_no_dup(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "test_results": [{"timestamp": "2026-03-10 00:00:00", "ph": 7.2}]
    }})
    preview = {"test_results": [{"timestamp": "2026-03-11 00:00:00", "ph": 7.2}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert dups == []


def test_find_duplicates_test_result_same_date_different_values_no_dup(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "test_results": [{"timestamp": "2026-03-10 00:00:00", "ph": 7.2, "ammonia": 0.0, "nitrate": 5.0}]
    }})
    # Only 1 param matches (ph) — not flagged as dup (needs >= 2)
    preview = {"test_results": [{"timestamp": "2026-03-10 00:00:00", "ph": 7.2, "ammonia": 0.5, "nitrate": 40.0}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert dups == []


def test_find_duplicates_event_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "events": [{"timestamp": "2026-03-15 00:00:00", "event_type": "water_change"}]
    }})
    preview = {"events": [{"timestamp": "2026-03-15 00:00:00", "event_type": "water_change"}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "events"


def test_find_duplicates_event_same_day_different_type_no_dup(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "events": [{"timestamp": "2026-03-15 00:00:00", "event_type": "water_change"}]
    }})
    preview = {"events": [{"timestamp": "2026-03-15 00:00:00", "event_type": "feeding"}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert dups == []


def test_find_duplicates_purchase_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "purchases": [{"item": "Fluval Spec V", "purchase_date": "2026-01-05", "cost": 89.99}]
    }})
    preview = {"purchases": [{"item": "Fluval Spec V", "purchase_date": "2026-01-05"}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "purchases"


def test_find_duplicates_purchase_case_insensitive(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "purchases": [{"item": "Fluval Spec V", "purchase_date": "2026-01-05"}]
    }})
    preview = {"purchases": [{"item": "fluval spec v", "purchase_date": "2026-01-05"}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1


def test_find_duplicates_inhabitant_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "inhabitants": [{"species": "Caridina cantonensis", "added_date": "2026-02-01", "count": 10}]
    }})
    preview = {"inhabitants": [{"species": "Caridina cantonensis", "added_date": "2026-02-01", "count": 10}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "inhabitants"


def test_find_duplicates_inhabitant_no_date_not_flagged(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "inhabitants": [{"species": "Caridina cantonensis", "count": 10}]
    }})
    # No added_date on either side — should not flag
    preview = {"inhabitants": [{"species": "Caridina cantonensis", "count": 10}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert dups == []


def test_find_duplicates_equipment_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "equipment": [{"category": "filter", "brand": "Fluval", "model": "Spec V Filter"}]
    }})
    preview = {"equipment": [{"category": "filter", "brand": "Fluval", "model": "Spec V Filter"}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "equipment"


def test_find_duplicates_hardscape_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "hardscape": [{"item": "Spider Wood", "quantity": 1}]
    }})
    preview = {"hardscape": [{"item": "Spider Wood", "quantity": 1}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "hardscape"


def test_find_duplicates_issue_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "issues": [{"title": "Brown algae bloom", "status": "resolved"}]
    }})
    preview = {"issues": [{"title": "Brown algae bloom", "status": "open"}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "issues"


def test_find_duplicates_observation_match(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "observations": [{"text": "Shrimp very active today", "created_at": "2026-03-20 10:00:00"}]
    }})
    preview = {"observations": [{"text": "Shrimp very active today", "created_at": "2026-03-20 00:00:00"}]}
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 1
    assert dups[0]["section"] == "observations"


def test_find_duplicates_multiple_sections(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "events": [{"timestamp": "2026-04-01 00:00:00", "event_type": "water_change"}],
        "hardscape": [{"item": "Lava Rock"}],
    }})
    preview = {
        "events": [{"timestamp": "2026-04-01 00:00:00", "event_type": "water_change"}],
        "hardscape": [{"item": "Lava Rock"}, {"item": "Driftwood"}],
    }
    with _db.get_db() as conn:
        dups = _find_duplicates(tank_id, preview, conn)
    assert len(dups) == 2
    sections = {d["section"] for d in dups}
    assert sections == {"events", "hardscape"}


# ── check-duplicates endpoint tests ─────────────────────────────────────────

def test_check_duplicates_endpoint_no_dups(client, tank_id):
    preview = {"test_results": [{"timestamp": "2026-05-01 00:00:00", "ph": 7.0}]}
    r = client.post(f"/tanks/{tank_id}/import/check-duplicates", json={"preview": preview})
    assert r.status_code == 200
    assert r.json()["duplicates"] == []


def test_check_duplicates_endpoint_finds_dup(client, tank_id):
    client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": {
        "purchases": [{"item": "API Test Kit", "purchase_date": "2026-05-10", "cost": 30.0}]
    }})
    preview = {"purchases": [{"item": "API Test Kit", "purchase_date": "2026-05-10"}]}
    r = client.post(f"/tanks/{tank_id}/import/check-duplicates", json={"preview": preview})
    assert r.status_code == 200
    dups = r.json()["duplicates"]
    assert len(dups) == 1
    assert dups[0]["section"] == "purchases"
    assert dups[0]["index"] == 0
    assert "message" in dups[0]


def test_check_duplicates_endpoint_404_for_unknown_tank(client):
    r = client.post("/tanks/9999/import/check-duplicates", json={"preview": {}})
    assert r.status_code == 404


# ── Rule 8: split multi-type entries (same date → multiple sections) ──────────

def test_confirm_multi_type_same_date_all_inserted(client, tank_id):
    """A single dated log block with a test, two events, and an observation produces separate rows in each table."""
    preview = {
        "test_results": [{"timestamp": "2026-05-20 00:00:00", "ph": 7.2, "ammonia": 0.0}],
        "events": [
            {"timestamp": "2026-05-20 00:00:00", "event_type": "water_change", "amount": 20},
            {"timestamp": "2026-05-20 00:00:00", "event_type": "maintenance", "notes": "Dosed Flourish"},
        ],
        "observations": [{"text": "Shrimp molting observed", "created_at": "2026-05-20 00:00:00"}],
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.status_code == 200
    inserted = r.json()["inserted"]
    assert inserted["test_results"] == 1
    assert inserted["events"] == 2
    assert inserted["observations"] == 1

    conn = sqlite3.connect(_db.DB_PATH)
    tr_count = conn.execute("SELECT count(*) FROM test_results WHERE tank_id=?", (tank_id,)).fetchone()[0]
    ev_count = conn.execute("SELECT count(*) FROM events WHERE tank_id=?", (tank_id,)).fetchone()[0]
    obs_count = conn.execute("SELECT count(*) FROM observations WHERE tank_id=?", (tank_id,)).fetchone()[0]
    conn.close()
    assert tr_count == 1
    assert ev_count == 2
    assert obs_count == 1


def test_confirm_multiple_events_same_date_different_types(client, tank_id):
    """Multiple event types on the same date are each inserted as separate rows."""
    preview = {
        "events": [
            {"timestamp": "2026-06-01 00:00:00", "event_type": "water_change", "amount": 25},
            {"timestamp": "2026-06-01 00:00:00", "event_type": "treatment", "notes": "Added Prime"},
            {"timestamp": "2026-06-01 00:00:00", "event_type": "maintenance", "notes": "Trimmed plants"},
        ]
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.status_code == 200
    assert r.json()["inserted"]["events"] == 3

    conn = sqlite3.connect(_db.DB_PATH)
    rows = conn.execute(
        "SELECT event_type FROM events WHERE tank_id=? ORDER BY event_type", (tank_id,)
    ).fetchall()
    conn.close()
    types = [row[0] for row in rows]
    assert "water_change" in types
    assert "treatment" in types
    assert "maintenance" in types


def test_confirm_same_date_cross_section_not_deduped(client, tank_id):
    """A test_result and an event sharing a date are both inserted — dup detection is within-section only."""
    preview = {
        "test_results": [{"timestamp": "2026-06-15 00:00:00", "ph": 7.0}],
        "events": [{"timestamp": "2026-06-15 00:00:00", "event_type": "water_change"}],
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.json()["inserted"]["test_results"] == 1
    assert r.json()["inserted"]["events"] == 1


# ── Rule 9: test kit methodology notes stored in notes field, not as param ────

def test_confirm_test_result_methodology_in_notes_not_param(client, tank_id):
    """kh=None with methodology text in notes: notes are stored, kh stays null."""
    preview = {
        "test_results": [
            {"timestamp": "2026-05-20 00:00:00", "ph": 7.2, "kh": None,
             "notes": "KH test: 9 drops until blue went green"}
        ]
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.status_code == 200
    assert r.json()["inserted"]["test_results"] == 1

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT ph, kh, notes FROM test_results WHERE tank_id=?", (tank_id,)).fetchone()
    conn.close()
    assert abs(row[0] - 7.2) < 0.001
    assert row[1] is None
    assert "drops" in (row[2] or "")


def test_confirm_test_result_notes_only_no_params(client, tank_id):
    """A test_result row with no numeric params but a notes string is still inserted cleanly."""
    preview = {
        "test_results": [
            {"timestamp": "2026-04-10 00:00:00", "notes": "Visual check only — water looks clear"}
        ]
    }
    r = client.post(f"/tanks/{tank_id}/import/confirm", json={"preview": preview})
    assert r.status_code == 200
    assert r.json()["inserted"]["test_results"] == 1

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT ph, notes FROM test_results WHERE tank_id=?", (tank_id,)).fetchone()
    conn.close()
    assert row[0] is None
    assert "Visual check" in (row[1] or "")


# ── _merge_results unit tests ─────────────────────────────────────────────────

def test_merge_results_combines_sections():
    chunk1 = {
        "test_results": [{"timestamp": "2026-01-01 00:00:00", "ph": 7.0}],
        "events": [{"timestamp": "2026-01-01 00:00:00", "event_type": "water_change"}],
    }
    chunk2 = {
        "test_results": [{"timestamp": "2026-01-02 00:00:00", "ph": 7.2}],
        "events": [{"timestamp": "2026-01-02 00:00:00", "event_type": "feeding"}],
    }
    merged, flags = _merge_results([chunk1, chunk2])
    assert len(merged["test_results"]) == 2
    assert len(merged["events"]) == 2
    assert flags == []


def test_merge_results_flag_indices_offset_by_chunk_length():
    chunk1 = {
        "test_results": [{"timestamp": "2026-01-01 00:00:00", "ph": 7.0}],
        "flags": [{"section": "test_results", "index": 0, "message": "pH low"}],
    }
    chunk2 = {
        "test_results": [{"timestamp": "2026-01-02 00:00:00", "ph": 6.0}],
        "flags": [{"section": "test_results", "index": 0, "message": "pH very low"}],
    }
    merged, flags = _merge_results([chunk1, chunk2])
    assert len(flags) == 2
    assert flags[0]["index"] == 0
    assert flags[1]["index"] == 1


def test_merge_results_tank_specs_first_value_wins():
    chunk1 = {"tank_specs": {"manufacturer": "Fluval", "model": "Spec V"}}
    chunk2 = {"tank_specs": {"manufacturer": "ADA", "volume_gallons": 5.5}}
    merged, _ = _merge_results([chunk1, chunk2])
    assert merged["tank_specs"]["manufacturer"] == "Fluval"
    assert merged["tank_specs"]["model"] == "Spec V"
    assert merged["tank_specs"]["volume_gallons"] == 5.5


def test_merge_results_empty_sections_preserved_as_lists():
    merged, _ = _merge_results([{"test_results": [], "events": []}])
    assert merged["test_results"] == []
    assert merged["events"] == []


# ── _split_chunks unit tests ──────────────────────────────────────────────────

def test_split_chunks_short_content_not_split():
    content = "short content"
    assert _split_chunks(content, max_chars=1000) == [content]


def test_split_chunks_splits_at_paragraph_boundary():
    para_a = "A" * 100
    para_b = "B" * 100
    content = para_a + "\n\n" + para_b
    chunks = _split_chunks(content, max_chars=150)
    assert len(chunks) == 2
    assert para_a in chunks[0]
    assert para_b in chunks[1]


def test_split_chunks_no_content_lost():
    paras = [f"paragraph {i} " + "x" * 50 for i in range(10)]
    content = "\n\n".join(paras)
    chunks = _split_chunks(content, max_chars=200)
    rejoined = "\n\n".join(chunks)
    for para in paras:
        assert para in rejoined
