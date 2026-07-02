from datetime import date
from pathlib import Path
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks", tags=["tanks"])


def _float(value: Optional[str]) -> Optional[float]:
    if value is None or value.strip() == "":
        return None
    return float(value)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

TANK_TABLES = [
    "test_results", "events", "inhabitants", "population_events",
    "purchases", "observations", "issues", "tank_state_summary",
    "plants", "hardscape", "tank_equipment", "recurring_schedule",
]


@router.get("", response_class=HTMLResponse)
async def list_tanks(request: Request):
    with get_db() as conn:
        tanks = rows_to_list(conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM inhabitants i WHERE i.tank_id=t.id AND i.count>0) as inhabitant_types,"
            " (SELECT COUNT(*) FROM issues iss WHERE iss.tank_id=t.id AND iss.status!='resolved') as open_issues"
            " FROM tanks t ORDER BY t.status, t.name"
        ).fetchall())
    return templates.TemplateResponse("tanks/list.html", {"request": request, "tanks": tanks})


@router.get("/new", response_class=HTMLResponse)
async def new_tank_form(request: Request):
    return templates.TemplateResponse("tanks/form.html", {"request": request, "tank": None, "action": "add"})


@router.post("", response_class=HTMLResponse)
async def create_tank(
    request: Request,
    name: str = Form(...),
    water_type: str = Form("fresh"),
    volume_gallons: Optional[str] = Form(None),
    dimensions_l: Optional[str] = Form(None),
    dimensions_w: Optional[str] = Form(None),
    dimensions_h: Optional[str] = Form(None),
    shape: Optional[str] = Form(None),
    manufacturer: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    substrate_type: Optional[str] = Form(None),
    substrate_brand: Optional[str] = Form(None),
    substrate_depth_inches: Optional[str] = Form(None),
    setup_date: Optional[str] = Form(None),
    status: str = Form("active"),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO tanks (name, water_type, volume_gallons, dimensions_l, dimensions_w, dimensions_h,
               shape, manufacturer, model, substrate_type, substrate_brand, substrate_depth_inches,
               setup_date, status, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, water_type, _float(volume_gallons), _float(dimensions_l), _float(dimensions_w), _float(dimensions_h),
             shape, manufacturer, model, substrate_type, substrate_brand, _float(substrate_depth_inches),
             setup_date, status, notes),
        )
        tank_id = cur.lastrowid
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


@router.get("/{tank_id}", response_class=HTMLResponse)
async def tank_detail(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")

        latest_test = row_to_dict(conn.execute(
            "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 1",
            (tank_id,),
        ).fetchone())

        inhabitants = rows_to_list(conn.execute(
            "SELECT * FROM inhabitants WHERE tank_id = ? AND (count IS NULL OR count > 0)"
            " ORDER BY count DESC NULLS LAST, common_name, species",
            (tank_id,),
        ).fetchall())

        open_issues = rows_to_list(conn.execute(
            "SELECT * FROM issues WHERE tank_id = ? AND status != 'resolved' ORDER BY opened_at DESC",
            (tank_id,),
        ).fetchall())

        recent_observations = rows_to_list(conn.execute(
            "SELECT * FROM observations WHERE tank_id = ? ORDER BY created_at DESC LIMIT 3",
            (tank_id,),
        ).fetchall())

        recent_events = rows_to_list(conn.execute(
            "SELECT * FROM events WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 10",
            (tank_id,),
        ).fetchall())

        equipment = rows_to_list(conn.execute(
            "SELECT * FROM tank_equipment WHERE tank_id = ? AND is_active = 1 ORDER BY category",
            (tank_id,),
        ).fetchall())

        summary = row_to_dict(conn.execute(
            "SELECT * FROM tank_state_summary WHERE tank_id = ?",
            (tank_id,),
        ).fetchone())

        recent_purchases = rows_to_list(conn.execute(
            "SELECT * FROM purchases WHERE tank_id = ? ORDER BY purchase_date DESC LIMIT 5",
            (tank_id,),
        ).fetchall())

        plants = rows_to_list(conn.execute(
            "SELECT * FROM plants WHERE tank_id = ? AND status = 'active' ORDER BY common_name, species",
            (tank_id,),
        ).fetchall())

        hardscape = rows_to_list(conn.execute(
            "SELECT * FROM hardscape WHERE tank_id = ? ORDER BY item",
            (tank_id,),
        ).fetchall())

        today_dow = date.today().strftime('%a').lower()
        today_date = date.today().isoformat()

        today_schedule = rows_to_list(conn.execute(
            """SELECT * FROM recurring_schedule
               WHERE tank_id=? AND is_active=1 AND tracking_mode='reference_only'
                 AND (day_of_week=? OR day_of_week IS NULL)
               ORDER BY category,
                 CASE day_of_week WHEN 'mon' THEN 0 WHEN 'tue' THEN 1 WHEN 'wed' THEN 2
                   WHEN 'thu' THEN 3 WHEN 'fri' THEN 4 WHEN 'sat' THEN 5 WHEN 'sun' THEN 6
                   ELSE 7 END""",
            (tank_id, today_dow),
        ).fetchall())

        maintenance_items = rows_to_list(conn.execute(
            """SELECT * FROM recurring_schedule
               WHERE tank_id=? AND is_active=1 AND tracking_mode='logged'
               ORDER BY CASE WHEN next_due IS NULL THEN 1 ELSE 0 END, next_due""",
            (tank_id,),
        ).fetchall())

    today_dow_label = date.today().strftime('%A')

    return templates.TemplateResponse("tanks/detail.html", {
        "request": request,
        "tank": tank,
        "latest_test": latest_test,
        "inhabitants": inhabitants,
        "open_issues": open_issues,
        "recent_observations": recent_observations,
        "recent_events": recent_events,
        "equipment": equipment,
        "summary": summary,
        "recent_purchases": recent_purchases,
        "plants": plants,
        "hardscape": hardscape,
        "today_schedule": today_schedule,
        "maintenance_items": maintenance_items,
        "today_dow_label": today_dow_label,
        "today_date": today_date,
    })


@router.get("/{tank_id}/edit", response_class=HTMLResponse)
async def edit_tank_form(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
    if not tank:
        raise HTTPException(status_code=404, detail="Tank not found")
    return templates.TemplateResponse("tanks/form.html", {"request": request, "tank": tank, "action": "edit"})


@router.post("/{tank_id}/edit", response_class=HTMLResponse)
async def update_tank(
    request: Request,
    tank_id: int,
    name: str = Form(...),
    water_type: str = Form("fresh"),
    volume_gallons: Optional[str] = Form(None),
    dimensions_l: Optional[str] = Form(None),
    dimensions_w: Optional[str] = Form(None),
    dimensions_h: Optional[str] = Form(None),
    shape: Optional[str] = Form(None),
    manufacturer: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    substrate_type: Optional[str] = Form(None),
    substrate_brand: Optional[str] = Form(None),
    substrate_depth_inches: Optional[str] = Form(None),
    setup_date: Optional[str] = Form(None),
    status: str = Form("active"),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        conn.execute(
            """UPDATE tanks SET name=?, water_type=?, volume_gallons=?, dimensions_l=?, dimensions_w=?,
               dimensions_h=?, shape=?, manufacturer=?, model=?, substrate_type=?, substrate_brand=?,
               substrate_depth_inches=?, setup_date=?, status=?, notes=?, updated_at=datetime('now')
               WHERE id=?""",
            (name, water_type, _float(volume_gallons), _float(dimensions_l), _float(dimensions_w), _float(dimensions_h),
             shape, manufacturer, model, substrate_type, substrate_brand, _float(substrate_depth_inches),
             setup_date, status, notes, tank_id),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


@router.post("/{tank_id}/delete")
async def delete_tank(tank_id: int, confirmation: str = Form(...)):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT name FROM tanks WHERE id = ?", (tank_id,)).fetchone())
    if not tank:
        raise HTTPException(status_code=404, detail="Tank not found")
    if confirmation != tank["name"]:
        raise HTTPException(status_code=400, detail="Confirmation name does not match")
    with get_db() as conn:
        conn.execute("DELETE FROM tanks WHERE id = ?", (tank_id,))
    return RedirectResponse(url="/tanks", status_code=303)


@router.post("/{tank_id}/reset")
async def reset_tank_data(tank_id: int, confirmation: str = Form(...)):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT name FROM tanks WHERE id = ?", (tank_id,)).fetchone())
    if not tank:
        raise HTTPException(status_code=404, detail="Tank not found")
    if confirmation != tank["name"]:
        raise HTTPException(status_code=400, detail="Confirmation name does not match")
    with get_db() as conn:
        for table in TANK_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE tank_id = ?", (tank_id,))
        conn.execute(
            """UPDATE tanks SET
               volume_gallons=NULL, dimensions_l=NULL, dimensions_w=NULL, dimensions_h=NULL,
               shape=NULL, manufacturer=NULL, model=NULL, substrate_type=NULL,
               substrate_brand=NULL, substrate_depth_inches=NULL, setup_date=NULL, notes=NULL,
               updated_at=datetime('now')
               WHERE id=?""",
            (tank_id,),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


# ── Chart data endpoints ───────────────────────────────────────────────────────

@router.get("/{tank_id}/charts/water-params")
async def chart_water_params(tank_id: int, limit: int = 30):
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT timestamp, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp
               FROM test_results WHERE tank_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (tank_id, limit),
        ).fetchall())
    rows.reverse()
    return JSONResponse({"data": rows})


@router.get("/{tank_id}/charts/population")
async def chart_population(tank_id: int):
    with get_db() as conn:
        events = rows_to_list(conn.execute(
            """SELECT pe.timestamp, pe.event_type, pe.count,
                      i.common_name, i.species
               FROM population_events pe
               LEFT JOIN inhabitants i ON i.id = pe.inhabitant_id
               WHERE pe.tank_id = ?
               ORDER BY pe.timestamp""",
            (tank_id,),
        ).fetchall())
        current = rows_to_list(conn.execute(
            "SELECT common_name, species, count FROM inhabitants WHERE tank_id = ?",
            (tank_id,),
        ).fetchall())
    return JSONResponse({"events": events, "current": current})


@router.get("/{tank_id}/charts/costs")
async def chart_costs(tank_id: int):
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """SELECT category, SUM(cost) as total, COUNT(*) as count
               FROM purchases WHERE tank_id = ?
               GROUP BY category ORDER BY total DESC""",
            (tank_id,),
        ).fetchall())
        monthly = rows_to_list(conn.execute(
            """SELECT strftime('%Y-%m', purchase_date) as month, SUM(cost) as total
               FROM purchases WHERE tank_id = ?
               GROUP BY month ORDER BY month""",
            (tank_id,),
        ).fetchall())
    return JSONResponse({"by_category": rows, "by_month": monthly})
