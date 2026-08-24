"""Tests for chat persistence + query_db tool (routers/chat.py).

Exercises the real tool-use loop with a fake anthropic client that requests a
query_db tool call before answering, plus SQL-safety checks on _run_query_db,
and the conversation CRUD endpoints.
"""
import json
import sqlite3

import pytest

import database as _db
from routers.chat import _build_system_prompt, _run_query_db, _title_from_message


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


class _FakeMessagesSimple:
    def __init__(self, text="Sure, here's the answer."):
        self._text = text
        self.calls = 0
        self.seen_messages = []

    def create(self, **kwargs):
        self.calls += 1
        self.seen_messages.append(kwargs.get("messages"))
        return _FakeMessage([_FakeTextBlock(self._text)], stop_reason="end_turn")


class _FakeAnthropicSimple:
    def __init__(self, *a, **kw):
        self.messages = _FakeMessagesSimple()


def test_title_from_message_truncates():
    short = "Hello"
    assert _title_from_message(short) == "Hello"
    long = "x" * 80
    title = _title_from_message(long)
    assert len(title) <= 48
    assert title.endswith("…")


def test_system_prompt_includes_multi_turn_style_rules():
    """Follow-ups should not re-brief or open with meta filler."""
    tank = {"name": "Test Tank", "water_type": "fresh", "volume_gallons": 5, "notes": None}
    prompt = _build_system_prompt(
        tank, None, [], [], [], [], None, [], [],
    )
    assert "multi-turn conversation" in prompt
    assert "Do not restate" in prompt
    assert "meta filler" in prompt
    assert "natural continuation" in prompt


def test_chat_uses_query_db_tool_and_answers(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicToolFlow)

    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "When were the Kuhli Loaches added?"})
    assert r.status_code == 200
    body = r.json()
    assert "2026-03-15" in body["reply"]
    assert body["conversation_id"]
    assert "Kuhli" in body["title"] or "When were" in body["title"]


def test_chat_persists_history_in_db_excludes_tool_exchange(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicToolFlow)

    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "When were the Kuhli Loaches added?"})
    assert r.status_code == 200
    conv_id = r.json()["conversation_id"]

    detail = client.get(f"/tanks/{tank_id}/chat/conversations/{conv_id}")
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert len(messages) == 2  # user + final assistant reply only
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "When were the Kuhli Loaches added?"
    assert messages[1]["role"] == "assistant"
    assert "tool" not in json.dumps(messages)


def test_chat_continues_existing_conversation(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    fake = _FakeAnthropicSimple()
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake)

    r1 = client.post(f"/tanks/{tank_id}/chat", json={"message": "What is my GH?"})
    assert r1.status_code == 200
    conv_id = r1.json()["conversation_id"]

    r2 = client.post(
        f"/tanks/{tank_id}/chat",
        json={"message": "And nitrate?", "conversation_id": conv_id},
    )
    assert r2.status_code == 200
    assert r2.json()["conversation_id"] == conv_id

    # Second call should include prior user+assistant turns in the API payload
    last_msgs = fake.messages.seen_messages[-1]
    roles = [m["role"] for m in last_msgs]
    assert roles == ["user", "assistant", "user"]
    assert last_msgs[0]["content"] == "What is my GH?"
    assert last_msgs[2]["content"] == "And nitrate?"

    detail = client.get(f"/tanks/{tank_id}/chat/conversations/{conv_id}")
    assert len(detail.json()["messages"]) == 4


def test_list_conversations_ordered_by_updated(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    r1 = client.post(f"/tanks/{tank_id}/chat", json={"message": "First conversation topic"})
    r2 = client.post(f"/tanks/{tank_id}/chat", json={"message": "Second conversation topic"})
    c1, c2 = r1.json()["conversation_id"], r2.json()["conversation_id"]

    # Bump the first conversation so it becomes most recent
    client.post(
        f"/tanks/{tank_id}/chat",
        json={"message": "Follow-up on first", "conversation_id": c1},
    )

    listed = client.get(f"/tanks/{tank_id}/chat/conversations")
    assert listed.status_code == 200
    ids = [c["id"] for c in listed.json()["conversations"]]
    assert ids[0] == c1
    assert c2 in ids
    assert listed.json()["conversations"][0]["message_count"] == 4  # 2 user + 2 assistant


def test_create_and_delete_conversation(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    created = client.post(f"/tanks/{tank_id}/chat/conversations")
    assert created.status_code == 201
    conv_id = created.json()["id"]
    assert created.json()["title"] == "New conversation"
    assert created.json()["messages"] == []

    # First message titles it
    r = client.post(
        f"/tanks/{tank_id}/chat",
        json={"message": "Hello about shrimp", "conversation_id": conv_id},
    )
    assert r.status_code == 200
    assert "Hello" in r.json()["title"] or "shrimp" in r.json()["title"]

    deleted = client.delete(f"/tanks/{tank_id}/chat/conversations/{conv_id}")
    assert deleted.status_code == 200

    gone = client.get(f"/tanks/{tank_id}/chat/conversations/{conv_id}")
    assert gone.status_code == 404

    listed = client.get(f"/tanks/{tank_id}/chat/conversations")
    assert all(c["id"] != conv_id for c in listed.json()["conversations"])


def test_delete_conversation_wrong_tank_404(client, make_tank, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    t1 = make_tank("Tank A")
    t2 = make_tank("Tank B")
    r = client.post(f"/tanks/{t1}/chat", json={"message": "secret tank A chat"})
    conv_id = r.json()["conversation_id"]

    assert client.delete(f"/tanks/{t2}/chat/conversations/{conv_id}").status_code == 404
    assert client.get(f"/tanks/{t2}/chat/conversations/{conv_id}").status_code == 404
    # Still exists on the correct tank
    assert client.get(f"/tanks/{t1}/chat/conversations/{conv_id}").status_code == 200


def test_chat_unknown_conversation_404(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    r = client.post(
        f"/tanks/{tank_id}/chat",
        json={"message": "hi", "conversation_id": 99999},
    )
    assert r.status_code == 404


def test_run_query_db_rejects_non_select(client, tank_id):
    result = _run_query_db("DELETE FROM tanks WHERE id = 1", tank_id)
    assert "error" in result


def test_run_query_db_rejects_multi_statement(client, tank_id):
    result = _run_query_db("SELECT * FROM tanks; DROP TABLE tanks;", tank_id)
    assert "error" in result


def test_run_query_db_readonly_connection_blocks_write_even_if_select_prefixed(client, tank_id):
    # Even a syntactically-invalid attempt to sneak a write past the regex must fail,
    # because _run_query_db opens the DB in SQLite read-only mode regardless of SQL text.
    result = _run_query_db("SELECT 1; UPDATE tanks SET name='hacked' WHERE id=1", tank_id)
    assert "error" in result
    conn = sqlite3.connect(_db.DB_PATH)
    name = conn.execute("SELECT name FROM tanks WHERE id=?", (tank_id,)).fetchone()[0]
    conn.close()
    assert name != "hacked"


def test_run_query_db_returns_rows_for_valid_select(client, tank_id):
    result = _run_query_db(f"SELECT id, name FROM tanks WHERE id = {tank_id}", tank_id)
    assert "rows" in result
    assert result["rows"][0]["id"] == tank_id


def test_run_query_db_requires_tank_id_on_tank_tables(client, tank_id):
    result = _run_query_db("SELECT ph FROM test_results", tank_id)
    assert "error" in result
    assert "tank_id" in result["error"]


def test_run_query_db_rejects_other_tank_id(client, tank_id):
    result = _run_query_db("SELECT ph FROM test_results WHERE tank_id = 999", tank_id)
    assert "error" in result


def test_run_query_db_rejects_or_bypass(client, tank_id):
    result = _run_query_db(
        f"SELECT ph FROM test_results WHERE tank_id = {tank_id} OR 1=1", tank_id
    )
    assert "error" in result


def test_run_query_db_rejects_union_and_sqlite_master(client, tank_id):
    assert "error" in _run_query_db(
        f"SELECT ph FROM test_results WHERE tank_id = {tank_id} UNION SELECT name FROM tanks",
        tank_id,
    )
    assert "error" in _run_query_db("SELECT sql FROM sqlite_master", tank_id)


def test_run_query_db_allows_scoped_and_or_on_other_columns(client, tank_id):
    result = _run_query_db(
        f"SELECT ph FROM test_results WHERE tank_id = {tank_id} AND (ph > 6 OR gh > 4)",
        tank_id,
    )
    assert "rows" in result


def test_run_query_db_rejects_chat_messages_without_join(client, tank_id):
    result = _run_query_db("SELECT content FROM chat_messages", tank_id)
    assert "error" in result


def test_chat_no_api_key_still_returns_503(client, tank_id, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "hello"})
    assert r.status_code == 503


def test_chat_full_page_new_conversation(client, tank_id):
    r = client.get(f"/tanks/{tank_id}/chat/new")
    assert r.status_code == 200
    assert "New conversation" in r.text
    assert "chat-page" in r.text
    assert "chat-page-messages" in r.text


def test_chat_full_page_existing_conversation(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "Tell me about nitrates"})
    conv_id = r.json()["conversation_id"]

    page = client.get(f"/tanks/{tank_id}/chat/c/{conv_id}")
    assert page.status_code == 200
    assert "chat-page" in page.text
    assert "Tell me about nitrates" in page.text
    # HTML-escaped apostrophe: here's → here&#39;s
    assert "Sure, here" in page.text and "the answer." in page.text


def test_chat_full_page_unknown_conversation_404(client, tank_id):
    assert client.get(f"/tanks/{tank_id}/chat/c/99999").status_code == 404


def test_chat_cascade_deletes_with_tank(client, make_tank, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    tid = make_tank("Doomed")
    r = client.post(f"/tanks/{tid}/chat", json={"message": "about to vanish"})
    conv_id = r.json()["conversation_id"]

    # Delete tank via app endpoint (confirmation must match tank name)
    del_r = client.post(
        f"/tanks/{tid}/delete",
        data={"confirmation": "Doomed"},
        follow_redirects=False,
    )
    assert del_r.status_code in (303, 302, 200)

    conn = sqlite3.connect(_db.DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM chat_conversations WHERE id=?", (conv_id,)).fetchone()[0]
    m = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?", (conv_id,)).fetchone()[0]
    conn.close()
    assert n == 0
    assert m == 0


def test_chat_rejects_overlong_message(client, tank_id, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "x" * 4001})
    assert r.status_code == 400
    assert r.json()["detail"] == "Message is too long"


def test_chat_ai_error_is_generic(client, tank_id, monkeypatch):
    import anthropic

    class _Boom:
        def __init__(self, *a, **kw):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("secret provider blob xyz")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _Boom)
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "hello"})
    assert r.status_code == 500
    assert r.json()["detail"] == "AI error"
    assert "secret" not in r.text


def test_ai_rate_limit_returns_429(client, tank_id, monkeypatch):
    import anthropic
    import security

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setenv("FATHOM_AI_RATE_LIMIT", "2")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)
    security.reset_ai_rate_limit()
    assert client.post(f"/tanks/{tank_id}/chat", json={"message": "one"}).status_code == 200
    assert client.post(f"/tanks/{tank_id}/chat", json={"message": "two"}).status_code == 200
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "three"})
    assert r.status_code == 429


class _ThinkingBlock:
    """Mirrors anthropic.types.ThinkingBlock: type=thinking, no .text attribute."""

    type = "thinking"

    def __init__(self, thinking="internal reasoning…"):
        self.thinking = thinking
        self.signature = "sig"


class _RecordingMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._respond(kwargs)

    def _respond(self, kwargs):
        raise NotImplementedError


def _install_recording(monkeypatch, messages):
    import anthropic

    class _FakeAnthropic:
        def __init__(self, *a, **kw):
            self.messages = messages

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    return messages


def test_chat_uses_thinking_sized_max_tokens(client, tank_id, monkeypatch):
    from ai_config import CLAUDE_MAX_TOKENS_CHAT

    class _Msgs(_RecordingMessages):
        def _respond(self, kwargs):
            return _FakeMessage([_FakeTextBlock("ok")], stop_reason="end_turn")

    msgs = _install_recording(monkeypatch, _Msgs())
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "How is the tank?"})
    assert r.status_code == 200
    assert msgs.calls[0]["max_tokens"] == CLAUDE_MAX_TOKENS_CHAT
    assert "thinking" not in msgs.calls[0]


def test_chat_retries_when_thinking_burns_budget(client, tank_id, monkeypatch):
    """Prod 2026-08-24: query_db round succeeded, then adaptive thinking used
    the whole 1024-token budget and returned no TextBlock — UI showed the
    misleading 'allotted lookups' fallback."""
    from ai_config import CLAUDE_THINKING_DISABLED, CLAUDE_MAX_TOKENS_CHAT

    recovered = (
        "Otos have a documented death history; seeing 3 is consistent with "
        "the current count after losses."
    )

    class _Msgs(_RecordingMessages):
        def _respond(self, kwargs):
            n = len(self.calls)
            if n == 1:
                return _FakeMessage(
                    [_FakeToolUseBlock("tool_1", "query_db", {
                        "sql": f"SELECT event_type FROM population_events WHERE tank_id = {tank_id}",
                    })],
                    stop_reason="tool_use",
                )
            if n == 2:
                return _FakeMessage(
                    [_ThinkingBlock("spent entire budget thinking")],
                    stop_reason="max_tokens",
                )
            return _FakeMessage([_FakeTextBlock(recovered)], stop_reason="end_turn")

    msgs = _install_recording(monkeypatch, _Msgs())
    r = client.post(
        f"/tanks/{tank_id}/chat",
        json={"message": "Something happened to my Otos. Natural deaths or under feeding?"},
    )
    assert r.status_code == 200
    assert recovered in r.json()["reply"]
    assert "allotted lookups" not in r.json()["reply"]
    assert len(msgs.calls) == 3
    assert "thinking" not in msgs.calls[0]
    assert "thinking" not in msgs.calls[1]
    assert msgs.calls[2].get("thinking") == CLAUDE_THINKING_DISABLED
    assert msgs.calls[0]["max_tokens"] == CLAUDE_MAX_TOKENS_CHAT
    assert msgs.calls[2]["max_tokens"] == CLAUDE_MAX_TOKENS_CHAT


def test_chat_answers_after_tool_round_cap(client, tank_id, monkeypatch):
    """When every tools-on turn keeps requesting query_db, omit tools and answer."""

    class _Msgs(_RecordingMessages):
        def _respond(self, kwargs):
            if kwargs.get("tools"):
                n = len(self.calls)
                return _FakeMessage(
                    [_FakeToolUseBlock(f"tool_{n}", "query_db", {"sql": "SELECT 1"})],
                    stop_reason="tool_use",
                )
            return _FakeMessage(
                [_FakeTextBlock("Here's what I found from the lookups I already ran.")],
                stop_reason="end_turn",
            )

    msgs = _install_recording(monkeypatch, _Msgs())
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "Tell me everything about this tank."})
    assert r.status_code == 200
    assert "Here's what I found" in r.json()["reply"]
    assert "allotted lookups" not in r.json()["reply"]
    assert len(msgs.calls) == 5  # 4 tool rounds + 1 no-tools synthesis
    assert all(c.get("tools") for c in msgs.calls[:4])
    assert "tools" not in msgs.calls[4]


def test_chat_thinking_exhausted_uses_generic_fallback(client, tank_id, monkeypatch):
    from ai_config import CLAUDE_THINKING_DISABLED

    class _Msgs(_RecordingMessages):
        def _respond(self, kwargs):
            return _FakeMessage(
                [_ThinkingBlock("still no text")],
                stop_reason="max_tokens",
            )

    msgs = _install_recording(monkeypatch, _Msgs())
    r = client.post(f"/tanks/{tank_id}/chat", json={"message": "How are the otos?"})
    assert r.status_code == 200
    assert "allotted lookups" not in r.json()["reply"]
    assert "wasn't able to generate a reply" in r.json()["reply"]
    assert len(msgs.calls) == 2
    assert "thinking" not in msgs.calls[0]
    assert msgs.calls[1].get("thinking") == CLAUDE_THINKING_DISABLED
