from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict
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
            """SELECT p.*,
                      ri.description    AS ref_description,
                      ri.care_notes     AS ref_care_notes,
                      ri.image_url      AS ref_image_url,
                      ri.image_attribution AS ref_image_attribution,
                      ri.fetched_at     AS ref_fetched_at,
                      ri.entity_name    AS ref_entity_name
               FROM plants p
               LEFT JOIN reference_info ri
                 ON ri.entity_type = 'plant'
                AND ri.entity_name = CASE
                      WHEN p.species IS NOT NULL AND trim(p.species) != ''
                        THEN lower(trim(p.species))
                      ELSE lower(trim(p.common_name))
                    END
               WHERE p.tank_id = ? AND p.status = 'active'
               ORDER BY p.common_name, p.species""",
            (tank_id,),
        ).fetchall())
        removed_plants = rows_to_list(conn.execute(
            "SELECT * FROM plants WHERE tank_id = ? AND status = 'removed' ORDER BY common_name, species",
            (tank_id,),
        ).fetchall())
        hardscape = rows_to_list(conn.execute(
            """SELECT h.*,
                      ri.description    AS ref_description,
                      ri.care_notes     AS ref_care_notes,
                      ri.image_url      AS ref_image_url,
                      ri.image_attribution AS ref_image_attribution,
                      ri.fetched_at     AS ref_fetched_at,
                      ri.entity_name    AS ref_entity_name
               FROM hardscape h
               LEFT JOIN reference_info ri
                 ON ri.entity_type = 'hardscape'
                AND ri.entity_name = lower(trim(h.item))
               WHERE h.tank_id = ?
               ORDER BY h.item""",
            (tank_id,),
        ).fetchall())
    # Queue reference info fetch for any entity not yet in reference_info
    for pl in plants:
        if pl.get("ref_entity_name") is None:
            entity_name = _canonical(pl.get("species") or pl.get("common_name") or "")
            if entity_name:
                display = pl.get("common_name") or pl.get("species") or ""
                maybe_fetch_reference_info(background_tasks, "plant", entity_name, display)

    for hs in hardscape:
        if hs.get("ref_entity_name") is None:
            entity_name = _canonical(hs.get("item") or "")
            if entity_name:
                maybe_fetch_reference_info(background_tasks, "hardscape", entity_name, hs.get("item", ""))

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
        maybe_fetch_reference_info(background_tasks, "plant", entity_name, display)

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
    cost: Optional[float] = Form(None),
    added_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO hardscape (tank_id, item, quantity, source, cost, added_date, notes) VALUES (?,?,?,?,?,?,?)",
            (tank_id, item, quantity, source or None, cost, added_date or None, notes or None),
        )

    entity_name = _canonical(item)
    if entity_name:
        maybe_fetch_reference_info(background_tasks, "hardscape", entity_name, item)

    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)


@router.post("/hardscape/{hs_id}/delete")
async def delete_hardscape(tank_id: int, hs_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM hardscape WHERE id=? AND tank_id=?", (hs_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/plants", status_code=303)
