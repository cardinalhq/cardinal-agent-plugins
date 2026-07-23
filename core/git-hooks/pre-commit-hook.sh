#!/usr/bin/env bash
# Advisory Invariant pre-commit hook — 1-week silent longitudinal trial
# scaffold (see core/git-hooks/README.md for install/uninstall + the
# dogfood-log.jsonl schema this appends to).
#
# CONTRACT: this hook NEVER blocks a commit. It always exits 0, no matter
# what check-pr.ts reports or whether anything below fails. It is advisory
# only — core/git-hooks/README.md: "aggressive availability, NOT forced
# consumption." A pre-commit hook exiting non-zero aborts the commit, which
# is exactly the behavior this trial must not have.
#
# Deliberately NOT `set -e`: any failure in this script must fall through to
# the trailing `exit 0`, not abort the commit.
set -uo pipefail

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0

# This hook is agent-agnostic and installs into ANY repo (not just
# conductor) — the invariant CLI it wraps lives in conductor's
# packages/invariant, checked out separately. Default to the conventional
# workspace layout; override via INVARIANT_PKG_DIR if conductor lives
# elsewhere. If it's not there, the tooling is unavailable — exit 0
# silently, never break a commit for missing tooling.
INVARIANT_PKG_DIR="${INVARIANT_PKG_DIR:-$HOME/workspace/conductor/packages/invariant}"
[ -d "$INVARIANT_PKG_DIR" ] || exit 0

INVARIANT_HOME="${HOME:-/tmp}/.invariant"
LOG_FILE="$INVARIANT_HOME/dogfood-log.jsonl"

mkdir -p "$INVARIANT_HOME" 2>/dev/null || exit 0

STAGED_DIFF=$(git diff --cached 2>/dev/null)
if [ -z "$STAGED_DIFF" ]; then
  exit 0
fi

STAGED_FILES_COUNT=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')

# `--guarantees-dir .` (matches the .github/workflows/invariant-gate.yml.example
# convention) run with cwd = packages/invariant, so it recursively picks up
# every *.invariants.yaml under this package in one pass.
REPORT=$(
  cd "$INVARIANT_PKG_DIR" 2>/dev/null &&
    printf '%s' "$STAGED_DIFF" | pnpm --filter @cardinalhq/invariant exec tsx scripts/check-pr.ts \
      --diff - \
      --guarantees-dir . 2>&1
)

# Advisory surface: print the report to stderr so it's visible before the
# user's editor/prompt opens for the commit message, but never touch the
# commit's exit status.
printf '%s\n' "$REPORT" >&2

SELF_REFERENTIAL=false
if printf '%s' "$REPORT" | grep -q 'Diff is self-referential'; then
  SELF_REFERENTIAL=true
fi

# Each fire renders as a `### ⚠️ \`id\`` (kind: guarantee) or
# `### 🐛 \`id\`` (kind: bug-pattern) heading — see render.ts. Parse those
# lines back into `{guarantee_id, kind}` records for the log.
FIRE_ENTRIES=""
while IFS= read -r line; do
  case "$line" in
    *⚠️*) KIND="guarantee" ;;
    *🐛*) KIND="bug-pattern" ;;
    *) continue ;;
  esac
  ID=$(printf '%s' "$line" | sed -E 's/.*`([^`]+)`.*/\1/')
  [ -z "$ID" ] && continue
  ENTRY="{\"guarantee_id\":\"$ID\",\"kind\":\"$KIND\"}"
  if [ -z "$FIRE_ENTRIES" ]; then
    FIRE_ENTRIES="$ENTRY"
  else
    FIRE_ENTRIES="$FIRE_ENTRIES,$ENTRY"
  fi
done <<EOF
$(printf '%s\n' "$REPORT" | grep -E '^### (⚠️|🐛) `' 2>/dev/null)
EOF

FIRES_JSON="[$FIRE_ENTRIES]"

# Logging policy (dogfood-trial calibration — see README):
#   - self-referential            -> log, with fires:[] (we want the
#                                     self-ref RATE too, for calibration)
#   - non-empty fires             -> log
#   - empty fires, not self-ref   -> do NOT log (keeps the log scannable)
if [ "$SELF_REFERENTIAL" = true ] || [ -n "$FIRE_ENTRIES" ]; then
  TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  REPO_NAME=$(basename "$REPO_ROOT")
  BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")
  RECORD=$(printf '{"ts":"%s","repo":"%s","branch":"%s","staged_files_count":%s,"self_referential":%s,"fires":%s,"outcome":null}' \
    "$TS" "$REPO_NAME" "$BRANCH" "${STAGED_FILES_COUNT:-0}" "$SELF_REFERENTIAL" "$FIRES_JSON")
  printf '%s\n' "$RECORD" >> "$LOG_FILE" 2>/dev/null
fi

exit 0
