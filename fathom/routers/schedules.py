from datetime import date, datetime, timedelta, timezone
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


_DOW_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# Fields whose edits are worth a timeline entry. last_done/next_due are excluded —
# those are corrections to history (already covered by mark-done's own event) rather
# than changes to the schedule's configuration.
_DIFF_FIELDS = [
    ("category", "category"),
    ("day_of_week", "day"),
    ("description", "description"),
    ("interval_days", "interval (days)"),
    ("is_active", "active"),
    ("notes", "notes"),
]


def _fmt_diff_val(field, value):
    if field == "day_of_week":
        return DOW_LABELS.get(value, "none") if value else "none"
    if field == "is_active":
        return "yes" if value else "no"
    if value is None or value == "":
        return "none"
    return str(value)


def _describe_schedule_changes(old, new):
    """Return a human-readable summary of changed fields, or None if nothing changed."""
    changes = []
    for field, label in _DIFF_FIELDS:
        old_val, new_val = old.get(field), new.get(field)
        if field == "is_active":
            old_val, new_val = bool(old_val), bool(new_val)
        if old_val != new_val:
            changes.append(f"{label}: {_fmt_diff_val(field, old_val)} → {_fmt_diff_val(field, new_val)}")
    if not changes:
        return None
    return f"Schedule updated ({new.get('description')}): " + "; ".join(changes)


def compute_next_due(day_of_week, interval_days, from_date):
    """Next due date after from_date (the day a task was just done).

    For weekly-cadence tasks (interval_days of 7 or less, or no interval at all)
    pinned to a day_of_week, due dates always land on that weekday, and marking
    done always completes the *current* cycle:

    - On the preferred day → same weekday next week (+7)
    - Late (e.g. Friday for a Thursday task) → the next preferred weekday
      (Friday→Thursday = +6)
    - Early (e.g. Wednesday for a Thursday task) → skip this week's preferred
      day and land on the following one (Wednesday→Thursday = +8), not tomorrow.
      Without this, an early water change would leave next_due as "tomorrow"
      and look due again almost immediately.

    Early vs late is decided by which preferred weekday is closer: if the
    upcoming preferred day is closer than the previous one, treat as early and
    add another week (handles Sunday-before-Monday as well as Wed-before-Thu).

    For longer intervals (e.g. a 30-day task that's also pinned to a weekday),
    the day_of_week is a landing-day preference rather than the cadence itself:
    the due date is computed as from_date + interval_days, then advanced to the
    next occurrence of that weekday on/after that mark — otherwise a 30-day
    interval task would incorrectly come due only 7 days later.
    """
    if day_of_week and day_of_week in _DOW_INDEX:
        preferred = _DOW_INDEX[day_of_week]
        if interval_days and interval_days > 7:
            target = from_date + timedelta(days=interval_days)
            return _next_weekday(target, preferred).isoformat()
        # Weekly cadence pinned to a weekday.
        days_since = (from_date.weekday() - preferred) % 7  # 0 = done on preferred day
        if days_since == 0:
            return (from_date + timedelta(days=7)).isoformat()
        days_until = (preferred - from_date.weekday()) % 7  # 1..6 → next preferred
        # Closer to the upcoming preferred day than the last one → early; skip it.
        if days_until < days_since:
            days_until += 7
        return (from_date + timedelta(days=days_until)).isoformat()
    if interval_days:
        return (from_date + timedelta(days=interval_days)).isoformat()
    return None


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
    return templates.TemplateResponse(request, "tanks/schedule.html", {
        "tank": tank, "by_cat": by_cat,
        "dow_labels": DOW_LABELS, "today": date.today().isoformat(),
    })


@router.post("")
async def add_schedule(
    request: Request,
    tank_id: int,
    category: str = Form(...),
    day_of_week: Optional[str] = Form(None),
    description: str = Form(...),
    interval_days: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    interval_days = int(interval_days) if interval_days and interval_days.strip() else None
    tracking_mode = "logged" if category == "maintenance" else "reference_only"
    interval_type = "interval_days" if (category == "maintenance" and interval_days) else None
    dow = day_of_week if day_of_week and day_of_week in ("mon","tue","wed","thu","fri","sat","sun") else None

    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO recurring_schedule
               (tank_id, category, tracking_mode, day_of_week, description,
                interval_type, interval_days, is_active, notes)
               VALUES (?,?,?,?,?,?,?,1,?)""",
            (tank_id, category, tracking_mode, dow, description, interval_type, interval_days, notes),
        )
        dow_label = DOW_LABELS.get(dow, "")
        detail = f" ({dow_label})" if dow_label else ""
        utc_now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO events (tank_id, event_type, notes, schedule_id, timestamp) VALUES (?,?,?,?,?)",
            (tank_id, "other", f"Schedule added: {description}{detail}", cur.lastrowid,
             utc_now.strftime("%Y-%m-%d %H:%M:%S")),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/schedule", status_code=303)


@router.post("/{sch_id}/update")
async def update_schedule(
    tank_id: int,
    sch_id: int,
    category: str = Form(...),
    day_of_week: Optional[str] = Form(None),
    description: str = Form(...),
    interval_days: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    last_done: Optional[str] = Form(None),
    next_due: Optional[str] = Form(None),
):
    interval_days = int(interval_days) if interval_days and interval_days.strip() else None
    tracking_mode = "logged" if category == "maintenance" else "reference_only"
    interval_type = "interval_days" if (category == "maintenance" and interval_days) else None
    dow = day_of_week if day_of_week and day_of_week in ("mon","tue","wed","thu","fri","sat","sun") else None
    active = 1 if is_active else 0

    with get_db() as conn:
        existing = row_to_dict(conn.execute("SELECT * FROM recurring_schedule WHERE id=? AND tank_id=?", (sch_id, tank_id)).fetchone())
        if not existing:
            raise HTTPException(status_code=404, detail="Schedule entry not found")

        # last_done / next_due are calendar-day fields. Blank form values preserve
        # whatever was already stored. An explicit next_due wins when provided
        # (e.g. catch-up water change Monday → push next due past this week's
        # normal Thursday). When last_done is corrected without an explicit
        # next_due, recompute next_due the same way mark-done does.
        last_done_val = existing["last_done"]
        next_due_val = existing["next_due"]
        last_done_set = last_done is not None and last_done.strip() != ""
        next_due_set = next_due is not None and next_due.strip() != ""
        if last_done_set:
            last_done_val = last_done.strip()
        if next_due_set:
            next_due_val = next_due.strip()
        elif last_done_set and next_due is None:
            # Field not in the form at all (API / older clients) → recompute.
            next_due_val = compute_next_due(dow, interval_days, date.fromisoformat(last_done_val))
        elif last_done_set and next_due is not None and not next_due.strip():
            # Field present but cleared while last_done was set → recompute.
            next_due_val = compute_next_due(dow, interval_days, date.fromisoformat(last_done_val))

        summary = _describe_schedule_changes(existing, {
            "category": category, "day_of_week": dow, "description": description,
            "interval_days": interval_days, "is_active": active, "notes": notes,
        })

        conn.execute(
            """UPDATE recurring_schedule SET
               category=?, tracking_mode=?, day_of_week=?, description=?,
               interval_type=?, interval_days=?, is_active=?, notes=?,
               last_done=?, next_due=?, updated_at=datetime('now')
               WHERE id=?""",
            (category, tracking_mode, dow, description, interval_type, interval_days, active, notes,
             last_done_val, next_due_val, sch_id),
        )

        if summary:
            utc_now = datetime.now(timezone.utc)
            conn.execute(
                "INSERT INTO events (tank_id, event_type, notes, schedule_id, timestamp) VALUES (?,?,?,?,?)",
                (tank_id, "other", summary, sch_id, utc_now.strftime("%Y-%m-%d %H:%M:%S")),
            )
    return RedirectResponse(url=f"/tanks/{tank_id}/schedule", status_code=303)


@router.post("/{sch_id}/delete")
async def delete_schedule(tank_id: int, sch_id: int):
    with get_db() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT * FROM recurring_schedule WHERE id=? AND tank_id=?", (sch_id, tank_id)
        ).fetchone())
        if existing:
            utc_now = datetime.now(timezone.utc)
            dow_label = DOW_LABELS.get(existing.get("day_of_week"), "")
            detail = f" ({dow_label})" if dow_label else ""
            conn.execute(
                "INSERT INTO events (tank_id, event_type, notes, schedule_id, timestamp) VALUES (?,?,?,?,?)",
                (tank_id, "other", f"Schedule removed: {existing['description']}{detail}", sch_id,
                 utc_now.strftime("%Y-%m-%d %H:%M:%S")),
            )
        conn.execute("DELETE FROM recurring_schedule WHERE id=? AND tank_id=?", (sch_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/schedule", status_code=303)


@router.post("/{sch_id}/mark-done")
async def mark_done(tank_id: int, sch_id: int, return_to: Optional[str] = Form(None)):
    with get_db() as conn:
        sched = row_to_dict(conn.execute(
            "SELECT * FROM recurring_schedule WHERE id=? AND tank_id=?", (sch_id, tank_id)
        ).fetchone())
        if not sched:
            raise HTTPException(status_code=404, detail="Schedule entry not found")

        # last_done/next_due are calendar-day fields (day_of_week matching, due-date
        # coloring) and stay on the server's local day. The event's own timestamp is
        # a moment in time and is stored as real UTC so it sorts correctly among
        # other UTC-stamped Timeline entries (e.g. AI Analysis observations).
        today = date.today().isoformat()
        next_due = compute_next_due(sched.get("day_of_week"), sched.get("interval_days"), date.today())

        utc_now = datetime.now(timezone.utc)
        conn.execute(
            "INSERT INTO events (tank_id, event_type, notes, schedule_id, timestamp) VALUES (?,?,?,?,?)",
            (tank_id, "maintenance", sched["description"], sch_id, utc_now.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.execute(
            "UPDATE recurring_schedule SET last_done=?, next_due=?, updated_at=datetime('now') WHERE id=?",
            (today, next_due, sch_id),
        )
    if return_to == "schedule":
        dest = f"/tanks/{tank_id}/schedule"
    elif return_to == "today":
        dest = "/today"
    else:
        dest = f"/tanks/{tank_id}"
    return RedirectResponse(url=dest, status_code=303)
