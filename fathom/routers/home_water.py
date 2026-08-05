"""Global home/source water test CRUD — not tank-scoped.

Most entries are flushed tap (WC source) with GH/KH only. Lab reports (PDF/CSV)
can be uploaded for LLM extraction of date + key metrics; user supplies sample
context (WC source vs unfiltered, hard/soft blend) because labs rarely encode that.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ai_config import CLAUDE_MODEL
from database import get_db, row_to_dict, rows_to_list

router = APIRouter(prefix="/home-water", tags=["home-water"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
logger = logging.getLogger(__name__)

# Prevent stacking concurrent summary regenerations (page load + save races).
_summary_in_flight = False

SAMPLE_POINTS = (
    # Fill-water sources — only these are injected into tank AI / WC analysis
    ("tap", "Tap (WC source)"),
    ("bottled_spring", "Bottled spring"),
    ("bottled_distilled", "Bottled distilled"),
    ("bottled", "Bottled (other)"),
    # Diagnostic / non-fill — home-water page + suitability only, never tank AI
    ("raw", "Unfiltered / raw well"),
    ("post_neutralizer", "Post-neutralizer"),
    ("post_softener", "Post-softener"),
    ("hose", "Hose"),
    ("other", "Other"),
)
SAMPLE_POINT_LABELS = dict(SAMPLE_POINTS)
VALID_SAMPLE_POINTS = set(SAMPLE_POINT_LABELS)

# Sources that can go into a tank water change (tap system or bottled)
FILL_WATER_SAMPLE_POINTS = frozenset({
    "tap", "bottled_spring", "bottled_distilled", "bottled",
})

WATER_BLENDS = (
    ("", "Not specified"),
    ("as_used", "As used for water changes"),
    ("hard", "Hard only (no softener)"),
    ("soft", "Soft only (fully softened)"),
    ("mixed", "Mixed hard + soft"),
    ("unknown", "Unknown"),
)
WATER_BLEND_LABELS = {k: v for k, v in WATER_BLENDS if k}
VALID_WATER_BLENDS = set(WATER_BLEND_LABELS)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
TEXT_EXTS = {".csv", ".txt", ".tsv", ".md", ".json"}
PDF_EXTS = {".pdf"}

LAB_EXTRACT_PROMPT = """You extract structured home/source water test data from a well or municipal lab report (or CSV export of such a report).

This is for an aquarium tracking app. The keeper has a private well and may soften/neutralize water before water changes. Your job is to pull the sample date(s) and the key chemistry values we store, converted into aquarium kit units where needed.

Return ONLY valid JSON (no markdown fences, no commentary):
{
  "readings": [
    {
      "timestamp": "YYYY-MM-DD HH:MM:SS",
      "ph": null,
      "gh": null,
      "kh": null,
      "ammonia": null,
      "nitrite": null,
      "nitrate": null,
      "tds": null,
      "temp": null,
      "sample_point_guess": "tap",
      "water_blend_guess": null,
      "notes": "",
      "flags": []
    }
  ],
  "report_meta": {
    "lab_name": null,
    "report_id": null,
    "sample_description": null
  }
}

FIELD RULES:
1. timestamp: Prefer the SAMPLE / COLLECTED date from the report (not "report printed" or "received" if those differ). Use 12:00:00 if only a date is given. Format YYYY-MM-DD HH:MM:SS.
2. Multiple sample points or sample dates on one report → one readings[] entry each.
3. pH: report pH as-is.
4. gh (general hardness): store as °dGH / dGH (German degrees), NOT mg/L CaCO3.
   - If report gives hardness as mg/L or ppm CaCO3 (total hardness), convert: dGH = CaCO3_mg_L / 17.86. Round to 1 decimal.
   - If already in dGH / °dH / German degrees, keep as-is.
   - Prefer total hardness (Ca+Mg). If only calcium hardness is given, use that and flag it.
5. kh (carbonate hardness / alkalinity): store as °dKH.
   - If alkalinity is mg/L or ppm as CaCO3, convert: dKH = alkalinity_mg_L_CaCO3 / 17.86. Round to 1 decimal.
   - If already in dKH, keep as-is.
6. nitrate: store as ppm of the nitrate ion (NO3-), matching aquarium API kits — NOT as nitrogen (NO3-N).
   - Synonyms that MUST be mapped to nitrate: "Nitrate", "Nitrate (as N)", "Nitrate as N", "NO3-N", "NO3 as N",
     "Nitrate-N", "Nitrate Nitrogen", SM 4500-NO3 methods.
   - If "as N" / NO3-N: multiply by 4.427 and round to 1 decimal; flag "nitrate converted from as-N (×4.427)".
   - If already as NO3 / NO3-: keep as-is.
7. ammonia: only if the report actually lists it. Synonyms: "Ammonia", "Ammonia (as N)", "Ammonia as N",
   "NH3-N", "NH3", "Ammonium", "Ammonium as N", "Total ammonia nitrogen", "TAN".
   - If as N: store the numeric mg/L as N value and flag "ammonia as N (not converted)" — aquarium kits
     are also usually read as total ammonia-N-ish; do not invent free NH3.
   - If the analyte is ABSENT from the report entirely → null (do not invent 0).
8. nitrite: CRITICAL — home well labs often list this even when ammonia is absent. You MUST extract it when present.
   - Synonyms that MUST be mapped to nitrite: "Nitrite", "Nitrite (as N)", "Nitrite as N", "NO2-N", "NO2 as N",
     "Nitrite-N", "Nitrite Nitrogen", SM 4500-NO2 methods.
   - If "as N" / NO2-N: multiply by 3.29 and round to 2–3 decimals (e.g. 0.10 as N → 0.329 as NO2-); flag conversion.
   - If already as NO2-: keep as-is.
   - Non-detects: if the report shows ND, "< MDL", "<0.10", "Not Detected", etc. for nitrite (or nitrate/ammonia),
     store 0 and flag "nitrite non-detect (treated as 0)" (same pattern for other analytes).
9. tds: total dissolved solids in ppm/mg/L if present.
10. temp: convert to °F if given in °C (F = C×9/5+32). Round to 1 decimal.
11. sample_point_guess: only if the report clearly labels the sample (raw well, untreated, after softener, kitchen tap, bottled spring/distilled, etc.). Otherwise null — the user will set this. Values if used: tap | bottled_spring | bottled_distilled | bottled | raw | post_neutralizer | post_softener | hose | other.
12. water_blend_guess: almost always null (labs don't know softener mix). Only set if the report text explicitly says hard/soft/mixed.
13. notes: short — lab method codes for key analytes (e.g. SM 4500-NO3 F), "as N converted…", original hardness units, anything useful. Include report sample ID if present.
14. flags: human-readable warnings (unit conversion applied, non-detect→0, value near MCL, missing hardness, ambiguous date, etc.).
15. Omit analytes not present on the report (use null). Do not invent values for missing analytes — but DO extract every nitrogen species that appears (nitrate AND nitrite are often both present; ammonia often is not).
16. is_lab_test is always true for these — do not include it in JSON; the app sets it.
17. Before finishing, re-scan the report for any line containing "nitrite" or "NO2" (case-insensitive). If found and nitrite is still null, you missed it — go back and fill it.

USER-SUPPLIED CONTEXT (authoritative for sample type / blend when the report is silent):
"""


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return float(s)


def _normalize_sample_point(value: Optional[str]) -> str:
    sp = (value or "tap").strip().lower()
    return sp if sp in VALID_SAMPLE_POINTS else "tap"


def _normalize_water_blend(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    vb = str(value).strip().lower()
    if not vb or vb in ("none", "null", "not_specified"):
        return None
    return vb if vb in VALID_WATER_BLENDS else None


def _parse_is_lab(value: Optional[str]) -> int:
    if value is None:
        return 0
    return 1 if str(value).strip().lower() in ("1", "true", "on", "yes") else 0


# Display order for composite baseline cards / AI (kit-first, then less-common).
HOME_WATER_PARAM_DEFS = (
    ("gh", "GH"),
    ("kh", "KH"),
    ("ph", "pH"),
    ("nitrate", "NO₃"),
    ("tds", "TDS"),
    ("ammonia", "NH₃"),
    ("nitrite", "NO₂"),
    ("temp", "Temp"),
)
HOME_WATER_PARAM_KEYS = tuple(k for k, _ in HOME_WATER_PARAM_DEFS)

# UI reminder: values whose as-of date is older than this are highlighted red
# ("consider a full kit test"). ~3 calendar months.
BASELINE_STALE_DAYS = 90


def _norm_sample_point_row(row: dict) -> str:
    sp = (row.get("sample_point") or "tap").strip().lower()
    return sp if sp else "tap"


def _row_sort_key(row: dict):
    """Newest first: timestamp DESC, then id DESC."""
    return (row.get("timestamp") or "", row.get("id") or 0)


def _parse_home_water_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse stored home-water timestamps (UTC 'YYYY-MM-DD[ HH:MM:SS]')."""
    if not ts:
        return None
    s = str(ts).strip().replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:n], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_baseline_ts_stale(
    ts: Optional[str],
    *,
    now: Optional[datetime] = None,
    stale_days: int = BASELINE_STALE_DAYS,
) -> bool:
    """True when the reading date is older than stale_days (default ~3 months)."""
    dt = _parse_home_water_ts(ts)
    if dt is None:
        return False
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    return (now_utc - dt).days > stale_days


def build_home_water_baseline(
    rows: list[dict],
    *,
    now: Optional[datetime] = None,
    stale_days: int = BASELINE_STALE_DAYS,
) -> Optional[dict]:
    """Last-known value per parameter across *rows* (partial logs OK).

    Walks newest → oldest and takes the first non-null for each field. Callers
    should pass a single sample stream (e.g. tap only) so GH from a WC-day
    reading is not mixed with a different sample type.

    Each param gets ``is_stale`` when its as-of date is older than ``stale_days``
    (~3 months) so the UI can remind the keeper to run a full kit test.

    Returns None if no numeric params exist. Structure::

        {
          "sample_point": "tap",
          "params": [
            {"key": "gh", "label": "GH", "value": 8.0,
             "timestamp": "2026-08-05 12:00:00", "source_id": 12,
             "is_stale": False},
            ...
          ],
          "by_key": {"gh": {...}, ...},
          "newest_timestamp": "2026-08-05 12:00:00",
          "is_composite": True,  # values come from more than one reading date
          "has_stale": True,     # any param older than stale_days
        }
    """
    if not rows:
        return None
    ordered = sorted(rows, key=_row_sort_key, reverse=True)
    found: dict[str, dict] = {}
    for r in ordered:
        for key, label in HOME_WATER_PARAM_DEFS:
            if key in found:
                continue
            val = r.get(key)
            if val is None:
                continue
            ts = r.get("timestamp")
            found[key] = {
                "key": key,
                "label": label,
                "value": val,
                "timestamp": ts,
                "source_id": r.get("id"),
                "is_stale": _is_baseline_ts_stale(ts, now=now, stale_days=stale_days),
            }
    if not found:
        return None
    params = [found[k] for k, _ in HOME_WATER_PARAM_DEFS if k in found]
    timestamps = {p["timestamp"] for p in params if p.get("timestamp")}
    newest_ts = ordered[0].get("timestamp")
    return {
        "sample_point": _norm_sample_point_row(ordered[0]),
        "params": params,
        "by_key": found,
        "newest_timestamp": newest_ts,
        "is_composite": len(timestamps) > 1,
        "has_stale": any(p.get("is_stale") for p in params),
    }


def load_wc_source_tests(conn, limit: int = 50) -> list[dict]:
    """Tap / default WC-source history, newest first (not raw/diagnostic/bottled)."""
    return rows_to_list(conn.execute(
        """SELECT * FROM home_water_tests
           WHERE sample_point IS NULL OR sample_point = '' OR sample_point = 'tap'
           ORDER BY timestamp DESC, id DESC LIMIT ?""",
        (limit,),
    ).fetchall())


def wc_source_baseline(conn, limit: int = 50) -> Optional[dict]:
    """Composite last-known-per-param for tap WC source (UI dashboard / home-water card)."""
    return build_home_water_baseline(load_wc_source_tests(conn, limit=limit))


def latest_wc_source_test(conn) -> Optional[dict]:
    """Latest reading suitable as water-change incoming water (tap / default)."""
    rows = load_wc_source_tests(conn, limit=1)
    return rows[0] if rows else None


def latest_raw_water_test(conn) -> Optional[dict]:
    """Latest unfiltered / raw well reading (horse / outdoor trough context)."""
    return row_to_dict(conn.execute(
        """SELECT * FROM home_water_tests
           WHERE sample_point = 'raw'
           ORDER BY timestamp DESC, id DESC LIMIT 1"""
    ).fetchone())


def _max_home_water_timestamp(conn) -> Optional[str]:
    """Overall newest reading (any sample point). Used rarely; prefer tap/raw helpers."""
    row = conn.execute("SELECT MAX(timestamp) FROM home_water_tests").fetchone()
    return row[0] if row and row[0] else None


def get_home_water_summary(conn) -> Optional[dict]:
    return row_to_dict(conn.execute(
        "SELECT * FROM home_water_summary WHERE id = 1"
    ).fetchone())


def clear_home_water_summary(conn) -> None:
    """Drop saved suitability text so the UI never shows notes for a stale basis."""
    conn.execute("DELETE FROM home_water_summary WHERE id = 1")


def _basis_timestamps(conn) -> tuple[Optional[str], Optional[str]]:
    """Return (latest_tap_ts, latest_raw_ts) for summary basis tracking."""
    tap = latest_wc_source_test(conn)
    raw = latest_raw_water_test(conn)
    return (
        (tap or {}).get("timestamp"),
        (raw or {}).get("timestamp"),
    )


def home_water_summary_is_stale(conn) -> bool:
    """True when WC-source and/or raw basis no longer match the saved summary.

    based_on_timestamp = latest *tap/WC-source* reading (not global max — raw may be newer).
    based_on_raw_timestamp = latest *raw* reading for the horse section.
    """
    tap_ts, raw_ts = _basis_timestamps(conn)
    if not tap_ts and not raw_ts:
        return False
    summary = get_home_water_summary(conn)
    if not summary or not (summary.get("summary_text") or "").strip():
        return True
    based_tap = (summary.get("based_on_timestamp") or "").strip() or None
    based_raw = (summary.get("based_on_raw_timestamp") or "").strip() or None
    # Mismatch (wrong basis, newer, or missing) on either stream
    if (tap_ts or None) != based_tap:
        return True
    if (raw_ts or None) != based_raw:
        return True
    return False


def should_refresh_home_water_summary_after_write(
    conn,
    *,
    pre_max_ts: Optional[str] = None,
    written_ts: Optional[str] = None,
    deleted: bool = False,
) -> bool:
    """Decide whether a write warrants regenerating the saved AI summary.

    Refresh when the latest WC-source (tap) or latest raw basis changes, or when
    the write edits the current latest tap/raw row. Older backfills alone do not.
    pre_max_ts is ignored (kept for call-site compatibility).
    """
    tap_ts, raw_ts = _basis_timestamps(conn)
    if not tap_ts and not raw_ts:
        return False
    if home_water_summary_is_stale(conn):
        return True
    # Content change of the current basis rows (same timestamps, new values)
    if written_ts and written_ts in (tap_ts, raw_ts):
        return True
    return False


def build_home_water_summary_prompt(
    tanks_ctx: list[dict],
    latest_tap: Optional[dict],
    recent_tap: list[dict],
    latest_raw: Optional[dict],
) -> str:
    tank_blocks = []
    for t in tanks_ctx:
        inh = t.get("inhabitants") or "  (none listed)"
        notes = (t.get("notes") or "").strip() or "(no tank notes)"
        tank_blocks.append(
            f"- {t['name']} ({t.get('water_type') or '?'} water, "
            f"{t.get('volume_gallons') or '?'} gal)\n"
            f"  Notes: {notes}\n"
            f"  Inhabitants:\n{inh}"
        )
    tanks_text = "\n".join(tank_blocks) if tank_blocks else "  (no tanks)"

    from routers.ai_analysis import _fmt_home_water, _fmt_home_water_baseline
    tap_block = _fmt_home_water([latest_tap] if latest_tap else [])
    # History in the prompt stays short; baseline walks the fuller recent_tap list.
    recent_block = _fmt_home_water((recent_tap or [])[:5]) if recent_tap else "  (none)"
    raw_block = _fmt_home_water([latest_raw] if latest_raw else [])
    # Composite last-known-per-param (GH-only WC days still keep prior KH/nitrate/etc.)
    baseline_rows = list(recent_tap or [])
    if latest_tap and not baseline_rows:
        baseline_rows = [latest_tap]
    tap_baseline = build_home_water_baseline(baseline_rows)
    baseline_block = _fmt_home_water_baseline(tap_baseline)

    return f"""You write a short suitability assessment of household well/source water for an aquarium keeper.

Do NOT restate numeric parameter values (no "GH is 7", no "nitrate 39 ppm"). The UI already shows numbers. Speak qualitatively: soft/hard, buffered or not, moderately elevated nitrate, non-detect nitrite, etc., and what that means.

STANDING HOUSEHOLD CONTEXT (always apply — do not hedge for groups that are not present):
- No infants and no pregnant people in the household. Do NOT mention infants, pregnancy, formula, or pediatric risk.
- Horses are all healthy adults (3+ years). No foaling, no pregnant mares, no foals/youngstock. Do NOT mention mares, foals, breeding, or young-horse sensitivity.
- Softener/neutralizer do NOT remove nitrate; source nitrate is a chronic well baseline, not a treatment failure.

NITRATE / API KIT CONTEXT:
- API nitrate color charts around the mid band (~40–50 ppm as NO3-) are extremely subjective; exact chart steps are not reliable precision.
- When kit color is consistent test-to-test in that band (and labs land in the same ballpark), treat it as a stable moderate baseline — not a crisis, not a number to over-interpret, and not something to re-flag every time.
- Only call out nitrate if it clearly trends up/down across readings or becomes a real floor issue for tank water changes (WC cannot dilute below source).
- Partial kit logs are normal (GH-only on water-change days). Use the CURRENT WC-SOURCE BASELINE (last known per parameter) when the latest single reading is incomplete.

ACTIVE TANKS (judge WC-source water against each tank's notes/targets/stock):
{tanks_text}

CURRENT WC-SOURCE BASELINE (last known value per parameter across recent tap logs; as-of dates may differ):
{baseline_block}

LATEST WC-SOURCE / TAP READING (single newest row — may be GH-only):
{tap_block}

RECENT WC-SOURCE HISTORY (newest first; for trend only — do not dump history):
{recent_block}

LATEST RAW / UNFILTERED WELL READING (bypass softener/neutralizer — NOT the normal WC fill unless noted):
{raw_block}

Write plain text only (no markdown headers, no bullets with dashes if you can avoid them; short paragraphs are fine).

Output format — plain text with exactly these two section markers (no JSON, no markdown).
Write RAW_OUTDOOR first so it is never cut off, then WC_SOURCE. Keep the whole answer tight (about 150–280 words total).

=== RAW_OUTDOOR ===
1 short paragraph (2–4 sentences) on the latest RAW well sample as drinking water for adult horses only (barn/pasture troughs). Routine suitability for healthy adult horses; nitrite only if relevant. If no raw sample exists, say so in one sentence. Do not restate numbers. Not aquarium advice. No mare/foal language.

=== WC_SOURCE ===
Structure this section for easy scanning (whitespace matters — the UI preserves blank lines):

1) One short block per named tank. Start each block with the tank name on its own line (e.g. "Shrimp Tank:"), then 1–3 sentences on suitability for that tank only (targets, stock, nitrate as stable source floor — not a kit-precision debate).
2) Put a blank line between each tank block so they are visually separated.
3) After all tanks, one blank line, then a short "Drinking water:" block for adult household use only (no infant/pregnancy caveats).

Do not restate numbers. Do not merge all tanks into one dense paragraph.

Rules:
- Prefer tank notes accepted baselines over generic species norms.
- WC-source and raw may differ a lot (softener/neutralizer); never conflate them.
- Outdoor section is adult-horse-specific (not poultry, dogs, foals, or generic livestock).
- Never invent infant/pregnancy/mare/foal warnings for this household.
- If data is thin, say what's uncertain rather than inventing.
- Plain text only — no JSON, no code fences, no bullet markdown.
- Stay concise; finish both sections completely.
- Always use blank lines between tank blocks in WC_SOURCE.
"""


async def run_home_water_summary(force: bool = False):
    """Generate and persist the home-water suitability summary (singleton row)."""
    global _summary_in_flight
    if _summary_in_flight:
        logger.info("Home water summary already in flight — skipping")
        return
    _summary_in_flight = True
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set, skipping home water summary")
            return

        with get_db() as conn:
            tap_ts, raw_ts = _basis_timestamps(conn)
            if not tap_ts and not raw_ts:
                conn.execute("DELETE FROM home_water_summary WHERE id = 1")
                logger.info("No home water tests — cleared summary")
                return

            if not force and not home_water_summary_is_stale(conn):
                logger.info(
                    "Home water summary already current | tap=%s raw=%s",
                    tap_ts, raw_ts,
                )
                return

            latest_tap = latest_wc_source_test(conn)
            latest_raw = latest_raw_water_test(conn)
            # Enough history for composite baseline when recent rows are GH-only.
            recent_tap = load_wc_source_tests(conn, limit=24)

            tanks = rows_to_list(conn.execute(
                """SELECT id, name, water_type, volume_gallons, notes, status
                   FROM tanks
                   WHERE COALESCE(status, 'active') = 'active'
                   ORDER BY name"""
            ).fetchall())

            from routers.ai_analysis import _fmt_inhabitants
            tanks_ctx = []
            for t in tanks:
                inhs = rows_to_list(conn.execute(
                    """SELECT common_name, species, count FROM inhabitants
                       WHERE tank_id = ? AND (count IS NULL OR count > 0)
                       ORDER BY common_name, species""",
                    (t["id"],),
                ).fetchall())
                tanks_ctx.append({
                    **t,
                    "inhabitants": _fmt_inhabitants(inhs),
                })

            prompt = build_home_water_summary_prompt(
                tanks_ctx, latest_tap, recent_tap, latest_raw,
            )

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        logger.info(
            "Claude call: home_water_summary | based_on_tap=%s based_on_raw=%s",
            tap_ts, raw_ts,
        )
        t0 = time.monotonic()
        msg = await asyncio.to_thread(
            client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
            timeout=90.0,
        )
        logger.info(
            "Claude done: home_water_summary | in=%d out=%d elapsed=%.1fs stop=%s",
            msg.usage.input_tokens, msg.usage.output_tokens, time.monotonic() - t0,
            getattr(msg, "stop_reason", None),
        )
        raw_text = ""
        for block in msg.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                raw_text += getattr(block, "text", "") or ""
            elif isinstance(block, dict) and block.get("type") == "text":
                raw_text += block.get("text") or ""
            elif isinstance(block, str):
                raw_text += block
        if not raw_text.strip() and msg.content:
            # Last resort: string-coerce first block
            raw_text = str(getattr(msg.content[0], "text", None) or msg.content[0] or "")

        summary_text, raw_outdoor = _parse_summary_sections(raw_text)
        # Allow raw-only households (no tap yet) if horse section has text
        if not summary_text and not raw_outdoor:
            logger.error("Home water summary empty after parse | raw=%r", raw_text[:500])
            return
        if not summary_text:
            summary_text = "(No WC-source / tap reading on file yet.)"

        with get_db() as conn:
            # Re-check basis in case a newer tap/raw landed mid-call
            based_tap, based_raw = _basis_timestamps(conn)
            conn.execute(
                """INSERT INTO home_water_summary
                   (id, summary_text, raw_outdoor_text, based_on_timestamp,
                    based_on_raw_timestamp, generated_at, updated_at)
                   VALUES (1, ?, ?, ?, ?, datetime('now'), datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                     summary_text = excluded.summary_text,
                     raw_outdoor_text = excluded.raw_outdoor_text,
                     based_on_timestamp = excluded.based_on_timestamp,
                     based_on_raw_timestamp = excluded.based_on_raw_timestamp,
                     generated_at = excluded.generated_at,
                     updated_at = datetime('now')""",
                (summary_text, raw_outdoor, based_tap, based_raw),
            )
        logger.info(
            "Home water summary saved | based_on_tap=%s based_on_raw=%s",
            based_tap, based_raw,
        )
    except Exception as e:
        logger.error("Home water summary failed: %s", e)
    finally:
        _summary_in_flight = False


def _queue_summary_if_needed(
    background_tasks: BackgroundTasks,
    conn,
    *,
    pre_max_ts: Optional[str],
    written_ts: Optional[str] = None,
    deleted: bool = False,
    force: bool = False,
) -> bool:
    """Queue AI regen if needed. Clears old summary first so UI never shows stale notes.

    Returns True if a refresh was queued (caller can redirect to a page without old text).
    """
    if force or should_refresh_home_water_summary_after_write(
        conn, pre_max_ts=pre_max_ts, written_ts=written_ts, deleted=deleted,
    ):
        clear_home_water_summary(conn)
        background_tasks.add_task(run_home_water_summary, True)
        logger.info("Cleared stale home water summary and queued refresh")
        return True
    return False


def _insert_home_water(
    conn,
    *,
    ts: Optional[str],
    ph, gh, kh, ammonia, nitrite, nitrate, tds, temp,
    sample_point: str,
    water_blend: Optional[str],
    is_lab: int,
    notes: Optional[str],
) -> int:
    if ts:
        cur = conn.execute(
            """INSERT INTO home_water_tests
               (timestamp, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp,
                sample_point, water_blend, is_lab_test, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, ph, gh, kh, ammonia, nitrite, nitrate, tds, temp,
             sample_point, water_blend, is_lab, notes),
        )
    else:
        cur = conn.execute(
            """INSERT INTO home_water_tests
               (ph, gh, kh, ammonia, nitrite, nitrate, tds, temp,
                sample_point, water_blend, is_lab_test, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ph, gh, kh, ammonia, nitrite, nitrate, tds, temp,
             sample_point, water_blend, is_lab, notes),
        )
    return cur.lastrowid


def _parse_summary_sections(raw: str) -> tuple[str, Optional[str]]:
    """Split model output into WC_SOURCE and RAW_OUTDOOR sections."""
    text = (raw or "").strip()
    if not text:
        return "", None
    # Strip accidental fences
    text = re.sub(r"^```(?:json|text)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    wc_marker = "=== WC_SOURCE ==="
    raw_marker = "=== RAW_OUTDOOR ==="

    # JSON fallback (older prompt / model compliance)
    if text.lstrip().startswith("{"):
        try:
            parsed = _parse_json_object(text)
            return (
                (parsed.get("summary_text") or "").strip(),
                ((parsed.get("raw_outdoor_text") or "").strip() or None),
            )
        except (ValueError, json.JSONDecodeError):
            pass

    def _section_after(marker: str) -> str:
        if marker not in text:
            return ""
        after = text.split(marker, 1)[1]
        # Stop at the next known section marker if present
        for other in (wc_marker, raw_marker):
            if other != marker and other in after:
                after = after.split(other, 1)[0]
        return after.strip()

    if wc_marker in text or raw_marker in text:
        wc_part = _section_after(wc_marker)
        raw_part = _section_after(raw_marker)
        return wc_part, (raw_part or None)

    # Unmarked plain text: treat entire body as WC summary
    return text, None


def _parse_json_object(raw: str) -> dict:
    text = re.sub(r"```json\s*", "", raw or "")
    text = re.sub(r"```\s*", "", text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object in model response")
    parsed = json.loads(match.group())
    if not isinstance(parsed, dict):
        raise ValueError("Expected JSON object")
    return parsed


def _coerce_reading(raw: dict, *, default_sp: str, default_blend: Optional[str]) -> dict:
    """Normalize one extracted reading for the review UI / save path."""
    def f(key):
        v = raw.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    sp = _normalize_sample_point(raw.get("sample_point") or raw.get("sample_point_guess") or default_sp)
    blend = _normalize_water_blend(
        raw.get("water_blend") if raw.get("water_blend") is not None
        else (raw.get("water_blend_guess") if raw.get("water_blend_guess") is not None else default_blend)
    )
    notes = (raw.get("notes") or "").strip() or None
    flags = raw.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    ts = (raw.get("timestamp") or "").strip() or None
    if ts and len(ts) == 10:
        ts = f"{ts} 12:00:00"
    return {
        "timestamp": ts,
        "ph": f("ph"),
        "gh": f("gh"),
        "kh": f("kh"),
        "ammonia": f("ammonia"),
        "nitrite": f("nitrite"),
        "nitrate": f("nitrate"),
        "tds": f("tds"),
        "temp": f("temp"),
        "sample_point": sp,
        "water_blend": blend,
        "is_lab_test": 1,
        "notes": notes,
        "flags": [str(x) for x in flags if x],
    }


@router.get("", response_class=HTMLResponse)
async def list_home_water(request: Request, background_tasks: BackgroundTasks):
    with get_db() as conn:
        tests = rows_to_list(conn.execute(
            "SELECT * FROM home_water_tests ORDER BY timestamp DESC, id DESC LIMIT 100"
        ).fetchall())
        latest = row_to_dict(conn.execute(
            "SELECT * FROM home_water_tests ORDER BY timestamp DESC, id DESC LIMIT 1"
        ).fetchone())
        latest_tap = latest_wc_source_test(conn)
        tap_baseline = wc_source_baseline(conn, limit=50)
        latest_raw = latest_raw_water_test(conn)
        summary_refreshing = False
        summary = get_home_water_summary(conn)
        # Stale or missing: wipe old notes so we never show them next to a new basis,
        # then regenerate in the background.
        if tests and home_water_summary_is_stale(conn):
            clear_home_water_summary(conn)
            background_tasks.add_task(run_home_water_summary, True)
            summary = None
            summary_refreshing = True
            logger.info("Home water page: cleared stale summary and queued refresh")
    return templates.TemplateResponse(request, "home_water/list.html", {
        "tests": tests,
        "latest": latest,
        "latest_tap": latest_tap,
        "tap_baseline": tap_baseline,
        "latest_raw": latest_raw,
        "summary": summary,
        "summary_refreshing": summary_refreshing,
        "sample_points": SAMPLE_POINTS,
        "sample_point_labels": SAMPLE_POINT_LABELS,
        "water_blends": WATER_BLENDS,
        "water_blend_labels": WATER_BLEND_LABELS,
        "active": "home_water",
    })


@router.post("")
async def add_home_water(
    request: Request,
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
    sample_point: Optional[str] = Form("tap"),
    water_blend: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    ts = timestamp.strip() if timestamp and timestamp.strip() else None
    ph_v, gh_v, kh_v = _parse_float(ph), _parse_float(gh), _parse_float(kh)
    ammonia_v, nitrite_v = _parse_float(ammonia), _parse_float(nitrite)
    nitrate_v, tds_v, temp_v = _parse_float(nitrate), _parse_float(tds), _parse_float(temp)
    sp = _normalize_sample_point(sample_point)
    blend = _normalize_water_blend(water_blend)
    notes_v = notes.strip() if notes and notes.strip() else None
    # Manual entry is always a kit/home reading. Lab flag is set only via PDF/CSV import.

    with get_db() as conn:
        pre_max = _max_home_water_timestamp(conn)
        result_id = _insert_home_water(
            conn,
            ts=ts,
            ph=ph_v, gh=gh_v, kh=kh_v, ammonia=ammonia_v, nitrite=nitrite_v,
            nitrate=nitrate_v, tds=tds_v, temp=temp_v,
            sample_point=sp, water_blend=blend, is_lab=0, notes=notes_v,
        )
        written = row_to_dict(conn.execute(
            "SELECT timestamp FROM home_water_tests WHERE id = ?", (result_id,),
        ).fetchone())
        written_ts = (written or {}).get("timestamp")
        _queue_summary_if_needed(
            background_tasks, conn, pre_max_ts=pre_max, written_ts=written_ts,
        )

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": result_id, "status": "created"}, status_code=201)
    return RedirectResponse(url="/home-water", status_code=303)


@router.post("/{test_id}/update")
async def update_home_water(
    request: Request,
    background_tasks: BackgroundTasks,
    test_id: int,
    timestamp: Optional[str] = Form(None),
    ph: Optional[str] = Form(None),
    gh: Optional[str] = Form(None),
    kh: Optional[str] = Form(None),
    ammonia: Optional[str] = Form(None),
    nitrite: Optional[str] = Form(None),
    nitrate: Optional[str] = Form(None),
    tds: Optional[str] = Form(None),
    temp: Optional[str] = Form(None),
    sample_point: Optional[str] = Form("tap"),
    water_blend: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    ts = timestamp.strip() if timestamp and timestamp.strip() else None
    ph_v, gh_v, kh_v = _parse_float(ph), _parse_float(gh), _parse_float(kh)
    ammonia_v, nitrite_v = _parse_float(ammonia), _parse_float(nitrite)
    nitrate_v, tds_v, temp_v = _parse_float(nitrate), _parse_float(tds), _parse_float(temp)
    sp = _normalize_sample_point(sample_point)
    blend = _normalize_water_blend(water_blend)
    notes_v = notes.strip() if notes and notes.strip() else None
    # Preserve is_lab_test from the existing row (set only by lab PDF/CSV import).

    with get_db() as conn:
        pre_max = _max_home_water_timestamp(conn)
        row = row_to_dict(conn.execute(
            "SELECT id, is_lab_test FROM home_water_tests WHERE id = ?", (test_id,),
        ).fetchone())
        if not row:
            raise HTTPException(status_code=404, detail="Home water test not found")
        lab = 1 if row.get("is_lab_test") else 0
        if ts:
            conn.execute(
                """UPDATE home_water_tests SET
                   timestamp=?, ph=?, gh=?, kh=?, ammonia=?, nitrite=?, nitrate=?,
                   tds=?, temp=?, sample_point=?, water_blend=?, is_lab_test=?, notes=?,
                   updated_at=datetime('now')
                   WHERE id=?""",
                (ts, ph_v, gh_v, kh_v, ammonia_v, nitrite_v, nitrate_v,
                 tds_v, temp_v, sp, blend, lab, notes_v, test_id),
            )
        else:
            conn.execute(
                """UPDATE home_water_tests SET
                   ph=?, gh=?, kh=?, ammonia=?, nitrite=?, nitrate=?,
                   tds=?, temp=?, sample_point=?, water_blend=?, is_lab_test=?, notes=?,
                   updated_at=datetime('now')
                   WHERE id=?""",
                (ph_v, gh_v, kh_v, ammonia_v, nitrite_v, nitrate_v,
                 tds_v, temp_v, sp, blend, lab, notes_v, test_id),
            )
        written = row_to_dict(conn.execute(
            "SELECT timestamp FROM home_water_tests WHERE id = ?", (test_id,),
        ).fetchone())
        written_ts = (written or {}).get("timestamp")
        _queue_summary_if_needed(
            background_tasks, conn, pre_max_ts=pre_max, written_ts=written_ts,
        )

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": test_id, "status": "updated"})
    return RedirectResponse(url="/home-water", status_code=303)


@router.post("/{test_id}/delete")
async def delete_home_water(request: Request, background_tasks: BackgroundTasks, test_id: int):
    with get_db() as conn:
        pre_max = _max_home_water_timestamp(conn)
        row = row_to_dict(conn.execute(
            "SELECT id, timestamp FROM home_water_tests WHERE id = ?", (test_id,),
        ).fetchone())
        if not row:
            raise HTTPException(status_code=404, detail="Home water test not found")
        conn.execute("DELETE FROM home_water_tests WHERE id = ?", (test_id,))
        _queue_summary_if_needed(
            background_tasks, conn, pre_max_ts=pre_max, deleted=True,
        )

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"status": "deleted"})
    return RedirectResponse(url="/home-water", status_code=303)


@router.post("/extract")
async def extract_lab_report(
    file: UploadFile = File(...),
    sample_point: Optional[str] = Form("tap"),
    water_blend: Optional[str] = Form(None),
    user_notes: Optional[str] = Form(None),
):
    """LLM-extract home water readings from a lab PDF or CSV/text file.

    User-supplied sample_point and water_blend are the defaults applied when the
    report does not state sample type / softener mix (usual for lab PDFs).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 20 MB)")

    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    default_sp = _normalize_sample_point(sample_point)
    default_blend = _normalize_water_blend(water_blend)
    extra_notes = (user_notes or "").strip()

    context_block = (
        f"- Default sample_point (user): {default_sp} "
        f"({SAMPLE_POINT_LABELS.get(default_sp, default_sp)})\n"
        f"- Default water_blend (user): {default_blend or 'not specified'} "
        f"({WATER_BLEND_LABELS.get(default_blend or '', 'not specified')})\n"
        f"- User notes: {extra_notes or '(none)'}\n"
        f"- Filename: {filename}\n"
        "Apply the user's sample_point and water_blend to every reading unless the "
        "report clearly describes a different sample stream for that row.\n"
    )
    prompt = LAB_EXTRACT_PROMPT + context_block

    try:
        import anthropic
    except ImportError as e:
        raise HTTPException(status_code=500, detail="anthropic SDK not installed") from e

    client = anthropic.Anthropic(api_key=api_key)

    if ext in PDF_EXTS or (file.content_type or "").startswith("application/pdf"):
        b64 = base64.standard_b64encode(raw).decode("ascii")
        content_blocks: list[Any] = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            },
            {"type": "text", "text": prompt},
        ]
    elif ext in TEXT_EXTS or (file.content_type or "").startswith("text/"):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        if len(text) > 100_000:
            text = text[:100_000] + "\n...[truncated]"
        content_blocks = [{"type": "text", "text": prompt + "\n\n--- REPORT TEXT ---\n" + text}]
    else:
        # Sniff PDF magic
        if raw[:4] == b"%PDF":
            b64 = base64.standard_b64encode(raw).decode("ascii")
            content_blocks = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64,
                    },
                },
                {"type": "text", "text": prompt},
            ]
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=400,
                    detail="Unsupported file type. Upload a PDF, CSV, or plain text lab report.",
                )
            content_blocks = [{"type": "text", "text": prompt + "\n\n--- REPORT TEXT ---\n" + text}]

    logger.info("Claude call: home_water_lab_extract | file=%s bytes=%d", filename, len(raw))
    t0 = time.monotonic()
    try:
        msg = await asyncio.to_thread(
            client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": content_blocks}],
            timeout=120.0,
        )
    except Exception as e:
        logger.error("Lab extract failed: %s", e)
        raise HTTPException(status_code=502, detail=f"AI extraction failed: {e}") from e

    logger.info(
        "Claude done: home_water_lab_extract | in=%d out=%d elapsed=%.1fs",
        msg.usage.input_tokens, msg.usage.output_tokens, time.monotonic() - t0,
    )

    # Collect text blocks from the response
    text_out = ""
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            text_out += block.text
    if not text_out.strip():
        raise HTTPException(status_code=502, detail="AI returned empty extraction")

    try:
        parsed = _parse_json_object(text_out)
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Lab extract JSON parse failed: %s | raw=%s", e, text_out[:500])
        raise HTTPException(status_code=502, detail="Could not parse AI extraction as JSON") from e

    raw_readings = parsed.get("readings") or []
    if not isinstance(raw_readings, list) or not raw_readings:
        raise HTTPException(status_code=422, detail="No readings found in the lab report")

    readings = [
        _coerce_reading(r if isinstance(r, dict) else {}, default_sp=default_sp, default_blend=default_blend)
        for r in raw_readings
    ]

    # Attach user notes / filename into each reading notes if useful
    meta = parsed.get("report_meta") if isinstance(parsed.get("report_meta"), dict) else {}
    meta_bits = []
    if meta.get("lab_name"):
        meta_bits.append(str(meta["lab_name"]))
    if meta.get("report_id"):
        meta_bits.append(f"report {meta['report_id']}")
    if filename:
        meta_bits.append(f"file: {filename}")
    meta_prefix = "; ".join(meta_bits)

    for r in readings:
        parts = [p for p in (meta_prefix, r.get("notes"), extra_notes) if p]
        r["notes"] = " | ".join(parts) if parts else None
        r["is_lab_test"] = 1

    return JSONResponse({
        "readings": readings,
        "report_meta": meta,
        "filename": filename,
    })


class BulkReading(BaseModel):
    timestamp: Optional[str] = None
    ph: Optional[float] = None
    gh: Optional[float] = None
    kh: Optional[float] = None
    ammonia: Optional[float] = None
    nitrite: Optional[float] = None
    nitrate: Optional[float] = None
    tds: Optional[float] = None
    temp: Optional[float] = None
    sample_point: Optional[str] = "tap"
    water_blend: Optional[str] = None
    is_lab_test: Optional[int] = 1
    notes: Optional[str] = None


class BulkSaveBody(BaseModel):
    readings: list[BulkReading] = Field(default_factory=list)


@router.post("/bulk")
async def bulk_save_home_water(body: BulkSaveBody, background_tasks: BackgroundTasks):
    """Save one or more reviewed lab-extracted readings."""
    if not body.readings:
        raise HTTPException(status_code=400, detail="No readings to save")

    ids = []
    with get_db() as conn:
        pre_max = _max_home_water_timestamp(conn)
        newest_written = None
        for r in body.readings:
            ts = (r.timestamp or "").strip() or None
            if ts and len(ts) == 10:
                ts = f"{ts} 12:00:00"
            sp = _normalize_sample_point(r.sample_point)
            blend = _normalize_water_blend(r.water_blend)
            lab = 1 if (r.is_lab_test is None or r.is_lab_test) else 0
            notes = (r.notes or "").strip() or None
            rid = _insert_home_water(
                conn,
                ts=ts,
                ph=r.ph, gh=r.gh, kh=r.kh, ammonia=r.ammonia, nitrite=r.nitrite,
                nitrate=r.nitrate, tds=r.tds, temp=r.temp,
                sample_point=sp, water_blend=blend, is_lab=lab, notes=notes,
            )
            ids.append(rid)
            written = row_to_dict(conn.execute(
                "SELECT timestamp FROM home_water_tests WHERE id = ?", (rid,),
            ).fetchone())
            wts = (written or {}).get("timestamp")
            if wts and (newest_written is None or wts > newest_written):
                newest_written = wts
        _queue_summary_if_needed(
            background_tasks, conn, pre_max_ts=pre_max, written_ts=newest_written,
        )

    return JSONResponse({"ids": ids, "status": "created", "count": len(ids)}, status_code=201)
