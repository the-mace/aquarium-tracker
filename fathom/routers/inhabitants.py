from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, get_ref_db, rows_to_list, row_to_dict
from routers.reference_info import maybe_fetch_reference_info, _canonical

router = APIRouter(prefix="/tanks/{tank_id}/inhabitants", tags=["inhabitants"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def list_inhabitants(request: Request, background_tasks: BackgroundTasks, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        inhabitants = rows_to_list(conn.execute(
            "SELECT * FROM inhabitants WHERE tank_id = ? ORDER BY count DESC NULLS LAST, common_name, species",
            (tank_id,),
        ).fetchall())
        pop_events = rows_to_list(conn.execute(
            "SELECT pe.*, i.common_name, i.species FROM population_events pe"
            " INNER JOIN inhabitants i ON i.id = pe.inhabitant_id"
            " WHERE pe.tank_id = ? ORDER BY pe.timestamp DESC LIMIT 20",
            (tank_id,),
        ).fetchall())

    # Merge reference info from persistent cache DB
    if inhabitants:
        entity_names = list({
            _canonical(i.get("species") or i.get("common_name") or "")
            for i in inhabitants
            if _canonical(i.get("species") or i.get("common_name") or "")
        })
        if entity_names:
            placeholders = ",".join("?" for _ in entity_names)
            with get_ref_db() as rconn:
                ref_rows = rows_to_list(rconn.execute(
                    f"SELECT * FROM reference_info WHERE entity_type='species' AND entity_name IN ({placeholders})",
                    entity_names,
                ).fetchall())
            ref_map = {r["entity_name"]: r for r in ref_rows}
        else:
            ref_map = {}

        for inh in inhabitants:
            entity_name = _canonical(inh.get("species") or inh.get("common_name") or "")
            ref = ref_map.get(entity_name, {})
            inh["ref_entity_name"] = ref.get("entity_name")
            inh["ref_scientific_name"] = ref.get("scientific_name")
            inh["ref_description"] = ref.get("description")
            inh["ref_care_notes"] = ref.get("care_notes")
            inh["ref_image_url"] = ref.get("image_url")
            inh["ref_image_source"] = ref.get("image_source")
            inh["ref_image_attribution"] = ref.get("image_attribution")
            inh["ref_fetched_at"] = ref.get("fetched_at")

    # Queue reference info fetch for any inhabitant not yet fetched (no row, or stuck placeholder)
    wt = tank.get("water_type", "freshwater") or "freshwater"
    for inh in inhabitants:
        if inh.get("ref_fetched_at") is None:
            entity_name = _canonical(inh.get("species") or inh.get("common_name") or "")
            if entity_name:
                display = inh.get("common_name") or inh.get("species") or ""
                maybe_fetch_reference_info(background_tasks, "species", entity_name, display, wt)

    return templates.TemplateResponse("inhabitants/list.html", {
        "request": request, "tank": tank,
        "inhabitants": inhabitants, "pop_events": pop_events,
    })


@router.post("")
async def add_inhabitant(
    request: Request,
    background_tasks: BackgroundTasks,
    tank_id: int,
    species: Optional[str] = Form(None),
    common_name: Optional[str] = Form(None),
    count: Optional[int] = Form(None),
    count_unknown: Optional[str] = Form(None),
    added_date: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    actual_count = None if count_unknown else (count if count is not None else 1)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO inhabitants (tank_id, species, common_name, count, added_date, source, notes)"
            " VALUES (?,?,?,?,?,?,?)",
            (tank_id, species, common_name, actual_count, added_date, source, notes),
        )
        inh_id = cur.lastrowid
        conn.execute(
            "INSERT INTO population_events (tank_id, inhabitant_id, event_type, count) VALUES (?,?,?,?)",
            (tank_id, inh_id, "added", actual_count or 0),
        )

    entity_name = _canonical(species or common_name or "")
    if entity_name:
        display = common_name or species or ""
        with get_db() as conn:
            t = row_to_dict(conn.execute("SELECT water_type FROM tanks WHERE id=?", (tank_id,)).fetchone())
        wt = (t or {}).get("water_type", "freshwater") or "freshwater"
        maybe_fetch_reference_info(background_tasks, "species", entity_name, display, wt)

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": inh_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


@router.post("/{inh_id}/update")
async def update_inhabitant(
    request: Request,
    tank_id: int,
    inh_id: int,
    species: Optional[str] = Form(None),
    common_name: Optional[str] = Form(None),
    count: Optional[int] = Form(None),
    count_unknown: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    actual_count = None if count_unknown else (count if count is not None else 0)
    with get_db() as conn:
        old = row_to_dict(conn.execute(
            "SELECT count FROM inhabitants WHERE id = ? AND tank_id = ?", (inh_id, tank_id),
        ).fetchone())
        if not old:
            raise HTTPException(status_code=404, detail="Inhabitant not found")
        conn.execute(
            "UPDATE inhabitants SET species=?, common_name=?, count=?, notes=?, updated_at=datetime('now')"
            " WHERE id=? AND tank_id=?",
            (species, common_name, actual_count, notes, inh_id, tank_id),
        )
        if actual_count is not None and old["count"] is not None:
            diff = actual_count - (old["count"] or 0)
            if diff != 0:
                etype = "added" if diff > 0 else "died"
                conn.execute(
                    "INSERT INTO population_events (tank_id, inhabitant_id, event_type, count) VALUES (?,?,?,?)",
                    (tank_id, inh_id, etype, abs(diff)),
                )
    return RedirectResponse(url=f"/tanks/{tank_id}/inhabitants", status_code=303)


@router.post("/{inh_id}/event")
async def record_population_event(
    request: Request,
    tank_id: int,
    inh_id: int,
    event_type: str = Form(...),
    count: int = Form(1),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        inh = row_to_dict(conn.execute(
            "SELECT * FROM inhabitants WHERE id = ? AND tank_id = ?", (inh_id, tank_id),
        ).fetchone())
        if not inh:
            raise HTTPException(status_code=404, detail="Inhabitant not found")

        conn.execute(
            "INSERT INTO population_events (tank_id, inhabitant_id, event_type, count, notes) VALUES (?,?,?,?,?)",
            (tank_id, inh_id, event_type, count, notes),
        )

        if event_type in ("died", "removed"):
            new_count = max(0, (inh["count"] or 0) - count)
        elif event_type in ("added", "born"):
            new_count = (inh["count"] or 0) + count
        else:
            new_count = inh["count"]

        conn.execute(
            "UPDATE inhabitants SET count=?, updated_at=datetime('now') WHERE id=?",
            (new_count, inh_id),
        )

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"status": "ok", "new_count": new_count})
    return RedirectResponse(url=f"/tanks/{tank_id}/inhabitants", status_code=303)


@router.post("/{inh_id}/delete")
async def delete_inhabitant(tank_id: int, inh_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM inhabitants WHERE id = ? AND tank_id = ?", (inh_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/inhabitants", status_code=303)
