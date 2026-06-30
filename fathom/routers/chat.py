import os
import logging
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/chat", tags=["chat"])
logger = logging.getLogger(__name__)

# In-memory conversation store keyed by tank_id
_conversations: dict[int, list[dict]] = {}
MAX_TURNS = 10


class ChatMessage(BaseModel):
    message: str


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

        recent_obs = rows_to_list(conn.execute(
            "SELECT text, source, created_at FROM observations WHERE tank_id = ? ORDER BY created_at DESC LIMIT 3",
            (tank_id,),
        ).fetchall())

    context_parts = [
        f"You are an expert aquarium keeper assistant. You have detailed knowledge of the following tank.",
        f"\nTank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons)",
    ]

    if summary and summary.get("summary_text"):
        context_parts.append(f"\nCurrent State Summary:\n{summary['summary_text']}")

    if recent_obs:
        context_parts.append("\nRecent Observations:")
        for obs in recent_obs:
            context_parts.append(f"  [{obs['source']}] {obs['created_at']}: {obs['text'][:300]}")

    context_parts.append("\nAnswer questions helpfully and concisely. If you're uncertain, say so.")
    system_prompt = "\n".join(context_parts)

    history = _conversations.get(tank_id, [])
    history.append({"role": "user", "content": body.message})

    # Keep last MAX_TURNS pairs (user+assistant = 2 messages per turn)
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
        reply = response.content[0].text
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
