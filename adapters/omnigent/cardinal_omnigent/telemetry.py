"""Observe-only Cardinal telemetry policy for omnigent.

Phase → Cardinal event mapping (spec docs/specs/omnigent-adapter.md):

  request      → cardinal.git_state       initiative facts from the labels
                                           convention (cardinal.repo /
                                           cardinal.branch), enriched by
                                           branch-change sniffing (below);
                                           user-turn boundary
  llm_response → api_request +
                 cardinal.turn_usage      PER-TURN granularity on the
                                           claude_sdk executor (verified:
                                           it evaluates PHASE_LLM_RESPONSE
                                           once per turn with the turn's
                                           ResultMessage usage — a
                                           documented divergence from the
                                           CLI adapters' per-model-call
                                           api_request); engines that fire
                                           per round-trip get per-call
                                           events for free. cost_usd from
                                           the delta of the session's
                                           cumulative context.usage
                                           .total_cost_usd (anchored via
                                           omnigent state_updates with an
                                           in-process mirror), falling
                                           back to core pricing tables
  tool_call    → cardinal.turn_tool       core bashclass on shell tools;
                                           tool_name from resolved
                                           event.target; shell commands
                                           are additionally sniffed for
                                           branch changes (git checkout
                                           -b / git switch -c/-C) so
                                           mid-session initiative moves
                                           are attributed — labels stay
                                           the primary channel
  tool_result  → tool_result
  response     → cardinal.subagent_usage  ONLY when the conversation's
                                           labels mark it as a sub-agent
                                           child (see SUBAGENT DETECTION)

SESSION IDENTITY: omnigent's contract carries no session id — see
_identity.py. Every handler threads the minted id's pending
state_updates into an ALLOW return so it persists in the engine's
per-conversation session_state.

SUBAGENT DETECTION (per-engine mapping, resolved from the omnigent
sources at the pinned commit):
  - claude-native / codex-native UI children carry
    `omnigent.wrapper` = "*-native-ui-subagent" plus id labels
    (omnigent.claude_native.subagent_id,
    omnigent.codex_native.subagent_thread_id / parent_thread_id /
    agent_nickname / agent_role). NOTE these mirror rows may never
    dispatch policies (the harness runs the child in-process, activity
    rides the parent) — the handler is correct if they do, silent if
    they don't.
  - native workflow / custom-YAML sub-agents run their own engine and
    DO dispatch policies, but omnigent stamps no distinguishing label;
    the Cardinal convention is to set `cardinal.subagent: <name>` in
    the sub-agent spec's guardrails labels (documented in README /
    connect flow). Upstream surfacing of conv.sub_agent_name in the
    event context is the clean fix (tracked as the EvaluationContext
    upstream PR).
Emitted usage is the child conversation's CUMULATIVE context.usage —
marked usage_scope="session_cumulative" so downstream folds take
last-per-session, not a sum.

Failure policy: fail-open. This policy observes; it must never block a
request because telemetry broke. Every entry point catches internal
errors and returns None (abstain → ALLOW). Non-None returns are ALLOWs
carrying `state_updates` (session-id mint, cost-delta anchor) —
state_updates are applied on ALLOW/DENY, withheld on ASK/abstain.

Identity is per-event (core 0.2.0 identity-as-argument): user.email
comes from event.context.actor.run_as; `cardinal.omnigent_harness` from
event.context.harness so downstream can slice by underlying harness.
Connection facts come from the policy `config` block (endpoint + key are
configured server-side by the org admin — see connect.py).
"""

from __future__ import annotations

import re
import time
from typing import Any

from cardinal_core import bashclass, initiative, otlp, pricing

from . import _events, _identity, _subagent

SCOPE_NAME = "cardinal-omnigent-policy"
# Single source of truth is the package __init__; imported lazily in
# _plugin_version() to dodge the circular import during package init.
_PLUGIN_VERSION_FALLBACK = "0.3.0"

# Session-state key anchoring the cost delta between llm_response events.
COST_ANCHOR_KEY = "cardinal.last_total_cost_usd"

# Tool names that carry a shell command (bash classification applies).
SHELL_TOOLS = frozenset({
    "bash", "Bash", "shell", "run_shell_command", "execute_command",
    "exec_command", "local_shell",
})

# --- Subagent label vocabulary (verified in omnigent server/routes/
# sessions.py at the pinned commit) plus the Cardinal spec-stamp
# convention for native workflow children.
WRAPPER_LABEL = "omnigent.wrapper"
CARDINAL_SUBAGENT_LABEL = "cardinal.subagent"
CLAUDE_SUBAGENT_ID_LABEL = "omnigent.claude_native.subagent_id"
CODEX_THREAD_ID_LABEL = "omnigent.codex_native.subagent_thread_id"
CODEX_PARENT_THREAD_ID_LABEL = "omnigent.codex_native.parent_thread_id"
CODEX_NICKNAME_LABEL = "omnigent.codex_native.agent_nickname"
CODEX_ROLE_LABEL = "omnigent.codex_native.agent_role"
CODEX_PROMPT_LABEL = "omnigent.codex_native.prompt"

# Branch creation/switch commands — deliberately conservative: creation
# flags and `git switch` (which only takes branches). Plain `git
# checkout <ref>` is ambiguous (files, tags, SHAs) and is not sniffed.
_BRANCH_RE = re.compile(
    r"\bgit\s+(?:checkout\s+-[bB]|switch\s+(?:-[cC]\s+)?)"
    r"\s*(?P<branch>[A-Za-z0-9][A-Za-z0-9._/-]*)"
)

# In-process per-session mirror: sequence counters + cost anchor +
# sniffed branch. Policies run in the long-lived omnigent server
# process, so module state is a valid cache; omnigent session_state
# (via state_updates) is the durable copy where the engine supports it.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSIONS_MAX = 4096


def _plugin_version() -> str:
    try:
        from . import PLUGIN_VERSION
        return PLUGIN_VERSION
    except Exception:
        return _PLUGIN_VERSION_FALLBACK


def _session_counters(session_id: str) -> dict[str, Any]:
    state = _SESSIONS.get(session_id)
    if state is None:
        if len(_SESSIONS) >= _SESSIONS_MAX:
            _SESSIONS.pop(next(iter(_SESSIONS)))
        state = {"user_turn_seq": 0, "turn_seq": 0, "tool_seq": 0}
        _SESSIONS[session_id] = state
    return state


def connection_from_config(config: dict[str, Any] | None) -> otlp.IngestConnection | None:
    """IngestConnection from the policy's `config:` block. None (emit
    becomes a no-op) when the block is absent or incomplete."""
    if not isinstance(config, dict):
        return None
    endpoint = config.get("ingest_endpoint")
    api_key = config.get("ingest_api_key")
    if not endpoint or not api_key:
        return None
    return otlp.IngestConnection(
        endpoint=str(endpoint).rstrip("/"),
        api_key=str(api_key),
        api_header=str(config.get("ingest_api_header") or otlp.DEFAULT_API_HEADER),
    )


def _resource(event: Any, config: dict[str, Any]) -> dict[str, str]:
    return otlp.resource_attrs(
        service_name=str(config.get("service_name") or "omnigent"),
        agent_runtime="omnigent",
        deployment_environment=config.get("deployment_environment"),
        user_email=_events.actor_email(event),
        org=config.get("org"),
        plugin_version=_plugin_version(),
        extra={"cardinal.omnigent_harness": _events.harness(event) or ""},
    )


def _emit(event: Any, config: dict[str, Any], records: list[dict[str, Any]]) -> None:
    if not records:
        return
    otlp.emit_records(
        records,
        connection_from_config(config),
        _resource(event, config),
        scope_name=SCOPE_NAME,
        scope_version=_plugin_version(),
    )


def _label_str(labels: dict[str, Any], key: str) -> str | None:
    value = labels.get(key)
    return str(value) if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# request → cardinal.git_state (labels convention + sniffed branch)
# ---------------------------------------------------------------------------

def _git_state_record(
    event: Any, session_id: str, branch: str | None, *,
    branch_source: str | None = None, prompt: Any = None, ts_ns: int,
) -> dict[str, Any]:
    labels = _events.labels(event)
    repo = _label_str(labels, "cardinal.repo")
    initiative_name, initiative_type = initiative.resolve_initiative(branch)
    attrs: dict[str, Any] = {
        "session_id": session_id,
        "cardinal_repo": repo,
        "cardinal_branch": branch,
        "cardinal_initiative_name": initiative_name,
        "cardinal_initiative_type": initiative_type,
        "cardinal_command": initiative.detect_command(prompt),
    }
    # Sub-agent marker attributes. When this session is a polly-dispatched
    # child (Conversation.kind == "sub_agent" per the omnigent store), stamp
    # the identity so downstream can distinguish parent-cwd-inherited
    # attribution from top-level session attribution. Correlation, when it
    # succeeded, already set branch_source to "correlated_from_parent_dispatch"
    # (trustworthy); a fallback here means the branch value is polly's own
    # branch that leaked through the child's cwd-derived label.
    identity = _subagent.lookup_subagent_identity(_subagent.current_conv_id())
    if identity is not None:
        attrs["cardinal_sub_agent"] = True
        attrs["cardinal_parent_conversation_id"] = identity.parent_conversation_id
        if identity.sub_agent_name:
            attrs["cardinal_sub_agent_name"] = identity.sub_agent_name
        if branch_source is None and branch:
            branch_source = "inherited_from_parent_cwd"
    if branch_source:
        attrs["cardinal_branch_source"] = branch_source
    return otlp.log_record("cardinal.git_state", attrs, ts_ns)


def _resolve_branch(event: Any, state: dict[str, Any]) -> tuple[str | None, str | None]:
    """(branch, source). Resolution order:

    1. Parent-side dispatch correlation — polly-orchestrated children have
       their real worktree branch registered when the parent's
       ``sys_session_send`` tool_result fired. This overrides labels because
       upstream (`sys_session_send` has no `workspace` field) means the
       child's `cardinal.branch` label was stamped by git-state.py running
       in polly's cwd — i.e. it's polly's branch, not the child's.
    2. Labels convention (``cardinal.branch`` on the child's session labels)
       — the primary channel for top-level sessions.
    3. In-session sniff — a `git checkout -b` / `git switch -c` in a shell
       tool_call, for sessions whose creation-time labels predate a
       mid-session branch move.
    """
    correlated = _subagent.child_correlated_branch(_subagent.current_conv_id())
    if correlated:
        return correlated, "correlated_from_parent_dispatch"
    branch = _label_str(_events.labels(event), "cardinal.branch")
    if branch:
        return branch, None
    sniffed = state.get("sniffed_branch")
    if isinstance(sniffed, str) and sniffed:
        return sniffed, "tool_sniff"
    return None, None


def _handle_request(event: Any, config: dict[str, Any], session_id: str) -> None:
    state = _session_counters(session_id)
    state["user_turn_seq"] = int(state.get("user_turn_seq") or 0) + 1
    state["turn_seq"] = 0
    state["tool_seq"] = 0

    # Policies never see the workspace (no cwd/repo/branch in omnigent's
    # contract) — initiative facts ride the labels convention. Unlabeled
    # sessions attribute to initiative=None, type=research, same as
    # protected-branch sessions on the CLI adapters, so rollups stay honest.
    branch, source = _resolve_branch(event, state)
    data = _events.data(event)
    prompt = data.get("prompt") or data.get("message") or data.get("content")
    if not isinstance(prompt, str):
        content = _events.get(event, "data")
        prompt = content if isinstance(content, str) else None

    _emit(event, config, [_git_state_record(
        event, session_id, branch, branch_source=source,
        prompt=prompt, ts_ns=time.time_ns(),
    )])


# ---------------------------------------------------------------------------
# llm_response → api_request + cardinal.turn_usage (per-turn)
# ---------------------------------------------------------------------------

def _normalize_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Map omnigent's llm_response usage payload onto the Cardinal
    contract's canonical bucket names, probing the cache-bucket
    spellings the claude_sdk executor and generic engines produce."""
    def _int(*keys: str) -> int:
        for k in keys:
            v = _events.as_int(raw.get(k))
            if v is not None:
                return v
        return 0

    return {
        "input_tokens": _int("input_tokens", "prompt_tokens"),
        "output_tokens": _int("output_tokens", "completion_tokens"),
        "total_tokens": _int("total_tokens"),
        "context_tokens": _int("context_tokens"),
        "cached_input_tokens": _int(
            "cached_input_tokens", "cache_read_input_tokens", "cache_read_tokens",
        ),
        "cache_creation_tokens": _int(
            "cache_creation_tokens", "cache_creation_input_tokens",
        ),
    }


def _fallback_cost(model: str | None, usage: dict[str, Any]) -> float | None:
    """Core pricing tables keyed on the model — used when the session's
    cumulative total_cost_usd delta is unavailable. omnigent model values
    may carry a serving-platform prefix (e.g. "databricks-claude-opus-4-8"),
    so on a miss we retry from the embedded "claude-" SKU. Unpriced models
    return None and the attribute is skipped — no misleading zero rows."""
    candidates = [model]
    if model and "claude-" in model and not model.startswith("claude-"):
        candidates.append(model[model.index("claude-"):])
    for candidate in candidates:
        for table in pricing.PROVIDER_TABLES.values():
            cost = pricing.compute_cost_usd(candidate, usage, table)
            if cost is not None:
                return cost
    return None


def _cost_delta(event: Any, state: dict[str, Any]) -> tuple[float | None, float | None]:
    """(cost_usd for this turn, current cumulative total) from the
    server-maintained context.usage.total_cost_usd. The previous total is
    read from omnigent session_state (durable, written back via
    state_updates) with the in-process mirror as fallback."""
    total = _events.as_float(_events.session_usage(event).get("total_cost_usd"))
    if total is None:
        return None, None
    last = _events.as_float(_events.session_state(event).get(COST_ANCHOR_KEY))
    if last is None:
        last = _events.as_float(state.get(COST_ANCHOR_KEY))
    if last is None:
        # First llm_response of the session: the cumulative total IS the cost.
        return (total if total > 0 else None), total
    delta = total - last
    return (round(delta, 6) if delta > 0 else None), total


def _handle_llm_response(
    event: Any, config: dict[str, Any], session_id: str
) -> list[dict[str, Any]] | None:
    """Returns extra state_updates to attach (cost anchor), or None."""
    data = _events.data(event)
    raw_usage = data.get("usage")
    if not isinstance(raw_usage, dict):
        return None
    usage = _normalize_usage(raw_usage)
    if not any(usage.values()):
        return None

    # data.usage.model (claude_sdk stamps it), else data.model, else the
    # engine-injected active session model from the context.
    model = raw_usage.get("model") or data.get("model")
    model = str(model) if model else _events.model(event)
    state = _session_counters(session_id)
    ts_ns = time.time_ns()

    cost_usd, current_total = _cost_delta(event, state)
    if cost_usd is None:
        cost_usd = _fallback_cost(model, usage)

    base: dict[str, Any] = {
        "session_id": session_id,
        "user_email": _events.actor_email(event),
        "agent_runtime": "omnigent",
        "model": model,
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"] or None,
        "context_tokens": usage["context_tokens"] or None,
        "cache_read_tokens": usage["cached_input_tokens"],
        "cache_read_input_tokens": usage["cached_input_tokens"],
        "cache_creation_tokens": usage["cache_creation_tokens"] or None,
    }
    if cost_usd is not None:
        base["cost_usd"] = cost_usd

    records = [
        otlp.log_record("api_request", base, ts_ns),
        otlp.log_record("cardinal.turn_usage", {
            **base,
            "ts": ts_ns,
            "user_turn_seq": state["user_turn_seq"],
            "turn_seq": state["turn_seq"],
            # Granularity marker — per-turn on the claude_sdk executor
            # (verified), per-call on engines that fire per round-trip.
            "usage_granularity": "turn",
        }, ts_ns + 1),
    ]
    _emit(event, config, records)

    state["turn_seq"] = int(state.get("turn_seq") or 0) + 1
    state["tool_seq"] = 0
    if current_total is None:
        return None
    state[COST_ANCHOR_KEY] = current_total
    return [{"action": "set", "key": COST_ANCHOR_KEY, "value": current_total}]


# ---------------------------------------------------------------------------
# tool_call → cardinal.turn_tool · tool_result → tool_result
# ---------------------------------------------------------------------------

def _tool_arguments(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("arguments", "input", "tool_input", "parameters"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return data


def _shell_command(data: dict[str, Any]) -> str:
    args = _tool_arguments(data)
    cmd = args.get("command") or args.get("cmd")
    return cmd if isinstance(cmd, str) else ""


def _split_mcp(name: str) -> tuple[str, str] | None:
    """'mcp__server__tool' → (server, tool); None for non-MCP names."""
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__")
    server = parts[1] if len(parts) > 1 else ""
    tool = parts[2] if len(parts) > 2 else name
    return server, tool


def _sniff_branch(
    event: Any, config: dict[str, Any], session_id: str,
    state: dict[str, Any], command: str,
) -> None:
    """Branch-change enrichment. Handles two kinds of shell command:

    - ``git checkout -b`` / ``git switch -c/-C`` — marks an initiative
      boundary the creation-time labels can't see. Emits a fresh
      ``cardinal.git_state`` at the boundary and caches the branch for
      the labels-absent fallback path in :func:`_resolve_branch`.
    - ``git worktree add <path> -b <branch>`` — polly's fanout skill
      creates a per-task worktree BEFORE dispatching each sub-agent.
      Recorded in the parent's ``worktree_registry``; consumed by
      ``_subagent.correlate_dispatch`` on the paired
      ``sys_session_send`` tool_result to override the child's polluted
      ``cardinal.branch`` label. No boundary emission for the parent —
      polly hasn't moved off its own branch.
    """
    worktree = _subagent.parse_worktree_add(command)
    if worktree is not None:
        _subagent.register_worktree(state, worktree[0], worktree[1])
        return
    match = _BRANCH_RE.search(command)
    if match is None:
        return
    branch = match.group("branch")
    if branch == state.get("sniffed_branch"):
        return
    state["sniffed_branch"] = branch
    if _label_str(_events.labels(event), "cardinal.branch"):
        return  # labels are authoritative; sniff kept only as fallback
    _emit(event, config, [_git_state_record(
        event, session_id, branch, branch_source="tool_sniff",
        ts_ns=time.time_ns(),
    )])


def _handle_tool_call(event: Any, config: dict[str, Any], session_id: str) -> None:
    tool_name = _events.target(event)
    if not tool_name:
        return
    state = _session_counters(session_id)
    ts_ns = time.time_ns()

    attrs: dict[str, Any] = {
        "session_id": session_id,
        "ts": ts_ns,
        "user_turn_seq": state["user_turn_seq"],
        "turn_seq": state["turn_seq"],
        "tool_seq": state["tool_seq"],
        # turn_tool carries the raw (possibly MCP-qualified) name — the
        # harvester's clustering signal, matching the CLI adapters.
        "tool_name": tool_name,
    }
    mcp = _split_mcp(tool_name)
    if mcp is not None:
        attrs["mcp_server_name"], attrs["mcp_tool_name"] = mcp
    elif tool_name in SHELL_TOOLS:
        command = _shell_command(_events.data(event))
        classified = bashclass.classify_bash_command(command)
        if classified is not None:
            bash_class, bash_multi = classified
            attrs["bash_class"] = bash_class
            if bash_multi:
                attrs["bash_multi"] = True
        _sniff_branch(event, config, session_id, state, command)

    _emit(event, config, [otlp.log_record("cardinal.turn_tool", attrs, ts_ns)])
    state["tool_seq"] = int(state.get("tool_seq") or 0) + 1


def _handle_tool_result(event: Any, config: dict[str, Any], session_id: str) -> None:
    tool_name = _events.target(event)
    if not tool_name:
        return
    data = _events.data(event)

    if tool_name == "sys_session_send":
        # Parent-side dispatch correlation: extract the child's conversation_id
        # from the tool result and match the dispatch input text against
        # worktree adds this parent session made earlier. On a match, the
        # child's real branch is registered so its own events attribute
        # correctly instead of inheriting polly's cwd-derived label. See
        # _subagent module docstring for the full mechanism and why this
        # cannot be a purely observational lookup.
        state = _session_counters(session_id)
        request_data = _events.get(event, "request_data")
        _subagent.correlate_dispatch(state, request_data, data)

    success = data.get("success")
    if success is None:
        exit_code = _events.as_int(data.get("exit_code"))
        if exit_code is not None:
            success = exit_code == 0
        else:
            status = data.get("status")
            if isinstance(status, str):
                success = status.lower() in {"ok", "success", "completed"}
    if isinstance(success, bool):
        success_str = "true" if success else "false"
    elif isinstance(success, str):
        success_str = success.lower()
    else:
        success_str = "true"

    # tool_result keeps the normalized name for MCP tools (turn_tool keeps
    # the raw qualified form) — same convention as the CLI adapters.
    normalized = "mcp_tool" if _split_mcp(tool_name) is not None else tool_name
    attrs: dict[str, Any] = {
        "session_id": session_id,
        "agent_runtime": "omnigent",
        "tool_name": normalized,
        "success": success_str,
    }
    _emit(event, config, [otlp.log_record("tool_result", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# response → cardinal.subagent_usage (sub-agent child conversations only)
# ---------------------------------------------------------------------------

def _subagent_description(labels: dict[str, Any]) -> str | None:
    """Best identifying copy for the child, in preference order: the
    Cardinal spec-stamp, codex nickname/role, the dispatch prompt, the
    wrapper marker itself."""
    for key in (CARDINAL_SUBAGENT_LABEL, CODEX_NICKNAME_LABEL,
                CODEX_ROLE_LABEL, CODEX_PROMPT_LABEL, WRAPPER_LABEL):
        value = _label_str(labels, key)
        if value:
            return value
    return None


def _handle_response(event: Any, config: dict[str, Any], session_id: str) -> None:
    labels = _events.labels(event)
    is_subagent = bool(
        _label_str(labels, CARDINAL_SUBAGENT_LABEL)
        or _label_str(labels, CLAUDE_SUBAGENT_ID_LABEL)
        or _label_str(labels, CODEX_THREAD_ID_LABEL)
        or (_label_str(labels, WRAPPER_LABEL) or "").endswith("subagent")
    )
    if not is_subagent:
        return

    usage = _events.session_usage(event)
    attrs: dict[str, Any] = {
        "session_id": session_id,
        "agent_runtime": "omnigent",
        "subagent_description": _subagent_description(labels) or "omnigent-subagent",
        # Engine-injected active model (memory: model powers latent-
        # subagent mining downstream).
        "model": _events.model(event),
        "input_tokens": _events.as_int(usage.get("input_tokens")),
        "output_tokens": _events.as_int(usage.get("output_tokens")),
        "total_tokens": _events.as_int(usage.get("total_tokens")),
        "cost_usd": _events.as_float(usage.get("total_cost_usd")),
        # Cumulative for the child conversation — downstream must take
        # last-per-session, not sum (multi-turn children re-emit).
        "usage_scope": "session_cumulative",
        "subagent_id": _label_str(labels, CLAUDE_SUBAGENT_ID_LABEL)
        or _label_str(labels, CODEX_THREAD_ID_LABEL),
        "parent_thread_id": _label_str(labels, CODEX_PARENT_THREAD_ID_LABEL),
    }
    _emit(event, config, [otlp.log_record(
        "cardinal.subagent_usage", attrs, time.time_ns())])


# ---------------------------------------------------------------------------
# policy entry points
# ---------------------------------------------------------------------------

_HANDLED_PHASES = frozenset(
    {"request", "llm_response", "tool_call", "tool_result", "response"}
)


def telemetry_policy(event: Any, config: Any = None) -> dict[str, Any] | None:
    """Observe-only Cardinal telemetry. Fail-open: any internal error is
    swallowed and the policy abstains (None → the engine treats it as no
    opinion). Never blocks, never asks; ALLOW returns exist only to
    carry state_updates (session-id mint, cost anchor)."""
    try:
        cfg = config if isinstance(config, dict) else {}
        phase = _events.phase(event)
        if phase not in _HANDLED_PHASES:
            return None
        session_id, updates = _identity.resolve(event)
        if not session_id:
            return None

        if phase == "request":
            _handle_request(event, cfg, session_id)
        elif phase == "llm_response":
            anchor = _handle_llm_response(event, cfg, session_id)
            if anchor:
                updates = [*updates, *anchor]
        elif phase == "tool_call":
            _handle_tool_call(event, cfg, session_id)
        elif phase == "tool_result":
            _handle_tool_result(event, cfg, session_id)
        elif phase == "response":
            _handle_response(event, cfg, session_id)

        if updates:
            # ALLOW (not abstain) so the engine applies the updates —
            # omnigent cannot apply state_updates on None.
            return {"result": "ALLOW", "state_updates": updates}
        return None
    except Exception:
        return None


def make_telemetry_policy(config: dict[str, Any] | None = None):
    """Factory form for omnigent's `function: {path, arguments}`
    registration — closes over the config block."""
    def policy(event: Any) -> dict[str, Any] | None:
        return telemetry_policy(event, config)
    return policy
