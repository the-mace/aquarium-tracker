from pathlib import Path
from urllib.parse import urlencode
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/observations", tags=["observations"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

COLUMN_BY_TYPE = {
    "inhabitant": "related_inhabitant_id",
    "plant": "related_plant_id",
    "hardscape": "related_hardscape_id",
    "equipment": "related_equipment_id",
}


def _entity_options(conn, tank_id: int) -> dict:
    inhabitants = [
        {"id": r[0], "label": r[1] or r[2] or f"Inhabitant #{r[0]}"}
        for r in conn.execute(
            "SELECT id, common_name, species FROM inhabitants WHERE tank_id=? ORDER BY common_name, species",
            (tank_id,),
        ).fetchall()
    ]
    plants = [
        {"id": r[0], "label": r[1] or r[2] or f"Plant #{r[0]}"}
        for r in conn.execute(
            "SELECT id, common_name, species FROM plants WHERE tank_id=? AND status='active' ORDER BY common_name, species",
            (tank_id,),
        ).fetchall()
    ]
    hardscape = [
        {"id": r[0], "label": r[1]}
        for r in conn.execute(
            "SELECT id, item FROM hardscape WHERE tank_id=? ORDER BY item", (tank_id,),
        ).fetchall()
    ]
    equipment = [
        {"id": r[0], "label": (f"{r[2] or ''} {r[3] or ''}".strip() or r[1])}
        for r in conn.execute(
            "SELECT id, category, brand, model FROM tank_equipment WHERE tank_id=? AND is_active=1 ORDER BY category",
            (tank_id,),
        ).fetchall()
    ]
    return {"inhabitant": inhabitants, "plant": plants, "hardscape": hardscape, "equipment": equipment}


def _parse_link_ref(link_ref: Optional[str]):
    """Parse a "type:id" link_ref into (type, id), or (None, None) if invalid/absent."""
    if link_ref and ":" in link_ref:
        ltype, _, lid = link_ref.partition(":")
        if ltype in COLUMN_BY_TYPE and lid.isdigit():
            return ltype, int(lid)
    return None, None


@router.get("", response_class=HTMLResponse)
async def list_observations(
    request: Request,
    tank_id: int,
    link_type: Optional[str] = None,
    link_id: Optional[int] = None,
    link_ref: Optional[str] = None,
    search: Optional[str] = None,
    source: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")

        entity_options = _entity_options(conn, tank_id)

        # link_ref (from the on-page "Linked to" filter select) takes priority over the
        # legacy link_type/link_id pair (still used by the "Observations" buttons on the
        # inhabitant/plant/hardscape/equipment pages).
        active_filter = None
        extra_where = ""
        params: list = [tank_id]
        if link_ref == "any":
            active_filter = {"type": "any", "id": None, "label": "any linked item"}
            extra_where += (" AND (o.related_inhabitant_id IS NOT NULL OR o.related_plant_id IS NOT NULL"
                             " OR o.related_hardscape_id IS NOT NULL OR o.related_equipment_id IS NOT NULL)")
        elif link_ref == "none":
            active_filter = {"type": "none", "id": None, "label": "unlinked notes"}
            extra_where += (" AND o.related_inhabitant_id IS NULL AND o.related_plant_id IS NULL"
                             " AND o.related_hardscape_id IS NULL AND o.related_equipment_id IS NULL")
        else:
            ref_type, ref_id = _parse_link_ref(link_ref)
            if ref_type is None and link_type in COLUMN_BY_TYPE and link_id is not None:
                ref_type, ref_id = link_type, link_id
            if ref_type in COLUMN_BY_TYPE and ref_id is not None:
                match = next((o for o in entity_options[ref_type] if o["id"] == ref_id), None)
                if match:
                    active_filter = {"type": ref_type, "id": ref_id, "label": match["label"]}
                    extra_where += f" AND o.{COLUMN_BY_TYPE[ref_type]} = ?"
                    params.append(ref_id)

        if search:
            extra_where += " AND lower(o.text) LIKE ?"
            params.append(f"%{search.lower()}%")
        if source in ("manual", "auto", "import"):
            extra_where += " AND o.source = ?"
            params.append(source)
        if date_from:
            extra_where += " AND date(o.created_at) >= ?"
            params.append(date_from)
        if date_to:
            extra_where += " AND date(o.created_at) <= ?"
            params.append(date_to)

        any_filter = bool(active_filter or search or source or date_from or date_to)
        limit_clause = "" if any_filter else " LIMIT 50"
        observations = rows_to_list(conn.execute(
            f"""SELECT o.*,
                   i.common_name AS li_common, i.species AS li_species,
                   p.common_name AS lp_common, p.species AS lp_species,
                   h.item AS lh_item,
                   e.category AS le_category, e.brand AS le_brand, e.model AS le_model
                FROM observations o
                LEFT JOIN inhabitants i ON i.id = o.related_inhabitant_id
                LEFT JOIN plants p ON p.id = o.related_plant_id
                LEFT JOIN hardscape h ON h.id = o.related_hardscape_id
                LEFT JOIN tank_equipment e ON e.id = o.related_equipment_id
                WHERE o.tank_id = ?{extra_where}
                ORDER BY o.created_at DESC{limit_clause}""",
            params,
        ).fetchall())

    for obs in observations:
        if obs.get("li_common") or obs.get("li_species"):
            obs["link_type"], obs["link_label"] = "inhabitant", obs.get("li_common") or obs.get("li_species")
        elif obs.get("lp_common") or obs.get("lp_species"):
            obs["link_type"], obs["link_label"] = "plant", obs.get("lp_common") or obs.get("lp_species")
        elif obs.get("lh_item"):
            obs["link_type"], obs["link_label"] = "hardscape", obs["lh_item"]
        elif obs.get("le_category") or obs.get("le_brand") or obs.get("le_model"):
            obs["link_type"] = "equipment"
            obs["link_label"] = f"{obs.get('le_brand') or ''} {obs.get('le_model') or ''}".strip() or obs.get("le_category")
        else:
            obs["link_type"] = obs["link_label"] = None

    base_url = f"/tanks/{tank_id}/observations"
    search_params = {k: v for k, v in {
        "search": search, "source": source, "date_from": date_from, "date_to": date_to,
    }.items() if v}
    active_link_ref = None
    if active_filter:
        active_link_ref = active_filter["type"] if active_filter["type"] in ("any", "none") else f'{active_filter["type"]}:{active_filter["id"]}'
    link_params = {"link_ref": active_link_ref} if active_link_ref else {}

    # "Clear filter" (banner) drops the entity link but keeps text/source/date filters
    clear_link_url = base_url + (f"?{urlencode(search_params)}" if search_params else "")
    # "Clear" (filter bar) drops text/source/date filters but keeps the entity link
    clear_search_url = base_url + (f"?{urlencode(link_params)}" if link_params else "")

    return templates.TemplateResponse("observations/list.html", {
        "request": request, "tank": tank, "observations": observations,
        "entity_options": entity_options, "active_filter": active_filter,
        "clear_link_url": clear_link_url, "clear_search_url": clear_search_url,
        "filter_search": search or "", "filter_source": source or "",
        "filter_date_from": date_from or "", "filter_date_to": date_to or "",
    })


@router.get("/json")
async def list_observations_json(tank_id: int, limit: int = 10):
    with get_db() as conn:
        observations = rows_to_list(conn.execute(
            "SELECT * FROM observations WHERE tank_id = ? ORDER BY created_at DESC LIMIT ?",
            (tank_id, limit),
        ).fetchall())
    return JSONResponse({"observations": observations})


@router.post("")
async def add_observation(
    request: Request,
    tank_id: int,
    text: str = Form(...),
    related_event_id: Optional[int] = Form(None),
    related_test_id: Optional[int] = Form(None),
    link_ref: Optional[str] = Form(None),
):
    related_inhabitant_id = related_plant_id = related_hardscape_id = related_equipment_id = None
    ltype, lid = _parse_link_ref(link_ref)
    if ltype == "inhabitant":
        related_inhabitant_id = lid
    elif ltype == "plant":
        related_plant_id = lid
    elif ltype == "hardscape":
        related_hardscape_id = lid
    elif ltype == "equipment":
        related_equipment_id = lid

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO observations (tank_id, related_event_id, related_test_id,"
            " related_inhabitant_id, related_plant_id, related_hardscape_id, related_equipment_id, source, text)"
            " VALUES (?,?,?,?,?,?,?,'manual',?)",
            (tank_id, related_event_id, related_test_id,
             related_inhabitant_id, related_plant_id, related_hardscape_id, related_equipment_id, text),
        )
        obs_id = cur.lastrowid

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": obs_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


@router.post("/{obs_id}/link")
async def set_observation_link(tank_id: int, obs_id: int, link_ref: Optional[str] = Form(None)):
    """Set, change, or clear the entity link on an existing observation. Empty link_ref clears it."""
    related_inhabitant_id = related_plant_id = related_hardscape_id = related_equipment_id = None
    ltype, lid = _parse_link_ref(link_ref)
    if ltype == "inhabitant":
        related_inhabitant_id = lid
    elif ltype == "plant":
        related_plant_id = lid
    elif ltype == "hardscape":
        related_hardscape_id = lid
    elif ltype == "equipment":
        related_equipment_id = lid

    with get_db() as conn:
        cur = conn.execute(
            "UPDATE observations SET related_inhabitant_id=?, related_plant_id=?,"
            " related_hardscape_id=?, related_equipment_id=?, updated_at=datetime('now')"
            " WHERE id=? AND tank_id=?",
            (related_inhabitant_id, related_plant_id, related_hardscape_id, related_equipment_id, obs_id, tank_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Observation not found")
    return JSONResponse({"status": "updated"})


@router.post("/{obs_id}/delete")
async def delete_observation(tank_id: int, obs_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM observations WHERE id = ? AND tank_id = ?", (obs_id, tank_id))
    return JSONResponse({"status": "deleted"})
