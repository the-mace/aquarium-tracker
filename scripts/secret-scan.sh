#!/usr/bin/env bash
# Scan files for common API-key patterns. Used by the git hook and by CI.
# Usage:
#   scripts/secret-scan.sh            # staged files (pre-commit)
#   scripts/secret-scan.sh --ci       # all tracked files
set -euo pipefail

# Deliberately specific — no generic "40 char base64" rule (false-positives
# on minified JS). AWS secret keys are caught next to an AKIA/ASIA id or
# an aws_secret_access_key assignment.
PATTERNS=(
  'sk-ant-[a-zA-Z0-9_-]{20,}'
  'AKIA[0-9A-Z]{16}'
  'ASIA[0-9A-Z]{16}'
  'aws_secret_access_key[[:space:]]*[=:]'
  'sk-[a-zA-Z0-9]{32,}'
  'ghp_[a-zA-Z0-9]{36}'
  'github_pat_[a-zA-Z0-9_]{20,}'
  'xoxb-[0-9]+-[a-zA-Z0-9]+'
)

MODE="staged"
if [ "${1:-}" = "--ci" ]; then
  MODE="ci"
fi

if [ "$MODE" = "staged" ]; then
  FILES=$(git diff --cached --name-only --diff-filter=ACM)
else
  FILES=$(git ls-files)
fi

if [ -z "$FILES" ]; then
  exit 0
fi

FOUND=0
while IFS= read -r file; do
  [ -z "$file" ] && continue
  [ -f "$file" ] || continue
  case "$file" in
    *.png|*.jpg|*.jpeg|*.webp|*.gif|*.ico|*.woff|*.woff2) continue ;;
  esac
  for pattern in "${PATTERNS[@]}"; do
    if [ "$MODE" = "staged" ]; then
      hits=$(git show ":$file" 2>/dev/null | grep -cE "$pattern" || true)
    else
      hits=$(grep -cE "$pattern" "$file" || true)
    fi
    if [ "${hits:-0}" -gt 0 ]; then
      echo "BLOCKED: Possible secret in $file (pattern: $pattern)"
      FOUND=1
    fi
  done
done <<< "$FILES"

if [ "$FOUND" -eq 1 ]; then
  echo ""
  echo "Secret scan blocked: potential secrets detected."
  echo "If this is a false positive, tighten the match or exclude the file."
  exit 1
fi

exit 0
