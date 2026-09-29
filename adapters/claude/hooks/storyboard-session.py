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
  - No session-id sentence when there is no id, or the id is not one maestro accepts
    (^[A-Za-z0-9_-]{1,128}$, routes/storyboards-mcp-tools.ts CreateSchema).
  - Key-without-URL warning (_key_disclosure.py): when CARDINAL_MCP_API_KEY
    is set but CARDINAL_MCP_URL is not, the plugin's .mcp.json sends that key
    to Cardinal Cloud. This hook runs connected or not, so it adds a short
    "tell the user" note to the context — at most once per session id
    (marker under ~/.cardinal/key-warning/; without an id, only on startup).
  - Fail open: never blocks or delays session start, never prints an error.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _key_disclosure  # noqa: E402

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
WARNED_DIR = Path(".cardinal") / "key-warning"
WARNED_TTL_S = 30 * 86400


def session_id(payload: dict) -> str | None:
    for value in (
        payload.get("session_id"),
        os.environ.get("CLAUDE_CODE_SESSION_ID"),
        os.environ.get("CLAUDE_SESSION_ID"),
    ):
        if isinstance(value, str) and SESSION_ID_RE.match(value):
            return value
    return None


def _first_warning(sid: str | None, source) -> bool:
    """True the first time this session should see the key warning. Records
    the session id so resume / clear / compact don't repeat it; prunes
    markers older than WARNED_TTL_S. Without an id: startup only."""
    if not sid:
        return source in (None, "startup")
    try:
        home = Path(os.environ.get("HOME") or str(Path.home()))
        warned = home / WARNED_DIR
        marker = warned / sid
        if marker.exists():
            return False
        warned.mkdir(parents=True, exist_ok=True)
        now = time.time()
        for old in warned.iterdir():
            try:
                if now - old.stat().st_mtime > WARNED_TTL_S:
                    old.unlink()
            except OSError:
                pass
        marker.touch()
    except OSError:
        pass  # can't record it: warn anyway, a repeat beats a silent send
    return True


def key_warning(sid: str | None, source) -> str | None:
    if not _key_disclosure.key_without_url():
        return None
    if not _first_warning(sid, source):
        return None
    return "Tell the user, briefly: " + _key_disclosure.WARNING


def main() -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    sid = session_id(payload)
    parts = []
    if sid:
        parts.append(
            f"Cardinal session id for this session: {sid}. "
            "Pass it as session_id to storyboard__create."
        )
    try:
        warning = key_warning(sid, payload.get("source"))
    except Exception:
        warning = None
    if warning:
        parts.append(warning)
    if not parts:
        return
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": " ".join(parts),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
