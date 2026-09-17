"""Structured write tools for Ask AI.

Chat can record notes and events; it cannot run arbitrary SQL writes.
All mutations are tank- or culture-scoped, append-only for notes, and never
delete rows or change water tests / inhabitant counts.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from database import get_db, row_to_dict, rows_to_list
from routers.observations import ENTITY_TYPES, _set_observation_links

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "water_change", "feeding", "purchase", "observation",
    "treatment", "maintenance", "other",
)
TANK_NOTE_TARGETS = ("equipment", "schedule", "issue", "tank")
CULTURE_NOTE_TARGETS = ("culture", "vessel")
ISSUE_STATUSES = ("open", "monitoring", "resolved")
CULTURE_LOG_KINDS = ("look", "other")
MAX_TEXT_LEN = 2000

_WRITE_RULE = (
    "\nYou CAN write to the database when the user asks you to record, log, note, "
    "or update something. Use the write tools for that — do not say you are "
    "read-only.\n"
    "- add_observation is REQUIRED for \"note that\", \"record this\", \"log that\", "
    "or any keeper note. Always call it. Link to equipment, inhabitants, plants, "
    "or hardscape when the note is about them.\n"
    "- log_event: also call this when it is a discrete action (water_change, "
    "feeding, treatment, maintenance, observation, other) in addition to the "
    "observation.\n"
    "- append_notes: also call this when standing notes on equipment, a "
    "schedule row, an issue, or the tank should carry the change. Dated append "
    "only — never replace existing text. Does not change schedule cadence "
    "(day/interval); mention the new cadence in the note and tell the user to "
    "edit the Schedule page if the structured fields need to change.\n"
    "A short \"note that\" about equipment (e.g. UV back on schedule) should "
    "add_observation (linked to that equipment) AND append_notes on that "
    "equipment. Do not only append standing notes.\n"
    "Do not write unless the user asked to record, log, note, save, or update. "
    "Questions stay read-only. Do not delete rows, change inhabitant counts, "
    "edit water tests, or resolve/close an issue unless the user explicitly "
    "asked. After a successful write, confirm in one short sentence what was "
    "saved and where. If a write fails, say so and do not claim it was recorded."
)

def culture_write_rule(culture_id: int) -> str:
    return (
        "\nYou CAN write to culture records when the user asks you to record, log, "
        "note, or update something. Use the write tools — do not say you are "
        "read-only.\n"
        f"- Writes always go to the station currently being viewed (id={culture_id}), "
        "even though you can query every station. Do not write to another station.\n"
        "- log_culture_note: add a culture_log row (kind look or other) with notes. "
        "Use this for \"note that\" on this station.\n"
        "- append_notes: add a dated line to this culture's or one of its bins' "
        "standing notes. Does not replace existing text.\n"
        "Do not write unless the user asked to record, log, note, save, or update. "
        "Do not log feed or harvest from chat (those have extra fields and "
        "destinations — use the culture page). After a successful write, confirm in "
        "one short sentence what was saved. If a write fails, say so."
    )


def tank_write_tools():
    return [
        {
            "name": "add_observation",
            "description": (
                "Save a manual observation (keeper note) on this tank. REQUIRED "
                "whenever the user says to note, record, log, or remember something. "
                "Do not skip this in favor of append_notes alone. Optionally link "
                "it to one entity (equipment, inhabitant, plant, or hardscape)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The note to save."},
                    "link_type": {
                        "type": "string",
                        "enum": list(ENTITY_TYPES),
                        "description": "Entity type to link, if any.",
                    },
                    "link_id": {
                        "type": "integer",
                        "description": "Id of the entity to link (preferred when known).",
                    },
                    "link_name": {
                        "type": "string",
                        "description": "Name to match if id is unknown (e.g. UV).",
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "log_event",
            "description": (
                "Log a discrete tank event (water change, feeding, treatment, "
                "maintenance, observation, other). Does not trigger background AI "
                "analysis. Use with add_observation when the user is both noting "
                "and logging that something happened."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
                    "notes": {"type": "string", "description": "What happened."},
                    "amount": {
                        "type": "number",
                        "description": "Optional amount (gallons, dose, etc.).",
                    },
                },
                "required": ["event_type", "notes"],
            },
        },
        {
            "name": "append_notes",
            "description": (
                "Append a dated line to standing notes on equipment, a schedule "
                "row, an issue, or the tank. Never overwrites existing notes. "
                "For issues, optionally set status (open/monitoring/resolved) "
                "when the user explicitly asked to change it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": list(TANK_NOTE_TARGETS)},
                    "id": {
                        "type": "integer",
                        "description": "Row id (not needed for target=tank).",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name to match if id is unknown.",
                    },
                    "note": {"type": "string", "description": "Line to append."},
                    "status": {
                        "type": "string",
                        "enum": list(ISSUE_STATUSES),
                        "description": "Issue status; only used when target=issue.",
                    },
                },
                "required": ["target", "note"],
            },
        },
    ]


def culture_write_tools(culture_id: int):
    return [
        {
            "name": "log_culture_note",
            "description": (
                f"Save a culture_log note on the station being viewed (id={culture_id}). "
                "Kind look or other. Use when the user says to note or record something. "
                "Always writes to this station; you cannot target another culture."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "notes": {"type": "string", "description": "The note to save."},
                    "kind": {
                        "type": "string",
                        "enum": list(CULTURE_LOG_KINDS),
                        "description": "look or other (default other).",
                    },
                },
                "required": ["notes"],
            },
        },
        {
            "name": "append_notes",
            "description": (
                f"Append a dated line to standing notes on the viewed station "
                f"(id={culture_id}) or one of its bins. Never overwrites existing notes."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": list(CULTURE_NOTE_TARGETS)},
                    "id": {
                        "type": "integer",
                        "description": "Bin id when target=vessel. Ignored for target=culture.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Bin name if id is unknown (target=vessel).",
                    },
                    "note": {"type": "string", "description": "Line to append."},
                },
                "required": ["target", "note"],
            },
        },
    ]


def _clean_text(value, field="text") -> str | dict:
    text = " ".join(str(value or "").split())
    if not text:
        return {"error": f"{field} is required."}
    if len(text) > MAX_TEXT_LEN:
        return {"error": f"{field} is too long (max {MAX_TEXT_LEN} characters)."}
    return text


def _dated_line(text: str) -> str:
    today = date.today().isoformat()
    if text.startswith(today):
        return text
    return f"{today}: {text}"


def _append(existing, addition: str) -> str:
    addition = _dated_line(addition)
    if not (existing or "").strip():
        return addition
    return existing.rstrip() + "\n" + addition


def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _one_match(rows, label: str):
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, f"No {label} matched on this tank."
    return None, (
        f"Multiple {label} matches: "
        + "; ".join(_row_label(r) for r in rows[:8])
        + ". Retry with id."
    )


def _row_label(row: dict) -> str:
    rid = row.get("id")
    name = (
        row.get("model")
        or row.get("description")
        or row.get("title")
        or row.get("name")
        or row.get("common_name")
        or row.get("species")
        or row.get("item")
        or row.get("category")
        or ""
    )
    extra = row.get("brand") or row.get("category") or ""
    bits = [f"id={rid}", str(name).strip()]
    if extra and extra != name:
        bits.append(f"({extra})")
    return " ".join(b for b in bits if b)


def _resolve_equipment(conn, tank_id: int, eq_id=None, name=None):
    eq_id = _int_or_none(eq_id)
    if eq_id is not None:
        row = row_to_dict(conn.execute(
            "SELECT * FROM tank_equipment WHERE id=? AND tank_id=?",
            (eq_id, tank_id),
        ).fetchone())
        if not row:
            return None, "No equipment with that id on this tank."
        return row, None
    needle = (name or "").strip()
    if not needle:
        return None, "Provide equipment id or name."
    like = f"%{needle}%"
    rows = rows_to_list(conn.execute(
        """SELECT * FROM tank_equipment
           WHERE tank_id=? AND is_active=1
             AND (category LIKE ? OR IFNULL(brand,'') LIKE ?
                  OR IFNULL(model,'') LIKE ?)""",
        (tank_id, like, like, like),
    ).fetchall())
    return _one_match(rows, "equipment")


def _resolve_schedule(conn, tank_id: int, sch_id=None, name=None):
    sch_id = _int_or_none(sch_id)
    if sch_id is not None:
        row = row_to_dict(conn.execute(
            "SELECT * FROM recurring_schedule WHERE id=? AND tank_id=?",
            (sch_id, tank_id),
        ).fetchone())
        if not row:
            return None, "No schedule row with that id on this tank."
        return row, None
    needle = (name or "").strip()
    if not needle:
        return None, "Provide schedule id or name."
    like = f"%{needle}%"
    rows = rows_to_list(conn.execute(
        """SELECT * FROM recurring_schedule
           WHERE tank_id=? AND is_active=1
             AND (description LIKE ? OR IFNULL(notes,'') LIKE ?)""",
        (tank_id, like, like),
    ).fetchall())
    return _one_match(rows, "schedule")


def _resolve_issue(conn, tank_id: int, issue_id=None, name=None):
    issue_id = _int_or_none(issue_id)
    if issue_id is not None:
        row = row_to_dict(conn.execute(
            "SELECT * FROM issues WHERE id=? AND tank_id=?",
            (issue_id, tank_id),
        ).fetchone())
        if not row:
            return None, "No issue with that id on this tank."
        return row, None
    needle = (name or "").strip()
    if not needle:
        return None, "Provide issue id or name."
    like = f"%{needle}%"
    rows = rows_to_list(conn.execute(
        "SELECT * FROM issues WHERE tank_id=? AND (title LIKE ? OR IFNULL(description,'') LIKE ?)",
        (tank_id, like, like),
    ).fetchall())
    return _one_match(rows, "issue")


def _resolve_entity(conn, tank_id: int, link_type: str, link_id=None, link_name=None):
    if link_type not in ENTITY_TYPES:
        return None, f"link_type must be one of {', '.join(ENTITY_TYPES)}."
    link_id = _int_or_none(link_id)
    if link_type == "equipment":
        return _resolve_equipment(conn, tank_id, link_id, link_name)
    if link_type == "inhabitant":
        table, cols = "inhabitants", "IFNULL(common_name,'') LIKE ? OR IFNULL(species,'') LIKE ?"
    elif link_type == "plant":
        table, cols = "plants", "IFNULL(common_name,'') LIKE ? OR IFNULL(species,'') LIKE ?"
    else:
        table, cols = "hardscape", "item LIKE ?"
    if link_id is not None:
        row = row_to_dict(conn.execute(
            f"SELECT * FROM {table} WHERE id=? AND tank_id=?",
            (link_id, tank_id),
        ).fetchone())
        if not row:
            return None, f"No {link_type} with that id on this tank."
        return row, None
    needle = (link_name or "").strip()
    if not needle:
        return None, f"Provide {link_type} id or name."
    like = f"%{needle}%"
    params = (tank_id, like) if table == "hardscape" else (tank_id, like, like)
    extra = " AND status='active'" if table == "plants" else ""
    rows = rows_to_list(conn.execute(
        f"SELECT * FROM {table} WHERE tank_id=? AND ({cols}){extra}",
        params,
    ).fetchall())
    return _one_match(rows, link_type)


def _add_observation(conn, tank_id: int, inp: dict) -> dict:
    text = _clean_text(inp.get("text"))
    if isinstance(text, dict):
        return text
    links = []
    link_type = (inp.get("link_type") or "").strip() or None
    if link_type or inp.get("link_id") is not None or inp.get("link_name"):
        if not link_type:
            # Default to equipment when the user is naming gear (UV, heater, light).
            link_type = "equipment"
        row, err = _resolve_entity(
            conn, tank_id, link_type, inp.get("link_id"), inp.get("link_name"),
        )
        if err:
            return {"error": err}
        links.append((link_type, row["id"]))
    cur = conn.execute(
        "INSERT INTO observations (tank_id, source, text) VALUES (?, 'manual', ?)",
        (tank_id, text),
    )
    obs_id = cur.lastrowid
    if links:
        _set_observation_links(conn, obs_id, links)
    result = {"ok": True, "tool": "add_observation", "id": obs_id, "text": text}
    if links:
        result["linked"] = [{"type": t, "id": i} for t, i in links]
    return result


def _log_event(conn, tank_id: int, inp: dict) -> dict:
    event_type = (inp.get("event_type") or "").strip()
    if event_type not in EVENT_TYPES:
        return {"error": f"event_type must be one of {', '.join(EVENT_TYPES)}."}
    notes = _clean_text(inp.get("notes"), "notes")
    if isinstance(notes, dict):
        return notes
    amount = inp.get("amount")
    if amount is not None and amount != "":
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return {"error": "amount must be a number."}
    else:
        amount = None
    cur = conn.execute(
        "INSERT INTO events (tank_id, event_type, notes, amount) VALUES (?,?,?,?)",
        (tank_id, event_type, notes, amount),
    )
    return {
        "ok": True,
        "tool": "log_event",
        "id": cur.lastrowid,
        "event_type": event_type,
        "notes": notes,
    }


def _append_tank_notes(conn, tank_id: int, inp: dict) -> dict:
    target = (inp.get("target") or "").strip()
    if target not in TANK_NOTE_TARGETS:
        return {"error": f"target must be one of {', '.join(TANK_NOTE_TARGETS)}."}
    note = _clean_text(inp.get("note"), "note")
    if isinstance(note, dict):
        return note
    status = (inp.get("status") or "").strip() or None
    if status and target != "issue":
        return {"error": "status is only valid when target is issue."}
    if status and status not in ISSUE_STATUSES:
        return {"error": f"status must be one of {', '.join(ISSUE_STATUSES)}."}

    if target == "tank":
        row = row_to_dict(conn.execute(
            "SELECT id, notes FROM tanks WHERE id=?", (tank_id,),
        ).fetchone())
        if not row:
            return {"error": "Tank not found."}
        conn.execute(
            "UPDATE tanks SET notes=?, updated_at=datetime('now') WHERE id=?",
            (_append(row.get("notes"), note), tank_id),
        )
        return {"ok": True, "tool": "append_notes", "target": "tank", "id": tank_id}

    if target == "equipment":
        row, err = _resolve_equipment(conn, tank_id, inp.get("id"), inp.get("name"))
        if err:
            return {"error": err}
        conn.execute(
            """UPDATE tank_equipment SET notes=?, updated_at=datetime('now')
               WHERE id=? AND tank_id=?""",
            (_append(row.get("notes"), note), row["id"], tank_id),
        )
        return {
            "ok": True, "tool": "append_notes", "target": "equipment",
            "id": row["id"], "label": _row_label(row),
        }

    if target == "schedule":
        row, err = _resolve_schedule(conn, tank_id, inp.get("id"), inp.get("name"))
        if err:
            return {"error": err}
        conn.execute(
            """UPDATE recurring_schedule SET notes=?, updated_at=datetime('now')
               WHERE id=? AND tank_id=?""",
            (_append(row.get("notes"), note), row["id"], tank_id),
        )
        return {
            "ok": True, "tool": "append_notes", "target": "schedule",
            "id": row["id"], "label": _row_label(row),
        }

    row, err = _resolve_issue(conn, tank_id, inp.get("id"), inp.get("name"))
    if err:
        return {"error": err}
    new_notes = _append(row.get("notes"), note)
    if status:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        monitoring_at = row.get("monitoring_at")
        resolved_at = row.get("resolved_at")
        if status == "monitoring" and not monitoring_at:
            monitoring_at = now
        if status == "resolved":
            resolved_at = resolved_at or now
        conn.execute(
            """UPDATE issues SET notes=?, status=?, monitoring_at=?, resolved_at=?,
               updated_at=datetime('now') WHERE id=? AND tank_id=?""",
            (new_notes, status, monitoring_at, resolved_at, row["id"], tank_id),
        )
        return {
            "ok": True, "tool": "append_notes", "target": "issue",
            "id": row["id"], "label": _row_label(row), "status": status,
        }
    conn.execute(
        "UPDATE issues SET notes=?, updated_at=datetime('now') WHERE id=? AND tank_id=?",
        (new_notes, row["id"], tank_id),
    )
    return {
        "ok": True, "tool": "append_notes", "target": "issue",
        "id": row["id"], "label": _row_label(row),
    }


def apply_tank_write(tank_id: int, name: str, inp: dict) -> dict:
    handlers = {
        "add_observation": _add_observation,
        "log_event": _log_event,
        "append_notes": _append_tank_notes,
    }
    fn = handlers.get(name)
    if not fn:
        return {"error": f"Unknown tool {name}."}
    try:
        with get_db() as conn:
            tank = conn.execute("SELECT id FROM tanks WHERE id=?", (tank_id,)).fetchone()
            if not tank:
                return {"error": "Tank not found."}
            result = fn(conn, tank_id, inp or {})
            if result.get("ok"):
                logger.info(
                    "Chat write: tank=%s tool=%s id=%s",
                    tank_id, name, result.get("id"),
                )
            return result
    except Exception:
        logger.exception("Chat write failed: tank=%s tool=%s", tank_id, name)
        return {"error": "Write failed."}


def _resolve_culture(conn, culture_id=None, name=None, default_id=None):
    cid = _int_or_none(culture_id)
    if cid is None and not (name or "").strip():
        cid = _int_or_none(default_id)
    if cid is not None:
        row = row_to_dict(conn.execute(
            "SELECT * FROM cultures WHERE id=?", (cid,),
        ).fetchone())
        if not row:
            return None, "No culture with that id."
        return row, None
    needle = (name or "").strip()
    if not needle:
        return None, "Provide culture id or name."
    like = f"%{needle}%"
    rows = rows_to_list(conn.execute(
        "SELECT * FROM cultures WHERE name LIKE ?", (like,),
    ).fetchall())
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, "No culture matching that name."
    return None, (
        "Multiple culture matches: "
        + "; ".join(_row_label(r) for r in rows[:8])
        + ". Retry with id."
    )


def _resolve_vessel(conn, vessel_id=None, name=None, culture_id=None):
    vid = _int_or_none(vessel_id)
    cid = _int_or_none(culture_id)
    if vid is not None:
        if cid is not None:
            row = row_to_dict(conn.execute(
                "SELECT * FROM culture_vessels WHERE id=? AND culture_id=?",
                (vid, cid),
            ).fetchone())
        else:
            row = row_to_dict(conn.execute(
                "SELECT * FROM culture_vessels WHERE id=?", (vid,),
            ).fetchone())
        if not row:
            return None, "No bin with that id on this station."
        return row, None
    needle = (name or "").strip()
    if not needle:
        return None, "Provide bin id or name."
    like = f"%{needle}%"
    if cid is not None:
        rows = rows_to_list(conn.execute(
            "SELECT * FROM culture_vessels WHERE culture_id=? AND name LIKE ? AND status='active'",
            (cid, like),
        ).fetchall())
    else:
        rows = rows_to_list(conn.execute(
            "SELECT * FROM culture_vessels WHERE name LIKE ? AND status='active'",
            (like,),
        ).fetchall())
    if len(rows) == 1:
        return rows[0], None
    if not rows:
        return None, "No bin matching that name."
    return None, (
        "Multiple bin matches: "
        + "; ".join(_row_label(r) for r in rows[:8])
        + ". Retry with id."
    )


def _log_culture_note(conn, default_culture_id: int, inp: dict) -> dict:
    notes = _clean_text(inp.get("notes"), "notes")
    if isinstance(notes, dict):
        return notes
    kind = (inp.get("kind") or "other").strip()
    if kind not in CULTURE_LOG_KINDS:
        return {"error": f"kind must be one of {', '.join(CULTURE_LOG_KINDS)}."}
    row, err = _resolve_culture(conn, default_culture_id, None, default_culture_id)
    if err:
        return {"error": err}
    cur = conn.execute(
        "INSERT INTO culture_log (culture_id, kind, notes) VALUES (?,?,?)",
        (row["id"], kind, notes),
    )
    return {
        "ok": True,
        "tool": "log_culture_note",
        "id": cur.lastrowid,
        "culture_id": row["id"],
        "kind": kind,
        "notes": notes,
    }


def _append_culture_notes(conn, default_culture_id: int, inp: dict) -> dict:
    target = (inp.get("target") or "").strip()
    if target not in CULTURE_NOTE_TARGETS:
        return {"error": f"target must be one of {', '.join(CULTURE_NOTE_TARGETS)}."}
    note = _clean_text(inp.get("note"), "note")
    if isinstance(note, dict):
        return note
    if target == "culture":
        row, err = _resolve_culture(conn, default_culture_id, None, default_culture_id)
        if err:
            return {"error": err}
        conn.execute(
            "UPDATE cultures SET notes=?, updated_at=datetime('now') WHERE id=?",
            (_append(row.get("notes"), note), row["id"]),
        )
        return {
            "ok": True, "tool": "append_notes", "target": "culture",
            "id": row["id"], "label": _row_label(row),
        }
    row, err = _resolve_vessel(
        conn, inp.get("id"), inp.get("name"), culture_id=default_culture_id,
    )
    if err:
        return {"error": err}
    conn.execute(
        "UPDATE culture_vessels SET notes=?, updated_at=datetime('now') WHERE id=?",
        (_append(row.get("notes"), note), row["id"]),
    )
    return {
        "ok": True, "tool": "append_notes", "target": "vessel",
        "id": row["id"], "label": _row_label(row),
    }


def apply_culture_write(default_culture_id: int, name: str, inp: dict) -> dict:
    handlers = {
        "log_culture_note": _log_culture_note,
        "append_notes": _append_culture_notes,
    }
    fn = handlers.get(name)
    if not fn:
        return {"error": f"Unknown tool {name}."}
    try:
        with get_db() as conn:
            result = fn(conn, default_culture_id, inp or {})
            if result.get("ok"):
                logger.info(
                    "Chat write: culture=%s tool=%s id=%s",
                    default_culture_id, name, result.get("id"),
                )
            return result
    except Exception:
        logger.exception(
            "Chat write failed: culture=%s tool=%s", default_culture_id, name,
        )
        return {"error": "Write failed."}
