from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, get_ref_db, rows_to_list, row_to_dict
from routers.reference_info import maybe_fetch_reference_info, _canonical

router = APIRouter(prefix="/tanks/{tank_id}", tags=["plants"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/plants", response_class=HTMLResponse)
async def list_plants(request: Request, background_tasks: BackgroundTasks, tank_id: int):
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

    # Merge reference info from persistent cache DB for plants
    if plants:
        plant_names = list({
            _canonical(p.get("species") or p.get("common_name") or "")
            for p in plants
            if _canonical(p.get("species") or p.get("common_name") or "")
        })
        if plant_names:
            placeholders = ",".join("?" for _ in plant_names)
            with get_ref_db() as rconn:
                ref_rows = rows_to_list(rconn.execute(
                    f"SELECT * FROM reference_info WHERE entity_type='plant' AND entity_name IN ({placeholders})",
                    plant_names,
                ).fetchall())
            ref_map = {r["entity_name"]: r for r in ref_rows}
        else:
            ref_map = {}
        for pl in plants:
            entity_name = _canonical(pl.get("species") or pl.get("common_name") or "")
            ref = ref_map.get(entity_name, {})
            pl["ref_entity_name"] = ref.get("entity_name")
            pl["ref_scientific_name"] = ref.get("scientific_name")
            pl["ref_description"] = ref.get("description")
            pl["ref_care_notes"] = ref.get("care_notes")
            pl["ref_image_url"] = ref.get("image_url")
            pl["ref_image_attribution"] = ref.get("image_attribution")
            pl["ref_fetched_at"] = ref.get("fetched_at")

    # Merge reference info for hardscape
    if hardscape:
        hs_names = list({
            _canonical(h.get("item") or "")
            for h in hardscape
            if _canonical(h.get("item") or "")
        })
        if hs_names:
            placeholders = ",".join("?" for _ in hs_names)
            with get_ref_db() as rconn:
                ref_rows = rows_to_list(rconn.execute(
                    f"SELECT * FROM reference_info WHERE entity_type='hardscape' AND entity_name IN ({placeholders})",
                    hs_names,
                ).fetchall())
            hs_ref_map = {r["entity_name"]: r for r in ref_rows}
        else:
            hs_ref_map = {}
        for hs in hardscape:
            entity_name = _canonical(hs.get("item") or "")
            ref = hs_ref_map.get(entity_name, {})
            hs["ref_entity_name"] = ref.get("entity_name")
            hs["ref_description"] = ref.get("description")
            hs["ref_care_notes"] = ref.get("care_notes")
            hs["ref_image_url"] = ref.get("image_url")
            hs["ref_image_attribution"] = ref.get("image_attribution")
            hs["ref_fetched_at"] = ref.get("fetched_at")

    # Queue reference info fetch for any entity not yet fetched (no row, or stuck placeholder)
    wt = tank.get("water_type", "freshwater") or "freshwater"
    for pl in plants:
        if pl.get("ref_fetched_at") is None:
            entity_name = _canonical(pl.get("species") or pl.get("common_name") or "")
            if entity_name:
                display = pl.get("common_name") or pl.get("species") or ""
                maybe_fetch_reference_info(background_tasks, "plant", entity_name, display, wt)

    for hs in hardscape:
        if hs.get("ref_fetched_at") is None:
            entity_name = _canonical(hs.get("item") or "")
            if entity_name:
                maybe_fetch_reference_info(background_tasks, "hardscape", entity_name, hs.get("item", ""), wt)

    return templates.TemplateResponse("plants/list.html", {
        "request": request, "tank": tank,
        "plants": plants, "removed_plants": removed_plants, "hardscape": hardscape,
    })


@router.post("/plants")
async def add_plant(
    background_tasks: BackgroundTasks,
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

    entity_name = _canonical(species or common_name or "")
    if entity_name:
        display = common_name or species or ""
        with get_db() as conn:
            t = row_to_dict(conn.execute("SELECT water_type FROM tanks WHERE id=?", (tank_id,)).fetchone())
        wt = (t or {}).get("water_type", "freshwater") or "freshwater"
        maybe_fetch_reference_info(background_tasks, "plant", entity_name, display, wt)

    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/plants/{plant_id}/update")
async def update_plant(
    tank_id: int,
    plant_id: int,
    common_name: Optional[str] = Form(None),
    species: Optional[str] = Form(None),
    added_date: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
):
    with get_db() as conn:
        conn.execute(
            """UPDATE plants SET common_name=?, species=?, added_date=?, source=?, notes=?,
               status=COALESCE(?,status), updated_at=datetime('now')
               WHERE id=? AND tank_id=?""",
            (common_name or None, species or None, added_date or None,
             source or None, notes or None, status or None, plant_id, tank_id),
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
    background_tasks: BackgroundTasks,
    tank_id: int,
    item: str = Form(...),
    quantity: int = Form(1),
    source: Optional[str] = Form(None),
    cost: Optional[str] = Form(None),
    added_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    cost_val = float(cost) if cost and cost.strip() else None
    with get_db() as conn:
        conn.execute(
            "INSERT INTO hardscape (tank_id, item, quantity, source, cost, added_date, notes) VALUES (?,?,?,?,?,?,?)",
            (tank_id, item, quantity, source or None, cost_val, added_date or None, notes or None),
        )

    entity_name = _canonical(item)
    if entity_name:
        with get_db() as conn:
            t = row_to_dict(conn.execute("SELECT water_type FROM tanks WHERE id=?", (tank_id,)).fetchone())
        wt = (t or {}).get("water_type", "freshwater") or "freshwater"
        maybe_fetch_reference_info(background_tasks, "hardscape", entity_name, item, wt)

    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/hardscape/{hs_id}/update")
async def update_hardscape(
    tank_id: int,
    hs_id: int,
    item: str = Form(...),
    quantity: int = Form(1),
    source: Optional[str] = Form(None),
    cost: Optional[str] = Form(None),
    added_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    cost_val = float(cost) if cost and cost.strip() else None
    with get_db() as conn:
        conn.execute(
            """UPDATE hardscape SET item=?, quantity=?, source=?, cost=?, added_date=?, notes=?
               WHERE id=? AND tank_id=?""",
            (item, quantity, source or None, cost_val, added_date or None, notes or None, hs_id, tank_id),
        )
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/hardscape/{hs_id}/delete")
async def delete_hardscape(tank_id: int, hs_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM hardscape WHERE id=? AND tank_id=?", (hs_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)
