import os
import json
import re
import logging
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from database import get_db, row_to_dict

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference_info"])


def _canonical(name: str) -> str:
    return name.lower().strip() if name else ""


def maybe_fetch_reference_info(
    background_tasks: BackgroundTasks,
    entity_type: str,
    entity_name: str,
    display_name: str = "",
):
    """Queue a reference info fetch only if no entry exists for this entity yet."""
    if not entity_name:
        return
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM reference_info WHERE entity_type=? AND entity_name=?",
            (entity_type, entity_name),
        ).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT OR IGNORE INTO reference_info (entity_type, entity_name, common_name) VALUES (?,?,?)",
            (entity_type, entity_name, display_name or None),
        )
    background_tasks.add_task(fetch_reference_info_bg, entity_type, entity_name, display_name)


def fetch_reference_info_bg(entity_type: str, entity_name: str, display_name: str = ""):
    """Sync background task: call Claude with web search to get description, care notes, and image."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return

    with get_db() as conn:
        row = row_to_dict(conn.execute(
            "SELECT fetched_at FROM reference_info WHERE entity_type=? AND entity_name=?",
            (entity_type, entity_name),
        ).fetchone())
        if row and row.get("fetched_at"):
            return  # Already fetched

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        type_labels = {
            "species": "aquarium fish, shrimp, or invertebrate species",
            "plant": "aquarium plant species",
            "hardscape": "aquarium hardscape item",
        }
        care_context = {
            "species": "water parameters (pH, GH, temperature range), minimum tank size, diet/feeding, and compatibility with other species",
            "plant": "lighting level (low/medium/high), CO2 requirements, fertilization needs, substrate preference, and growth rate",
            "hardscape": "preparation before aquarium use (boiling, soaking, curing time), effects on water chemistry (pH, hardness, tannins), and suitability for planted or fish-only tanks",
        }

        name_label = display_name or entity_name

        prompt = f"""Look up information about this {type_labels.get(entity_type, 'aquarium item')}: "{name_label}"

Search the web for accurate information. Then provide:
1. A concise 2-3 sentence description for an aquarium keeper
2. Key care notes covering: {care_context.get(entity_type, 'general care requirements')}
3. A direct image URL from Wikimedia Commons (must start with https://upload.wikimedia.org/wikipedia/commons/ and end in .jpg, .jpeg, .png, or .svg). Search for the species on Wikimedia Commons to find a real image.

Respond ONLY with valid JSON, no explanation or markdown fences:
{{
  "description": "...",
  "care_notes": "...",
  "image_url": "https://upload.wikimedia.org/..." or null,
  "image_source": "Wikimedia Commons" or null,
  "image_attribution": "Photographer name, CC BY-SA X.X" or null
}}"""

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[{"type": "web_search_20260209", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
        )

        text = ""
        for block in msg.content:
            if hasattr(block, "text"):
                text += block.text

        text = text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)

        data = json.loads(text)

        # Validate image URL — must actually be an upload.wikimedia.org image
        image_url = data.get("image_url")
        if image_url and not (
            image_url.startswith("https://upload.wikimedia.org/")
            and re.search(r"\.(jpg|jpeg|png|svg)(\?|$)", image_url, re.IGNORECASE)
        ):
            image_url = None

        with get_db() as conn:
            conn.execute(
                """INSERT INTO reference_info
                   (entity_type, entity_name, common_name, description, care_notes,
                    image_url, image_source, image_attribution, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(entity_type, entity_name) DO UPDATE SET
                     description = excluded.description,
                     care_notes = excluded.care_notes,
                     image_url = excluded.image_url,
                     image_source = excluded.image_source,
                     image_attribution = excluded.image_attribution,
                     fetched_at = excluded.fetched_at,
                     updated_at = datetime('now')""",
                (entity_type, entity_name, display_name or None,
                 data.get("description"), data.get("care_notes"),
                 image_url, data.get("image_source") if image_url else None,
                 data.get("image_attribution") if image_url else None),
            )
        logger.info("Reference info fetched for %s/%s", entity_type, entity_name)

    except Exception as e:
        logger.error("Reference info fetch failed for %s/%s: %s", entity_type, entity_name, e)
        try:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO reference_info (entity_type, entity_name, common_name, fetched_at)
                       VALUES (?,?,?,datetime('now'))
                       ON CONFLICT(entity_type, entity_name) DO UPDATE SET
                         fetched_at = datetime('now'), updated_at = datetime('now')""",
                    (entity_type, entity_name, display_name or None),
                )
        except Exception:
            pass


@router.get("/reference-info")
async def get_reference_info(entity_type: str, entity_name: str):
    with get_db() as conn:
        row = row_to_dict(conn.execute(
            "SELECT * FROM reference_info WHERE entity_type=? AND entity_name=?",
            (entity_type, entity_name),
        ).fetchone())
    if not row:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse(row)


@router.post("/reference-info/refresh")
async def refresh_reference_info(background_tasks: BackgroundTasks, request: Request):
    body = await request.json()
    entity_type = body.get("entity_type", "")
    entity_name = body.get("entity_name", "")
    display_name = body.get("display_name", "")

    if not entity_type or not entity_name:
        return JSONResponse({"error": "entity_type and entity_name required"}, status_code=400)

    with get_db() as conn:
        conn.execute(
            """INSERT INTO reference_info (entity_type, entity_name, common_name) VALUES (?,?,?)
               ON CONFLICT(entity_type, entity_name) DO UPDATE SET
                 fetched_at = NULL, description = NULL, care_notes = NULL,
                 image_url = NULL, image_source = NULL, image_attribution = NULL,
                 updated_at = datetime('now')""",
            (entity_type, entity_name, display_name or None),
        )

    background_tasks.add_task(fetch_reference_info_bg, entity_type, entity_name, display_name)
    return JSONResponse({"status": "refresh_queued"})
