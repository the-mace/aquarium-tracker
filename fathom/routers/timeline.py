from itertools import groupby
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
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

ORDER BY ts DESC NULLS LAST, kind
"""


@router.get("/{tank_id}/timeline", response_class=HTMLResponse)
async def tank_timeline(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        rows = rows_to_list(conn.execute(_QUERY, (tank_id,) * 6).fetchall())

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
    })
