import os
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from database import get_db, rows_to_list, row_to_dict
from ai_config import (
    CLAUDE_MODEL,
    CLAUDE_THINKING_DISABLED,
    ANALYSIS_FAILURE_PREFIX,
    CLAUDE_MAX_TOKENS_ANALYSIS,
    CLAUDE_MAX_TOKENS_ISSUE_REVIEW,
    CLAUDE_MAX_TOKENS_SUMMARY,
    CLAUDE_MAX_TOKENS_NOTES_PROPOSAL,
    CLAUDE_MAX_TOKENS_RECOMMENDATION,
    CLAUDE_MAX_TOKENS_GOAL_PROGRESS,
    CLAUDE_MAX_TOKENS_GOAL_REVIEW,
)

logger = logging.getLogger(__name__)

# Claude call timeout (adaptive thinking can take longer than plain replies).
_CLAUDE_TIMEOUT = 90.0


def _message_text(msg) -> str:
    """Visible text from a Claude messages response (skip thinking / non-text blocks).

    On Claude Sonnet 5, adaptive thinking may put ThinkingBlock first; that block
    has no ``.text`` attribute, so ``msg.content[0].text`` raises AttributeError.
    """
    parts = []
    for block in getattr(msg, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype in ("thinking", "redacted_thinking"):
            continue
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


async def _claude_text(client, *, label, tank_id, max_tokens, messages, timeout=_CLAUDE_TIMEOUT):
    """Call Claude with adaptive thinking; if no visible text, retry once with thinking off.

    Returns (message, text). text is '' if both attempts produce no TextBlock.
    """
    last_msg = None
    # Attempt 1: omit thinking → Sonnet 5 adaptive default (model may think).
    # Attempt 2: force thinking off so max_tokens is all reply text.
    attempts = (
        ("adaptive", None),
        ("no_thinking", CLAUDE_THINKING_DISABLED),
    )
    for attempt_name, thinking in attempts:
        kwargs = {
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": messages,
            "timeout": timeout,
        }
        if thinking is not None:
            kwargs["thinking"] = thinking

        logger.info("Claude call: %s | tank=%d | thinking=%s", label, tank_id, attempt_name)
        t0 = time.monotonic()
        msg = await asyncio.to_thread(client.messages.create, **kwargs)
        elapsed = time.monotonic() - t0
        usage = getattr(msg, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        stop = getattr(msg, "stop_reason", None)
        if stop == "max_tokens":
            logger.warning(
                "Claude %s hit max_tokens for tank %d (thinking=%s) — response may be truncated",
                label, tank_id, attempt_name,
            )
        logger.info(
            "Claude done: %s | tank=%d | thinking=%s | in=%d out=%d elapsed=%.1fs stop=%s",
            label, tank_id, attempt_name, in_tok, out_tok, elapsed, stop,
        )
        text = _message_text(msg)
        if text:
            if attempt_name == "no_thinking":
                logger.info(
                    "Claude %s recovered via thinking-disabled retry | tank=%d",
                    label, tank_id,
                )
            return msg, text
        logger.warning(
            "Claude %s returned no text for tank %d (stop=%s, thinking=%s)",
            label, tank_id, stop, attempt_name,
        )
        last_msg = msg
    return last_msg, ""


def _record_analysis_failure(tank_id, trigger_type, trigger_id, error):
    """Persist a visible failure so the UI + wait page are not silent.

    Writes an auto observation (linked to the triggering test/event when possible).
    Does NOT overwrite a good tank_state_summary.
    """
    err = str(error).strip() or "unknown error"
    # Keep the note readable; full traceback stays in logs.
    if len(err) > 500:
        err = err[:500] + "…"
    text = (
        f"{ANALYSIS_FAILURE_PREFIX} {err}\n\n"
        "The previous AI summary (if any) was left unchanged. "
        "Details are in the server log; try saving another test, or check later."
    )
    related_test_id = trigger_id if trigger_type == "test" else None
    related_event_id = trigger_id if trigger_type == "event" else None
    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO observations (tank_id, related_event_id, related_test_id, source, text)
                   VALUES (?, ?, ?, 'auto', ?)""",
                (tank_id, related_event_id, related_test_id, text),
            )
        logger.error(
            "Recorded AI analysis failure observation for tank %d (%s=%s): %s",
            tank_id, trigger_type, trigger_id, err,
        )
    except Exception as e:
        logger.error(
            "Could not record AI analysis failure for tank %d: %s (original: %s)",
            tank_id, e, err,
        )


from routers.ai_prompts import (
    _CURRENT_PRACTICES_RULE,
    _HOME_WATER_PROMPT_RULE,
    _baseline_from_fill_rows,
    _draft_looks_rough,
    _fmt_events,
    _fmt_goals,
    _fmt_hardscape,
    _fmt_home_water,
    _fmt_home_water_baseline,
    _fmt_home_water_block,
    _fmt_inhabitants,
    _fmt_issues,
    _fmt_issues_with_id,
    _fmt_plants,
    _fmt_schedule,
    _fmt_tank_notes,
    _fmt_test_results,
    _fmt_timeline_rows,
    _looks_like_review_meta,
    _parse_goal_progress_updates,
    _parse_goal_review,
    _parse_issue_updates,
    _parse_notes_proposal,
    _proposed_needs_rewrite,
    build_analysis_prompt,
    build_goal_progress_prompt,
    build_goal_review_prompt,
    build_issue_review_prompt,
    build_notes_proposal_prompt,
    build_recommendation_prompt,
    build_summary_prompt,
    load_home_water_tests,
    load_keeper_log,
)

async def run_ai_analysis(tank_id: int, trigger_type: str, trigger_id: int):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping AI analysis")
        _record_analysis_failure(tank_id, trigger_type, trigger_id, "ANTHROPIC_API_KEY not set")
        return

    try:
        import anthropic

        with get_db() as conn:
            tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
            if not tank:
                return

            test_results = rows_to_list(conn.execute(
                "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 10",
                (tank_id,),
            ).fetchall())

            issues = rows_to_list(conn.execute(
                "SELECT * FROM issues WHERE tank_id = ? AND status != 'resolved' ORDER BY opened_at DESC",
                (tank_id,),
            ).fetchall())

            from routers.goals import load_active_goals
            goals = load_active_goals(conn, tank_id)

            events = rows_to_list(conn.execute(
                "SELECT * FROM events WHERE tank_id = ? AND timestamp >= datetime('now','-30 days') ORDER BY timestamp DESC",
                (tank_id,),
            ).fetchall())

            inhabitants = rows_to_list(conn.execute(
                "SELECT * FROM inhabitants WHERE tank_id = ?",
                (tank_id,),
            ).fetchall())

            plants = rows_to_list(conn.execute(
                "SELECT * FROM plants WHERE tank_id = ? AND status = 'active'",
                (tank_id,),
            ).fetchall())

            hardscape = rows_to_list(conn.execute(
                "SELECT * FROM hardscape WHERE tank_id = ?",
                (tank_id,),
            ).fetchall())

            schedule_rows = rows_to_list(conn.execute(
                "SELECT * FROM recurring_schedule WHERE tank_id = ? AND is_active = 1",
                (tank_id,),
            ).fetchall())

            home_water_tests = load_home_water_tests(conn)
            keeper_log = load_keeper_log(conn, tank_id)

        client = anthropic.Anthropic(api_key=api_key)

        analysis_prompt = build_analysis_prompt(
            tank, test_results, issues, events, inhabitants, plants, hardscape, schedule_rows,
            home_water_tests=home_water_tests, goals=goals, keeper_log=keeper_log,
        )
        _, analysis_text = await _claude_text(
            client,
            label="analysis",
            tank_id=tank_id,
            max_tokens=CLAUDE_MAX_TOKENS_ANALYSIS,
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        if not analysis_text:
            _record_analysis_failure(
                tank_id, trigger_type, trigger_id,
                "Claude returned no analysis text after adaptive thinking + no-thinking retry",
            )
            return

        issue_updates = []
        if issues:
            issue_review_prompt = build_issue_review_prompt(tank, issues, test_results)
            _, issue_raw = await _claude_text(
                client,
                label="issue_review",
                tank_id=tank_id,
                max_tokens=CLAUDE_MAX_TOKENS_ISSUE_REVIEW,
                messages=[{"role": "user", "content": issue_review_prompt}],
            )
            issue_updates = _parse_issue_updates(issue_raw, {i["id"] for i in issues})

        related_test_id = trigger_id if trigger_type == "test" else None
        related_event_id = trigger_id if trigger_type == "event" else None

        with get_db() as conn:
            conn.execute(
                """INSERT INTO observations (tank_id, related_event_id, related_test_id, source, text)
                   VALUES (?, ?, ?, 'auto', ?)""",
                (tank_id, related_event_id, related_test_id, analysis_text),
            )

            issues_by_id = {i["id"]: i for i in issues}
            for upd in issue_updates:
                issue = issues_by_id.get(upd["issue_id"])
                if not issue or upd["status"] == issue["status"]:
                    continue
                note_line = f"Auto-updated to '{upd['status']}' by AI analysis: {upd['reason']}"
                new_notes = f"{issue.get('notes') or ''}\n\n{note_line}".strip()
                if upd["status"] == "resolved":
                    conn.execute(
                        """UPDATE issues SET status=?, notes=?, resolved_at=datetime('now'),
                           updated_at=datetime('now') WHERE id=?""",
                        (upd["status"], new_notes, upd["issue_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE issues SET status=?, notes=?, updated_at=datetime('now') WHERE id=?",
                        (upd["status"], new_notes, upd["issue_id"]),
                    )
                verb = {"resolved": "auto-resolved", "monitoring": "moved to monitoring", "open": "reopened"}[upd["status"]]
                conn.execute(
                    "INSERT INTO observations (tank_id, source, text) VALUES (?, 'auto', ?)",
                    (tank_id, f"Issue \"{issue['title']}\" {verb} by AI analysis: {upd['reason']}"),
                )
                logger.info("Issue %d %s for tank %d: %s", upd["issue_id"], verb, tank_id, upd["reason"])
                issue["status"] = upd["status"]

        summary_prompt = build_summary_prompt(
            tank, test_results, issues, inhabitants, plants, hardscape, analysis_text,
            schedule_rows, events, home_water_tests=home_water_tests, goals=goals,
            keeper_log=keeper_log,
        )
        _, summary_text = await _claude_text(
            client,
            label="summary",
            tank_id=tank_id,
            max_tokens=CLAUDE_MAX_TOKENS_SUMMARY,
            messages=[{"role": "user", "content": summary_prompt}],
        )
        if not summary_text:
            # Analysis observation already saved; still surface that summary failed
            # so the wait page unblocks and the user sees the error.
            _record_analysis_failure(
                tank_id, trigger_type, trigger_id,
                "Claude returned no summary text after adaptive thinking + no-thinking retry "
                "(analysis note was saved)",
            )
            return

        with get_db() as conn:
            conn.execute(
                """INSERT INTO tank_state_summary (tank_id, summary_text, generated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(tank_id) DO UPDATE SET
                     summary_text = excluded.summary_text,
                     generated_at = excluded.generated_at,
                     updated_at = datetime('now')""",
                (tank_id, summary_text),
            )

        # After summary: if schedule/events contradict tank notes, propose a notes refresh
        # for the user to accept/dismiss on the dashboard (never auto-write notes).
        await _maybe_propose_tank_notes_update(
            client, tank_id, tank, schedule_rows, events, test_results, home_water_tests,
            keeper_log=keeper_log,
        )

        logger.info("AI analysis complete for tank %d", tank_id)

    except Exception as e:
        logger.error("AI analysis failed for tank %d: %s", tank_id, e, exc_info=True)
        _record_analysis_failure(tank_id, trigger_type, trigger_id, e)


async def _maybe_propose_tank_notes_update(client, tank_id, tank, schedule_rows, events, test_results,
                                          home_water_tests=None, keeper_log=None):
    """If notes look stale vs schedule/events, store a pending proposal for user confirmation."""
    home_water_tests = home_water_tests or []
    with get_db() as conn:
        pending = conn.execute(
            """SELECT id FROM tank_notes_proposals
               WHERE tank_id = ? AND status = 'pending' LIMIT 1""",
            (tank_id,),
        ).fetchone()
        if pending:
            logger.info("Notes proposal already pending for tank %d — skipping", tank_id)
            return

    # Skip when there's nothing operational to compare against
    if not schedule_rows and not events:
        return

    prompt = build_notes_proposal_prompt(
        tank, schedule_rows, events, test_results, home_water_tests=home_water_tests,
        keeper_log=keeper_log,
    )
    try:
        _, proposal_raw = await _claude_text(
            client,
            label="notes_proposal",
            tank_id=tank_id,
            max_tokens=CLAUDE_MAX_TOKENS_NOTES_PROPOSAL,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        logger.error("Notes proposal Claude call failed for tank %d: %s", tank_id, e)
        return
    proposal = _parse_notes_proposal(proposal_raw, tank.get("notes"))
    if not proposal:
        logger.info("No tank notes update needed for tank %d", tank_id)
        return

    with get_db() as conn:
        # Don't re-offer the exact same proposal the user already dismissed
        last_dismissed = conn.execute(
            """SELECT proposed_notes FROM tank_notes_proposals
               WHERE tank_id = ? AND status = 'dismissed'
               ORDER BY resolved_at DESC LIMIT 1""",
            (tank_id,),
        ).fetchone()
        if last_dismissed and (last_dismissed[0] or "").strip() == proposal["proposed_notes"]:
            logger.info("Identical notes proposal already dismissed for tank %d — skipping", tank_id)
            return

        conn.execute(
            """INSERT INTO tank_notes_proposals
               (tank_id, proposed_notes, reason, prior_notes, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (tank_id, proposal["proposed_notes"], proposal["reason"], proposal["prior_notes"]),
        )
    logger.info("Stored pending tank notes proposal for tank %d: %s", tank_id, proposal["reason"])


async def run_test_recommendation(tank_id: int, result_id: int):
    """Ask Claude for a recommended action after a manually-logged test result, and
    append the answer to that test result's notes. Only wired up from the manual
    'Add Test Result' form submit — not run for tests inserted via import."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping test recommendation")
        return

    try:
        import anthropic
        from routers.timeline import _QUERY as _TIMELINE_QUERY

        with get_db() as conn:
            tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
            test_result = row_to_dict(conn.execute(
                "SELECT * FROM test_results WHERE id = ? AND tank_id = ?", (result_id, tank_id)
            ).fetchone())
            if not tank or not test_result:
                return

            recent_tests = rows_to_list(conn.execute(
                "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 6", (tank_id,)
            ).fetchall())

            issues = rows_to_list(conn.execute(
                "SELECT * FROM issues WHERE tank_id = ? AND status != 'resolved' ORDER BY opened_at DESC",
                (tank_id,),
            ).fetchall())

            from routers.goals import load_active_goals
            goals = load_active_goals(conn, tank_id)

            inhabitants = rows_to_list(conn.execute(
                "SELECT * FROM inhabitants WHERE tank_id = ?", (tank_id,)
            ).fetchall())

            schedule_rows = rows_to_list(conn.execute(
                "SELECT * FROM recurring_schedule WHERE tank_id = ? AND is_active = 1", (tank_id,)
            ).fetchall())

            timeline_rows = rows_to_list(conn.execute(_TIMELINE_QUERY, (tank_id,) * 9).fetchall())

            home_water_tests = load_home_water_tests(conn)

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=28)).isoformat()
        timeline_rows = [r for r in timeline_rows if (r.get("ts") or "")[:10] >= cutoff]

        client = anthropic.Anthropic(api_key=api_key)

        prompt = build_recommendation_prompt(
            tank, test_result, recent_tests, issues, inhabitants, schedule_rows, timeline_rows,
            home_water_tests=home_water_tests, goals=goals,
        )
        _, recommendation = await _claude_text(
            client,
            label="test_recommendation",
            tank_id=tank_id,
            max_tokens=CLAUDE_MAX_TOKENS_RECOMMENDATION,
            messages=[{"role": "user", "content": prompt}],
        )
        if not recommendation:
            logger.warning(
                "Test recommendation returned no text for tank %d test %d (after retry)",
                tank_id, result_id,
            )
            return

        with get_db() as conn:
            current = conn.execute(
                "SELECT notes FROM test_results WHERE id = ? AND tank_id = ?", (result_id, tank_id)
            ).fetchone()
            if current is None:
                return
            existing_notes = (current[0] or "").strip()
            new_notes = f"{existing_notes}\n\nAI Recommendation: {recommendation}" if existing_notes else f"AI Recommendation: {recommendation}"
            conn.execute(
                "UPDATE test_results SET notes = ?, updated_at = datetime('now') WHERE id = ?",
                (new_notes, result_id),
            )

        logger.info("Test recommendation complete for tank %d test %d", tank_id, result_id)

    except Exception as e:
        logger.error("Test recommendation failed for tank %d test %d: %s", tank_id, result_id, e)


# Prevent concurrent progress runs for the same tank (create + list backfill + test save).
_goal_progress_in_flight: set[int] = set()


async def run_goal_progress(tank_id: int, result_id: int | None = None):
    """Refresh AI progress summaries for all active goals on a tank.

    Triggered when a goal is created and after each manual water-test save.
    One Claude call covers every open/in_progress goal.
    """
    if tank_id in _goal_progress_in_flight:
        logger.info("Goal progress already in flight for tank %d — skip", tank_id)
        return
    _goal_progress_in_flight.add(tank_id)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping goal progress update")
        _goal_progress_in_flight.discard(tank_id)
        return

    try:
        import anthropic
        from routers.goals import load_active_goals

        with get_db() as conn:
            tank = row_to_dict(conn.execute("SELECT * FROM tanks WHERE id = ?", (tank_id,)).fetchone())
            if not tank:
                return

            goals = load_active_goals(conn, tank_id)
            if not goals:
                logger.info("No active goals for tank %d — skip progress update", tank_id)
                return

            test_results = rows_to_list(conn.execute(
                "SELECT * FROM test_results WHERE tank_id = ? ORDER BY timestamp DESC LIMIT 10",
                (tank_id,),
            ).fetchall())

            inhabitants = rows_to_list(conn.execute(
                "SELECT * FROM inhabitants WHERE tank_id = ?", (tank_id,),
            ).fetchall())

            events = rows_to_list(conn.execute(
                "SELECT * FROM events WHERE tank_id = ? AND timestamp >= datetime('now','-30 days') ORDER BY timestamp DESC",
                (tank_id,),
            ).fetchall())

            home_water_tests = load_home_water_tests(conn)
            keeper_log = load_keeper_log(conn, tank_id)

        client = anthropic.Anthropic(api_key=api_key)
        prompt = build_goal_progress_prompt(
            tank, goals, test_results, inhabitants, events,
            home_water_tests=home_water_tests, keeper_log=keeper_log,
        )
        _, raw = await _claude_text(
            client,
            label="goal_progress",
            tank_id=tank_id,
            max_tokens=CLAUDE_MAX_TOKENS_GOAL_PROGRESS,
            messages=[{"role": "user", "content": prompt}],
        )
        if not raw:
            logger.warning("Goal progress returned no text for tank %d", tank_id)
            return

        updates = _parse_goal_progress_updates(raw, {g["id"] for g in goals})
        if not updates:
            logger.warning("Goal progress produced no valid updates for tank %d: %s", tank_id, raw[:200])
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with get_db() as conn:
            for upd in updates:
                conn.execute(
                    """UPDATE goals SET progress_summary = ?, progress_summary_at = ?,
                       updated_at = datetime('now')
                       WHERE id = ? AND tank_id = ?
                         AND status IN ('open', 'in_progress')""",
                    (upd["progress_summary"], now, upd["goal_id"], tank_id),
                )

        logger.info(
            "Goal progress updated for tank %d (%d goals)%s",
            tank_id, len(updates),
            f" after test {result_id}" if result_id else "",
        )

    except Exception as e:
        logger.error("Goal progress update failed for tank %d: %s", tank_id, e)
    finally:
        _goal_progress_in_flight.discard(tank_id)
