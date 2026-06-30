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
│   │   └── ai_analysis.py   # BackgroundTask: auto-analysis on test/event save
│   ├── templates/
│   │   ├── base.html        # Sidebar nav, Chart.js CDN
│   │   ├── tanks/           # list, detail, form, import
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

## Current state (as of 2026-06-29)

- Full app built, all fixes applied and committed (4 commits on main)
- AI features active (ANTHROPIC_API_KEY configured in .env)
- 5G Fish Tank data imported from Apple Notes markdown export
- **Not yet deployed to Mac mini** — commit history on local main, remote: `git@github.com:the-mace/aquarium-tracker.git`
- Need to push to remote and restart launchd service on Mac mini (192.168.50.205) to deploy

### Changes in 2026-06-29 session
- Schema: `plants` and `hardscape` tables
- UI: events show date only; tank specs panel on dashboard; plants/hardscape cards; modal close CSS fix; inhabitants "many" toggle
- AI: chat context now includes all water params, plants, hardscape, open issues, 5 observations; ai_analysis includes plants/hardscape in summary
- Import: rich extraction prompt (issues, plants, hardscape, observations, tank_specs); interactive flagged review UI
