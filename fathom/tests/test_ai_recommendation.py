"""Tests for the post-test-submit AI recommendation (routers/ai_analysis.run_test_recommendation).

These call the real function directly (imported at module load time, before the
`client` fixture monkeypatches it to a no-op) with a fake anthropic client, since
conftest.py mocks this background task for every other test to avoid API calls.
"""
import asyncio
import sqlite3

import database as _db
from ai_config import CLAUDE_MODEL, CLAUDE_THINKING_DISABLED
from routers.ai_analysis import run_test_recommendation as _real_run_test_recommendation


class _ThinkingBlock:
    type = "thinking"

    def __init__(self, thinking="…"):
        self.thinking = thinking


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeContent:
    """Legacy plain block (no .type) — still supported by _message_text."""

    def __init__(self, text):
        self.text = text


class _FakeUsage:
    input_tokens = 1
    output_tokens = 1


class _FakeMessage:
    def __init__(self, content):
        if isinstance(content, str):
            content = [_FakeContent(content)]
        self.content = content
        self.usage = _FakeUsage()
        self.stop_reason = "end_turn"


class _FakeMessages:
    def __init__(self, content):
        self._content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeMessage(self._content)


def _install_fake(monkeypatch, content="Do a 25% water change per the weekly schedule."):
    import anthropic

    messages = _FakeMessages(content)

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = messages

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    return messages


def _add_test(client, tank_id, **fields):
    data = {"ph": "7.0", **fields}
    r = client.post(
        f"/tanks/{tank_id}/tests",
        data=data,
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_run_test_recommendation_appends_to_notes(client, tank_id, monkeypatch):
    messages = _install_fake(monkeypatch)
    result_id = _add_test(client, tank_id, notes="did a water change today")

    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    assert messages.calls
    assert messages.calls[0].get("model") == CLAUDE_MODEL
    # Adaptive thinking: thinking field omitted on first attempt
    assert "thinking" not in messages.calls[0]

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert notes.startswith("did a water change today")
    assert "AI Recommendation:" in notes
    assert "25% water change" in notes


def test_run_test_recommendation_no_human_notes(client, tank_id, monkeypatch):
    _install_fake(monkeypatch)
    result_id = _add_test(client, tank_id)

    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert notes == "AI Recommendation: Do a 25% water change per the weekly schedule."


def test_run_test_recommendation_handles_thinking_block(client, tank_id, monkeypatch):
    """Sonnet 5 may return ThinkingBlock before text — must still append recommendation."""
    _install_fake(
        monkeypatch,
        content=[_ThinkingBlock(), _TextBlock("Skip water change — just did one.")],
    )
    result_id = _add_test(client, tank_id, notes="checked parameters")

    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert "checked parameters" in notes
    assert "AI Recommendation: Skip water change — just did one." in notes


def test_run_test_recommendation_thinking_only_retries_then_leaves_notes(client, tank_id, monkeypatch):
    """Adaptive empty + no-thinking empty → leave human notes alone (no silent partial)."""
    # Script two empty thinking-only responses (adaptive then disabled retry)
    import anthropic

    class _EmptyMessages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return _FakeMessage([_ThinkingBlock("no room for text")])

    empty = _EmptyMessages()

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = empty

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    result_id = _add_test(client, tank_id, notes="human only")
    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    assert len(empty.calls) == 2
    assert "thinking" not in empty.calls[0]
    assert empty.calls[1].get("thinking") == CLAUDE_THINKING_DISABLED

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert notes == "human only"


def test_run_test_recommendation_retries_without_thinking(client, tank_id, monkeypatch):
    import anthropic

    class _RetryMessages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return _FakeMessage([_ThinkingBlock("thinking only")])
            return _FakeMessage([_TextBlock("Retry recommendation: dose Flourish.")])

    msgs = _RetryMessages()

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = msgs

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    result_id = _add_test(client, tank_id, notes="baseline")
    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    assert len(msgs.calls) == 2
    assert msgs.calls[1].get("thinking") == CLAUDE_THINKING_DISABLED
    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert "Retry recommendation: dose Flourish." in notes


def test_run_test_recommendation_skips_without_api_key(client, tank_id, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result_id = _add_test(client, tank_id, notes="original notes")

    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert notes == "original notes"
