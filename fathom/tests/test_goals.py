"""Tests for tank goals + cross-tank dependencies + AI progress updates."""
import asyncio
import json
import sqlite3

import database as _db
from routers.ai_analysis import (
    _fmt_goals,
    _parse_goal_progress_updates,
    _parse_goal_review,
    build_goal_progress_prompt,
    build_goal_review_prompt,
    run_goal_progress as _real_run_goal_progress,
)


def _add_goal(client, tank_id, title="GH for Amano", **extra):
    data = {"title": title, **extra}
    if "status" not in data:
        data["status"] = "in_progress"
    r = client.post(
        f"/tanks/{tank_id}/goals",
        data=data,
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_add_goal_returns_id(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/goals",
        data={"title": "Stable GH 7", "target": "GH 7 ±0.5 for 2 weeks"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    assert "id" in r.json()


def test_add_goal_defaults_to_in_progress(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Starts active")
    conn = sqlite3.connect(_db.DB_PATH)
    status = conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()[0]
    conn.close()
    assert status == "in_progress"


def test_review_goal_without_api_key_returns_draft(client, tank_id, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(
        f"/tanks/{tank_id}/goals/review",
        data={"title": "Lower GH", "target": "GH 7"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["proposed"]["title"] == "Lower GH"
    assert body["draft"]["target"] == "GH 7"
    assert body["changed"] is False
    # Review must not create a goal
    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM goals WHERE tank_id=?", (tank_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_review_goal_with_fake_claude(client, tank_id, monkeypatch):
    import anthropic

    class _TextBlock:
        type = "text"
        def __init__(self, text): self.text = text

    class _FakeUsage:
        input_tokens = 1
        output_tokens = 1

    class _FakeMessage:
        def __init__(self, content):
            self.content = content
            self.usage = _FakeUsage()
            self.stop_reason = "end_turn"

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeMessage([_TextBlock(
                '{"reasonable": false, "summary": "Make the target measurable.", '
                '"suggestions": ["Add a stability window"], '
                '"proposed": {"title": "Stabilize GH", "target": "GH 7 for 2 weeks", '
                '"description": "For neocaridina comfort", "notes": ""}}'
            )])

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = _FakeMessages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    r = client.post(
        f"/tanks/{tank_id}/goals/review",
        data={"title": "GH better", "target": "lower"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["reasonable"] is False
    assert "measurable" in body["summary"].lower() or "Make the target" in body["summary"]
    assert body["proposed"]["title"] == "Stabilize GH"
    assert body["proposed"]["target"] == "GH 7 for 2 weeks"
    assert body["changed"] is True
    assert "stability" in " ".join(body["suggestions"]).lower()


def test_parse_goal_review_fallback_and_changed():
    draft = {"title": "A", "target": "B", "description": "C", "notes": ""}
    out = _parse_goal_review("not json", draft)
    assert out["proposed"]["title"] == "A"
    assert out["changed"] is False
    assert out.get("parse_failed") is True
    assert out["reasonable"] is False

    raw = '{"reasonable": true, "summary": "ok", "suggestions": [], "proposed": {"title": "A2", "target": "B", "description": "C", "notes": ""}}'
    out2 = _parse_goal_review(raw, draft)
    assert out2["changed"] is True
    assert out2["proposed"]["title"] == "A2"
    assert not out2.get("parse_failed")


def test_parse_goal_review_rejects_meta_feedback_in_proposed_fields():
    """Reviewer critique must not land in savable title/target/description."""
    from routers.ai_analysis import _looks_like_review_meta

    assert _looks_like_review_meta(
        "Undefined — needs specific destination tank before this can be tracked"
    )
    assert _looks_like_review_meta("Clarify shrimp relocation / GH goal")
    assert _looks_like_review_meta(
        "Draft intent is unclear: Fire Red Shrimp are already stocked."
    )
    assert not _looks_like_review_meta("Raise GH for Amano shrimp")
    assert not _looks_like_review_meta("GH 6–8 for 4+ consecutive weeks")

    draft = {
        "title": "move shrimp when GH ok",
        "target": "higher GH",
        "description": "want neos in fish tank",
        "notes": "",
    }
    raw = json.dumps({
        "reasonable": False,
        "summary": "Intent unclear; pick destination tank.",
        "suggestions": ["Name the destination tank"],
        "proposed": {
            "title": "Clarify shrimp relocation / GH goal",
            "target": "Undefined — needs specific destination tank and measurable GH/KH/temp criteria before this can be tracked",
            "description": "Draft intent is unclear: Fire Red Shrimp are already stocked in this tank.",
            "notes": "",
        },
    })
    out = _parse_goal_review(raw, draft)
    # Meta text rejected → fall back to user's draft for those fields
    assert out["proposed"]["title"] == draft["title"]
    assert out["proposed"]["target"] == draft["target"]
    assert out["proposed"]["description"] == draft["description"]
    assert "unclear" in out["summary"].lower() or out["suggestions"]


def test_build_goal_review_prompt_includes_draft():
    tank = {"name": "Fish", "water_type": "fresh", "volume_gallons": 40}
    draft = {"title": "Amano GH", "target": "GH 6-8", "description": "", "notes": ""}
    prompt = build_goal_review_prompt(tank, draft, [], latest_test={"gh": 10, "timestamp": "2026-08-01"})
    assert "Amano GH" in prompt
    assert "GH=10" in prompt
    assert "proposed" in prompt
    assert "CRITICAL" in prompt or "never put review feedback" in prompt.lower()
    assert "do not hallucinate" in prompt.lower() or "NOT currently in the tank" in prompt


def test_build_goal_review_prompt_separates_zero_count_inhabitants():
    tank = {"name": "Fish Tank", "water_type": "fresh", "volume_gallons": 40}
    draft = {"title": "get red shrimp into tank", "target": "gh good", "description": "", "notes": ""}
    inhabitants = [
        {"common_name": "Otocinclus Catfish", "species": "Otocinclus vittatus", "count": 6},
        {"common_name": "Red Rili Shrimp", "species": "Neocaridina davidi", "count": 0},
        {"common_name": "Amano Shrimp", "species": "Caridina multidentata", "count": 0},
    ]
    prompt = build_goal_review_prompt(
        tank, draft, [], latest_test={"gh": 2, "timestamp": "2026-08-01"}, inhabitants=inhabitants,
    )
    assert "Currently stocked" in prompt
    assert "Otocinclus" in prompt
    assert "NOT currently in the tank" in prompt
    assert "Red Rili" in prompt  # listed as former only
    assert "count=0" in prompt.lower() or "count 0" in prompt


def test_build_goal_review_prompt_includes_other_tanks_stock():
    tank = {"name": "Fish Tank", "water_type": "fresh", "volume_gallons": 40}
    draft = {
        "title": "get red shrimp into tank",
        "target": "gh good",
        "description": "from the shrimp tank",
        "notes": "",
    }
    other = [{
        "name": "Shrimp Tank",
        "volume_gallons": 5,
        "inhabitants": [
            {"common_name": "Fire Red Shrimp", "species": "Neocaridina davidi", "count": 10},
        ],
    }]
    prompt = build_goal_review_prompt(
        tank, draft, [], latest_test={"gh": 2}, inhabitants=[], other_tanks_stock=other,
    )
    assert "Other tanks" in prompt
    assert "Shrimp Tank" in prompt
    assert "Fire Red Shrimp" in prompt
    assert "cross-reference" in prompt.lower() or "source tank" in prompt.lower()


def test_goals_page_has_review_ui(client, tank_id):
    r = client.get(f"/tanks/{tank_id}/goals")
    assert r.status_code == 200
    assert "goal-review-panel" in r.text
    assert "Review Goal" in r.text
    assert "/goals/review" in r.text
    # Create uses explicit button click (Safari-safe), not form submit
    assert "startGoalReview()" in r.text
    assert 'id="review-title" required' not in r.text
    assert 'onclick="startGoalReview()"' in r.text


def test_add_goal_always_queues_progress_even_without_tests(client, tank_id, monkeypatch):
    """New active goals always get an initial AI progress run (no water-test gate)."""
    import routers.ai_analysis as ai

    calls = []

    def _capture(*a, **kw):
        calls.append((a, kw))

    # conftest no-ops run_goal_progress on ai_analysis; goals imports it at call time
    monkeypatch.setattr(ai, "run_goal_progress", _capture)

    r = client.post(
        f"/tanks/{tank_id}/goals",
        data={"title": "Initial progress goal"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    # Starlette BackgroundTasks run after the response in TestClient
    assert len(calls) == 1
    assert calls[0][0][0] == tank_id


def test_add_goal_persisted(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Breeding Neocaridina", target="berried females + juveniles")
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT title, status, target FROM goals WHERE id=?", (goal_id,)
    ).fetchone()
    conn.close()
    assert row[0] == "Breeding Neocaridina"
    assert row[1] == "in_progress"
    assert "berried" in row[2]


def test_pause_and_resume_goal(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Pausable")
    client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={"title": "Pausable", "status": "paused"},
        headers={"Accept": "application/json"},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    assert conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()[0] == "paused"
    client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={"title": "Pausable", "status": "in_progress"},
        headers={"Accept": "application/json"},
    )
    assert conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()[0] == "in_progress"
    conn.close()
    page = client.get(f"/tanks/{tank_id}/goals")
    assert "Pause" in page.text
    assert "Resume" in page.text or "in progress" in page.text.lower()


def test_list_goals_page(client, tank_id):
    _add_goal(client, tank_id, title="Listed goal")
    r = client.get(f"/tanks/{tank_id}/goals")
    assert r.status_code == 200
    assert "Listed goal" in r.text
    assert "Goals" in r.text


def test_dashboard_shows_active_goals(client, tank_id):
    _add_goal(client, tank_id, title="Dashboard goal")
    r = client.get(f"/tanks/{tank_id}")
    assert r.status_code == 200
    assert "Dashboard goal" in r.text


def test_update_goal_to_achieved_sets_achieved_at(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Amano-ready GH")
    r = client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={"title": "Amano-ready GH", "status": "achieved", "achieved_at": "2026-08-01"},
        headers={"Accept": "application/json"},
    )
    assert r.json()["status"] == "updated"
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT status, achieved_at FROM goals WHERE id=?", (goal_id,)).fetchone()
    conn.close()
    assert row[0] == "achieved"
    assert row[1] is not None
    assert row[1].startswith("2026-08-01")


def test_update_to_in_progress(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Work item")
    client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={"title": "Work item", "status": "in_progress"},
        headers={"Accept": "application/json"},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    status = conn.execute("SELECT status FROM goals WHERE id=?", (goal_id,)).fetchone()[0]
    conn.close()
    assert status == "in_progress"


def test_resume_from_achieved_clears_achieved_at(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Resume me")
    client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={"title": "Resume me", "status": "achieved"},
        headers={"Accept": "application/json"},
    )
    client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={"title": "Resume me", "status": "in_progress"},
        headers={"Accept": "application/json"},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute("SELECT status, achieved_at FROM goals WHERE id=?", (goal_id,)).fetchone()
    conn.close()
    assert row[0] == "in_progress"
    assert row[1] is None


def test_delete_goal(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Temp")
    r = client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/delete",
        follow_redirects=False,
    )
    assert r.status_code == 303
    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM goals WHERE id=?", (goal_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_dependency_cross_tank(client, make_tank):
    fish = make_tank("Fish Tank")
    shrimp = make_tank("Shrimp Tank")
    breed_id = _add_goal(client, shrimp, title="Successful breeding")
    mature_id = _add_goal(client, shrimp, title="New generation matured")
    amano_id = _add_goal(client, fish, title="GH ready for Amano")
    transfer_id = _add_goal(
        client, fish,
        title="Move Neocaridina from shrimp tank",
        depends_on=[str(breed_id), str(mature_id), str(amano_id)],
    )

    conn = sqlite3.connect(_db.DB_PATH)
    deps = {
        r[0]
        for r in conn.execute(
            "SELECT depends_on_goal_id FROM goal_dependencies WHERE goal_id=?",
            (transfer_id,),
        ).fetchall()
    }
    conn.close()
    assert deps == {breed_id, mature_id, amano_id}

    # List page should show cross-tank dep labels + blocked state
    r = client.get(f"/tanks/{fish}/goals")
    assert r.status_code == 200
    assert "Move Neocaridina" in r.text
    assert "blocked" in r.text.lower()
    assert "Successful breeding" in r.text
    assert "Shrimp Tank" in r.text


def test_edit_preserves_deps_when_status_only_update(client, tank_id, make_tank):
    other = make_tank("Other")
    prereq = _add_goal(client, other, title="Prereq")
    goal_id = _add_goal(client, tank_id, title="Dependent", depends_on=[str(prereq)])
    # Status transition form does not send update_deps — deps must survive
    client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={"title": "Dependent", "status": "in_progress"},
        headers={"Accept": "application/json"},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM goal_dependencies WHERE goal_id=?", (goal_id,)
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_edit_can_replace_deps(client, tank_id, make_tank):
    other = make_tank("Other")
    a = _add_goal(client, other, title="A")
    b = _add_goal(client, other, title="B")
    goal_id = _add_goal(client, tank_id, title="Has A", depends_on=[str(a)])
    client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={
            "title": "Has B",
            "status": "in_progress",
            "update_deps": "1",
            "depends_on": str(b),
        },
        headers={"Accept": "application/json"},
    )
    conn = sqlite3.connect(_db.DB_PATH)
    deps = [
        r[0]
        for r in conn.execute(
            "SELECT depends_on_goal_id FROM goal_dependencies WHERE goal_id=?",
            (goal_id,),
        ).fetchall()
    ]
    conn.close()
    assert deps == [b]


def test_self_dependency_rejected(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="Self")
    r = client.post(
        f"/tanks/{tank_id}/goals/{goal_id}/update",
        data={
            "title": "Self",
            "status": "in_progress",
            "update_deps": "1",
            "depends_on": str(goal_id),
        },
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 400


def test_cycle_rejected(client, tank_id):
    a = _add_goal(client, tank_id, title="A")
    b = _add_goal(client, tank_id, title="B", depends_on=[str(a)])
    # A depends on B would cycle
    r = client.post(
        f"/tanks/{tank_id}/goals/{a}/update",
        data={
            "title": "A",
            "status": "open",
            "update_deps": "1",
            "depends_on": str(b),
        },
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 400


def test_blocked_clears_when_deps_achieved(client, tank_id, make_tank):
    other = make_tank("Prereq Tank")
    prereq = _add_goal(client, other, title="Must finish first")
    goal_id = _add_goal(client, tank_id, title="Blocked then free", depends_on=[str(prereq)])

    page = client.get(f"/tanks/{tank_id}/goals")
    assert "is-blocked" in page.text or "blocked" in page.text.lower()

    client.post(
        f"/tanks/{other}/goals/{prereq}/update",
        data={"title": "Must finish first", "status": "achieved"},
        headers={"Accept": "application/json"},
    )
    page2 = client.get(f"/tanks/{tank_id}/goals")
    # Card should no longer carry is-blocked class for this goal
    assert 'data-blocked="0"' in page2.text


def test_update_nonexistent_404(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/goals/9999/update",
        data={"title": "Ghost", "status": "in_progress"},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 404


def test_add_goal_redirects_without_json_accept(client, tank_id):
    r = client.post(
        f"/tanks/{tank_id}/goals",
        data={"title": "Silent goal"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert f"/tanks/{tank_id}/goals" in r.headers["location"]


def test_reset_tank_deletes_goals(client, tank_id):
    _add_goal(client, tank_id, title="Wipe me")
    conn = sqlite3.connect(_db.DB_PATH)
    name = conn.execute("SELECT name FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    conn.close()
    r = client.post(
        f"/tanks/{tank_id}/reset",
        data={"confirmation": name},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303, 302)
    conn = sqlite3.connect(_db.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM goals WHERE tank_id=?", (tank_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_fmt_goals_includes_target_and_blocked():
    rows = [{
        "title": "Move shrimp",
        "status": "open",
        "target": "GH 8+",
        "description": "From shrimp tank",
        "blocked": True,
        "dependencies": [
            {"title": "Breeding", "status": "in_progress", "tank_name": "Shrimp Tank"},
        ],
        "progress_summary": "Still waiting on breeding.",
    }]
    text = _fmt_goals(rows)
    assert "Move shrimp" in text
    assert "GH 8+" in text
    assert "BLOCKED" in text
    assert "Breeding" in text
    assert "Still waiting" in text


def test_parse_goal_progress_updates():
    raw = '[{"goal_id": 1, "progress_summary": "GH still high."}, {"goal_id": 2, "progress_summary": "On track."}]'
    out = _parse_goal_progress_updates(raw, {1, 2})
    assert len(out) == 2
    assert out[0]["goal_id"] == 1
    assert "high" in out[0]["progress_summary"]


def test_parse_goal_progress_strips_fences_and_unknown_ids():
    raw = '```json\n[{"goal_id": 1, "progress_summary": "ok"}, {"goal_id": 99, "progress_summary": "nope"}]\n```'
    out = _parse_goal_progress_updates(raw, {1})
    assert len(out) == 1
    assert out[0]["goal_id"] == 1


def test_build_goal_progress_prompt_lists_goals():
    tank = {"name": "Fish", "water_type": "fresh", "volume_gallons": 40}
    goals = [{
        "id": 5,
        "title": "Amano GH",
        "status": "open",
        "target": "GH 6-8",
        "dependencies": [],
    }]
    prompt = build_goal_progress_prompt(tank, goals, [], [], [])
    assert "Amano GH" in prompt
    assert "goal_id" in prompt
    assert "id=5" in prompt


def test_run_goal_progress_updates_summaries(client, tank_id, monkeypatch):
    """Drive the real run_goal_progress with a fake Anthropic client."""
    goal_id = _add_goal(client, tank_id, title="Lower GH to 7", target="GH 7 stable")
    # Add a test so context is non-empty
    client.post(
        f"/tanks/{tank_id}/tests",
        data={"gh": "10", "ph": "7.2"},
        headers={"Accept": "application/json"},
    )

    import anthropic

    summary = f'[{{"goal_id": {goal_id}, "progress_summary": "GH is 10; still above the stable-7 target."}}]'

    class _TextBlock:
        type = "text"

        def __init__(self, text):
            self.text = text

    class _FakeUsage:
        input_tokens = 1
        output_tokens = 1

    class _FakeMessage:
        def __init__(self, content):
            self.content = content
            self.usage = _FakeUsage()
            self.stop_reason = "end_turn"

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeMessage([_TextBlock(summary)])

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = _FakeMessages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    asyncio.run(_real_run_goal_progress(tank_id, 1))

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT progress_summary, progress_summary_at FROM goals WHERE id=?",
        (goal_id,),
    ).fetchone()
    conn.close()
    assert row[0] is not None
    assert "GH is 10" in row[0]
    assert row[1] is not None


def test_goals_page_shows_progress_summary(client, tank_id):
    goal_id = _add_goal(client, tank_id, title="With progress")
    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute(
        "UPDATE goals SET progress_summary=?, progress_summary_at=? WHERE id=?",
        ("GH trending down toward target.", "2026-08-12 12:00:00", goal_id),
    )
    conn.commit()
    conn.close()
    r = client.get(f"/tanks/{tank_id}/goals")
    assert r.status_code == 200
    assert "AI progress" in r.text
    assert "GH trending down" in r.text


def test_goals_page_pending_says_generating_not_wait_for_test(client, tank_id):
    _add_goal(client, tank_id, title="Pending blurb")
    r = client.get(f"/tanks/{tank_id}/goals")
    assert r.status_code == 200
    assert "Generating AI progress" in r.text
    assert "after the next water test" not in r.text
