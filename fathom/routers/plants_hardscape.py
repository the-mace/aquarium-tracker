from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}", tags=["plants"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/plants", response_class=HTMLResponse)
async def list_plants(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        plants = rows_to_list(conn.execute(
            "SELECT * FROM plants WHERE tank_id = ? AND status = 'active' ORDER BY common_name, species",
            (tank_id,),
        ).fetchall())
        removed_plants = rows_to_list(conn.execute(
            "SELECT * FROM plants WHERE tank_id = ? AND status = 'removed' ORDER BY common_name, species",
            (tank_id,),
        ).fetchall())
        hardscape = rows_to_list(conn.execute(
            "SELECT * FROM hardscape WHERE tank_id = ? ORDER BY item",
            (tank_id,),
        ).fetchall())
    return templates.TemplateResponse("plants/list.html", {
        "request": request, "tank": tank,
        "plants": plants, "removed_plants": removed_plants, "hardscape": hardscape,
    })


@router.post("/plants")
async def add_plant(
    tank_id: int,
    common_name: Optional[str] = Form(None),
    species: Optional[str] = Form(None),
    added_date: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO plants (tank_id, common_name, species, added_date, source, notes, status) VALUES (?,?,?,?,?,?,'active')",
            (tank_id, common_name or None, species or None, added_date or None, source or None, notes or None),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/plants/{plant_id}/remove")
async def remove_plant(tank_id: int, plant_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE plants SET status='removed', updated_at=datetime('now') WHERE id=? AND tank_id=?",
            (plant_id, tank_id),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/plants/{plant_id}/delete")
async def delete_plant(tank_id: int, plant_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM plants WHERE id=? AND tank_id=?", (plant_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/hardscape")
async def add_hardscape(
    tank_id: int,
    item: str = Form(...),
    quantity: int = Form(1),
    source: Optional[str] = Form(None),
    cost: Optional[float] = Form(None),
    added_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO hardscape (tank_id, item, quantity, source, cost, added_date, notes) VALUES (?,?,?,?,?,?,?)",
            (tank_id, item, quantity, source or None, cost, added_date or None, notes or None),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/hardscape/{hs_id}/delete")
async def delete_hardscape(tank_id: int, hs_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM hardscape WHERE id=? AND tank_id=?", (hs_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)
