import os
import json
import re
import time
import threading
import logging
import urllib.request
import urllib.parse
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from database import get_db, get_ref_db, row_to_dict

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference_info"])

# Wikimedia asks for a descriptive User-Agent identifying the app + a contact method
# (https://meta.wikimedia.org/wiki/User-Agent_policy) — generic/anonymous UAs are more
# likely to land in a stricter shared rate-limit tier, which is the likely reason the
# mini started getting 429s on image HEAD checks partway through a fetch batch.
_WIKIMEDIA_USER_AGENT = "Fathom/1.0 (https://github.com/the-mace/aquarium-tracker; personal aquarium-tracking app)"

# Minimum gap enforced between any request to *.wikimedia.org (search API + per-image
# HEAD checks), across all entities/threads, to stay well under their rate limits during
# a big batch (e.g. a fresh import queuing many entities' fetches back to back).
_WIKIMEDIA_MIN_INTERVAL = 1.5
_wikimedia_lock = threading.Lock()
_last_wikimedia_request = 0.0


def _wikimedia_throttle():
    global _last_wikimedia_request
    with _wikimedia_lock:
        wait = _last_wikimedia_request + _WIKIMEDIA_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_wikimedia_request = time.monotonic()

# Wikimedia Commons API + DDG fallback produces better results than hand-curated URLs
# for most entities, but reference_info is keyed at the species level, so all color
# morphs of one species (e.g. Neocaridina davidi: Fire Red, Blue Dream, Yellow...) share
# a single image. Curated entries override that when the generic search picks the wrong
# variant. Harmless no-op for keys that don't match any current entity_name.
USE_KNOWN_IMAGES = True

# Curated image URLs — checked before web search. Add entries here to pin an image
# permanently or provide a fallback. Keys: (entity_type, canonical_entity_name).
KNOWN_IMAGES: dict[tuple[str, str], str] = {
    # hardscape
    ("hardscape", "spider wood"): "https://m.media-amazon.com/images/I/81quwi3NdoL._AC_SL1500_.jpg",
    ("hardscape", "petrified wood accent"): "https://aquarockscolorado.com/cdn/shop/products/IMG_5807_29e8f183-ebe4-4a86-99cb-565cbb4dd82d.jpg?v=1620172777&width=1800",
    # plants
    ("plant", "hygrophila sp."):     "https://upload.wikimedia.org/wikipedia/commons/a/a8/Hygrophila_polysperma.JPG",
    ("plant", "littorella uniflora"): "https://upload.wikimedia.org/wikipedia/commons/5/57/Littorella_uniflora_kz01.jpg",
    ("plant", "monosolenium tenerum"): "https://upload.wikimedia.org/wikipedia/commons/8/86/Pellia_endiviifolia_(Clouange-12).JPG",
    ("plant", "taxiphyllum barbieri"): "https://upload.wikimedia.org/wikipedia/commons/a/a5/Javamoos.jpg",
    # Old pick was a murky underwater shot of the roots, leaves barely visible. Both
    # canonical keys pinned since imports have used both "Frogbit" and the scientific
    # name as the species field over time.
    ("plant", "limnobium laevigatum"): "https://upload.wikimedia.org/wikipedia/commons/1/1c/Limnobium_spongia_02.jpg",
    ("plant", "frogbit"):              "https://upload.wikimedia.org/wikipedia/commons/1/1c/Limnobium_spongia_02.jpg",
    ("plant", "helanthium tenellum"):  "https://www.aquariumcoop.com/cdn/shop/files/dwarf-chain-sword-1814832.jpg?v=1766095149&width=832",
    # species
    ("species", "copepoda spp."):    "https://upload.wikimedia.org/wikipedia/commons/2/28/Copepodkils.jpg",
    ("species", "neocaridina davidi"): "https://upload.wikimedia.org/wikipedia/commons/d/d9/Neocaridina-heteropoda-var-red.jpg",
    # "Neritina sp." is used for non-zebra nerite snails, but Claude's own training-knowledge
    # scientific_name guess for a bare "Nerite Snail (non-zebra)" query still comes back
    # "Neritina natalensis" — the zebra nerite's own scientific name — which then pulls the
    # zebra-patterned photo for the "non-zebra" entry too. Pin a genuinely non-striped species.
    ("species", "neritina sp."): "https://upload.wikimedia.org/wikipedia/commons/b/b5/Neritina_reclivata.jpg",
    # Otocinclus and Kuhli Loach searches now avoid the old book-scan/map candidates thanks to
    # the broadened illustration filter above, but pinned anyway for certainty rather than
    # relying solely on that heuristic holding up against future Commons uploads.
    ("species", "otocinclus vittatus"): "https://upload.wikimedia.org/wikipedia/commons/0/0a/Otocinclus3.jpg",
    ("species", "otocinclus sp."):      "https://upload.wikimedia.org/wikipedia/commons/0/0a/Otocinclus3.jpg",
    ("species", "pangio kuhlii"):       "https://upload.wikimedia.org/wikipedia/commons/c/cc/Kuhli_loach.JPG",
    ("species", "oligochaeta spp."): "https://upload.wikimedia.org/wikipedia/commons/4/4e/Naididae.jpg",
    ("species", "ostracoda spp."):   "https://upload.wikimedia.org/wikipedia/commons/9/93/Ostracod.JPG",
    ("species", "physidae sp."):     "https://upload.wikimedia.org/wikipedia/commons/5/53/Physa_acuta_001.JPG",
    ("species", "planorbidae sp."):  "https://upload.wikimedia.org/wikipedia/commons/1/15/Ramshorn_Snail_(Planorbidae)_-_Guelph,_Ontario.jpg",
    # The Commons search's top verified candidate for "Tanichthys albonubes" was a real but
    # useless photo — a tiny, unidentifiable fry adrift in a wall of green algae/plants, no
    # detail visible. Pinned to a proper full-frame adult shot instead.
    ("species", "tanichthys albonubes"): "https://upload.wikimedia.org/wikipedia/commons/e/e8/White_Cloud_Mountain_minnow_(Tanichthys_albonubes)_captive_adult_female.jpg",
}

# Tracks entities whose background fetch is currently running.
# Prevents multiple concurrent tasks for the same entity when fetched_at=NULL
# and the list page is loaded repeatedly.
_in_flight: set[tuple[str, str]] = set()

# Wikimedia Commons is full of public-domain botanical/zoological plate scans
# (Britton & Brown 1913 "BB-" plates, old "NA-" flora plates, herbarium sheet
# scans, range maps) that rank highly for scientific-name searches but are
# line drawings/diagrams, not photos. Filtered out of image candidates by
# filename/title so we prefer actual photographs (e.g. iNaturalist uploads).
# `\bmap\b` (bare word, not just "range/distribution map") was added after a
# literal "Kuhli Loach Map.png" range-map file slipped through and got picked
# as the species image. `\(\d+ of \d+\)` catches multi-page composite plates
# from taxonomic checklist papers (e.g. "Fish fauna from Rio Pilcomayo
# National Park (5 of 7)") that depict a dozen unrelated species side by side,
# not a photo of the one species being searched for.
_ILLUSTRATION_PATTERN = re.compile(
    r"(?i)(illustration|drawing|engrav|lithograph|clip[- ]?art|line[-_ ]art|"
    r"herbarium|specimen|woodcut|sketch|diagram|\bmap\b|"
    r"\bnymap\b|\bBB-\d{4}\b|\bNA-\d{4}\b|\bFMIB\b|\(\d+ of \d+\))"
)

# Filename patterns rarely say "illustration" (e.g. Commons' bulk-uploaded
# "FMIB_12345_..." scans from 1920s field guides). Commons attaches a category
# like "X - botanical illustrations" to nearly all of these regardless of
# filename, so checking categories catches what the filename regex misses.
# "Internet Archive Book Images" / "Biodiversity Heritage Library" flag old
# scanned-book plates specifically (added after an 1906 journal scan showing
# skeletal/fin-ray diagrams — not a photo — was picked for Otocinclus vittatus,
# despite a filename that gave no hint it was a scan).
_ILLUSTRATION_CATEGORY_PATTERN = re.compile(
    r"(?i)(illustration|engraving|lithograph|woodcut|clip ?art|line art|"
    r"herbarium|diagram|drawing|sketch|distribution map|range map|"
    r"internet archive book images|biodiversity heritage library|"
    r"freshwater and marine image bank)"
)


def _canonical(name: str) -> str:
    return name.lower().strip() if name else ""


# Tracks tank ids whose dimensions/volume fetch is currently running.
_dim_in_flight: set[int] = set()


def maybe_fetch_tank_dimensions(background_tasks: BackgroundTasks, tank_id: int):
    """Queue a web-search fetch of a known tank product's volume/dimensions, if the tank
    has a manufacturer or model on record but is still missing volume or dimensions."""
    if tank_id in _dim_in_flight:
        return
    with get_db() as conn:
        tank = row_to_dict(conn.execute(
            "SELECT manufacturer, model, volume_gallons, dimensions_l, dimensions_w, dimensions_h"
            " FROM tanks WHERE id = ?",
            (tank_id,),
        ).fetchone())
    if not tank:
        return
    manufacturer, model = tank.get("manufacturer"), tank.get("model")
    if not manufacturer and not model:
        return
    missing = (
        tank.get("volume_gallons") is None or tank.get("dimensions_l") is None
        or tank.get("dimensions_w") is None or tank.get("dimensions_h") is None
    )
    if not missing:
        return
    _dim_in_flight.add(tank_id)
    background_tasks.add_task(fetch_tank_dimensions_bg, tank_id, manufacturer, model)


def fetch_tank_dimensions_bg(tank_id: int, manufacturer: str, model: str):
    """Sync background task: web-search for a known tank product's rated volume and external
    dimensions, then backfill only the tank's still-empty spec fields — never overwrites a
    value the user (or import extraction) already set."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        _dim_in_flight.discard(tank_id)
        return

    product_label = " ".join(p for p in (manufacturer, model) if p).strip()
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        prompt = f"""Search the web for the official specifications of this aquarium tank/kit: "{product_label}"

Find its rated volume (gallons) and external dimensions (length x width x height, in inches).

Respond ONLY with valid JSON, no explanation or markdown fences:
{{
  "volume_gallons": <number or null>,
  "dimensions_l": <number or null, inches, longest external side>,
  "dimensions_w": <number or null, inches>,
  "dimensions_h": <number or null, inches>,
  "shape": "<rectangular|bowfront|cube|hexagon|other, or null>"
}}"""

        logger.info("Claude call: tank-dims | tank=%d | %s", tank_id, product_label)
        t0 = time.monotonic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content": prompt}],
            timeout=45.0,
        )
        logger.info("Claude done: tank-dims | tank=%d | in=%d out=%d elapsed=%.1fs",
                    tank_id, msg.usage.input_tokens, msg.usage.output_tokens, time.monotonic() - t0)
        raw_text = "".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```\s*$", "", raw_text)
        m = re.search(r"\{.*\}", raw_text, re.DOTALL)
        data = json.loads(m.group(0) if m else raw_text)

        with get_db() as conn:
            conn.execute(
                """UPDATE tanks SET
                     volume_gallons = COALESCE(volume_gallons, ?),
                     dimensions_l = COALESCE(dimensions_l, ?),
                     dimensions_w = COALESCE(dimensions_w, ?),
                     dimensions_h = COALESCE(dimensions_h, ?),
                     shape = COALESCE(shape, ?),
                     updated_at = datetime('now')
                   WHERE id = ?""",
                (data.get("volume_gallons"), data.get("dimensions_l"), data.get("dimensions_w"),
                 data.get("dimensions_h"), data.get("shape"), tank_id),
            )
        logger.info("Tank dimensions fetched for tank=%d (%s)", tank_id, product_label)
    except Exception as e:
        logger.error("Tank dimensions fetch failed for tank=%d (%s): %s", tank_id, product_label, e)
    finally:
        _dim_in_flight.discard(tank_id)


def maybe_fetch_reference_info(
    background_tasks: BackgroundTasks,
    entity_type: str,
    entity_name: str,
    display_name: str = "",
    water_type: str = "freshwater",
):
    """Queue a reference info fetch if no entry exists yet OR if a placeholder exists but was never fetched."""
    if not entity_name:
        return
    key = (entity_type, entity_name)
    if key in _in_flight:
        return  # task already running — don't stack another one
    with get_ref_db() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT id, fetched_at FROM reference_info WHERE entity_type=? AND entity_name=?",
            (entity_type, entity_name),
        ).fetchone())
        if existing and existing.get("fetched_at"):
            return  # Already fetched — skip
        if not existing:
            conn.execute(
                "INSERT OR IGNORE INTO reference_info (entity_type, entity_name, common_name) VALUES (?,?,?)",
                (entity_type, entity_name, display_name or None),
            )
    _in_flight.add(key)
    background_tasks.add_task(fetch_reference_info_bg, entity_type, entity_name, display_name, water_type)


def fetch_reference_info_bg(entity_type: str, entity_name: str, display_name: str = "", water_type: str = "freshwater"):
    """Sync background task: two Claude calls — text from training knowledge, image via web search."""
    key = (entity_type, entity_name)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        _in_flight.discard(key)
        return

    with get_ref_db() as conn:
        row = row_to_dict(conn.execute(
            "SELECT fetched_at FROM reference_info WHERE entity_type=? AND entity_name=?",
            (entity_type, entity_name),
        ).fetchone())
        if row and row.get("fetched_at"):
            _in_flight.discard(key)
            return  # Already fetched

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        name_label = display_name or entity_name
        wt_label = water_type or "freshwater"

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

        # Call 1: text from training knowledge — no web search, completes in ~2s
        scientific_name_field = (
            '\n  "scientific_name": "Genus species binomial, or null if unknown",'
            if entity_type in ("plant", "species") else ""
        )
        text_prompt = f"""You are an expert aquarium keeper. From your training knowledge (no search needed), provide information about this {type_labels.get(entity_type, 'aquarium item')} kept in a {wt_label} aquarium: "{name_label}"

Respond ONLY with valid JSON, no explanation or markdown fences:
{{{scientific_name_field}
  "description": "2-3 sentence description for an aquarium keeper",
  "care_notes": "{care_context.get(entity_type, 'general care requirements')}"
}}"""

        logger.info("Claude call: ref-text | %s/%s", entity_type, entity_name)
        t0 = time.monotonic()
        text_msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": text_prompt}],
            timeout=30.0,
        )
        logger.info("Claude done: ref-text | %s/%s | in=%d out=%d elapsed=%.1fs",
                    entity_type, entity_name,
                    text_msg.usage.input_tokens, text_msg.usage.output_tokens,
                    time.monotonic() - t0)
        raw_text = "".join(b.text for b in text_msg.content if hasattr(b, "text")).strip()
        logger.debug("Reference info text response for %s/%s: %r", entity_type, entity_name, raw_text[:500])
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```\s*$", "", raw_text)
        m = re.search(r"\{.*\}", raw_text, re.DOTALL)
        text_data = json.loads(m.group(0) if m else raw_text)

        # Call 2: image URL — check curated list first, then fall back to Claude.
        image_url = image_source = image_attribution = None
        curated_url = KNOWN_IMAGES.get((entity_type, _canonical(entity_name))) if USE_KNOWN_IMAGES else None
        if curated_url:
            image_url = curated_url
            image_source = "curated"
            logger.info("Reference info: using curated image for %s/%s: %s", entity_type, entity_name, image_url)
        else:
            try:
                sci_name = text_data.get("scientific_name") or ""
                search_name = sci_name if sci_name else name_label.split("/")[0].strip()

                # Phase 1: Wikimedia Commons API — curated, labeled images, high relevance
                candidates = []
                try:
                    wiki_search = f"{search_name} aquarium" if entity_type == "hardscape" else search_name
                    wiki_query = urllib.parse.quote(wiki_search)
                    wiki_api = (
                        f"https://commons.wikimedia.org/w/api.php?action=query"
                        f"&generator=search&gsrsearch={wiki_query}&gsrnamespace=6&gsrlimit=8"
                        f"&prop=imageinfo|categories&iiprop=url&cllimit=50&format=json"
                    )
                    wiki_req = urllib.request.Request(wiki_api, headers={"User-Agent": _WIKIMEDIA_USER_AGENT})
                    _wikimedia_throttle()
                    t1 = time.monotonic()
                    with urllib.request.urlopen(wiki_req, timeout=10) as wiki_resp:
                        wiki_data = json.loads(wiki_resp.read())
                    pages = wiki_data.get("query", {}).get("pages", {})
                    for page in pages.values():
                        title = page.get("title", "")
                        categories = " | ".join(c.get("title", "") for c in page.get("categories", []))
                        if _ILLUSTRATION_PATTERN.search(title) or _ILLUSTRATION_CATEGORY_PATTERN.search(categories):
                            continue
                        for ii in page.get("imageinfo", []):
                            url = ii.get("url", "")
                            if url and url.startswith("https://") and re.search(
                                r"\.(jpg|jpeg|png|webp)$", url, re.IGNORECASE
                            ) and not _ILLUSTRATION_PATTERN.search(url):
                                candidates.append((url, "commons.wikimedia.org"))
                    logger.info("Wikimedia Commons search | %s/%s | %d candidates | elapsed=%.1fs",
                                entity_type, entity_name, len(candidates), time.monotonic() - t1)
                except Exception as wiki_err:
                    logger.debug("Wikimedia Commons search failed for %s/%s: %s", entity_type, entity_name, wiki_err)

                def _verify(candidate_list):
                    for candidate_url, candidate_source in candidate_list:
                        try:
                            if "wikimedia.org" in candidate_url:
                                _wikimedia_throttle()
                            req = urllib.request.Request(candidate_url, method="HEAD",
                                                         headers={"User-Agent": _WIKIMEDIA_USER_AGENT})
                            with urllib.request.urlopen(req, timeout=8) as resp:
                                content_type = resp.headers.get("Content-Type", "")
                                if resp.status == 200 and content_type.startswith("image/"):
                                    logger.info("Reference info: image verified for %s/%s (%s): %s",
                                                entity_type, entity_name, content_type, candidate_url)
                                    return candidate_url, candidate_source
                                else:
                                    logger.debug("Reference info: skipping %s (HTTP %s, %s)",
                                                 candidate_url, resp.status, content_type)
                        except Exception as head_exc:
                            logger.debug("Reference info: skipping %s (HEAD failed: %s)", candidate_url, head_exc)
                    return None, None

                # Try to verify a Commons candidate first
                image_url, image_source = _verify(candidates)

                # Phase 2: DDG fallback if Commons returned nothing, or none of its
                # candidates verified (e.g. Wikimedia rate-limiting HEAD requests
                # after a burst of prior fetches in the same batch)
                if not image_url:
                    from ddgs import DDGS
                    if entity_type == "hardscape":
                        query = f"{search_name} aquarium"
                    else:
                        query = f"{search_name} {wt_label} aquarium"
                    logger.info("DDG image search | %s/%s | query: %r", entity_type, entity_name, query)
                    t1 = time.monotonic()
                    results = list(DDGS().images(query, max_results=15))
                    logger.info("DDG image search done | %s/%s | %d results | elapsed=%.1fs",
                                entity_type, entity_name, len(results), time.monotonic() - t1)
                    ddg_candidates = []
                    for r in results:
                        url = r.get("image", "")
                        if url and url.startswith("https://") and re.search(
                            r"\.(jpg|jpeg|png|webp)(\?|$)", url, re.IGNORECASE
                        ) and not _ILLUSTRATION_PATTERN.search(url) and not _ILLUSTRATION_PATTERN.search(r.get("title", "")):
                            wiki_thumb = re.match(
                                r"(https://upload\.wikimedia\.org/wikipedia/commons)/thumb(/[^/]+/[^/]+/[^/]+)/\d+px-.+",
                                url,
                            )
                            if wiki_thumb:
                                url = wiki_thumb.group(1) + wiki_thumb.group(2)
                            source = r.get("source") or (r.get("url", "").split("/")[2] if r.get("url") else None)
                            ddg_candidates.append((url, source))
                    candidates += ddg_candidates
                    image_url, image_source = _verify(ddg_candidates)

                if not image_url:
                    logger.warning("Reference info: no valid image found for %s/%s after %d candidates",
                                   entity_type, entity_name, len(candidates))

            except Exception as img_err:
                logger.warning(
                    "Reference info: image search failed for %s/%s (%s), saving text only",
                    entity_type, entity_name, img_err,
                )

        with get_ref_db() as conn:
            conn.execute(
                """INSERT INTO reference_info
                   (entity_type, entity_name, common_name, scientific_name, description, care_notes,
                    image_url, image_source, image_attribution, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(entity_type, entity_name) DO UPDATE SET
                     scientific_name = excluded.scientific_name,
                     description = excluded.description,
                     care_notes = excluded.care_notes,
                     image_url = COALESCE(excluded.image_url, reference_info.image_url),
                     image_source = CASE WHEN excluded.image_url IS NOT NULL THEN excluded.image_source ELSE reference_info.image_source END,
                     image_attribution = CASE WHEN excluded.image_url IS NOT NULL THEN excluded.image_attribution ELSE reference_info.image_attribution END,
                     fetched_at = excluded.fetched_at,
                     updated_at = datetime('now')""",
                (entity_type, entity_name, display_name or None,
                 text_data.get("scientific_name") or None,
                 text_data.get("description"), text_data.get("care_notes"),
                 image_url, image_source, image_attribution),
            )
        logger.info("Reference info fetched for %s/%s (image: %s)", entity_type, entity_name,
                    "yes" if image_url else "no")

    except Exception as e:
        logger.error("Reference info fetch failed for %s/%s: %s", entity_type, entity_name, e)
        import anthropic as _anthropic
        is_transient = isinstance(e, (_anthropic.APITimeoutError, _anthropic.APIConnectionError))
        if not is_transient:
            try:
                with get_ref_db() as conn:
                    conn.execute(
                        """INSERT INTO reference_info (entity_type, entity_name, common_name, fetched_at)
                           VALUES (?,?,?,datetime('now'))
                           ON CONFLICT(entity_type, entity_name) DO UPDATE SET
                             fetched_at = datetime('now'), updated_at = datetime('now')""",
                        (entity_type, entity_name, display_name or None),
                    )
            except Exception:
                pass
    finally:
        _in_flight.discard(key)


@router.get("/reference-info")
async def get_reference_info(entity_type: str, entity_name: str):
    with get_ref_db() as conn:
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
    water_type = body.get("water_type", "freshwater")

    if not entity_type or not entity_name:
        return JSONResponse({"error": "entity_type and entity_name required"}, status_code=400)

    _in_flight.discard((entity_type, entity_name))
    with get_ref_db() as conn:
        conn.execute(
            """INSERT INTO reference_info (entity_type, entity_name, common_name) VALUES (?,?,?)
               ON CONFLICT(entity_type, entity_name) DO UPDATE SET
                 fetched_at = NULL,
                 updated_at = datetime('now')""",
            (entity_type, entity_name, display_name or None),
        )

    _in_flight.add((entity_type, entity_name))
    background_tasks.add_task(fetch_reference_info_bg, entity_type, entity_name, display_name, water_type)
    return JSONResponse({"status": "refresh_queued"})


@router.post("/reference-info/set-image")
async def set_reference_image(request: Request):
    body = await request.json()
    entity_type = body.get("entity_type", "")
    entity_name = body.get("entity_name", "")
    image_url = (body.get("image_url") or "").strip()

    if not entity_type or not entity_name:
        return JSONResponse({"error": "entity_type and entity_name required"}, status_code=400)
    if not image_url:
        return JSONResponse({"error": "image_url required"}, status_code=400)

    try:
        if "wikimedia.org" in image_url:
            _wikimedia_throttle()
        req = urllib.request.Request(image_url, method="HEAD", headers={"User-Agent": _WIKIMEDIA_USER_AGENT})
        with urllib.request.urlopen(req, timeout=8) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if resp.status != 200 or not content_type.startswith("image/"):
                return JSONResponse(
                    {"error": f"URL did not return an image (HTTP {resp.status}, Content-Type: {content_type})"},
                    status_code=400,
                )
    except Exception as e:
        return JSONResponse({"error": f"Could not reach URL: {e}"}, status_code=400)

    with get_ref_db() as conn:
        conn.execute(
            """INSERT INTO reference_info (entity_type, entity_name, image_url, fetched_at)
               VALUES (?,?,?,datetime('now'))
               ON CONFLICT(entity_type, entity_name) DO UPDATE SET
                 image_url = excluded.image_url,
                 image_source = 'manual',
                 image_attribution = NULL,
                 updated_at = datetime('now')""",
            (entity_type, entity_name, image_url),
        )
    logger.info("Manual image URL set for %s/%s: %s", entity_type, entity_name, image_url)
    return JSONResponse({"status": "ok", "image_url": image_url})
