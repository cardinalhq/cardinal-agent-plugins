#!/usr/bin/env python3
"""cardinal storyboard discovery — SessionStart + UserPromptSubmit + SubagentStart hook.

Puts the Cardinal storyboards that may relate to this work (about it, or
only written from the same PR, branch, commit or edited files; labelled
which) in Claude's context, so a review,
a debugging session or resumed work starts from what the org already
established. Core logic, shared with the other runtimes:
cardinal_core.storyboard_authoring.relook_if_moved (over
storyboard_discovery.discover); this adapter's connection and client header:
hooks/_storyboard_discovery.py.

Contract:
  - Input on stdin: Claude Code's hook payload {session_id, cwd,
    hook_event_name, ...}.
  - SessionStart (startup, resume, clear, compact) always looks.
    UserPromptSubmit looks again only when the branch or HEAD changed since
    this session's last look (one git call otherwise, no network). Every
    look is remembered, failures included, so an unreachable maestro costs
    nothing on later prompts.
  - SubagentStart: a subagent (a forked skill, an Agent/Task call) starts
    without the session's SessionStart context, so the block the session's
    last look rendered (stored next to its branch/HEAD) is emitted again for
    the subagent. No git call, no network. Nothing when no block is stored
    (no look yet, nothing matched last time, no session id), and nothing
    for the supervise skill's fresh-context subagent (agent_type
    cardinal:stepback), which must see only its brief and the code.
  - Output: hookSpecificOutput {hookEventName, additionalContext} with at
    most 3 storyboards and 2 KB, framed as DATA written by org members, not
    instructions, and pointing at storyboard__get. Draft acts' statements are
    inlined marked "[draft, not yet checked]". Nothing when nothing matches.
  - The unified prompt-submit.py calls this adapter in background mode:
    changed HEADs schedule a detached refresh, whose pending block is
    delivered on the next human prompt. SessionStart/SubagentStart remain
    synchronous.
  - Silent no-op (exit 0, no output, no network) when not connected, when
    the connection has no MCP key (telemetry-only), outside a git repo with
    an origin, or with CARDINAL_STORYBOARD_DISCOVERY=0.
  - Bounded, from INTERPRETER START (imports and settings reads count: when
    several sessions start at once they alone can take most of a second): the
    network work ends by STARTED + BUDGET_S (at least MIN_NETWORK_S after the
    imports, never past STARTED + HARD_CAP_S). hooks.json's timeout (15) is
    the backstop, well clear of HARD_CAP_S. A block ready after DELIVER_BY_S
    is not printed (Claude Code may have stopped listening): it is stored as
    pending and the next prompt emits it, so the session and its subagents
    never disagree. Never blocks the prompt, never prints an error.
  - CARDINAL_HOOK_DEBUG=1: one line per run (event, wall time from
    interpreter start, whether a block was printed) in
    ~/.claude/cardinal/hook-debug.log (_hook_debug).
"""

from __future__ import annotations

import time

# Before any other import: the budget is measured from here.
STARTED = time.monotonic()

import json  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _connection  # noqa: E402
import _storyboard_discovery  # noqa: E402

EVENTS = ("SessionStart", "UserPromptSubmit", "SubagentStart")
# Subagents that get no discovery block (agents/stepback.md; a plugin agent's
# agent_type is "<plugin>:<name>").
FRESH_AGENTS = ("stepback",)

# Seconds from process start. hooks.json's timeout for this hook is 15.
BUDGET_S = 2.1
MIN_NETWORK_S = 1.0
HARD_CAP_S = 4.0
# Before hooks.json's 15 with room for what the clock cannot see: the
# interpreter's own startup before STARTED, and record_run + print after the
# late check.
DELIVER_BY_S = 4.5


def deadlines(now: float, started: float = STARTED) -> tuple:
    """(deadline, deliver_by) as absolute time.monotonic() values: the
    network work ends by started + BUDGET_S, or now + MIN_NETWORK_S when the
    startup ran late, but never after started + HARD_CAP_S."""
    deadline = min(max(started + BUDGET_S, now + MIN_NETWORK_S), started + HARD_CAP_S)
    return deadline, started + DELIVER_BY_S


def main(payload=None, *, emit=None, background=False, deadline=None, after_write=None) -> None:
    # Env and local-file checks first: no subprocess, no network.
    if _storyboard_discovery.is_disabled():
        return
    if not _connection.is_connected():
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}") if payload is None else payload
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    if event not in EVENTS:
        return
    agent_type = payload.get("agent_type")
    if event == "SubagentStart" and isinstance(agent_type, str) and agent_type.rsplit(":", 1)[-1] in FRESH_AGENTS:
        return
    if event == "UserPromptSubmit":
        from cardinal_core.machine_prompts import is_claude_machine_prompt
        if payload.get("agent_id") or is_claude_machine_prompt(payload.get("prompt")):
            return
    cwd = payload.get("cwd")
    if not (isinstance(cwd, str) and cwd and os.path.isdir(cwd)):
        cwd = os.getcwd()
    sid = payload.get("session_id")
    sid = sid if isinstance(sid, str) and sid else None

    local_deadline, deliver_by = deadlines(time.monotonic())
    # Background mode does no HTTP here; keep the remaining local-work budget.
    if not background or deadline is None:
        deadline = local_deadline if deadline is None else min(local_deadline, deadline)
    schedule = None
    if background and event == "UserPromptSubmit" and sid:
        from _discovery_refresh import spawn
        schedule = lambda: spawn(sid, cwd)
    from cardinal_core import storyboard_authoring as sa
    block = sa.relook_if_moved(sa.agent_paths("claude"), "claude", sid, cwd, event=event,
                               connection=_storyboard_discovery.connection,
                               client=_storyboard_discovery.client_header, deadline=deadline,
                               deliver_by=deliver_by, schedule=schedule, after_write=after_write)
    try:
        import _hook_debug
        _hook_debug.log("storyboard-discovery", STARTED, event=event, block=bool(block))
    except Exception:
        pass
    if not block:
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": block,
        }
    }
    if emit is None:
        sys.stdout.write(json.dumps(out))
    else:
        emit(out)


if __name__ == "__main__":
    # Bound the whole adapter, including imports/settings/Git/cache writes.
    from _prompt_support import bounded
    outputs = []
    try:
        bounded(lambda: main(emit=outputs.append), STARTED + DELIVER_BY_S)
        if outputs:
            sys.stdout.write(json.dumps(outputs[0]))
            sys.stdout.flush()
    except Exception:
        pass
    os._exit(0)
