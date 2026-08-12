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
                                home_water_tests=None):
    home_water_tests = home_water_tests or []
    return f"""You are helping during routine aquarium maintenance, right after a water test was just logged. Write a short status update the keeper will read immediately, mid-maintenance.

Background context (use this ONLY to judge whether something needs attention — e.g. species-appropriate parameter ranges for the inhabitants below, or whether a scheduled task is overdue. Do NOT summarize or restate this background in your answer; the keeper already knows their own tank contents):

Tank: {tank['name']} ({tank.get('water_type','unknown')} water, {tank.get('volume_gallons','?')} gallons){_fmt_tank_notes(tank)}
Inhabitants: {_fmt_inhabitants(inhabitants)}
Open issues: {_fmt_issues(issues)}
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

2-4 sentences total, plain text, no markdown, no headers, no preamble like "Recommendation:" or "Analysis:" — this text is appended directly to the test result's own notes field."""


def build_analysis_prompt(tank, test_results, issues, events, inhabitants, plants, hardscape,
                          schedule_rows=None, home_water_tests=None):
    schedule_rows = schedule_rows or []
    home_water_tests = home_water_tests or []
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
5. If the latest test's own notes mention something new (an inhabitant added/removed, an action taken, a change noticed) that isn't already reflected in the Current Inhabitants/Plants/Hardscape lists above, acknowledge it explicitly — don't let it get crowded out by the water-chemistry discussion.

Keep your response concise and practical. Use plain text, no markdown formatting."""


def build_summary_prompt(tank, test_results, issues, inhabitants, plants, hardscape, latest_analysis,
                         schedule_rows=None, events=None, home_water_tests=None):
    schedule_rows = schedule_rows or []
    events = events or []
    home_water_tests = home_water_tests or []
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

Recurring schedule (current planned feeding/dosing/maintenance — authoritative for what the keeper currently does):
{_fmt_schedule(schedule_rows)}

Recent Events (last 30 days — evidence of actual practices, including water source and dosing):
{_fmt_events(events)}

Latest Analysis:
{latest_analysis}

{_CURRENT_PRACTICES_RULE}

{_PARAMETER_BASELINE_RULE}

Write the summary as plain text, no markdown. Be specific about current parameter values, inhabitants, current water source and dosing practice (from schedule/events and home-water readings, not obsolete notes), and any active concerns. If the latest analysis or the latest test's notes mention a new development (an inhabitant added/removed, an action taken) not yet reflected in the Inhabitants/Plants/Hardscape lists above, mention it — this summary is what future questions rely on for "what's currently going on" context."""


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
            home_water_tests=home_water_tests,
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
            schedule_rows, events, home_water_tests=home_water_tests,
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
            home_water_tests=home_water_tests,
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
