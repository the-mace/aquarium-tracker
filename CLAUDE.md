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

**`query_db` tool**: the system prompt context is a snapshot only (latest test, current inhabitants, 5 recent observations, etc.) — for anything needing history (full test trends, `population_events`, purchase totals) Claude is given a `query_db` tool that runs a single read-only SQL `SELECT` against the DB (schema auto-generated via `database.get_schema_text()`, so it never drifts from the actual tables). Safety is two-layered: `_run_query_db` regex-rejects anything not starting with `SELECT`, and the query itself executes over `database.get_db_readonly()` — a SQLite URI `mode=ro` connection, so even a bypassed regex can't write. Up to `MAX_TOOL_ROUNDS=4` tool round-trips per message; only the final text reply is persisted into `_conversations` (intermediate tool_use/tool_result exchanges are not kept, to avoid bloating future-turn token usage).

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

Mac mini at `192.168.50.205`, SSH via `ssh -A rob@192.168.50.205`. Repo at `/Users/rob/Documents/Code/aquarium-tracker` (same path as local dev, not `/Users/rob/aquarium-tracker` as earlier notes said). Run as a launchd system service:

- Plist: `/Library/LaunchDaemons/com.fathom.plist` (has `UserName: rob` so it runs as `rob`, not root — a LaunchDaemon runs as root by default otherwise, which would leave the DB/venv files root-owned)
- Uvicorn binary: `/Users/rob/Documents/Code/aquarium-tracker/.venv/bin/uvicorn`
- Working dir: `/Users/rob/Documents/Code/aquarium-tracker/fathom`
- Env: `DOTENV_PATH=/Users/rob/Documents/Code/aquarium-tracker/.env`
- Logs: `/tmp/fathom.log` / `/tmp/fathom.err`
- venv built with `~/.pyenv/versions/3.14.0/bin/python3` (matches local dev's pyenv version; the mini's system `/usr/bin/python3` is 3.9.6 and unsuitable)

`sudo launchctl load`/`unload` need an interactive password — non-interactive `ssh host "sudo ..."` will fail with "a password is required". Use `ssh -A -t` for a single sudo command, or `ssh -A rob@192.168.50.205` to get a shell and run sudo commands one at a time there.

Reload after deploy: `ssh -A rob@192.168.50.205 "sudo launchctl unload /Library/LaunchDaemons/com.fathom.plist && sudo launchctl load /Library/LaunchDaemons/com.fathom.plist"`

S3 backup cron (3am daily, **live as of 2026-07-03**): `0 3 * * * PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin bash /Users/rob/Documents/Code/aquarium-tracker/fathom/scripts/backup_db.sh >> /tmp/fathom-backup.log 2>&1` — note the explicit `PATH=` prefix; cron's default PATH doesn't include Homebrew's `/opt/homebrew/bin`, where `aws` lives.

Bucket: `lpf-fathom-backups` (`us-east-1`), 30-day S3 Lifecycle expiration on the `backups/` prefix. Uploads go through a dedicated IAM user (`fathom`, account `REDACTED-AWS-ACCOUNT-ID`) with a policy scoped to just this bucket: `CreateBucket`/`ListBucket` at the bucket level, `PutObject`/`GetObject` at the object level — deliberately no `DeleteObject` (the Lifecycle rule handles expiration internally at the S3-service level, so the uploading IAM user never needs delete permission). `AWS_PROFILE=default` in `.env` on both machines points `aws` at this IAM user's key (configured via `aws configure`, not a named profile — simpler since nothing else was already using `default` locally).

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

- Tank manufacturer/model (e.g. "Seapora 40 gallon long") now triggers a real web-search-backed AI fetch to backfill missing volume/dimensions, instead of relying on unreliable training-knowledge guessing — see eleventh-session notes below. 263 tests passing
- Chat ("Ask AI") now has a read-only `query_db` tool so it can answer history questions (e.g. "when was X added", "GH history") instead of claiming the data doesn't exist — see tenth-session notes below.
- Fixed a batch of real data-quality issues in the "Fish Tank" (40g, tank_id=7) import: two food/supplement items (Cuttlebone, Calcium Bites) were miscategorized as Hardscape; Otocinclus, Kuhli Loach, and non-zebra Nerite Snail were each split into two duplicate inhabitant rows because a later "recount" passage used a slightly different species string for the same population; Red Rili/Red Cherry Shrimp (merged into one inhabitant record) never got zeroed out despite the whole batch dying off. See ninth-session notes below for the fix (both this tank's data and the import root cause).
- Population chart now shows currently-"many"/unknown-count species as a gap + "(now: many)" legend label instead of freezing at a stale precise number; equipment and purchases are now editable in the UI; import prompt strips parenthetical detail out of `source` (inhabitants/plants/hardscape) and equipment `model` into `notes` (see below); 233 tests passing
- Equipment's "Specs" field no longer implies JSON is required — edit modal flattens stored JSON specs to plain text; Equipment/Purchases row action buttons now have spacing between them (see below)
- All moment-in-time timestamps (`events.timestamp`, `test_results.timestamp`, `observations.created_at`) are now standardized on UTC storage with browser-local display/input via new `app.js` helpers (see below)
- Add Test Result form prefills date/time and water parameters from the tank's most recent test (see below); a background AI call now recommends a next action after each manual test submit and appends it to the test's notes — prompt tuned to be terse and skip restating tank inventory (see below)
- Timeline entries are now individually deletable, routed by kind to the existing per-entity delete endpoint (see below)
- `tanks.notes` (free-text field, e.g. "Targets: GH 7-8, KH 2-10...") is now included in every AI prompt (recommendation, analysis, summary, chat) so tank-specific accepted baselines override generic species norms (see below)
- Observation ↔ entity linkage now supports multiple entities per note via an `observation_links` junction table (see below), incl. on-page "linked to" filter + editable links
- Reference Info feature added (see above)
- Quick Log feature added
- Recurring schedule feature added; full app built and committed
- AI features active (ANTHROPIC_API_KEY configured in .env)
- 5G Fish Tank data imported from Apple Notes markdown export
- **Deployed to Mac mini for the first time on 2026-07-03** — see twelfth-session notes below. Running live at `192.168.50.205:8000` as a fresh install (empty DB, no tanks yet). S3 backup cron is now live too (see "Production deployment" above) — bucket `lpf-fathom-backups`, 30-day retention, dedicated least-privilege IAM user.
- Ongoing: after future commits, deploy by pulling on the mini and reloading the service — see "Reload after deploy" above.
- **Prompt caching**: not implemented — all AI call sites build fully dynamic prompts from live DB state; call volume too low; revisit if multi-user

## Testing

257 pytest integration tests in `fathom/tests/`. Run with:

```bash
.venv/bin/python -m pytest fathom/tests/ -q
```

Always run before committing. Coverage: tanks CRUD + cascade, test_results, events, inhabitants (null count / population events / population-event delete), issues status workflow, equipment + purchases + observations, import confirm (all 9 sections), `_strip_html` unit tests, DB helpers, AI prompt formatters, recurring_schedule CRUD + mark-done + dashboard widgets + event schedule_id link, quick-log endpoints, reference_info CRUD + placeholder insert + list join, timeline (all entry kinds incl. water tests/observations, kind filtering, out-of-range param coloring, delete-button rendering), test-form prefill, post-submit AI recommendation, chat's `query_db` tool loop + SQL-safety guards (`test_chat.py`).

AI calls are mocked in all tests: `run_ai_analysis` → no-op; `run_test_recommendation` → no-op; `fetch_reference_info_bg` → no-op. No API credits consumed by tests. `test_ai_recommendation.py` imports the *real* `run_test_recommendation` at module load time (before the `client` fixture's monkeypatch applies) and drives it directly with a fake `anthropic.Anthropic`, so that one file does exercise the real code path — see its module docstring.

### Changes in 2026-07-03 session (twelfth)

- **First-ever deployment to the Mac mini.** The mini had literally never been touched for this project — no repo, no venv, no plist, no cron entry. Full first-time setup performed live over SSH (`ssh -A rob@192.168.50.205`):
  - `git clone git@github.com:the-mace/aquarium-tracker.git` — initially cloned to `/Users/rob/aquarium-tracker` per the (aspirational, never-validated) path in this file's older "Production deployment" section, then **moved to `/Users/rob/Documents/Code/aquarium-tracker`** per Rob's correction, to match the local dev machine's layout. Updated "Production deployment" above accordingly — that section's paths were wrong until this session actually exercised them.
  - venv built with `~/.pyenv/versions/3.14.0/bin/python3` (already installed on the mini, matches local dev's pyenv version) — the mini's system `/usr/bin/python3` is 3.9.6, too old and not what dev uses. `pip install -r requirements.txt` succeeded clean, no compiled-wheel issues despite Xcode CLT not being strictly needed (was present anyway).
  - `.env` copied from local dev via `scp` (contains the real `ANTHROPIC_API_KEY`), `chmod 600` on the remote copy.
  - `com.fathom.plist` installed as a system `LaunchDaemon` at `/Library/LaunchDaemons/com.fathom.plist` — added a `UserName: rob` key (not present in the README's example plist) so it runs as `rob` rather than root; a `LaunchDaemon` without `UserName` runs as root by default, which would've left the DB/venv files root-owned and broken future `git pull`/manual runs as `rob`.
  - **`sudo launchctl load` needs an interactive password** — a plain `ssh host "sudo ..."` fails with "a password is required" since there's no TTY. Also tried `ssh -A -t host "cmd1 && cmd2 && ..."` as one chained line, which Rob reported failing with a `zsh: permission denied: /Library/LaunchDaemons/com.fathom.plist` error — looked like a quoting/paste issue splitting the chained command apart in his terminal rather than an actual permission problem (the final bare path got executed standalone). Fix: have Rob `ssh -A rob@192.168.50.205` to get a real interactive shell first, then run each `sudo mv`/`chown`/`chmod`/`launchctl load` command one at a time there — worked cleanly.
  - Verified thoroughly post-deploy (not just "it started"): log tail showed clean `Application startup complete`; `curl localhost:8000/` returned the expected `307` (root redirect) and `curl -L .../` showed the real dashboard title; fresh `fathom.db` auto-created via `init_db()` on first request with all 13+ tables and 0 tank rows (confirmed by direct `sqlite3` query over SSH, to make sure a stray dev DB hadn't been copied over by accident); **killed the running uvicorn PID directly and confirmed launchd's `KeepAlive` respawned it within 3 seconds** and the service kept serving `200`s — this is the check that actually proves the LaunchDaemon (not just a manually-started process) is in control, since `ps aux` alone can't distinguish the two.
  - **S3 backup cron completed later the same session.** Rob picked `lpf-fathom-backups` as the bucket name (globally-unique-across-AWS constraint discussed first). Rather than reuse his existing `saml`-profile SSO credentials (expired, and probably a work-federated account rather than personal), Rob created a fresh dedicated IAM user `fathom` in his own AWS account and generated a long-lived access key for it — the right call for an unattended cron job, since SSO tokens expire and would silently break nightly backups. Scoped its policy to exactly `lpf-fathom-backups`: `CreateBucket`+`ListBucket` (bucket-level) and `PutObject`+`GetObject` (object-level), no `DeleteObject`. Verified the policy was actually least-privilege by testing a delete immediately after a test upload — it correctly failed with `AccessDenied`.
  - Rob raised the obvious follow-up: with no delete permission and no rotation logic in `backup_db.sh`, backups would accumulate forever. Fixed via an **S3 Lifecycle rule** (`backups/` prefix, 30-day `Expiration`) rather than adding delete permission or script-side rotation logic — S3 expires objects internally at the service level, so the uploading IAM user never needs `DeleteObject` at all. Setting the lifecycle rule itself needed two more IAM actions (`Put`/`GetLifecycleConfiguration`) added to the `fathom` policy temporarily by Rob via the console (`PutBucketLifecycleConfiguration` isn't covered by plain object/bucket CRUD actions) — first attempt hit `AccessDenied` immediately after Rob saved the policy edit, resolved on retry a few seconds later (ordinary IAM propagation delay, not a real problem).
  - `awscli` installed on the mini via `brew install awscli`. Credentials configured via `aws configure` (interactively, by Rob himself over SSH — deliberately did not scp the credentials file over, to avoid duplicating the secret as a second on-disk copy) into the `default` profile on both machines, matching `.env`'s `AWS_PROFILE=default` — a named profile isn't needed since "fathom" is just the IAM *user's* name, unrelated to the local CLI profile name. (Rob briefly ended up with both a `default` and an unused duplicate `fathom` profile locally, from following an earlier version of my instructions before I simplified to "just use default" — harmless, the duplicate is just unused.)
  - Cron entry appended (not overwritten) to Rob's existing crontab, which has many unrelated personal-project jobs: `0 3 * * * PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin bash .../backup_db.sh >> /tmp/fathom-backup.log 2>&1`. The explicit `PATH=` prefix is required — cron's default PATH doesn't include `/opt/homebrew/bin`, and `backup_db.sh` invokes `aws` by bare name. Verified with a real live run (not just `--dry-run`): produced an actual `.db.gz` in `s3://lpf-fathom-backups/backups/`, confirmed via `aws s3 ls`.
  - No code changes this session — pure infrastructure/ops. Test suite untouched.

### Changes in 2026-07-02 session (eleventh)

- **Tank dimensions weren't being filled in on import.** Rob reported that importing "Fish Tank" correctly identified it as a "Seapora 40 gallon long" (manufacturer/model extracted fine) but `dimensions_l/w/h` stayed null. Root cause: `IMPORT_PROMPT` rule 2 asked Claude to "fill in standard dimensions... from your knowledge" during the plain extraction call — no actual web search, just a training-knowledge guess, which is unreliable for less-common brands like Seapora (works fine for well-known kits like Fluval Spec V, which is presumably why this wasn't caught earlier).
- Added `maybe_fetch_tank_dimensions(background_tasks, tank_id)` / `fetch_tank_dimensions_bg(tank_id, manufacturer, model)` to `reference_info.py`, alongside the existing species/plant/hardscape reference-info fetcher — same shape (module-level `_dim_in_flight` set guards against duplicate concurrent fetches, mocked to no-op in `conftest.py`). Unlike `fetch_reference_info_bg` (which does NOT use Claude's server-side web search — it hand-rolls Wikimedia Commons + DDG lookups for images), this one uses the actual `web_search_20260209` tool in a single `messages.create` call, since precise product specs benefit from a real search rather than training-data recall. Only backfills fields that are still `NULL` (`COALESCE(col, ?)` in the UPDATE) — never overwrites a value the user or import extraction already set.
- Wired into three call sites, all self-checking via the same `maybe_fetch_tank_dimensions` (queues only if manufacturer/model is set AND at least one of volume/dimensions is still null): `import_confirm` (after the `tank_specs` UPDATE block — this is the case Rob hit), and `tanks.py`'s `create_tank`/`update_tank` (manual Add/Edit Tank forms), which previously had no dimension-backfill path at all.
- Backfilled the real tank 7 data live by calling `fetch_tank_dimensions_bg(7, 'Seapora', '40 gallon long')` directly against the running dev DB (not a fabricated/manual value) — came back 48.0" × 13.0" × 16.0", matches the real product's listed external dimensions. Verified via `curl localhost:8000/tanks/7` that the detail page now renders it.
- 6 new tests in `test_reference_info.py`: `maybe_fetch_tank_dimensions` queuing/skipping (manufacturer known + dims missing → queues; no manufacturer/model → skips; specs already complete → skips; unknown tank id → skips), plus two endpoint-level tests confirming `create_tank`/`update_tank` trigger the fetch when manufacturer/model is submitted. Also fixed a latent test-isolation gap surfaced by these: `_in_flight`/`_dim_in_flight` are module-level sets that only get cleared by the real background task's `finally` block — a test that swaps in a `MagicMock` for the bg task (rather than letting it run) leaks the tank_id/entity_name into the set for the rest of the pytest process, since every test gets a fresh per-test DB where autoincrement ids restart at 1. Existing reference-info tests dodged this by luck (always using distinct entity name strings); the new tank_id-keyed tests collided immediately. Fixed by clearing both sets in `conftest.py`'s `client` fixture on every test setup. 263 tests total, all passing.

### Changes in 2026-07-02 session (tenth)

- **Chat gave wrong "I don't have that data" answers for things the DB actually records.** Two real examples from Rob: asking when the Kuhli Loaches were added got "I don't have a record" even though `inhabitants.added_date` was set; asking for GH history got "I only have a single reading" even though every weekly test result has a GH value. Root cause was two-fold: (1) `chat.py`'s inhabitant SQL/formatting never selected or displayed `added_date` at all (also true of the recommendation/analysis/summary prompts, which share `_fmt_inhabitants` in `ai_analysis.py`) — fixed by adding `added_date` to `_fmt_inhabitants`'s output and chat's `SELECT`, and deleting chat's near-duplicate inline copy of that formatter in favor of importing the shared one; (2) the chat system prompt is fundamentally a *snapshot* (latest test only, current inhabitants only, 5 recent observations) with no way to reach further back, and no amount of adding more prefetched fields generalizes to arbitrary "history of X" questions.
- **Added a `query_db` tool to chat** so Claude can run a single read-only SQL `SELECT` against the live DB mid-conversation for anything the snapshot doesn't cover. `_query_db_tool(tank_id)` builds the tool definition with the DB schema inlined (via new `database.get_schema_text()`, introspected from `sqlite_master`/`PRAGMA table_info` at call time so it can never drift from the actual tables) and an instruction to filter `WHERE tank_id = {tank_id}`. `chat()`'s single `messages.create` call became a loop (`MAX_TOOL_ROUNDS=4`) that re-calls Claude with `tool_result` blocks appended until it stops requesting tools or the round cap is hit. Only the final text reply is appended to the long-lived `_conversations` history — the tool_use/tool_result exchange is scoped to `working_messages`, a local copy, so future turns don't carry that overhead.
- **SQL safety, two layers**: `_run_query_db` regex-rejects anything that doesn't start with `SELECT` (also naturally blocks multi-statement injection, since Python's `sqlite3.execute()` refuses more than one statement anyway); underneath that, the query always runs over a new `database.get_db_readonly()` connection — a SQLite URI `mode=ro` connection — so even a query that slipped past the regex physically cannot write. Verified both layers with tests (`fathom/tests/test_chat.py`): non-`SELECT` rejected, semicolon-chained write rejected, and a real DB-level check that the tank's name was unchanged after attempting a sneaky `UPDATE`.
- Verified live against real production data (not just mocks): asking tank 7 chat "When did I add the Kuhli Loaches?" now answers directly from the snapshot (`added_date` fix alone was enough, no tool call needed); asking tank 5 chat "Tell me the GH history of this tank" triggered a real `query_db` call (`SELECT timestamp, gh FROM test_results WHERE tank_id = 5 ORDER BY timestamp ASC`, visible in `/tmp/fathom.log`) and returned the full 17-reading history with a sensible trend summary.
- 9 new tests (2 in `test_helpers.py` for `_fmt_inhabitants`'s `added_date`, 7 in new `test_chat.py`); 257 total, all passing. `query_db` is mocked at the transport layer in tests the same way as other AI calls — via a fake `anthropic.Anthropic` class, not a real API key — so no credits are consumed.

### Changes in 2026-07-02 session (ninth)

- **Root cause of the inhabitant-duplication bug**: both the import review's dedup check (`_find_duplicates`) and the actual UPSERT match in `import_confirm` (`import_data.py`) keyed inhabitants purely on an exact-match `species` string. When the source text re-narrates the same population later with a recount (e.g. "Otocinclus sp." confirming "6 on 2026-06-05" — the exact same purchase/death history as an earlier "Otocinclus vittatus" entry, just phrased differently), the differing species string meant it was never recognized as the same inhabitant, so a second row got created instead of updating the first. Real example from Rob's live "Fish Tank" (40g) import: this happened for Otocinclus Catfish, Kuhli Loach, and non-zebra Nerite Snail. Fixed both the dedup check and the UPSERT match to key on `common_name` first (falling back to `species`) — common_name stayed stable across the duplicate mentions in all three cases even though species text drifted. Added `IMPORT_PROMPT` rule 18 instructing Claude to keep species/common_name strings consistent for the same real-world population across a document, and rule 17 clarifying that consumable/food items (cuttlebone, calcium chews) are not Hardscape.
- **Zero-count inhabitants no longer shown**: `inhabitants.count = 0` (a population that fully died off, as opposed to `NULL` = uncountable "many") was still rendering as a current inhabitant on both `/tanks/{id}/inhabitants` and the dashboard's Inhabitants panel. Both queries (`routers/inhabitants.py`, `routers/tanks.py`) now filter `WHERE count IS NULL OR count > 0`. Historical `population_events` and AI-facing queries are untouched — a dead-off population should disappear from "what's in the tank now" views but stay fully visible in history/analysis.
- **Cleaned up tank 7's actual data** (real user data, not test fixtures) to match: deleted the two food-item Hardscape rows; merged each duplicate-inhabitant pair into the earlier row, added a backdated `died` population_event to reconcile the tracked count down to the later recount's true count (Otocinclus 9→6, Kuhli Loach 7→3, non-zebra Nerite 4→3, each with a note explaining the reconciliation), and deleted the redundant duplicate row + its now-redundant population_event; zeroed out the merged Red Rili/Red Cherry Shrimp inhabitant (was still showing count 10 despite the whole batch dying off per Rob) with a dated correction event, and resolved issue #78 ("Red Cherry Shrimp deaths post-introduction") accordingly. Took a timestamped `.bak` copy of `fathom.db` before editing. Verified live via Playwright against the running dev server: Inhabitants page and dashboard panel both show the correct 8 species with no duplicates and no zero-count rows; Hardscape page no longer lists Cuttlebone/Calcium Bites; historical activity feed still correctly shows the die-off/reconciliation events.
- 233 existing tests still pass; no new tests added (this was primarily a production-data correction plus two small query filters — the import dedup/UPSERT keying change is exercised by existing `test_import.py` coverage via the pre-existing species-based dedup tests, which use inputs where species and common_name never diverge).

### Changes in 2026-07-02 session (eighth)

- **Equipment specs field UX**: the "Specs (JSON or description)" label/placeholder implied users should type JSON, but nobody would do that by hand. Relabeled to plain "Specs" on both Add and Edit forms. The edit modal was also prefilling the raw stored JSON (e.g. `{"type": "prefilter sponge"}` from imports, or `{"description": "..."}` for hand-typed text) straight into the textarea — added `_specs_display()` in `equipment.py` (list route only) to flatten any stored JSON back to readable plain text (`{"description": x}` → `x`; other dicts → `key: value, key: value`; falls back to raw string if not JSON). Saving still goes through the existing wrap-as-JSON-if-not-valid-JSON logic in the POST handlers, unchanged.
- **Action button spacing**: Equipment and Purchases table rows had their Edit/💬 Observations/✕ delete buttons packed edge-to-edge with no gap (inline elements, no margin). Added `class="actions-cell"` to both tables' action `<td>` and a `.actions-cell > * + * { margin-left: .35rem }` CSS rule.
- 1 test suite run only (no new tests — pure display/CSS fix, existing 233 still pass).

### Changes in 2026-07-02 session (seventh)

- **Population chart froze at a stale precise number once a species' count became "many"/unknown**: `update_inhabitant` only ever inserted a `population_events` row when transitioning between two *known* counts (`actual_count is not None and old["count"] is not None`) — going from a known count to `count_unknown=true` (or back) silently updated `inhabitants.count` but recorded no event, so the chart's delta-summed line just kept showing the last known total forever (e.g. Ramshorn Snail frozen at 24, Bladder Snail frozen at 2, on Rob's real tank-5 data). Rather than invent a new event type for the delta model, `chart_population`'s `current` query (`routers/tanks.py`) dropped its `count > 0` filter so it now returns *every* inhabitant including unknown ones (`count: null`), and `loadPopChart` (`app.js`) uses that as the authoritative value for "today" instead of the summed running total — a `null` there is a real gap in the Chart.js line (default `spanGaps: false`), and the dataset legend label gets a `(now: many)` suffix. Verified live against tank 5 by inspecting the actual `Chart.js` dataset arrays via Playwright: Ramshorn/Bladder Snail data now end in `null` after their last known-count date, Fire Red Shrimp (still numerically tracked) is unaffected.
- **Import prompt: parenthetical detail dumped into structured fields instead of notes**: same pattern as the existing hardscape-item-name (rule 13) and equipment-brand/model-split (rule 14) fixes, found in two more places on Rob's real tank-5 data. Rule 15: `source` (inhabitants/plants/hardscape) should be a short vendor/origin name only — `"SF Aquatic (purchased online, $31.02)"` should become `source="SF Aquatic"` + the parenthetical moved to `notes`. Rule 16: equipment `model` should be the product name only, not a full listing title — `"Prefilter Intake Cover for Fluval Flex Spec Evo (Spec III & V 2.6/5G)"` should become `model="Prefilter Intake Cover"` + the compatibility note moved to `notes`. Both are prompt-only fixes (`IMPORT_PROMPT` in `import_data.py`); existing bad rows in tank 5 were left as-is (real production data, not touched without being asked).
- **Equipment and purchases were view/add/delete-only in the UI** — no way to fix a typo or update a value without deleting and re-adding. `equipment.py` already had a working `POST /{eq_id}/update` backend with no UI wired to it; added an edit modal + button to `equipment/list.html` (data-attributes read via JS, same pattern as the ref-info trigger, to avoid the quote-escaping fragility of the existing `openEditInh`-style inline-string approach). Purchases had no update endpoint at all — added `POST /purchases/{purchase_id}/update` to `purchases.py` (unprefixed, matching the existing delete route's shape) plus a matching edit modal in `purchases/list.html`. Both verified end-to-end live via Playwright (prefill → edit → save → persisted) against tank 4 (scratch tank).
- 3 new tests: equipment update, purchase update, population-chart `current` includes unknown-count inhabitants.

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
