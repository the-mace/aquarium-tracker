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


IMPORT_PROMPT = """You are a data extraction expert for an aquarium tracking app. Extract ALL aquarium-related data from the provided text.

This text may be a personal journal, Apple Notes export, or narrative log — not necessarily structured data. Read the entire text carefully before extracting, paying special attention to:
- Intro paragraphs describing tank setup costs and specifications
- Narrative mentions of purchases ("picked up a $12 bag of sand")
- Problem/resolution patterns that indicate issues
- Equipment described narratively, including filter modifications and accessories
- Plants and hardscape items (driftwood, rocks, coconut shell, substrate decorations)
- Journal-style entries and qualitative observations

Return ONLY valid JSON (no markdown fences, no explanation). Include only top-level keys for data you actually find:

{
  "tank_specs": {
    "volume_gallons": null,
    "dimensions_l": null,
    "dimensions_w": null,
    "dimensions_h": null,
    "manufacturer": null,
    "model": null,
    "substrate_type": null,
    "substrate_brand": null,
    "substrate_depth_inches": null,
    "setup_date": null,
    "notes": null
  },
  "test_results": [
    {"timestamp": "YYYY-MM-DD HH:MM:SS", "ph": 7.0, "gh": 8.0, "kh": 5.0,
     "ammonia": 0.0, "nitrite": 0.0, "nitrate": 10.0, "tds": 150.0, "temp": 76.0, "notes": ""}
  ],
  "events": [
    {"timestamp": "YYYY-MM-DD HH:MM:SS", "event_type": "water_change", "notes": "", "amount": null}
  ],
  "purchases": [
    {"item": "", "category": "equipment", "vendor": "", "cost": null, "purchase_date": "YYYY-MM-DD", "notes": ""}
  ],
  "inhabitants": [
    {"species": "", "common_name": "", "count": 1, "count_unknown": false, "added_date": "YYYY-MM-DD", "source": "", "notes": ""}
  ],
  "plants": [
    {"species": null, "common_name": "", "added_date": null, "source": null, "notes": null, "status": "active"}
  ],
  "equipment": [
    {"category": "filter", "brand": null, "model": null, "specs": {}, "installed_date": null, "notes": ""}
  ],
  "hardscape": [
    {"item": "", "quantity": 1, "source": null, "cost": null, "added_date": null, "notes": null}
  ],
  "issues": [
    {"title": "", "description": "", "status": "open", "opened_at": "YYYY-MM-DD", "resolved_at": null, "notes": ""}
  ],
  "observations": [
    {"text": "", "created_at": "YYYY-MM-DD HH:MM:SS"}
  ],
  "flags": [
    {"section": "test_results", "index": 0, "field": "kh", "message": "KH of 22 is very high for freshwater (typical range: 3-12 dKH). Please verify."}
  ]
}

EXTRACTION RULES (follow carefully):
1. PURCHASES: Capture ALL cost items — initial setup costs mentioned in intro paragraphs, individual item purchases, consumables. If a total cost is mentioned without itemization, create one purchase record for it.
2. TANK SPECS: If you can identify the tank manufacturer/model (e.g. "Fluval Spec V", "Fluval Spec III", "ADA 60-P"), fill in standard dimensions and volume from your knowledge if not explicitly stated. Note inferred values in the notes field.
3. EQUIPMENT: Be exhaustive — include filter media modifications (foam blocks, bio-media, ceramic rings), prefilter sponges, nozzle changes, floating plant corrals, UV sterilizers, CO2 systems, and any hardware mentioned narratively, not just in structured equipment lists.
4. ISSUES: Look for problem/resolution patterns:
   - "had algae bloom for weeks until I added UV sterilizer which fixed it" → resolved issue, resolved_at = date UV was added
   - "struggling with high nitrates, not sure what to do" → open issue
   - "snails were dying, started feeding zucchini, they recovered" → resolved issue
   - "frogbit wilting, started Flourish dosing, it improved" → resolved issue
5. INHABITANTS: If population is uncountable ("lots of MTS snails", "countless pest snails", "a colony of shrimp"), set count_unknown=true and count=null.
6. OBSERVATIONS: Capture journal entries, personal qualitative notes, and observations (e.g. "shrimp seem very active today", "noticed some plant melt on the anubias"). Do NOT duplicate structured measurement data as observations.
7. FLAGS: Flag values that seem incorrect or unusual:
   - Water parameters out of normal range for the tank type (KH > 15 for freshwater, pH > 8.5 or < 5.5 for freshwater, ammonia > 4, nitrate > 160)
   - Counts that contradict the narrative (e.g. extracted count=5 but the text says "11 shrimp")
   - Dates that seem out of sequence
   - Any value you're uncertain about

Valid event_type values: water_change, feeding, purchase, observation, treatment, maintenance, other
Valid equipment categories: filter, heater, light, uv, pump, co2, other
Valid purchase categories: equipment, livestock, plants, hardscape, consumables, food, decor, other
Valid issue status: open, monitoring, resolved
Valid plant status: active, removed

Use "YYYY-MM-DD 00:00:00" for timestamps where time is unknown. Omit tank_specs fields that are null. Return empty arrays (not null) for sections with no data found. Do NOT invent data that is not present or clearly inferable.

TEXT TO PARSE:
"""


def _strip_html(html_content: str) -> str:
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
    if filename.lower().endswith((".html", ".htm")) or content.lstrip().startswith("<"):
        content = _strip_html(content)

    if len(content) > 60000:
        content = content[:60000] + "\n...[truncated]"

    try:
        import anthropic
        import re as _re
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": IMPORT_PROMPT + content}],
        )
        raw_json = response.content[0].text.strip()
        logger.debug("Import raw response (first 500): %s", raw_json[:500])

        raw_json = _re.sub(r"```json\s*", "", raw_json)
        raw_json = _re.sub(r"```\s*", "", raw_json)
        raw_json = raw_json.strip()

        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            match = _re.search(r'\{.*\}', raw_json, _re.DOTALL)
            if not match:
                raise
            parsed = json.loads(match.group())
    except json.JSONDecodeError as e:
        logger.error("Import JSON parse error: %s", e)
        raise HTTPException(status_code=422, detail=f"Claude returned invalid JSON: {e}")
    except Exception as e:
        logger.error("Import error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    # Separate flags from importable data
    flags = parsed.pop("flags", [])
    if not isinstance(flags, list):
        flags = []

    counts = {}
    for k, v in parsed.items():
        if isinstance(v, list) and v:
            counts[k] = len(v)
        elif k == "tank_specs" and isinstance(v, dict):
            non_null = sum(1 for val in v.values() if val is not None)
            if non_null:
                counts[k] = non_null

    return JSONResponse({"preview": parsed, "counts": counts, "flags": flags})


@router.post("/tanks/{tank_id}/import/confirm")
async def import_confirm(tank_id: int, request: Request):
    body = await request.json()
    preview = body.get("preview", {})

    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")

        inserted = {}

        # Tank specs — UPDATE the existing tank record
        specs = preview.get("tank_specs")
        if specs and isinstance(specs, dict):
            spec_fields = ["volume_gallons", "dimensions_l", "dimensions_w", "dimensions_h",
                           "manufacturer", "model", "substrate_type", "substrate_brand",
                           "substrate_depth_inches", "setup_date", "notes"]
            updates = {k: specs[k] for k in spec_fields if k in specs and specs[k] is not None and specs[k] != ""}
            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                conn.execute(
                    f"UPDATE tanks SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
                    list(updates.values()) + [tank_id],
                )
                inserted["tank_specs"] = len(updates)

        # Test results
        for tr in preview.get("test_results", []):
            ts = tr.get("timestamp")
            if ts:
                conn.execute(
                    "INSERT INTO test_results (tank_id, timestamp, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (tank_id, ts, tr.get("ph"), tr.get("gh"), tr.get("kh"),
                     tr.get("ammonia"), tr.get("nitrite"), tr.get("nitrate"),
                     tr.get("tds"), tr.get("temp"), tr.get("notes")),
                )
            else:
                conn.execute(
                    "INSERT INTO test_results (tank_id, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp, notes)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (tank_id, tr.get("ph"), tr.get("gh"), tr.get("kh"),
                     tr.get("ammonia"), tr.get("nitrite"), tr.get("nitrate"),
                     tr.get("tds"), tr.get("temp"), tr.get("notes")),
                )
        inserted["test_results"] = len(preview.get("test_results", []))

        # Events
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

        # Purchases
        for pur in preview.get("purchases", []):
            conn.execute(
                "INSERT INTO purchases (tank_id, item, category, vendor, cost, purchase_date, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, pur.get("item", ""), pur.get("category", "other"),
                 pur.get("vendor"), pur.get("cost"), pur.get("purchase_date"), pur.get("notes")),
            )
        inserted["purchases"] = len(preview.get("purchases", []))

        # Inhabitants
        for inh in preview.get("inhabitants", []):
            count_val = None if inh.get("count_unknown") else inh.get("count", 1)
            cur = conn.execute(
                "INSERT INTO inhabitants (tank_id, species, common_name, count, added_date, source, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, inh.get("species"), inh.get("common_name"), count_val,
                 inh.get("added_date"), inh.get("source"), inh.get("notes")),
            )
            conn.execute(
                "INSERT INTO population_events (tank_id, inhabitant_id, event_type, count) VALUES (?,?,?,?)",
                (tank_id, cur.lastrowid, "added", count_val or 0),
            )
        inserted["inhabitants"] = len(preview.get("inhabitants", []))

        # Plants
        for pl in preview.get("plants", []):
            conn.execute(
                "INSERT INTO plants (tank_id, species, common_name, added_date, source, notes, status) VALUES (?,?,?,?,?,?,?)",
                (tank_id, pl.get("species"), pl.get("common_name"),
                 pl.get("added_date"), pl.get("source"), pl.get("notes"), pl.get("status", "active")),
            )
        inserted["plants"] = len(preview.get("plants", []))

        # Equipment
        for eq in preview.get("equipment", []):
            specs_val = eq.get("specs")
            if isinstance(specs_val, dict):
                specs_val = json.dumps(specs_val)
            conn.execute(
                "INSERT INTO tank_equipment (tank_id, category, brand, model, specs, installed_date, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, eq.get("category", "other"), eq.get("brand"),
                 eq.get("model"), specs_val, eq.get("installed_date"), eq.get("notes")),
            )
        inserted["equipment"] = len(preview.get("equipment", []))

        # Hardscape
        for hs in preview.get("hardscape", []):
            conn.execute(
                "INSERT INTO hardscape (tank_id, item, quantity, source, cost, added_date, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, hs.get("item", ""), hs.get("quantity", 1),
                 hs.get("source"), hs.get("cost"), hs.get("added_date"), hs.get("notes")),
            )
        inserted["hardscape"] = len(preview.get("hardscape", []))

        # Issues
        for iss in preview.get("issues", []):
            conn.execute(
                "INSERT INTO issues (tank_id, title, description, status, opened_at, resolved_at, notes) VALUES (?,?,?,?,?,?,?)",
                (tank_id, iss.get("title", ""), iss.get("description", ""),
                 iss.get("status", "open"), iss.get("opened_at"), iss.get("resolved_at"), iss.get("notes")),
            )
        inserted["issues"] = len(preview.get("issues", []))

        # Observations
        for obs in preview.get("observations", []):
            ts = obs.get("created_at")
            if ts:
                conn.execute(
                    "INSERT INTO observations (tank_id, source, text, created_at) VALUES (?,?,?,?)",
                    (tank_id, "manual", obs.get("text", ""), ts),
                )
            else:
                conn.execute(
                    "INSERT INTO observations (tank_id, source, text) VALUES (?,?,?)",
                    (tank_id, "manual", obs.get("text", "")),
                )
        inserted["observations"] = len(preview.get("observations", []))

    # Only report non-zero counts
    inserted = {k: v for k, v in inserted.items() if v}
    return JSONResponse({"status": "imported", "inserted": inserted})
