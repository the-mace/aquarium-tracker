from datetime import date
from pathlib import Path
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict
from routers.schedules import compute_next_due

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
    schedule_id: Optional[int] = Form(None),
):
    with get_db() as conn:
        if timestamp:
            cur = conn.execute(
                "INSERT INTO events (tank_id, event_type, notes, amount, timestamp, schedule_id) VALUES (?,?,?,?,?,?)",
                (tank_id, event_type, notes, amount, timestamp, schedule_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO events (tank_id, event_type, notes, amount, schedule_id) VALUES (?,?,?,?,?)",
                (tank_id, event_type, notes, amount, schedule_id),
            )
        event_id = cur.lastrowid

        if event_type == "maintenance" and schedule_id:
            sched = row_to_dict(conn.execute(
                "SELECT * FROM recurring_schedule WHERE id=? AND tank_id=?", (schedule_id, tank_id)
            ).fetchone())
            if sched:
                event_date = timestamp[:10] if timestamp else date.today().isoformat()
                next_due = compute_next_due(sched.get("day_of_week"), sched.get("interval_days"), date.fromisoformat(event_date))
                conn.execute(
                    "UPDATE recurring_schedule SET last_done=?, next_due=?, updated_at=datetime('now') WHERE id=?",
                    (event_date, next_due, schedule_id),
                )

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
