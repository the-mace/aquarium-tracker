"""Tests for the post-test-submit AI recommendation (routers/ai_analysis.run_test_recommendation).

These call the real function directly (imported at module load time, before the
`client` fixture monkeypatches it to a no-op) with a fake anthropic client, since
conftest.py mocks this background task for every other test to avoid API calls.
"""
import asyncio
import sqlite3

import database as _db
from routers.ai_analysis import run_test_recommendation as _real_run_test_recommendation


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeUsage:
    input_tokens = 1
    output_tokens = 1


class _FakeMessage:
    def __init__(self, text):
        self.content = [_FakeContent(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeMessage(self._text)


class _FakeAnthropic:
    def __init__(self, *a, **kw):
        self.messages = _FakeMessages("Do a 25% water change per the weekly schedule.")


def test_run_test_recommendation_appends_to_notes(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.0", "notes": "did a water change today"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]

    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert notes.startswith("did a water change today")
    assert "AI Recommendation:" in notes
    assert "25% water change" in notes


def test_run_test_recommendation_no_human_notes(client, tank_id, monkeypatch):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)

    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.0"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]

    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert notes == "AI Recommendation: Do a 25% water change per the weekly schedule."


def test_run_test_recommendation_skips_without_api_key(client, tank_id, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    r = client.post(
        f"/tanks/{tank_id}/tests",
        data={"ph": "7.0", "notes": "original notes"},
        headers={"Accept": "application/json"},
    )
    result_id = r.json()["id"]

    asyncio.run(_real_run_test_recommendation(tank_id, result_id))

    conn = sqlite3.connect(_db.DB_PATH)
    notes = conn.execute("SELECT notes FROM test_results WHERE id=?", (result_id,)).fetchone()[0]
    conn.close()
    assert notes == "original notes"
