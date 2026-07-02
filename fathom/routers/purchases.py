from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from typing import Optional
from database import get_db, rows_to_list, row_to_dict

router = APIRouter(tags=["purchases"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/tanks/{tank_id}/purchases", response_class=HTMLResponse)
async def list_purchases(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        purchases = rows_to_list(conn.execute(
            "SELECT * FROM purchases WHERE tank_id = ? ORDER BY purchase_date DESC",
            (tank_id,),
        ).fetchall())
        total = conn.execute(
            "SELECT SUM(cost) as total FROM purchases WHERE tank_id = ?", (tank_id,),
        ).fetchone()["total"] or 0
    return templates.TemplateResponse("purchases/list.html", {
        "request": request, "tank": tank, "purchases": purchases, "total": total,
    })


@router.post("/tanks/{tank_id}/purchases")
async def add_purchase_for_tank(
    request: Request,
    tank_id: int,
    item: str = Form(...),
    category: str = Form("other"),
    vendor: Optional[str] = Form(None),
    cost: Optional[float] = Form(None),
    purchase_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO purchases (tank_id, item, category, vendor, cost, purchase_date, notes)"
            " VALUES (?,?,?,?,?,?,?)",
            (tank_id, item, category, vendor, cost, purchase_date, notes),
        )
        purchase_id = cur.lastrowid

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": purchase_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}/purchases", status_code=303)


@router.post("/purchases/{purchase_id}/update")
async def update_purchase(
    request: Request,
    purchase_id: int,
    item: str = Form(...),
    category: str = Form("other"),
    vendor: Optional[str] = Form(None),
    cost: Optional[float] = Form(None),
    purchase_date: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    with get_db() as conn:
        row = row_to_dict(conn.execute(
            "SELECT tank_id FROM purchases WHERE id = ?", (purchase_id,),
        ).fetchone())
        if not row:
            raise HTTPException(status_code=404, detail="Purchase not found")
        conn.execute(
            "UPDATE purchases SET item=?, category=?, vendor=?, cost=?, purchase_date=?, notes=?,"
            " updated_at=datetime('now') WHERE id=?",
            (item, category, vendor, cost, purchase_date, notes, purchase_id),
        )
    return RedirectResponse(url=f"/tanks/{row['tank_id']}/purchases", status_code=303)


@router.post("/purchases/{purchase_id}/delete")
async def delete_purchase(request: Request, purchase_id: int):
    with get_db() as conn:
        row = row_to_dict(conn.execute(
            "SELECT tank_id FROM purchases WHERE id = ?", (purchase_id,),
        ).fetchone())
        conn.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
    redirect = f"/tanks/{row['tank_id']}/purchases" if row and row.get("tank_id") else "/tanks"
    return RedirectResponse(url=redirect, status_code=303)
