"""Tank goals with optional cross-tank dependencies.

Goals track longer-horizon aims (water params, stocking, breeding) distinct from
issues (problems). A goal can depend on other goals — including ones on a
different tank — so multi-tank plans like "breed Neocaridina in tank A, then
move them once tank B's GH is ready" are first-class.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from database import get_db, row_to_dict, rows_to_list

router = APIRouter(prefix="/tanks/{tank_id}/goals", tags=["goals"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

VALID_STATUSES = frozenset({"open", "in_progress", "paused", "achieved", "abandoned"})
# Worked on / shown with AI progress updates
ACTIVE_STATUSES = frozenset({"open", "in_progress"})
# Shown on dashboard (includes paused so nothing disappears when held)
VISIBLE_STATUSES = frozenset({"open", "in_progress", "paused"})

# Status sort: active work first, then paused, then done, then abandoned
_STATUS_ORDER = (
    "CASE g.status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1 "
    "WHEN 'paused' THEN 2 WHEN 'achieved' THEN 3 ELSE 4 END"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _date_or_now(date_str: Optional[str]) -> str:
    if date_str:
        return f"{date_str} 12:00:00"
    return _now_utc()


def _parse_dep_ids(depends_on: Optional[List[str]]) -> list[int]:
    if not depends_on:
        return []
    ids: list[int] = []
    for raw in depends_on:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            ids.append(int(raw))
        except ValueError:
            continue
    # Preserve order, drop dups
    seen: set[int] = set()
    out: list[int] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _would_create_cycle(conn, goal_id: int, dep_ids: list[int]) -> bool:
    """True if setting goal_id's deps to dep_ids would introduce a cycle."""
    if not dep_ids:
        return False
    # Build adjacency: for every goal except goal_id, keep existing outs;
    # for goal_id use the proposed dep_ids.
    rows = conn.execute(
        "SELECT goal_id, depends_on_goal_id FROM goal_dependencies"
    ).fetchall()
    graph: dict[int, list[int]] = {}
    for r in rows:
        g, d = r[0], r[1]
        if g == goal_id:
            continue
        graph.setdefault(g, []).append(d)
    graph[goal_id] = list(dep_ids)

    # Can we reach goal_id starting from any of its deps?
    stack = list(dep_ids)
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if node == goal_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return False


def _set_dependencies(conn, goal_id: int, dep_ids: list[int]) -> None:
    if goal_id in dep_ids:
        raise HTTPException(status_code=400, detail="A goal cannot depend on itself")
    if dep_ids:
        existing = {
            r[0]
            for r in conn.execute(
                f"SELECT id FROM goals WHERE id IN ({','.join('?' * len(dep_ids))})",
                dep_ids,
            ).fetchall()
        }
        missing = [i for i in dep_ids if i not in existing]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown dependency goal id(s): {missing}")
        if _would_create_cycle(conn, goal_id, dep_ids):
            raise HTTPException(status_code=400, detail="Dependencies would create a cycle")
    conn.execute("DELETE FROM goal_dependencies WHERE goal_id = ?", (goal_id,))
    for dep_id in dep_ids:
        conn.execute(
            "INSERT INTO goal_dependencies (goal_id, depends_on_goal_id) VALUES (?, ?)",
            (goal_id, dep_id),
        )


def _deps_for_goals(conn, goal_ids: list[int]) -> dict[int, list[dict]]:
    """Map goal_id → list of dependency goal dicts (with tank_name)."""
    if not goal_ids:
        return {}
    placeholders = ",".join("?" * len(goal_ids))
    rows = rows_to_list(conn.execute(
        f"""SELECT gd.goal_id AS owner_id,
                   d.id, d.tank_id, d.title, d.status, d.target,
                   t.name AS tank_name
            FROM goal_dependencies gd
            JOIN goals d ON d.id = gd.depends_on_goal_id
            JOIN tanks t ON t.id = d.tank_id
            WHERE gd.goal_id IN ({placeholders})
            ORDER BY t.name, d.title""",
        goal_ids,
    ).fetchall())
    by_owner: dict[int, list[dict]] = {gid: [] for gid in goal_ids}
    for r in rows:
        owner = r.pop("owner_id")
        by_owner.setdefault(owner, []).append(r)
    return by_owner


def _enrich_goals(conn, goals: list[dict]) -> list[dict]:
    """Attach dependencies + blocked flag to each goal dict."""
    deps_map = _deps_for_goals(conn, [g["id"] for g in goals])
    for g in goals:
        deps = deps_map.get(g["id"], [])
        g["dependencies"] = deps
        g["blocked"] = any(d.get("status") != "achieved" for d in deps)
        g["unmet_deps"] = [d for d in deps if d.get("status") != "achieved"]
    return goals


def load_active_goals(conn, tank_id: int) -> list[dict]:
    """Goals still being worked on (for AI prompts / progress updates)."""
    goals = rows_to_list(conn.execute(
        f"""SELECT g.* FROM goals g
            WHERE g.tank_id = ? AND g.status IN ('open', 'in_progress')
            ORDER BY {_STATUS_ORDER}, g.sort_order, g.opened_at""",
        (tank_id,),
    ).fetchall())
    return _enrich_goals(conn, goals)


def load_dashboard_goals(conn, tank_id: int) -> list[dict]:
    """In-progress + paused goals for the tank dashboard panel."""
    goals = rows_to_list(conn.execute(
        f"""SELECT g.* FROM goals g
            WHERE g.tank_id = ? AND g.status IN ('open', 'in_progress', 'paused')
            ORDER BY {_STATUS_ORDER}, g.sort_order, g.opened_at""",
        (tank_id,),
    ).fetchall())
    return _enrich_goals(conn, goals)


def load_all_goals_for_picker(conn, exclude_goal_id: Optional[int] = None) -> list[dict]:
    """All non-abandoned goals across tanks, for the dependency multi-select."""
    sql = """SELECT g.id, g.tank_id, g.title, g.status, g.target, t.name AS tank_name
             FROM goals g JOIN tanks t ON t.id = g.tank_id
             WHERE g.status != 'abandoned'"""
    params: list = []
    if exclude_goal_id is not None:
        sql += " AND g.id != ?"
        params.append(exclude_goal_id)
    sql += (
        " ORDER BY t.name, CASE g.status WHEN 'in_progress' THEN 0 WHEN 'open' THEN 1 "
        "WHEN 'paused' THEN 2 ELSE 3 END, g.title"
    )
    return rows_to_list(conn.execute(sql, params).fetchall())


@router.get("", response_class=HTMLResponse)
async def list_goals(request: Request, tank_id: int, background_tasks: BackgroundTasks):
    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        goals = rows_to_list(conn.execute(
            f"""SELECT g.* FROM goals g
                WHERE g.tank_id = ?
                ORDER BY {_STATUS_ORDER}, g.sort_order, g.opened_at DESC""",
            (tank_id,),
        ).fetchall())
        goals = _enrich_goals(conn, goals)
        picker_goals = load_all_goals_for_picker(conn)
        # Group picker by tank for the template
        picker_by_tank: dict[str, list[dict]] = {}
        for pg in picker_goals:
            picker_by_tank.setdefault(pg["tank_name"], []).append(pg)

        # Backfill AI progress for active goals that never got a summary (e.g. failed
        # before the progress_summary migration, or API blip on create).
        needs_progress = any(
            g.get("status") in ACTIVE_STATUSES and not (g.get("progress_summary") or "").strip()
            for g in goals
        )

    if needs_progress:
        from routers.ai_analysis import run_goal_progress
        background_tasks.add_task(run_goal_progress, tank_id, None)

    return templates.TemplateResponse(request, "goals/list.html", {
        "tank": tank,
        "goals": goals,
        "picker_by_tank": picker_by_tank,
    })


@router.post("/review")
async def review_goal(
    tank_id: int,
    title: str = Form(...),
    description: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    depends_on: Optional[List[str]] = Form(None),
):
    """AI review of a draft goal before save. Does not write to the DB."""
    import os
    from routers.ai_analysis import (
        build_goal_review_prompt, _parse_goal_review, _claude_text, load_home_water_tests,
    )
    from ai_config import CLAUDE_MAX_TOKENS_GOAL_REVIEW

    draft = {
        "title": (title or "").strip(),
        "description": (description or "").strip(),
        "target": (target or "").strip(),
        "notes": (notes or "").strip(),
    }
    if not draft["title"]:
        raise HTTPException(status_code=400, detail="Title is required")

    dep_ids = _parse_dep_ids(depends_on)

    with get_db() as conn:
        tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        existing = load_active_goals(conn, tank_id)
        latest_test = row_to_dict(conn.execute(
            "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 1",
            (tank_id,),
        ).fetchone())
        inhabitants = rows_to_list(conn.execute(
            "SELECT * FROM inhabitants WHERE tank_id = ?", (tank_id,),
        ).fetchall())
        # Other tanks' current stock — needed when draft says "from shrimp tank" / "other tank".
        other_tanks = rows_to_list(conn.execute(
            "SELECT id, name, volume_gallons FROM tanks WHERE id != ? AND status != 'archived' ORDER BY name",
            (tank_id,),
        ).fetchall())
        other_tanks_stock = []
        for ot in other_tanks:
            stock = rows_to_list(conn.execute(
                """SELECT common_name, species, count FROM inhabitants
                   WHERE tank_id = ? AND (count IS NULL OR count > 0)
                   ORDER BY common_name, species""",
                (ot["id"],),
            ).fetchall())
            other_tanks_stock.append({
                "id": ot["id"],
                "name": ot["name"],
                "volume_gallons": ot.get("volume_gallons"),
                "inhabitants": stock,
            })
        home_water_tests = load_home_water_tests(conn)
        dep_goals: list[dict] = []
        if dep_ids:
            placeholders = ",".join("?" * len(dep_ids))
            dep_goals = rows_to_list(conn.execute(
                f"""SELECT g.id, g.title, g.status, g.target, t.name AS tank_name
                    FROM goals g JOIN tanks t ON t.id = g.tank_id
                    WHERE g.id IN ({placeholders})
                    ORDER BY t.name, g.title""",
                dep_ids,
            ).fetchall())

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return JSONResponse({
            "reasonable": True,
            "summary": "AI review unavailable (no API key). You can save the goal as written.",
            "suggestions": [],
            "proposed": draft,
            "changed": False,
            "draft": draft,
        })

    prompt = build_goal_review_prompt(
        tank, draft, existing, latest_test=latest_test, inhabitants=inhabitants,
        dep_goals=dep_goals, home_water_tests=home_water_tests,
        other_tanks_stock=other_tanks_stock,
    )
    try:
        import asyncio
        import logging
        import time
        import anthropic
        from ai_config import CLAUDE_MODEL, CLAUDE_THINKING_DISABLED
        from routers.ai_analysis import (
            _message_text, _proposed_needs_rewrite, _draft_looks_rough,
        )

        logger = logging.getLogger(__name__)
        client = anthropic.Anthropic(api_key=api_key)

        async def _one_review_call(messages, attempt_name):
            kwargs = {
                "model": CLAUDE_MODEL,
                "max_tokens": CLAUDE_MAX_TOKENS_GOAL_REVIEW,
                "messages": messages,
                "timeout": 90.0,
                "thinking": CLAUDE_THINKING_DISABLED,
            }
            logger.info("Claude call: goal_review | tank=%d | %s", tank_id, attempt_name)
            t0 = time.monotonic()
            msg = await asyncio.to_thread(client.messages.create, **kwargs)
            elapsed = time.monotonic() - t0
            stop = getattr(msg, "stop_reason", None)
            usage = getattr(msg, "usage", None)
            logger.info(
                "Claude done: goal_review | tank=%d | %s | in=%s out=%s elapsed=%.1fs stop=%s",
                tank_id, attempt_name,
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
                elapsed, stop,
            )
            return _message_text(msg)

        # Prefer thinking off for reliable JSON (adaptive previously truncated mid-object).
        raw = await _one_review_call(
            [{"role": "user", "content": prompt}],
            "no_thinking",
        )
        result = _parse_goal_review(raw, draft)

        # If parse failed, one adaptive retry then re-parse.
        if result.get("parse_failed"):
            raw2 = await _one_review_call(
                [{"role": "user", "content": prompt}],
                "adaptive_retry",
            )
            # adaptive_retry still uses thinking disabled in _one_review_call — keep it that way
            # for reliability; name is historical.
            if raw2:
                result = _parse_goal_review(raw2, draft)

        # Rough draft left unpolished (e.g. target still "gh 5 minimum?") → one rewrite nudge.
        if (
            not result.get("parse_failed")
            and _draft_looks_rough(draft)
            and _proposed_needs_rewrite(result.get("proposed") or {}, draft)
        ):
            logger.info(
                "Goal review proposed left rough draft intact for tank %d — rewrite pass",
                tank_id,
            )
            nudge = (
                "Your previous proposed fields were too close to the rough draft. "
                "Rewrite proposed.title / proposed.target / proposed.description into a "
                "finished, save-ready goal: proper capitalization and punctuation; "
                "target MUST include ideal GH/parameter range, tolerable range if useful, "
                "and a hold timeline (no question marks, no 'minimum?'). "
                "Species named in the draft may use standard care ranges. "
                "Keep summary/suggestions for feedback only. Return the same JSON shape only."
            )
            raw3 = await _one_review_call(
                [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": raw or "{}"},
                    {"role": "user", "content": nudge},
                ],
                "rewrite_nudge",
            )
            if raw3:
                result2 = _parse_goal_review(raw3, draft)
                if not result2.get("parse_failed"):
                    result = result2
    except Exception as e:
        return JSONResponse({
            "reasonable": False,
            "summary": f"AI review failed ({e}). You can save the goal as written or try Re-review.",
            "suggestions": [],
            "proposed": draft,
            "changed": False,
            "draft": draft,
        })

    result["draft"] = draft
    return JSONResponse(result)


@router.post("")
async def add_goal(
    request: Request,
    tank_id: int,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    description: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
    status: str = Form("in_progress"),
    notes: Optional[str] = Form(None),
    depends_on: Optional[List[str]] = Form(None),
):
    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    if status not in VALID_STATUSES:
        status = "in_progress"
    dep_ids = _parse_dep_ids(depends_on)

    with get_db() as conn:
        tank = conn.execute("SELECT id FROM tanks WHERE id = ?", (tank_id,)).fetchone()
        if not tank:
            raise HTTPException(status_code=404, detail="Tank not found")
        achieved_at = _now_utc() if status == "achieved" else None
        cur = conn.execute(
            """INSERT INTO goals (tank_id, title, description, target, status, notes, achieved_at)
               VALUES (?,?,?,?,?,?,?)""",
            (tank_id, title, description or None, target or None, status, notes or None, achieved_at),
        )
        goal_id = cur.lastrowid
        _set_dependencies(conn, goal_id, dep_ids)

    # Always seed an AI progress blurb for new active goals so the list never
    # sits empty waiting for the next water test.
    if status in ACTIVE_STATUSES:
        from routers.ai_analysis import run_goal_progress
        background_tasks.add_task(run_goal_progress, tank_id, None)

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"id": goal_id, "status": "created"}, status_code=201)
    return RedirectResponse(url=f"/tanks/{tank_id}/goals", status_code=303)


@router.post("/{goal_id}/update")
async def update_goal(
    request: Request,
    tank_id: int,
    goal_id: int,
    title: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
    status: str = Form("in_progress"),
    notes: Optional[str] = Form(None),
    comment: Optional[str] = Form(None),
    achieved_at: Optional[str] = Form(None),
    depends_on: Optional[List[str]] = Form(None),
    update_deps: Optional[str] = Form(None),
):
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")

    with get_db() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT * FROM goals WHERE id = ? AND tank_id = ?", (goal_id, tank_id),
        ).fetchone())
        if not existing:
            raise HTTPException(status_code=404, detail="Goal not found")

        final_title = (title if title is not None else existing["title"] or "").strip()
        if not final_title:
            final_title = existing["title"]
        final_description = description if description is not None else existing["description"]
        final_target = target if target is not None else existing["target"]
        final_notes = notes if notes is not None else existing["notes"]
        if comment:
            stamped = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}: {comment}"
            final_notes = f"{final_notes}\n\n{stamped}" if final_notes else stamped

        becoming_achieved = status == "achieved" and existing["status"] != "achieved"
        leaving_achieved = status != "achieved" and existing["status"] == "achieved"
        if becoming_achieved:
            final_achieved_at = _date_or_now(achieved_at)
        elif leaving_achieved:
            final_achieved_at = None
        else:
            final_achieved_at = existing["achieved_at"]

        conn.execute(
            """UPDATE goals SET title=?, description=?, target=?, status=?, notes=?,
               achieved_at=?, updated_at=datetime('now') WHERE id=?""",
            (final_title, final_description, final_target, status, final_notes,
             final_achieved_at, goal_id),
        )

        # Only rewrite dependencies when the form explicitly sent the dep multi-select
        # (edit modal). Status-transition forms omit update_deps so they don't clear deps.
        if update_deps is not None:
            _set_dependencies(conn, goal_id, _parse_dep_ids(depends_on))

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse({"status": "updated"})
    return RedirectResponse(url=f"/tanks/{tank_id}/goals", status_code=303)


@router.post("/{goal_id}/delete")
async def delete_goal(tank_id: int, goal_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM goals WHERE id = ? AND tank_id = ?", (goal_id, tank_id))
    return RedirectResponse(url=f"/tanks/{tank_id}/goals", status_code=303)
