from itertools import groupby
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database import get_db, row_to_dict, rows_to_list

router = APIRouter(prefix="/tanks", tags=["timeline"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

_QUERY = """
SELECT 'event'       AS kind,
       timestamp     AS ts,
       event_type    AS subtype,
       event_type    AS label,
       notes         AS detail,
       amount        AS amount,
       id            AS item_id
FROM events WHERE tank_id = ?

UNION ALL

SELECT 'issue_open',
       opened_at, status, title, description, NULL, id
FROM issues
WHERE tank_id = ? AND opened_at IS NOT NULL AND opened_at != ''

UNION ALL

SELECT 'issue_resolve',
       resolved_at, status, title, description, NULL, id
FROM issues
WHERE tank_id = ? AND resolved_at IS NOT NULL AND resolved_at != ''

UNION ALL

SELECT 'equip_install',
       installed_date, category,
       TRIM(COALESCE(brand,'') || CASE WHEN brand IS NOT NULL AND model IS NOT NULL THEN ' ' ELSE '' END || COALESCE(model,'')),
       notes, NULL, id
FROM tank_equipment
WHERE tank_id = ? AND installed_date IS NOT NULL AND installed_date != ''

UNION ALL

SELECT 'equip_remove',
       removed_date, category,
       TRIM(COALESCE(brand,'') || CASE WHEN brand IS NOT NULL AND model IS NOT NULL THEN ' ' ELSE '' END || COALESCE(model,'')),
       notes, NULL, id
FROM tank_equipment
WHERE tank_id = ? AND removed_date IS NOT NULL AND removed_date != ''

UNION ALL

SELECT 'population',
       pe.timestamp, pe.event_type,
       COALESCE(i.common_name, i.species, 'Unknown species'),
       pe.notes, CAST(pe.count AS REAL), pe.id
FROM population_events pe
LEFT JOIN inhabitants i ON i.id = pe.inhabitant_id
WHERE pe.tank_id = ?

UNION ALL

SELECT 'plant_added',
       added_date, status,
       COALESCE(common_name, species, 'Unknown plant'),
       notes, NULL, id
FROM plants
WHERE tank_id = ? AND added_date IS NOT NULL AND added_date != ''

UNION ALL

SELECT 'hardscape_added',
       added_date, 'added',
       item,
       notes, CAST(quantity AS REAL), id
FROM hardscape
WHERE tank_id = ? AND added_date IS NOT NULL AND added_date != ''

ORDER BY ts DESC NULLS LAST, kind
"""

_KIND_GROUPS = {
    "event": {"event"},
    "issue": {"issue_open", "issue_resolve"},
    "equipment": {"equip_install", "equip_remove"},
    "population": {"population"},
    "plants": {"plant_added"},
    "hardscape": {"hardscape_added"},
}


@router.get("/{tank_id}/timeline", response_class=HTMLResponse)
async def tank_timeline(
    request: Request,
    tank_id: int,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    kind: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        rows = rows_to_list(conn.execute(_QUERY, (tank_id,) * 8).fetchall())

    # Apply Python-level filters
    allowed_kinds = _KIND_GROUPS.get(kind) if kind else None
    search_lower = search.lower() if search else None

    def _matches(r):
        ts = (r.get("ts") or "")[:10]
        if date_from and ts < date_from:
            return False
        if date_to and ts > date_to:
            return False
        if allowed_kinds and r.get("kind") not in allowed_kinds:
            return False
        if search_lower:
            haystack = " ".join(filter(None, [
                r.get("label") or "", r.get("detail") or "", r.get("subtype") or ""
            ])).lower()
            if search_lower not in haystack:
                return False
        return True

    rows = [r for r in rows if _matches(r)]

    def _date(r):
        return (r.get("ts") or "")[:10]

    groups = [
        {"date": date or "Unknown", "entries": list(items)}
        for date, items in groupby(rows, key=_date)
    ]

    return templates.TemplateResponse("tanks/timeline.html", {
        "request": request,
        "tank": tank,
        "groups": groups,
        "total": len(rows),
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "filter_kind": kind or "",
        "filter_search": search or "",
    })
