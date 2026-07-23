#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
REPO_ROOT=$(git rev-parse --show-toplevel)
HOOK_DIR="$REPO_ROOT/.git/hooks"
HOOK_PATH="$HOOK_DIR/pre-commit"
mkdir -p "$HOOK_DIR"

# Back up any existing pre-commit hook that isn't already our symlink,
# so we never silently clobber a user's Husky / lefthook / hand-written hook.
if [ -e "$HOOK_PATH" ] && [ ! -L "$HOOK_PATH" ]; then
  BACKUP="$HOOK_PATH.pre-invariant.bak"
  mv "$HOOK_PATH" "$BACKUP"
  echo "Backed up existing hook → $BACKUP"
fi

ln -sf "${SCRIPT_DIR}/pre-commit-hook.sh" "$HOOK_PATH"
echo "Installed advisory Invariant pre-commit hook. Fires will log to ~/.invariant/dogfood-log.jsonl."
