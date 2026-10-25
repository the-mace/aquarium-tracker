---
name: deploy-mini
description: Deploy Fathom (aquarium-tracker) to the production Mac mini with bin/deploy-mini, check the result, read mini logs, or fix the mini's launchd, firewall, PATH or S3 backup cron. Use when Rob says deploy, ship, push to the mini, or the mini is down or serving old code.
---

# Deploy Fathom to the mini

Host, SSH alias and clone path are in gitignored `CLAUDE.local.md` and in
`FATHOM_MINI_HOST` / `FATHOM_MINI_REPO` in `.env`. Dev and mini databases
are separate; never copy one over the other without an explicit yes
(`AGENTS.md` → **Production data**).

## Routine deploy

1. Tests pass locally (`AGENTS.md` → **Testing**) and the change is committed.
2. `git push` to `origin/main`. The mini pulls `origin main`, not your
   checkout, so unpushed commits do not deploy.
3. Run `bin/deploy-mini`. It refuses a dirty mini checkout, backs the mini
   DB up to S3, fast-forwards `main`, installs `requirements.txt`, restarts
   uvicorn, and health-checks `GET /tanks`. "Already up to date" means
   nothing was pushed since the last deploy.
4. On "Healthy", spot-check what changed with `bin/mini-logs` (`-n 200`,
   `-f`, `err`) or by loading the changed page on the mini.
5. On a failed health check the script resets code to the previous SHA and
   restarts. It does **not** restore the database. If the new code applied a
   schema upgrade on startup (`SCHEMA_VERSION` bump), the DB stays upgraded
   under the old code; the S3 backup taken in step 3 is the restore point.
   Read `bin/mini-logs err` for the cause and tell Rob before restoring
   anything.

Dirty mini checkout: show Rob the `git status` output. Do not reset or
stash on the mini without a yes; those edits may be hand fixes.

## How production runs

launchd plist `/Library/LaunchDaemons/com.fathom.plist` with `UserName` set to the login user (without it the daemon runs as root). Working directory is the clone's `fathom/`. Uvicorn is the venv binary built from pyenv 3.14, not `/usr/bin/python3`. `KeepAlive` restarts the process after `bin/deploy-mini` kills it, so routine deploys need no sudo. `sudo launchctl` needs a real TTY.

macOS Application Firewall allow-lists by binary path. The pyenv Python must be added and unblocked or other machines time out on port 8000.

`bin/deploy-mini` refuses a dirty mini checkout, S3-backs up the DB, fast-forwards `origin/main`, installs `requirements.txt` only, kills uvicorn, health-checks `GET /tanks`, and resets to the previous SHA if that check doesn't return 200.

Non-interactive SSH on the mini has a PATH that makes Apple's git hit an Xcode license error. `deploy-mini` prefixes `/opt/homebrew/bin`. The backup cron needs the same prefix so `aws` resolves:

```bash
0 3 * * * PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin bash <clone>/fathom/scripts/backup_db.sh >> /tmp/fathom-backup.log 2>&1
```

Bucket `$S3_BACKUP_BUCKET`, 30-day lifecycle on `backups/`. The upload IAM user has no `DeleteObject`.

VPN notes (L2TP to the home router, split tunnel, hairpin doesn't work from inside the LAN) live in `CLAUDE.local.md` along with the hostname.
