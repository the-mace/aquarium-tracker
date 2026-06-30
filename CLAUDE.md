# Fathom — CLAUDE.md

Project context for AI-assisted development sessions.

## What this is

Fathom is a personal aquarium tracking web app with AI-powered analysis. Single user, self-hosted. No auth, no multi-tenancy.

## Stack

- **Backend**: Python 3 + FastAPI, uvicorn
- **Database**: SQLite at `fathom/data/fathom.db` (gitignored)
- **Templates**: Jinja2, plain HTML/CSS/JS
- **Charts**: Chart.js 4.4.0 via CDN
- **AI**: Anthropic Python SDK, model `claude-sonnet-4-6`
- **Env**: python-dotenv, `.env` at repo root (gitignored)

## How to run

```bash
cd fathom
source ../.venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The `.venv` is at `aquarium-tracker/.venv` (one level above `fathom/`).

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

## Database schema (12 tables)

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

## AI features

### Background analysis (`ai_analysis.py`)
Triggered as FastAPI `BackgroundTask` after every test_result or event save. Fetches last 10 tests, open issues, 30-day events, and inhabitants. Calls Claude, stores result as an `observations` row (source=auto) and upserts `tank_state_summary`.

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

Mac mini at `192.168.50.205`, SSH via `ssh -A rob@192.168.50.205`. Repo path same as dev. Run as launchd service — see README.md for plist.

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

Seed data seeded on startup for "Fish Tank 5G" (5G shrimp tank). The 40G tank seed runs automatically once a tank with volume 38-45g exists in the DB. The 40G seed includes Clean pre-filter (30d), Clean all stages (75d), and Squeeze sponge (manual reminder, no interval).

Import prompt includes rule 10 for parsing `recurring_schedule` from narrative text.

Management page: `/tanks/{id}/schedule`

## Current state (as of 2026-06-30)

- Recurring schedule feature added (see above); 171 tests passing
- Full app built, all fixes applied, committed, and smoke-tested end-to-end
- AI features active (ANTHROPIC_API_KEY configured in .env)
- 5G Fish Tank data imported from Apple Notes markdown export
- Import tested from scratch against a narrative journal: all 10 data types extracted correctly, tank specs auto-filled from Claude's product knowledge, "many" count, issue resolution patterns, flags — everything verified in DB
- **Not yet deployed to Mac mini** — commits on local main, remote: `git@github.com:the-mace/aquarium-tracker.git`
- Next step: push to remote and restart launchd service on Mac mini (192.168.50.205)
- **Prompt caching**: not implemented — all three AI call sites (ai_analysis, chat, import) build fully dynamic prompts from live DB state; call volume too low for caching to pay off; revisit if multi-user

## Testing

171 pytest integration tests in `fathom/tests/`. Run with:

```bash
.venv/bin/python -m pytest fathom/tests/ -q
```

Always run before committing. Coverage: tanks CRUD + cascade, test_results, events, inhabitants (null count / population events), issues status workflow, equipment + purchases + observations, import confirm (all 9 sections), `_strip_html` unit tests, DB helpers, AI prompt formatters, recurring_schedule CRUD + mark-done + dashboard widgets + event schedule_id link.

### Changes in 2026-06-29 session
- Schema: `plants` and `hardscape` tables (both cascade-delete with tank)
- UI: events show date only (no time, no amount); tank specs panel on dashboard; plants/hardscape cards; modal close CSS fix (flex !important vs display: none); inhabitants "many" toggle (null count)
- AI: chat context now injects all water params explicitly, plants, hardscape, open issues, 5 observations, tank hardware/substrate; debug log for context length; ai_analysis includes plants/hardscape in summary prompt
- Import: rich extraction prompt covering issues, plants, hardscape, observations, tank_specs, narrative equipment; interactive flagged review UI with editable tables, per-row checkboxes, amber flag highlighting; sidebar nav link added
- Tests: 118 pytest integration tests added (`fathom/tests/`); `pytest.ini` with `pythonpath = fathom`; AI and DB isolated per test via monkeypatch
- Repo: MIT LICENSE added; `.gitignore` updated with pytest/coverage entries
