from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/inhabitants", tags=["inhabitants"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def list_inhabitants(request: Request, tank_id: int):
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
    return templates.TemplateResponse("inhabitants/list.html", {
        "request": request, "tank": tank,
        "inhabitants": inhabitants, "pop_events": pop_events,
    })


@router.post("")
async def add_inhabitant(
    request: Request,
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
