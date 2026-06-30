# Fathom

Smart aquarium tracking with AI-powered analysis.

## Stack

- **Backend**: Python + FastAPI
- **Database**: SQLite (`fathom/data/fathom.db`)
- **Frontend**: Plain HTML/CSS/JS + Chart.js
- **Templates**: Jinja2
- **AI**: Anthropic claude-sonnet-4-6

## Setup

### 1. Clone and install dependencies

```bash
git clone git@github.com:the-mace/aquarium-tracker.git
cd aquarium-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY
```

### 3. Run

```bash
cd fathom
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## Features

- **Dashboard** per tank: latest water parameters, inhabitants, open issues, AI summary, Chart.js charts
- **Water test logging**: pH, GH, KH, ammonia, nitrite, nitrate, TDS, temperature
- **AI analysis**: automatic background analysis triggered on each test/event save (claude-sonnet-4-6)
- **AI chat**: ask questions about a tank with full context injection
- **Import**: upload Apple Notes HTML or plain text exports; Claude extracts structured data for preview before inserting
- **Population tracking**: per-species counts with event log (added/died/removed/born)
- **Equipment, purchases, issues** management
- **Cost charts**: spending by category and by month

## Production Deployment (Mac mini)

The Mac mini at `192.168.50.205` is the production deployment. SSH via `ssh -A rob@192.168.50.205`.

### Initial setup on Mac mini

```bash
ssh -A rob@192.168.50.205
git clone git@github.com:the-mace/aquarium-tracker.git
cd aquarium-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# add keys to .env
```

### Run as a service

Create `/Library/LaunchDaemons/com.fathom.plist` (or use `launchctl` as the user):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.fathom</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/rob/aquarium-tracker/.venv/bin/uvicorn</string>
    <string>main:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/rob/aquarium-tracker/fathom</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DOTENV_PATH</key><string>/Users/rob/aquarium-tracker/.env</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/fathom.log</string>
  <key>StandardErrorPath</key><string>/tmp/fathom.err</string>
</dict>
</plist>
```

## S3 Backup

The backup script at `fathom/scripts/backup_db.sh` gzips the SQLite database and uploads it to S3.

### Setup

1. Set `S3_BACKUP_BUCKET` and `AWS_PROFILE` in `.env`
2. Ensure AWS credentials are configured for the profile
3. Test manually: `bash fathom/scripts/backup_db.sh`

### Cron on Mac mini

```bash
# Edit crontab
crontab -e

# Add a daily backup at 3am
0 3 * * * cd /Users/rob/aquarium-tracker && bash fathom/scripts/backup_db.sh >> /tmp/fathom-backup.log 2>&1
```

## Project Structure

```
aquarium-tracker/
├── fathom/
│   ├── main.py              # FastAPI app entry point
│   ├── database.py          # Schema, migrations, connection helpers
│   ├── routers/
│   │   ├── tanks.py         # Tank CRUD + dashboard + chart data
│   │   ├── test_results.py  # Water test CRUD + AI trigger
│   │   ├── events.py        # Event log + AI trigger
│   │   ├── inhabitants.py   # Species management + population events
│   │   ├── equipment.py     # Equipment per tank
│   │   ├── purchases.py     # Purchase tracking
│   │   ├── issues.py        # Issue tracker
│   │   ├── observations.py  # Manual + AI observations
│   │   ├── chat.py          # AI chat endpoint
│   │   ├── import_data.py   # File import + Claude extraction
│   │   └── ai_analysis.py   # Background AI analysis helper
│   ├── templates/           # Jinja2 HTML templates
│   ├── static/              # CSS + JS
│   ├── data/                # SQLite DB (gitignored)
│   └── scripts/
│       └── backup_db.sh     # S3 backup script
├── .env.example
├── requirements.txt
└── README.md
```

## Security

A pre-commit hook (`.git/hooks/pre-commit`) scans staged files for common API key patterns and blocks the commit if found. `.env` and `fathom/data/` are gitignored.
