from datetime import date, timedelta
from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/schedule", tags=["schedule"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

DOW_LABELS = {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
               "fri": "Friday", "sat": "Saturday", "sun": "Sunday"}

def _next_weekday(d, weekday):
    """Return d advanced to the next occurrence of weekday (0=Mon … 6=Sun), or d if already that day."""
    days_ahead = weekday - d.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


@router.get("", response_class=HTMLResponse)
async def schedule_page(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id=?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        schedules = rows_to_list(conn.execute(
            """SELECT * FROM recurring_schedule WHERE tank_id=?
               ORDER BY category,
                 CASE day_of_week WHEN 'mon' THEN 0 WHEN 'tue' THEN 1 WHEN 'wed' THEN 2
                   WHEN 'thu' THEN 3 WHEN 'fri' THEN 4 WHEN 'sat' THEN 5 WHEN 'sun' THEN 6
                   ELSE 7 END,
                 description""",
            (tank_id,),
        ).fetchall())
    by_cat = {"feeding": [], "dosing": [], "maintenance": []}
    for s in schedules:
        cat = s.get("category", "feeding")
        if cat in by_cat:
            by_cat[cat].append(s)
    return templates.TemplateResponse("tanks/schedule.html", {
        "request": request, "tank": tank, "by_cat": by_cat,
        "dow_labels": DOW_LABELS, "today": date.today().isoformat(),
    })


@router.post("")
async def add_schedule(
    request: Request,
    tank_id: int,
    category: str = Form(...),
    day_of_week: Optional[str] = Form(None),
    description: str = Form(...),
    interval_days: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
):
    tracking_mode = "logged" if category == "maintenance" else "reference_only"
    interval_type = "interval_days" if (category == "maintenance" and interval_days) else None
    dow = day_of_week if day_of_week and day_of_week in ("mon","tue","wed","thu","fri","sat","sun") else None

    with get_db() as conn:
        conn.execute(
            """INSERT INTO recurring_schedule
               (tank_id, category, tracking_mode, day_of_week, description,
                interval_type, interval_days, is_active, notes)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            (tank_id, category, tracking_mode, dow, description, interval_type, interval_days, notes),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/schedule", status_code=303)


@router.post("/{sch_id}/update")
async def update_schedule(
    tank_id: int,
    sch_id: int,
    category: str = Form(...),
    day_of_week: Optional[str] = Form(None),
    description: str = Form(...),
    interval_days: Optional[int] = Form(None),
    is_active: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    tracking_mode = "logged" if category == "maintenance" else "reference_only"
    interval_type = "interval_days" if (category == "maintenance" and interval_days) else None
    dow = day_of_week if day_of_week and day_of_week in ("mon","tue","wed","thu","fri","sat","sun") else None
    active = 1 if is_active else 0

    with get_db() as conn:
        existing = conn.execute("SELECT id FROM recurring_schedule WHERE id=? AND tank_id=?", (sch_id, tank_id)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Schedule entry not found")
        conn.execute(
            """UPDATE recurring_schedule SET
               category=?, tracking_mode=?, day_of_week=?, description=?,
               interval_type=?, interval_days=?, is_active=?, notes=?, updated_at=datetime('now')
               WHERE id=?""",
            (category, tracking_mode, dow, description, interval_type, interval_days, active, notes, sch_id),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/schedule", status_code=303)


@router.post("/{sch_id}/delete")
async def delete_schedule(tank_id: int, sch_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM recurring_schedule WHERE id=? AND tank_id=?", (sch_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/schedule", status_code=303)


@router.post("/{sch_id}/mark-done")
async def mark_done(tank_id: int, sch_id: int):
    with get_db() as conn:
        sched = row_to_dict(conn.execute(
            "SELECT * FROM recurring_schedule WHERE id=? AND tank_id=?", (sch_id, tank_id)
        ).fetchone())
        if not sched:
            raise HTTPException(status_code=404, detail="Schedule entry not found")

        today = date.today().isoformat()
        next_due = None
        if sched.get("interval_days"):
            next_due = (date.today() + timedelta(days=sched["interval_days"])).isoformat()

        conn.execute(
            "INSERT INTO events (tank_id, event_type, notes, schedule_id, timestamp) VALUES (?,?,?,?,?)",
            (tank_id, "maintenance", sched["description"], sch_id, today + " 00:00:00"),
        )
        conn.execute(
            "UPDATE recurring_schedule SET last_done=?, next_due=?, updated_at=datetime('now') WHERE id=?",
            (today, next_due, sch_id),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)
