"""Tests for culture Ask AI (routers/chat.py culture_router).

Conversations live on a culture station page; the model sees ALL cultures
and cannot query tank tables.
"""
import sqlite3

import database as _db
from routers.chat import (
    _build_culture_system_prompt,
    _gather_cultures_context,
    _run_query_db,
    _title_from_message,
)


JSON = {"Accept": "application/json"}


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
        self.system = None
        self.tools = None

    def create(self, **kwargs):
        self.calls += 1
        self.seen_messages.append(kwargs.get("messages"))
        self.system = kwargs.get("system")
        self.tools = kwargs.get("tools")
        if self.calls == 1:
            return _FakeMessage(
                [_FakeToolUseBlock("tool_1", "query_db", {"sql": self._sql})],
                stop_reason="tool_use",
            )
        return _FakeMessage([_FakeTextBlock(self._final_text)], stop_reason="end_turn")


class _FakeAnthropicToolFlow:
    _sql = "SELECT name, kind FROM cultures ORDER BY name"
    _final_text = "Green water feeds the Daphnia station."

    def __init__(self, *a, **kw):
        self.messages = _FakeMessagesToolFlow(self._sql, self._final_text)


class _FakeMessagesSimple:
    def __init__(self, text="Sure, here's the culture answer."):
        self._text = text
        self.calls = 0
        self.seen_messages = []
        self.system = None

    def create(self, **kwargs):
        self.calls += 1
        self.seen_messages.append(kwargs.get("messages"))
        self.system = kwargs.get("system")
        return _FakeMessage([_FakeTextBlock(self._text)], stop_reason="end_turn")


class _FakeAnthropicSimple:
    def __init__(self, *a, **kw):
        self.messages = _FakeMessagesSimple()


class _CapturingAnthropic:
    """Captures the system prompt from the live chat() call."""
    last_system = None

    def __init__(self, *a, **kw):
        self.messages = self

    def create(self, **kwargs):
        _CapturingAnthropic.last_system = kwargs.get("system")
        return _FakeMessage([_FakeTextBlock("ok")], stop_reason="end_turn")


def _create_culture(client, name="Live Food", **extra):
    data = {"name": name, **extra}
    r = client.post("/cultures", data=data, headers=JSON)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_culture_system_prompt_includes_all_stations_not_tanks(client):
    daph = _create_culture(client, "Daphnia", kind="daphnia")
    _create_culture(client, "Green water", kind="green_water", destination=f"culture:{daph}")

    from database import get_db
    with get_db() as conn:
        ctx = _gather_cultures_context(conn)
        current = next(s["culture"] for s in ctx["stations"] if s["culture"]["id"] == daph)
        prompt = _build_culture_system_prompt(current, ctx["stations"], ctx.get("bench_air"))

    assert "Daphnia" in prompt
    assert "Green water" in prompt
    assert "currently viewing" in prompt
    assert "ALL culture stations" in prompt
    assert "this chat is for cultures only" in prompt
    assert "Inhabitants:" not in prompt
    assert "Latest Water Parameters" not in prompt
    assert "Home Water" not in prompt
    assert "Fill water" not in prompt
    assert "multi-turn conversation" in prompt


def test_culture_chat_uses_query_db_across_stations(client, monkeypatch):
    import anthropic
    daph = _create_culture(client, "Daphnia", kind="daphnia")
    _create_culture(client, "Green water", kind="green_water")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicToolFlow)

    r = client.post(f"/cultures/{daph}/chat", json={"message": "What cultures do I have?"})
    assert r.status_code == 200
    body = r.json()
    assert "Green water feeds the Daphnia station." in body["reply"]
    assert body["conversation_id"]


def test_culture_chat_system_prompt_has_every_station(client, monkeypatch):
    import anthropic
    daph = _create_culture(client, "Daphnia", kind="daphnia")
    _create_culture(client, "Green water", kind="green_water")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    _CapturingAnthropic.last_system = None
    monkeypatch.setattr(anthropic, "Anthropic", _CapturingAnthropic)

    r = client.post(f"/cultures/{daph}/chat", json={"message": "How are the cultures?"})
    assert r.status_code == 200
    prompt = _CapturingAnthropic.last_system or ""
    assert "Daphnia" in prompt
    assert "Green water" in prompt
    assert "Inhabitants:" not in prompt
    assert "Latest Water Parameters" not in prompt
    assert "query_db" in prompt


def test_culture_chat_persists_on_culture_not_tank(client, tank_id, monkeypatch):
    import anthropic
    cid = _create_culture(client, "Daphnia", kind="daphnia")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    r = client.post(f"/cultures/{cid}/chat", json={"message": "How dense are the daphnia?"})
    assert r.status_code == 200
    conv_id = r.json()["conversation_id"]

    conn = sqlite3.connect(_db.DB_PATH)
    row = conn.execute(
        "SELECT tank_id, culture_id FROM chat_conversations WHERE id=?", (conv_id,)
    ).fetchone()
    conn.close()
    assert row[0] is None
    assert row[1] == cid

    listed = client.get(f"/cultures/{cid}/chat/conversations")
    assert listed.status_code == 200
    ids = [c["id"] for c in listed.json()["conversations"]]
    assert conv_id in ids

    tank_listed = client.get(f"/tanks/{tank_id}/chat/conversations")
    assert all(c["id"] != conv_id for c in tank_listed.json()["conversations"])


def test_culture_chat_continues_existing_conversation(client, monkeypatch):
    import anthropic
    cid = _create_culture(client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    fake = _FakeAnthropicSimple()
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake)

    r1 = client.post(f"/cultures/{cid}/chat", json={"message": "How is density?"})
    assert r1.status_code == 200
    conv_id = r1.json()["conversation_id"]

    r2 = client.post(
        f"/cultures/{cid}/chat",
        json={"message": "And guts?", "conversation_id": conv_id},
    )
    assert r2.status_code == 200
    assert r2.json()["conversation_id"] == conv_id

    last_msgs = fake.messages.seen_messages[-1]
    roles = [m["role"] for m in last_msgs]
    assert roles == ["user", "assistant", "user"]
    assert last_msgs[0]["content"] == "How is density?"
    assert last_msgs[2]["content"] == "And guts?"


def test_culture_chat_wrong_culture_404(client, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    c1 = _create_culture(client, "A")
    c2 = _create_culture(client, "B")
    r = client.post(f"/cultures/{c1}/chat", json={"message": "secret culture A chat"})
    conv_id = r.json()["conversation_id"]

    assert client.delete(f"/cultures/{c2}/chat/conversations/{conv_id}").status_code == 404
    assert client.get(f"/cultures/{c2}/chat/conversations/{conv_id}").status_code == 404
    assert client.get(f"/cultures/{c1}/chat/conversations/{conv_id}").status_code == 200


def test_culture_chat_unknown_culture_404(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    r = client.post("/cultures/99999/chat", json={"message": "hello"})
    assert r.status_code == 404


def test_culture_query_db_allows_all_cultures(client):
    daph = _create_culture(client, "Daphnia", kind="daphnia")
    green = _create_culture(client, "Green water", kind="green_water")
    result = _run_query_db("SELECT id, name FROM cultures ORDER BY name", scope="culture")
    assert "rows" in result
    names = {row["name"] for row in result["rows"]}
    assert names == {"Daphnia", "Green water"}
    ids = {row["id"] for row in result["rows"]}
    assert ids == {daph, green}


def test_culture_query_db_rejects_tank_tables(client, tank_id):
    result = _run_query_db("SELECT name FROM tanks", scope="culture")
    assert "error" in result
    assert "culture" in result["error"].lower()

    result = _run_query_db(
        f"SELECT ph FROM test_results WHERE tank_id = {tank_id}", scope="culture"
    )
    assert "error" in result

    result = _run_query_db("SELECT * FROM inhabitants", scope="culture")
    assert "error" in result


def test_culture_query_db_rejects_union_and_non_select(client):
    assert "error" in _run_query_db(
        "SELECT name FROM cultures UNION SELECT name FROM tanks", scope="culture"
    )
    assert "error" in _run_query_db("DELETE FROM cultures", scope="culture")
    assert "error" in _run_query_db("SELECT sql FROM sqlite_master", scope="culture")


def test_culture_chat_no_api_key_503(client, monkeypatch):
    cid = _create_culture(client)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(f"/cultures/{cid}/chat", json={"message": "hello"})
    assert r.status_code == 503


def test_culture_chat_full_page(client):
    cid = _create_culture(client, "Daphnia")
    r = client.get(f"/cultures/{cid}/chat/new")
    assert r.status_code == 200
    assert "New conversation" in r.text
    assert "chat-page" in r.text
    assert "your cultures" in r.text
    assert f"const CULTURE_ID = {cid}" in r.text
    assert "const TANK_ID" not in r.text


def test_culture_chat_full_page_existing_conversation(client, monkeypatch):
    import anthropic
    cid = _create_culture(client, "Daphnia")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    r = client.post(f"/cultures/{cid}/chat", json={"message": "Tell me about tint"})
    conv_id = r.json()["conversation_id"]

    page = client.get(f"/cultures/{cid}/chat/c/{conv_id}")
    assert page.status_code == 200
    assert "chat-page" in page.text
    assert "Tell me about tint" in page.text
    assert "Sure, here" in page.text


def test_culture_chat_full_page_unknown_conversation_404(client):
    cid = _create_culture(client)
    assert client.get(f"/cultures/{cid}/chat/c/99999").status_code == 404


def test_culture_chat_cascade_deletes_with_culture(client, monkeypatch):
    import anthropic
    cid = _create_culture(client, "Doomed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropicSimple)

    r = client.post(f"/cultures/{cid}/chat", json={"message": "about to vanish"})
    conv_id = r.json()["conversation_id"]

    del_r = client.post(f"/cultures/{cid}/delete", follow_redirects=False)
    assert del_r.status_code in (303, 302, 200)

    conn = sqlite3.connect(_db.DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM chat_conversations WHERE id=?", (conv_id,)).fetchone()[0]
    m = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE conversation_id=?", (conv_id,)).fetchone()[0]
    conn.close()
    assert n == 0
    assert m == 0


def test_culture_detail_has_ask_ai(client):
    cid = _create_culture(client, "Daphnia")
    r = client.get(f"/cultures/{cid}")
    assert r.status_code == 200
    assert "Ask AI" in r.text
    assert "startNewChat()" in r.text
    assert f"const CULTURE_ID = {cid}" in r.text
    assert f"/cultures/{cid}/chat/new" in r.text


def test_culture_chat_rejects_overlong_message(client, monkeypatch):
    cid = _create_culture(client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    r = client.post(f"/cultures/{cid}/chat", json={"message": "x" * 4001})
    assert r.status_code == 400


def test_culture_chat_ai_error_is_generic(client, monkeypatch):
    import anthropic

    class _Boom:
        def __init__(self, *a, **kw):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("secret provider blob xyz")

    cid = _create_culture(client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _Boom)
    r = client.post(f"/cultures/{cid}/chat", json={"message": "hello"})
    assert r.status_code == 500
    assert r.json()["detail"] == "AI error"
    assert "secret" not in r.text


def test_title_from_message_still_used():
    assert _title_from_message("Hello cultures") == "Hello cultures"


class _ThinkingBlock:
    type = "thinking"

    def __init__(self, thinking="internal reasoning…"):
        self.thinking = thinking
        self.signature = "sig"


def test_culture_chat_uses_thinking_sized_max_tokens(client, monkeypatch):
    """Culture Ask AI shares _claude_chat_reply with tanks (Sonnet 5 thinking budget)."""
    import anthropic
    from ai_config import CLAUDE_MAX_TOKENS_CHAT

    calls = []

    class _Msgs:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _FakeMessage([_FakeTextBlock("ok")], stop_reason="end_turn")

    class _Fake:
        def __init__(self, *a, **kw):
            self.messages = _Msgs()

    cid = _create_culture(client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _Fake)
    r = client.post(f"/cultures/{cid}/chat", json={"message": "How is density?"})
    assert r.status_code == 200
    assert calls[0]["max_tokens"] == CLAUDE_MAX_TOKENS_CHAT
    assert "thinking" not in calls[0]


def test_culture_chat_retries_when_thinking_burns_budget(client, monkeypatch):
    import anthropic
    from ai_config import CLAUDE_THINKING_DISABLED

    recovered = "Last look was faint tint on both green-water bins."
    calls = []

    class _Msgs:
        def create(self, **kwargs):
            calls.append(kwargs)
            n = len(calls)
            if n == 1:
                return _FakeMessage(
                    [_ThinkingBlock("spent entire budget thinking")],
                    stop_reason="max_tokens",
                )
            return _FakeMessage([_FakeTextBlock(recovered)], stop_reason="end_turn")

    class _Fake:
        def __init__(self, *a, **kw):
            self.messages = _Msgs()

    cid = _create_culture(client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _Fake)
    r = client.post(f"/cultures/{cid}/chat", json={"message": "How is the green water?"})
    assert r.status_code == 200
    assert recovered in r.json()["reply"]
    assert "allotted lookups" not in r.json()["reply"]
    assert len(calls) == 2
    assert "thinking" not in calls[0]
    assert calls[1].get("thinking") == CLAUDE_THINKING_DISABLED
