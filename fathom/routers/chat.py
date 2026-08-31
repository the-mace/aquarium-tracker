import os
import re
import json
import time
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
from database import get_db, get_db_readonly, get_schema_text, rows_to_list, row_to_dict
from security import require_ai_budget
from ai_config import CLAUDE_MODEL, CLAUDE_THINKING_DISABLED, CLAUDE_MAX_TOKENS_CHAT
from routers.ai_analysis import (
    _fmt_tank_notes, _fmt_inhabitants, _fmt_schedule, _fmt_goals, _CURRENT_PRACTICES_RULE,
    _fmt_home_water_block, _HOME_WATER_PROMPT_RULE, load_home_water_tests,
    _message_text,
)
from routers.goals import load_active_goals
from routers.cultures import (
    CATEGORY_LABELS, CULTURE_KIND_LABELS, DENSITY_LABELS, FOOD_LABELS, GUTS_LABELS,
    HARVEST_STATUS_LABELS, KIND_LABELS, TINT_LABELS,
    _CULTURE_SELECT, _attach_log_bins, _culture_or_404, _latest_bench_air, _vessels,
    _with_destination,
)

router = APIRouter(prefix="/tanks/{tank_id}/chat", tags=["chat"])
culture_router = APIRouter(prefix="/cultures/{culture_id}/chat", tags=["culture-chat"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
logger = logging.getLogger(__name__)

MAX_TURNS = 10
MAX_TOOL_ROUNDS = 4
# Tool rounds + thinking-disabled retry + a no-tools synthesis after the cap.
_MAX_CHAT_API_CALLS = MAX_TOOL_ROUNDS + 3
_CLAUDE_TIMEOUT = 90.0
QUERY_ROW_LIMIT = 200
TITLE_MAX_LEN = 48
MAX_CHAT_MESSAGE_CHARS = 4000

_TANK_SCOPED_TABLES = {
    "tank_equipment", "test_results", "inhabitants", "population_events",
    "purchases", "events", "issues", "observations", "plants", "hardscape",
    "tank_state_summary", "goals", "recurring_schedule", "tank_notes_proposals",
    "chat_conversations",
}
_JOIN_SCOPED_TABLES = {
    "observation_links": "observations",
    "goal_dependencies": "goals",
    "chat_messages": "chat_conversations",
}
_BLOCKED_IDENTIFIERS = {
    "sqlite_master", "sqlite_temp_master", "sqlite_schema", "sqlite_sequence",
}
_CULTURE_ALLOWED_TABLES = frozenset({
    "cultures", "culture_vessels", "culture_log", "culture_log_vessels", "culture_schedule",
})
_NON_CULTURE_TABLES = frozenset({
    "tanks", "tank_equipment", "test_results", "events", "inhabitants", "population_events",
    "purchases", "issues", "observations", "observation_links", "tank_state_summary",
    "recurring_schedule", "plants", "hardscape", "reference_info", "goals", "goal_dependencies",
    "tank_notes_proposals", "home_water_tests", "chat_conversations", "chat_messages",
})
_CONVERSATION_STYLE = (
    "\nConversation style:\n"
    "- Answer helpfully and concisely in plain text only — no markdown (no **bold**, no *italic*, "
    "no headers, no bullet dashes).\n"
    "- This is a multi-turn conversation. Prior user and assistant messages are already in the "
    "thread; the user has just read them.\n"
    "- On follow-ups, answer the new question directly. Do not restate conclusions, timelines, "
    "or facts you already covered unless the user asks for a recap or you need one short "
    "reference to support a new point.\n"
    "- Never open with meta filler (e.g. \"Good question\", \"That gives me a full picture\", "
    "\"Based on everything above\", \"Here is a comprehensive answer\"). Jump straight into the "
    "answer.\n"
    "- Prefer a natural continuation over a standalone re-briefing. Use snapshot data when it "
    "changes the answer; do not re-list inventory or re-walk prior reasoning by default.\n"
    "- Equipment and named products (heaters, lights, wattage vs volume) are in scope. You cannot "
    "browse listings or verify a SKU; do not refuse or say that is outside what you can do. "
    "Answer feasibility from general knowledge plus the snapshot."
)


class ChatMessage(BaseModel):
    message: str
    conversation_id: Optional[int] = None


def _utc_now() -> str:
    """UTC timestamp with fractional seconds so same-second updates sort correctly."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _title_from_message(message: str) -> str:
    text = " ".join((message or "").split())
    if not text:
        return "New conversation"
    if len(text) <= TITLE_MAX_LEN:
        return text
    return text[: TITLE_MAX_LEN - 1].rstrip() + "…"


def _query_db_tool(tank_id):
    return {
        "name": "query_db",
        "description": (
            "Run a single read-only SQL SELECT query against the Fathom database for anything "
            "not already covered by the context above — e.g. full test_results history/trends, "
            "population_events (when an inhabitant was added/died/removed), purchase totals, "
            f"older observations. This tank's id is {tank_id}. Every query that touches a "
            f"tank-scoped table MUST include tank_id = {tank_id} (no other tank). "
            f"Queries on tanks must include id = {tank_id}. Cross-tank comparison is not "
            f"available through this tool. Returns up to {QUERY_ROW_LIMIT} rows as JSON.\n\n"
            f"Schema:\n{get_schema_text()}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["sql"],
        },
    }


def _strip_sql_literals(sql: str) -> str:
    """Drop string literals and comments so they cannot fake a tank_id filter."""
    out = re.sub(r"'([^']|'')*'", "''", sql)
    out = re.sub(r"--.*?$", " ", out, flags=re.MULTILINE)
    out = re.sub(r"/\*.*?\*/", " ", out, flags=re.DOTALL)
    return out


def _sql_identifiers(sql: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _strip_sql_literals(sql).lower()))


def _sql_scope_error(sql: str, tank_id: int) -> str | None:
    """Return an error if the SELECT is not scoped to this tank."""
    visible = _strip_sql_literals(sql)
    ident = _sql_identifiers(sql)
    if ident & _BLOCKED_IDENTIFIERS or any(i.startswith("pragma") for i in ident):
        return "That table is not queryable."
    if ident & {"union", "except", "intersect"}:
        return "UNION/EXCEPT/INTERSECT are not allowed."
    tid = int(tank_id)
    needs_tank_id = bool(ident & _TANK_SCOPED_TABLES)
    for child, parent in _JOIN_SCOPED_TABLES.items():
        if child in ident:
            needs_tank_id = True
            if parent not in ident:
                return f"Queries on {child} must join {parent} and filter tank_id = {tid}."
    if needs_tank_id:
        if not re.search(rf"\btank_id\s*=\s*{tid}\b", visible, re.IGNORECASE):
            return f"Queries on tank tables must include tank_id = {tid}."
        other_ids = [int(x) for x in re.findall(r"\btank_id\s*=\s*(\d+)", visible, re.IGNORECASE)]
        if any(i != tid for i in other_ids):
            return "Queries cannot target another tank."
        if re.search(r"\btank_id\s*(?:not\s+in|in|!=|<>|>=|<=|>|<)", visible, re.IGNORECASE):
            return "Queries on tank tables must filter tank_id with '='."
        if re.search(rf"\btank_id\s*=\s*{tid}\s+or\b", visible, re.IGNORECASE):
            return "Queries on tank tables cannot OR against tank_id."
        if re.search(r"\bor\s+tank_id\b", visible, re.IGNORECASE):
            return "Queries on tank tables cannot OR against tank_id."
    if "tanks" in ident and not needs_tank_id:
        if not re.search(rf"\b(?:tanks\.)?id\s*=\s*{tid}\b", visible, re.IGNORECASE):
            return f"Queries on tanks must include id = {tid}."
    return None


def _sql_culture_scope_error(sql: str) -> str | None:
    """Return an error if the SELECT is not limited to culture tables."""
    ident = _sql_identifiers(sql)
    if ident & _BLOCKED_IDENTIFIERS or any(i.startswith("pragma") for i in ident):
        return "That table is not queryable."
    if ident & {"union", "except", "intersect"}:
        return "UNION/EXCEPT/INTERSECT are not allowed."
    blocked = ident & _NON_CULTURE_TABLES
    if blocked:
        return "Only culture tables are queryable."
    return None


def _run_query_db(sql: str, tank_id: int | None = None, *, scope: str = "tank") -> dict:
    stripped = (sql or "").strip().rstrip(";")
    if not re.match(r"(?is)^\s*select\b", stripped):
        return {"error": "Only SELECT statements are allowed."}
    if scope == "culture":
        scope_err = _sql_culture_scope_error(stripped)
        if scope_err:
            return {"error": scope_err}
    elif tank_id is not None:
        scope_err = _sql_scope_error(stripped, tank_id)
        if scope_err:
            return {"error": scope_err}
    try:
        with get_db_readonly() as conn:
            rows = conn.execute(stripped).fetchmany(QUERY_ROW_LIMIT)
            return {"rows": rows_to_list(rows)}
    except Exception as e:
        return {"error": str(e)}


def _query_db_tool_cultures():
    return {
        "name": "query_db",
        "description": (
            "Run a single read-only SQL SELECT against live-food culture tables for anything "
            "not already covered by the context above — e.g. full culture_log history, harvest "
            "totals, tint/density trends, schedule due dates. You may query ALL cultures "
            "(green water is grown as feed for live food). Do not query tanks or other "
            "non-culture tables. Returns up to "
            f"{QUERY_ROW_LIMIT} rows as JSON.\n\n"
            f"Schema:\n{get_schema_text(_CULTURE_ALLOWED_TABLES)}"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT statement."}},
            "required": ["sql"],
        },
    }


def _build_system_prompt(tank, latest_test, inhabitants, plants, hardscape, open_issues, summary,
                         recent_obs, schedule_rows=None, home_water_tests=None, goals=None):
    schedule_rows = schedule_rows or []
    home_water_tests = home_water_tests or []
    goals = goals or []
    parts = [
        "You are an expert aquarium keeper assistant with detailed knowledge of the following tank.",
        f"\nTank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}",
    ]

    if tank.get("manufacturer") or tank.get("model"):
        parts.append(f"Hardware: {(tank.get('manufacturer') or '')} {(tank.get('model') or '')}".strip())

    if tank.get("dimensions_l"):
        parts.append(f"Dimensions: {tank['dimensions_l']}\" × {tank['dimensions_w']}\" × {tank['dimensions_h']}\"")

    if tank.get("substrate_type"):
        sub = tank["substrate_type"]
        if tank.get("substrate_brand"):
            sub += f" ({tank['substrate_brand']})"
        if tank.get("substrate_depth_inches"):
            sub += f", {tank['substrate_depth_inches']}\""
        parts.append(f"Substrate: {sub}")

    if latest_test:
        params = []
        for field in ("ph", "gh", "kh", "ammonia", "nitrite", "nitrate", "tds", "temp"):
            val = latest_test.get(field)
            if val is not None:
                params.append(f"{field.upper()}={val}")
        if params:
            ts = (latest_test.get("timestamp") or "")[:10]
            parts.append(f"\nLatest Water Parameters ({ts}):\n  " + ", ".join(params))
    else:
        parts.append("\nLatest Water Parameters: none recorded")

    parts.append(
        "\nFill water for water changes (tap WC source and/or bottled only — "
        "NOT raw/diagnostic home-water samples):\n"
        + _fmt_home_water_block(home_water_tests)
    )
    parts.append(_HOME_WATER_PROMPT_RULE)

    if inhabitants:
        parts.append("\nInhabitants:\n" + _fmt_inhabitants(inhabitants))
    else:
        parts.append("\nInhabitants: none recorded")

    if plants:
        lines = ["  " + (p.get("common_name") or p.get("species") or "Unknown plant") for p in plants]
        parts.append("\nPlants:\n" + "\n".join(lines))

    if hardscape:
        lines = []
        for h in hardscape:
            qty = h.get("quantity") or 1
            prefix = f"{qty}× " if qty > 1 else ""
            lines.append(f"  {prefix}{h['item']}")
        parts.append("\nHardscape:\n" + "\n".join(lines))

    if open_issues:
        lines = [f"  [{i['status'].upper()}] {i['title']}: {i.get('description','')}" for i in open_issues]
        parts.append("\nOpen Issues:\n" + "\n".join(lines))

    if goals:
        parts.append("\nActive Goals:\n" + _fmt_goals(goals))

    parts.append(
        "\nRecurring schedule (current planned feeding/dosing/maintenance — authoritative for "
        "what the keeper currently does):\n" + _fmt_schedule(schedule_rows)
    )

    if summary and summary.get("summary_text"):
        parts.append(f"\nRecent AI Summary:\n{summary['summary_text']}")

    if recent_obs:
        parts.append("\nRecent Observations:")
        for obs in recent_obs:
            ts = (obs.get("created_at") or "")[:10]
            parts.append(f"  [{obs['source']}] {ts}: {obs['text'][:200]}")

    parts.append("\n" + _CURRENT_PRACTICES_RULE)
    parts.append(
        "\nThe context above is a snapshot (current inhabitants, latest test, recent items only). "
        "Use the query_db tool whenever a question needs history or data beyond this snapshot — "
        "e.g. 'when was X added', 'GH trend/history', 'how much have I spent on Y' — rather than "
        "saying the data isn't available."
    )
    parts.append(
        "\nConversation style:\n"
        "- Answer helpfully and concisely in plain text only — no markdown (no **bold**, no *italic*, "
        "no headers, no bullet dashes).\n"
        "- This is a multi-turn conversation. Prior user and assistant messages are already in the "
        "thread; the user has just read them.\n"
        "- On follow-ups, answer the new question directly. Do not restate conclusions, timelines, "
        "or tank facts you already covered unless the user asks for a recap or you need one short "
        "reference to support a new point.\n"
        "- Never open with meta filler (e.g. \"Good question\", \"That gives me a full picture\", "
        "\"Based on everything above\", \"Here is a comprehensive answer\"). Jump straight into the "
        "answer.\n"
        "- Prefer a natural continuation over a standalone re-briefing. Use snapshot data when it "
        "changes the answer; do not re-list inventory, re-summarize water params, or re-walk prior "
        "reasoning by default.\n"
        "- Equipment and named products (heaters, lights, wattage vs volume) are in scope. You cannot "
        "browse listings or verify a SKU; do not refuse or say that is outside what you can do. "
        "Answer feasibility from general knowledge plus the snapshot."
    )
    return "\n".join(parts)


def _require_tank(conn, tank_id: int) -> dict:
    tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
    if not tank:
        raise HTTPException(status_code=404, detail="Tank not found")
    return tank


def _get_conversation(conn, tank_id: int, conversation_id: int) -> dict:
    conv = row_to_dict(conn.execute(
        "SELECT * FROM chat_conversations WHERE id = ? AND tank_id = ?",
        (conversation_id, tank_id),
    ).fetchone())
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def _load_messages(conn, conversation_id: int) -> list[dict]:
    return rows_to_list(conn.execute(
        "SELECT id, role, content, created_at FROM chat_messages "
        "WHERE conversation_id = ? ORDER BY id ASC",
        (conversation_id,),
    ).fetchall())


def _gather_tank_context(conn, tank_id: int) -> dict:
    return {
        "summary": row_to_dict(conn.execute(
            "SELECT summary_text FROM tank_state_summary WHERE tank_id = ?", (tank_id,),
        ).fetchone()),
        "latest_test": row_to_dict(conn.execute(
            "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 1",
            (tank_id,),
        ).fetchone()),
        "inhabitants": rows_to_list(conn.execute(
            "SELECT common_name, species, count, added_date FROM inhabitants WHERE tank_id = ? ORDER BY common_name, species",
            (tank_id,),
        ).fetchall()),
        "plants": rows_to_list(conn.execute(
            "SELECT common_name, species FROM plants WHERE tank_id = ? AND status = 'active'",
            (tank_id,),
        ).fetchall()),
        "hardscape": rows_to_list(conn.execute(
            "SELECT item, quantity FROM hardscape WHERE tank_id = ?",
            (tank_id,),
        ).fetchall()),
        "open_issues": rows_to_list(conn.execute(
            "SELECT title, description, status FROM issues WHERE tank_id = ? AND status != 'resolved'",
            (tank_id,),
        ).fetchall()),
        "goals": load_active_goals(conn, tank_id),
        "recent_obs": rows_to_list(conn.execute(
            "SELECT text, source, created_at FROM observations WHERE tank_id = ? ORDER BY created_at DESC LIMIT 5",
            (tank_id,),
        ).fetchall()),
        "schedule_rows": rows_to_list(conn.execute(
            "SELECT * FROM recurring_schedule WHERE tank_id = ? AND is_active = 1",
            (tank_id,),
        ).fetchall()),
        "home_water_tests": load_home_water_tests(conn),
    }


def _label(mapping, key, fallback=""):
    if not key:
        return fallback
    return mapping.get(key, key)


def _fmt_bench_air(row):
    if not row:
        return "  None recorded."
    ts = (row.get("timestamp") or "")[:16]
    bits = []
    if row.get("temp_f") is not None:
        bits.append(f"{row['temp_f']}°F")
    if row.get("temp_low") is not None and row.get("temp_high") is not None:
        bits.append(f"({row['temp_low']}–{row['temp_high']})")
    if row.get("rh") is not None:
        bits.append(f"{int(row['rh'])}% relative humidity")
    if row.get("rh_low") is not None and row.get("rh_high") is not None:
        bits.append(f"({int(row['rh_low'])}–{int(row['rh_high'])})")
    return f"  {ts}: " + (" ".join(bits) if bits else "(no numeric readings)")


def _fmt_culture_log(rows):
    if not rows:
        return "  No log yet."
    lines = []
    for r in rows:
        ts = (r.get("timestamp") or "")[:16]
        kind = _label(KIND_LABELS, r.get("kind"), r.get("kind") or "log")
        bits = [f"  {ts} [{kind}]"]
        if r.get("held"):
            bits.append("held")
        if r.get("food"):
            bits.append(_label(FOOD_LABELS, r["food"], r["food"]))
        if r.get("amount_text"):
            bits.append(r["amount_text"])
        if r.get("tint"):
            bits.append("tint " + _label(TINT_LABELS, r["tint"], r["tint"]))
        if r.get("density"):
            bits.append("density " + _label(DENSITY_LABELS, r["density"], r["density"]))
        if r.get("guts"):
            bits.append("guts " + _label(GUTS_LABELS, r["guts"], r["guts"]))
        if r.get("temp_f") is not None:
            bits.append(f"{r['temp_f']}°F")
        if r.get("vessel_names"):
            bits.append(r["vessel_names"])
        line = " · ".join(bits)
        if r.get("notes"):
            note = " ".join(str(r["notes"]).split())
            line += f" — {note[:160]}"
        bins = r.get("bins") or []
        bin_bits = []
        for b in bins:
            inner = []
            if b.get("tint"):
                inner.append(_label(TINT_LABELS, b["tint"], b["tint"]))
            if b.get("density"):
                inner.append(_label(DENSITY_LABELS, b["density"], b["density"]))
            if b.get("guts"):
                inner.append("guts " + _label(GUTS_LABELS, b["guts"], b["guts"]))
            if b.get("amount_text"):
                inner.append(b["amount_text"])
            if b.get("temp_f") is not None:
                inner.append(f"{b['temp_f']}°F")
            if inner:
                bin_bits.append(f"{b.get('vessel_name') or 'bin'}: {', '.join(inner)}")
        if bin_bits:
            line += " (" + "; ".join(bin_bits) + ")"
        lines.append(line)
    return "\n".join(lines)


def _fmt_culture_schedule(rows):
    if not rows:
        return "  No recurring schedule."
    lines = []
    for r in rows:
        cat = _label(CATEGORY_LABELS, r.get("category"), r.get("category") or "")
        desc = r.get("description") or ""
        extra = f" (bin: {r['vessel_name']})" if r.get("vessel_name") else ""
        if r.get("tracking_mode") == "logged":
            interval = r.get("interval_days") or "?"
            last_done = r.get("last_done") or "never"
            next_due = r.get("next_due") or "not set"
            line = f"  [{cat}] {desc}{extra} — every {interval} days, last done {last_done}, next due {next_due}"
        else:
            line = f"  [{cat}] {desc}{extra} — reference only"
        if r.get("notes"):
            line += f" — notes: {r['notes']}"
        lines.append(line)
    return "\n".join(lines)


def _fmt_culture_station(station, current_id=None):
    c = station["culture"]
    kind = _label(CULTURE_KIND_LABELS, c.get("kind"), c.get("kind") or "other")
    viewing = " — currently viewing" if current_id is not None and c.get("id") == current_id else ""
    lines = [f"\nStation: {c.get('name')} (id={c.get('id')}, {kind}, {c.get('status') or 'active'}){viewing}"]
    harvest = _label(HARVEST_STATUS_LABELS, c.get("harvest_status"), c.get("harvest_status") or "")
    if harvest:
        lines.append(f"  Harvest status: {harvest}")
    dest = c.get("destination_label")
    if dest:
        kind_dest = c.get("destination_kind") or "destination"
        if kind_dest == "tank":
            lines.append(
                f"  Harvest destination: {dest} (display tank name only — no tank chemistry or livestock)"
            )
        else:
            lines.append(f"  Harvest destination: {dest} ({kind_dest})")
    else:
        lines.append("  Harvest destination: none")
    if c.get("isolation_notes"):
        lines.append(f"  Isolation: {c['isolation_notes']}")
    if c.get("notes"):
        lines.append(f"  Notes: {c['notes']}")
    if (c.get("next_action") or "").strip():
        nxt = c["next_action"].strip()
        if c.get("next_action_date"):
            nxt += f" ({c['next_action_date']})"
        lines.append(f"  One-off next action: {nxt}")

    vessels = station.get("vessels") or []
    if vessels:
        lines.append("  Bins:")
        for v in vessels:
            bits = [v.get("name") or "bin"]
            bits.append("lit" if v.get("is_lit") else "unlit")
            if v.get("is_heated"):
                heat = "heated"
                if v.get("heater_set_f") is not None:
                    heat += f" {v['heater_set_f']}°F"
                bits.append(heat)
            else:
                bits.append("unheated")
            if v.get("volume_gallons") is not None:
                bits.append(f"{v['volume_gallons']}g")
            if v.get("status") and v["status"] != "active":
                bits.append(v["status"])
            obs = []
            if v.get("last_tint"):
                obs.append("tint " + _label(TINT_LABELS, v["last_tint"], v["last_tint"]))
            if v.get("last_density"):
                obs.append(_label(DENSITY_LABELS, v["last_density"], v["last_density"]))
            if v.get("last_guts"):
                obs.append("guts " + _label(GUTS_LABELS, v["last_guts"], v["last_guts"]))
            if v.get("last_temp_f") is not None:
                obs.append(f"{v['last_temp_f']}°F")
            if obs:
                bits.append("last look " + ", ".join(obs))
            if v.get("last_feed_at"):
                feed = f"last feed {(v['last_feed_at'] or '')[:16]}"
                if v.get("last_feed_food"):
                    feed += " " + _label(FOOD_LABELS, v["last_feed_food"], v["last_feed_food"])
                if v.get("last_feed_amount"):
                    feed += f" {v['last_feed_amount']}"
                bits.append(feed)
            if v.get("hitchhikers"):
                bits.append("hitchhikers: " + v["hitchhikers"])
            if v.get("notes"):
                bits.append("notes: " + v["notes"])
            lines.append("    - " + ", ".join(bits))
    else:
        lines.append("  Bins: none")

    lines.append("  Schedule:\n" + _fmt_culture_schedule(station.get("schedule") or []))
    lines.append("  Recent log:\n" + _fmt_culture_log(station.get("recent_logs") or []))
    return "\n".join(lines)


def _gather_cultures_context(conn) -> dict:
    cultures = rows_to_list(conn.execute(
        _CULTURE_SELECT +
        " ORDER BY CASE c.status WHEN 'active' THEN 0 ELSE 1 END, c.name"
    ).fetchall())
    stations = []
    for raw in cultures:
        culture = _with_destination(raw)
        recent_logs = rows_to_list(conn.execute(
            """SELECT l.*, group_concat(v.name, ', ') AS vessel_names
               FROM culture_log l
               LEFT JOIN culture_log_vessels lv ON lv.log_id = l.id
               LEFT JOIN culture_vessels v ON v.id = lv.vessel_id
               WHERE l.culture_id = ?
               GROUP BY l.id
               ORDER BY l.timestamp DESC, l.id DESC
               LIMIT 8""",
            (culture["id"],),
        ).fetchall())
        _attach_log_bins(conn, recent_logs)
        stations.append({
            "culture": culture,
            "vessels": _vessels(conn, culture["id"]),
            "schedule": rows_to_list(conn.execute(
                """SELECT s.*, v.name AS vessel_name
                   FROM culture_schedule s
                   LEFT JOIN culture_vessels v ON v.id = s.vessel_id
                   WHERE s.culture_id = ? AND s.is_active = 1
                   ORDER BY s.tracking_mode DESC, s.category, s.description""",
                (culture["id"],),
            ).fetchall()),
            "recent_logs": recent_logs,
        })
    return {
        "stations": stations,
        "bench_air": _latest_bench_air(conn),
    }


def _build_culture_system_prompt(current, stations, bench_air=None):
    kind = _label(CULTURE_KIND_LABELS, current.get("kind"), current.get("kind") or "other")
    parts = [
        "You are an expert live-food culture keeper assistant (Daphnia, green water, and similar stations).",
        "You have data for ALL culture stations in this household, not only the one the user is viewing.",
        "Green water is grown as feed for live food (for example Daphnia); a harvest can go to another "
        "culture or bin. Green-water cultures are not fed; Daphnia cultures are.",
        "Do not discuss display tanks, tank water chemistry, tank livestock, or home-water tests. "
        "If asked about a tank, say this chat is for cultures only.",
        "Equipment for these stations is in scope (heaters, lights, named products, wattage vs "
        "bin volume). You cannot look up listings; still answer from general knowledge and the "
        "snapshot. Do not refuse a product question as out of scope.",
        f"\nThe user is currently viewing: {current.get('name')} (id={current.get('id')}, {kind}).",
        "\nLatest bench air (shared culture-station environment, not a tank):\n"
        + _fmt_bench_air(bench_air),
    ]
    if not stations:
        parts.append("\nNo culture stations recorded.")
    else:
        for station in stations:
            parts.append(_fmt_culture_station(station, current_id=current.get("id")))
    parts.append(
        "\nThe context above is a snapshot (current bins, recent log, active schedule). "
        "Use the query_db tool whenever a question needs history or data beyond this snapshot — "
        "e.g. 'when did I last harvest', 'tint history', 'how often have I held feeding' — rather "
        "than saying the data isn't available. You may query every culture, including stations "
        "other than the one currently being viewed."
    )
    parts.append(_CONVERSATION_STYLE)
    return "\n".join(parts)


def _get_culture_conversation(conn, culture_id: int, conversation_id: int) -> dict:
    conv = row_to_dict(conn.execute(
        "SELECT * FROM chat_conversations WHERE id = ? AND culture_id = ?",
        (conversation_id, culture_id),
    ).fetchone())
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


_NO_REPLY_FALLBACK = (
    "I wasn't able to generate a reply. Try asking again, or a narrower question."
)


async def _claude_chat_create(client, *, system, messages, log_label, tools=None, thinking=None):
    kwargs = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS_CHAT,
        "system": system,
        "messages": messages,
        "timeout": _CLAUDE_TIMEOUT,
    }
    if tools:
        kwargs["tools"] = tools
    if thinking is not None:
        kwargs["thinking"] = thinking
    attempt = "no_thinking" if thinking else "adaptive"
    logger.info("Claude call: %s | thinking=%s", log_label, attempt)
    t0 = time.monotonic()
    response = await asyncio.to_thread(client.messages.create, **kwargs)
    usage = getattr(response, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) if usage else 0
    out_tok = getattr(usage, "output_tokens", 0) if usage else 0
    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        logger.warning(
            "Claude %s hit max_tokens (thinking=%s) — response may be truncated",
            log_label, attempt,
        )
    logger.info(
        "Claude done: %s | thinking=%s | in=%d out=%d elapsed=%.1fs stop=%s",
        log_label, attempt, in_tok, out_tok, time.monotonic() - t0, stop,
    )
    return response


def _apply_query_db_tools(response, run_tool, working_messages, log_label):
    working_messages.append({"role": "assistant", "content": response.content})
    tool_results = []
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        sql = (getattr(block, "input", None) or {}).get("sql", "")
        logger.info("Chat tool call: %s | query_db: %s", log_label, sql)
        result = run_tool(sql)
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(result, default=str),
        })
    working_messages.append({"role": "user", "content": tool_results})


async def _claude_chat_reply(*, api_key, system_prompt, tools, api_history, run_tool, log_label):
    """Tool loop with Sonnet 5 adaptive-thinking retry.

    Adaptive thinking shares max_tokens with the visible reply. A diagnostic
    question can burn the whole budget on a ThinkingBlock (prod 2026-08-24:
    Fish Tank Otos Ask AI, out=1024, no text) and look like a tool-round miss.
    Empty text → one thinking-disabled retry. After MAX_TOOL_ROUNDS, a further
    call is made with tools omitted so the model has to answer from what it has.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    working_messages = list(api_history)
    raw = ""
    tool_rounds_used = 0
    thinking_retry = False
    t0 = time.monotonic()
    for _ in range(_MAX_CHAT_API_CALLS):
        use_tools = tools if tool_rounds_used < MAX_TOOL_ROUNDS else None
        thinking = CLAUDE_THINKING_DISABLED if thinking_retry else None
        response = await _claude_chat_create(
            client,
            system=system_prompt,
            messages=working_messages,
            log_label=log_label,
            tools=use_tools,
            thinking=thinking,
        )
        if response.stop_reason == "tool_use" and use_tools:
            _apply_query_db_tools(response, run_tool, working_messages, log_label)
            tool_rounds_used += 1
            thinking_retry = False
            continue

        raw = _message_text(response)
        if raw:
            if thinking_retry:
                logger.info("Claude %s recovered via thinking-disabled retry", log_label)
            break
        if not thinking_retry:
            logger.warning(
                "Claude %s returned no text (stop=%s); retrying with thinking disabled",
                log_label, getattr(response, "stop_reason", None),
            )
            thinking_retry = True
            continue
        raw = _NO_REPLY_FALLBACK
        break
    else:
        if not raw:
            raw = _NO_REPLY_FALLBACK

    logger.info(
        "Chat turn complete: %s | tool_rounds=%d elapsed=%.1fs",
        log_label, tool_rounds_used, time.monotonic() - t0,
    )
    reply = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', raw)
    reply = re.sub(r'^#{1,6}\s+', '', reply, flags=re.MULTILINE)
    return reply


def _prepare_conversation(conn, *, owner_col, owner_id, conversation_id, message, now):
    """Create or load a conversation, insert the user message, return ids + API history."""
    created_new = False
    if conversation_id is not None:
        conv = row_to_dict(conn.execute(
            f"SELECT * FROM chat_conversations WHERE id = ? AND {owner_col} = ?",
            (conversation_id, owner_id),
        ).fetchone())
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_id = conv["id"]
    else:
        cur = conn.execute(
            f"INSERT INTO chat_conversations ({owner_col}, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (owner_id, _title_from_message(message), now, now),
        )
        conversation_id = cur.lastrowid
        created_new = True

    if not created_new:
        msg_count = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()["n"]
        if msg_count == 0:
            conn.execute(
                "UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
                (_title_from_message(message), now, conversation_id),
            )
        else:
            conn.execute(
                "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )

    conn.execute(
        "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
        (conversation_id, message, now),
    )
    history_rows = _load_messages(conn, conversation_id)
    api_history = [{"role": r["role"], "content": r["content"]} for r in history_rows]
    if len(api_history) > MAX_TURNS * 2:
        api_history = api_history[-(MAX_TURNS * 2):]
    title = conn.execute(
        "SELECT title FROM chat_conversations WHERE id = ?", (conversation_id,)
    ).fetchone()["title"]
    return conversation_id, api_history, title


def _store_assistant_reply(conn, conversation_id, reply, done_at):
    conn.execute(
        "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
        (conversation_id, reply, done_at),
    )
    conn.execute(
        "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
        (done_at, conversation_id),
    )
    turn_count = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = ? AND role = 'user'",
        (conversation_id,),
    ).fetchone()["n"]
    title = conn.execute(
        "SELECT title FROM chat_conversations WHERE id = ?", (conversation_id,)
    ).fetchone()["title"]
    return turn_count, title


# ── Full-page views (left-nav) ──────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
async def new_chat_page(request: Request, tank_id: int):
    with get_db() as conn:
        tank = _require_tank(conn, tank_id)
    return templates.TemplateResponse(request, "chat/page.html", {
        "tank": tank,
        "conversation": None,
        "messages": [],
        "active": "chat",
        "active_conversation_id": None,
    })


@router.get("/c/{conversation_id}", response_class=HTMLResponse)
async def conversation_page(request: Request, tank_id: int, conversation_id: int):
    with get_db() as conn:
        tank = _require_tank(conn, tank_id)
        conv = _get_conversation(conn, tank_id, conversation_id)
        messages = _load_messages(conn, conversation_id)
    return templates.TemplateResponse(request, "chat/page.html", {
        "tank": tank,
        "conversation": conv,
        "messages": messages,
        "active": "chat",
        "active_conversation_id": conv["id"],
    })


# ── JSON API ────────────────────────────────────────────────────────────────

@router.get("/conversations")
async def list_conversations(tank_id: int):
    with get_db() as conn:
        _require_tank(conn, tank_id)
        rows = rows_to_list(conn.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS message_count
               FROM chat_conversations c
               WHERE c.tank_id = ?
               ORDER BY c.updated_at DESC, c.id DESC""",
            (tank_id,),
        ).fetchall())
    return JSONResponse({"conversations": rows})


@router.post("/conversations")
async def create_conversation(tank_id: int):
    now = _utc_now()
    with get_db() as conn:
        _require_tank(conn, tank_id)
        cur = conn.execute(
            "INSERT INTO chat_conversations (tank_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (tank_id, "New conversation", now, now),
        )
        conv_id = cur.lastrowid
        conv = row_to_dict(conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_conversations WHERE id = ?",
            (conv_id,),
        ).fetchone())
    conv["message_count"] = 0
    conv["messages"] = []
    return JSONResponse(conv, status_code=201)


@router.get("/conversations/{conversation_id}")
async def get_conversation(tank_id: int, conversation_id: int):
    with get_db() as conn:
        _require_tank(conn, tank_id)
        conv = _get_conversation(conn, tank_id, conversation_id)
        messages = _load_messages(conn, conversation_id)
    return JSONResponse({
        "id": conv["id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "messages": messages,
    })


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(tank_id: int, conversation_id: int):
    with get_db() as conn:
        _require_tank(conn, tank_id)
        _get_conversation(conn, tank_id, conversation_id)
        conn.execute("DELETE FROM chat_conversations WHERE id = ?", (conversation_id,))
    return JSONResponse({"status": "deleted", "id": conversation_id})


@router.post("")
async def chat(tank_id: int, body: ChatMessage, request: Request):
    require_ai_budget(request)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="AI features require ANTHROPIC_API_KEY")

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    if len(message) > MAX_CHAT_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail="Message is too long")

    now = _utc_now()
    with get_db() as conn:
        tank = _require_tank(conn, tank_id)
        ctx = _gather_tank_context(conn, tank_id)

        created_new = False
        if body.conversation_id is not None:
            conv = _get_conversation(conn, tank_id, body.conversation_id)
            conversation_id = conv["id"]
        else:
            cur = conn.execute(
                "INSERT INTO chat_conversations (tank_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (tank_id, _title_from_message(message), now, now),
            )
            conversation_id = cur.lastrowid
            created_new = True

        # Title empty/new conversations from the first user message
        if not created_new:
            msg_count = conn.execute(
                "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["n"]
            if msg_count == 0:
                conn.execute(
                    "UPDATE chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (_title_from_message(message), now, conversation_id),
                )
            else:
                conn.execute(
                    "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
                    (now, conversation_id),
                )

        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (conversation_id, message, now),
        )

        history_rows = _load_messages(conn, conversation_id)
        # Keep full history for display/storage; only send the last N turns to Claude
        api_history = [
            {"role": r["role"], "content": r["content"]}
            for r in history_rows
        ]
        if len(api_history) > MAX_TURNS * 2:
            api_history = api_history[-(MAX_TURNS * 2):]

        title = conn.execute(
            "SELECT title FROM chat_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()["title"]

    system_prompt = _build_system_prompt(
        tank, ctx["latest_test"], ctx["inhabitants"], ctx["plants"], ctx["hardscape"],
        ctx["open_issues"], ctx["summary"], ctx["recent_obs"], ctx["schedule_rows"],
        home_water_tests=ctx.get("home_water_tests"), goals=ctx.get("goals"),
    )
    logger.info("Chat system prompt for tank %d conv %d: %d chars", tank_id, conversation_id, len(system_prompt))

    tools = [_query_db_tool(tank_id)]

    try:
        logger.info(
            "Claude call: chat | tank=%d conv=%d turn=%d",
            tank_id, conversation_id, len(api_history) // 2,
        )
        reply = await _claude_chat_reply(
            api_key=api_key,
            system_prompt=system_prompt,
            tools=tools,
            api_history=api_history,
            run_tool=lambda sql: _run_query_db(sql, tank_id),
            log_label=f"chat | tank={tank_id} conv={conversation_id}",
        )
    except Exception as e:
        # Roll back the user message if the AI call failed on a brand-new empty conv we just made,
        # otherwise leave the user message (they can retry). On failure after insert, still store nothing
        # for the assistant and keep the user message for continuity.
        logger.error("Chat error for tank %d conv %d: %s", tank_id, conversation_id, e)
        raise HTTPException(status_code=500, detail="AI error")

    done_at = _utc_now()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
            (conversation_id, reply, done_at),
        )
        conn.execute(
            "UPDATE chat_conversations SET updated_at = ? WHERE id = ?",
            (done_at, conversation_id),
        )
        turn_count = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE conversation_id = ? AND role = 'user'",
            (conversation_id,),
        ).fetchone()["n"]
        title = conn.execute(
            "SELECT title FROM chat_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()["title"]

    return JSONResponse({
        "reply": reply,
        "turns": turn_count,
        "conversation_id": conversation_id,
        "title": title,
    })


# ── Culture Ask AI (all stations, no tank data) ─────────────────────────────

@culture_router.get("/new", response_class=HTMLResponse)
async def new_culture_chat_page(request: Request, culture_id: int):
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
    return templates.TemplateResponse(request, "chat/page.html", {
        "culture": culture,
        "conversation": None,
        "messages": [],
        "active": "chat",
        "active_conversation_id": None,
    })


@culture_router.get("/c/{conversation_id}", response_class=HTMLResponse)
async def culture_conversation_page(request: Request, culture_id: int, conversation_id: int):
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
        conv = _get_culture_conversation(conn, culture_id, conversation_id)
        messages = _load_messages(conn, conversation_id)
    return templates.TemplateResponse(request, "chat/page.html", {
        "culture": culture,
        "conversation": conv,
        "messages": messages,
        "active": "chat",
        "active_conversation_id": conv["id"],
    })


@culture_router.get("/conversations")
async def list_culture_conversations(culture_id: int):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        rows = rows_to_list(conn.execute(
            """SELECT c.id, c.title, c.created_at, c.updated_at,
                      (SELECT COUNT(*) FROM chat_messages m WHERE m.conversation_id = c.id) AS message_count
               FROM chat_conversations c
               WHERE c.culture_id = ?
               ORDER BY c.updated_at DESC, c.id DESC""",
            (culture_id,),
        ).fetchall())
    return JSONResponse({"conversations": rows})


@culture_router.post("/conversations")
async def create_culture_conversation(culture_id: int):
    now = _utc_now()
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        cur = conn.execute(
            "INSERT INTO chat_conversations (culture_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (culture_id, "New conversation", now, now),
        )
        conv_id = cur.lastrowid
        conv = row_to_dict(conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_conversations WHERE id = ?",
            (conv_id,),
        ).fetchone())
    conv["message_count"] = 0
    conv["messages"] = []
    return JSONResponse(conv, status_code=201)


@culture_router.get("/conversations/{conversation_id}")
async def get_culture_conversation(culture_id: int, conversation_id: int):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        conv = _get_culture_conversation(conn, culture_id, conversation_id)
        messages = _load_messages(conn, conversation_id)
    return JSONResponse({
        "id": conv["id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "messages": messages,
    })


@culture_router.delete("/conversations/{conversation_id}")
async def delete_culture_conversation(culture_id: int, conversation_id: int):
    with get_db() as conn:
        _culture_or_404(conn, culture_id)
        _get_culture_conversation(conn, culture_id, conversation_id)
        conn.execute("DELETE FROM chat_conversations WHERE id = ?", (conversation_id,))
    return JSONResponse({"status": "deleted", "id": conversation_id})


@culture_router.post("")
async def culture_chat(culture_id: int, body: ChatMessage, request: Request):
    require_ai_budget(request)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="AI features require ANTHROPIC_API_KEY")

    message = (body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")
    if len(message) > MAX_CHAT_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail="Message is too long")

    now = _utc_now()
    with get_db() as conn:
        culture = _culture_or_404(conn, culture_id)
        ctx = _gather_cultures_context(conn)
        conversation_id, api_history, title = _prepare_conversation(
            conn,
            owner_col="culture_id",
            owner_id=culture_id,
            conversation_id=body.conversation_id,
            message=message,
            now=now,
        )

    system_prompt = _build_culture_system_prompt(
        culture, ctx["stations"], bench_air=ctx.get("bench_air"),
    )
    logger.info(
        "Chat system prompt for culture %d conv %d: %d chars",
        culture_id, conversation_id, len(system_prompt),
    )
    tools = [_query_db_tool_cultures()]

    try:
        logger.info(
            "Claude call: culture-chat | culture=%d conv=%d turn=%d",
            culture_id, conversation_id, len(api_history) // 2,
        )
        reply = await _claude_chat_reply(
            api_key=api_key,
            system_prompt=system_prompt,
            tools=tools,
            api_history=api_history,
            run_tool=lambda sql: _run_query_db(sql, scope="culture"),
            log_label=f"culture-chat | culture={culture_id}",
        )
    except Exception as e:
        logger.error("Chat error for culture %d conv %d: %s", culture_id, conversation_id, e)
        raise HTTPException(status_code=500, detail="AI error")

    done_at = _utc_now()
    with get_db() as conn:
        turn_count, title = _store_assistant_reply(conn, conversation_id, reply, done_at)

    return JSONResponse({
        "reply": reply,
        "turns": turn_count,
        "conversation_id": conversation_id,
        "title": title,
    })
