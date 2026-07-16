"""Unit tests for pure utility functions in database.py and ai_analysis.py."""
import json
import pytest
import database as _db
from database import row_to_dict, rows_to_list, init_db, get_db
from routers.ai_analysis import (
    _fmt_test_results, _fmt_inhabitants, _fmt_plants, _fmt_hardscape,
    _fmt_issues, _fmt_issues_with_id, _fmt_events, _fmt_schedule, _fmt_timeline_rows, _fmt_tank_notes,
    build_recommendation_prompt, build_analysis_prompt, build_summary_prompt,
    build_issue_review_prompt, _parse_issue_updates,
    build_notes_proposal_prompt, _parse_notes_proposal,
)


# ── database helpers ────────────────────────────────────────────────────────

def test_row_to_dict_none():
    assert row_to_dict(None) is None


def test_row_to_dict_converts_row(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO tanks (name) VALUES (?)", ("Alpha",))
        row = conn.execute("SELECT id, name FROM tanks WHERE name=?", ("Alpha",)).fetchone()
    result = row_to_dict(row)
    assert isinstance(result, dict)
    assert result["name"] == "Alpha"
    assert "id" in result


def test_rows_to_list_empty():
    assert rows_to_list([]) == []


def test_rows_to_list(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO tanks (name) VALUES (?)", ("Beta",))
        conn.execute("INSERT INTO tanks (name) VALUES (?)", ("Gamma",))
        rows = conn.execute("SELECT id, name FROM tanks").fetchall()
    result = rows_to_list(rows)
    assert len(result) == 2
    assert {r["name"] for r in result} == {"Beta", "Gamma"}


def test_init_db_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))
    init_db()
    init_db()  # second call must not raise
    with get_db() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    expected = {
        "tanks", "test_results", "events", "inhabitants", "population_events",
        "purchases", "tank_equipment", "issues", "observations",
        "tank_state_summary", "plants", "hardscape",
    }
    assert expected.issubset(tables)


def test_get_db_rolls_back_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "test.db"))
    init_db()
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO tanks (name) VALUES (?)", ("RollbackMe",))
            raise RuntimeError("forced error")
    except RuntimeError:
        pass
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM tanks WHERE name=?", ("RollbackMe",)).fetchone()[0]
    assert count == 0


# ── ai_analysis formatter helpers ───────────────────────────────────────────

def test_fmt_test_results_empty():
    result = _fmt_test_results([])
    assert "No test results" in result


def test_fmt_test_results_formats_fields():
    rows = [{"timestamp": "2026-01-01 12:00:00", "ph": 7.2, "temp": 76.0,
             "gh": None, "kh": None, "ammonia": 0.0, "nitrite": 0.0,
             "nitrate": 10.0, "tds": None, "notes": None}]
    result = _fmt_test_results(rows)
    assert "7.2" in result
    assert "76.0" in result
    assert "2026-01-01" in result


def test_fmt_test_results_appends_notes():
    rows = [{"timestamp": "2026-01-01 00:00:00", "ph": 7.0, "gh": None,
             "kh": None, "ammonia": None, "nitrite": None, "nitrate": None,
             "tds": None, "temp": None, "notes": "post water change"}]
    assert "post water change" in _fmt_test_results(rows)


def test_fmt_inhabitants_empty():
    assert "None" in _fmt_inhabitants([])


def test_fmt_inhabitants_null_count_displays_many():
    rows = [{"common_name": "MTS Snail", "species": None, "count": None}]
    assert "many" in _fmt_inhabitants(rows)


def test_fmt_inhabitants_named():
    rows = [{"common_name": "Neon Tetra", "species": None, "count": 6}]
    result = _fmt_inhabitants(rows)
    assert "6x" in result
    assert "Neon Tetra" in result


def test_fmt_inhabitants_includes_added_date():
    rows = [{"common_name": "Kuhli Loach", "species": None, "count": 3, "added_date": "2026-03-15"}]
    result = _fmt_inhabitants(rows)
    assert "2026-03-15" in result


def test_fmt_inhabitants_no_added_date():
    rows = [{"common_name": "Kuhli Loach", "species": None, "count": 3, "added_date": None}]
    result = _fmt_inhabitants(rows)
    assert "added" not in result.lower()


def test_fmt_plants_empty():
    assert "None" in _fmt_plants([])


def test_fmt_plants_named():
    rows = [{"common_name": "Java Fern", "species": None}]
    assert "Java Fern" in _fmt_plants(rows)


def test_fmt_hardscape_empty():
    assert "None" in _fmt_hardscape([])


def test_fmt_hardscape_quantity_prefix():
    rows = [{"item": "Driftwood", "quantity": 2}]
    result = _fmt_hardscape(rows)
    assert "2x" in result
    assert "Driftwood" in result


def test_fmt_hardscape_single_no_prefix():
    rows = [{"item": "Rock", "quantity": 1}]
    result = _fmt_hardscape(rows)
    assert "1x" not in result
    assert "Rock" in result


def test_fmt_issues_empty():
    assert "None" in _fmt_issues([])


def test_fmt_issues_status_in_output():
    rows = [{"status": "open", "title": "High nitrates", "description": "Over 40ppm"}]
    result = _fmt_issues(rows)
    assert "OPEN" in result
    assert "High nitrates" in result


def test_fmt_events_empty():
    assert "None" in _fmt_events([])


def test_fmt_events_formats_row():
    rows = [{"timestamp": "2026-01-05 08:00:00", "event_type": "water_change", "notes": "30%"}]
    result = _fmt_events(rows)
    assert "water_change" in result
    assert "30%" in result


def test_fmt_tank_notes_empty():
    assert _fmt_tank_notes({}) == ""
    assert _fmt_tank_notes({"notes": None}) == ""
    assert _fmt_tank_notes({"notes": "   "}) == ""


def test_fmt_tank_notes_present():
    result = _fmt_tank_notes({"notes": "Targets: GH 7-8, KH 2-10, pH 7.0-7.5."})
    assert "Targets: GH 7-8, KH 2-10" in result
    assert "accepted parameter targets" in result
    # Prefer schedule/events over stale operational notes
    assert "prefer the recurring schedule and recent events" in result


def test_build_recommendation_prompt_includes_tank_notes():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5,
            "notes": "Targets: GH 7-8, KH 2-10 (home water can't go lower without RO)."}
    test_result = {"id": 1, "timestamp": "2026-07-02 08:00:00", "ph": 7.0, "gh": None, "kh": 10.0,
                    "ammonia": 0.0, "nitrite": 0.0, "nitrate": 5.0, "tds": None, "temp": 76.0, "notes": None}
    prompt = build_recommendation_prompt(tank, test_result, [test_result], [], [], [], [])
    assert "KH 2-10" in prompt
    assert "can't go lower without RO" in prompt


def test_build_analysis_prompt_includes_tank_notes():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5,
            "notes": "Targets: GH 7-8, KH 2-10."}
    prompt = build_analysis_prompt(tank, [], [], [], [], [], [])
    assert "KH 2-10" in prompt


def test_build_summary_prompt_includes_tank_notes():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5,
            "notes": "Targets: GH 7-8, KH 2-10."}
    prompt = build_summary_prompt(tank, [], [], [], [], [], "latest analysis text")
    assert "KH 2-10" in prompt


def test_build_notes_proposal_prompt_includes_current_and_schedule():
    tank = {
        "name": "Shrimp Tank", "water_type": "fresh", "volume_gallons": 5,
        "notes": "Water source: spring water + Equilibrium",
    }
    schedule = [{
        "category": "maintenance",
        "description": "20% water change with room temp tap water; dose 5ml Flourish",
        "tracking_mode": "logged", "interval_days": 7,
        "last_done": "2026-07-09", "next_due": "2026-07-16",
    }]
    events = [{"timestamp": "2026-07-09", "event_type": "water_change",
               "notes": "tap water + Flourish"}]
    prompt = build_notes_proposal_prompt(tank, schedule, events, [])
    assert "spring water + Equilibrium" in prompt
    assert "room temp tap water" in prompt
    assert "update_needed" in prompt
    assert "proposed_notes" in prompt


def test_parse_notes_proposal_accepts_valid_update():
    raw = json.dumps({
        "update_needed": True,
        "reason": "Notes still say spring water; schedule uses tap + Flourish.",
        "proposed_notes": "Water source: home tap water. Dose Flourish weekly.",
    })
    result = _parse_notes_proposal(raw, "Water source: spring water + Equilibrium")
    assert result is not None
    assert "tap water" in result["proposed_notes"]
    assert "spring" not in result["proposed_notes"]
    assert result["prior_notes"] == "Water source: spring water + Equilibrium"


def test_parse_notes_proposal_rejects_no_update_and_identical():
    assert _parse_notes_proposal('{"update_needed": false, "reason": "ok", "proposed_notes": ""}', "x") is None
    same = "Water source: tap water"
    assert _parse_notes_proposal(json.dumps({
        "update_needed": True, "reason": "same", "proposed_notes": same,
    }), same) is None
    assert _parse_notes_proposal("not json", "notes") is None
    assert _parse_notes_proposal(json.dumps({
        "update_needed": True, "reason": "", "proposed_notes": "new notes",
    }), "old") is None


def test_analysis_and_summary_prefer_current_practices_over_stale_notes():
    """Stale tank notes may still say spring water + Equilibrium after a switch to tap;
    schedule/events and the current-practices rule must be in the prompt so Claude prefers them."""
    tank = {
        "name": "Shrimp Tank", "water_type": "fresh", "volume_gallons": 5,
        "notes": (
            "Water source: purchased spring water + Seachem Equilibrium "
            "(well water softener NOT used for this tank). Targets: GH 7-8, KH 2-4."
        ),
    }
    schedule = [{
        "category": "maintenance",
        "description": "20% water change with room temp tap water from prior week; dose 5ml Flourish",
        "tracking_mode": "logged", "interval_days": 7,
        "last_done": "2026-07-09", "next_due": "2026-07-16",
    }]
    events = [{
        "timestamp": "2026-07-09 16:30:30", "event_type": "maintenance",
        "notes": "20% water change with room temp tap water from prior week; dose 5ml Flourish",
    }]
    analysis = build_analysis_prompt(tank, [], [], events, [], [], [], schedule)
    summary = build_summary_prompt(
        tank, [], [], [], [], [], "stable parameters", schedule, events,
    )
    for prompt in (analysis, summary):
        assert "spring water" in prompt  # notes still present for history
        assert "room temp tap water" in prompt
        assert "5ml Flourish" in prompt
        assert "Describe CURRENT practices only" in prompt
        assert "do not name those discontinued products" in prompt
        assert "Recurring schedule" in prompt
        assert "Recent Events" in prompt


def test_fmt_schedule_empty():
    assert "No recurring schedule" in _fmt_schedule([])


def test_fmt_schedule_logged_row():
    rows = [{"category": "maintenance", "description": "Weekly water change",
             "tracking_mode": "logged", "interval_days": 7,
             "last_done": "2026-06-20", "next_due": "2026-06-27"}]
    result = _fmt_schedule(rows)
    assert "Weekly water change" in result
    assert "every 7 days" in result
    assert "2026-06-20" in result


def test_fmt_schedule_reference_only_row():
    rows = [{"category": "feeding", "description": "Flakes",
             "tracking_mode": "reference_only", "day_of_week": "mon"}]
    result = _fmt_schedule(rows)
    assert "Flakes" in result
    assert "mon" in result


def test_fmt_timeline_rows_empty():
    assert "No recent activity" in _fmt_timeline_rows([])


def test_fmt_timeline_rows_formats_entry():
    rows = [{"kind": "event", "subtype": "water_change", "ts": "2026-06-20 08:00:00",
             "label": "water_change", "detail": "30%"}]
    result = _fmt_timeline_rows(rows)
    assert "event/water_change" in result
    assert "30%" in result


def test_build_recommendation_prompt_includes_key_sections():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5}
    test_result = {"id": 2, "timestamp": "2026-07-02 08:00:00", "ph": 7.0, "gh": None, "kh": None,
                    "ammonia": 0.0, "nitrite": 0.0, "nitrate": 5.0, "tds": None, "temp": 76.0,
                    "notes": "did a partial water change"}
    recent_tests = [test_result, {"id": 1, "timestamp": "2026-06-25 08:00:00", "ph": 7.0, "gh": None,
                                   "kh": None, "ammonia": 0.0, "nitrite": 0.0, "nitrate": 10.0,
                                   "tds": None, "temp": 76.0, "notes": None}]
    issues = []
    inhabitants = [{"common_name": "Neocaridina Shrimp", "species": None, "count": 15}]
    schedule_rows = [{"category": "maintenance", "description": "Weekly water change",
                       "tracking_mode": "logged", "interval_days": 7,
                       "last_done": "2026-06-20", "next_due": "2026-06-27"}]
    timeline_rows = [{"kind": "event", "subtype": "water_change", "ts": "2026-06-25 08:00:00",
                       "label": "water_change", "detail": "25%"}]
    prompt = build_recommendation_prompt(tank, test_result, recent_tests, issues, inhabitants, schedule_rows, timeline_rows)
    assert "5G Tank" in prompt
    assert "did a partial water change" in prompt
    assert "Weekly water change" in prompt
    assert "25%" in prompt
    assert "Neocaridina Shrimp" in prompt
    assert "10.0" in prompt  # prior test's nitrate value present for trend comparison


def test_fmt_issues_with_id_empty():
    assert "None" in _fmt_issues_with_id([])


def test_fmt_issues_with_id_includes_id():
    rows = [{"id": 42, "status": "open", "title": "KH instability", "description": "Dropped to 1 once"}]
    result = _fmt_issues_with_id(rows)
    assert "id=42" in result
    assert "KH instability" in result


def test_build_issue_review_prompt_includes_issues_and_tests():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5}
    issues = [{"id": 7, "status": "open", "title": "KH instability", "description": "KH dropped to 1"}]
    test_results = [{"timestamp": "2026-07-01 08:00:00", "ph": 7.0, "gh": 6.0, "kh": 5.0,
                      "ammonia": 0.0, "nitrite": 0.0, "nitrate": 5.0, "tds": None, "temp": 76.0, "notes": None}]
    prompt = build_issue_review_prompt(tank, issues, test_results)
    assert "id=7" in prompt
    assert "KH instability" in prompt
    assert "KH=5.0" in prompt
    assert "JSON array" in prompt


def test_parse_issue_updates_valid_json():
    raw = '[{"issue_id": 7, "status": "resolved", "reason": "KH stable across last 4 tests"}]'
    updates = _parse_issue_updates(raw, {7})
    assert updates == [{"issue_id": 7, "status": "resolved", "reason": "KH stable across last 4 tests"}]


def test_parse_issue_updates_strips_code_fence():
    raw = '```json\n[{"issue_id": 7, "status": "monitoring", "reason": "Improving"}]\n```'
    updates = _parse_issue_updates(raw, {7})
    assert updates[0]["status"] == "monitoring"


def test_parse_issue_updates_empty_array():
    assert _parse_issue_updates("[]", {7}) == []


def test_parse_issue_updates_drops_unknown_issue_id():
    raw = '[{"issue_id": 99, "status": "resolved", "reason": "n/a"}]'
    assert _parse_issue_updates(raw, {7}) == []


def test_parse_issue_updates_drops_invalid_status():
    raw = '[{"issue_id": 7, "status": "closed", "reason": "n/a"}]'
    assert _parse_issue_updates(raw, {7}) == []


def test_parse_issue_updates_drops_missing_reason():
    raw = '[{"issue_id": 7, "status": "resolved", "reason": ""}]'
    assert _parse_issue_updates(raw, {7}) == []


def test_parse_issue_updates_malformed_json_returns_empty():
    assert _parse_issue_updates("not json at all", {7}) == []


def test_parse_issue_updates_extracts_array_from_surrounding_text():
    raw = 'Here is my answer:\n[{"issue_id": 7, "status": "resolved", "reason": "Stable"}]\nThanks.'
    updates = _parse_issue_updates(raw, {7})
    assert updates[0]["issue_id"] == 7
