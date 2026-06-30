import os
import json
import logging
from pathlib import Path
from fastapi import APIRouter, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
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
8. SPLIT MULTI-TYPE ENTRIES: A single dated log block often records multiple things at once — a water test AND a water change AND dosing AND observations. Always produce separate records for each, all sharing the same date. Never collapse them into one row or omit the secondary items. Example: "2024-03-15: pH 7.2, kh 5 | 20% WC | dosed Flourish | shrimp active" → 1 test_result (ph=7.2, kh=5) + 1 water_change event (amount=20) + 1 maintenance event (notes="Flourish dose") + 1 observation (shrimp active), all dated 2024-03-15.
9. TEST KIT METHODOLOGY: Phrases describing how a test was performed ("went blue to green", "9 drops to change color", "waited 5 min", "API kit") describe kit procedure, not numeric values. Store them in the test_result's notes field if informative, or discard them entirely. Never parse kit methodology text as a numeric water parameter.

Valid event_type values: water_change, feeding, purchase, observation, treatment, maintenance, other
Valid equipment categories: filter, heater, light, uv, pump, co2, other
Valid purchase categories: equipment, livestock, plants, hardscape, consumables, food, decor, other
Valid issue status: open, monitoring, resolved
Valid plant status: active, removed

Use "YYYY-MM-DD 00:00:00" for timestamps where time is unknown. Omit tank_specs fields that are null. Return empty arrays (not null) for sections with no data found. Do NOT invent data that is not present or clearly inferable.

TEXT TO PARSE:
"""


def _split_chunks(content: str, max_chars: int = 15000) -> list:
    """Split content at paragraph boundaries so each chunk fits in one Claude response."""
    if len(content) <= max_chars:
        return [content]
    paragraphs = content.split('\n\n')
    chunks, current, current_len = [], [], 0
    for para in paragraphs:
        para_len = len(para) + 2
        if current_len + para_len > max_chars and current:
            chunks.append('\n\n'.join(current))
            current, current_len = [para], para_len
        else:
            current.append(para)
            current_len += para_len
    if current:
        chunks.append('\n\n'.join(current))
    return chunks


def _merge_results(results: list) -> tuple:
    """Merge per-chunk extraction results; offset flag indices into the merged arrays."""
    merged = {}
    all_flags = []
    array_keys = ["test_results", "events", "purchases", "inhabitants", "plants",
                  "equipment", "hardscape", "issues", "observations"]
    for result in results:
        specs = result.get("tank_specs")
        if specs and isinstance(specs, dict):
            merged.setdefault("tank_specs", {})
            for k, v in specs.items():
                if v is not None and k not in merged["tank_specs"]:
                    merged["tank_specs"][k] = v
        section_offsets = {}
        for key in array_keys:
            items = result.get(key) or []
            section_offsets[key] = len(merged.get(key, []))
            merged.setdefault(key, []).extend(items)
        for flag in result.get("flags") or []:
            if not isinstance(flag, dict):
                continue
            section = flag.get("section")
            adjusted = dict(flag)
            if section in section_offsets:
                adjusted["index"] = flag.get("index", 0) + section_offsets[section]
            all_flags.append(adjusted)
    return merged, all_flags


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

    if len(content) > 100000:
        content = content[:100000] + "\n...[truncated]"

    chunks = _split_chunks(content, max_chars=8000)
    n_chunks = len(chunks)
    chunk_line_counts = [max(1, c.count('\n') + 1) for c in chunks]
    total_lines = sum(chunk_line_counts)

    async def generate():
        import anthropic
        import re as _re

        def evt(payload):
            return f'data: {json.dumps(payload)}\n\n'

        yield evt({"phase": "analyzing", "label": "Analyzing with AI…", "current": 0, "total": total_lines})

        try:
            client = anthropic.AsyncAnthropic(api_key=api_key)
            chunk_results = []
            lines_done = 0

            for i, chunk in enumerate(chunks):
                label = f"Analyzing part {i + 1} of {n_chunks}…" if n_chunks > 1 else "Analyzing with AI…"
                chunk_total = chunk_line_counts[i]
                chunk_current = 0
                chunk_header = (
                    f"[Part {i + 1} of {n_chunks} — extract all data found in this section]\n\n"
                    if n_chunks > 1 else ""
                )
                full_text = ''

                # Signal start of this chunk immediately, before waiting for Claude
                yield evt({"phase": "analyzing", "label": label, "current": lines_done, "total": total_lines})

                async with client.messages.stream(
                    model="claude-sonnet-4-6",
                    max_tokens=32000,
                    messages=[{"role": "user", "content": IMPORT_PROMPT + chunk_header + chunk}],
                ) as stream:
                    async for text in stream.text_stream:
                        full_text += text
                        new_lines = text.count('\n')
                        if new_lines > 0:
                            chunk_current += new_lines
                            bar_pos = min(chunk_current, chunk_total)
                            if chunk_current > chunk_total:
                                active_label = (
                                    f"Receiving response, part {i + 1} of {n_chunks}… ({chunk_current} lines)"
                                    if n_chunks > 1 else f"Receiving AI response… ({chunk_current} lines)"
                                )
                            else:
                                active_label = label
                            yield evt({"phase": "analyzing", "label": active_label,
                                       "current": lines_done + bar_pos, "total": total_lines})
                    finish_label = (f"Finishing part {i + 1} of {n_chunks} response…"
                                    if n_chunks > 1 else "Finishing AI response…")
                    yield evt({"phase": "analyzing", "label": finish_label,
                               "current": lines_done + chunk_total, "total": total_lines})
                    final_msg = await stream.get_final_message()

                if final_msg.stop_reason == 'max_tokens':
                    logger.warning("Import chunk %d/%d truncated at max_tokens for tank %s", i + 1, n_chunks, tank_id)
                    suffix = f" (part {i + 1} of {n_chunks})" if n_chunks > 1 else ""
                    yield evt({"phase": "error",
                               "message": f"Claude's response was cut off{suffix} — this section may be unusually dense."})
                    return

                parse_label = (f"Processing part {i + 1} of {n_chunks} response…"
                               if n_chunks > 1 else "Processing response…")
                yield evt({"phase": "analyzing", "label": parse_label,
                           "current": lines_done + chunk_total, "total": total_lines})

                raw_json = full_text.strip()
                logger.debug("Import chunk %d raw response (first 300): %s", i + 1, raw_json[:300])
                raw_json = _re.sub(r"```json\s*", "", raw_json)
                raw_json = _re.sub(r"```\s*", "", raw_json)
                raw_json = raw_json.strip()
                try:
                    parsed_chunk = json.loads(raw_json)
                except json.JSONDecodeError:
                    match = _re.search(r'\{.*\}', raw_json, _re.DOTALL)
                    if not match:
                        logger.error("Import JSON parse error in chunk %d", i + 1)
                        yield evt({"phase": "error",
                                   "message": f"Claude returned invalid JSON for part {i + 1}."})
                        return
                    parsed_chunk = json.loads(match.group())

                chunk_results.append(parsed_chunk)
                lines_done += chunk_total
                if i + 1 < n_chunks:
                    next_label = f"Starting part {i + 2} of {n_chunks}…"
                    yield evt({"phase": "analyzing", "label": next_label,
                               "current": lines_done, "total": total_lines})

        except Exception as e:
            logger.error("Import stream error: %s", e)
            yield evt({"phase": "error", "message": str(e)})
            return

        merge_label = "Merging results…" if n_chunks > 1 else "Processing results…"
        yield evt({"phase": "processing", "label": merge_label, "current": total_lines, "total": total_lines})

        parsed, flags = _merge_results(chunk_results)
        logger.debug("Import merged %d chunks: %s", n_chunks, {k: len(v) for k, v in parsed.items() if isinstance(v, list)})

        counts = {}
        for k, v in parsed.items():
            if isinstance(v, list) and v:
                counts[k] = len(v)
            elif k == "tank_specs" and isinstance(v, dict):
                non_null = sum(1 for val in v.values() if val is not None)
                if non_null:
                    counts[k] = non_null

        yield evt({"phase": "complete", "preview": parsed, "counts": counts, "flags": flags})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _find_duplicates(tank_id: int, preview: dict, conn) -> list:
    """Check preview rows against existing DB rows; return list of {section, index, message}."""
    dups = []

    # test_results: same date + at least 2 numeric params match
    existing = conn.execute(
        "SELECT date(timestamp), ph, ammonia, nitrate, nitrite, gh, kh, tds, temp"
        " FROM test_results WHERE tank_id=?", (tank_id,)
    ).fetchall()
    param_fields = [("ph", 1), ("ammonia", 2), ("nitrate", 3), ("nitrite", 4),
                    ("gh", 5), ("kh", 6), ("tds", 7), ("temp", 8)]
    for i, tr in enumerate(preview.get("test_results", [])):
        ts = (tr.get("timestamp") or "")[:10]
        if not ts:
            continue
        for row in existing:
            if row[0] != ts:
                continue
            compared = matches = 0
            for field, col in param_fields:
                iv, dv = tr.get(field), row[col]
                if iv is not None and dv is not None:
                    compared += 1
                    try:
                        if abs(float(iv) - float(dv)) < 0.01:
                            matches += 1
                    except (TypeError, ValueError):
                        pass
            if compared == 0 or matches >= 2:
                dups.append({"section": "test_results", "index": i,
                             "message": f"A test result for {ts} already exists in the database."})
                break

    # events: same date + event_type
    existing_set = set(
        (r[0], r[1]) for r in conn.execute(
            "SELECT date(timestamp), event_type FROM events WHERE tank_id=?", (tank_id,)
        ).fetchall()
    )
    for i, ev in enumerate(preview.get("events", [])):
        ts = (ev.get("timestamp") or "")[:10]
        et = ev.get("event_type", "other")
        if ts and (ts, et) in existing_set:
            dups.append({"section": "events", "index": i,
                         "message": f"A '{et}' event on {ts} already exists."})

    # purchases: same purchase_date + item (case-insensitive)
    existing_set = set(
        (r[0], r[1]) for r in conn.execute(
            "SELECT purchase_date, lower(trim(item)) FROM purchases WHERE tank_id=?", (tank_id,)
        ).fetchall()
    )
    for i, pur in enumerate(preview.get("purchases", [])):
        d = pur.get("purchase_date") or ""
        item = (pur.get("item") or "").lower().strip()
        if d and item and (d, item) in existing_set:
            dups.append({"section": "purchases", "index": i,
                         "message": f"Purchase '{pur.get('item')}' on {d} already exists."})

    # inhabitants: same species + added_date (only when both are non-null)
    existing_set = set(
        (r[0], r[1]) for r in conn.execute(
            "SELECT lower(trim(species)), added_date FROM inhabitants WHERE tank_id=?", (tank_id,)
        ).fetchall() if r[0] and r[1]
    )
    for i, inh in enumerate(preview.get("inhabitants", [])):
        sp = (inh.get("species") or "").lower().strip()
        d = inh.get("added_date") or ""
        if sp and d and (sp, d) in existing_set:
            dups.append({"section": "inhabitants", "index": i,
                         "message": f"Inhabitant '{inh.get('species')}' added {d} already exists."})

    # plants: same species + added_date (active plants)
    existing_set = set(
        (r[0], r[1]) for r in conn.execute(
            "SELECT lower(trim(species)), added_date FROM plants WHERE tank_id=? AND status='active'", (tank_id,)
        ).fetchall() if r[0] and r[1]
    )
    for i, pl in enumerate(preview.get("plants", [])):
        sp = (pl.get("species") or "").lower().strip()
        d = pl.get("added_date") or ""
        if sp and d and (sp, d) in existing_set:
            name = pl.get("species") or pl.get("common_name") or "plant"
            dups.append({"section": "plants", "index": i,
                         "message": f"Plant '{name}' added {d} already exists."})

    # equipment: same brand + model (case-insensitive, active only)
    existing_set = set(
        (r[0] or "", r[1] or "") for r in conn.execute(
            "SELECT lower(trim(brand)), lower(trim(model))"
            " FROM tank_equipment WHERE tank_id=? AND is_active=1", (tank_id,)
        ).fetchall()
    )
    for i, eq in enumerate(preview.get("equipment", [])):
        brand = (eq.get("brand") or "").lower().strip()
        model = (eq.get("model") or "").lower().strip()
        if brand and model and (brand, model) in existing_set:
            dups.append({"section": "equipment", "index": i,
                         "message": f"Equipment '{eq.get('brand')} {eq.get('model')}' is already listed."})

    # hardscape: same item name (case-insensitive)
    existing_set = set(
        r[0] for r in conn.execute(
            "SELECT lower(trim(item)) FROM hardscape WHERE tank_id=?", (tank_id,)
        ).fetchall()
    )
    for i, hs in enumerate(preview.get("hardscape", [])):
        item = (hs.get("item") or "").lower().strip()
        if item and item in existing_set:
            dups.append({"section": "hardscape", "index": i,
                         "message": f"Hardscape item '{hs.get('item')}' already exists."})

    # issues: same title (case-insensitive)
    existing_set = set(
        r[0] for r in conn.execute(
            "SELECT lower(trim(title)) FROM issues WHERE tank_id=?", (tank_id,)
        ).fetchall()
    )
    for i, iss in enumerate(preview.get("issues", [])):
        title = (iss.get("title") or "").lower().strip()
        if title and title in existing_set:
            dups.append({"section": "issues", "index": i,
                         "message": f"Issue '{iss.get('title')}' already exists."})

    # observations: same date + first 100 chars of text (case-insensitive)
    existing_set = set(
        (r[0], r[1]) for r in conn.execute(
            "SELECT date(created_at), lower(substr(text,1,100)) FROM observations WHERE tank_id=?", (tank_id,)
        ).fetchall() if r[0] and r[1]
    )
    for i, obs in enumerate(preview.get("observations", [])):
        ts = (obs.get("created_at") or "")[:10]
        snippet = (obs.get("text") or "").lower()[:100]
        if ts and snippet and (ts, snippet) in existing_set:
            dups.append({"section": "observations", "index": i,
                         "message": "This observation appears to already be recorded."})

    return dups


@router.post("/tanks/{tank_id}/import/check-duplicates")
async def import_check_duplicates(tank_id: int, request: Request):
    body = await request.json()
    preview = body.get("preview", {})
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT id FROM tanks WHERE id=?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        duplicates = _find_duplicates(tank_id, preview, conn)
    return JSONResponse({"duplicates": duplicates})


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
