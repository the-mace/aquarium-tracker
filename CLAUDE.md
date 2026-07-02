# Fathom — CLAUDE.md

Project context for AI-assisted development sessions.

## What this is

Fathom is a personal aquarium tracking web app with AI-powered analysis. Single user, self-hosted. No auth, no multi-tenancy.

## Stack

- **Backend**: Python 3 + FastAPI, uvicorn
- **Database**: SQLite at `fathom/data/fathom.db` (gitignored)
- **Templates**: Jinja2, plain HTML/CSS/JS
- **Charts**: Chart.js 4.4.0 via CDN
- **AI**: Anthropic Python SDK `0.115.0`, model `claude-sonnet-4-6`
- **Env**: python-dotenv, `.env` at repo root (gitignored)

## How to run

```bash
cd fathom
source ../.venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The `.venv` is at `aquarium-tracker/.venv` (one level above `fathom/`).

## Checking current issues / live logs

All logs (uvicorn + app) are teed to `/tmp/fathom.log` (RotatingFileHandler, 5 MB, 2 backups) in addition to stdout. To watch live:

```bash
tail -f /tmp/fathom.log
```

To check recent background task activity (reference info fetches, AI analysis errors):

```bash
grep -E "reference_info|ai_analysis|ERROR|WARNING" /tmp/fathom.log | tail -50
```

On the production Mac mini the launchd stderr goes to `/tmp/fathom.err`; app logs go to `/tmp/fathom.log` there too.

## Project structure

```
aquarium-tracker/
├── fathom/
│   ├── main.py              # App entry, router includes, startup init_db()
│   ├── database.py          # Schema (10 tables), get_db() context manager, helpers
│   ├── routers/
│   │   ├── tanks.py         # Tank CRUD, dashboard, chart data endpoints
│   │   ├── test_results.py  # Water test CRUD, triggers AI analysis
│   │   ├── events.py        # Event log CRUD, triggers AI analysis
│   │   ├── inhabitants.py   # Species + population events
│   │   ├── equipment.py     # Equipment per tank
│   │   ├── purchases.py     # Purchase tracking
│   │   ├── issues.py        # Issue tracker with status workflow
│   │   ├── observations.py  # Manual + AI observations
│   │   ├── chat.py          # AI chat (in-memory session, 10-turn limit)
│   │   ├── import_data.py   # File upload → Claude extraction → bulk insert
│   │   ├── schedules.py     # Recurring schedule CRUD + mark-done
│   │   └── ai_analysis.py   # BackgroundTask: auto-analysis on test/event save
│   ├── templates/
│   │   ├── base.html        # Sidebar nav, Chart.js CDN
│   │   ├── tanks/           # list, detail, form, import, schedule
│   │   ├── inhabitants/     # list with inline edit modal
│   │   ├── observations/    # list
│   │   └── ...              # other section templates
│   ├── static/
│   │   ├── css/style.css    # Dark ocean theme (--bg: #0a0f1e, --primary: #00c4a0)
│   │   └── js/app.js        # Modal helpers, chat panel, Chart.js loaders
│   ├── data/                # SQLite DB lives here (gitignored)
│   └── scripts/
│       └── backup_db.sh     # gzip + aws s3 cp backup
├── .env                     # ANTHROPIC_API_KEY, AWS config (gitignored)
├── .env.example             # Template for .env
├── requirements.txt
├── README.md
└── CLAUDE.md                # This file
```

## Database schema (13 tables)

All tank-scoped tables have `tank_id` with `ON DELETE CASCADE`.

| Table | Purpose |
|---|---|
| `tanks` | Tank metadata (name, volume, setup_date, status) |
| `test_results` | Water tests (ph, gh, kh, ammonia, nitrite, nitrate, tds, temp) |
| `events` | Event log (event_type, amount, notes) |
| `inhabitants` | Current stock per species (count, added_date, source) |
| `population_events` | Per-inhabitant history (added/died/removed/born) |
| `purchases` | Spending (item, category, vendor, cost, purchase_date) |
| `tank_equipment` | Equipment (category, brand, model, specs JSON, is_active) |
| `issues` | Issue tracker (status: open/investigating/resolved, opened_at, resolved_at) |
| `observations` | AI (source=auto) and manual (source=manual) notes |
| `tank_state_summary` | Latest AI summary upserted per tank |
| `recurring_schedule` | Feeding/dosing/maintenance plans; tracking_mode=reference_only (no log) or logged (due-date tracking) |
| `plants` | Active plants per tank (species, common_name, added_date, source, notes, status active/removed) |
| `hardscape` | Hardscape items (item, quantity, source, cost, added_date, notes) |
| `reference_info` | Species/plant/hardscape descriptions + care notes + image URLs from Claude web search. UNIQUE(entity_type, entity_name). |

## AI features

### Background analysis (`ai_analysis.py`)
Triggered as FastAPI `BackgroundTask` after every test_result or event save. Fetches last 10 tests, open issues, 30-day events, and inhabitants. Calls Claude, stores result as an `observations` row (source=auto) and upserts `tank_state_summary`.

### Reference Info (`reference_info.py`)
Background task triggered when inhabitants, plants, or hardscape items are added (or imported). Checks if a `reference_info` row already exists for that entity; if not, inserts a placeholder and queues `fetch_reference_info_bg`. That sync background task calls `claude-sonnet-4-6` with `web_search_20260209` (server-side tool — no client tool loop needed) to fetch: description, care notes, and a Wikimedia Commons image URL. Result stored with `ON CONFLICT … DO UPDATE`.

List views (`/inhabitants`, `/plants`) also trigger auto-queue on first load for any entity not yet in `reference_info`. The list query does a LEFT JOIN on `reference_info` to pass data to templates.

UI: small 46px thumbnail in table; click → modal with larger image, description, care notes, attribution, and "Refresh Info" button (POST `/reference-info/refresh`). Shows ⏳ while pending, ℹ when fetched but no image.

Routes (prefix-free router): `GET /reference-info?entity_type=…&entity_name=…`, `POST /reference-info/refresh` (JSON body).

`entity_name` is always `lower(trim(species or common_name))` for species/plants, `lower(trim(item))` for hardscape — this is the canonical UNIQUE key.

Web search tool requires anthropic SDK ≥ 0.115.0.

### Chat (`chat.py`)
`POST /tanks/{id}/chat` — in-memory `_conversations` dict keyed by tank_id, max 10 turns. System prompt injects tank summary + 3 recent observations. Returns 503 if no API key. `DELETE /tanks/{id}/chat` clears history.

### Import (`import_data.py`)
`POST /tanks/{id}/import` — uploads a file (HTML or plain text/markdown), strips HTML if needed, sends to Claude for structured extraction, returns JSON preview. `POST /tanks/{id}/import/confirm` bulk-inserts the confirmed preview. Claude extracts: test_results, events, purchases, inhabitants, equipment.

Import robustness: strips markdown code fences, falls back to regex `{...}` extraction if direct JSON parse fails. `max_tokens=8192`.

## Key decisions & gotchas

- **No React, no build step** — all templates are Jinja2 + vanilla JS. Keep it that way.
- **SQLite WAL mode + foreign_keys ON** — set in `get_connection()` in `database.py`.
- **Router prefix issue** — `import_data.py` uses `APIRouter(tags=["import"])` with no prefix; routes include full paths like `/tanks/{id}/import`. Other routers use `prefix="/tanks"`.
- **Observations delete** — JS calls `POST /tanks/{id}/observations/{obs_id}/delete` (not DELETE verb, form-based).
- **Inhabitants edit** — inline modal populated by `openEditInh()` JS function, sets form action to `/{id}/update`. No separate edit page.
- **venv location** — `.venv` is at repo root (`aquarium-tracker/.venv`), not inside `fathom/`. Activate with `source ../.venv/bin/activate` from the `fathom/` directory.
- **Pre-commit hook** — scans staged files for `sk-ant-`, `AKIA`, GitHub tokens, etc. Will block commits with secrets. Hook is at `.git/hooks/pre-commit`.

## Production deployment

Mac mini at `192.168.50.205`, SSH via `ssh -A rob@192.168.50.205`. Repo at `/Users/rob/aquarium-tracker` (same layout as dev). Run as a launchd system service:

- Plist: `/Library/LaunchDaemons/com.fathom.plist`
- Uvicorn binary: `/Users/rob/aquarium-tracker/.venv/bin/uvicorn`
- Working dir: `/Users/rob/aquarium-tracker/fathom`
- Env: `DOTENV_PATH=/Users/rob/aquarium-tracker/.env`
- Logs: `/tmp/fathom.log` / `/tmp/fathom.err`

Reload after deploy: `ssh -A rob@192.168.50.205 "sudo launchctl unload /Library/LaunchDaemons/com.fathom.plist && sudo launchctl load /Library/LaunchDaemons/com.fathom.plist"`

S3 backup cron (3am daily): `0 3 * * * cd /Users/rob/aquarium-tracker && bash fathom/scripts/backup_db.sh >> /tmp/fathom-backup.log 2>&1`

## Database schema (12 tables)

Added `plants` and `hardscape` tables in the 2026-06-29 session (both cascade-delete on tank).

| Table | Purpose |
|---|---|
| `plants` | Active plants per tank (species, common_name, added_date, source, notes, status active/removed) |
| `hardscape` | Hardscape items (item, quantity, source, cost, added_date, notes) |

`inhabitants.count` may be NULL to represent an uncountable population (displayed as "many" badge).

## Import pipeline (as of 2026-06-29)

Import now uses a comprehensive Claude prompt that extracts:
- `tank_specs` → UPDATE tanks row
- `test_results`, `events`, `purchases`, `inhabitants`, `equipment` (original)
- `plants`, `hardscape`, `issues`, `observations` (new)
- `flags` → returned separately for review UI (not inserted)

Claude fills in known product specs (Fluval Spec V etc.) from training data. Issues are extracted from problem/resolution narrative patterns.

The review screen shows editable tables per section with flagged rows highlighted. Users check/uncheck rows before confirming. Only selected rows are written.

## Recurring Schedule (added 2026-06-30)

`recurring_schedule` table with two tracking modes:
- **reference_only** (feeding, dosing): shown in "Today's Schedule" widget on dashboard, matching `day_of_week` to today. Multiple rows per day allowed. No logging required.
- **logged** (maintenance): shown in "Maintenance Due" widget. Tracks `last_done` + `next_due` (= last_done + interval_days). Mark-done creates a linked `events` row and updates due date.

`events.schedule_id` (nullable FK): links maintenance events to their schedule entry. Set automatically by mark-done; can also be set when logging events manually.

Import prompt includes rule 10 for parsing `recurring_schedule` from narrative text.

Management page: `/tanks/{id}/schedule`

## Quick Log (added 2026-06-30)

Primary logging workflow — textarea instead of file upload:

- **Dashboard button**: "⚡ Quick Log" (highlighted primary style) opens a modal with a large textarea
- On submit: text is stored in `sessionStorage`, browser navigates to `/tanks/{id}/quick-log-page`
- That page auto-reads the text and POSTs JSON `{"text": "..."}` to `POST /tanks/{id}/quick-log`
- Today's date is prepended as `[Today's date: YYYY-MM-DD]` so undated entries default to today
- Same SSE streaming response, same review UI (editable tables, flags, dup detection), same confirm flow as import
- Confirm goes to the existing `/tanks/{id}/import/confirm` endpoint — no new insertion logic needed

**Backend refactor**: The `generate()` closure inside `import_preview` was extracted into a module-level async generator `_extraction_sse_stream(content, api_key)`. Both `import_preview` and `quick_log` call it; zero logic duplication.

New routes (in `import_data.py`):
- `GET /tanks/{id}/quick-log-page` → renders `tanks/quick_log.html`
- `POST /tanks/{id}/quick-log` → JSON body `{"text": "..."}` → SSE extraction stream

Template: `fathom/templates/tanks/quick_log.html`

## Current state (as of 2026-07-02)

- All moment-in-time timestamps (`events.timestamp`, `test_results.timestamp`, `observations.created_at`) are now standardized on UTC storage with browser-local display/input via new `app.js` helpers (see below); 230 tests passing
- Add Test Result form prefills date/time and water parameters from the tank's most recent test (see below); a background AI call now recommends a next action after each manual test submit and appends it to the test's notes — prompt tuned to be terse and skip restating tank inventory (see below)
- Timeline entries are now individually deletable, routed by kind to the existing per-entity delete endpoint (see below)
- `tanks.notes` (free-text field, e.g. "Targets: GH 7-8, KH 2-10...") is now included in every AI prompt (recommendation, analysis, summary, chat) so tank-specific accepted baselines override generic species norms (see below)
- Observation ↔ entity linkage now supports multiple entities per note via an `observation_links` junction table (see below), incl. on-page "linked to" filter + editable links
- Reference Info feature added (see above)
- Quick Log feature added
- Recurring schedule feature added; full app built and committed
- AI features active (ANTHROPIC_API_KEY configured in .env)
- 5G Fish Tank data imported from Apple Notes markdown export
- **Not yet deployed to Mac mini** — commits on local main, remote: `git@github.com:the-mace/aquarium-tracker.git`
- Next step: push to remote and restart launchd service on Mac mini (192.168.50.205)
- **Prompt caching**: not implemented — all AI call sites build fully dynamic prompts from live DB state; call volume too low; revisit if multi-user

## Testing

230 pytest integration tests in `fathom/tests/`. Run with:

```bash
.venv/bin/python -m pytest fathom/tests/ -q
```

Always run before committing. Coverage: tanks CRUD + cascade, test_results, events, inhabitants (null count / population events / population-event delete), issues status workflow, equipment + purchases + observations, import confirm (all 9 sections), `_strip_html` unit tests, DB helpers, AI prompt formatters, recurring_schedule CRUD + mark-done + dashboard widgets + event schedule_id link, quick-log endpoints, reference_info CRUD + placeholder insert + list join, timeline (all entry kinds incl. water tests/observations, kind filtering, out-of-range param coloring, delete-button rendering), test-form prefill, post-submit AI recommendation.

AI calls are mocked in all tests: `run_ai_analysis` → no-op; `run_test_recommendation` → no-op; `fetch_reference_info_bg` → no-op. No API credits consumed by tests. `test_ai_recommendation.py` imports the *real* `run_test_recommendation` at module load time (before the `client` fixture's monkeypatch applies) and drives it directly with a fake `anthropic.Anthropic`, so that one file does exercise the real code path — see its module docstring.

### Changes in 2026-07-02 session (sixth)

- **Quick Log / Import recurring_schedule bug**: `SECTION_CONFIG` in both `tanks/quick_log.html` and `tanks/import.html` (the JS objects driving the review-table checkboxes) never got a `recurring_schedule` entry when that feature was added — the backend extracted schedule rows fine, but the frontend had no table to render them, so "Save Selected" always saw zero rows for a schedule-only quick log. Added the missing section config to both templates. Rob's real feeding/maintenance schedule (8 rows, later 12 after a prompt fix below) was saved live via the fixed flow.
- **mark-done redirect**: `POST /tanks/{id}/schedule/{sch_id}/mark-done` always redirected to the dashboard, even when clicked from the Schedule page itself. Added a `return_to` hidden form field (`schedule.html` sends `"schedule"`, `detail.html`'s dashboard widget sends `"dashboard"`); `mark_done` in `schedules.py` branches the `RedirectResponse` target on it.
- **Weekly day-of-week maintenance never got a due date**: `mark_done` only computes `next_due` from `interval_days`, but the import prompt (rule 10) only set `interval_days` for explicit day-count phrasing ("clean filter monthly" → 30) — a task tied to a `day_of_week` (e.g. "Thursday: water change") never triggered that rule, so `next_due` stayed null forever after the first mark-done. Fixed rule 10 to always set `interval_type='weekly'`/`interval_days=7` for `logged` tasks with a `day_of_week`.
- **Timestamps standardized on UTC, displayed/entered in browser-local time**: found while debugging why mark-done completions weren't sorting correctly on the Timeline — the app mixed UTC (SQL `DEFAULT (datetime('now'))`, already UTC everywhere) with server-local Python-built timestamps (`schedules.py` mark_done, `ai_analysis.py`'s 28-day cutoff) and *raw, unconverted* browser-local strings from the three `datetime-local` inputs (Add Test Result page, dashboard's Log Water Test / Log Event modals). Now: `mark_done` stores `events.timestamp` via `datetime.now(timezone.utc)`; new `app.js` helpers (`localDatetimeToUTCString`, `prepareLocalTimestamps`) convert each `datetime-local` input to UTC via a paired hidden field before submit (`onsubmit="return prepareLocalTimestamps(this)"`); `formatLocalTimestamp`/`hydrateLocalTimestamps` rewrite every `<span class="ts-local" data-utc="...">` (5 sites: observations list, dashboard latest-test/observations panels, population-event history, tests list) to local time on `DOMContentLoaded`. AI-extracted dates with unknown time-of-day now pad to `12:00:00` (noon) instead of midnight, so the noon anchor never rolls to the wrong local calendar day after UTC→local conversion (`import_data.py` prompt + the two population-event insert sites). **Scoped out deliberately**: `recurring_schedule.last_done`/`next_due`, `tanks.py`'s `today_dow`/`today_date`, and Timeline's date-group headers stay on server-local time — those are calendar-day concepts (schedule matching, overdue coloring), not moments in time; making them browser-tz-aware would need a new cookie+`zoneinfo` mechanism, judged out of scope for "standardize timestamps." No DB migration — current dev DB is disposable test data per Rob.
- Verified live with Playwright forcing a non-UTC/non-server browser timezone (`Asia/Tokyo`, UTC+9): a stored `17:45:27` UTC row displayed as `2026-07-03 02:45`; a `09:00` local `datetime-local` input landed in the DB as the correctly-shifted UTC value. No JS console errors across the dashboard, tests, observations, inhabitants, schedule, and timeline pages.
- Two mark-done events from earlier in the session (before the UTC fix existed) had been hand-patched via raw SQL to local-time strings for a prior debugging step, and were never migrated — corrected to true UTC (+4h, EDT) so they sort correctly on the Timeline. One older test result also had a pre-fix malformed `"...T11:38"` timestamp (raw, unconverted `datetime-local` value, predating this session's fix) that sorted out of order due to `T` > space in raw string comparison — corrected to proper `"... 15:38:00"` UTC format.

### Changes in 2026-07-02 session (fifth)

- **AI test recommendation prompt tuned down**: `build_recommendation_prompt` in `ai_analysis.py` was too close to the pre-existing `run_ai_analysis` "AI Analysis" observation — verbose, and read like a tank summary (inhabitant counts etc.) which Rob doesn't want repeated back to him mid-maintenance. Reworked to: feed inhabitants/issues/recent test history (last 6 `test_results`, for real trend comparison — previously the prompt had *no* prior test data at all, so "stable trend" claims were unsupported) in as **reasoning-only context**, with an explicit instruction not to restate it; response now covers only (1) open-issues status, (2) notable parameter values/trends vs. recent tests, (3) the action to take — target 2-4 sentences, e.g. "No open issues. Nitrate dropped from 10 to 5 ppm... Proceed with the standard water change." Verified live against tank 5.
- **Timeline delete**: every `.tl-item` now has a hover-revealed ✕ button (`.tl-delete` CSS, opacity 0→1 on `.tl-item:hover`). `deleteTimelineItem(kind, id)` in `timeline.html`'s `{% block scripts %}` maps each of the 9 timeline `kind`s to its *existing* per-entity delete endpoint (event→DELETE `/tanks/{id}/events/{id}`, test→DELETE `/tanks/{id}/tests/{id}`, observation / issue / equipment / plant / hardscape → their `POST .../delete` routes) — no new unified delete endpoint, just routing. `issue_open`/`issue_resolve` both delete the underlying issue (same for `equip_install`/`equip_remove`); confirm dialog says so. Added the one missing delete route, `POST /tanks/{id}/inhabitants/population-events/{pe_id}/delete` (population events previously had no delete endpoint anywhere in the app).
- Prompted by Rob noticing my own live-testing (multiple manual `curl` test-result submits while verifying the two features above) had left orphaned "AI Analysis" observations and a stray test row in his real tank-5 data — cleaned up via the same endpoints now exposed in the UI. Lesson: prefer creating verification data in a scratch tank, or cleaning up via API immediately after, when live-testing against the user's real DB — background AI tasks (`run_ai_analysis`) fire on *every* test/event save regardless of who's testing, and deleting the triggering row does not cascade-delete the resulting observation.
- Two real duplicate test-result rows also turned up in tank 5 (#126/#127, ~1 min apart, identical values — an accidental resubmit) with their own auto-generated near-identical "AI Analysis" observations. Confirmed the Timeline delete flow end-to-end for both the `observation` (POST) and `test` (DELETE) kinds by driving a real headless browser against the running dev server with Playwright, in **both Chromium and WebKit engines** (`sync_playwright`, not installed in the project venv — used system `python3`, ran `python3 -m playwright install webkit --with-deps` once) — all four combinations correctly deleted the row from the DB. Removed the later duplicate (#127) and its observation.
- **Root cause of "KH=10 keeps getting flagged"**: not a code constant anywhere — found the tank's own `tanks.notes` free-text field (populated at import time) literally said `"Targets: GH 7-8, KH 2-4, ..."`, a stale aspirational target Rob's home water can't reach without RO. Updated tank 5's notes to `KH 2-10` with an explanatory aside via the real `POST /tanks/{id}/edit` endpoint (not raw SQL) so the change goes through the app's normal validation path. Separately discovered `tanks.notes` was **never read by any AI prompt** — `build_recommendation_prompt`, `build_analysis_prompt`, `build_summary_prompt` (`ai_analysis.py`), and `chat.py`'s system prompt all built a `Tank: {name} (...)` header from `water_type`/`volume_gallons` only. Added `_fmt_tank_notes(tank)` (returns `""` if empty/whitespace-only, else a labeled line telling Claude to defer to it over generic species norms) and appended it to that header line in all four prompt builders — `chat.py` imports it from `ai_analysis.py`. Verified live: submitting a KH=10 test after the notes fix produced a recommendation that treated GH dropping to 8 as the only parameter worth mentioning, no KH flag.

### Changes in 2026-07-02 session (fourth)

- **Add Test Result form prefill**: `GET /tanks/{id}/tests/new` now fetches the tank's most recent `test_results` row and prefills every parameter field's `value=`; a small `<script>` sets the `datetime-local` timestamp field to the browser's current local time on load (server can't know the client's timezone). Renamed the field label from "Timestamp (leave blank for now)" to "Date & Time" — the blank-defaults-to-now behavior in `test_results.py`'s POST handler is unchanged, prefilling is purely a UX convenience.
- **Post-submit AI recommendation**: new `run_test_recommendation(tank_id, result_id)` in `ai_analysis.py`, queued as a second `BackgroundTask` from `test_results.py`'s `POST /tanks/{id}/tests` handler only (not triggered by import or quick-log inserts). Gathers the just-saved test result, all active `recurring_schedule` rows, and the tank's timeline entries from the last 28 days (reuses `routers.timeline._QUERY`, filtered in Python by date since it's a plain SQL string built for a different endpoint). Prompt asks Claude to recommend an action — usually "follow the normal water-change schedule" but it can deviate if history suggests otherwise (e.g. a change was already done recently). The response text is appended to the test result's own `notes` column as `\n\nAI Recommendation: {text}`, preserving whatever the human typed. Mocked to a no-op in `conftest.py` like `run_ai_analysis`.
- Verified live against tank 5 with a real API call (not just mocked tests) — Claude correctly pulled the last water-change date and dosing amount from the schedule/timeline context into its recommendation.

### Changes in 2026-07-02 session (third)

- **Date filter UX fix on Timeline/Observations**: the inline `date_from`/`date_to` `<input type="date">` fields were replaced with a "📅 Dates" button that opens a modal (`tl-date-modal` / `obs-date-modal`, standard `.modal`/`.modal-box-sm` markup) — the date inputs live inside it and submit the same outer GET filter form on "Apply". Root cause: Safari renders an *empty* `<input type="date">` with today's date pre-filled in the digit sub-fields, and WebKit ignores `color` overrides on those fields entirely (confirmed by installing a real headless WebKit via Playwright and inspecting rendered pixels — `::-webkit-datetime-edit-text` recolors the `/` separators but not the digits), so there was no CSS-only fix. The "Dates" button highlights (`btn-accent`) when a filter is active instead, and — since the button's own text never changes — the Filter/Clear buttons after it never shift position.
- Tried and reverted along the way: forcing the empty-state color via CSS class (no effect, per above); swapping `type="date"`/`type="text"` on focus/blur to fake a placeholder (caused a double-click-to-open-picker regression and field-width jumping); an always-visible "filtering X → Y" status line next to the fields (removed per Rob's feedback — the highlighted button alone reads clearly enough).

### Changes in 2026-07-02 session (second)

- **Timeline gets water tests and observations**: `routers/timeline.py`'s `_QUERY` UNION gained an `observation` branch (badge text/color keyed off `source`: manual/auto/import); water tests are fetched via a separate query and merged + re-sorted in Python (`(tank_id,) * 9` for the UNION placeholders) rather than folded into the SQL string, because each test parameter needs individual out-of-range styling.
- **Out-of-range test param coloring**: `_PARAM_DEFS`/`_test_params()` in `timeline.py` classify each of pH/GH/KH/NH3/NO2/NO3/TDS/Temp as `danger`/`warn`/normal using the *same* thresholds already used on the dashboard latest-test panel and `tests/list.html` (NH3 >0.25 danger, >0 warn; NO2 >0.1 danger; NO3 >40 warn — pH/GH/KH/TDS/Temp have no established thresholds anywhere in the app, so they're never colored). Rendered as a `tl-param`/`tl-param-danger`/`tl-param-warn` span per parameter in `tanks/timeline.html`.
- New CSS: `.tl-dot-test`/`.tl-badge-test` (violet), `.tl-dot-obs-manual/-auto/-import` + matching badges (grey/green/blue, matching the Observations page's own badge colors), `.tl-param-danger`/`.tl-param-warn`.
- Timeline filter dropdown, legend, and empty-state copy updated for the two new kinds (`kind=tests`, `kind=observations`).
- The dedicated `/tanks/{id}/tests` list page and sidebar "Add Test" link are unchanged — this just gives water tests a second, chronological home in the Timeline.

### Changes in 2026-07-02 session (first)

- **Observation ↔ entity linkage**: `observations` initially got four new nullable columns (`related_inhabitant_id`/`related_plant_id`/`related_hardscape_id`/`related_equipment_id`), then **refactored the same session** into an `observation_links` junction table (`observation_id, entity_type, entity_id`, `UNIQUE(observation_id, entity_type, entity_id)`, cascade-deletes with the observation) so one note can link to *multiple* entities — e.g. "pruned frogbit, ramshorn snails died off, UV light back on" links to a plant, an inhabitant, and an equipment item at once. `database.py` migrates any legacy single-column data into the junction table (reads old FK values before the `observations` table rebuild, since `observation_links`' `ON DELETE CASCADE` would wipe rows inserted before the rebuild's `DROP TABLE`). `routers/observations.py`: `_set_observation_links()`/`_links_by_observation()` replace the old `COLUMN_BY_TYPE` single-column lookups; `add_observation`/`set_observation_link` now take `link_ref: List[str]` (repeated form field) instead of a single value.
- **Observations page** (`routers/observations.py`, `templates/observations/list.html`): `GET /tanks/{id}/observations` accepts `?link_type=inhabitant|plant|hardscape|equipment&link_id=N` as a legacy fallback, plus a `link_ref=type:id` filter (special values `any`/`none`); shows a "Showing notes for… · Clear filter" banner when filtered. `POST .../observations` accepts one or more `link_ref` fields from a new "Relates to" multi-select in the Add Note modal.
- **"💬 Observations" links** (renamed from "Notes" — collided with the entity's own free-text notes field): added to the Actions cell on `inhabitants/list.html`, `plants/list.html` (both the plants and hardscape tables), and `equipment/list.html` — each links to the Observations page pre-filtered to that row. Styled `.btn-accent` (filled blue pill, new CSS class) rather than a ghost/underlined link.
- **Import/Quick Log auto-linking**: `IMPORT_PROMPT` observations now carry a `subjects` list (`[{subject_type, subject_name}, ...]`) instead of a single subject pair, so one extracted note can tag several distinct items. `import_confirm` builds canonical-name → id lookup maps (reusing `_canonical()` from `reference_info.py`), preloaded from the tank's existing entities and kept current as each section's insert/update loop runs, then inserts one `observation_links` row per resolved subject (deduped). Falls back to reading the older singular `subject_type`/`subject_name` shape for any cached preview from before this change. Unmatched/empty subjects leave the note with zero links — no error.
- **Observations manual filter bar**: mirrors the Timeline page's filter UX — `search` (text, LIKE on `o.text`), `source` (manual/auto/import), `date_from`/`date_to`. Combines with the entity-link filter: `clear_link_url` drops just the entity link and keeps search/source/date; `clear_search_url` does the reverse. Both computed server-side in `list_observations` via `urlencode`.
- **Editable links**: every observation card has a "🔗 Add/Change links" button → small modal → `POST /tanks/{id}/observations/{obs_id}/link` (JSON fetch, empty `link_ref` list clears all links) — previously links could only be set at creation time. The `<optgroup>` markup for entity pickers was pulled into a shared Jinja macro (`entity_optgroups`) in `observations/list.html`, used by the add-note form, the filter select, and the edit-link modal.
- **Also bundled** (prior uncommitted work): `plants_hardscape.py` gained `POST /plants/{id}/update` and `POST /hardscape/{id}/update` (the edit modals in `plants/list.html` already called these); `count`/`cost` form fields changed from `int`/`float` to `str` + manual parsing so empty-string submissions from the edit modals don't 422; `.form-group input[type="checkbox"]` CSS fix so checkboxes don't stretch to full field width.

### Changes in 2026-06-30 session (fourth)
- **Reference Info**: new `reference_info` table (UNIQUE entity_type+entity_name). Background task uses `claude-sonnet-4-6` + `web_search_20260209` to fetch description, care notes, Wikimedia Commons image URL. Auto-queued on add and on list-page load for items with no row yet.
- **Inhabitants & Plants lists**: LEFT JOIN `reference_info`; 46px thumbnail column; click → ref-info modal with image + description + care notes + Refresh button.
- **anthropic SDK**: upgraded 0.40.0 → 0.115.0 (required for web_search_20260209 response parsing).
- **Tests**: 9 new reference_info tests; `fetch_reference_info_bg` mocked to no-op in conftest — no API credits consumed by test suite.

### Changes in 2026-06-30 session (third)
- **Observations source**: DB migration adds `'import'` to CHECK constraint. Import confirm saves observations with `source='import'`; flag notes get `source='auto'`. Templates show "Import Note" badge (blue) vs "AI Analysis" (green) vs "Manual Note" (grey).
- **Inhabitants import fix**: `_find_duplicates` now checks count diff — same count auto-unchecks ("no change needed"), different count stays checked ("will update from X to Y"). Within-preview: earlier entries for same species auto-unchecked; `import_confirm` sorts inhabitants by date so latest wins on UPSERT. After count updates, saves an "Import updated inhabitants" observation.
- **Plants species in prompt**: Rule 12 added — always populate scientific species name for plants (Java moss → Taxiphyllum barbieri, etc.).
- **Issues list**: Client-side filter bar — status pills (All/Open/Monitoring/Resolved), text search, oldest/newest sort toggle.
- **Timeline**: Server-side filtering — date range, kind (events, issues, equipment, population, plants, hardscape), text search. Plants and hardscape added to timeline query with their own dot/badge colors.
- **Plants & Hardscape page**: New `/tanks/{id}/plants` page with CRUD for both. Router: `plants_hardscape.py`. Template: `plants/list.html`. Sidebar link added to base.html between Inhabitants and Equipment.
- **CSS**: `.filter-bar`, `.filter-pills`, `.pill`, `.pill-active` styles; `.obs-import` badge; `.tl-dot-plant`, `.tl-dot-hardscape`, `.tl-badge-plant`, `.tl-badge-hardscape` timeline styles.

### Changes in 2026-06-30 session (second)
- Quick Log: dashboard modal → sessionStorage handoff → `/tanks/{id}/quick-log-page` with auto-start parse
- Refactor: `_extraction_sse_stream` extracted from `import_preview`; both import and quick-log reuse it
- New routes: `GET/POST /tanks/{id}/quick-log`, `GET /tanks/{id}/quick-log-page`
- New template: `tanks/quick_log.html`
- CSS: `.action-btn-primary` for highlighted Quick Log button
- Tests: 6 new quick-log endpoint tests

### Changes in 2026-06-30 session (first)
- Recurring schedule feature added (see above)

### Changes in 2026-06-29 session
- Schema: `plants` and `hardscape` tables (both cascade-delete with tank)
- UI: events show date only (no time, no amount); tank specs panel on dashboard; plants/hardscape cards; modal close CSS fix (flex !important vs display: none); inhabitants "many" toggle (null count)
- AI: chat context now injects all water params explicitly, plants, hardscape, open issues, 5 observations, tank hardware/substrate; debug log for context length; ai_analysis includes plants/hardscape in summary prompt
- Import: rich extraction prompt covering issues, plants, hardscape, observations, tank_specs, narrative equipment; interactive flagged review UI with editable tables, per-row checkboxes, amber flag highlighting; sidebar nav link added
- Tests: 118 pytest integration tests added (`fathom/tests/`); `pytest.ini` with `pythonpath = fathom`; AI and DB isolated per test via monkeypatch
- Repo: MIT LICENSE added; `.gitignore` updated with pytest/coverage entries
