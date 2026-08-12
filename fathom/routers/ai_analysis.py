import os
import re
import json
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


def _fmt_tank_notes(tank):
    notes = (tank.get("notes") or "").strip()
    if not notes:
        return ""
    return (
        "\nTank notes (setup hardware, accepted parameter targets/baselines, and other keeper "
        "context. Prefer this tank's accepted parameter targets over generic species norms. "
        "Notes may include historical setup details that are no longer current — for water source, "
        "dosing products, and maintenance practices, prefer the recurring schedule and recent events "
        f"when those contradict the notes): {notes}"
    )


# Shared instruction so analysis/summary don't re-assert discontinued water/dosing practices
# just because tank notes still mention them (common after import or early setup notes).
_CURRENT_PRACTICES_RULE = (
    "Describe CURRENT practices only. If tank notes mention a water source or dosing product "
    "(e.g. spring water, Seachem Equilibrium) but the recurring schedule and/or recent events "
    "show a different practice (e.g. tap/well water, Flourish only), treat the schedule and recent "
    "events as authoritative for what the keeper does now and treat the contradicting notes as "
    "historical setup context only. State what is done now (e.g. 'tap/well water, Flourish with "
    "water changes') — do not present discontinued practices as current, and do not name those "
    "discontinued products/sources at all in the summary unless needed to explain a live parameter "
    "anomaly. Never write lines like 'no longer uses spring water/Equilibrium'."
)

# Prevent inventing "typical bands" from sparse readings (e.g. TDS pen newly acquired).
_PARAMETER_BASELINE_RULE = (
    "Parameter baselines and 'typical bands': "
    "(1) Only treat a value as an accepted baseline / typical band if tank notes say so, or if "
    "there is a clear multi-reading history (several weeks) for that parameter. "
    "(2) Do NOT invent a historical typical range from one or two early readings of a newly "
    "measured parameter (common when a TDS pen is first used) — sparse early readings are not "
    "a baseline. "
    "(3) Prefer tank notes over any range you might infer from short history. "
    "(4) When fill/source water has higher GH (and usually higher TDS) than the tank, gradual "
    "TDS climb toward fill-water levels across successive water changes is expected and often "
    "desired — do not flag that as a tank-level problem or invent a lower 'prior band' to "
    "watch against. "
    "(5) Do not invent multi-week 'watch-items' for a parameter solely because two recent "
    "readings differ from one earlier sparse reading."
)


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


_HOME_WATER_SAMPLE_LABELS = {
    "tap": "tap_WC_source",
    "bottled_spring": "bottled_spring",
    "bottled_distilled": "bottled_distilled",
    "bottled": "bottled_other",
    "raw": "raw",
    "post_neutralizer": "post_neutralizer",
    "post_softener": "post_softener",
    "hose": "hose",
    "other": "other",
}

# Only these sample points may be considered as tank fill / water-change water.
# Raw, post-treatment diagnostics, hose, etc. are never injected into tank AI.
FILL_WATER_SAMPLE_POINTS = frozenset({
    "tap", "bottled_spring", "bottled_distilled", "bottled",
})

_HOME_WATER_PROMPT_RULE = (
    "Fill-water / source readings above are SHARED across tanks (not tank chemistry). "
    "ONLY tap (home WC source) and bottled water (spring/distilled/other) appear here — "
    "these are the only streams that may be used for water changes. "
    "Never invent or assume raw well, post-neutralizer, post-softener, hose, or other "
    "diagnostic home-water samples as tank fill; those are tracked separately and are "
    "irrelevant to tank water-change advice. "
    "Partial home-water logs are normal (e.g. GH-only on water-change days). When a "
    "'Current fill-water baseline' line is present, use it as the best estimate of current "
    "incoming chemistry: each parameter is the last known value with its own as-of date. "
    "Do not treat a GH-only newest row as meaning KH/nitrate/etc. are unknown if the "
    "baseline still has those values from earlier readings. "
    "For water-change advice, choose the INCOMING fill chemistry carefully: "
    "(1) Prefer tank notes and the recurring schedule over a naive 'newest home-water row' "
    "when they describe a standing practice. "
    "(2) If tank notes or schedule say water changes use tap water aged from the prior week "
    "(or similar multi-day aging/preconditioning), the water going into the tank is NOT "
    "necessarily today's latest home-water test — treat a home-water reading as the batch "
    "that would be used for the *next* week's change only if its date is ~1 week old "
    "(or older, if that is the newest available pre-aged batch). Do not claim today's fresh "
    "home-water reading is what is going into that tank on today's water change. "
    "Use the aged-batch reading for GH/KH/nitrate pull estimates when available; if only a "
    "fresh reading exists, say the change uses last week's aged water whose chemistry is "
    "assumed similar to recent home-water baselines, not identical to today's test. "
    "(3) Default fill stream is home tap (WC source). Use bottled spring/distilled only when "
    "it is the newest fill-water row that matches practice, or when notes/schedule/events "
    "say bottled water was used. "
    "(4) Compare tank parameters to that incoming water (e.g. how a % change pulls GH/KH). "
    "Do NOT flag source GH/KH as tank out-of-range. "
    "Kit nitrate near ~40–50 ppm is expected from this well (API chart colors are subjective; "
    "consistent color band is enough — do not over-precise or repeatedly flag stable ~40–50 "
    "source nitrate as a new crisis). Softener/neutralizer do not remove nitrate, so WCs from "
    "tap cannot dilute tank nitrate below source. Bottled distilled is essentially zero minerals "
    "unless remineralized; spring varies by brand — use notes/vendor when present. "
    "Household context: no infants or pregnancy; horses are all healthy adults (3+ years), "
    "no foaling — do not hedge for infants, pregnant people, mares, or foals."
)


_HOME_WATER_BLEND_LABELS = {
    "as_used": "as_used_for_WC",
    "hard": "hard_only",
    "soft": "soft_only",
    "mixed": "mixed_hard_soft",
    "unknown": "blend_unknown",
}


def _fmt_home_water(rows):
    """Format shared home/source water tests for AI prompts."""
    if not rows:
        return "  No home/source water readings recorded."
    lines = []
    for r in rows:
        parts = []
        for field in ("ph", "gh", "kh", "ammonia", "nitrite", "nitrate", "tds", "temp"):
            val = r.get(field)
            if val is not None:
                parts.append(f"{field.upper()}={val}")
        sp = r.get("sample_point") or "tap"
        label = _HOME_WATER_SAMPLE_LABELS.get(sp, sp)
        tags = [label]
        if r.get("is_lab_test"):
            tags.append("lab")
        blend = r.get("water_blend")
        if blend:
            tags.append(_HOME_WATER_BLEND_LABELS.get(blend, blend))
        tag_str = ", ".join(tags)
        params = ", ".join(parts) if parts else "(no numeric params)"
        line = f"  {r.get('timestamp')}: [{tag_str}] {params}"
        if r.get("notes"):
            line += f" | {r['notes']}"
        lines.append(line)
    return "\n".join(lines)


def _baseline_from_fill_rows(rows):
    """Composite last-known-per-param for the newest fill-water sample stream."""
    if not rows:
        return None
    from routers.home_water import build_home_water_baseline
    newest_sp = (rows[0].get("sample_point") or "tap").strip().lower() or "tap"
    same = [
        r for r in rows
        if ((r.get("sample_point") or "tap").strip().lower() or "tap") == newest_sp
    ]
    return build_home_water_baseline(same)


def _fmt_home_water_baseline(baseline):
    """One-line composite baseline for AI prompts."""
    if not baseline or not baseline.get("params"):
        return "  (no numeric baseline yet)"
    sp = baseline.get("sample_point") or "tap"
    label = _HOME_WATER_SAMPLE_LABELS.get(sp, sp)
    parts = []
    for p in baseline["params"]:
        ts = (p.get("timestamp") or "")[:10] or "?"
        parts.append(f"{p['key'].upper()}={p['value']} (as of {ts})")
    composite = "composite" if baseline.get("is_composite") else "single reading"
    return f"  [{label}; {composite}] " + ", ".join(parts)


def _fmt_home_water_block(rows):
    """Baseline + recent reading history for AI fill-water sections."""
    if not rows:
        return "  No home/source water readings recorded."
    baseline = _baseline_from_fill_rows(rows)
    lines = [
        "Current fill-water baseline (last known value per parameter; dates may differ "
        "when logs are partial — e.g. GH-only on WC days):",
        _fmt_home_water_baseline(baseline),
        "Recent fill-water readings (newest first):",
        _fmt_home_water(rows),
    ]
    return "\n".join(lines)


def load_home_water_tests(conn, limit=24):
    """Load recent *fill-water* home readings for tank AI (tap + bottled only).

    Raw / post-treatment / hose / other diagnostic samples are excluded — they are
    for home-water suitability and history, not tank water-change analysis.
    Default limit is high enough that a stretch of GH-only WC-day logs still leaves
    older KH/nitrate/etc. available for the composite baseline.
    """
    placeholders = ",".join("?" * len(FILL_WATER_SAMPLE_POINTS))
    return rows_to_list(conn.execute(
        f"""SELECT * FROM home_water_tests
            WHERE sample_point IN ({placeholders})
               OR sample_point IS NULL
               OR sample_point = ''
            ORDER BY timestamp DESC, id DESC LIMIT ?""",
        (*FILL_WATER_SAMPLE_POINTS, limit),
    ).fetchall())


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


def _fmt_goals(rows):
    """Format active goals for AI prompts (includes targets, deps, progress)."""
    if not rows:
        return "  None"
    lines = []
    for r in rows:
        parts = [f"  [{r.get('status', 'open').upper()}] {r['title']}"]
        if r.get("target"):
            parts.append(f"    Target: {r['target']}")
        if r.get("description"):
            parts.append(f"    {r['description']}")
        deps = r.get("dependencies") or []
        if deps:
            dep_bits = []
            for d in deps:
                tank_bit = f"{d.get('tank_name')}: " if d.get("tank_name") else ""
                dep_bits.append(f"{tank_bit}{d['title']} [{d.get('status', '?')}]")
            parts.append(f"    Depends on: {'; '.join(dep_bits)}")
            if r.get("blocked"):
                parts.append("    Status: BLOCKED (one or more dependencies not yet achieved)")
        if r.get("progress_summary"):
            parts.append(f"    Progress: {r['progress_summary']}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _fmt_goals_for_progress(rows):
    """Compact goal list with ids for the progress-update JSON prompt."""
    if not rows:
        return "  None"
    lines = []
    for r in rows:
        bits = [f"id={r['id']}", f"[{r.get('status', 'open').upper()}]", r["title"]]
        if r.get("target"):
            bits.append(f"— target: {r['target']}")
        if r.get("description"):
            bits.append(f"— {r['description']}")
        deps = r.get("dependencies") or []
        if deps:
            dep_s = "; ".join(
                f"{(d.get('tank_name') + ': ') if d.get('tank_name') else ''}{d['title']} [{d.get('status','?')}]"
                for d in deps
            )
            bits.append(f"— depends on: {dep_s}")
        if r.get("progress_summary"):
            bits.append(f"— prior progress note: {r['progress_summary']}")
        lines.append("  " + " ".join(bits))
    return "\n".join(lines)


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
            tod = r.get("time_of_day")
            when = f"{dow} {tod.upper()}" if tod in ("am", "pm") else dow
            lines.append(f"  [{cat}] {desc} — {when}")
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


def build_recommendation_prompt(tank, test_result, recent_tests, issues, inhabitants, schedule_rows, timeline_rows,
                                home_water_tests=None, goals=None):
    home_water_tests = home_water_tests or []
    goals = goals or []
    return f"""You are helping during routine aquarium maintenance, right after a water test was just logged. Write a short status update the keeper will read immediately, mid-maintenance.

Background context (use this ONLY to judge whether something needs attention — e.g. species-appropriate parameter ranges for the inhabitants below, or whether a scheduled task is overdue. Do NOT summarize or restate this background in your answer; the keeper already knows their own tank contents):

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}
Inhabitants: {_fmt_inhabitants(inhabitants)}
Open issues: {_fmt_issues(issues)}
Active goals (longer-horizon aims — only mention if this test clearly advances or blocks one):
{_fmt_goals(goals)}
Recurring feeding/dosing/maintenance schedule:
{_fmt_schedule(schedule_rows)}
Tank activity over the last 4 weeks (newest first):
{_fmt_timeline_rows(timeline_rows)}

Fill water for water changes (tap WC source and/or bottled only — NOT raw/diagnostic):
{_fmt_home_water_block(home_water_tests)}
{_HOME_WATER_PROMPT_RULE}

Water test just recorded (newest) plus recent tests for trend comparison:
{_fmt_test_results([test_result] + [t for t in recent_tests if t.get('id') != test_result.get('id')])}

Now write the actual response. Cover only what's relevant, briefly:
1. Open issues — one short line (e.g. "No open issues at this time."). Skip if genuinely nothing to say.
2. Any water parameter values or trends worth flagging vs. the recent tests above (e.g. a drop/rise since the last test, or a value outside the *safe tolerance* range for the inhabitants). Use precise, species-specific tolerance ranges rather than overly cautious defaults. A value outside a narrower "ideal"/breeding-optimal sub-range but still within safe tolerance is NOT a concern — at most note it's outside the ideal range for breeding/growth; reserve concern language for values actually near or outside the safe tolerance boundary. Only mention parameters that are actually notable, skip the rest.
{_PARAMETER_BASELINE_RULE}
3. The action to take now. Usually this is simply "Proceed with the standard water change" per the schedule above — do not restate the schedule's gallons/dose/interval details, the keeper already has those. Only describe something different if this test's results or recent history genuinely call for a different action. When a water change is the plan, you may briefly note how incoming home-water GH/KH will pull tank parameters if that is actually material (skip if home water is unknown or already aligned).
4. Goals — only if this reading clearly moves an active goal forward or shows it is stalled (e.g. GH finally in range for a stocking goal). One short clause max; skip if nothing goal-related changed.

2-4 sentences total, plain text, no markdown, no headers, no preamble like "Recommendation:" or "Analysis:" — this text is appended directly to the test result's own notes field."""


def build_analysis_prompt(tank, test_results, issues, events, inhabitants, plants, hardscape,
                          schedule_rows=None, home_water_tests=None, goals=None):
    schedule_rows = schedule_rows or []
    home_water_tests = home_water_tests or []
    goals = goals or []
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

Fill water for water changes (tap WC source and/or bottled only — NOT raw/diagnostic):
{_fmt_home_water_block(home_water_tests)}
{_HOME_WATER_PROMPT_RULE}

Open Issues:
{_fmt_issues(issues)}

Active Goals (longer-horizon aims for this tank — water targets, stocking, breeding; may depend on goals on other tanks):
{_fmt_goals(goals)}

Recurring schedule (current planned feeding/dosing/maintenance — authoritative for what the keeper currently does):
{_fmt_schedule(schedule_rows)}

Recent Events (last 30 days — evidence of actual practices, including water source and dosing):
{_fmt_events(events)}

{_CURRENT_PRACTICES_RULE}

{_PARAMETER_BASELINE_RULE}

Please provide:
1. A brief analysis of the water chemistry trends (include how tank parameters relate to fill/source water when relevant, especially after water changes)
2. Any flags or concerns about parameters outside the *safe tolerance* range for this tank's inhabitants — use precise, species-specific ranges rather than overly cautious defaults. A value outside a narrower "ideal"/breeding-optimal sub-range but still within safe tolerance is NOT a concern — at most note it's outside the ideal range for breeding/growth; reserve concern language for values actually near or outside the safe tolerance boundary.
3. Specific actionable recommendations
4. For each open issue, suggest whether it should remain open, move to monitoring, or be resolved
5. Progress toward active goals when the data speaks to them (e.g. GH vs a stocking target) — brief, only when relevant
6. If the latest test's own notes mention something new (an inhabitant added/removed, an action taken, a change noticed) that isn't already reflected in the Current Inhabitants/Plants/Hardscape lists above, acknowledge it explicitly — don't let it get crowded out by the water-chemistry discussion.

Keep your response concise and practical. Use plain text, no markdown formatting."""


def build_summary_prompt(tank, test_results, issues, inhabitants, plants, hardscape, latest_analysis,
                         schedule_rows=None, events=None, home_water_tests=None, goals=None):
    schedule_rows = schedule_rows or []
    events = events or []
    home_water_tests = home_water_tests or []
    goals = goals or []
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

Fill water for water changes (tap WC source and/or bottled only — NOT raw/diagnostic):
{_fmt_home_water_block(home_water_tests)}
{_HOME_WATER_PROMPT_RULE}

Open Issues:
{_fmt_issues([i for i in issues if i.get('status') != 'resolved'])}

Active Goals:
{_fmt_goals(goals)}

Recurring schedule (current planned feeding/dosing/maintenance — authoritative for what the keeper currently does):
{_fmt_schedule(schedule_rows)}

Recent Events (last 30 days — evidence of actual practices, including water source and dosing):
{_fmt_events(events)}

Latest Analysis:
{latest_analysis}

{_CURRENT_PRACTICES_RULE}

{_PARAMETER_BASELINE_RULE}

Write the summary as plain text, no markdown. Be specific about current parameter values, inhabitants, current water source and dosing practice (from schedule/events and home-water readings, not obsolete notes), active goals when they matter for "what the keeper is working toward", and any active concerns. If the latest analysis or the latest test's notes mention a new development (an inhabitant added/removed, an action taken) not yet reflected in the Inhabitants/Plants/Hardscape lists above, mention it — this summary is what future questions rely on for "what's currently going on" context."""


def build_goal_progress_prompt(tank, goals, test_results, inhabitants, events, home_water_tests=None):
    """Prompt for per-goal progress blurbs after a water test (JSON response)."""
    home_water_tests = home_water_tests or []
    return f"""You are assessing progress toward aquarium goals right after a new water test.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}

Inhabitants:
{_fmt_inhabitants(inhabitants)}

Recent Test Results (newest first):
{_fmt_test_results(test_results)}

Fill water for water changes (tap WC source and/or bottled only — NOT raw/diagnostic):
{_fmt_home_water_block(home_water_tests)}
{_HOME_WATER_PROMPT_RULE}

Recent Events (last 30 days):
{_fmt_events(events)}

Active goals to update (use the id field exactly):
{_fmt_goals_for_progress(goals)}

{_PARAMETER_BASELINE_RULE}

For EACH goal above, write a short progress assessment (2-4 sentences, plain text, no markdown):
- How current water params / stock / events stand relative to the goal's target and description
- Whether dependencies still block it (name them)
- What concrete next step would move it forward (one sentence max)
- Do not invent readings that aren't in the data; if data is sparse, say so briefly

Return ONLY a JSON array (no markdown fences, no extra text), one object per goal:
[{{"goal_id": <int>, "progress_summary": "<text>"}}]
Include every goal id listed above exactly once."""


def _fmt_other_tanks_stock(other_tanks_stock):
    """Format other tanks' currently stocked animals for cross-tank goal drafts."""
    if not other_tanks_stock:
        return "  (no other tanks)"
    lines = []
    for ot in other_tanks_stock:
        name = ot.get("name") or "Tank"
        vol = ot.get("volume_gallons")
        header = f"  {name}"
        if vol is not None:
            header += f" ({vol}g)"
        stock = ot.get("inhabitants") or []
        if not stock:
            lines.append(f"{header}: (no current stock)")
            continue
        bits = []
        for r in stock:
            label = (r.get("common_name") or r.get("species") or "Unknown").strip()
            c = r.get("count")
            if c is None:
                bits.append(f"{label} (many)")
            else:
                bits.append(f"{label} x{c}")
        lines.append(f"{header}: {', '.join(bits)}")
    return "\n".join(lines)


def build_goal_review_prompt(tank, draft, existing_goals, latest_test=None, inhabitants=None,
                             dep_goals=None, home_water_tests=None, other_tanks_stock=None):
    """Review a draft goal before it is saved; propose clearer/measurable wording."""
    latest_test = latest_test or {}
    inhabitants = inhabitants or []
    dep_goals = dep_goals or []
    home_water_tests = home_water_tests or []
    other_tanks_stock = other_tanks_stock or []
    draft_title = (draft.get("title") or "").strip()
    draft_target = (draft.get("target") or "").strip()
    draft_desc = (draft.get("description") or "").strip()
    draft_notes = (draft.get("notes") or "").strip()

    if latest_test:
        params = []
        for field in ("ph", "gh", "kh", "ammonia", "nitrite", "nitrate", "tds", "temp"):
            val = latest_test.get(field)
            if val is not None:
                params.append(f"{field.upper()}={val}")
        latest_line = ", ".join(params) if params else "no numeric params"
        latest_block = f"  {(latest_test.get('timestamp') or '')[:10]}: {latest_line}"
    else:
        latest_block = "  No water tests recorded yet."

    if dep_goals:
        dep_lines = "\n".join(
            f"  - [{d.get('status','?')}] {d.get('tank_name','?')}: {d.get('title','')}"
            + (f" — {d['target']}" if d.get("target") else "")
            for d in dep_goals
        )
    else:
        dep_lines = "  (none selected)"

    # Split stock so the model does not treat count=0 rows as living animals.
    current_inh = [
        r for r in inhabitants
        if r.get("count") is None or (isinstance(r.get("count"), (int, float)) and r.get("count") > 0)
    ]
    former_inh = [
        r for r in inhabitants
        if r.get("count") is not None and r.get("count") == 0
    ]
    current_block = _fmt_inhabitants(current_inh)
    if former_inh:
        former_bits = []
        for r in former_inh:
            label = (r.get("common_name") or r.get("species") or "Unknown").strip()
            former_bits.append(f"  {label} (count 0 — NOT currently in the tank)")
        former_block = "\n".join(former_bits)
    else:
        former_block = "  None"

    return f"""You are helping a keeper turn a rough draft into a clean, savable aquarium goal.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}

Latest water test:
{latest_block}

Currently stocked (count null/"many" or count > 0 only — these are in the tank now):
{current_block}

Formerly stocked / not present now (count 0 — do NOT treat as living stock; do NOT assume restocking this exact variety unless the draft names it):
{former_block}

Other tanks' currently stocked animals (for cross-tank drafts — moving/breeding/sourcing between tanks):
{_fmt_other_tanks_stock(other_tanks_stock)}

Fill/source water (context only):
{_fmt_home_water_block(home_water_tests)}

Other goals already on this tank (avoid near-duplicates; note conflicts in summary/suggestions only):
{_fmt_goals(existing_goals)}

Dependencies the keeper selected for this new goal:
{dep_lines}

Draft goal (user's rough intent — interpret carefully; do not invent facts):
  Title: {draft_title or '(empty)'}
  Target: {draft_target or '(empty)'}
  Description: {draft_desc or '(empty)'}
  Notes: {draft_notes or '(empty)'}

Your job has TWO separate outputs:

1) FEEDBACK (summary + suggestions only):
- Is this a reasonable goal for this tank?
- What's vague, missing, duplicate, already true, or blocked?
- What must the keeper clarify (species, destination tank, duration)?
- Put ALL critique, questions, and "please clarify X" language HERE only
- READ the full draft (title + target + description + notes) before claiming species is unknown
- If the draft says stock will come from another tank ("shrimp tank", "other tank", "from the X tank", "both tanks", "move from"), you MUST cross-reference "Other tanks' currently stocked animals" and resolve species from that source tank when it matches
- Only ask "which species?" when neither this tank's current stock NOR a named/implied source tank's stock can resolve the draft

2) PROPOSED GOAL FIELDS (title/target/description/notes):
- These are the actual goal text that will be saved if the user clicks Save
- ALWAYS rewrite rough drafts into polished finished goal text — summary/suggestions can discuss gaps, but proposed must still be a complete goal the user could save
- NEVER leave proposed equal to a casual draft (lowercase starts, trailing "?", "minimum?", "good for them", fragments)
- Title: short, clear, proper capitalization (e.g. "Raise GH for Amano shrimp") — not "Get GH ready for amano shrimp" or "Clarify …"
- Target: ALWAYS a concrete measurable line for parameter goals. Must include ideal range AND (when useful) tolerable range AND a hold/timeline. Never leave "gh 5 minimum?" or similar.
  Example for Amano shrimp (when draft names Amano): "Ideal GH 6–8 dGH (tolerable ~4–10 dGH); hold ideal for 2–4 consecutive weeks before adding Amano shrimp"
  If draft suggests a minimum (e.g. "GH 5 minimum?"), convert to a proper ideal+tolerable target using species-appropriate care ranges for species named in the draft — that is care knowledge, not inventing stock
- Description: 2–4 complete sentences with capitalization and punctuation: why, how success is judged, realistic approach (dosing/WC) given tank + fill water; if sourcing from another tank, name that tank and stock
- Notes: optional short keeper notes only; leave "" if nothing useful

SPECIES / FACTUAL ACCURACY — do not hallucinate stock; DO use care ranges for named species; DO cross-reference other tanks:
- Allowed species names in proposed fields only if they appear in: (a) the draft text, (b) this tank's Currently stocked list, or (c) a source tank named/implied in the draft under "Other tanks' currently stocked animals"
- count=0 / formerly stocked on THIS tank is NOT by itself permission to name that variety
- When draft implies a source tank and that tank has a clear matching animal, USE that name — do not claim species is unknown
- If multiple matching varieties on the source tank, ask which one in suggestions; if only one clear match, use it
- Do not invent cultivars not present in draft / current stock / referenced source tank stock
- Care parameter ranges (GH/KH/temp/pH) for a species named in the draft ARE allowed and expected (e.g. draft says "Amano" → give Amano-appropriate GH ideal/tolerable ranges even if Amano count is 0)

CRITICAL — never put review feedback into proposed fields:
- No "undefined", "needs specific…", "before this can be tracked"
- No "draft intent is unclear", "if the goal is…", "confirm whether…"
- No scolding about what is already stocked
- Feedback belongs only in summary/suggestions; proposed is always save-ready polished goal text
- Only copy the draft into proposed if it is already production-quality (proper capitalization, no "?", complete measurable target with ranges)

Return ONLY JSON (no markdown fences, no text before or after the object):
{{
  "reasonable": <true|false>,
  "summary": "<2-4 sentences of assessment/feedback only>",
  "suggestions": ["<feedback bullet 1>", "..."],
  "proposed": {{
    "title": "<finished goal title>",
    "target": "<finished measurable target with ideal/tolerable ranges and timeline>",
    "description": "<finished goal description, complete sentences>",
    "notes": "<optional notes or empty string>"
  }}
}}

No markdown in any string."""


# Phrases that mean the model wrote reviewer meta-text into a proposed goal field.
_GOAL_REVIEW_META_PATTERNS = re.compile(
    r"(?i)\b("
    r"undefined|needs?\s+specific|before\s+this\s+can\s+be\s+tracked|"
    r"draft\s+intent|intent\s+is\s+unclear|if\s+the\s+goal\s+is|"
    r"confirm\s+whether|clarify\s+(the\s+)?|needs?\s+clarif|"
    r"this\s+likely\s+duplicates|not\s+a\s+goal|cannot\s+be\s+tracked|"
    r"you\s+should|the\s+user\s+should|keeper\s+should|"
    r"still\s+needs\s+to\s+be\s+specified|needs\s+to\s+be\s+specified|"
    r"by\s+the\s+keeper"
    r")\b"
)


def _looks_like_review_meta(text: str) -> bool:
    """True if text is critique/instructions rather than savable goal wording."""
    if not text or not text.strip():
        return False
    t = text.strip()
    if _GOAL_REVIEW_META_PATTERNS.search(t):
        return True
    # Titles that start with review verbs
    if re.match(r"(?i)^(clarify|review|fix|define|specify|decide)\b", t):
        return True
    return False


def _draft_looks_rough(draft: dict) -> bool:
    """True if draft still needs a polished rewrite (not save-ready as-is)."""
    title = (draft.get("title") or "").strip()
    target = (draft.get("target") or "").strip()
    desc = (draft.get("description") or "").strip()
    blob = f"{title} {target} {desc}"
    if "?" in blob:
        return True
    if title and title[0].islower():
        return True
    if target and (target[0].islower() or len(target) < 20):
        return True
    if re.search(r"(?i)\b(min(imum)?|good for them|better|stuff)\b", blob):
        return True
    # Parameter goal without a numeric range in target
    if re.search(r"(?i)\b(gh|kh|ph|tds|temp|nitrate)\b", blob) and not re.search(r"\d", target):
        return True
    return False


def _proposed_needs_rewrite(proposed: dict, draft: dict) -> bool:
    """True if proposed still mirrors a rough draft (failed to polish)."""
    if not _draft_looks_rough(draft):
        return False
    pt = (proposed.get("target") or "").strip().lower()
    dt = (draft.get("target") or "").strip().lower()
    ptitle = (proposed.get("title") or "").strip().lower()
    dtitle = (draft.get("title") or "").strip().lower()
    # Target barely changed or still has ?
    if pt == dt or "?" in (proposed.get("target") or ""):
        return True
    if ptitle == dtitle and dtitle and dtitle[0:1].islower():
        return True
    # Target still lacks numbers when draft was a parameter goal
    blob = f"{draft.get('title','')} {draft.get('target','')}"
    if re.search(r"(?i)\b(gh|kh|ph|tds)\b", blob) and not re.search(r"\d", proposed.get("target") or ""):
        return True
    return False


def _parse_goal_review(raw: str, draft: dict) -> dict:
    """Parse goal-review JSON; fall back to draft fields when parsing fails."""
    draft_out = {
        "title": (draft.get("title") or "").strip(),
        "target": (draft.get("target") or "").strip(),
        "description": (draft.get("description") or "").strip(),
        "notes": (draft.get("notes") or "").strip(),
    }
    fallback = {
        "reasonable": False,
        "summary": "Could not parse AI review — you can save the goal as written or try Re-review.",
        "suggestions": [],
        "proposed": dict(draft_out),
        "changed": False,
        "parse_failed": True,
    }
    if not raw:
        return fallback
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Extract first balanced {...} object (handles leading/trailing prose)
        start = text.find("{")
        if start >= 0:
            depth = 0
            in_str = False
            esc = False
            end = -1
            for i, ch in enumerate(text[start:], start):
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    data = None
        if data is None:
            logger.warning("Goal review parse failed: %s", text[:200])
            return fallback
    if not isinstance(data, dict):
        return fallback

    proposed_in = data.get("proposed") if isinstance(data.get("proposed"), dict) else {}
    proposed = {
        "title": (proposed_in.get("title") if proposed_in.get("title") is not None else draft_out["title"]),
        "target": (proposed_in.get("target") if proposed_in.get("target") is not None else draft_out["target"]),
        "description": (
            proposed_in.get("description") if proposed_in.get("description") is not None
            else draft_out["description"]
        ),
        "notes": (proposed_in.get("notes") if proposed_in.get("notes") is not None else draft_out["notes"]),
    }
    for k in proposed:
        proposed[k] = (proposed[k] or "").strip() if isinstance(proposed[k], str) else draft_out[k]

    # If the model dumped reviewer feedback into proposed fields, keep the user's draft
    # for those fields rather than showing meta-text as the "goal".
    for k in ("title", "target", "description", "notes"):
        if _looks_like_review_meta(proposed[k]):
            logger.info("Goal review proposed.%s looked like meta-feedback; using draft", k)
            proposed[k] = draft_out[k]

    suggestions = data.get("suggestions") or []
    if not isinstance(suggestions, list):
        suggestions = []
    suggestions = [str(s).strip() for s in suggestions if str(s).strip()]

    summary = (data.get("summary") or "").strip() or fallback["summary"]
    reasonable = data.get("reasonable")
    if not isinstance(reasonable, bool):
        reasonable = True

    changed = any(proposed[k] != draft_out[k] for k in draft_out)
    return {
        "reasonable": reasonable,
        "summary": summary,
        "suggestions": suggestions,
        "proposed": proposed,
        "changed": changed,
    }


def _parse_goal_progress_updates(raw: str, valid_ids: set[int]) -> list[dict]:
    """Parse Claude's goal-progress JSON; drop unknown ids / empty summaries."""
    if not raw or not valid_ids:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            logger.warning("Goal progress parse failed (no JSON array): %s", text[:200])
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            logger.warning("Goal progress parse failed: %s", text[:200])
            return []
    if not isinstance(data, list):
        return []
    out = []
    seen: set[int] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            gid = int(item.get("goal_id"))
        except (TypeError, ValueError):
            continue
        if gid not in valid_ids or gid in seen:
            continue
        summary = (item.get("progress_summary") or "").strip()
        if not summary:
            continue
        seen.add(gid)
        out.append({"goal_id": gid, "progress_summary": summary})
    return out


def build_notes_proposal_prompt(tank, schedule_rows, events, test_results, home_water_tests=None):
    """Ask Claude whether tank notes should be refreshed from schedule/events."""
    current = (tank.get("notes") or "").strip() or "(empty — no notes set)"
    home_water_tests = home_water_tests or []
    return f"""You review whether a tank's free-text notes field is out of date versus current practice.

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons)

Current tank notes:
{current}

Active recurring schedule (authoritative for planned maintenance/dosing/feeding):
{_fmt_schedule(schedule_rows)}

Recent events (last 30 days — evidence of actual water source and dosing):
{_fmt_events(events)}

Fill water readings (tap WC source and/or bottled only — measured incoming water for changes; prefer over free-text guesses):
{_fmt_home_water_block(home_water_tests)}

Recent test results with notes (newest first; may record accepted parameter baselines):
{_fmt_test_results(test_results[:6])}

Decide if the notes need updating so future AI summaries stop using stale practice info.
Focus only on durable standing facts that notes should capture:
- water source (spring water vs home tap/well, RO, aging/preconditioning, softener/neutralizer mix)
- regular dosing products (Equilibrium, Flourish, Prime, Potassium, Iron, etc.)
- accepted parameter targets/baselines the keeper has explicitly accepted (e.g. KH ~10 is permanent)
- regular water-change practice when it differs from what notes claim
- optional: approximate home-water GH/KH when structured home-water readings exist and notes still invent different numbers

Do NOT propose an update for:
- trivial wording or style differences
- feeding details (those live on the schedule)
- one-off events, temporary issues, or inhabitant count changes
- inventing facts not supported by schedule, events, home-water readings, or test notes
- inventing multi-reading "typical bands" for a parameter from only 1–2 early readings (especially TDS after a new pen) — only revise accepted baselines when notes are clearly wrong vs sustained history or explicit keeper acceptance
- copying every home-water history row into notes (a single current tap baseline is enough if useful)

Preserve accurate hardware/setup text that is still true (dimensions, filter media, location, etc.).
If notes are empty but schedule/events establish clear standing practices, you may propose a concise notes block.

Return ONLY a JSON object (no markdown fences, no commentary):
{{"update_needed": true|false, "reason": "1-2 sentences citing the contradiction", "proposed_notes": "full replacement notes text if update_needed, else empty string"}}

When update_needed is true, proposed_notes must be the complete new notes field (not a diff),
similar length/style to the current notes when possible, and must state CURRENT practice only."""


def _parse_notes_proposal(raw_text, current_notes):
    """Parse notes-proposal JSON; return dict or None if no update should be stored."""
    text = re.sub(r"```json\s*", "", raw_text or "")
    text = re.sub(r"```\s*", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    if not parsed.get("update_needed"):
        return None
    proposed = (parsed.get("proposed_notes") or "").strip()
    reason = (parsed.get("reason") or "").strip()
    if not proposed or not reason:
        return None
    current = (current_notes or "").strip()
    if proposed == current:
        return None
    return {"proposed_notes": proposed, "reason": reason, "prior_notes": current or None}


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

        client = anthropic.Anthropic(api_key=api_key)

        analysis_prompt = build_analysis_prompt(
            tank, test_results, issues, events, inhabitants, plants, hardscape, schedule_rows,
            home_water_tests=home_water_tests, goals=goals,
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
        )

        logger.info("AI analysis complete for tank %d", tank_id)

    except Exception as e:
        logger.error("AI analysis failed for tank %d: %s", tank_id, e, exc_info=True)
        _record_analysis_failure(tank_id, trigger_type, trigger_id, e)


async def _maybe_propose_tank_notes_update(client, tank_id, tank, schedule_rows, events, test_results,
                                          home_water_tests=None):
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

        client = anthropic.Anthropic(api_key=api_key)
        prompt = build_goal_progress_prompt(
            tank, goals, test_results, inhabitants, events,
            home_water_tests=home_water_tests,
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
