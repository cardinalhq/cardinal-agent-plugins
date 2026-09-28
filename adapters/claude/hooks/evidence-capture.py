#!/usr/bin/env python3
"""cardinal evidence capture — PostToolUse hook on every MCP tool (mcp__.*).

Records the result of a call to a non-Cardinal MCP server in the local
evidence spool (cardinal_core.evidence), so a storyboard can later cite it as
*captured* evidence. Cardinal's own gateway tools (mcp__cardinal__*,
mcp__plugin_cardinal_cardinal__*) are skipped: the gateway already mints a
*witnessed* receipt for them.

Contract:
  - Input on stdin: Claude Code's PostToolUse payload {tool_name, tool_input,
    tool_response, tool_use_id, session_id, ...}. tool_response is whatever
    the MCP tool returned, in any shape cardinal_core.evidence.normalize
    accepts (a string, {content, structuredContent}, a list of content
    blocks, or Claude Code's "Output has been saved to <file>" spill notice,
    followed only when the file resolves under ~/.claude/projects/).
  - Writes ~/.cardinal/evidence/<session_id>/ev_<12 hex>.json: directories
    0700, file 0600, written atomically. Arguments and result are scrubbed
    of credentials (the gateway's receipt scrub, ported) and the result is
    capped at 256 KiB. Entries older than 14 days are removed
    opportunistically (time-bounded).
  - Output: hookSpecificOutput.additionalContext, one line:
    "[evidence:ev_xxx] captured locally from <server>/<tool>; to cite it in
    a storyboard run cardinal-evidence promote ev_xxx".
  - Opt-out: CARDINAL_EVIDENCE_CAPTURE=0 or the flag file
    ~/.cardinal/evidence/disabled. Silent when disabled, for a Cardinal tool
    or a non-MCP tool, or when the payload is unreadable.
  - No network. Fail open: always exits 0, never blocks the tool, never
    prints an error. hooks.json gives it ~2 s.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Cardinal's own MCP servers, as Claude Code names them (a user-scope
# `cardinal` server, or the plugin's bundled one). The gateway mints
# witnessed receipts for these.
CARDINAL_SERVERS = ("cardinal", "plugin_cardinal_cardinal")


def home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def context_line(ev_id: str, server: str, tool: str) -> str:
    return (f"[evidence:{ev_id}] captured locally from {server}/{tool}; "
            f"to cite it in a storyboard run cardinal-evidence promote {ev_id}")


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    from cardinal_core import evidence

    tool_name = payload.get("tool_name")
    parts = evidence.split_mcp_tool(tool_name)
    if parts is None:
        return
    server, tool = parts
    if server in CARDINAL_SERVERS:
        return
    home = home_dir()
    root = evidence.default_root(home)
    if evidence.capture_disabled(root):
        return
    entry = evidence.build_entry(
        server=server,
        tool=tool,
        tool_name=tool_name,
        tool_input=payload.get("tool_input"),
        tool_response=payload.get("tool_response"),
        session_id=payload.get("session_id"),
        spill_root=home / ".claude" / "projects",
        tool_use_id=payload.get("tool_use_id"),
        agent="claude-code",
    )
    evidence.write_entry(root, entry)
    try:
        evidence.gc(root)
    except Exception:
        pass
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context_line(entry["evidence_id"], server, tool),
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    sys.exit(0)
