# Fathom

Personal aquarium and live-food tracking with AI-powered analysis. Single user, self-hosted. No auth, no multi-tenancy.

## Stack

- **Backend**: Python 3 + FastAPI + uvicorn
- **Database**: SQLite (`fathom/data/fathom.db`, gitignored)
- **Frontend**: Jinja2 + plain HTML/CSS/JS (no React, no build step)
- **Charts**: Chart.js (vendored at `fathom/static/js/chart.umd.min.js`)
- **AI**: Anthropic Claude (`claude-sonnet-5`, configured in `fathom/ai_config.py`)

## Setup

### 1. Clone and install dependencies

```bash
git clone git@github.com:the-mace/aquarium-tracker.git
cd aquarium-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The venv lives at the **repo root** (`aquarium-tracker/.venv`), not inside `fathom/`.

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
chmod 600 .env
git config core.hooksPath scripts/git-hooks   # secret scan on commit (also runs in CI)
```

AI features (analysis, Ask AI, import/Quick Log, reference info) need `ANTHROPIC_API_KEY`. The rest of the app works without it.

### 3. Run

```bash
bin/run
# or:
cd fathom
source ../.venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. `bin/stop` kills a local uvicorn process.

## Features

### House-wide

- **Today** (`/today`): feeding/dosing for the current weekday plus maintenance due across tanks **and** live-food cultures
- **Home water** (`/home-water`): shared tap/source tests (fill water for changes). Optional lab-report upload. Used as incoming-water context for tank AI — not tank chemistry
- **Cultures** (`/cultures`): live-food stations, **not** display tanks. One purpose per station (Daphnia *or* green water — green water is grown as feed, not fed itself). Each station has bins, a feed/look/harvest log, a recurring schedule, harvest destination (tank, another culture, or a specific bin), and harvest-status badges. Logged culture tasks also show on Today. Culture log writes never trigger tank AI

### Per tank

- **Dashboard**: latest water parameters, inhabitants, plants/hardscape, open issues, goals, AI summary, Chart.js water/population/cost charts, today's schedule and maintenance due
- **Water tests**: pH, GH, KH, ammonia, nitrite, nitrate, TDS, temperature. Form prefills from the last test. Saving queues background AI analysis + a short next-action recommendation, then waits on a status page until the dashboard summary is fresh
- **Inhabitants**: per-species counts (or "many" when uncountable) with a population event log (added/died/removed/born)
- **Plants & hardscape**: active plants and hardscape items
- **Equipment and purchases**: add/edit/delete; cost charts by category and month
- **Issues**: problem tracker (open / investigating / resolved)
- **Goals**: longer-horizon aims (params, stocking, breeding), distinct from issues. Optional dependencies, including on another tank
- **Observations**: manual notes plus auto AI analysis. A note can link to multiple inhabitants, plants, hardscape, or equipment
- **Timeline**: chronological mix of tests, events, observations, population changes, plants, hardscape, equipment, and issues; filterable and individually deletable
- **Schedule**: recurring feeding/dosing (reference, shown on Today) and logged maintenance (due dates, mark-done writes an event)
- **Quick Log**: paste freeform notes; Claude extracts structured rows for review (same confirm flow as file import)
- **Import**: upload Apple Notes HTML or plain text; Claude extracts tank specs, tests, events, purchases, inhabitants, equipment, plants, hardscape, issues, observations, and schedule. Review UI lets you edit/uncheck rows before save

### AI

- **Background analysis** after each water test or event: observation + tank-state summary. Tank notes override generic species norms when they describe accepted baselines
- **Ask AI (tanks)**: persisted conversations on the tank, with a read-only `query_db` tool for history (test trends, when something was added, spend). Popup on tank pages plus a full-page thread list
- **Ask AI (cultures)**: same UI on a culture station (`/cultures/{id}`). Knows **all** culture stations (green water feeds live food) — bins, logs, schedules, harvest destinations as names. Does not include tank chemistry or livestock; `query_db` is limited to culture tables
- **Reference info**: on inhabitant/plant/hardscape add (and list load), Claude fetches a description, care notes, and an image. Thumbnail in the table; click for the full card and a refresh button
- **Tank dimensions**: manufacturer/model can backfill missing volume/dimensions via a web-search-backed fetch (only fills still-empty fields)

## Tests

```bash
.venv/bin/python -m pytest fathom/tests/ -q
```

Integration tests in `fathom/tests/`. AI calls are mocked (no API credits). Run before committing.

## Running as a Service

For always-on access (home server, NAS, spare Mac, etc.), run Fathom as a background service.

### macOS — launchd

Create a plist at `~/Library/LaunchAgents/com.fathom.plist` (user service) or `/Library/LaunchDaemons/com.fathom.plist` (system service). Replace `/path/to/aquarium-tracker` with your actual clone path.

A **LaunchDaemon** without `UserName` runs as root and will leave the DB/venv root-owned — set `UserName` to the login user that owns the clone.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.fathom</string>
  <key>UserName</key><string>YOUR_LOGIN</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/aquarium-tracker/.venv/bin/uvicorn</string>
    <string>main:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>/path/to/aquarium-tracker/fathom</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DOTENV_PATH</key><string>/path/to/aquarium-tracker/.env</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/fathom.log</string>
  <key>StandardErrorPath</key><string>/tmp/fathom.err</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.fathom.plist` (or `sudo launchctl load` for a LaunchDaemon).

macOS Application Firewall allow-lists by **binary path**. If the venv's Python is not system `/usr/bin/python3`, LAN clients may time out on port 8000 until that binary is added and unblocked in the firewall (headless launchd never shows the GUI prompt).

### Linux — systemd

```ini
[Unit]
Description=Fathom aquarium tracker
After=network.target

[Service]
WorkingDirectory=/path/to/aquarium-tracker/fathom
ExecStart=/path/to/aquarium-tracker/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Environment=DOTENV_PATH=/path/to/aquarium-tracker/.env
Restart=always

[Install]
WantedBy=multi-user.target
```

Place in `/etc/systemd/system/fathom.service`, then `systemctl enable --now fathom`.

## S3 Backup

The backup script at `fathom/scripts/backup_db.sh` gzips the SQLite database and uploads it to S3.

### Setup

1. Set `S3_BACKUP_BUCKET` and `AWS_PROFILE` in `.env`
2. Ensure AWS credentials are configured for the profile
3. Test manually: `bash fathom/scripts/backup_db.sh`

A 30-day S3 Lifecycle expiration on the `backups/` prefix is a good way to rotate objects without giving the upload user `DeleteObject`.

### Schedule with cron

Homebrew `aws` is often not on cron's default `PATH` — set PATH explicitly if `aws` lives in `/opt/homebrew/bin`:

```bash
crontab -e

# Daily backup at 3am
0 3 * * * PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin bash /path/to/aquarium-tracker/fathom/scripts/backup_db.sh >> /tmp/fathom-backup.log 2>&1
```

## Project Structure

```
aquarium-tracker/
├── fathom/
│   ├── main.py              # FastAPI app, router includes, startup init_db()
│   ├── database.py          # Schema, migrations, connection helpers
│   ├── ai_config.py         # CLAUDE_MODEL and token budgets
│   ├── routers/
│   │   ├── tanks.py         # Tank CRUD + dashboard + chart data
│   │   ├── test_results.py  # Water test CRUD + AI trigger
│   │   ├── events.py        # Event log + AI trigger
│   │   ├── inhabitants.py   # Species + population events
│   │   ├── plants_hardscape.py
│   │   ├── equipment.py
│   │   ├── purchases.py
│   │   ├── issues.py
│   │   ├── goals.py
│   │   ├── observations.py
│   │   ├── timeline.py
│   │   ├── schedules.py     # Tank recurring schedule
│   │   ├── chat.py          # Ask AI (tanks + cultures)
│   │   ├── import_data.py   # File import + Quick Log extraction
│   │   ├── reference_info.py
│   │   ├── today.py
│   │   ├── home_water.py
│   │   ├── cultures.py      # Live-food stations (not tanks)
│   │   └── ai_analysis.py   # Background analysis / summary / recommendation
│   ├── templates/           # Jinja2 (tanks, cultures, today, home water, chat, …)
│   ├── static/              # CSS + JS + vendored Chart.js
│   ├── data/                # SQLite DB (gitignored)
│   ├── tests/               # pytest
│   └── scripts/
│       └── backup_db.sh
├── bin/
│   ├── run                  # local uvicorn --reload
│   ├── stop
│   ├── deploy-mini          # backup, pull, restart, health-check, rollback
│   └── mini-logs            # SSH tail of production logs
├── scripts/
│   ├── secret-scan.sh
│   └── git-hooks/           # core.hooksPath
├── .env.example
├── requirements.txt
└── README.md
```

## Security

Repo-managed git hooks (`git config core.hooksPath scripts/git-hooks`) run `scripts/secret-scan.sh` on commit. CI runs the same scan on every PR. `.env` and `fathom/data/` are gitignored. The app also tightens `.env` file mode on startup.
