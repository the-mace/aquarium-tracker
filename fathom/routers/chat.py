import os
import logging
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/chat", tags=["chat"])
logger = logging.getLogger(__name__)

_conversations: dict[int, list[dict]] = {}
MAX_TURNS = 10


class ChatMessage(BaseModel):
    message: str


def _build_system_prompt(tank, latest_test, inhabitants, plants, hardscape, open_issues, summary, recent_obs):
    parts = [
        "You are an expert aquarium keeper assistant with detailed knowledge of the following tank.",
        f"\nTank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons)",
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

    if inhabitants:
        lines = []
        for i in inhabitants:
            name = i.get("common_name") or i.get("species") or "Unknown"
            count = i.get("count")
            count_str = "many" if count is None else str(count)
            lines.append(f"  {count_str}x {name}")
        parts.append("\nInhabitants:\n" + "\n".join(lines))
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

    if summary and summary.get("summary_text"):
        parts.append(f"\nRecent AI Summary:\n{summary['summary_text']}")

    if recent_obs:
        parts.append("\nRecent Observations:")
        for obs in recent_obs:
            ts = (obs.get("created_at") or "")[:10]
            parts.append(f"  [{obs['source']}] {ts}: {obs['text'][:200]}")

    parts.append("\nAnswer questions helpfully and concisely. Reference specific data from above when relevant. Do not use markdown formatting (no **bold**, no *italic*, no headers, no bullet dashes) — plain text only.")
    return "\n".join(parts)


@router.post("")
async def chat(tank_id: int, body: ChatMessage):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="AI features require ANTHROPIC_API_KEY")

    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")

        summary = row_to_dict(conn.execute(
            "SELECT summary_text FROM tank_state_summary WHERE tank_id = ?", (tank_id,),
        ).fetchone())

        latest_test = row_to_dict(conn.execute(
            "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 1",
            (tank_id,),
        ).fetchone())

        inhabitants = rows_to_list(conn.execute(
            "SELECT common_name, species, count FROM inhabitants WHERE tank_id = ? ORDER BY common_name, species",
            (tank_id,),
        ).fetchall())

        plants = rows_to_list(conn.execute(
            "SELECT common_name, species FROM plants WHERE tank_id = ? AND status = 'active'",
            (tank_id,),
        ).fetchall())

        hardscape = rows_to_list(conn.execute(
            "SELECT item, quantity FROM hardscape WHERE tank_id = ?",
            (tank_id,),
        ).fetchall())

        open_issues = rows_to_list(conn.execute(
            "SELECT title, description, status FROM issues WHERE tank_id = ? AND status != 'resolved'",
            (tank_id,),
        ).fetchall())

        recent_obs = rows_to_list(conn.execute(
            "SELECT text, source, created_at FROM observations WHERE tank_id = ? ORDER BY created_at DESC LIMIT 5",
            (tank_id,),
        ).fetchall())

    system_prompt = _build_system_prompt(
        tank, latest_test, inhabitants, plants, hardscape, open_issues, summary, recent_obs
    )
    logger.info("Chat system prompt for tank %d: %d chars", tank_id, len(system_prompt))

    history = _conversations.get(tank_id, [])
    history.append({"role": "user", "content": body.message})

    if len(history) > MAX_TURNS * 2:
        history = history[-(MAX_TURNS * 2):]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=history,
        )
        import re
        raw = response.content[0].text
        # Strip markdown that the chat panel renders as literal characters
        reply = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', raw)   # **bold**, *italic*, ***both***
        reply = re.sub(r'^#{1,6}\s+', '', reply, flags=re.MULTILINE)  # headings
    except Exception as e:
        logger.error("Chat error for tank %d: %s", tank_id, e)
        raise HTTPException(status_code=500, detail=f"AI error: {str(e)}")

    history.append({"role": "assistant", "content": reply})
    _conversations[tank_id] = history

    return JSONResponse({"reply": reply, "turns": len(history) // 2})


@router.delete("")
async def clear_chat(tank_id: int):
    _conversations.pop(tank_id, None)
    return JSONResponse({"status": "cleared"})
