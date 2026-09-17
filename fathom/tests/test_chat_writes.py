"""Structured write tools for Ask AI (chat_writes.py + chat tool loop)."""
import sqlite3

import database as _db
from chat_writes import apply_culture_write, apply_tank_write
from routers.chat import _build_system_prompt
from test_chat import (
    _FakeMessage, _FakeTextBlock, _FakeToolUseBlock, _RecordingMessages, _install_recording,
)


def _create_culture(client, name="Live Food", **extra):
    r = client.post("/cultures", data={"name": name, **extra}, headers={"Accept": "application/json"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _eq(client, tank_id, category="uv", brand="AquaUV", model="Classic 15W"):
    r = client.post(
        f"/tanks/{tank_id}/equipment",
        data={"category": category, "brand": brand, "model": model, "is_active": "1"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_system_prompt_tells_model_it_can_write():
    tank = {"name": "Test Tank", "water_type": "fresh", "volume_gallons": 5, "notes": None}
    prompt = _build_system_prompt(tank, None, [], [], [], [], None, [], [])
    assert "You CAN write to the database" in prompt
    assert "add_observation" in prompt
    assert "add_observation is REQUIRED" in prompt
    assert "do not say you are read-only" in prompt
    assert "query_db" in prompt


def test_system_prompt_includes_equipment_and_ids(client, tank_id):
    from database import get_db
    from routers.chat import _gather_tank_context, _require_tank

    _eq(client, tank_id)
    client.post(
        f"/tanks/{tank_id}/schedule",
        data={
            "category": "maintenance",
            "description": "UV sterilizer Tue/Thu 12am-3am",
            "day_of_week": "tue",
            "time_of_day": "am",
        },
        follow_redirects=False,
    )
    with get_db() as conn:
        tank = _require_tank(conn, tank_id)
        ctx = _gather_tank_context(conn, tank_id)
    prompt = _build_system_prompt(
        tank, ctx["latest_test"], ctx["inhabitants"], ctx["plants"], ctx["hardscape"],
        ctx["open_issues"], ctx["summary"], ctx["recent_obs"], ctx["schedule_rows"],
        equipment=ctx["equipment"],
    )
    assert "Equipment:" in prompt
    assert "[uv]" in prompt
    assert "id=" in prompt
    assert "UV sterilizer" in prompt


def test_add_observation_links_uv_by_name(client, tank_id):
    eq_id = _eq(client, tank_id)
    text = (
        "UV reverted to Tue/Thu 12am-3am schedule, 9/17, following resolution of "
        "Clado outbreak with no new symptoms since 9/7 treatment."
    )
    result = apply_tank_write(tank_id, "add_observation", {
        "text": text,
        "link_name": "UV",
    })
    assert result.get("ok") is True
    assert result["linked"] == [{"type": "equipment", "id": eq_id}]

    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    obs = conn.execute(
        "SELECT text, source FROM observations WHERE id=?", (result["id"],)
    ).fetchone()
    assert obs["text"] == text
    assert obs["source"] == "manual"
    link = conn.execute(
        "SELECT entity_type, entity_id FROM observation_links WHERE observation_id=?",
        (result["id"],),
    ).fetchone()
    conn.close()
    assert link["entity_type"] == "equipment"
    assert link["entity_id"] == eq_id


def test_log_event_does_not_create_auto_observation(client, tank_id):
    result = apply_tank_write(tank_id, "log_event", {
        "event_type": "maintenance",
        "notes": "UV reverted to Tue/Thu 12am-3am schedule.",
    })
    assert result.get("ok") is True
    conn = sqlite3.connect(_db.DB_PATH)
    ev = conn.execute(
        "SELECT event_type, notes, tank_id FROM events WHERE id=?", (result["id"],)
    ).fetchone()
    auto = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE tank_id=? AND source='auto'",
        (tank_id,),
    ).fetchone()[0]
    conn.close()
    assert ev[0] == "maintenance"
    assert "UV reverted" in ev[1]
    assert ev[2] == tank_id
    assert auto == 0


def test_append_notes_on_equipment_and_tank(client, tank_id):
    eq_id = _eq(client, tank_id)
    r1 = apply_tank_write(tank_id, "append_notes", {
        "target": "equipment", "name": "AquaUV",
        "note": "Reverted to Tue/Thu 12am-3am after Clado treatment.",
    })
    assert r1.get("ok") is True
    r2 = apply_tank_write(tank_id, "append_notes", {
        "target": "tank",
        "note": "Watch for Clado recurrence through 9/21.",
    })
    assert r2.get("ok") is True

    conn = sqlite3.connect(_db.DB_PATH)
    eq_notes = conn.execute(
        "SELECT notes FROM tank_equipment WHERE id=?", (eq_id,)
    ).fetchone()[0]
    tank_notes = conn.execute(
        "SELECT notes FROM tanks WHERE id=?", (tank_id,)
    ).fetchone()[0]
    conn.close()
    assert "Reverted to Tue/Thu" in eq_notes
    assert "Watch for Clado" in tank_notes


def test_append_notes_rejects_other_tank_equipment(client, make_tank):
    t1 = make_tank("Tank A")
    t2 = make_tank("Tank B")
    eq_id = _eq(client, t2)
    result = apply_tank_write(t1, "append_notes", {
        "target": "equipment", "id": eq_id, "note": "should not land",
    })
    assert "error" in result
    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM tank_equipment WHERE id=?", (eq_id,)).fetchone()[0]
    conn.close()
    assert not notes


def test_append_notes_ambiguous_name(client, tank_id):
    _eq(client, tank_id, brand="AquaUV", model="15W")
    _eq(client, tank_id, brand="AquaUV", model="25W")
    result = apply_tank_write(tank_id, "append_notes", {
        "target": "equipment", "name": "AquaUV", "note": "which one?",
    })
    assert "error" in result
    assert "Multiple" in result["error"]


def test_invalid_event_type_and_unknown_tool(client, tank_id):
    assert "error" in apply_tank_write(tank_id, "log_event", {
        "event_type": "explode", "notes": "nope",
    })
    assert "error" in apply_tank_write(tank_id, "drop_table", {"sql": "nope"})
    assert "error" in apply_tank_write(tank_id, "add_observation", {"text": ""})


def test_issue_status_only_changes_when_asked(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/issues",
        data={"title": "Clado outbreak", "status": "open"},
        headers={"Accept": "application/json"},
    )
    issue_id = r.json()["id"]
    noted = apply_tank_write(tank_id, "append_notes", {
        "target": "issue", "name": "Clado",
        "note": "No new symptoms since 9/7; watch through 9/21.",
    })
    assert noted.get("ok") is True
    conn = sqlite3.connect(_db.DB_PATH)
    status = conn.execute("SELECT status FROM issues WHERE id=?", (issue_id,)).fetchone()[0]
    conn.close()
    assert status == "open"

    resolved = apply_tank_write(tank_id, "append_notes", {
        "target": "issue", "id": issue_id,
        "note": "Closing out after two-week mark.",
        "status": "resolved",
    })
    assert resolved.get("ok") is True
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT status, resolved_at FROM issues WHERE id=?", (issue_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "resolved"
    assert row[1]


def test_chat_write_tool_loop_persists(client, tank_id, monkeypatch):
    eq_id = _eq(client, tank_id)
    note = "UV reverted to Tue/Thu 12am-3am schedule following Clado treatment."

    class _Msgs(_RecordingMessages):
        def _respond(self, kwargs):
            if kwargs.get("tools") and len(self.calls) == 1:
                return _FakeMessage(
                    [
                        _FakeToolUseBlock("t1", "add_observation", {
                            "text": note,
                            "link_type": "equipment",
                            "link_name": "UV",
                        }),
                        _FakeToolUseBlock("t2", "log_event", {
                            "event_type": "maintenance",
                            "notes": note,
                        }),
                        _FakeToolUseBlock("t3", "append_notes", {
                            "target": "equipment",
                            "name": "UV",
                            "note": note,
                        }),
                    ],
                    stop_reason="tool_use",
                )
            return _FakeMessage(
                [_FakeTextBlock("Logged the UV schedule change on the tank.")],
                stop_reason="end_turn",
            )

    msgs = _install_recording(monkeypatch, _Msgs())
    r = client.post(
        f"/tanks/{tank_id}/chat",
        json={"message": "Ok UV back to regular schedule. Note that."},
    )
    assert r.status_code == 200
    body = r.json()
    assert "Logged the UV" in body["reply"]
    assert len(body["writes"]) == 3
    names = [t["name"] for t in msgs.calls[0]["tools"]]
    assert names == ["query_db", "add_observation", "log_event", "append_notes"]

    conn = sqlite3.connect(_db.DB_PATH)
    obs = conn.execute(
        "SELECT id, text FROM observations WHERE tank_id=? AND source='manual'",
        (tank_id,),
    ).fetchone()
    link = conn.execute(
        "SELECT entity_id FROM observation_links WHERE observation_id=?",
        (obs[0],),
    ).fetchone()[0]
    ev = conn.execute(
        "SELECT event_type FROM events WHERE tank_id=?", (tank_id,)
    ).fetchone()[0]
    eq_notes = conn.execute(
        "SELECT notes FROM tank_equipment WHERE id=?", (eq_id,)
    ).fetchone()[0]
    conn.close()
    assert note in obs[1]
    assert link == eq_id
    assert ev == "maintenance"
    assert "UV reverted" in eq_notes


def test_culture_write_stays_on_viewed_station(client):
    viewed = _create_culture(client, "Scratch Station", kind="other")
    other = _create_culture(client, "Daphnia", kind="daphnia")
    result = apply_culture_write(viewed, "log_culture_note", {
        "notes": "Should land on the viewed station, not Daphnia.",
        "culture_id": other,
        "culture_name": "Daphnia",
    })
    assert result.get("ok") is True
    assert result["culture_id"] == viewed
    conn = sqlite3.connect(_db.DB_PATH)
    landed = conn.execute(
        "SELECT culture_id FROM culture_log WHERE id=?", (result["id"],)
    ).fetchone()[0]
    other_n = conn.execute(
        "SELECT COUNT(*) FROM culture_log WHERE culture_id=?", (other,)
    ).fetchone()[0]
    conn.close()
    assert landed == viewed
    assert other_n == 0


def test_culture_log_note_from_chat(client):
    cid = _create_culture(client, "Daphnia", kind="daphnia")
    result = apply_culture_write(cid, "log_culture_note", {
        "notes": "Held feeding this morning; guts looked empty.",
        "kind": "look",
    })
    assert result.get("ok") is True
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT kind, notes, culture_id FROM culture_log WHERE id=?",
        (result["id"],),
    ).fetchone()
    conn.close()
    assert row == ("look", "Held feeding this morning; guts looked empty.", cid)


def test_culture_append_notes(client):
    cid = _create_culture(client, "Green water", kind="green_water")
    result = apply_culture_write(cid, "append_notes", {
        "target": "culture",
        "note": "Moved bins closer to the light bar.",
    })
    assert result.get("ok") is True
    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM cultures WHERE id=?", (cid,)).fetchone()[0]
    conn.close()
    assert "Moved bins closer" in notes


def test_culture_chat_write_tool_loop(client, monkeypatch):
    cid = _create_culture(client, "Daphnia", kind="daphnia")

    class _Msgs(_RecordingMessages):
        def _respond(self, kwargs):
            if kwargs.get("tools") and len(self.calls) == 1:
                return _FakeMessage(
                    [_FakeToolUseBlock("t1", "log_culture_note", {
                        "notes": "Population still thin; holding feed.",
                        "kind": "look",
                    })],
                    stop_reason="tool_use",
                )
            return _FakeMessage(
                [_FakeTextBlock("Noted on the Daphnia log.")],
                stop_reason="end_turn",
            )

    _install_recording(monkeypatch, _Msgs())
    r = client.post(
        f"/cultures/{cid}/chat",
        json={"message": "Note that I'm holding feed, population still thin."},
    )
    assert r.status_code == 200
    assert r.json()["writes"][0]["tool"] == "log_culture_note"
    conn = sqlite3.connect(_db.DB_PATH)
    n = conn.execute(
        "SELECT COUNT(*) FROM culture_log WHERE culture_id=? AND notes LIKE ?",
        (cid, "%holding feed%"),
    ).fetchone()[0]
    conn.close()
    assert n == 1
