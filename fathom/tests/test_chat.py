"""Tests for the chat endpoint's query_db tool (routers/chat.py).

Exercises the real tool-use loop with a fake anthropic client that requests a
query_db tool call before answering, plus SQL-safety checks on _run_query_db.
"""
import json
import sqlite3

import pytest

import database as _db
import routers.chat as chat_mod
from routers.chat import _run_query_db


@pytest.fixture(autouse=True)
def _clear_chat_history():
    # _conversations is a module-level dict keyed by tank_id; since each test's `client`
    # fixture gets a fresh DB where ids restart at 1, it must be cleared between tests too.
    chat_mod._conversations.clear()


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _FakeUsage:
    input_tokens = 1
    output_tokens = 1


class _FakeMessage:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _FakeMessagesToolFlow:
    def __init__(self, sql, final_text):
        self._sql = sql
        self._final_text = final_text
        self.calls = 0
        self.seen_messages = []

    def create(self, **kwargs):
        self.calls += 1
        self.seen_messages.append(kwargs.get("messages"))
        if self.calls == 1:
            return _FakeMessage(
                [_FakeToolUseBlock("tool_1", "query_db", {"sql": self._sql})],
                stop_reason="tool_use",
            )
        return _FakeMessage([_FakeTextBlock(self._final_text)], stop_reason="end_turn")


class _FakeAnthropicToolFlow:
    _sql = "SELECT event_type, timestamp FROM population_events WHERE tank_id = 1"
    _final_text = "Kuhli Loaches were added on 2026-03-15."

    def __init__(self, *a, **kw):
        self.messages = _FakeMessagesToolFlow(self._sql, self._final_text)


def test_chat_uses_query_db_tool_and_answers(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicToolFlow)

    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "When were the Kuhli Loaches added?"})
    assert r.status_code == 200
    assert "2026-03-15" in r.json()["reply"]


def test_chat_persisted_history_excludes_tool_exchange(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicToolFlow)

    client.post(f"/tanks/{tank_id}/chat", json={"message": "When were the Kuhli Loaches added?"})

    history = chat_mod._conversations[tank_id]
    assert len(history) == 2  # just the user message + final assistant reply
    assert history[0]["content"] == "When were the Kuhli Loaches added?"
    assert history[1]["role"] == "assistant"
    assert "tool" not in json.dumps(history)


def test_run_query_db_rejects_non_select(client, tank_id):
    result = _run_query_db("DELETE FROM tanks WHERE id = 1")
    assert "error" in result


def test_run_query_db_rejects_multi_statement(client, tank_id):
    result = _run_query_db("SELECT * FROM tanks; DROP TABLE tanks;")
    assert "error" in result


def test_run_query_db_readonly_connection_blocks_write_even_if_select_prefixed(client, tank_id):
    # Even a syntactically-invalid attempt to sneak a write past the regex must fail,
    # because _run_query_db opens the DB in SQLite read-only mode regardless of SQL text.
    result = _run_query_db("SELECT 1; UPDATE tanks SET name='hacked' WHERE id=1")
    assert "error" in result
    conn = sqlite3.connect(_db.DB_PATH)
    name = conn.execute("SELECT name FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    conn.close()
    assert name != "hacked"


def test_run_query_db_returns_rows_for_valid_select(client, tank_id):
    result = _run_query_db(f"SELECT id, name FROM tanks WHERE id = {tank_id}")
    assert "rows" in result
    assert result["rows"][0]["id"] == tank_id


def test_chat_no_api_key_still_returns_503(client, tank_id, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "hello"})
    assert r.status_code == 503
