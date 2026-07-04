from pathlib import Path
from urllib.parse import urlencode
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import List, Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/observations", tags=["observations"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

ENTITY_TYPES = ("inhabitant", "plant", "hardscape", "equipment")


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
        if ltype in ENTITY_TYPES and lid.isdigit():
            return ltype, int(lid)
    return None, None


def _parse_link_refs(link_refs) -> list:
    """Parse a list of "type:id" refs into deduped (type, id) tuples, skipping invalid/empty ones."""
    seen = []
    for ref in link_refs or []:
        ltype, lid = _parse_link_ref(ref)
        if ltype and (ltype, lid) not in seen:
            seen.append((ltype, lid))
    return seen


def _links_by_observation(conn, obs_ids: list) -> dict:
    """Fetch all observation_links rows for the given observation ids, resolved to display labels."""
    if not obs_ids:
        return {}
    placeholders = ",".join("?" for _ in obs_ids)
    rows = conn.execute(
        f"""SELECT ol.observation_id, ol.entity_type, ol.entity_id,
               i.common_name AS i_common, i.species AS i_species,
               p.common_name AS p_common, p.species AS p_species,
               h.item AS h_item,
               e.category AS e_category, e.brand AS e_brand, e.model AS e_model
            FROM observation_links ol
            LEFT JOIN inhabitants i ON i.id = ol.entity_id AND ol.entity_type='inhabitant'
            LEFT JOIN plants p ON p.id = ol.entity_id AND ol.entity_type='plant'
            LEFT JOIN hardscape h ON h.id = ol.entity_id AND ol.entity_type='hardscape'
            LEFT JOIN tank_equipment e ON e.id = ol.entity_id AND ol.entity_type='equipment'
            WHERE ol.observation_id IN ({placeholders})
            ORDER BY ol.id""",
        obs_ids,
    ).fetchall()

    links_by_obs: dict = {}
    for row in rows:
        etype = row["entity_type"]
        if etype == "inhabitant":
            label = row["i_common"] or row["i_species"] or f"Inhabitant #{row['entity_id']}"
        elif etype == "plant":
            label = row["p_common"] or row["p_species"] or f"Plant #{row['entity_id']}"
        elif etype == "hardscape":
            label = row["h_item"] or f"Hardscape #{row['entity_id']}"
        else:
            label = f"{row['e_brand'] or ''} {row['e_model'] or ''}".strip() or row["e_category"] or f"Equipment #{row['entity_id']}"
        links_by_obs.setdefault(row["observation_id"], []).append(
            {"type": etype, "id": row["entity_id"], "label": label}
        )
    return links_by_obs


def _set_observation_links(conn, obs_id: int, refs: list):
    """Replace all entity links on an observation with the given deduped (type, id) tuples."""
    conn.execute("DELETE FROM observation_links WHERE observation_id=?", (obs_id,))
    for ltype, lid in refs:
        conn.execute(
            "INSERT OR IGNORE INTO observation_links (observation_id, entity_type, entity_id) VALUES (?,?,?)",
            (obs_id, ltype, lid),
        )


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
            extra_where += " AND EXISTS (SELECT 1 FROM observation_links ol2 WHERE ol2.observation_id = o.id)"
        elif link_ref == "none":
            active_filter = {"type": "none", "id": None, "label": "unlinked notes"}
            extra_where += " AND NOT EXISTS (SELECT 1 FROM observation_links ol2 WHERE ol2.observation_id = o.id)"
        else:
            ref_type, ref_id = _parse_link_ref(link_ref)
            if ref_type is None and link_type in ENTITY_TYPES and link_id is not None:
                ref_type, ref_id = link_type, link_id
            if ref_type in ENTITY_TYPES and ref_id is not None:
                match = next((o for o in entity_options[ref_type] if o["id"] == ref_id), None)
                if match:
                    active_filter = {"type": ref_type, "id": ref_id, "label": match["label"]}
                    extra_where += (" AND EXISTS (SELECT 1 FROM observation_links ol2"
                                     " WHERE ol2.observation_id = o.id AND ol2.entity_type = ? AND ol2.entity_id = ?)")
                    params.append(ref_type)
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
            f"""SELECT o.* FROM observations o
                WHERE o.tank_id = ?{extra_where}
                ORDER BY o.created_at DESC{limit_clause}""",
            params,
        ).fetchall())

        links_by_obs = _links_by_observation(conn, [o["id"] for o in observations])

    for obs in observations:
        obs["links"] = links_by_obs.get(obs["id"], [])
        obs["link_refs"] = [f"{l['type']}:{l['id']}" for l in obs["links"]]

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
    link_ref: List[str] = Form([]),
    return_to: Optional[str] = Form(None),
):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO observations (tank_id, related_event_id, related_test_id, source, text)"
            " VALUES (?,?,?,'manual',?)",
            (tank_id, related_event_id, related_test_id, text),
        )
        obs_id = cur.lastrowid
        _set_observation_links(conn, obs_id, _parse_link_refs(link_ref))

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": obs_id, "status": "created"}, status_code=201)
    dest = f"/tanks/{tank_id}/observations" if return_to == "observations" else f"/tanks/{tank_id}"
    return RedirectResponse(url=dest, status_code=303)


@router.post("/{obs_id}/update")
async def update_observation(tank_id: int, obs_id: int, text: str = Form(...)):
    with get_db() as conn:
        obs_row = conn.execute("SELECT id FROM observations WHERE id=? AND tank_id=?", (obs_id, tank_id)).fetchone()
        if not obs_row:
            raise HTTPException(status_code=404, detail="Observation not found")
        conn.execute(
            "UPDATE observations SET text=?, updated_at=datetime('now') WHERE id=?",
            (text, obs_id),
        )
    return JSONResponse({"status": "updated"})


@router.post("/{obs_id}/link")
async def set_observation_link(tank_id: int, obs_id: int, link_ref: List[str] = Form([])):
    """Set, change, or clear the entity links on an existing observation. Empty/no refs clears them all."""
    with get_db() as conn:
        obs_row = conn.execute("SELECT id FROM observations WHERE id=? AND tank_id=?", (obs_id, tank_id)).fetchone()
        if not obs_row:
            raise HTTPException(status_code=404, detail="Observation not found")
        _set_observation_links(conn, obs_id, _parse_link_refs(link_ref))
        conn.execute("UPDATE observations SET updated_at=datetime('now') WHERE id=?", (obs_id,))
    return JSONResponse({"status": "updated"})


@router.post("/{obs_id}/delete")
async def delete_observation(tank_id: int, obs_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM observations WHERE id = ? AND tank_id = ?", (obs_id, tank_id))
    return JSONResponse({"status": "deleted"})
