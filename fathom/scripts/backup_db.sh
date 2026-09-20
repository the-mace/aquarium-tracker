#!/usr/bin/env bash
# Fathom DB backup — gzip and upload to S3
# Usage: ./backup_db.sh [--dry-run]
#   --dry-run  gzip locally and verify AWS credentials + bucket access with read-only
#              calls (sts get-caller-identity, s3 ls), but upload nothing
# Intended for cron on Mac mini. See README for cron setup.
set -euo pipefail

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) echo "ERROR: unknown argument: $arg (usage: backup_db.sh [--dry-run])" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Load .env if present
if [ -f "$REPO_ROOT/.env" ]; then
  set -a; source "$REPO_ROOT/.env"; set +a
fi

DB_PATH="${DB_PATH:-$SCRIPT_DIR/../data/fathom.db}"
S3_BUCKET="${S3_BACKUP_BUCKET:-}"
AWS_PROFILE="${AWS_PROFILE:-default}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_NAME="fathom_${TIMESTAMP}.db.gz"
umask 077
TMP_FILE="$(mktemp "${TMPDIR:-/tmp}/fathom_backup.XXXXXX.db.gz")"

if [ ! -f "$DB_PATH" ]; then
  echo "ERROR: Database not found at $DB_PATH" >&2
  exit 1
fi
if [ -z "$S3_BUCKET" ]; then
  echo "ERROR: S3_BACKUP_BUCKET not set" >&2
  exit 1
fi

echo "Backing up $DB_PATH → s3://${S3_BUCKET}/backups/${BACKUP_NAME}"
gzip -c "$DB_PATH" > "$TMP_FILE"
if [ "$DRY_RUN" -eq 1 ]; then
  rm -f "$TMP_FILE"
  echo "Dry run: checking AWS credentials (profile: $AWS_PROFILE)..."
  AWS_PROFILE="$AWS_PROFILE" aws sts get-caller-identity --query Arn --output text
  echo "Dry run: checking read access to s3://${S3_BUCKET}/backups/ ..."
  AWS_PROFILE="$AWS_PROFILE" aws s3 ls "s3://${S3_BUCKET}/backups/" > /dev/null
  echo "Dry run OK — credentials and bucket reachable; nothing uploaded (PutObject not exercised)."
  exit 0
fi
AWS_PROFILE="$AWS_PROFILE" aws s3 cp "$TMP_FILE" "s3://${S3_BUCKET}/backups/${BACKUP_NAME}"
rm -f "$TMP_FILE"

echo "Backup complete: s3://${S3_BUCKET}/backups/${BACKUP_NAME}"
