#!/usr/bin/env bash
# Fathom DB backup — gzip and upload to S3
# Usage: ./backup_db.sh
# Intended for cron on Mac mini. See README for cron setup.
set -euo pipefail

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
TMP_FILE="/tmp/${BACKUP_NAME}"

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
AWS_PROFILE="$AWS_PROFILE" aws s3 cp "$TMP_FILE" "s3://${S3_BUCKET}/backups/${BACKUP_NAME}"
rm -f "$TMP_FILE"

echo "Backup complete: s3://${S3_BUCKET}/backups/${BACKUP_NAME}"
