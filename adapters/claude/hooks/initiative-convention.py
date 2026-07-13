#!/usr/bin/env python3
"""cardinal initiative convention — SessionStart hook.

Tells Claude the branch-naming convention Cardinal uses to attribute
agent spend to initiatives. Every session in a git repo sees this
prompt so that when Claude cuts a new branch during the conversation,
the branch name produces a clean (initiative_name, initiative_type)
classification in the Outcomes Dashboard.

Contract:
  - Input on stdin: Claude Code's SessionStart hook JSON payload
    {session_id, cwd, hook_event_name, source, ...}.
  - Output: JSON on stdout with hookSpecificOutput.additionalContext
    when cwd is inside a git repo. Otherwise exits silently with no
    output (there's no branch to advise on).
  - Best-effort: any failure exits 0 silently. Never blocks session
    start.

Why a hook (not just README): Claude only sees what's in its context.
A README in the plugin repo doesn't reach the session running in a
different repo. SessionStart additionalContext is the surface Claude
Code provides for "tell the model this on every session" — short,
authoritative, in-context.

The convention text and standing-lines rendering come from
cardinal_core (session.convention_prompt / limits.standing_lines); the
budget fetch goes through the adapter's _limits shim because the ingest
key lives in Claude's OTel settings, not cardinal-secrets.json.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _limits  # noqa: E402
from cardinal_core.initiative import git_facts, is_git_repo  # noqa: E402
from cardinal_core.limits import standing_lines  # noqa: E402
from cardinal_core.session import convention_prompt  # noqa: E402

PROMPT = convention_prompt("Claude Code")


def _budget_standing(payload: dict, cwd: str) -> str | None:
    """One synchronous limits fetch at session start (short timeout, fail
    open) so the budget is part of the session's standing context from
    turn one — the agent starts economical instead of being corrected
    mid-flight. Also warm-writes the verdict file the per-turn sync gate
    reads. No-op when the backend doesn't advertise the limits protocol.

    Spec: conductor docs/specs/agent-spend-limits.md §Delivery.
    """
    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    if not session_id:
        return None

    if not _limits.limits_config():
        return None
    repo, branch = git_facts(cwd)
    verdict = _limits.maybe_refresh_verdict(
        session_id=session_id, repo=repo, branch=branch, force=True, timeout=1.5
    )
    if not verdict:
        return None

    lines = standing_lines(verdict)
    if not lines:
        return None
    parts = ["Cardinal spend budgets apply to this session:"]
    parts.extend(lines)
    # Server-authored copy rides through verbatim — when a threshold is
    # already crossed at session start, lead with the server's message.
    user_message = verdict.get("user_message")
    if isinstance(user_message, str) and user_message:
        parts.append(user_message)
    parts.append(
        "Work economically as budgets tighten; budget standing updates "
        "arrive automatically as the session proceeds."
    )
    return "\n".join(parts)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    cwd = (
        payload.get("cwd")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    if not is_git_repo(cwd):
        # Outside a git repo there's no branch to advise on; suppress
        # the prompt to avoid wasted context.
        sys.exit(0)

    context = PROMPT
    try:
        standing = _budget_standing(payload, cwd)
        if standing:
            context = f"{PROMPT}\n\n{standing}"
    except Exception:
        # Budget standing is additive — never let it cost the convention
        # prompt (or session start).
        pass

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
