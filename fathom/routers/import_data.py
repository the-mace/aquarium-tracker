import os
import json
import logging
from pathlib import Path
from fastapi import APIRouter, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from database import get_db, row_to_dict

router = APIRouter(tags=["import"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
logger = logging.getLogger(__name__)

@router.get("/tanks/{tank_id}/import-page", response_class=HTMLResponse)
async def import_page(request: Request, tank_id: int):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
    if not tank:
        raise HTTPException(status_code=404, detail="Tank not found")
    return templates.TemplateResponse("tanks/import.html", {"request": request, "tank": tank})


IMPORT_PROMPT = """You are a data extraction assistant for an aquarium tracking app.

Extract all aquarium-related data from the provided text and return ONLY a valid JSON object
(no markdown, no explanation) with the following structure. Include only keys for data you actually find:

{
  "test_results": [
    {"timestamp": "YYYY-MM-DD HH:MM:SS", "ph": 7.0, "gh": 8.0, "kh": 5.0,
     "ammonia": 0.0, "nitrite": 0.0, "nitrate": 10.0, "tds": 150.0, "temp": 76.0, "notes": ""}
  ],
  "events": [
    {"timestamp": "YYYY-MM-DD HH:MM:SS", "event_type": "water_change", "notes": "", "amount": 10.0}
  ],
  "purchases": [
    {"item": "", "category": "equipment", "vendor": "", "cost": 0.0, "purchase_date": "YYYY-MM-DD", "notes": ""}
  ],
  "inhabitants": [
    {"species": "", "common_name": "", "count": 1, "added_date": "YYYY-MM-DD", "source": "", "notes": ""}
  ],
  "equipment": [
    {"category": "filter", "brand": "", "model": "", "specs": {}, "installed_date": "YYYY-MM-DD", "notes": ""}
  ]
}

Valid event_type values: water_change, feeding, purchase, observation, treatment, maintenance, other
Valid equipment category values: filter, heater, light, uv, pump, co2, other
Valid purchase category values: equipment, livestock, plants, hardscape, consumables, food, decor, other

If a date has no time, use "YYYY-MM-DD 00:00:00". If you cannot determine a date, omit the timestamp field.
Return empty arrays for categories with no data found. Do NOT invent data.

Text to parse:
"""


def _strip_html(html_content: str) -> str:
    """Very basic HTML-to-text conversion for Apple Notes exports."""
    import re
    text = re.sub(r'<br\s*/?>', '\n', html_content, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</li>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


@router.post("/tanks/{tank_id}/import")
async def import_preview(tank_id: int, file: UploadFile = File(...)):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
    if not tank:
        raise HTTPException(status_code=404, detail="Tank not found")

    raw = await file.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("latin-1")

    filename = file.filename or ""
    if filename.lower().endswith(".html") or filename.lower().endswith(".htm") or content.lstrip().startswith("<"):
        content = _strip_html(content)

    if len(content) > 50000:
        content = content[:50000] + "\n...[truncated]"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": IMPORT_PROMPT + content}],
        )
        raw_json = response.content[0].text.strip()

        # Strip any accidental markdown code fences
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]

        parsed = json.loads(raw_json)
    except json.JSONDecodeError as e:
        logger.error("Import JSON parse error: %s", e)
        raise HTTPException(status_code=422, detail=f"Claude returned invalid JSON: {e}")
    except Exception as e:
        logger.error("Import error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    counts = {k: len(v) for k, v in parsed.items() if isinstance(v, list)}
    return JSONResponse({"preview": parsed, "counts": counts})


@router.post("/tanks/{tank_id}/import/confirm")
async def import_confirm(tank_id: int, request: Request):
    body = await request.json()
    preview = body.get("preview", {})

    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")

        inserted = {}

        for tr in preview.get("test_results", []):
            ts = tr.get("timestamp")
            if ts:
                conn.execute(
                    """INSERT INTO test_results (tank_id, timestamp, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (tank_id, ts, tr.get("ph"), tr.get("gh"), tr.get("kh"),
                     tr.get("ammonia"), tr.get("nitrite"), tr.get("nitrate"),
                     tr.get("tds"), tr.get("temp"), tr.get("notes")),
                )
            else:
                conn.execute(
                    """INSERT INTO test_results (tank_id, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (tank_id, tr.get("ph"), tr.get("gh"), tr.get("kh"),
                     tr.get("ammonia"), tr.get("nitrite"), tr.get("nitrate"),
                     tr.get("tds"), tr.get("temp"), tr.get("notes")),
                )
        inserted["test_results"] = len(preview.get("test_results", []))

        for ev in preview.get("events", []):
            ts = ev.get("timestamp")
            if ts:
                conn.execute(
                    "INSERT INTO events (tank_id, timestamp, event_type, notes, amount) VALUES (?,?,?,?,?)",
                    (tank_id, ts, ev.get("event_type", "other"), ev.get("notes"), ev.get("amount")),
                )
            else:
                conn.execute(
                    "INSERT INTO events (tank_id, event_type, notes, amount) VALUES (?,?,?,?)",
                    (tank_id, ev.get("event_type", "other"), ev.get("notes"), ev.get("amount")),
                )
        inserted["events"] = len(preview.get("events", []))

        for pur in preview.get("purchases", []):
            conn.execute(
                "INSERT INTO purchases (tank_id, item, category, vendor, cost, purchase_date, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, pur.get("item", ""), pur.get("category", "other"),
                 pur.get("vendor"), pur.get("cost"), pur.get("purchase_date"), pur.get("notes")),
            )
        inserted["purchases"] = len(preview.get("purchases", []))

        for inh in preview.get("inhabitants", []):
            cur = conn.execute(
                "INSERT INTO inhabitants (tank_id, species, common_name, count, added_date, source, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, inh.get("species"), inh.get("common_name"), inh.get("count", 1),
                 inh.get("added_date"), inh.get("source"), inh.get("notes")),
            )
            conn.execute(
                "INSERT INTO population_events (tank_id, inhabitant_id, event_type, count) VALUES (?,?,?,?)",
                (tank_id, cur.lastrowid, "added", inh.get("count", 1)),
            )
        inserted["inhabitants"] = len(preview.get("inhabitants", []))

        for eq in preview.get("equipment", []):
            specs = eq.get("specs")
            if isinstance(specs, dict):
                import json as _json
                specs = _json.dumps(specs)
            conn.execute(
                "INSERT INTO tank_equipment (tank_id, category, brand, model, specs, installed_date, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, eq.get("category", "other"), eq.get("brand"),
                 eq.get("model"), specs, eq.get("installed_date"), eq.get("notes")),
            )
        inserted["equipment"] = len(preview.get("equipment", []))

    return JSONResponse({"status": "imported", "inserted": inserted})
