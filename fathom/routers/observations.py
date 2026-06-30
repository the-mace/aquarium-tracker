from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/observations", tags=["observations"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def list_observations(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        observations = rows_to_list(conn.execute(
            "SELECT * FROM observations WHERE tank_id = ? ORDER BY created_at DESC LIMIT 50",
            (tank_id,),
        ).fetchall())
    return templates.TemplateResponse("observations/list.html", {
        "request": request, "tank": tank, "observations": observations,
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
):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO observations (tank_id, related_event_id, related_test_id, source, text)"
            " VALUES (?,?,?,'manual',?)",
            (tank_id, related_event_id, related_test_id, text),
        )
        obs_id = cur.lastrowid

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": obs_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


@router.post("/{obs_id}/delete")
async def delete_observation(tank_id: int, obs_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM observations WHERE id = ? AND tank_id = ?", (obs_id, tank_id))
    return JSONResponse({"status": "deleted"})
