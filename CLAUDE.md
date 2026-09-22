# Fathom — CLAUDE.md

Project context for AI-assisted development. Older session diaries are in git history.

## Before starting any work

**Always pull the latest `main` before beginning a session or starting a new task.** Dependabot is enabled and **auto-merges** its PRs (`.github/dependabot.yml` + `.github/workflows/dependabot-automerge.yml`), so `origin/main` can move while you're idle.

```bash
git checkout main
git pull --ff-only origin main
```

If `requirements.txt` or `requirements-dev.txt` changed, reinstall:

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium webkit   # only after a Playwright bump
```

Local UI verification uses **this venv's Playwright** (pinned in `requirements-dev.txt`), not system Python. Production (`bin/deploy-mini`) installs `requirements.txt` only.

Do **not** assume a clean `git status` means you're current with remote. If local work has diverged from an auto-merged Dependabot commit, rebase or merge `main`. Do not force-push over remote history.

**README.md is the public feature list.** When adding or changing a user-facing feature, update `README.md` in the same change (Features and Project Structure at minimum).

**File size.** Once in a while (session start, or after a file grew a lot), `wc -l` the files in play. If something is too large to edit well, split it. Do not report line counts unless there is a problem or Rob asks.

## What this is

Fathom is a personal aquarium and live-food tracking web app with AI analysis. Single user, self-hosted. No auth, no multi-tenancy.

## Stack

- **Backend**: Python 3 + FastAPI, uvicorn
- **Database**: SQLite. Main file `fathom/data/fathom.db`. Species/plant/hardscape cards in `fathom/data/reference_cache.db` (both gitignored)
- **Templates**: Jinja2, plain HTML/CSS/JS. No React, no build step
- **Charts**: Chart.js, vendored at `fathom/static/js/chart.umd.min.js`
- **AI**: Anthropic Python SDK, model id `CLAUDE_MODEL` in `fathom/ai_config.py` (current: `claude-sonnet-5`). One model for analysis, chat, import, and reference info
- **Env**: `.env` at repo root (gitignored). `chmod 600`. Secret scan: `git config core.hooksPath scripts/git-hooks` (CI runs the same scan)

## How to run

```bash
bin/run
# equivalent:
cd fathom && source ../.venv/bin/activate && uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The `.venv` is at the repo root. `bin/stop` kills a local uvicorn.

Logs (uvicorn + app) go to `/tmp/fathom.log` as well as stdout. On the mini, launchd stderr is `/tmp/fathom.err`. Use `bin/mini-logs` (`-n`, `-f`, `err`) instead of hand-written SSH.

## Production data

Dev and the Mac mini have **separate databases** and will keep diverging. Dev is for development. The mini is the real aquarium. Do not copy either DB over the other without an explicit yes. `bin/deploy-mini` takes an S3 backup of the mini DB before pulling code.

Host, SSH alias, and clone path are in gitignored `CLAUDE.local.md` and in `FATHOM_MINI_HOST` / `FATHOM_MINI_REPO` in `.env`.

## Project structure

```
aquarium-tracker/
├── fathom/
│   ├── main.py              # App, router includes, startup
│   ├── database.py          # Connections
│   ├── schema.py            # Canonical schema + versioned upgrades
│   ├── ai_config.py
│   ├── chat_writes.py       # Ask AI write tools
│   ├── routers/             # One module per feature; ai_prompts.py is prompt text
│   ├── templates/
│   ├── static/              # style.css, app.js, page scripts, Chart.js
│   ├── data/                # fathom.db, reference_cache.db (gitignored)
│   └── tests/
├── bin/                     # run, stop, deploy-mini, mini-logs
├── requirements.txt         # production
├── requirements-dev.txt     # local, includes Playwright
└── README.md
```

## Schema

`schema.py` holds the current `CREATE` statements and `SCHEMA_VERSION`. Startup (`init_db` → `ensure_schema`):

- Already at `SCHEMA_VERSION`: return.
- Empty database: run `CANONICAL_SCHEMA`, stamp version 1.
- Database from before versions (tables exist, no `schema_migrations`): require the current sentinel columns, copy any main-database `reference_info` rows into the cache, drop that table, stamp version 1.
- Later changes: add a function in `_apply_pending` and bump `SCHEMA_VERSION`. Applied versions do not run again.

A file older than those sentinels refuses to stamp. Open it once on the last pre-version release, then upgrade.

Table groups (details are the SQL, not a second list here):

- **Tanks**: `tanks`, tests, events, inhabitants, population events, plants, hardscape, equipment, purchases, issues, goals + dependencies, observations + links, schedules, tank summary, notes proposals, chat
- **House**: `home_water_tests`, `home_water_summary`
- **Cultures** (not tanks): `cultures`, `culture_vessels`, `culture_log`, `culture_log_vessels`, `culture_schedule`
- **Cache DB**: `reference_info` (species / plant / hardscape). Ask AI reads it through a read-only temp view named `reference_info`. It is not a table in `fathom.db`

SQLite WAL and `foreign_keys=ON` are set in `get_connection()`.

## AI

- **Analysis** after a water test or event: `routers/ai_analysis.py` calls Claude and stores an observation plus `tank_state_summary`. Prompt text and parsers are in `routers/ai_prompts.py`. Tank notes override generic species norms.
- **Recommendation** after a manual test save appends a short next action to that test's notes. Import and Quick Log do not queue it.
- **Goal review / progress** and **tank-notes proposals** use the same prompt module. Notes change only after the user accepts a proposal.
- **Ask AI**: persisted conversations per tank or per culture. `query_db` is one read-only `SELECT` (`get_db_readonly`, `mode=ro`, plus the reference-cache view). Tank chat must filter `tank_id`. Culture chat can read every culture station and cannot read tank tables. Write tools (`chat_writes.py`): tank chat can add an observation, log an event (no background analysis), and append notes. Culture chat can log a look/other note and append notes on the **viewed** station or one of its bins. No SQL writes, deletes, or water-test/count edits.
- **Reference info**: cache DB only. Fetched with web search when an inhabitant, plant, or hardscape item is added or missing on the list page. Tank manufacturer/model can backfill still-empty volume/dimensions.
- **Import / Quick Log**: one extraction stream, one confirm path, one review script (`static/js/import-review.js`).
- **Blocking calls**: `messages.create` from `async def` must run in `asyncio.to_thread`. A sync Anthropic call on the single uvicorn worker stalls every request.
- **Model policy**: mass-class Sonnet only, one id in `ai_config.py`. No second provider. No prompt caching (prompts are built from live rows; volume is one user).

## Gotchas

- Router prefixes are inconsistent. `import_data.py` and `purchases.py` have no prefix; paths are fully spelled on each route. Most other routers use a prefix.
- Moment timestamps (`events`, `test_results`, `observations`) are stored UTC and shown in browser-local time (`app.js` helpers). Schedule dates and Today are calendar days, not instants.
- Culture log writes never trigger tank AI. Do not model bins as tanks.
- `CHECK` constraints (culture feed, event type, issue status, and similar) need a versioned table rebuild to extend. The culture feed list is `spirulina`, `green_water`, `yeast`, `none`.
- Page behavior for cultures, goals, home water, and import review lives in `fathom/static/js/`, not in the templates. Shared chrome is `app.js`.
- Static files are served `Cache-Control: no-cache` because there is no hashed filename.

## Cultures

Not tanks. One station has one purpose (Daphnia *or* green water). Bins inherit that role. Harvest destination is a tank, another culture, or a specific bin. Logged culture tasks show on Today.

Green water is not fed. Daphnia is. Looks store tint (green water) or density + guts (Daphnia), plus water temp per bin. Heater setpoint on a bin is standing config, not a measured temp. Bench air temp/RH is its own log kind.

**Shrimp Tank stays isolated.** Harvest, water, animals, nets, and cups from the Daphnia/green bins go to the Fish Tank only.

Husbandry that doesn't change day to day:

- Cladoceran is Daphnia magna. Food is green water long-term, with a light powder bridge. Powder doses are logged as `spirulina` until the feed list grows; Chlorella is the powder actually in use.
- Wide shallow bins, low-flow air. Daphnia air goes through a seasoned sponge, very light, no surface ripples beyond the bubbles.
- Dose a pinch into a cup of **that bin's** water. Don't share a mix-cup across bins. First feeds on a new powder are spare.
- Gut check 1–4 h after feeding (sweet spot ~2 h), through the wall, backlit. Don't net them. Next morning, judge water clarity only.
- Don't harvest for the Fish Tank until the culture is clearly breeding, and don't dump or big-water-change the Daphnia bins.
- Green water starts on Fish Tank water, no soap, lights 12–15 h, no powder or fertilizer until tint is real. If it's still clear past a couple of weeks, escalate the inoculum (light sponge squeeze or scrape, then closer light, then a live phytoplankton starter). One light fertilizer dose only after there's something to feed.
- No Daphnia in green bins. No snails in green bins. Snails in Daphnia bins only once the Daphnia are established.
- The bench probe reads air, not bin water. Lights can warm the water during the on-window.
- Mesh over open bins when larvae show up. No pesticide near the cultures.
- One crashed bin stays isolated: dump it, rinse the net, never pour crash water into a healthy bin.

Order history, the room layout, and sitter logistics stay in gitignored `.claude/live-food-culture.md`.

## Production deployment

launchd plist `/Library/LaunchDaemons/com.fathom.plist` with `UserName` set to the login user (without it the daemon runs as root). Working directory is the clone's `fathom/`. Uvicorn is the venv binary built from pyenv 3.14, not `/usr/bin/python3`. `KeepAlive` restarts the process after `bin/deploy-mini` kills it, so routine deploys need no sudo. `sudo launchctl` needs a real TTY.

macOS Application Firewall allow-lists by binary path. The pyenv Python must be added and unblocked or other machines time out on port 8000.

`bin/deploy-mini` refuses a dirty mini checkout, S3-backs up the DB, fast-forwards `origin/main`, installs `requirements.txt` only, kills uvicorn, health-checks `GET /tanks`, and resets to the previous SHA if that check doesn't return 200.

Non-interactive SSH on the mini has a PATH that makes Apple's git hit an Xcode license error. `deploy-mini` prefixes `/opt/homebrew/bin`. The backup cron needs the same prefix so `aws` resolves:

```bash
0 3 * * * PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin bash <clone>/fathom/scripts/backup_db.sh >> /tmp/fathom-backup.log 2>&1
```

Bucket `$S3_BACKUP_BUCKET`, 30-day lifecycle on `backups/`. The upload IAM user has no `DeleteObject`.

VPN notes (L2TP to the home router, split tunnel, hairpin doesn't work from inside the LAN) live in `CLAUDE.local.md` along with the hostname.

## Testing

```bash
.venv/bin/python -m pytest fathom/tests/ -q
```

Run before committing. AI calls are mocked in `conftest.py` (no API credits). `test_ai_recommendation.py` drives the real recommendation function with a fake Anthropic client.

UI changes: start `bin/run` and drive the flow with venv Playwright (`.venv/bin/python`, `sync_playwright`). Chromium by default. WebKit when layout or WebKit bugs matter. Don't write test data into the real Fish Tank; use a scratch tank.
