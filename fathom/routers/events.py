from pathlib import Path
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/events", tags=["events"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=JSONResponse)
async def list_events(tank_id: int, limit: int = 20):
    with get_db() as conn:
        events = rows_to_list(conn.execute(
            "SELECT * FROM events WHERE tank_id = ? ORDER BY timestamp DESC LIMIT ?",
            (tank_id, limit),
        ).fetchall())
    return JSONResponse({"events": events})


@router.post("")
async def add_event(
    request: Request,
    tank_id: int,
    background_tasks: BackgroundTasks,
    event_type: str = Form(...),
    notes: Optional[str] = Form(None),
    amount: Optional[float] = Form(None),
    timestamp: Optional[str] = Form(None),
):
    with get_db() as conn:
        if timestamp:
            cur = conn.execute(
                "INSERT INTO events (tank_id, event_type, notes, amount, timestamp) VALUES (?,?,?,?,?)",
                (tank_id, event_type, notes, amount, timestamp),
            )
        else:
            cur = conn.execute(
                "INSERT INTO events (tank_id, event_type, notes, amount) VALUES (?,?,?,?)",
                (tank_id, event_type, notes, amount),
            )
        event_id = cur.lastrowid

    from routers.ai_analysis import run_ai_analysis
    background_tasks.add_task(run_ai_analysis, tank_id, "event", event_id)

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": event_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


@router.delete("/{event_id}")
async def delete_event(tank_id: int, event_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM events WHERE id = ? AND tank_id = ?", (event_id, tank_id))
    return JSONResponse({"status": "deleted"})
