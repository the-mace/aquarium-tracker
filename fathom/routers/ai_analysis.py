import os
import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from database import get_db, rows_to_list, row_to_dict

logger = logging.getLogger(__name__)


def _fmt_tank_notes(tank):
    notes = (tank.get("notes") or "").strip()
    if not notes:
        return ""
    return f"\nTank notes (may include this tank's own accepted parameter targets/baselines — defer to these over generic species norms): {notes}"


def _fmt_test_results(rows):
    if not rows:
        return "  No test results recorded."
    lines = []
    for r in rows:
        parts = []
        for field in ("ph", "gh", "kh", "ammonia", "nitrite", "nitrate", "tds", "temp"):
            val = r.get(field)
            if val is not None:
                parts.append(f"{field.upper()}={val}")
        lines.append(f"  {r['timestamp']}: {', '.join(parts)}" + (f" | {r['notes']}" if r.get("notes") else ""))
    return "\n".join(lines)


def _fmt_inhabitants(rows):
    if not rows:
        return "  None"
    lines = []
    for r in rows:
        name = r.get("common_name") or r.get("species") or "Unknown"
        count = r.get("count")
        count_str = "many" if count is None else str(count)
        added = r.get("added_date")
        added_str = f" (added {added})" if added else ""
        lines.append(f"  {count_str}x {name}{added_str}")
    return "\n".join(lines)


def _fmt_plants(rows):
    if not rows:
        return "  None"
    return "\n".join(f"  {r.get('common_name') or r.get('species') or 'Unknown plant'}" for r in rows)


def _fmt_hardscape(rows):
    if not rows:
        return "  None"
    lines = []
    for r in rows:
        qty = r.get("quantity") or 1
        prefix = f"{qty}x " if qty > 1 else ""
        lines.append(f"  {prefix}{r['item']}")
    return "\n".join(lines)


def _fmt_issues(rows):
    if not rows:
        return "  None"
    return "\n".join(f"  [{r['status'].upper()}] {r['title']}: {r.get('description','')}" for r in rows)


def _fmt_issues_with_id(rows):
    if not rows:
        return "  None"
    return "\n".join(
        f"  id={r['id']} [{r['status'].upper()}] {r['title']}: {r.get('description','')}" for r in rows
    )


def _fmt_events(rows):
    if not rows:
        return "  None"
    return "\n".join(f"  {r['timestamp']} {r['event_type']}: {r.get('notes','')}" for r in rows)


def _fmt_schedule(rows):
    if not rows:
        return "  No recurring schedule configured."
    lines = []
    for r in rows:
        cat = r.get("category")
        desc = r.get("description")
        if r.get("tracking_mode") == "logged":
            interval = r.get("interval_days") or "?"
            last_done = r.get("last_done") or "never"
            next_due = r.get("next_due") or "not set"
            lines.append(f"  [{cat}] {desc} — every {interval} days, last done {last_done}, next due {next_due}")
        else:
            dow = r.get("day_of_week") or "unscheduled"
            lines.append(f"  [{cat}] {desc} — {dow}")
    return "\n".join(lines)


def _fmt_timeline_rows(rows):
    if not rows:
        return "  No recent activity."
    lines = []
    for r in rows:
        header = r.get("kind") or ""
        if r.get("subtype"):
            header += f"/{r['subtype']}"
        text = " ".join(filter(None, [r.get("label"), r.get("detail")]))
        lines.append(f"  {r.get('ts')} [{header}] {text}".rstrip())
    return "\n".join(lines)


def build_recommendation_prompt(tank, test_result, recent_tests, issues, inhabitants, schedule_rows, timeline_rows):
    return f"""You are helping during routine aquarium maintenance, right after a water test was just logged. Write a short status update the keeper will read immediately, mid-maintenance.

Background context (use this ONLY to judge whether something needs attention — e.g. species-appropriate parameter ranges for the inhabitants below, or whether a scheduled task is overdue. Do NOT summarize or restate this background in your answer; the keeper already knows their own tank contents):

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}
Inhabitants: {_fmt_inhabitants(inhabitants)}
Open issues: {_fmt_issues(issues)}
Recurring feeding/dosing/maintenance schedule:
{_fmt_schedule(schedule_rows)}
Tank activity over the last 4 weeks (newest first):
{_fmt_timeline_rows(timeline_rows)}

Water test just recorded (newest) plus recent tests for trend comparison:
{_fmt_test_results([test_result] + [t for t in recent_tests if t.get('id') != test_result.get('id')])}

Now write the actual response. Cover only what's relevant, briefly:
1. Open issues — one short line (e.g. "No open issues at this time."). Skip if genuinely nothing to say.
2. Any water parameter values or trends worth flagging vs. the recent tests above (e.g. a drop/rise since the last test, or a value outside the *safe tolerance* range for the inhabitants). Use precise, species-specific tolerance ranges rather than overly cautious defaults. A value outside a narrower "ideal"/breeding-optimal sub-range but still within safe tolerance is NOT a concern — at most note it's outside the ideal range for breeding/growth; reserve concern language for values actually near or outside the safe tolerance boundary. Only mention parameters that are actually notable, skip the rest.
3. The action to take now. Usually this is simply "Proceed with the standard water change" per the schedule above — do not restate the schedule's gallons/dose/interval details, the keeper already has those. Only describe something different if this test's results or recent history genuinely call for a different action.

2-4 sentences total, plain text, no markdown, no headers, no preamble like "Recommendation:" or "Analysis:" — this text is appended directly to the test result's own notes field."""


def build_analysis_prompt(tank, test_results, issues, events, inhabitants, plants, hardscape):
    return f"""You are an expert aquarium keeper analyzing water chemistry and tank health data.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}

Current Inhabitants:
{_fmt_inhabitants(inhabitants)}

Plants:
{_fmt_plants(plants)}

Hardscape:
{_fmt_hardscape(hardscape)}

Recent Test Results (newest first):
{_fmt_test_results(test_results)}

Open Issues:
{_fmt_issues(issues)}

Recent Events (last 30 days):
{_fmt_events(events)}

Please provide:
1. A brief analysis of the water chemistry trends
2. Any flags or concerns about parameters outside the *safe tolerance* range for this tank's inhabitants — use precise, species-specific ranges rather than overly cautious defaults. A value outside a narrower "ideal"/breeding-optimal sub-range but still within safe tolerance is NOT a concern — at most note it's outside the ideal range for breeding/growth; reserve concern language for values actually near or outside the safe tolerance boundary.
3. Specific actionable recommendations
4. For each open issue, suggest whether it should remain open, move to monitoring, or be resolved

Keep your response concise and practical. Use plain text, no markdown formatting."""


def build_summary_prompt(tank, test_results, issues, inhabitants, plants, hardscape, latest_analysis):
    return f"""You are an expert aquarium keeper. Write a concise 2-3 paragraph summary of this tank's current state for use as context in future questions.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}

Inhabitants:
{_fmt_inhabitants(inhabitants)}

Plants:
{_fmt_plants(plants)}

Hardscape:
{_fmt_hardscape(hardscape)}

Latest Water Parameters:
{_fmt_test_results(test_results[:1])}

Open Issues:
{_fmt_issues([i for i in issues if i.get('status') != 'resolved'])}

Latest Analysis:
{latest_analysis}

Write the summary as plain text, no markdown. Be specific about current parameter values, inhabitants, and any active concerns."""


def build_issue_review_prompt(tank, issues, test_results):
    return f"""You are an expert aquarium keeper reviewing open issues against recent water test data to decide whether any should change status.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}

Open/monitoring issues (id, current status, title, description):
{_fmt_issues_with_id(issues)}

Recent test results (newest first):
{_fmt_test_results(test_results)}

For each issue, decide whether the recent data shows it has resolved (the underlying problem is no longer occurring — evidenced by MULTIPLE consecutive stable/normal readings, not just one good reading) or should move to "monitoring" (improving but not yet confirmed stable), or should move back to "open" (data shows the problem recurring). Be conservative: only mark an issue resolved when the trend is clearly and consistently stable across several recent data points. If there isn't enough recent data to judge, leave the issue unchanged.

Return ONLY a JSON array (no markdown, no commentary) with one entry for EVERY issue whose status should change from its current status. Omit any issue that should remain unchanged. Each entry: {{"issue_id": <id>, "status": "open"|"monitoring"|"resolved", "reason": "one sentence citing the specific data that supports this"}}.

If no issues should change, return an empty array: []"""


def _parse_issue_updates(raw_text, valid_ids):
    """Parse the issue-review JSON response, keeping only well-formed entries for known issue ids."""
    text = re.sub(r"```json\s*", "", raw_text or "")
    text = re.sub(r"```\s*", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    updates = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        issue_id = entry.get("issue_id")
        status = entry.get("status")
        reason = (entry.get("reason") or "").strip()
        if issue_id in valid_ids and status in ("open", "monitoring", "resolved") and reason:
            updates.append({"issue_id": issue_id, "status": status, "reason": reason})
    return updates


async def run_ai_analysis(tank_id: int, trigger_type: str, trigger_id: int):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping AI analysis")
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

        client = anthropic.Anthropic(api_key=api_key)

        analysis_prompt = build_analysis_prompt(tank, test_results, issues, events, inhabitants, plants, hardscape)
        logger.info("Claude call: analysis | tank=%d", tank_id)
        t0 = time.monotonic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": analysis_prompt}],
            timeout=60.0,
        )
        logger.info("Claude done: analysis | tank=%d | in=%d out=%d elapsed=%.1fs",
                    tank_id, msg.usage.input_tokens, msg.usage.output_tokens, time.monotonic() - t0)
        analysis_text = msg.content[0].text

        issue_updates = []
        if issues:
            issue_review_prompt = build_issue_review_prompt(tank, issues, test_results)
            logger.info("Claude call: issue_review | tank=%d", tank_id)
            t_ir = time.monotonic()
            issue_msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                messages=[{"role": "user", "content": issue_review_prompt}],
                timeout=60.0,
            )
            logger.info("Claude done: issue_review | tank=%d | in=%d out=%d elapsed=%.1fs",
                        tank_id, issue_msg.usage.input_tokens, issue_msg.usage.output_tokens, time.monotonic() - t_ir)
            issue_updates = _parse_issue_updates(issue_msg.content[0].text, {i["id"] for i in issues})

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

        summary_prompt = build_summary_prompt(tank, test_results, issues, inhabitants, plants, hardscape, analysis_text)
        logger.info("Claude call: summary | tank=%d", tank_id)
        t1 = time.monotonic()
        sum_msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": summary_prompt}],
            timeout=60.0,
        )
        logger.info("Claude done: summary | tank=%d | in=%d out=%d elapsed=%.1fs",
                    tank_id, sum_msg.usage.input_tokens, sum_msg.usage.output_tokens, time.monotonic() - t1)
        summary_text = sum_msg.content[0].text

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

        logger.info("AI analysis complete for tank %d", tank_id)

    except Exception as e:
        logger.error("AI analysis failed for tank %d: %s", tank_id, e)


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

            inhabitants = rows_to_list(conn.execute(
                "SELECT * FROM inhabitants WHERE tank_id = ?", (tank_id,)
            ).fetchall())

            schedule_rows = rows_to_list(conn.execute(
                "SELECT * FROM recurring_schedule WHERE tank_id = ? AND is_active = 1", (tank_id,)
            ).fetchall())

            timeline_rows = rows_to_list(conn.execute(_TIMELINE_QUERY, (tank_id,) * 9).fetchall())

        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=28)).isoformat()
        timeline_rows = [r for r in timeline_rows if (r.get("ts") or "")[:10] >= cutoff]

        client = anthropic.Anthropic(api_key=api_key)

        prompt = build_recommendation_prompt(tank, test_result, recent_tests, issues, inhabitants, schedule_rows, timeline_rows)
        logger.info("Claude call: test_recommendation | tank=%d test=%d", tank_id, result_id)
        t0 = time.monotonic()
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        logger.info("Claude done: test_recommendation | tank=%d test=%d | in=%d out=%d elapsed=%.1fs",
                    tank_id, result_id, msg.usage.input_tokens, msg.usage.output_tokens, time.monotonic() - t0)
        recommendation = msg.content[0].text.strip()
        if not recommendation:
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
