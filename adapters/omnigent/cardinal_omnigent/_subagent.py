"""Sub-agent detection and parent-side branch correlation.

`sys_session_send` (polly's dispatch tool) has no `workspace`/`cwd` field on
its schema, so a polly-orchestrated child claude subprocess inherits polly's
cwd. The Claude Code CardinalHQ plugin's SessionStart hook runs
`git rev-parse` in that cwd and stamps polly's branch on every child's
`cardinal.branch` label — every polly fan-out rolls up under polly's
initiative regardless of the actual worktree the child was meant to work in.

Fixing that upstream requires threading `workspace` through
`sys_session_send` → `_execute_subagent_tool` → `POST /v1/sessions` →
`Conversation.workspace` → `runner/app.py` cwd resolution. Tracked as an
issue against omnigent-ai/omnigent; not merged yet.

This module implements the adapter-side workaround. The correlation shape:

  1. Polly's `fanout` skill mandates ``git worktree add <path> -b <branch>``
     BEFORE each ``sys_session_send``, and mandates the worktree path appear
     in the dispatch's ``args.input`` free text. Both flow through the
     adapter as tool_call events on the parent's session.
  2. On the parent-side bash tool_call, ``_sniff_worktree_add`` records
     ``worktree_registry[path] = branch`` in the parent's per-session state.
  3. On the parent-side ``sys_session_send`` tool_result,
     ``correlate_dispatch`` extracts the child's ``conversation_id`` from
     the JSON result and matches ``args.input`` (from ``request_data``)
     against the registered worktree paths. On a match it registers
     ``_CHILD_BRANCH_MAP[child_conv_id] = branch``.
  4. On the child's events, ``_resolve_branch`` (in telemetry.py) consults
     ``child_correlated_branch(child_conv_id)`` FIRST — a hit overrides the
     polluted ``cardinal.branch`` label with the true worktree branch.

Fallback path (when the correlation cannot resolve — polly used an existing
branch without ``-b``, the input text omits the worktree path, or the
subagent path is non-polly-native): ``lookup_subagent_identity`` uses
``omnigent.runtime`` internals to detect ``Conversation.kind == "sub_agent"``
and lets the caller stamp marker attributes (``cardinal.sub_agent`` etc.) so
downstream can audit inherited-attribution rows. Every internal-import path
is guarded — omnigent runtime moves and we degrade to no-op, never break the
policy loop.

The upstream `sys_session_send` schema fix, when it lands, makes this
module redundant. Watched by ``build/omnigent_drift.py``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

# --- Sub-agent identity via omnigent internals --------------------------------
#
# `omnigent.runtime.telemetry.current_session_id` returns the omnigent
# conversation id bound to the current request context; `get_conversation_store`
# opens the row where `kind`, `parent_conversation_id`, and `sub_agent_name`
# live. Both are internal (no policy-contract surface), so every access is
# guarded and returns None on any failure — never break telemetry because
# omnigent moved a private symbol.


@dataclass(frozen=True)
class SubagentIdentity:
    conversation_id: str
    parent_conversation_id: str
    sub_agent_name: str | None


def current_conv_id() -> str | None:
    """Omnigent conversation id from the request-scoped ContextVar, or
    None. Missing on: telemetry disabled, runner-local gate (no FastAPI
    hook), and any omnigent-internal move that renames the symbol."""
    try:
        from omnigent.runtime.telemetry import current_session_id
    except Exception:
        return None
    try:
        value = current_session_id()
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def lookup_subagent_identity(conv_id: str | None) -> SubagentIdentity | None:
    """Return SubagentIdentity when ``conv_id`` names a sub-agent child
    row; None for top-level sessions, unknown ids, or any store failure."""
    if not conv_id:
        return None
    try:
        from omnigent.runtime import get_conversation_store
    except Exception:
        return None
    try:
        store = get_conversation_store()
    except Exception:
        # Runtime not initialized (e.g. tests running without the server).
        return None
    try:
        conv = store.get_conversation(conv_id)
    except Exception:
        return None
    if conv is None:
        return None
    kind = getattr(conv, "kind", None)
    parent_id = getattr(conv, "parent_conversation_id", None)
    if kind != "sub_agent" or not isinstance(parent_id, str) or not parent_id:
        return None
    sub_agent_name = getattr(conv, "sub_agent_name", None)
    return SubagentIdentity(
        conversation_id=conv_id,
        parent_conversation_id=parent_id,
        sub_agent_name=sub_agent_name
        if isinstance(sub_agent_name, str) and sub_agent_name
        else None,
    )


# --- Parent-side worktree/dispatch correlation -------------------------------

# `git worktree add [<flags>] <path> -b <branch>` and its `-B` variant. Polly's
# fanout skill uses `-b polly/<task_id>` exactly; the -B alternate covers
# force-recreate. The optional `--` disambiguator between flags and path is
# accepted. `<path>` and `<branch>` accept the same character set as the
# existing branch-sniff regex (`_BRANCH_RE` in telemetry.py).
_WORKTREE_ADD_RES = (
    # -b/-B before path
    re.compile(
        r"\bgit\s+worktree\s+add\s+"
        r"(?:(?:-[fF]|--force|--detach|--checkout|--lock|--no-track)\s+)*"
        r"-[bB]\s+(?P<branch>[A-Za-z0-9][A-Za-z0-9._/-]*)\s+"
        r"(?:--\s+)?(?P<path>[^\s;&|]+)"
    ),
    # path before -b/-B
    re.compile(
        r"\bgit\s+worktree\s+add\s+"
        r"(?:(?:-[fF]|--force|--detach|--checkout|--lock|--no-track)\s+)*"
        r"(?:--\s+)?(?P<path>[^\s;&|]+)\s+"
        r"-[bB]\s+(?P<branch>[A-Za-z0-9][A-Za-z0-9._/-]*)"
    ),
)


def parse_worktree_add(command: str) -> tuple[str, str] | None:
    """(path, branch) when the command creates a new-branch worktree,
    else None. Attach-to-existing (no -b/-B) returns None on purpose:
    the branch is not on the command line and would require a shell-out
    to resolve — fallback path handles those."""
    for regex in _WORKTREE_ADD_RES:
        match = regex.search(command)
        if match is None:
            continue
        path = match.group("path").strip("'\"")
        branch = match.group("branch")
        if path and branch:
            return path, branch
    return None


# Cross-session map: CHILD omnigent conv id → correlated branch. Bounded LRU
# with TTL — children complete, orphaned entries expire. The upstream fix
# obsoletes this map entirely.
_CHILD_BRANCH_MAP: dict[str, tuple[str, float]] = {}
_CHILD_BRANCH_MAP_MAX = 4096
_CHILD_BRANCH_MAP_TTL_SEC = 24 * 3600.0  # a long-running fanout is still bounded


def _register_child_branch(child_conv_id: str, branch: str) -> None:
    now = time.monotonic()
    if child_conv_id in _CHILD_BRANCH_MAP:
        _CHILD_BRANCH_MAP.pop(child_conv_id)
    elif len(_CHILD_BRANCH_MAP) >= _CHILD_BRANCH_MAP_MAX:
        _CHILD_BRANCH_MAP.pop(next(iter(_CHILD_BRANCH_MAP)))
    _CHILD_BRANCH_MAP[child_conv_id] = (branch, now)


def child_correlated_branch(child_conv_id: str | None) -> str | None:
    """The branch correlated to ``child_conv_id`` at dispatch time, or
    None. TTL-evicts stale entries lazily on read."""
    if not child_conv_id:
        return None
    entry = _CHILD_BRANCH_MAP.get(child_conv_id)
    if entry is None:
        return None
    branch, stamped = entry
    if time.monotonic() - stamped > _CHILD_BRANCH_MAP_TTL_SEC:
        _CHILD_BRANCH_MAP.pop(child_conv_id, None)
        return None
    return branch


def register_worktree(state: dict[str, Any], path: str, branch: str) -> None:
    """Record a parent-side ``git worktree add`` — the parent's per-session
    state grows a ``worktree_registry`` map of path → branch. Correlation
    later substring-matches these paths against ``sys_session_send`` input
    text."""
    registry = state.get("worktree_registry")
    if not isinstance(registry, dict):
        registry = {}
        state["worktree_registry"] = registry
    registry[path] = branch


def _extract_input_text(request_data: Any) -> str:
    """The dispatch input text from a ``sys_session_send`` tool_call. The
    arguments field is a JSON string per the tool contract."""
    if not isinstance(request_data, dict):
        return ""
    args = request_data.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return args  # raw string; substring match still works
    if not isinstance(args, dict):
        return ""
    # The object-form args carry the input under `args.input`; the
    # top-level `input` is the flat-form.
    nested = args.get("args")
    if isinstance(nested, dict) and isinstance(nested.get("input"), str):
        return nested["input"]
    top = args.get("input")
    return top if isinstance(top, str) else ""


def _extract_child_conv_id(result_data: Any) -> str | None:
    """The child conversation id from a ``sys_session_send`` tool_result.
    The tool returns a JSON handle
    ``{task_id, kind, agent, title, conversation_id, status, message}`` —
    verified in ``omnigent/tools/builtins/spawn.py``."""
    payload: Any = result_data
    if isinstance(payload, dict):
        # Some engines wrap the tool string under `output` / `result` /
        # `content`; probe those before falling through to a direct read.
        for key in ("output", "result", "content", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                payload = value
                break
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if not isinstance(payload, dict):
        return None
    conv_id = payload.get("conversation_id")
    return conv_id if isinstance(conv_id, str) and conv_id else None


def correlate_dispatch(
    state: dict[str, Any], request_data: Any, result_data: Any,
) -> tuple[str, str] | None:
    """Correlate a ``sys_session_send`` (parent-side) with a known worktree.
    Returns (child_conv_id, branch) on success and registers the mapping;
    returns None on any miss so callers can decide whether to log or
    fallback. Called from ``_handle_tool_result`` when target is
    ``sys_session_send``."""
    registry = state.get("worktree_registry")
    if not isinstance(registry, dict) or not registry:
        return None
    child_conv_id = _extract_child_conv_id(result_data)
    if not child_conv_id:
        return None
    input_text = _extract_input_text(request_data)
    if not input_text:
        return None
    # First-match wins. Polly's fanout skill puts one worktree per
    # dispatch, so ambiguity is rare; when it arises the deterministic
    # iteration order (insertion order per dict semantics) yields the
    # earliest-registered worktree, which the LLM typically names first.
    for path, branch in registry.items():
        if path and path in input_text:
            _register_child_branch(child_conv_id, branch)
            return child_conv_id, branch
    return None


# --- Testing hooks ------------------------------------------------------------


def _clear_for_tests() -> None:
    _CHILD_BRANCH_MAP.clear()
