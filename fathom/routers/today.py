from datetime import date
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database import get_db, rows_to_list

router = APIRouter(tags=["today"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/today", response_class=HTMLResponse)
async def today_page(request: Request):
    today_dow = date.today().strftime("%a").lower()
    today_date = date.today().isoformat()

    with get_db() as conn:
        tanks = rows_to_list(conn.execute(
            "SELECT id, name FROM tanks WHERE status='active' ORDER BY name"
        ).fetchall())

        for tank in tanks:
            tank["today_schedule"] = rows_to_list(conn.execute(
                """SELECT * FROM recurring_schedule
                   WHERE tank_id=? AND is_active=1 AND tracking_mode='reference_only'
                     AND day_of_week=?
                   ORDER BY category, description""",
                (tank["id"], today_dow),
            ).fetchall())

            # "Due today or overdue" — a next_due in the past/today, or an interval-based
            # task that's never been done yet (next_due NULL but interval_days set). Plain
            # manual-reminder items (no next_due, no interval) have no due-date concept and
            # are excluded here on purpose, to keep this page scoped to what needs attention.
            # Also include anything already completed today (last_done=today) even though
            # its next_due has since rolled forward, so a finished item stays visible with
            # a checkmark instead of vanishing the moment it's marked done.
            tank["maintenance_items"] = rows_to_list(conn.execute(
                """SELECT * FROM recurring_schedule
                   WHERE tank_id=? AND is_active=1 AND tracking_mode='logged'
                     AND (
                       (next_due IS NOT NULL AND next_due <= ?)
                       OR (next_due IS NULL AND interval_days IS NOT NULL)
                       OR last_done=?
                     )
                   ORDER BY CASE WHEN last_done=? THEN 1 ELSE 0 END,
                     CASE WHEN next_due IS NULL THEN 1 ELSE 0 END, next_due""",
                (tank["id"], today_date, today_date, today_date),
            ).fetchall())

    return templates.TemplateResponse("today.html", {
        "request": request,
        "tanks": tanks,
        "today_date": today_date,
        "today_dow_label": date.today().strftime("%A"),
    })
