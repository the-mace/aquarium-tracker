from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(prefix="/tanks/{tank_id}/issues", tags=["issues"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def list_issues(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        issues = rows_to_list(conn.execute(
            "SELECT * FROM issues WHERE tank_id = ? ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'monitoring' THEN 1 ELSE 2 END, opened_at DESC",
            (tank_id,),
        ).fetchall())
    return templates.TemplateResponse("issues/list.html", {
        "request": request, "tank": tank, "issues": issues,
    })


@router.post("")
async def add_issue(
    request: Request,
    tank_id: int,
    title: str = Form(...),
    description: Optional[str] = Form(None),
    status: str = Form("open"),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO issues (tank_id, title, description, status, notes) VALUES (?,?,?,?,?)",
            (tank_id, title, description, status, notes),
        )
        issue_id = cur.lastrowid

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": issue_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}/issues", status_code=303)


@router.post("/{issue_id}/update")
async def update_issue(
    request: Request,
    tank_id: int,
    issue_id: int,
    title: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    status: str = Form("open"),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT * FROM issues WHERE id = ? AND tank_id = ?", (issue_id, tank_id),
        ).fetchone())
        if not existing:
            raise HTTPException(status_code=404, detail="Issue not found")

        resolved_at = "datetime('now')" if status == "resolved" and existing["status"] != "resolved" else existing.get("resolved_at")
        if status == "resolved" and existing["status"] != "resolved":
            conn.execute(
                """UPDATE issues SET title=?, description=?, status=?, notes=?,
                   resolved_at=datetime('now'), updated_at=datetime('now') WHERE id=?""",
                (title or existing["title"], description, status, notes, issue_id),
            )
        else:
            conn.execute(
                "UPDATE issues SET title=?, description=?, status=?, notes=?, updated_at=datetime('now') WHERE id=?",
                (title or existing["title"], description, status, notes, issue_id),
            )

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"status": "updated"})
    return RedirectResponse(url=f"/tanks/{tank_id}/issues", status_code=303)


@router.post("/{issue_id}/delete")
async def delete_issue(tank_id: int, issue_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM issues WHERE id = ? AND tank_id = ?", (issue_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/issues", status_code=303)
