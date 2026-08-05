"""Unit tests for pure utility functions in database.py and ai_analysis.py."""
import json
import pytest
import database as _db
from database import row_to_dict, rows_to_list, init_db, get_db
from routers.ai_analysis import (
    _fmt_test_results, _fmt_inhabitants, _fmt_plants, _fmt_hardscape,
    _fmt_issues, _fmt_issues_with_id, _fmt_events, _fmt_schedule, _fmt_timeline_rows, _fmt_tank_notes,
    _fmt_home_water, _fmt_home_water_block, _baseline_from_fill_rows,
    _message_text,
    build_recommendation_prompt, build_analysis_prompt, build_summary_prompt,
    build_issue_review_prompt, _parse_issue_updates,
    build_notes_proposal_prompt, _parse_notes_proposal,
)
from routers.home_water import build_home_water_baseline


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
        "tank_state_summary", "plants", "hardscape", "home_water_tests",
        "home_water_summary",
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


# ── Claude response text extraction ─────────────────────────────────────────

class _Blk:
    def __init__(self, type=None, text=None, thinking=None):
        self.type = type
        if text is not None:
            self.text = text
        if thinking is not None:
            self.thinking = thinking


class _Msg:
    def __init__(self, content):
        self.content = content


def test_message_text_plain_text_only():
    assert _message_text(_Msg([_Blk(type="text", text="hello")])) == "hello"


def test_message_text_skips_thinking_block():
    """Sonnet 5 adaptive thinking: ThinkingBlock first has no .text."""
    msg = _Msg([
        _Blk(type="thinking", thinking="reason step by step…"),
        _Blk(type="text", text="Tank looks stable."),
    ])
    assert _message_text(msg) == "Tank looks stable."


def test_message_text_thinking_only_returns_empty():
    """max_tokens exhausted on thinking → no TextBlock (the prod failure mode)."""
    msg = _Msg([_Blk(type="thinking", thinking="…")])
    assert _message_text(msg) == ""


def test_message_text_legacy_fake_without_type():
    """test_ai_recommendation fakes only set .text — still must work."""
    class _Fake:
        def __init__(self, text):
            self.text = text
    assert _message_text(_Msg([_Fake("ok")])) == "ok"


def test_message_text_skips_redacted_thinking():
    msg = _Msg([
        _Blk(type="redacted_thinking"),
        _Blk(type="text", text="visible"),
    ])
    assert _message_text(msg) == "visible"


# ── ai_analysis formatter helpers ───────────────────────────────────────────

def test_fmt_home_water_empty():
    assert "No home/source water" in _fmt_home_water([])


def test_fmt_home_water_formats_sample_and_lab():
    rows = [{
        "timestamp": "2026-07-20 12:00:00", "gh": 8.0, "kh": 10.0,
        "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
        "tds": None, "temp": None, "sample_point": "tap", "is_lab_test": 0,
        "water_blend": "mixed", "notes": None,
    }, {
        "timestamp": "2026-03-01 12:00:00", "gh": 1.0, "kh": 0.5,
        "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
        "tds": 10, "temp": None, "sample_point": "bottled_distilled", "is_lab_test": 0,
        "water_blend": None, "notes": "store brand",
    }]
    result = _fmt_home_water(rows)
    assert "[tap_WC_source, mixed_hard_soft]" in result
    assert "GH=8.0" in result
    assert "[bottled_distilled]" in result
    assert "TDS=10" in result
    assert "store brand" in result


def test_build_home_water_baseline_merges_partial_rows():
    """GH-only newest reading still carries earlier KH/pH into the baseline."""
    from datetime import datetime, timezone
    now = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"id": 2, "timestamp": "2026-08-05 12:00:00", "gh": 8.0, "kh": None, "ph": None,
         "ammonia": None, "nitrite": None, "nitrate": None, "tds": None, "temp": None,
         "sample_point": "tap"},
        {"id": 1, "timestamp": "2026-07-01 12:00:00", "gh": 7.5, "kh": 10.0, "ph": 7.2,
         "ammonia": None, "nitrite": None, "nitrate": 40.0, "tds": None, "temp": None,
         "sample_point": "tap"},
    ]
    baseline = build_home_water_baseline(rows, now=now)
    assert baseline is not None
    assert baseline["is_composite"] is True
    assert baseline["by_key"]["gh"]["value"] == 8.0
    assert baseline["by_key"]["gh"]["timestamp"] == "2026-08-05 12:00:00"
    assert baseline["by_key"]["kh"]["value"] == 10.0
    assert baseline["by_key"]["kh"]["timestamp"] == "2026-07-01 12:00:00"
    assert baseline["by_key"]["ph"]["value"] == 7.2
    assert baseline["by_key"]["nitrate"]["value"] == 40.0
    # July→August is well under 90 days
    assert baseline["has_stale"] is False
    assert baseline["by_key"]["gh"]["is_stale"] is False
    assert baseline["by_key"]["kh"]["is_stale"] is False


def test_build_home_water_baseline_marks_stale_over_90_days():
    """Params whose as-of date is >3 months old are flagged for the red UI cue."""
    from datetime import datetime, timezone
    now = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
    rows = [
        {"id": 2, "timestamp": "2026-08-01 12:00:00", "gh": 8.0, "kh": None, "ph": None,
         "ammonia": None, "nitrite": None, "nitrate": None, "tds": None, "temp": None,
         "sample_point": "tap"},
        {"id": 1, "timestamp": "2026-04-01 12:00:00", "gh": 7.0, "kh": 10.0, "ph": 7.2,
         "ammonia": None, "nitrite": None, "nitrate": 40.0, "tds": None, "temp": None,
         "sample_point": "tap"},
    ]
    baseline = build_home_water_baseline(rows, now=now)
    assert baseline["by_key"]["gh"]["is_stale"] is False
    assert baseline["by_key"]["kh"]["is_stale"] is True
    assert baseline["by_key"]["ph"]["is_stale"] is True
    assert baseline["has_stale"] is True


def test_build_home_water_baseline_single_full_row_not_composite():
    from datetime import datetime, timezone
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    rows = [{
        "id": 1, "timestamp": "2026-07-01 12:00:00", "gh": 8.0, "kh": 10.0,
        "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
        "tds": None, "temp": None, "sample_point": "tap",
    }]
    baseline = build_home_water_baseline(rows, now=now)
    assert baseline["is_composite"] is False
    assert len(baseline["params"]) == 2
    assert baseline["has_stale"] is False


def test_build_home_water_baseline_empty():
    assert build_home_water_baseline([]) is None
    assert build_home_water_baseline([{"gh": None, "kh": None, "timestamp": "x"}]) is None


def test_fmt_home_water_block_includes_baseline():
    rows = [
        {"id": 2, "timestamp": "2026-08-05 12:00:00", "gh": 8.0, "kh": None,
         "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
         "tds": None, "temp": None, "sample_point": "tap", "is_lab_test": 0,
         "water_blend": None, "notes": None},
        {"id": 1, "timestamp": "2026-07-01 12:00:00", "gh": 7.5, "kh": 10.0,
         "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
         "tds": None, "temp": None, "sample_point": "tap", "is_lab_test": 0,
         "water_blend": None, "notes": None},
    ]
    block = _fmt_home_water_block(rows)
    assert "Current fill-water baseline" in block
    assert "GH=8.0" in block
    assert "KH=10.0" in block
    assert "as of 2026-08-05" in block
    assert "as of 2026-07-01" in block
    assert "Recent fill-water readings" in block
    # Baseline uses only the newest stream's sample_point (tap), not bottled
    assert _baseline_from_fill_rows(rows)["sample_point"] == "tap"


def test_baseline_from_fill_rows_prefers_newest_stream():
    rows = [
        {"id": 3, "timestamp": "2026-08-05", "gh": 1.0, "kh": None, "sample_point": "bottled_spring",
         "ph": None, "ammonia": None, "nitrite": None, "nitrate": None, "tds": None, "temp": None},
        {"id": 2, "timestamp": "2026-08-01", "gh": 8.0, "kh": 10.0, "sample_point": "tap",
         "ph": None, "ammonia": None, "nitrite": None, "nitrate": None, "tds": None, "temp": None},
    ]
    baseline = _baseline_from_fill_rows(rows)
    assert baseline["sample_point"] == "bottled_spring"
    assert baseline["by_key"]["gh"]["value"] == 1.0
    assert "kh" not in baseline["by_key"]  # don't pull KH from a different stream


def test_build_recommendation_prompt_includes_home_water():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5, "notes": ""}
    test_result = {"id": 1, "timestamp": "2026-07-02 08:00:00", "ph": 7.0, "gh": 6.0, "kh": 4.0,
                   "ammonia": 0.0, "nitrite": 0.0, "nitrate": 5.0, "tds": None, "temp": 76.0, "notes": None}
    home = [{"timestamp": "2026-07-01", "gh": 8.0, "kh": 10.0, "sample_point": "tap",
             "is_lab_test": 0, "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
             "tds": None, "temp": None, "notes": None}]
    prompt = build_recommendation_prompt(tank, test_result, [test_result], [], [], [], [],
                                         home_water_tests=home)
    assert "Fill water" in prompt
    assert "GH=8.0" in prompt
    assert "Current fill-water baseline" in prompt
    assert "INCOMING" in prompt or "incoming" in prompt
    assert "bottled" in prompt.lower()
    assert "prior week" in prompt.lower() or "aged" in prompt.lower()
    assert "infants" in prompt.lower()
    assert "adult" in prompt.lower() or "3+" in prompt
    assert "partial" in prompt.lower() or "last known" in prompt.lower()


def test_load_home_water_tests_excludes_raw_includes_bottled(tmp_path, monkeypatch):
    import database as _db
    from database import init_db, get_db
    from routers.ai_analysis import load_home_water_tests

    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "hw_fill.db"))
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO home_water_tests (timestamp, gh, sample_point) VALUES (?,?,?)",
            ("2026-04-20 12:00:00", 5.0, "raw"),
        )
        conn.execute(
            "INSERT INTO home_water_tests (timestamp, gh, sample_point) VALUES (?,?,?)",
            ("2026-02-04 12:00:00", 8.0, "tap"),
        )
        conn.execute(
            "INSERT INTO home_water_tests (timestamp, gh, sample_point, notes) VALUES (?,?,?,?)",
            ("2026-03-01 12:00:00", 1.0, "bottled_spring", "Poland Spring"),
        )
        rows = load_home_water_tests(conn, limit=8)
    points = {r["sample_point"] for r in rows}
    assert "raw" not in points
    assert "tap" in points
    assert "bottled_spring" in points
    assert all(r["sample_point"] in ("tap", "bottled_spring", "bottled_distilled", "bottled")
               or not r["sample_point"] for r in rows)


def test_build_analysis_prompt_includes_home_water():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5}
    home = [{"timestamp": "2026-07-01", "gh": 8.0, "kh": 10.0, "sample_point": "tap",
             "is_lab_test": 0, "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
             "tds": None, "temp": None, "notes": None}]
    prompt = build_analysis_prompt(tank, [], [], [], [], [], [], home_water_tests=home)
    assert "Fill water" in prompt
    assert "GH=8.0" in prompt


def test_build_summary_prompt_includes_home_water():
    tank = {"name": "5G Tank", "water_type": "fresh", "volume_gallons": 5}
    home = [{"timestamp": "2026-07-01", "gh": 8.0, "kh": 10.0, "sample_point": "tap",
             "is_lab_test": 0, "ph": None, "ammonia": None, "nitrite": None, "nitrate": None,
             "tds": None, "temp": None, "notes": None}]
    prompt = build_summary_prompt(tank, [], [], [], [], [], "analysis", home_water_tests=home)
    assert "Fill water" in prompt
    assert "KH=10.0" in prompt


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
