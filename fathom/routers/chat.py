import os
import re
import json
import time
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
from database import get_db, get_db_readonly, get_schema_text, rows_to_list, row_to_dict
from ai_config import CLAUDE_MODEL
from routers.ai_analysis import (
    _fmt_tank_notes, _fmt_inhabitants, _fmt_schedule, _CURRENT_PRACTICES_RULE,
    _fmt_home_water_block, _HOME_WATER_PROMPT_RULE, load_home_water_tests,
)

router = APIRouter(prefix="/tanks/{tank_id}/chat", tags=["chat"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
logger = logging.getLogger(__name__)

MAX_TURNS = 10
MAX_TOOL_ROUNDS = 4
QUERY_ROW_LIMIT = 200
TITLE_MAX_LEN = 48


class ChatMessage(BaseModel):
    message: str
    conversation_id: Optional[int] = None


def _utc_now() -> str:
    """UTC timestamp with fractional seconds so same-second updates sort correctly."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _title_from_message(message: str) -> str:
    text = " ".join((message or "").split())
    if not text:
        return "New conversation"
    if len(text) <= TITLE_MAX_LEN:
        return text
    return text[: TITLE_MAX_LEN - 1].rstrip() + "…"


def _query_db_tool(tank_id):
    return {
        "name": "query_db",
        "description": (
            "Run a single read-only SQL SELECT query against the Fathom database for anything "
            "not already covered by the context above — e.g. full test_results history/trends, "
            "population_events (when an inhabitant was added/died/removed), purchase totals, "
            f"older observations. This tank's id is {tank_id} — filter WHERE tank_id = {tank_id} "
            f"unless the user is deliberately comparing tanks. Returns up to {QUERY_ROW_LIMIT} rows as JSON.\n\n"
            f"Schema:\n{get_schema_text()}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["sql"],
        },
    }


def _run_query_db(sql: str) -> dict:
    stripped = (sql or "").strip().rstrip(";")
    if not re.match(r"(?is)^\s*select\b", stripped):
        return {"error": "Only SELECT statements are allowed."}
    try:
        with get_db_readonly() as conn:
            rows = conn.execute(stripped).fetchmany(QUERY_ROW_LIMIT)
            return {"rows": rows_to_list(rows)}
    except Exception as e:
        return {"error": str(e)}


def _build_system_prompt(tank, latest_test, inhabitants, plants, hardscape, open_issues, summary,
                         recent_obs, schedule_rows=None, home_water_tests=None):
    schedule_rows = schedule_rows or []
    home_water_tests = home_water_tests or []
    parts = [
        "You are an expert aquarium keeper assistant with detailed knowledge of the following tank.",
        f"\nTank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}",
    ]

    if tank.get("manufacturer") or tank.get("model"):
        parts.append(f"Hardware: {(tank.get('manufacturer') or '')} {(tank.get('model') or '')}".strip())

    if tank.get("dimensions_l"):
        parts.append(f"Dimensions: {tank['dimensions_l']}\" × {tank['dimensions_w']}\" × {tank['dimensions_h']}\"")

    if tank.get("substrate_type"):
        sub = tank["substrate_type"]
        if tank.get("substrate_brand"):
            sub += f" ({tank['substrate_brand']})"
        if tank.get("substrate_depth_inches"):
            sub += f", {tank['substrate_depth_inches']}\""
        parts.append(f"Substrate: {sub}")

    if latest_test:
        params = []
        for field in ("ph", "gh", "kh", "ammonia", "nitrite", "nitrate", "tds", "temp"):
            val = latest_test.get(field)
            if val is not None:
                params.append(f"{field.upper()}={val}")
        if params:
            ts = (latest_test.get("timestamp") or "")[:10]
            parts.append(f"\nLatest Water Parameters ({ts}):\n  " + ", ".join(params))
    else:
        parts.append("\nLatest Water Parameters: none recorded")

    parts.append(
        "\nFill water for water changes (tap WC source and/or bottled only — "
        "NOT raw/diagnostic home-water samples):\n"
        + _fmt_home_water_block(home_water_tests)
    )
    parts.append(_HOME_WATER_PROMPT_RULE)

    if inhabitants:
        parts.append("\nInhabitants:\n" + _fmt_inhabitants(inhabitants))
    else:
        parts.append("\nInhabitants: none recorded")

    if plants:
        lines = ["  " + (p.get("common_name") or p.get("species") or "Unknown plant") for p in plants]
        parts.append("\nPlants:\n" + "\n".join(lines))

    if hardscape:
        lines = []
        for h in hardscape:
            qty = h.get("quantity") or 1
            prefix = f"{qty}× " if qty > 1 else ""
            lines.append(f"  {prefix}{h['item']}")
        parts.append("\nHardscape:\n" + "\n".join(lines))

    if open_issues:
        lines = [f"  [{i['status'].upper()}] {i['title']}: {i.get('description','')}" for i in open_issues]
        parts.append("\nOpen Issues:\n" + "\n".join(lines))

    parts.append(
        "\nRecurring schedule (current planned feeding/dosing/maintenance — authoritative for "
        "what the keeper currently does):\n" + _fmt_schedule(schedule_rows)
    )

    if summary and summary.get("summary_text"):
        parts.append(f"\nRecent AI Summary:\n{summary['summary_text']}")

    if recent_obs:
        parts.append("\nRecent Observations:")
        for obs in recent_obs:
            ts = (obs.get("created_at") or "")[:10]
            parts.append(f"  [{obs['source']}] {ts}: {obs['text'][:200]}")

    parts.append("\n" + _CURRENT_PRACTICES_RULE)
    parts.append(
        "\nThe context above is a snapshot (current inhabitants, latest test, recent items only). "
        "Use the query_db tool whenever a question needs history or data beyond this snapshot — "
        "e.g. 'when was X added', 'GH trend/history', 'how much have I spent on Y' — rather than "
        "saying the data isn't available."
    )
    parts.append(
        "\nConversation style:\n"
        "- Answer helpfully and concisely in plain text only — no markdown (no **bold**, no *italic*, "
        "no headers, no bullet dashes).\n"
        "- This is a multi-turn conversation. Prior user and assistant messages are already in the "
        "thread; the user has just read them.\n"
        "- On follow-ups, answer the new question directly. Do not restate conclusions, timelines, "
        "or tank facts you already covered unless the user asks for a recap or you need one short "
        "reference to support a new point.\n"
        "- Never open with meta filler (e.g. \"Good question\", \"That gives me a full picture\", "
        "\"Based on everything above\", \"Here is a comprehensive answer\"). Jump straight into the "
        "answer.\n"
        "- Prefer a natural continuation over a standalone re-briefing. Use snapshot data when it "
        "changes the answer; do not re-list inventory, re-summarize water params, or re-walk prior "
        "reasoning by default."
    )
    return "\n".join(parts)


def _require_tank(conn, tank_id: int) -> dict:
    tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
    if not tank:
        raise HTTPException(status_code=404, detail="Tank not found")
    return tank


def _get_conversation(conn, tank_id: int, conversation_id: int) -> dict:
    conv = row_to_dict(conn.execute(
        "SELECT * FROM chat_conversations WHERE id = ? AND tank_id = ?",
        (conversation_id, tank_id),
    ).fetchone())
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _load_messages(conn, conversation_id: int) -> list[dict]:
    return rows_to_list(conn.execute(
        "SELECT id, role, content, created_at FROM chat_messages "
        "WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    ).fetchall())


def _gather_tank_context(conn, tank_id: int) -> dict:
    return {
        "summary": row_to_dict(conn.execute(
            "SELECT summary_text FROM tank_state_summary WHERE tank_id = ?", (tank_id,),
        ).fetchone()),
        "latest_test": row_to_dict(conn.execute(
            "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 1",
            (tank_id,),
        ).fetchone()),
        "inhabitants": rows_to_list(conn.execute(
            "SELECT common_name, species, count, added_date FROM inhabitants WHERE tank_id = ? ORDER BY common_name, species",
            (tank_id,),
        ).fetchall()),
        "plants": rows_to_list(conn.execute(
            "SELECT common_name, species FROM plants WHERE tank_id = ? AND status = 'active'",
            (tank_id,),
        ).fetchall()),
        "hardscape": rows_to_list(conn.execute(
            "SELECT item, quantity FROM hardscape WHERE tank_id = ?",
            (tank_id,),
        ).fetchall()),
        "open_issues": rows_to_list(conn.execute(
            "SELECT title, description, status FROM issues WHERE tank_id = ? AND status != 'resolved'",
            (tank_id,),
        ).fetchall()),
        "recent_obs": rows_to_list(conn.execute(
            "SELECT text, source, created_at FROM observations WHERE tank_id = ? ORDER BY created_at DESC LIMIT 5",
            (tank_id,),
        ).fetchall()),
        "schedule_rows": rows_to_list(conn.execute(
            "SELECT * FROM recurring_schedule WHERE tank_id = ? AND is_active = 1",
            (tank_id,),
        ).fetchall()),
        "home_water_tests": load_home_water_tests(conn),
    }


# ── Full-page views (left-nav) ──────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_chat_page(request: Request, tank_id: int):
    with get_db() as conn:
        tank = _require_tank(conn, tank_id)
    return templates.TemplateResponse(request, "chat/page.html", {
        "tank": tank,
        "conversation": None,
        "messages": [],
        "active": "chat",
        "active_conversation_id": None,
    })


@router.get("/c/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(request: Request, tank_id: int, conversation_id: int):
    with get_db() as conn:
        tank = _require_tank(conn, tank_id)
        conv = _get_conversation(conn, tank_id, conversation_id)
        messages = _load_messages(conn, conversation_id)
    return templates.TemplateResponse(request, "chat/page.html", {
        "tank": tank,
        "conversation": conv,
        "messages": messages,
        "active": "chat",
        "active_conversation_id": conv["id"],
    })


# ── JSON API ────────────────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations(tank_id: int):
    with get_db() as conn:
        _require_tank(conn, tank_id)
        rows = rows_to_list(conn.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS message_count
               FROM chat_conversations c
               WHERE c.tank_id = ?
               ORDER BY c.updated_at DESC, c.id DESC""",
            (tank_id,),
        ).fetchall())
    return JSONResponse({"conversations": rows})


@router.post("/conversations")
async def create_conversation(tank_id: int):
    now = _utc_now()
    with get_db() as conn:
        _require_tank(conn, tank_id)
        cur = conn.execute(
            "INSERT INTO chat_conversations (tank_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (tank_id, "New conversation", now, now),
        )
        conv_id = cur.lastrowid
        conv = row_to_dict(conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_conversations WHERE id = ?",
            (conv_id,),
        ).fetchone())
    conv["message_count"] = 0
    conv["messages"] = []
    return JSONResponse(conv, status_code=201)


@router.get("/conversations/{conversation_id}")
async def get_conversation(tank_id: int, conversation_id: int):
    with get_db() as conn:
        _require_tank(conn, tank_id)
        conv = _get_conversation(conn, tank_id, conversation_id)
        messages = _load_messages(conn, conversation_id)
    return JSONResponse({
        "id": conv["id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "messages": messages,
    })


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(tank_id: int, conversation_id: int):
    with get_db() as conn:
        _require_tank(conn, tank_id)
        _get_conversation(conn, tank_id, conversation_id)
        conn.execute("DELETE FROM chat_conversations WHERE id = ?", (conversation_id,))
    return JSONResponse({"status": "deleted", "id": conversation_id})


@router.post("")
async def chat(tank_id: int, body: ChatMessage):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="AI features require ANTHROPIC_API_KEY")

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    now = _utc_now()
    with get_db() as conn:
        tank = _require_tank(conn, tank_id)
        ctx = _gather_tank_context(conn, tank_id)

        created_new = False
        if body.conversation_id is not None:
            conv = _get_conversation(conn, tank_id, body.conversation_id)
            conversation_id = conv["id"]
        else:
            cur = conn.execute(
                "INSERT INTO chat_conversations (tank_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (tank_id, _title_from_message(message), now, now),
            )
            conversation_id = cur.lastrowid
            created_new = True

        # Title empty/new conversations from the first user message
        if not created_new:
            msg_count = conn.execute(
                "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["n"]
            if msg_count == 0:
                conn.execute(
                    "UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (_title_from_message(message), now, conversation_id),
                )
            else:
                conn.execute(
                    "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )

        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (conversation_id, message, now),
        )

        history_rows = _load_messages(conn, conversation_id)
        # Keep full history for display/storage; only send the last N turns to Claude
        api_history = [
            {"role": r["role"], "content": r["content"]}
            for r in history_rows
        ]
        if len(api_history) > MAX_TURNS * 2:
            api_history = api_history[-(MAX_TURNS * 2):]

        title = conn.execute(
            "SELECT title FROM chat_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()["title"]

    system_prompt = _build_system_prompt(
        tank, ctx["latest_test"], ctx["inhabitants"], ctx["plants"], ctx["hardscape"],
        ctx["open_issues"], ctx["summary"], ctx["recent_obs"], ctx["schedule_rows"],
        home_water_tests=ctx.get("home_water_tests"),
    )
    logger.info("Chat system prompt for tank %d conv %d: %d chars", tank_id, conversation_id, len(system_prompt))

    tools = [_query_db_tool(tank_id)]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        logger.info("Claude call: chat | tank=%d conv=%d turn=%d", tank_id, conversation_id, len(api_history) // 2)

        working_messages = list(api_history)
        raw = None
        response = None
        t0 = time.monotonic()
        for round_num in range(MAX_TOOL_ROUNDS + 1):
            response = await asyncio.to_thread(
                client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=1024,
                system=system_prompt,
                tools=tools,
                messages=working_messages,
            )
            if response.stop_reason != "tool_use" or round_num == MAX_TOOL_ROUNDS:
                raw = "".join(b.text for b in response.content if b.type == "text")
                if not raw:
                    raw = "I wasn't able to find a complete answer within the allotted lookups — try rephrasing or asking a narrower question."
                break

            working_messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.info("Chat tool call: chat | tank=%d | query_db: %s", tank_id, block.input.get("sql", ""))
                result = _run_query_db(block.input.get("sql", ""))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })
            working_messages.append({"role": "user", "content": tool_results})

        logger.info("Claude done: chat | tank=%d conv=%d | in=%d out=%d elapsed=%.1fs",
                    tank_id, conversation_id, response.usage.input_tokens, response.usage.output_tokens,
                    time.monotonic() - t0)
        # Strip markdown that the chat panel renders as literal characters
        reply = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', raw)   # **bold**, *italic*, ***both***
        reply = re.sub(r'^#{1,6}\s+', '', reply, flags=re.MULTILINE)  # headings
    except Exception as e:
        # Roll back the user message if the AI call failed on a brand-new empty conv we just made,
        # otherwise leave the user message (they can retry). On failure after insert, still store nothing
        # for the assistant and keep the user message for continuity.
        logger.error("Chat error for tank %d conv %d: %s", tank_id, conversation_id, e)
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

    done_at = _utc_now()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (conversation_id, reply, done_at),
        )
        conn.execute(
            "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
            (done_at, conversation_id),
        )
        turn_count = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = ? AND role = 'user'",
            (conversation_id,),
        ).fetchone()["n"]
        title = conn.execute(
            "SELECT title FROM chat_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()["title"]

    return JSONResponse({
        "reply": reply,
        "turns": turn_count,
        "conversation_id": conversation_id,
        "title": title,
    })
