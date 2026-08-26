"""Live-food cultures — not tanks.

One purpose per culture (Daphnia *or* green water). Green water isn't fed;
harvest goes to a destination tank, culture, or bin. Vessels are the bins
of that culture. Due tasks use culture_schedule.
"""
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from database import get_db, row_to_dict, rows_to_list
from routers.schedules import compute_next_due

router = APIRouter(prefix="/cultures", tags=["cultures"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

ROLES = ("daphnia", "green_water", "other")
ROLE_LABELS = {
    "daphnia": "Daphnia",
    "green_water": "Green water",
    "other": "Other",
}
VESSEL_STATUSES = ("active", "crashed", "archived")
CULTURE_STATUSES = ("active", "archived")
CULTURE_KINDS = ("daphnia", "green_water", "other")
CULTURE_KIND_LABELS = {
    "daphnia": "Daphnia",
    "green_water": "Green water",
    "other": "Other",
}
LOG_KINDS = ("feed", "look", "harvest", "seed", "crash", "temp", "other")
KIND_LABELS = {
    "feed": "Feed",
    "look": "Look",
    "harvest": "Harvest",
    "seed": "Seed",
    "crash": "Crash",
    "temp": "Temp",
    "other": "Other",
}
FOODS = ("spirulina", "green_water", "yeast", "none")
FOOD_LABELS = {
    "spirulina": "Spirulina",
    "green_water": "Green water",
    "yeast": "Yeast",
    "none": "None",
}
TINTS = ("clear", "faint", "green", "soup", "milky")
TINT_LABELS = {
    "clear": "Clear",
    "faint": "Faint tint",
    "green": "Green",
    "soup": "Soup",
    "milky": "Milky",
}
DENSITIES = ("thin", "ok", "dense", "crash")
DENSITY_LABELS = {
    "thin": "Thin",
    "ok": "OK",
    "dense": "Dense",
    "crash": "Crash",
}
GUTS = ("empty_pink", "darker", "mixed")
GUTS_LABELS = {
    "empty_pink": "Empty / pink",
    "darker": "Darker",
    "mixed": "Mixed",
}
SCHEDULE_CATEGORIES = ("feeding", "look", "maintenance")
CATEGORY_LABELS = {
    "feeding": "Feeding",
    "look": "Look",
    "maintenance": "Maintenance",
}
TRACKING_MODES = ("logged", "reference_only")
HARVEST_STATUSES = ("not_ready", "ready")
HARVEST_STATUS_LABELS = {
    "not_ready": "Don't harvest yet",
    "ready": "OK to harvest",
}
# Phrases that belong on the harvest badge, not in Next — including
# next_action values that just restated harvest status.
_HARVEST_NEXT_PHRASES = frozenset({
    "dont harvest",
    "dont harvest yet",
    "do not harvest",
    "do not harvest yet",
    "ok to harvest",
    "okay to harvest",
    "not ready",
    "not_ready",
    "ready",
} | {v.lower().replace("'", "") for v in HARVEST_STATUS_LABELS.values()})
TEMP_KINDS = ("water", "air")
TEMP_KIND_LABELS = {"water": "Water", "air": "Air (bench)"}

_KIND_FROM_CATEGORY = {
    "feeding": "feed",
    "look": "look",
    "maintenance": "other",
}


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _blank(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _int_or_none(value: Optional[str]) -> Optional[int]:
    text = _blank(value)
    if text is None:
        return None
    return int(text)


def _float_or_none(value: Optional[str]) -> Optional[float]:
    text = _blank(value)
    if text is None:
        return None
    return float(text)


def _choice(value: Optional[str], allowed) -> Optional[str]:
    text = _blank(value)
    if text is None:
        return None
    if text not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid value: {text}")
    return text


def _fmt_cups(n: float) -> str:
    if float(n) == int(n):
        shown = str(int(n))
    else:
        shown = f"{n:g}"
    return f"{shown} cup" if n == 1 else f"{shown} cups"


def _normalize_action_text(text: Optional[str]) -> str:
    collapsed = " ".join((text or "").lower().split())
    return collapsed.replace("'", "").replace("’", "").rstrip(".…").strip()


def _is_harvest_status_text(text: Optional[str]) -> bool:
    """True when a next_action string is just harvest readiness, not a task."""
    norm = _normalize_action_text(text)
    if not norm:
        return False
    if norm in _HARVEST_NEXT_PHRASES:
        return True
    labels = {_normalize_action_text(v) for v in HARVEST_STATUS_LABELS.values()}
    return norm in labels


def _culture_next(culture, schedule, today_date: str):
    """Soonest upcoming logged task, else a genuine one-off next_action.

    Harvest status is a badge, not Next. Due-today / overdue / never-done
    logged items belong in the Due panel; Next is what's coming after that.
    """
    candidates = []
    for s in schedule or []:
        if not s.get("is_active"):
            continue
        if s.get("tracking_mode") != "logged":
            continue
        due = s.get("next_due")
        if due and due > today_date:
            candidates.append({
                "text": (s.get("description") or "").strip(),
                "date": due,
                "source": "schedule",
            })
    action = (culture.get("next_action") or "").strip()
    if action and not _is_harvest_status_text(action):
        candidates.append({
            "text": action,
            "date": culture.get("next_action_date") or None,
            "source": "manual",
        })
    if not candidates:
        return None

    def sort_key(item):
        # Dated first (soonest), undated last; prefer schedule over one-off ties.
        dated = item.get("date") or "9999-99-99"
        source_rank = 0 if item["source"] == "schedule" else 1
        return (dated, source_rank, item["text"])

    candidates.sort(key=sort_key)
    return candidates[0]


def _apply_schedule_dates(existing, last_done, next_due, interval_days):
    """Same last_done/next_due rules as tank maintenance schedule edits."""
    last_done_val = existing["last_done"]
    next_due_val = existing["next_due"]
    last_done_set = last_done is not None and last_done.strip() != ""
    next_due_set = next_due is not None and next_due.strip() != ""
    if last_done_set:
        last_done_val = last_done.strip()
    if next_due_set:
        next_due_val = next_due.strip()
    elif last_done_set and next_due is None:
        next_due_val = compute_next_due(None, interval_days, date.fromisoformat(last_done_val))
    elif last_done_set and next_due is not None and not next_due.strip():
        next_due_val = compute_next_due(None, interval_days, date.fromisoformat(last_done_val))
    return last_done_val, next_due_val


_CULTURE_SELECT = """
SELECT c.*,
  t.name AS consumer_tank_name,
  dc.name AS destination_culture_name,
  dv.name AS destination_vessel_name,
  dvc.id AS destination_vessel_culture_id,
  dvc.name AS destination_vessel_culture_name
FROM cultures c
LEFT JOIN tanks t ON t.id = c.consumer_tank_id
LEFT JOIN cultures dc ON dc.id = c.destination_culture_id
LEFT JOIN culture_vessels dv ON dv.id = c.destination_vessel_id
LEFT JOIN cultures dvc ON dvc.id = dv.culture_id
"""


def _with_destination(culture):
    if not culture:
        return culture
    if culture.get("kind") not in CULTURE_KINDS:
        culture["kind"] = "other"
    if culture.get("consumer_tank_id"):
        culture["destination_kind"] = "tank"
        culture["destination_value"] = f"tank:{culture['consumer_tank_id']}"
        culture["destination_label"] = culture.get("consumer_tank_name") or "tank"
        culture["destination_href"] = f"/tanks/{culture['consumer_tank_id']}"
    elif culture.get("destination_culture_id"):
        culture["destination_kind"] = "culture"
        culture["destination_value"] = f"culture:{culture['destination_culture_id']}"
        culture["destination_label"] = culture.get("destination_culture_name") or "culture"
        culture["destination_href"] = f"/cultures/{culture['destination_culture_id']}"
    elif culture.get("destination_vessel_id"):
        culture["destination_kind"] = "vessel"
        culture["destination_value"] = f"vessel:{culture['destination_vessel_id']}"
        cname = culture.get("destination_vessel_culture_name") or ""
        vname = culture.get("destination_vessel_name") or "bin"
        culture["destination_label"] = f"{cname} / {vname}" if cname else vname
        href_id = culture.get("destination_vessel_culture_id")
        culture["destination_href"] = f"/cultures/{href_id}" if href_id else ""
    else:
        culture["destination_kind"] = None
        culture["destination_value"] = ""
        culture["destination_label"] = None
        culture["destination_href"] = None
    return culture


def _culture_or_404(conn, culture_id: int):
    culture = row_to_dict(conn.execute(
        _CULTURE_SELECT + " WHERE c.id = ?",
        (culture_id,),
    ).fetchone())
    if not culture:
        raise HTTPException(status_code=404, detail="Culture not found")
    return _with_destination(culture)


def _parse_destination(value: Optional[str], legacy_tank: Optional[str] = None):
    text = _blank(value)
    if not text:
        tank = _int_or_none(legacy_tank)
        return tank, None, None
    if ":" not in text:
        tank = _int_or_none(text)
        return tank, None, None
    kind, _, rest = text.partition(":")
    dest_id = _int_or_none(rest)
    if dest_id is None:
        raise HTTPException(status_code=400, detail="Invalid destination")
    if kind == "tank":
        return dest_id, None, None
    if kind == "culture":
        return None, dest_id, None
    if kind == "vessel":
        return None, None, dest_id
    raise HTTPException(status_code=400, detail="Invalid destination")


def _validate_destination(conn, tank_id, dest_culture_id, dest_vessel_id, exclude_culture_id=None):
    if dest_culture_id is not None:
        if exclude_culture_id is not None and dest_culture_id == exclude_culture_id:
            raise HTTPException(status_code=400, detail="A culture cannot send harvest to itself")
        row = conn.execute("SELECT id FROM cultures WHERE id=?", (dest_culture_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Destination culture not found")
    if dest_vessel_id is not None:
        row = conn.execute(
            "SELECT id, culture_id FROM culture_vessels WHERE id=?", (dest_vessel_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Destination bin not found")
        if exclude_culture_id is not None and row["culture_id"] == exclude_culture_id:
            raise HTTPException(status_code=400, detail="Pick a bin on another culture")
    if tank_id is not None:
        row = conn.execute("SELECT id FROM tanks WHERE id=?", (tank_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Destination tank not found")
    return tank_id, dest_culture_id, dest_vessel_id


def _destination_options(conn, exclude_culture_id=None):
    tanks = _tanks(conn)
    if exclude_culture_id is None:
        cultures = rows_to_list(conn.execute(
            "SELECT id, name FROM cultures WHERE status='active' ORDER BY name"
        ).fetchall())
        vessels = rows_to_list(conn.execute(
            """SELECT v.id, v.name, c.name AS culture_name
               FROM culture_vessels v
               JOIN cultures c ON c.id = v.culture_id
               WHERE v.status='active'
               ORDER BY c.name, v.sort_order, v.id"""
        ).fetchall())
    else:
        cultures = rows_to_list(conn.execute(
            """SELECT id, name FROM cultures
               WHERE status='active' AND id != ?
               ORDER BY name""",
            (exclude_culture_id,),
        ).fetchall())
        vessels = rows_to_list(conn.execute(
            """SELECT v.id, v.name, c.name AS culture_name
               FROM culture_vessels v
               JOIN cultures c ON c.id = v.culture_id
               WHERE v.status='active' AND v.culture_id != ?
               ORDER BY c.name, v.sort_order, v.id""",
            (exclude_culture_id,),
        ).fetchall())
    return tanks, cultures, vessels


def _resolve_pour_targets(conn, culture):
    """Where a harvest should be poured (another culture's bins)."""
    vid = culture.get("destination_vessel_id")
    if vid:
        row = row_to_dict(conn.execute(
            "SELECT id, culture_id FROM culture_vessels WHERE id=?", (vid,)
        ).fetchone())
        if row:
            return row["culture_id"], [row["id"]]
        return None, []
    dest_cid = culture.get("destination_culture_id")
    if dest_cid:
        rows = conn.execute(
            """SELECT id FROM culture_vessels
               WHERE culture_id=? AND status='active'
               ORDER BY sort_order, id""",
            (dest_cid,),
        ).fetchall()
        return dest_cid, [r["id"] for r in rows]
    return None, []


def _tanks(conn):
    return rows_to_list(conn.execute(
        "SELECT id, name FROM tanks WHERE status='active' ORDER BY name"
    ).fetchall())


def _vessels(conn, culture_id: int):
    latest_look = """SELECT {col} FROM culture_log l
                     JOIN culture_log_vessels lv ON lv.log_id = l.id
                     WHERE lv.vessel_id = v.id AND l.kind = 'look'
                     ORDER BY l.timestamp DESC, l.id DESC LIMIT 1"""
    latest_feed = """SELECT {col} FROM culture_log l
                     JOIN culture_log_vessels lv ON lv.log_id = l.id
                     WHERE lv.vessel_id = v.id AND l.kind = 'feed' AND COALESCE(l.held,0)=0
                     ORDER BY l.timestamp DESC, l.id DESC LIMIT 1"""
    latest_temp = """SELECT {col} FROM culture_log l
                     JOIN culture_log_vessels lv ON lv.log_id = l.id
                     WHERE lv.vessel_id = v.id AND l.temp_f IS NOT NULL
                     ORDER BY l.timestamp DESC, l.id DESC LIMIT 1"""
    latest_bin_note = """SELECT lv.notes FROM culture_log l
                     JOIN culture_log_vessels lv ON lv.log_id = l.id
                     WHERE lv.vessel_id = v.id AND COALESCE(lv.notes,'') != ''
                     ORDER BY l.timestamp DESC, l.id DESC LIMIT 1"""
    return rows_to_list(conn.execute(
        f"""SELECT v.*,
                  ({latest_feed.format(col='l.timestamp')}) AS last_feed_at,
                  ({latest_feed.format(col='l.food')}) AS last_feed_food,
                  ({latest_feed.format(col='COALESCE(lv.amount_text, l.amount_text)')}) AS last_feed_amount,
                  ({latest_look.format(col='l.timestamp')}) AS last_look_at,
                  ({latest_look.format(col='COALESCE(lv.tint, l.tint)')}) AS last_tint,
                  ({latest_look.format(col='COALESCE(lv.density, l.density)')}) AS last_density,
                  ({latest_look.format(col='COALESCE(lv.guts, l.guts)')}) AS last_guts,
                  ({latest_bin_note}) AS last_bin_notes,
                  ({latest_temp.format(col='l.temp_f')}) AS last_temp_f
           FROM culture_vessels v
           WHERE v.culture_id = ?
           ORDER BY v.sort_order, v.id""",
        (culture_id,),
    ).fetchall())


def _valid_vessel_ids(conn, culture_id: int, raw_ids: List[str]) -> List[int]:
    wanted = []
    for item in raw_ids or []:
        text = str(item).strip()
        if text.isdigit():
            wanted.append(int(text))
    if not wanted:
        return []
    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT id FROM culture_vessels WHERE culture_id=? AND id IN ({placeholders})",
        (culture_id, *wanted),
    ).fetchall()
    found = {row["id"] for row in rows}
    return [vid for vid in wanted if vid in found]


def _tag_log_vessels(conn, log_id: int, vessel_ids: List[int], details=None):
    by_id = {d["id"]: d for d in (details or []) if d.get("id") is not None}
    for vid in vessel_ids:
        d = by_id.get(vid, {})
        conn.execute(
            """INSERT OR IGNORE INTO culture_log_vessels
               (log_id, vessel_id, tint, density, guts, amount_text, notes)
               VALUES (?,?,?,?,?,?,?)""",
            (log_id, vid, d.get("tint"), d.get("density"), d.get("guts"),
             d.get("amount_text"), d.get("notes")),
        )


def _replace_log_vessels(conn, log_id: int, vessel_ids: List[int], details=None):
    conn.execute("DELETE FROM culture_log_vessels WHERE log_id=?", (log_id,))
    _tag_log_vessels(conn, log_id, vessel_ids, details)


def _log_values_from_form(form, *, default_kind=None, existing=None):
    """Parse shared culture_log fields from a create/update form.

    Fields omitted from the POST (disabled inputs) keep their existing value
    on update. Unchecked checkboxes are omitted too — `held` is the exception
    and always means off when missing.
    """
    existing = existing or {}
    kind = _choice(form.get("kind"), LOG_KINDS) or default_kind or existing.get("kind")

    def choice_field(key, allowed):
        if key in form:
            return _choice(form.get(key), allowed)
        return existing.get(key)

    def float_field(key):
        if key in form:
            return _float_or_none(form.get(key))
        return existing.get(key)

    if "cups" in form and _float_or_none(form.get("cups")) is not None:
        amount = _fmt_cups(_float_or_none(form.get("cups")))
    elif "amount_text" in form:
        amount = _blank(form.get("amount_text"))
    else:
        amount = existing.get("amount_text")

    held = form.get("held") in ("1", "on", "true")
    if kind == "feed" and held:
        kind = "look"
    timestamp = _blank(form.get("timestamp")) if "timestamp" in form else existing.get("timestamp")
    notes = _blank(form.get("notes")) if "notes" in form else existing.get("notes")
    temp_f = float_field("temp_f")
    temp_kind = choice_field("temp_kind", TEMP_KINDS)
    # Look logs only record water temp (no air/RH). Infer or clear temp_kind
    # from whether a reading was given, so an empty look field doesn't leave
    # a stray kind and a filled one doesn't need a hidden temp_kind input.
    if kind == "look" and "temp_f" in form:
        temp_kind = "water" if temp_f is not None else None
    return {
        "kind": kind,
        "timestamp": timestamp,
        "food": choice_field("food", FOODS),
        "amount_text": amount,
        "notes": notes,
        "tint": choice_field("tint", TINTS),
        "density": choice_field("density", DENSITIES),
        "guts": choice_field("guts", GUTS),
        "temp_f": temp_f,
        "temp_kind": temp_kind,
        "rh": float_field("rh"),
        "rh_low": float_field("rh_low"),
        "rh_high": float_field("rh_high"),
        "temp_low": float_field("temp_low"),
        "temp_high": float_field("temp_high"),
        "held": held,
    }


def _insert_log(conn, culture_id: int, *, kind: str, timestamp=None, food=None,
                amount_text=None, notes=None, tint=None, density=None, guts=None,
                temp_f=None, temp_kind=None, rh=None, rh_low=None, rh_high=None,
                temp_low=None, temp_high=None, held=0, vessel_ids=None,
                vessel_details=None) -> int:
    if not timestamp:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        """INSERT INTO culture_log
           (culture_id, timestamp, kind, food, amount_text, notes, tint, density, guts,
            temp_f, temp_kind, rh, rh_low, rh_high, temp_low, temp_high, held)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (culture_id, timestamp, kind, food, amount_text, notes, tint, density, guts,
         temp_f, temp_kind, rh, rh_low, rh_high, temp_low, temp_high, 1 if held else 0),
    )
    log_id = cur.lastrowid
    _tag_log_vessels(conn, log_id, vessel_ids or [], vessel_details)
    return log_id


def _log_calendar_date(timestamp: Optional[str]) -> date:
    """Calendar day for last_done from a UTC log timestamp (server-local)."""
    text = (timestamp or "").strip()
    if len(text) >= 19:
        try:
            naive = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            return naive.replace(tzinfo=timezone.utc).astimezone().date()
        except ValueError:
            pass
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    return date.today()


def _advance_logged_schedules(conn, culture_id: int, *, category: str,
                              vessel_ids=None, timestamp=None):
    """Bump last_done / next_due on matching logged schedule rows.

    Station-wide tasks (no vessel_id) always match. Per-bin tasks match when
    that bin was tagged. An older backdated log does not rewind last_done.
    """
    done = _log_calendar_date(timestamp)
    done_s = done.isoformat()
    tagged = set(vessel_ids or [])
    rows = rows_to_list(conn.execute(
        """SELECT * FROM culture_schedule
           WHERE culture_id=? AND is_active=1 AND tracking_mode='logged'
             AND category=?""",
        (culture_id, category),
    ).fetchall())
    for sched in rows:
        vid = sched.get("vessel_id")
        if vid is not None and vid not in tagged:
            continue
        last = sched.get("last_done")
        if last and last > done_s:
            continue
        next_due = compute_next_due(None, sched.get("interval_days"), done)
        conn.execute(
            """UPDATE culture_schedule
               SET last_done=?, next_due=?, updated_at=datetime('now')
               WHERE id=?""",
            (done_s, next_due, sched["id"]),
        )


def _vessel_role_for_culture(culture) -> str:
    kind = culture.get("kind") or "other"
    return kind if kind in ROLES else "other"


def _default_mark_done_vessels(conn, culture_id: int, sched) -> List[int]:
    if sched.get("vessel_id"):
        return [sched["vessel_id"]]
    if sched.get("category") in ("feeding", "look"):
        rows = conn.execute(
            """SELECT id FROM culture_vessels
               WHERE culture_id=? AND status='active'
               ORDER BY sort_order, id""",
            (culture_id,),
        ).fetchall()
        return [r["id"] for r in rows]
    return []


def load_today_cultures(conn, today_date: str):
    """Active cultures that have something to show on Today (due, done today, or next)."""
    cultures = rows_to_list(conn.execute(
        """SELECT c.id, c.name, c.next_action, c.next_action_date FROM cultures c
           WHERE c.status='active' ORDER BY c.name"""
    ).fetchall())
    visible = []
    for culture in cultures:
        schedule = rows_to_list(conn.execute(
            """SELECT * FROM culture_schedule
               WHERE culture_id=? AND is_active=1
               ORDER BY category, description""",
            (culture["id"],),
        ).fetchall())
        culture["today_schedule"] = [
            s for s in schedule if s.get("tracking_mode") == "reference_only"
        ]
        due = [
            s for s in schedule if s.get("tracking_mode") == "logged" and (
                (s.get("next_due") and s["next_due"] <= today_date)
                or (s.get("next_due") is None and s.get("interval_days") is not None)
                or s.get("last_done") == today_date
            )
        ]
        due.sort(key=lambda s: (
            1 if s.get("last_done") == today_date else 0,
            1 if s.get("next_due") is None else 0,
            s.get("next_due") or "",
        ))
        culture["maintenance_items"] = due
        culture["next_item"] = _culture_next(culture, schedule, today_date)
        if culture["today_schedule"] or culture["maintenance_items"] or culture["next_item"]:
            visible.append(culture)
    return visible


def _template_labels():
    return {
        "role_labels": ROLE_LABELS,
        "kind_labels": KIND_LABELS,
        "food_labels": FOOD_LABELS,
        "tint_labels": TINT_LABELS,
        "density_labels": DENSITY_LABELS,
        "guts_labels": GUTS_LABELS,
        "category_labels": CATEGORY_LABELS,
        "roles": ROLES,
        "vessel_statuses": VESSEL_STATUSES,
        "culture_statuses": CULTURE_STATUSES,
        "culture_kinds": CULTURE_KINDS,
        "culture_kind_labels": CULTURE_KIND_LABELS,
        "foods": FOODS,
        "tints": TINTS,
        "densities": DENSITIES,
        "guts_values": GUTS,
        "schedule_categories": SCHEDULE_CATEGORIES,
        "tracking_modes": TRACKING_MODES,
        "log_kinds": LOG_KINDS,
        "harvest_statuses": HARVEST_STATUSES,
        "harvest_status_labels": HARVEST_STATUS_LABELS,
        "temp_kinds": TEMP_KINDS,
        "temp_kind_labels": TEMP_KIND_LABELS,
    }


@router.get("", response_class=HTMLResponse)
async def list_cultures(request: Request):
    with get_db() as conn:
        cultures = rows_to_list(conn.execute(
            _CULTURE_SELECT +
            " ORDER BY CASE c.status WHEN 'active' THEN 0 ELSE 1 END, c.name"
        ).fetchall())
        today_date = date.today().isoformat()
        for culture in cultures:
            _with_destination(culture)
            culture["vessels"] = rows_to_list(conn.execute(
                """SELECT id, name, role, status, is_lit
                   FROM culture_vessels WHERE culture_id=?
                   ORDER BY sort_order, id""",
                (culture["id"],),
            ).fetchall())
            schedule = rows_to_list(conn.execute(
                """SELECT * FROM culture_schedule
                   WHERE culture_id=? AND is_active=1""",
                (culture["id"],),
            ).fetchall())
            culture["next_item"] = _culture_next(culture, schedule, today_date)
    return templates.TemplateResponse(request, "cultures/list.html", {
        "cultures": cultures,
        **_template_labels(),
    })


@router.get("/new", response_class=HTMLResponse)
async def new_culture_form(request: Request):
    with get_db() as conn:
        dest_tanks, dest_cultures, dest_vessels = _destination_options(conn)
    return templates.TemplateResponse(request, "cultures/form.html", {
        "culture": None,
        "action": "add",
        "dest_tanks": dest_tanks,
        "dest_cultures": dest_cultures,
        "dest_vessels": dest_vessels,
        **_template_labels(),
    })


@router.post("")
async def create_culture(
    request: Request,
    name: str = Form(...),
    kind: str = Form("other"),
    destination: Optional[str] = Form(None),
    consumer_tank_id: Optional[str] = Form(None),
    isolation_notes: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    harvest_status: str = Form("not_ready"),
    next_action: Optional[str] = Form(None),
    next_action_date: Optional[str] = Form(None),
    status: str = Form("active"),
):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    status = _choice(status, CULTURE_STATUSES) or "active"
    kind = _choice(kind, CULTURE_KINDS) or "other"
    tank_id, dest_culture_id, dest_vessel_id = _parse_destination(destination, consumer_tank_id)
    with get_db() as conn:
        tank_id, dest_culture_id, dest_vessel_id = _validate_destination(
            conn, tank_id, dest_culture_id, dest_vessel_id
        )
        cur = conn.execute(
            """INSERT INTO cultures
               (name, kind, consumer_tank_id, destination_culture_id, destination_vessel_id,
                isolation_notes, notes, harvest_status, next_action, next_action_date, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (name, kind, tank_id, dest_culture_id, dest_vessel_id,
             _blank(isolation_notes), _blank(notes),
             _choice(harvest_status, HARVEST_STATUSES) or "not_ready",
             _blank(next_action), _blank(next_action_date), status),
        )
        culture_id = cur.lastrowid
    if _wants_json(request):
        return JSONResponse({"id": culture_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.get("/{culture_id}", response_class=HTMLResponse)
async def culture_detail(request: Request, culture_id: int):
    today_date = date.today().isoformat()
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
        vessels = _vessels(conn, culture_id)
        log_rows = rows_to_list(conn.execute(
            """SELECT l.*, group_concat(v.name, ', ') AS vessel_names
               FROM culture_log l
               LEFT JOIN culture_log_vessels lv ON lv.log_id = l.id
               LEFT JOIN culture_vessels v ON v.id = lv.vessel_id
               WHERE l.culture_id = ?
               GROUP BY l.id
               ORDER BY l.timestamp DESC, l.id DESC
               LIMIT 80""",
            (culture_id,),
        ).fetchall())
        _attach_log_bins(conn, log_rows)
        bench_air = _latest_bench_air(conn)
        schedule = rows_to_list(conn.execute(
            """SELECT s.*, v.name AS vessel_name
               FROM culture_schedule s
               LEFT JOIN culture_vessels v ON v.id = s.vessel_id
               WHERE s.culture_id = ?
               ORDER BY s.tracking_mode DESC, s.category, s.description""",
            (culture_id,),
        ).fetchall())
        dest_tanks, dest_cultures, dest_vessels = _destination_options(conn, culture_id)
    due_items = [
        s for s in schedule
        if s["is_active"] and s["tracking_mode"] == "logged" and (
            (s.get("next_due") and s["next_due"] <= today_date)
            or (s.get("next_due") is None and s.get("interval_days") is not None)
            or s.get("last_done") == today_date
        )
    ]
    next_item = _culture_next(culture, schedule, today_date)
    return templates.TemplateResponse(request, "cultures/detail.html", {
        "culture": culture,
        "vessels": vessels,
        "log_rows": log_rows,
        "schedule": schedule,
        "due_items": due_items,
        "next_item": next_item,
        "dest_tanks": dest_tanks,
        "dest_cultures": dest_cultures,
        "dest_vessels": dest_vessels,
        "today_date": today_date,
        "bench_air": bench_air,
        **_template_labels(),
    })


@router.get("/{culture_id}/edit", response_class=HTMLResponse)
async def edit_culture_form(request: Request, culture_id: int):
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
        dest_tanks, dest_cultures, dest_vessels = _destination_options(conn, culture_id)
    return templates.TemplateResponse(request, "cultures/form.html", {
        "culture": culture,
        "action": "edit",
        "dest_tanks": dest_tanks,
        "dest_cultures": dest_cultures,
        "dest_vessels": dest_vessels,
        **_template_labels(),
    })


@router.post("/{culture_id}/edit")
async def update_culture(
    request: Request,
    culture_id: int,
    name: str = Form(...),
    kind: str = Form("other"),
    destination: Optional[str] = Form(None),
    consumer_tank_id: Optional[str] = Form(None),
    isolation_notes: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    harvest_status: str = Form("not_ready"),
    next_action: Optional[str] = Form(None),
    next_action_date: Optional[str] = Form(None),
    status: str = Form("active"),
):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    status = _choice(status, CULTURE_STATUSES) or "active"
    kind = _choice(kind, CULTURE_KINDS) or "other"
    tank_id, dest_culture_id, dest_vessel_id = _parse_destination(destination, consumer_tank_id)
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        tank_id, dest_culture_id, dest_vessel_id = _validate_destination(
            conn, tank_id, dest_culture_id, dest_vessel_id, exclude_culture_id=culture_id
        )
        conn.execute(
            """UPDATE cultures
               SET name=?, kind=?, consumer_tank_id=?, destination_culture_id=?,
                   destination_vessel_id=?, isolation_notes=?, notes=?,
                   harvest_status=?, next_action=?, next_action_date=?, status=?,
                   updated_at=datetime('now')
               WHERE id=?""",
            (name, kind, tank_id, dest_culture_id, dest_vessel_id,
             _blank(isolation_notes), _blank(notes),
             _choice(harvest_status, HARVEST_STATUSES) or "not_ready",
             _blank(next_action), _blank(next_action_date), status, culture_id),
        )
        conn.execute(
            "UPDATE culture_vessels SET role=?, updated_at=datetime('now') WHERE culture_id=?",
            (_vessel_role_for_culture({"kind": kind}), culture_id),
        )
    if _wants_json(request):
        return JSONResponse({"status": "updated"})
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/delete")
async def delete_culture(request: Request, culture_id: int):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        conn.execute("DELETE FROM cultures WHERE id=?", (culture_id,))
    if _wants_json(request):
        return JSONResponse({"status": "deleted"})
    return RedirectResponse(url="/cultures", status_code=303)


@router.post("/{culture_id}/vessels")
async def add_vessel(
    request: Request,
    culture_id: int,
    name: str = Form(...),
    volume_gallons: Optional[str] = Form(None),
    is_lit: Optional[str] = Form(None),
    status: str = Form("active"),
    notes: Optional[str] = Form(None),
    hitchhikers: Optional[str] = Form(None),
    sort_order: Optional[str] = Form(None),
):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    status = _choice(status, VESSEL_STATUSES) or "active"
    lit = 1 if is_lit in ("1", "on", "true") else 0
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
        role = _vessel_role_for_culture(culture)
        order = _int_or_none(sort_order)
        if order is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM culture_vessels WHERE culture_id=?",
                (culture_id,),
            ).fetchone()
            order = row["n"]
        cur = conn.execute(
            """INSERT INTO culture_vessels
               (culture_id, name, role, volume_gallons, is_lit, status, sort_order, notes, hitchhikers)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (culture_id, name, role, _float_or_none(volume_gallons), lit, status, order,
             _blank(notes), _blank(hitchhikers)),
        )
        vessel_id = cur.lastrowid
    if _wants_json(request):
        return JSONResponse({"id": vessel_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/vessels/{vessel_id}/update")
async def update_vessel(
    request: Request,
    culture_id: int,
    vessel_id: int,
    name: str = Form(...),
    volume_gallons: Optional[str] = Form(None),
    is_lit: Optional[str] = Form(None),
    status: str = Form("active"),
    notes: Optional[str] = Form(None),
    hitchhikers: Optional[str] = Form(None),
    sort_order: Optional[str] = Form(None),
):
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    status = _choice(status, VESSEL_STATUSES) or "active"
    lit = 1 if is_lit in ("1", "on", "true") else 0
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
        role = _vessel_role_for_culture(culture)
        existing = row_to_dict(conn.execute(
            "SELECT * FROM culture_vessels WHERE id=? AND culture_id=?",
            (vessel_id, culture_id),
        ).fetchone())
        if not existing:
            raise HTTPException(status_code=404, detail="Vessel not found")
        order = _int_or_none(sort_order)
        if order is None:
            order = existing["sort_order"]
        conn.execute(
            """UPDATE culture_vessels
               SET name=?, role=?, volume_gallons=?, is_lit=?, status=?, sort_order=?,
                   notes=?, hitchhikers=?, updated_at=datetime('now')
               WHERE id=?""",
            (name, role, _float_or_none(volume_gallons), lit, status, order,
             _blank(notes), _blank(hitchhikers), vessel_id),
        )
    if _wants_json(request):
        return JSONResponse({"status": "updated"})
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/vessels/{vessel_id}/delete")
async def delete_vessel(request: Request, culture_id: int, vessel_id: int):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        conn.execute(
            "DELETE FROM culture_vessels WHERE id=? AND culture_id=?",
            (vessel_id, culture_id),
        )
    if _wants_json(request):
        return JSONResponse({"status": "deleted"})
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


def _form_list(form, key):
    return [str(v) for v in form.getlist(key)]


def _vessel_details_from_form(form, ids, existing_by_id=None):
    existing_by_id = existing_by_id or {}
    details = []
    for vid in ids:
        prev = existing_by_id.get(vid) or {}
        notes_key = f"notes_{vid}"
        hitch_key = f"hitchhikers_{vid}"
        details.append({
            "id": vid,
            "tint": _choice(form.get(f"tint_{vid}"), TINTS),
            "density": _choice(form.get(f"density_{vid}"), DENSITIES),
            "guts": _choice(form.get(f"guts_{vid}"), GUTS),
            "amount_text": _blank(form.get(f"amount_{vid}")),
            "notes": _blank(form.get(notes_key)) if notes_key in form else prev.get("notes"),
            "hitchhikers_set": hitch_key in form,
            "hitchhikers": _blank(form.get(hitch_key)) if hitch_key in form else None,
        })
    return details


def _apply_log_vessel_state(conn, kind, details):
    """Write log fields that belong on the bin card (hitchhikers, crash)."""
    for d in details or []:
        if kind == "crash":
            conn.execute(
                """UPDATE culture_vessels
                   SET status='crashed', updated_at=datetime('now')
                   WHERE id=?""",
                (d["id"],),
            )
        if d.get("hitchhikers_set"):
            conn.execute(
                """UPDATE culture_vessels
                   SET hitchhikers=?, updated_at=datetime('now')
                   WHERE id=?""",
                (d.get("hitchhikers"), d["id"]),
            )


def _attach_log_bins(conn, log_rows):
    if not log_rows:
        return
    ids = [r["id"] for r in log_rows]
    placeholders = ",".join("?" * len(ids))
    bins = rows_to_list(conn.execute(
        f"""SELECT lv.*, v.name AS vessel_name
            FROM culture_log_vessels lv
            JOIN culture_vessels v ON v.id = lv.vessel_id
            WHERE lv.log_id IN ({placeholders})
            ORDER BY v.sort_order, v.id""",
        ids,
    ).fetchall())
    by_log = {}
    for b in bins:
        by_log.setdefault(b["log_id"], []).append(b)
    for row in log_rows:
        row["bins"] = by_log.get(row["id"], [])


def _latest_bench_air(conn):
    return row_to_dict(conn.execute(
        """SELECT * FROM culture_log
           WHERE kind='temp' AND temp_kind='air'
           ORDER BY timestamp DESC, id DESC LIMIT 1"""
    ).fetchone())


@router.post("/{culture_id}/log")
async def add_log(request: Request, culture_id: int):
    form = await request.form()
    values = _log_values_from_form(form)
    kind = values["kind"]
    amount = values["amount_text"]
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
        ids = _valid_vessel_ids(conn, culture_id, _form_list(form, "vessel_ids"))
        details = _vessel_details_from_form(form, ids)
        log_id = _insert_log(
            conn, culture_id,
            vessel_ids=ids,
            vessel_details=details,
            **values,
        )
        _apply_log_vessel_state(conn, kind, details)
        if kind == "feed":
            _advance_logged_schedules(
                conn, culture_id, category="feeding",
                vessel_ids=ids, timestamp=values["timestamp"],
            )
        tank_event_id = None
        feed_log_id = None
        dest_kind = culture.get("destination_kind")
        pour_to_bins = dest_kind in ("culture", "vessel")
        ts = values["timestamp"]
        user_notes = values["notes"]
        log_on_tank = form.get("log_on_tank")
        if kind == "harvest" and pour_to_bins:
            dest_cid, dest_ids = _resolve_pour_targets(conn, culture)
            if dest_cid and dest_ids:
                feed_notes = user_notes or f"From {culture['name']}"
                feed_log_id = _insert_log(
                    conn, dest_cid,
                    kind="feed",
                    timestamp=ts,
                    food="green_water",
                    amount_text=amount,
                    notes=feed_notes,
                    vessel_ids=dest_ids,
                )
                _advance_logged_schedules(
                    conn, dest_cid, category="feeding",
                    vessel_ids=dest_ids, timestamp=ts,
                )
        elif (
            kind == "harvest"
            and dest_kind == "tank"
            and log_on_tank in ("1", "on", "true")
            and culture.get("consumer_tank_id")
        ):
            names = []
            if ids:
                placeholders = ",".join("?" * len(ids))
                name_rows = conn.execute(
                    f"SELECT name FROM culture_vessels WHERE id IN ({placeholders}) ORDER BY sort_order, id",
                    ids,
                ).fetchall()
                names = [r["name"] for r in name_rows]
            from_part = f" from {', '.join(names)}" if names else ""
            cups_part = f" {amount}" if amount else ""
            event_notes = f"Live food harvest{cups_part}{from_part}."
            if user_notes:
                event_notes = f"{event_notes} {user_notes}"
            utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            ts = ts or utc_now
            cur = conn.execute(
                "INSERT INTO events (tank_id, event_type, notes, timestamp) VALUES (?,?,?,?)",
                (culture["consumer_tank_id"], "feeding", event_notes, ts),
            )
            tank_event_id = cur.lastrowid
            # Deliberately do not queue run_ai_analysis — harvest is culture ops,
            # not a tank water-test/event the analysis prompt is built for.
    if _wants_json(request):
        body = {"id": log_id, "status": "created"}
        if tank_event_id is not None:
            body["tank_event_id"] = tank_event_id
        if feed_log_id is not None:
            body["feed_log_id"] = feed_log_id
        return JSONResponse(body, status_code=201)
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/log/{log_id}/update")
async def update_log(request: Request, culture_id: int, log_id: int):
    form = await request.form()
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        existing = row_to_dict(conn.execute(
            "SELECT * FROM culture_log WHERE id=? AND culture_id=?",
            (log_id, culture_id),
        ).fetchone())
        if not existing:
            raise HTTPException(status_code=404, detail="Log entry not found")
        values = _log_values_from_form(
            form, default_kind=existing["kind"], existing=existing,
        )
        if not values["kind"]:
            values["kind"] = existing["kind"]
        if not values["timestamp"]:
            values["timestamp"] = existing["timestamp"]
        ids = _valid_vessel_ids(conn, culture_id, _form_list(form, "vessel_ids"))
        existing_bins = rows_to_list(conn.execute(
            "SELECT * FROM culture_log_vessels WHERE log_id=?", (log_id,)
        ).fetchall())
        existing_by_id = {b["vessel_id"]: b for b in existing_bins}
        details = _vessel_details_from_form(form, ids, existing_by_id)
        conn.execute(
            """UPDATE culture_log
               SET timestamp=?, kind=?, food=?, amount_text=?, notes=?,
                   tint=?, density=?, guts=?, temp_f=?, temp_kind=?,
                   rh=?, rh_low=?, rh_high=?, temp_low=?, temp_high=?, held=?,
                   updated_at=datetime('now')
               WHERE id=? AND culture_id=?""",
            (values["timestamp"], values["kind"], values["food"], values["amount_text"],
             values["notes"], values["tint"], values["density"], values["guts"],
             values["temp_f"], values["temp_kind"], values["rh"], values["rh_low"],
             values["rh_high"], values["temp_low"], values["temp_high"],
             1 if values["held"] else 0, log_id, culture_id),
        )
        _replace_log_vessels(conn, log_id, ids, details)
        _apply_log_vessel_state(conn, values["kind"], details)
        # Harvest side effects (tank feeding / dest-culture feed) stay as they
        # were — this only edits the culture history row itself.
    if _wants_json(request):
        return JSONResponse({"status": "updated"})
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/log/{log_id}/delete")
async def delete_log(request: Request, culture_id: int, log_id: int):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        conn.execute(
            "DELETE FROM culture_log WHERE id=? AND culture_id=?",
            (log_id, culture_id),
        )
    if _wants_json(request):
        return JSONResponse({"status": "deleted"})
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/schedule")
async def add_schedule(
    request: Request,
    culture_id: int,
    category: str = Form(...),
    description: str = Form(...),
    tracking_mode: str = Form("logged"),
    interval_days: Optional[str] = Form(None),
    vessel_id: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    is_active: Optional[str] = Form("1"),
):
    description = (description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="Description is required")
    category = _choice(category, SCHEDULE_CATEGORIES)
    tracking_mode = _choice(tracking_mode, TRACKING_MODES) or "logged"
    active = 0 if is_active in ("0", "off", "false") else 1
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        vid = _int_or_none(vessel_id)
        if vid is not None:
            owned = conn.execute(
                "SELECT id FROM culture_vessels WHERE id=? AND culture_id=?",
                (vid, culture_id),
            ).fetchone()
            if not owned:
                raise HTTPException(status_code=400, detail="Vessel not in this culture")
        cur = conn.execute(
            """INSERT INTO culture_schedule
               (culture_id, vessel_id, category, tracking_mode, description,
                interval_days, is_active, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (culture_id, vid, category, tracking_mode, description,
             _int_or_none(interval_days), active, _blank(notes)),
        )
        sch_id = cur.lastrowid
    if _wants_json(request):
        return JSONResponse({"id": sch_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/schedule/{sch_id}/update")
async def update_schedule(
    request: Request,
    culture_id: int,
    sch_id: int,
    category: str = Form(...),
    description: str = Form(...),
    tracking_mode: str = Form("logged"),
    interval_days: Optional[str] = Form(None),
    vessel_id: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    is_active: Optional[str] = Form("1"),
    last_done: Optional[str] = Form(None),
    next_due: Optional[str] = Form(None),
):
    description = (description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="Description is required")
    category = _choice(category, SCHEDULE_CATEGORIES)
    tracking_mode = _choice(tracking_mode, TRACKING_MODES) or "logged"
    active = 0 if is_active in ("0", "off", "false") else 1
    interval = _int_or_none(interval_days)
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        existing = row_to_dict(conn.execute(
            "SELECT * FROM culture_schedule WHERE id=? AND culture_id=?",
            (sch_id, culture_id),
        ).fetchone())
        if not existing:
            raise HTTPException(status_code=404, detail="Schedule entry not found")
        vid = _int_or_none(vessel_id)
        if vid is not None:
            owned = conn.execute(
                "SELECT id FROM culture_vessels WHERE id=? AND culture_id=?",
                (vid, culture_id),
            ).fetchone()
            if not owned:
                raise HTTPException(status_code=400, detail="Vessel not in this culture")
        last_done_val, next_due_val = _apply_schedule_dates(existing, last_done, next_due, interval)
        conn.execute(
            """UPDATE culture_schedule
               SET vessel_id=?, category=?, tracking_mode=?, description=?,
                   interval_days=?, is_active=?, notes=?, last_done=?, next_due=?,
                   updated_at=datetime('now')
               WHERE id=?""",
            (vid, category, tracking_mode, description, interval,
             active, _blank(notes), last_done_val, next_due_val, sch_id),
        )
    if _wants_json(request):
        return JSONResponse({"status": "updated"})
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/schedule/{sch_id}/delete")
async def delete_schedule(request: Request, culture_id: int, sch_id: int):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        conn.execute(
            "DELETE FROM culture_schedule WHERE id=? AND culture_id=?",
            (sch_id, culture_id),
        )
    if _wants_json(request):
        return JSONResponse({"status": "deleted"})
    return RedirectResponse(url=f"/cultures/{culture_id}", status_code=303)


@router.post("/{culture_id}/schedule/{sch_id}/mark-done")
async def mark_done(
    request: Request,
    culture_id: int,
    sch_id: int,
    return_to: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    outcome: Optional[str] = Form("fed"),
):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        sched = row_to_dict(conn.execute(
            "SELECT * FROM culture_schedule WHERE id=? AND culture_id=?",
            (sch_id, culture_id),
        ).fetchone())
        if not sched:
            raise HTTPException(status_code=404, detail="Schedule entry not found")
        today = date.today().isoformat()
        next_due = compute_next_due(None, sched.get("interval_days"), date.today())
        held = outcome == "held"
        kind = _KIND_FROM_CATEGORY.get(sched["category"], "other")
        if held:
            kind = "look"
        vessel_ids = _default_mark_done_vessels(conn, culture_id, sched)
        log_notes = _blank(notes) or ("Held" if held else sched["description"])
        _insert_log(
            conn, culture_id,
            kind=kind,
            notes=log_notes,
            held=held,
            vessel_ids=vessel_ids,
        )
        conn.execute(
            """UPDATE culture_schedule
               SET last_done=?, next_due=?, updated_at=datetime('now')
               WHERE id=?""",
            (today, next_due, sch_id),
        )
    if _wants_json(request):
        return JSONResponse({"status": "done"})
    if return_to == "today":
        dest = "/today"
    else:
        dest = f"/cultures/{culture_id}"
    return RedirectResponse(url=dest, status_code=303)
