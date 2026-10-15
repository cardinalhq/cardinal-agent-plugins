#!/usr/bin/env python3
"""cardinal owner relay — UserPromptSubmit: the owner decides, in this
(supervisor) session's own terminal, for the worker it supervises
(cardinal_core.owner_relay; docs/specs/relayed-owner-decisions.md).

When the owner's whole prompt is `decide: <text>` (or `decide <name>:
<text>`), this hook relays <text> verbatim to the registered worker target
(`cardinal-storyboard investigation supervising`). When it is `approve
dp_…` (bare `approve` while it is the only pending proposal) or `reject
dp_…`, it relays or drops the decision the supervisor drafted with
`cardinal-storyboard investigation propose-decision` (nothing is sent until
then). The relay goes to maestro's relay-owner-decision with this machine's
API key (bounded, ~3 s), and the outcome is reported. This hook is the only caller of
that route: no CLI subcommand and no MCP tool relays.

Independent of owner-input capture (owner-input.py, a separate hook): it
runs whether or not owner input is on. Refused: a subagent's prompt
(agent_id) and a task notification (owner_input.normalize_prompt).

Contract:
  - Input on stdin: Claude Code's UserPromptSubmit payload {session_id,
    prompt, agent_id?, ...}.
  - Output: nothing unless the prompt is a decision, approval or rejection; then ONE
    JSON object: systemMessage (the outcome, shown to the user) and
    hookSpecificOutput.additionalContext (the same outcome, for the model).
    The prompt is never blocked: it still reaches the model.
  - The debug log (CARDINAL_HOOK_DEBUG) gets a status code only.
  - Fail open: always exits 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

START = time.monotonic()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402

# A cheap pre-check before anything is imported: the core matches the
# normalized prompt exactly (owner_relay.APPROVAL_RE).
_MAYBE = re.compile(r"\s*(?:approve|reject|decide)\b", re.IGNORECASE)


def main() -> str:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return "bad_payload"
    if not isinstance(payload, dict):
        return "bad_payload"
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not _MAYBE.match(prompt):
        return "no_match"
    sid = payload.get("session_id")
    home = Path(os.environ.get("HOME") or str(Path.home()))
    if not isinstance(sid, str) or not sid or "/" in sid:
        return "invalid"
    if not _connection.is_connected():
        return "unconnected"
    import _storyboard_discovery
    from cardinal_core import owner_relay
    got = owner_relay.handle_prompt(home, sid, prompt, runtime="claude", conn=_storyboard_discovery.connection(),
                                    client=_storyboard_discovery.client_header(), agent_id=payload.get("agent_id"),
                                    deadline=START + owner_relay.BUDGET)
    if got.message:
        sys.stdout.write(json.dumps({
            "systemMessage": got.message,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": got.message},
        }))
        sys.stdout.flush()
    return got.status


if __name__ == "__main__":
    status = "error"
    try:
        status = main()
    except Exception:
        pass
    try:
        import _hook_debug
        _hook_debug.log("owner-relay", START, status=status)
    except Exception:
        pass
    sys.exit(0)
