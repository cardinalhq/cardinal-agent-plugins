#!/usr/bin/env python3
"""cardinal decision_prompt hook — UserPromptSubmit.

When decision capture is on (`cardinal-decision on`), tells the agent to
record meaningful decisions with bin/cardinal-decision and lists this
session's decisions so far, so new ones can link to them (follows /
refines / supersedes). Silent when capture is off.

Contract: stdin is Claude Code's UserPromptSubmit JSON ({session_id,
prompt, ...}); stdout is hookSpecificOutput.additionalContext when
capture is on, else nothing. Sync (its output must reach this turn's
context). Disabled sessions skip telemetry and decision imports. Every failure
exits 0 silently.

Not connected (hooks/_connection.py: /cardinal:connect never ran, the
plugin is local-only and its MCP server has no URL): exits 0 at once, with
no output and no network.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402
from cardinal_core.machine_prompts import is_claude_machine_prompt  # noqa: E402
CLI = Path(__file__).resolve().parent.parent / "bin" / "cardinal-decision"


def build_context(cli: str, session_id: str, entries: list[dict]) -> str:
    from cardinal_core.decisions import render_ledger
    return (
        "Cardinal decision capture is on for this session. When you make a choice that "
        "constrains later work (picking between approaches, settling an open question, or "
        "the user deciding something), record it right away with one Bash call:\n"
        f'"{cli}" record --session {session_id} --choice "<the option chosen, 2-7 words>" '
        '--question "<what had to be settled>" --why "<one sentence>" '
        '[--alt "<rejected option>"]... [--by user] [--anchor <path>[::Symbol]]... '
        "[--follows|--refines|--supersedes <id>]\n"
        "Record choices, not progress, findings, or tool calls. Use --by user when the user "
        "made the call. Anchor the files or symbols the decision governs. Link a decision to "
        "an earlier one when it builds on, narrows, or replaces it.\n"
        "Decisions so far this session:\n"
        f"{render_ledger(entries)}"
    )


def main(payload=None, *, emit=None) -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}") if payload is None else payload
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    prompt = payload.get("prompt")
    if payload.get("agent_id") or is_claude_machine_prompt(prompt):
        return
    if not _connection.is_connected():
        return
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return
    # Check the switch before importing the decision and telemetry modules.
    settings_env = _connection._settings_env(Path.home() / ".claude")
    override = settings_env.get("CARDINAL_DECISIONS") or os.environ.get("CARDINAL_DECISIONS")
    runtime_dir = Path.home() / ".claude" / "cardinal"
    if isinstance(override, str) and override.strip().lower() in ("1", "true", "on", "yes", "0", "false", "off", "no"):
        enabled = override.strip().lower() in ("1", "true", "on", "yes")
    else:
        try:
            enabled = json.loads((runtime_dir / "decisions" / "config.json").read_text()).get("enabled") is True
        except (OSError, ValueError, AttributeError):
            enabled = False
    if not enabled:
        return

    from cardinal_core import decisions
    entries = decisions.read_ledger(runtime_dir, session_id)
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": build_context(str(CLI), session_id, entries),
        }
    }
    (emit or (lambda value: print(json.dumps(value))))(out)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
