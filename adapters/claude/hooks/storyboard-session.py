#!/usr/bin/env python3
"""cardinal storyboard session id — SessionStart hook.

Puts this Claude Code session's id in Claude's context so the storyboard
skill can pass it to `storyboard__create` as `session_id` (conductor
docs/specs/investigation-storyboards.md §15). maestro stores it on the
storyboard row only; receipts do not carry it.

Contract:
  - Input on stdin: Claude Code's SessionStart payload {session_id, ...}.
    Falls back to $CLAUDE_CODE_SESSION_ID / $CLAUDE_SESSION_ID.
  - Output: hookSpecificOutput.additionalContext with one sentence, in any
    directory (a storyboard does not need a git repo). SessionStart also
    fires on resume / clear / compact, so the id survives compaction.
  - Silent when there is no id, or the id is not one maestro accepts
    (^[A-Za-z0-9_-]{1,128}$, routes/storyboards-mcp-tools.ts CreateSchema).
  - In the slim cardinal-storyboards plugin: silent while the full cardinal
    plugin is active (hooks/_plugin_mode.py), which says it instead.
  - Fail open: never blocks or delays session start, never prints an error.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _plugin_mode  # noqa: E402
except Exception:  # this file copied alone: behave as the full plugin
    _plugin_mode = None

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def session_id(payload: dict) -> str | None:
    for value in (
        payload.get("session_id"),
        os.environ.get("CLAUDE_CODE_SESSION_ID"),
        os.environ.get("CLAUDE_SESSION_ID"),
    ):
        if isinstance(value, str) and SESSION_ID_RE.match(value):
            return value
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    home = Path(os.environ.get("HOME") or str(Path.home()))
    if _plugin_mode and _plugin_mode.slim_should_yield(home, cwd=payload.get("cwd")):
        return
    sid = session_id(payload)
    if not sid:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"Cardinal session id for this session: {sid}. "
                "Pass it as session_id to storyboard__create."
            ),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
