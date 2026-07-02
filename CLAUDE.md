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

- Observation ↔ entity linkage added, incl. on-page "linked to" filter + editable links (see below); 199 tests passing
- Reference Info feature added (see above)
- Quick Log feature added
- Recurring schedule feature added; full app built and committed
- AI features active (ANTHROPIC_API_KEY configured in .env)
- 5G Fish Tank data imported from Apple Notes markdown export
- **Not yet deployed to Mac mini** — commits on local main, remote: `git@github.com:the-mace/aquarium-tracker.git`
- Next step: push to remote and restart launchd service on Mac mini (192.168.50.205)
- **Prompt caching**: not implemented — all AI call sites build fully dynamic prompts from live DB state; call volume too low; revisit if multi-user

## Testing

199 pytest integration tests in `fathom/tests/`. Run with:

```bash
.venv/bin/python -m pytest fathom/tests/ -q
```

Always run before committing. Coverage: tanks CRUD + cascade, test_results, events, inhabitants (null count / population events), issues status workflow, equipment + purchases + observations, import confirm (all 9 sections), `_strip_html` unit tests, DB helpers, AI prompt formatters, recurring_schedule CRUD + mark-done + dashboard widgets + event schedule_id link, quick-log endpoints, reference_info CRUD + placeholder insert + list join.

AI calls are mocked in all tests: `run_ai_analysis` → no-op; `fetch_reference_info_bg` → no-op. No API credits consumed by tests.

### Changes in 2026-07-02 session

- **Observation ↔ entity linkage**: `observations` gets four new nullable columns — `related_inhabitant_id`, `related_plant_id`, `related_hardscape_id`, `related_equipment_id` (no declared FK, same style as the existing `related_event_id`/`related_test_id`). Migration in `database.py` (`PRAGMA table_info` check + `ALTER TABLE ADD COLUMN`); the legacy "add 'import' to source CHECK" rebuild migration now uses an explicit column list on its `INSERT INTO observations_new` so it doesn't choke on the wider live schema.
- **Observations page** (`routers/observations.py`, `templates/observations/list.html`): `GET /tanks/{id}/observations` accepts `?link_type=inhabitant|plant|hardscape|equipment&link_id=N` — LEFT JOINs all four related tables so every row can show a "linked to X: Y" badge even when unfiltered; shows a "Showing notes for… · Clear filter" banner when filtered. `POST .../observations` accepts `link_ref` (form field, format `"type:id"`, e.g. `"inhabitant:5"`) from a new "Relates to" `<select>` in the Add Note modal — pre-selected server-side when the page was reached via an active filter.
- **"💬 Observations" links** (renamed from "Notes" — collided with the entity's own free-text notes field): added to the Actions cell on `inhabitants/list.html`, `plants/list.html` (both the plants and hardscape tables), and `equipment/list.html` — each links to the Observations page pre-filtered to that row. Styled `.btn-accent` (filled blue pill, new CSS class) rather than a ghost/underlined link.
- **Import/Quick Log auto-linking**: `IMPORT_PROMPT` observations now carry optional `subject_type`/`subject_name`; rule 6 tells Claude to tag the subject when an observation is clearly about one specific inhabitant/plant/hardscape item/equipment piece. `import_confirm` builds canonical-name → id lookup maps (reusing `_canonical()` from `reference_info.py`), preloaded from the tank's existing entities and kept current as each section's insert/update loop runs, so a subject can resolve to either a pre-existing item or one created earlier in the same import. Unmatched/null subjects leave all four link columns null — no error.
- **Observations manual filter bar**: mirrors the Timeline page's filter UX — `search` (text, LIKE on `o.text`), `source` (manual/auto/import), `date_from`/`date_to`. Combines with the entity-link filter: `clear_link_url` drops just the entity link and keeps search/source/date; `clear_search_url` does the reverse. Both computed server-side in `list_observations` via `urlencode`.
- **"Linked to" filter + editable links (follow-up)**: the filter bar also has a "Linked to" `<select>` driven by a single `link_ref=type:id` query param (`_parse_link_ref()` helper), with special values `any` (linked to something) and `none` (unlinked) in addition to specific entities; legacy `link_type`/`link_id` params from the entity-page buttons still work as a fallback. Every observation card now has a "🔗 Add/Change link" button → small modal → `POST /tanks/{id}/observations/{obs_id}/link` (JSON fetch, `link_ref` empty string clears the link) — previously the link could only be set at creation time. The `<optgroup>` markup for entity pickers was pulled into a shared Jinja macro (`entity_optgroups`) in `observations/list.html`, used by the add-note form, the filter select, and the edit-link modal.
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
