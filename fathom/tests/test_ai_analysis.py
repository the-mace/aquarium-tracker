"""Integration tests for run_ai_analysis (the post-test / post-event AI pipeline).

These call the real function (imported at module load, before conftest monkeypatches
it to a no-op) with a fake Anthropic client — no API credits.

Coverage goals:
- Happy path writes auto observation + tank_state_summary
- Sonnet 5 ThinkingBlock-first responses still extract text
- Adaptive thinking by default; empty text → one thinking-disabled retry
- Terminal failures write a visible auto observation (not log-only)
- summary-status ready on success OR failure (wait-page unblocks)
- Dashboard surfaces failure banner + keeps stale summary
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import database as _db
from ai_config import (
    CLAUDE_MODEL,
    CLAUDE_THINKING_DISABLED,
    ANALYSIS_FAILURE_PREFIX,
    CLAUDE_MAX_TOKENS_ANALYSIS,
    CLAUDE_MAX_TOKENS_CHAT,
)
from routers.ai_analysis import run_ai_analysis as _real_run_ai_analysis


# ── Fake Anthropic surface ──────────────────────────────────────────────────

class _ThinkingBlock:
    """Mirrors anthropic.types.ThinkingBlock: type=thinking, no .text attribute."""

    type = "thinking"

    def __init__(self, thinking="internal reasoning…"):
        self.thinking = thinking
        self.signature = "sig"


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Usage:
    input_tokens = 10
    output_tokens = 20


class _Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


def _text_only(text):
    return _Message([_TextBlock(text)])


def _thinking_then_text(text):
    """Sonnet 5 adaptive-thinking shape that crashed content[0].text."""
    return _Message([_ThinkingBlock(), _TextBlock(text)])


def _thinking_only():
    return _Message([_ThinkingBlock("spent entire budget thinking")], stop_reason="max_tokens")


class _ScriptedMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError(
                f"unexpected extra Claude call (already used all scripted responses); kwargs={kwargs!r}"
            )
        return self._responses.pop(0)


def _install_fake(monkeypatch, responses):
    """Swap anthropic.Anthropic for a client that returns the scripted messages."""
    import anthropic

    scripted = _ScriptedMessages(responses)

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = scripted

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    return scripted


def _add_test(client, tank_id, **fields):
    data = {"ph": "7.2", "nitrate": "10", **fields}
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data=data,
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _db_rows(sql, params=()):
    conn = sqlite3.connect(_db.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def _assert_model(calls):
    assert calls, "expected at least one messages.create call"
    for kwargs in calls:
        assert kwargs.get("model") == CLAUDE_MODEL, kwargs


def _thinking_mode(kwargs):
    """None = adaptive (field omitted); dict = explicit thinking config."""
    return kwargs.get("thinking")


# ── Happy path ──────────────────────────────────────────────────────────────

def test_run_ai_analysis_writes_auto_observation_and_summary(client, tank_id, monkeypatch):
    result_id = _add_test(client, tank_id, notes="weekly check")
    scripted = _install_fake(monkeypatch, [
        _text_only("Analysis: parameters look stable."),
        _text_only("Summary: healthy shrimp tank, nitrate 10 ppm."),
    ])

    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    _assert_model(scripted.calls)
    assert len(scripted.calls) == 2  # analysis + summary
    # Adaptive first: thinking field omitted
    assert _thinking_mode(scripted.calls[0]) is None
    assert scripted.calls[0]["max_tokens"] == CLAUDE_MAX_TOKENS_ANALYSIS

    auto = _db_rows(
        "SELECT source, text, related_test_id FROM observations "
        "WHERE tank_id=? AND source='auto' AND related_test_id=?",
        (tank_id, result_id),
    )
    assert len(auto) == 1
    assert auto[0]["text"] == "Analysis: parameters look stable."

    summary = _db_rows(
        "SELECT summary_text FROM tank_state_summary WHERE tank_id=?", (tank_id,)
    )
    assert len(summary) == 1
    assert "healthy shrimp tank" in summary[0]["summary_text"]


def test_run_ai_analysis_handles_thinking_block_before_text(client, tank_id, monkeypatch):
    """Regression: prod 2026-07-27 — content[0] was ThinkingBlock, .text AttributeError."""
    result_id = _add_test(client, tank_id)
    scripted = _install_fake(monkeypatch, [
        _thinking_then_text("Analysis after thinking."),
        _thinking_then_text("Summary after thinking."),
    ])

    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    _assert_model(scripted.calls)
    auto = _db_rows(
        "SELECT text FROM observations WHERE tank_id=? AND source='auto' AND related_test_id=?",
        (tank_id, result_id),
    )
    assert len(auto) == 1
    assert auto[0]["text"] == "Analysis after thinking."
    summary = _db_rows(
        "SELECT summary_text FROM tank_state_summary WHERE tank_id=?", (tank_id,)
    )
    assert summary[0]["summary_text"] == "Summary after thinking."


def test_run_ai_analysis_retries_without_thinking_when_empty(client, tank_id, monkeypatch):
    """Full thinking budget, no TextBlock → retry with thinking disabled → success."""
    result_id = _add_test(client, tank_id)
    scripted = _install_fake(monkeypatch, [
        _thinking_only(),  # analysis attempt 1 (adaptive) — empty text
        _text_only("Analysis recovered without thinking."),  # analysis attempt 2
        _text_only("Summary ok."),  # summary attempt 1
    ])

    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    _assert_model(scripted.calls)
    assert len(scripted.calls) == 3
    assert _thinking_mode(scripted.calls[0]) is None  # adaptive
    assert _thinking_mode(scripted.calls[1]) == CLAUDE_THINKING_DISABLED
    assert _thinking_mode(scripted.calls[2]) is None  # summary adaptive

    auto = _db_rows(
        "SELECT text FROM observations WHERE tank_id=? AND source='auto' AND related_test_id=?",
        (tank_id, result_id),
    )
    assert len(auto) == 1
    assert auto[0]["text"] == "Analysis recovered without thinking."
    summary = _db_rows(
        "SELECT summary_text FROM tank_state_summary WHERE tank_id=?", (tank_id,)
    )
    assert summary[0]["summary_text"] == "Summary ok."


def test_run_ai_analysis_empty_after_retry_records_failure(client, tank_id, monkeypatch):
    """Both adaptive + no-thinking empty → visible failure, stale summary kept."""
    result_id = _add_test(client, tank_id)
    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute(
        "INSERT INTO tank_state_summary (tank_id, summary_text, generated_at) "
        "VALUES (?, 'STALE 7/16 summary', '2026-07-16 12:00:00')",
        (tank_id,),
    )
    conn.commit()
    conn.close()

    scripted = _install_fake(monkeypatch, [
        _thinking_only(),  # analysis adaptive
        _thinking_only(),  # analysis no-thinking retry still empty
    ])

    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    assert len(scripted.calls) == 2
    assert _thinking_mode(scripted.calls[1]) == CLAUDE_THINKING_DISABLED

    fails = _db_rows(
        "SELECT text, related_test_id FROM observations "
        "WHERE tank_id=? AND source='auto' AND text LIKE ?",
        (tank_id, f"{ANALYSIS_FAILURE_PREFIX}%"),
    )
    assert len(fails) == 1
    assert fails[0]["related_test_id"] == result_id
    assert "no analysis text" in fails[0]["text"].lower() or "retry" in fails[0]["text"].lower()

    summary = _db_rows(
        "SELECT summary_text, generated_at FROM tank_state_summary WHERE tank_id=?",
        (tank_id,),
    )
    assert summary[0]["summary_text"] == "STALE 7/16 summary"
    assert summary[0]["generated_at"] == "2026-07-16 12:00:00"


def test_run_ai_analysis_exception_records_failure(client, tank_id, monkeypatch):
    result_id = _add_test(client, tank_id)

    import anthropic

    class _BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("simulated API outage")

    class _Boom:
        def __init__(self, *a, **kw):
            self.messages = _BoomMessages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _Boom)

    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    fails = _db_rows(
        "SELECT text FROM observations WHERE tank_id=? AND source='auto' AND text LIKE ?",
        (tank_id, f"{ANALYSIS_FAILURE_PREFIX}%"),
    )
    assert len(fails) == 1
    assert "simulated API outage" in fails[0]["text"]


def test_run_ai_analysis_missing_api_key_records_failure(client, tank_id, monkeypatch):
    result_id = _add_test(client, tank_id)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    fails = _db_rows(
        "SELECT text FROM observations WHERE tank_id=? AND source='auto' AND text LIKE ?",
        (tank_id, f"{ANALYSIS_FAILURE_PREFIX}%"),
    )
    assert len(fails) == 1
    assert "ANTHROPIC_API_KEY" in fails[0]["text"]


# ── Wait-page + dashboard contracts ─────────────────────────────────────────

def test_run_ai_analysis_makes_summary_status_ready(client, tank_id, monkeypatch):
    result_id = _add_test(client, tank_id)
    since = "2026-07-27 17:21:31"
    assert client.get(
        f"/tanks/{tank_id}/tests/summary-status?since={since}&result_id={result_id}"
    ).json() == {"ready": False, "error": False}

    _install_fake(monkeypatch, [
        _text_only("Analysis ok."),
        _text_only("Fresh dashboard summary for today."),
    ])
    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    status = client.get(
        f"/tanks/{tank_id}/tests/summary-status?since={since}&result_id={result_id}"
    )
    assert status.json() == {"ready": True, "error": False}


def test_summary_status_ready_on_analysis_failure(client, tank_id, monkeypatch):
    """Wait page must unblock on failure so the user sees the error, not a silent timeout."""
    result_id = _add_test(client, tank_id)
    since = "2000-01-01 00:00:00"

    _install_fake(monkeypatch, [_thinking_only(), _thinking_only()])
    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    status = client.get(
        f"/tanks/{tank_id}/tests/summary-status?since={since}&result_id={result_id}"
    )
    assert status.json() == {"ready": True, "error": True}


def test_run_ai_analysis_dashboard_shows_new_summary_not_stale(client, tank_id, monkeypatch):
    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute(
        "INSERT INTO tank_state_summary (tank_id, summary_text, generated_at) "
        "VALUES (?, 'OLD NOTES FROM 7/16', '2026-07-16 12:00:00')",
        (tank_id,),
    )
    conn.commit()
    conn.close()

    before = client.get(f"/tanks/{tank_id}")
    assert "OLD NOTES FROM 7/16" in before.text

    result_id = _add_test(client, tank_id)
    _install_fake(monkeypatch, [
        _thinking_then_text("New analysis for 7/27."),
        _thinking_then_text("NEW NOTES FROM 7/27 — nitrates holding."),
    ])
    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    after = client.get(f"/tanks/{tank_id}")
    assert "NEW NOTES FROM 7/27" in after.text
    assert "OLD NOTES FROM 7/16" not in after.text


def test_run_ai_analysis_failure_visible_on_dashboard(client, tank_id, monkeypatch):
    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute(
        "INSERT INTO tank_state_summary (tank_id, summary_text, generated_at) "
        "VALUES (?, 'STALE KEEP ME', '2026-07-16 12:00:00')",
        (tank_id,),
    )
    conn.commit()
    conn.close()

    result_id = _add_test(client, tank_id)
    _install_fake(monkeypatch, [_thinking_only(), _thinking_only()])
    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    page = client.get(f"/tanks/{tank_id}?ai=failed")
    assert page.status_code == 200
    assert "AI analysis failed" in page.text
    assert "STALE KEEP ME" in page.text  # previous summary not wiped
    assert ANALYSIS_FAILURE_PREFIX in page.text


def test_run_ai_analysis_observation_appears_on_observations_and_timeline(client, tank_id, monkeypatch):
    result_id = _add_test(client, tank_id)
    _install_fake(monkeypatch, [
        _text_only("Unique analysis marker XYZ-991."),
        _text_only("Summary text."),
    ])
    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    page = client.get(f"/tanks/{tank_id}/observations")
    assert page.status_code == 200
    assert "Unique analysis marker XYZ-991." in page.text
    assert "AI Analysis" in page.text

    timeline = client.get(f"/tanks/{tank_id}/timeline")
    assert timeline.status_code == 200
    assert "Unique analysis marker XYZ-991." in timeline.text


# ── Issue review branch ─────────────────────────────────────────────────────

def test_run_ai_analysis_with_open_issue_runs_issue_review(client, tank_id, monkeypatch):
    conn = sqlite3.connect(_db.DB_PATH)
    cur = conn.execute(
        "INSERT INTO issues (tank_id, title, status, opened_at) VALUES (?, 'Algae', 'open', datetime('now'))",
        (tank_id,),
    )
    issue_id = cur.lastrowid
    conn.commit()
    conn.close()

    result_id = _add_test(client, tank_id)
    scripted = _install_fake(monkeypatch, [
        _text_only("Analysis with issue context."),
        _text_only(f'[{{"issue_id": {issue_id}, "status": "monitoring", "reason": "improving"}}]'),
        _text_only("Summary after issue review."),
    ])

    asyncio.run(_real_run_ai_analysis(tank_id, "test", result_id))

    _assert_model(scripted.calls)
    assert len(scripted.calls) == 3

    status = _db_rows("SELECT status FROM issues WHERE id=?", (issue_id,))[0]["status"]
    assert status == "monitoring"


# ── Shape / config guards ───────────────────────────────────────────────────

def test_thinking_block_has_no_text_attr():
    block = _ThinkingBlock()
    assert block.type == "thinking"
    assert not hasattr(block, "text")
    try:
        _ = SimpleNamespace(content=[block]).content[0].text
        raised = False
    except AttributeError:
        raised = True
    assert raised


def test_claude_budgets_leave_room_for_thinking():
    """Budgets must be well above pre-Sonnet-5 text-only sizes."""
    assert CLAUDE_MAX_TOKENS_ANALYSIS >= 4096
    assert CLAUDE_MAX_TOKENS_CHAT >= 4096
    assert ANALYSIS_FAILURE_PREFIX.startswith("AI analysis failed")
