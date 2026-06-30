import json
from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/equipment", tags=["equipment"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def list_equipment(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        equipment = rows_to_list(conn.execute(
            "SELECT * FROM tank_equipment WHERE tank_id = ? ORDER BY is_active DESC, category",
            (tank_id,),
        ).fetchall())
    return templates.TemplateResponse("equipment/list.html", {
        "request": request, "tank": tank, "equipment": equipment,
    })


@router.post("")
async def add_equipment(
    request: Request,
    tank_id: int,
    category: str = Form(...),
    brand: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    specs: Optional[str] = Form(None),
    installed_date: Optional[str] = Form(None),
    is_active: int = Form(1),
    notes: Optional[str] = Form(None),
):
    specs_json = None
    if specs:
        try:
            json.loads(specs)
            specs_json = specs
        except json.JSONDecodeError:
            specs_json = json.dumps({"description": specs})

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO tank_equipment (tank_id, category, brand, model, specs, installed_date, is_active, notes)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (tank_id, category, brand, model, specs_json, installed_date, is_active, notes),
        )
        eq_id = cur.lastrowid

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": eq_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}/equipment", status_code=303)


@router.post("/{eq_id}/update")
async def update_equipment(
    request: Request,
    tank_id: int,
    eq_id: int,
    category: str = Form(...),
    brand: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    specs: Optional[str] = Form(None),
    installed_date: Optional[str] = Form(None),
    removed_date: Optional[str] = Form(None),
    is_active: int = Form(1),
    notes: Optional[str] = Form(None),
):
    specs_json = None
    if specs:
        try:
            json.loads(specs)
            specs_json = specs
        except json.JSONDecodeError:
            specs_json = json.dumps({"description": specs})

    with get_db() as conn:
        conn.execute(
            """UPDATE tank_equipment SET category=?, brand=?, model=?, specs=?,
               installed_date=?, removed_date=?, is_active=?, notes=?, updated_at=datetime('now')
               WHERE id=? AND tank_id=?""",
            (category, brand, model, specs_json, installed_date, removed_date, is_active, notes, eq_id, tank_id),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/equipment", status_code=303)


@router.post("/{eq_id}/delete")
async def delete_equipment(tank_id: int, eq_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM tank_equipment WHERE id = ? AND tank_id = ?", (eq_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/equipment", status_code=303)
