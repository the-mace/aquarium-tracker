from pathlib import Path
from fastapi import APIRouter, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/tests", tags=["tests"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _parse_float(value: Optional[str]) -> Optional[float]:
    return float(value) if value and value.strip() else None


@router.get("", response_class=HTMLResponse)
async def list_tests(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        tests = rows_to_list(conn.execute(
            "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 50",
            (tank_id,),
        ).fetchall())
    return templates.TemplateResponse("tests/list.html", {"request": request, "tank": tank, "tests": tests})


@router.get("/new", response_class=HTMLResponse)
async def new_test_form(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        latest = row_to_dict(conn.execute(
            "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
            (tank_id,),
        ).fetchone())
    return templates.TemplateResponse("tests/form.html", {"request": request, "tank": tank, "latest": latest})


@router.post("")
async def add_test_result(
    request: Request,
    tank_id: int,
    background_tasks: BackgroundTasks,
    timestamp: Optional[str] = Form(None),
    ph: Optional[str] = Form(None),
    gh: Optional[str] = Form(None),
    kh: Optional[str] = Form(None),
    ammonia: Optional[str] = Form(None),
    nitrite: Optional[str] = Form(None),
    nitrate: Optional[str] = Form(None),
    tds: Optional[str] = Form(None),
    temp: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    ts = timestamp or None
    ph, gh, kh, ammonia, nitrite, nitrate, tds, temp = (
        _parse_float(ph), _parse_float(gh), _parse_float(kh), _parse_float(ammonia),
        _parse_float(nitrite), _parse_float(nitrate), _parse_float(tds), _parse_float(temp),
    )
    with get_db() as conn:
        if ts:
            cur = conn.execute(
                """INSERT INTO test_results (tank_id, timestamp, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (tank_id, ts, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes),
            )
        else:
            cur = conn.execute(
                """INSERT INTO test_results (tank_id, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (tank_id, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes),
            )
        result_id = cur.lastrowid

    from routers.ai_analysis import run_ai_analysis, run_test_recommendation
    background_tasks.add_task(run_ai_analysis, tank_id, "test", result_id)
    background_tasks.add_task(run_test_recommendation, tank_id, result_id)

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": result_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}", status_code=303)


@router.delete("/{result_id}")
async def delete_test_result(tank_id: int, result_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM test_results WHERE id = ? AND tank_id = ?", (result_id, tank_id))
    return JSONResponse({"status": "deleted"})
