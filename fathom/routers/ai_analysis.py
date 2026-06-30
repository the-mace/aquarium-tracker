import os
import logging
from database import get_db, rows_to_list, row_to_dict

logger = logging.getLogger(__name__)


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
    return "\n".join(f"  {r['count']}x {r.get('common_name') or r.get('species','Unknown')}" for r in rows)


def _fmt_issues(rows):
    if not rows:
        return "  None"
    return "\n".join(f"  [{r['status'].upper()}] {r['title']}: {r.get('description','')}" for r in rows)


def _fmt_events(rows):
    if not rows:
        return "  None"
    return "\n".join(f"  {r['timestamp']} {r['event_type']}: {r.get('notes','')}" for r in rows)


def build_analysis_prompt(tank, test_results, issues, events, inhabitants):
    return f"""You are an expert aquarium keeper analyzing water chemistry and tank health data.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons)

Current Inhabitants:
{_fmt_inhabitants(inhabitants)}

Recent Test Results (newest first):
{_fmt_test_results(test_results)}

Open Issues:
{_fmt_issues(issues)}

Recent Events (last 30 days):
{_fmt_events(events)}

Please provide:
1. A brief analysis of the water chemistry trends
2. Any flags or concerns about parameters outside safe ranges for this tank type
3. Specific actionable recommendations
4. For each open issue, suggest whether it should remain open, move to monitoring, or be resolved

Keep your response concise and practical. Use plain text, no markdown formatting."""


def build_summary_prompt(tank, test_results, issues, inhabitants, latest_analysis):
    return f"""You are an expert aquarium keeper. Write a concise 2-3 paragraph summary of this tank's current state for use as context in future questions.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons)

Inhabitants:
{_fmt_inhabitants(inhabitants)}

Latest Water Parameters:
{_fmt_test_results(test_results[:3])}

Open Issues:
{_fmt_issues([i for i in issues if i.get('status') != 'resolved'])}

Latest Analysis:
{latest_analysis}

Write the summary as plain text, no markdown. Be specific about current parameter values and any active concerns."""


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
                "SELECT * FROM inhabitants WHERE tank_id = ? AND count > 0",
                (tank_id,),
            ).fetchall())

        client = anthropic.Anthropic(api_key=api_key)

        analysis_prompt = build_analysis_prompt(tank, test_results, issues, events, inhabitants)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": analysis_prompt}],
        )
        analysis_text = msg.content[0].text

        related_test_id = trigger_id if trigger_type == "test" else None
        related_event_id = trigger_id if trigger_type == "event" else None

        with get_db() as conn:
            conn.execute(
                """INSERT INTO observations (tank_id, related_event_id, related_test_id, source, text)
                   VALUES (?, ?, ?, 'auto', ?)""",
                (tank_id, related_event_id, related_test_id, analysis_text),
            )

        summary_prompt = build_summary_prompt(tank, test_results, issues, inhabitants, analysis_text)
        sum_msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": summary_prompt}],
        )
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
