#!/usr/bin/env python3
"""Phase 1 Claude vertical slice — transcript -> Envelope records.

Walks a Claude Code session transcript (and any subagent sub-transcripts
it references) end-to-end and yields `Envelope` records against the
schema-v1 canonical execution graph contract (`cardinal_core.envelope`,
`docs/canonical-model.md`).

NOT wired as a hook yet (`hooks.json` unchanged — wiring is a follow-up).
Opens NO network connection — HTTP POST to lakerunner's `/v1/envelopes`
is a separate follow-up (see
docs/local-notes/plans/agent-execution-graph.md, Phase 1). Call
`emit_from_transcript()` directly; the caller is responsible for
transport.

Empirical findings this module encodes (see the P1 report for the full
writeup — every claim below was verified against real transcripts under
~/.claude/projects/, not assumed from the design doc):

- `parent_tool_use_id` does NOT appear anywhere as a structural field on
  `tool_use` blocks in any captured transcript. The only "parent" signal
  available is POSITION in the transcript (which assistant message's
  `content` array a tool_use sits in) — hence every invocation node below
  gets `parent_source=TRANSCRIPT`, never `NATIVE`. This resolves the
  Phase 0 open question ("Claude parent_tool_use_id presence on Task
  calls") empirically: it is absent.
- Claude Code's subagent-spawn tool is named `"Agent"` in every captured
  transcript, not `"Task"` — `SUBAGENT_TOOL_NAMES` accepts both so a
  future/older Claude Code build using `"Task"` still resolves correctly.
- Claude Code's skill mechanism is a *distinct* `tool_use` named
  `"Skill"` with `input={"skill": <name>, "args": <free text>}` — NOT a
  `"SlashCommand"` tool_use (no such block was found in any captured
  transcript). `SlashCommand` is kept as a defensive/forward-compat
  alias per the task brief, but it is unverified against real data.
- The static, on-disk transcript's `tool_result` for an `Agent`/`Task`
  call carries free-text content (the subagent's final report), NOT a
  structured `agentId` — that field only exists in the *live*
  PostToolUse hook payload (see `subagent-usage.py`), not in the
  transcript file. The reliable, transcript-native way to resolve a
  subagent invocation to its own sub-transcript is
  `<transcript_dir>/subagents/agent-<id>.meta.json`'s `toolUseId` field,
  which is written by the harness and points straight back at the
  spawning `tool_use.id`. This module uses that, not `tool_result`.
- `message.model` is present on every assistant message in every
  captured transcript — reliable, native, one per llm_call.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_core import envelope as env  # noqa: E402
from cardinal_core import initiative  # noqa: E402
from cardinal_core.otlp import parse_ts_ns  # noqa: E402
from cardinal_core.redaction import redact_file_path, redact_tool_args  # noqa: E402

# ---------------------------------------------------------------------------
# Tool-name classification (see module docstring for the empirical basis)
# ---------------------------------------------------------------------------

SUBAGENT_TOOL_NAMES = frozenset({"Agent", "Task"})
SKILL_TOOL_NAMES = frozenset({"Skill", "SlashCommand"})

# Tools whose invocation mutates a file, independent of the tool node
# itself (canonical-model.md EventKind.FILE_MUTATION). Read is
# deliberately excluded — it observes, it doesn't mutate.
FILE_MUTATION_TOOL_NAMES = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})

# Allowlisted file-path-shaped input keys per tool (privacy boundary —
# matches turn-usage.py's TARGET_KEYS exactly; membership IS the
# allowlist, nothing else is read from `input` for this purpose).
_TARGET_KEYS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def _extract_target(tool_name: str, tool_input: Any) -> str | None:
    key = _TARGET_KEYS.get(tool_name)
    if key is None or not isinstance(tool_input, dict):
        return None
    path = tool_input.get(key)
    return path if isinstance(path, str) and path else None


# ---------------------------------------------------------------------------
# Identity — local, non-secret key derivation.
#
# docs/canonical-model.md §7 defines execution_key/node_key as HMACs
# keyed by a secret (`cardinal_execution_key`) that only ingest holds —
# adapters never mint or persist execution_id. Since the payload
# dataclasses (NodeObserved et al.) still require a non-empty
# execution_key/node_key string, this module computes a LOCAL,
# non-secret, deterministic digest instead of the real HMAC. It is
# stable across repeated emissions of the same session (so repeated
# Stop-hook firings key the same real-world node identically — required
# for ingest-side dedup/replace-on-observation), but it is NOT the
# authoritative identity. Ingest is expected to resolve the canonical
# execution_key from the envelope WRAPPER's (org_id, adapter,
# session_id) using its own secret; the payload-level value here only
# has to keep node/edge cross-references internally consistent within
# one adapter emission. This is a deliberate, documented scoping choice,
# not a provenance lie — execution_key/node_key are identity fields, not
# one of the six provenance axes.
# ---------------------------------------------------------------------------


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8", "surrogateescape")).hexdigest()


def _execution_key(org_id: str, adapter: str, session_id: str) -> str:
    return _digest("execution_key", org_id, adapter, session_id)


def _node_key(execution_key: str, node_kind: env.NodeKind, native_seed: str) -> str:
    return _digest("node_key", execution_key, node_kind.value, native_seed)


def _toolkit_key(execution_key: str, toolkit_type: env.ToolkitType, toolkit_name: str) -> str:
    """Stable synthetic target for `used_toolkit` edges. NOT a node that
    gets its own NodeObserved record in Phase 1 — no graph node
    represents "the toolkit" as a standalone entity yet, only the
    invocation that used it. Kept as a documented simplification (see
    the P1 report): the edge's own `attributes` carry
    toolkit_type/toolkit_name redundantly so a consumer never has to
    dereference this key to know what it means."""
    return _digest("toolkit", execution_key, toolkit_type.value, toolkit_name)


def _json_safe(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _record_id(record_type: env.RecordType, payload_kwargs: dict[str, Any]) -> str:
    safe = _json_safe(payload_kwargs)
    safe["__record_type__"] = record_type.value
    return env.record_id_for(safe)


# ---------------------------------------------------------------------------
# Transcript reading (best-effort, matches the existing hooks' style —
# see turn-usage.py/subagent-usage.py: malformed lines are skipped, a
# missing/unreadable file yields an empty transcript rather than raising)
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except (OSError, UnicodeDecodeError):
        return []
    return out


def _is_real_user_message(msg: dict) -> bool:
    """Same rule as turn-usage.py: a 'real' user message (typed prompt,
    not a tool_result-only continuation) marks a turn boundary."""
    if msg.get("role") != "user":
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") != "tool_result":
                return True
        return False
    return False


def _discover_subagent_transcripts(transcript_path: Path) -> dict[str, Path]:
    """tool_use_id -> subagent transcript path, via
    subagents/agent-<id>.meta.json's `toolUseId` field (see module
    docstring: this is the reliable static-transcript link, NOT
    tool_result.agentId, which only exists in the live hook payload).

    Layout (matches subagent-usage.py's `sub = Path(transcript_path[:
    -len(".jsonl")]) / "subagents" / ...` exactly): the subagents/ dir
    sits under a directory named after the transcript's OWN stem
    (typically the session_id), sibling to the .jsonl file itself --
    NOT a direct child of transcript_path.parent."""
    mapping: dict[str, Path] = {}
    name = transcript_path.name
    stem = name[: -len(".jsonl")] if name.endswith(".jsonl") else transcript_path.stem
    sub_dir = transcript_path.parent / stem / "subagents"
    if not sub_dir.is_dir():
        return mapping
    try:
        meta_paths = sorted(sub_dir.glob("*.meta.json"))
    except OSError:
        return mapping
    for meta_path in meta_paths:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict):
            continue
        tool_use_id = meta.get("toolUseId")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            continue
        name = meta_path.name
        if not name.endswith(".meta.json"):
            continue
        stem = name[: -len(".meta.json")]
        agent_jsonl = meta_path.parent / f"{stem}.jsonl"
        if agent_jsonl.is_file():
            mapping[tool_use_id] = agent_jsonl
    return mapping


# ---------------------------------------------------------------------------
# Emitter — imperative builder. Generator-of-generators for a tree walk
# is more trouble than it's worth here (need both "yield an envelope"
# AND "return the node_key I just created" at every step); a plain list
# collector with a small typed API is far easier to get right, and the
# public function below still returns/streams an Iterable[Envelope].
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    org_id: str
    adapter: str
    session_id: str
    execution_key: str
    now_ns: int


class _Emitter:
    def __init__(self, ctx: _Ctx) -> None:
        self.ctx = ctx
        self.records: list[env.Envelope] = []
        self._fallback_seq = 0

    def _wrap(self, record_type: env.RecordType, payload: Any, payload_kwargs: dict, effective_ns: int | None) -> None:
        record_id = _record_id(record_type, payload_kwargs)
        envelope = env.Envelope(
            schema_version=env.SCHEMA_VERSION,
            org_id=self.ctx.org_id,
            adapter=self.ctx.adapter,
            session_id=self.ctx.session_id,
            record_id=record_id,
            record_type=record_type,
            observed_ns=self.ctx.now_ns,
            effective_ns=effective_ns if effective_ns is not None else self.ctx.now_ns,
            payload=payload,
        )
        self.records.append(envelope)

    def next_fallback_seq(self) -> int:
        self._fallback_seq += 1
        return self._fallback_seq

    def node(
        self,
        *,
        node_kind: env.NodeKind,
        node_name: str,
        native_seed: str,
        identity_source: env.IdentitySource,
        parent_source: env.ParentSource,
        timing_source: env.TimingSource,
        model_source: env.ModelSource,
        toolkit_source: env.ToolkitSource,
        usage_source: env.UsageSource,
        invocation_kind: env.InvocationKind | None = None,
        tool_kind: env.ToolKind | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
        orchestrator_model: str | None = None,
        request_model: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> str:
        node_key = _node_key(self.ctx.execution_key, node_kind, native_seed)
        kwargs = dict(
            execution_key=self.ctx.execution_key,
            node_key=node_key,
            node_kind=node_kind,
            node_name=node_name,
            identity_source=identity_source,
            parent_source=parent_source,
            timing_source=timing_source,
            model_source=model_source,
            toolkit_source=toolkit_source,
            usage_source=usage_source,
            invocation_kind=invocation_kind,
            tool_kind=tool_kind,
            start_ns=start_ns,
            end_ns=end_ns,
            orchestrator_model=orchestrator_model,
            request_model=request_model,
            attributes=attributes or {},
        )
        payload = env.NodeObserved(**kwargs)
        self._wrap(env.RecordType.NODE_OBSERVED, payload, kwargs, start_ns)
        return node_key

    def edge(
        self,
        source_node_key: str,
        target_node_key: str,
        edge_kind: env.EdgeKind,
        *,
        attributes: dict[str, Any] | None = None,
        effective_ns: int | None = None,
    ) -> None:
        kwargs = dict(
            execution_key=self.ctx.execution_key,
            source_node_key=source_node_key,
            target_node_key=target_node_key,
            edge_kind=edge_kind,
            attributes=attributes or {},
        )
        payload = env.EdgeObserved(**kwargs)
        self._wrap(env.RecordType.EDGE_OBSERVED, payload, kwargs, effective_ns)

    def event(
        self,
        event_kind: env.EventKind,
        event_ns: int,
        *,
        related_node_key: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        kwargs = dict(
            execution_key=self.ctx.execution_key,
            event_kind=event_kind,
            event_ns=event_ns,
            related_node_key=related_node_key,
            attributes=attributes or {},
        )
        payload = env.ExecutionEvent(**kwargs)
        self._wrap(env.RecordType.EXECUTION_EVENT, payload, kwargs, event_ns)

    def usage(
        self,
        node_key: str,
        *,
        input_tokens: int,
        output_tokens: int,
        usage_source: env.UsageSource,
        cached_tokens: int | None = None,
        cost_usd: float | None = None,
        effective_ns: int | None = None,
    ) -> None:
        kwargs = dict(
            execution_key=self.ctx.execution_key,
            node_key=node_key,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_source=usage_source,
            cached_tokens=cached_tokens,
            cost_usd=cost_usd,
        )
        payload = env.UsageObserved(**kwargs)
        self._wrap(env.RecordType.USAGE_OBSERVED, payload, kwargs, effective_ns)


def _parse_ts(ctx: _Ctx, raw: Any) -> int | None:
    if not raw:
        return None
    return parse_ts_ns(raw, ctx.now_ns)


# ---------------------------------------------------------------------------
# Transcript walk
# ---------------------------------------------------------------------------


def _index_tool_result_timestamps(records: list[dict]) -> dict[str, Any]:
    """tool_use_id -> raw (unparsed) timestamp of the record carrying its
    tool_result. Used to reconstruct a [start, end) span for invocation
    nodes by combining two independently-native timestamp readings —
    hence `timing_source=RECONSTRUCTED`, never `NATIVE` for these spans
    (see module docstring)."""
    idx: dict[str, Any] = {}
    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        ts_raw = rec.get("timestamp")
        if not ts_raw:
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tuid = block.get("tool_use_id")
                if isinstance(tuid, str) and tuid and tuid not in idx:
                    idx[tuid] = ts_raw
    return idx


def _first_cwd(records: list[dict]) -> str | None:
    for rec in records:
        cwd = rec.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _process_tool_use(
    emitter: _Emitter,
    block: dict,
    *,
    llm_call_key: str,
    orchestrator_model: str | None,
    start_ns: int | None,
    result_ts_index: dict[str, Any],
    cwd: str | None,
    subagent_map: dict[str, Path],
) -> None:
    name = block.get("name")
    tool_use_id = block.get("id")
    tool_input = block.get("input")
    if not isinstance(name, str) or not name:
        return
    if not isinstance(tool_use_id, str) or not tool_use_id:
        # No native id at all -- can't identify this invocation honestly;
        # skip rather than fabricate identity.
        return

    end_ns = None
    timing_source = env.TimingSource.MARKER if start_ns is not None else env.TimingSource.UNKNOWN
    result_ts_raw = result_ts_index.get(tool_use_id)
    if result_ts_raw is not None:
        end_ns = _parse_ts(emitter.ctx, result_ts_raw)
        if end_ns is not None and start_ns is not None:
            timing_source = env.TimingSource.RECONSTRUCTED

    model_source = env.ModelSource.INHERITED if orchestrator_model else env.ModelSource.UNKNOWN

    if name in SUBAGENT_TOOL_NAMES:
        subagent_type = None
        description = None
        if isinstance(tool_input, dict):
            v = tool_input.get("subagent_type")
            subagent_type = v if isinstance(v, str) and v else None
            d = tool_input.get("description")
            description = d[:160] if isinstance(d, str) and d else None
        node_name = subagent_type or "general-purpose"
        attrs: dict[str, Any] = {"subagent_type": node_name}
        if description:
            attrs["description"] = description
        node_key = emitter.node(
            node_kind=env.NodeKind.INVOCATION,
            invocation_kind=env.InvocationKind.SUBAGENT,
            node_name=node_name,
            native_seed=tool_use_id,
            start_ns=start_ns,
            end_ns=end_ns,
            orchestrator_model=orchestrator_model,
            identity_source=env.IdentitySource.NATIVE,
            parent_source=env.ParentSource.TRANSCRIPT,
            timing_source=timing_source,
            model_source=model_source,
            toolkit_source=env.ToolkitSource.NATIVE,
            usage_source=env.UsageSource.UNKNOWN,
            attributes=attrs,
        )
        emitter.edge(llm_call_key, node_key, env.EdgeKind.INVOKED, effective_ns=start_ns)
        toolkit_key = _toolkit_key(emitter.ctx.execution_key, env.ToolkitType.SUBAGENT, node_name)
        emitter.edge(
            node_key, toolkit_key, env.EdgeKind.USED_TOOLKIT,
            attributes={"toolkit_type": env.ToolkitType.SUBAGENT.value, "toolkit_name": node_name},
            effective_ns=start_ns,
        )
        sub_path = subagent_map.get(tool_use_id)
        if sub_path is not None:
            sub_records = _read_jsonl(sub_path)
            if sub_records:
                child_turn_key = _emit_subagent_child_turn(emitter, tool_use_id, sub_records)
                emitter.edge(node_key, child_turn_key, env.EdgeKind.DELEGATED_TO, effective_ns=start_ns)
                _walk_segment(emitter, sub_records, child_turn_key, subagent_map)
        return

    if name in SKILL_TOOL_NAMES:
        skill_name = None
        if isinstance(tool_input, dict):
            v = tool_input.get("skill") or tool_input.get("command")
            skill_name = v if isinstance(v, str) and v else None
        node_name = skill_name or name
        node_key = emitter.node(
            node_kind=env.NodeKind.INVOCATION,
            invocation_kind=env.InvocationKind.SKILL,
            node_name=node_name,
            native_seed=tool_use_id,
            start_ns=start_ns,
            end_ns=end_ns,
            orchestrator_model=orchestrator_model,
            identity_source=env.IdentitySource.NATIVE,
            parent_source=env.ParentSource.TRANSCRIPT,
            timing_source=timing_source,
            model_source=model_source,
            toolkit_source=env.ToolkitSource.NATIVE,
            usage_source=env.UsageSource.UNKNOWN,
            attributes={
                "skill_name": node_name,
                "lifecycle_state": env.SkillLifecycleState.EXECUTED.value,
            },
        )
        emitter.edge(llm_call_key, node_key, env.EdgeKind.INVOKED, effective_ns=start_ns)
        toolkit_key = _toolkit_key(emitter.ctx.execution_key, env.ToolkitType.SKILL, node_name)
        emitter.edge(
            node_key, toolkit_key, env.EdgeKind.USED_TOOLKIT,
            attributes={"toolkit_type": env.ToolkitType.SKILL.value, "toolkit_name": node_name},
            effective_ns=start_ns,
        )
        emitter.event(
            env.EventKind.SKILL_RESOLUTION,
            event_ns=start_ns if start_ns is not None else emitter.ctx.now_ns,
            related_node_key=node_key,
            attributes={
                "skill_name": node_name,
                "lifecycle_state": env.SkillLifecycleState.EXECUTED.value,
            },
        )
        return

    # Generic tool invocation.
    tool_kind = env.ToolKind.MCP if name.startswith("mcp__") else env.ToolKind.BUILTIN
    toolkit_type = env.ToolkitType.MCP_TOOL if tool_kind == env.ToolKind.MCP else env.ToolkitType.BUILTIN_TOOL
    attrs = redact_tool_args(name, tool_input if isinstance(tool_input, dict) else {})
    node_key = emitter.node(
        node_kind=env.NodeKind.INVOCATION,
        invocation_kind=env.InvocationKind.TOOL,
        tool_kind=tool_kind,
        node_name=name,
        native_seed=tool_use_id,
        start_ns=start_ns,
        end_ns=end_ns,
        orchestrator_model=orchestrator_model,
        identity_source=env.IdentitySource.NATIVE,
        parent_source=env.ParentSource.TRANSCRIPT,
        timing_source=timing_source,
        model_source=model_source,
        toolkit_source=env.ToolkitSource.NATIVE,
        usage_source=env.UsageSource.UNKNOWN,
        attributes=attrs,
    )
    emitter.edge(llm_call_key, node_key, env.EdgeKind.INVOKED, effective_ns=start_ns)
    toolkit_key = _toolkit_key(emitter.ctx.execution_key, toolkit_type, name)
    emitter.edge(
        node_key, toolkit_key, env.EdgeKind.USED_TOOLKIT,
        attributes={"toolkit_type": toolkit_type.value, "toolkit_name": name},
        effective_ns=start_ns,
    )

    if name in FILE_MUTATION_TOOL_NAMES:
        target = _extract_target(name, tool_input)
        mutation_attrs: dict[str, Any] = {"tool_name": name}
        if target:
            mutation_attrs["file_path"] = redact_file_path(target, cwd)
        emitter.event(
            env.EventKind.FILE_MUTATION,
            event_ns=end_ns if end_ns is not None else (start_ns if start_ns is not None else emitter.ctx.now_ns),
            related_node_key=node_key,
            attributes=mutation_attrs,
        )


def _process_assistant_message(
    emitter: _Emitter,
    rec: dict,
    turn_key: str,
) -> tuple[str, str | None, int | None]:
    msg = rec["message"]
    model = msg.get("model")
    model = model if isinstance(model, str) and model else None
    msg_id = msg.get("id")
    ts_raw = rec.get("timestamp")
    start_ns = _parse_ts(emitter.ctx, ts_raw)

    if isinstance(msg_id, str) and msg_id:
        native_seed = msg_id
        identity_source = env.IdentitySource.NATIVE
    else:
        native_seed = f"{turn_key}:llm_call:{emitter.next_fallback_seq()}"
        identity_source = env.IdentitySource.DERIVED

    usage = msg.get("usage")
    usage_source = env.UsageSource.NATIVE if isinstance(usage, dict) else env.UsageSource.UNKNOWN

    llm_key = emitter.node(
        node_kind=env.NodeKind.LLM_CALL,
        node_name=model or "unknown-model",
        native_seed=native_seed,
        start_ns=start_ns,
        end_ns=None,
        request_model=model,
        identity_source=identity_source,
        parent_source=env.ParentSource.TRANSCRIPT,
        timing_source=env.TimingSource.MARKER if start_ns is not None else env.TimingSource.UNKNOWN,
        model_source=env.ModelSource.EXPLICIT if model else env.ModelSource.UNKNOWN,
        toolkit_source=env.ToolkitSource.UNKNOWN,
        usage_source=usage_source,
    )
    emitter.edge(turn_key, llm_key, env.EdgeKind.PARENT_OF, effective_ns=start_ns)

    if isinstance(usage, dict):
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_read = usage.get("cache_read_input_tokens")
        cache_creation = usage.get("cache_creation_input_tokens")
        cached_tokens: int | None = None
        if isinstance(cache_read, (int, float)) or isinstance(cache_creation, (int, float)):
            cached_tokens = int(cache_read or 0) + int(cache_creation or 0)
        emitter.usage(
            llm_key,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_source=env.UsageSource.NATIVE,
            cached_tokens=cached_tokens,
            effective_ns=start_ns,
        )

    return llm_key, model, start_ns


def _walk_segment(
    emitter: _Emitter,
    records: list[dict],
    turn_key: str,
    subagent_map: dict[str, Path],
) -> None:
    """Emit llm_call + invocation nodes/edges/events for one turn's (or
    one subagent's) flat record slice."""
    result_ts_index = _index_tool_result_timestamps(records)
    cwd = _first_cwd(records)
    for rec in records:
        msg = rec.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        llm_key, model, start_ns = _process_assistant_message(emitter, rec, turn_key)
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            _process_tool_use(
                emitter,
                block,
                llm_call_key=llm_key,
                orchestrator_model=model,
                start_ns=start_ns,
                result_ts_index=result_ts_index,
                cwd=cwd,
                subagent_map=subagent_map,
            )


def _last_timestamp(records: list[dict]) -> Any:
    for rec in reversed(records):
        ts = rec.get("timestamp")
        if ts:
            return ts
    return None


def _emit_subagent_child_turn(emitter: _Emitter, tool_use_id: str, sub_records: list[dict]) -> str:
    """A subagent's entire delegated transcript is treated as ONE child
    turn (Phase 1 scoping simplification, documented in the P1 report):
    the orchestrator sees a single delegated unit of work, not a
    separately-numbered user-turn sequence. start/end are reconstructed
    from the sub-transcript's own first/last native timestamps."""
    native_seed = f"subagent-turn:{tool_use_id}"
    start_raw = sub_records[0].get("timestamp") if sub_records else None
    end_raw = _last_timestamp(sub_records)
    start_ns = _parse_ts(emitter.ctx, start_raw)
    end_ns = _parse_ts(emitter.ctx, end_raw) if end_raw is not None else None
    if start_ns is not None and end_ns is not None:
        timing_source = env.TimingSource.RECONSTRUCTED
    elif start_ns is not None:
        timing_source = env.TimingSource.MARKER
    else:
        timing_source = env.TimingSource.UNKNOWN
    return emitter.node(
        node_kind=env.NodeKind.TURN,
        node_name=f"subagent-turn:{tool_use_id}",
        native_seed=native_seed,
        start_ns=start_ns,
        end_ns=end_ns,
        identity_source=env.IdentitySource.DERIVED,
        parent_source=env.ParentSource.UNKNOWN,
        timing_source=timing_source,
        model_source=env.ModelSource.UNKNOWN,
        toolkit_source=env.ToolkitSource.UNKNOWN,
        usage_source=env.UsageSource.UNKNOWN,
    )


def _split_into_turns(records: list[dict]) -> list[tuple[int, list[dict], str | None, Any]]:
    """[(user_turn_seq, turn_records, prompt_text_or_None, boundary_ts)].
    turn_records excludes the boundary user message itself (it holds
    only the follow-up tool_result-carrying user records and assistant
    records — same convention as turn-usage.py's `_walk_current_turn`,
    generalized to every turn instead of just the latest)."""
    turns: list[tuple[int, list[dict], str | None, Any]] = []
    current: list[dict] = []
    user_turn_seq = 0
    prompt_text: str | None = None
    boundary_ts: Any = None

    def _flush() -> None:
        if user_turn_seq > 0:
            turns.append((user_turn_seq, current[:], prompt_text, boundary_ts))

    for rec in records:
        msg = rec.get("message")
        if isinstance(msg, dict) and _is_real_user_message(msg):
            _flush()
            user_turn_seq += 1
            current = []
            content = msg.get("content")
            prompt_text = content if isinstance(content, str) else None
            boundary_ts = rec.get("timestamp")
            continue
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
            current.append(rec)
    _flush()
    return turns


def _emit_requested_skill(
    emitter: _Emitter,
    turn_key: str,
    user_turn_seq: int,
    command_name: str,
    boundary_ts_raw: Any,
) -> None:
    """A `/command`-shaped prompt with no structural tool_use evidence
    yet that the adapter loaded/ran it — REQUESTED lifecycle only,
    toolkit_source=COMMAND_PARSE (canonical-model.md §5). Emitted
    independent of any Skill/SlashCommand tool_use possibly appearing
    later in the same turn — the funnel signal ("requested" vs
    "executed") is deliberately not deduplicated, see §5."""
    start_ns = _parse_ts(emitter.ctx, boundary_ts_raw)
    native_seed = f"cmd:{user_turn_seq}:{command_name}"
    node_key = emitter.node(
        node_kind=env.NodeKind.INVOCATION,
        invocation_kind=env.InvocationKind.SKILL,
        node_name=command_name,
        native_seed=native_seed,
        start_ns=start_ns,
        end_ns=start_ns,
        identity_source=env.IdentitySource.SYNTHETIC,
        parent_source=env.ParentSource.INFERRED,
        timing_source=env.TimingSource.MARKER if start_ns is not None else env.TimingSource.UNKNOWN,
        model_source=env.ModelSource.UNKNOWN,
        toolkit_source=env.ToolkitSource.COMMAND_PARSE,
        usage_source=env.UsageSource.UNKNOWN,
        attributes={
            "skill_name": command_name,
            "lifecycle_state": env.SkillLifecycleState.REQUESTED.value,
        },
    )
    emitter.edge(turn_key, node_key, env.EdgeKind.PARENT_OF, effective_ns=start_ns)
    emitter.event(
        env.EventKind.SKILL_RESOLUTION,
        event_ns=start_ns if start_ns is not None else emitter.ctx.now_ns,
        related_node_key=node_key,
        attributes={
            "skill_name": command_name,
            "lifecycle_state": env.SkillLifecycleState.REQUESTED.value,
        },
    )


def _emit_turn_node(
    emitter: _Emitter,
    user_turn_seq: int,
    turn_records: list[dict],
    boundary_ts_raw: Any,
) -> str:
    native_seed = f"turn:{user_turn_seq}"
    start_ns = _parse_ts(emitter.ctx, boundary_ts_raw)
    last_raw = _last_timestamp(turn_records)
    end_ns = _parse_ts(emitter.ctx, last_raw) if last_raw is not None else None
    if start_ns is not None and end_ns is not None:
        timing_source = env.TimingSource.RECONSTRUCTED
    elif start_ns is not None:
        timing_source = env.TimingSource.MARKER
    else:
        timing_source = env.TimingSource.UNKNOWN
    return emitter.node(
        node_kind=env.NodeKind.TURN,
        node_name=f"turn-{user_turn_seq}",
        native_seed=native_seed,
        start_ns=start_ns,
        end_ns=end_ns,
        identity_source=env.IdentitySource.DERIVED,
        parent_source=env.ParentSource.UNKNOWN,
        timing_source=timing_source,
        model_source=env.ModelSource.UNKNOWN,
        toolkit_source=env.ToolkitSource.UNKNOWN,
        usage_source=env.UsageSource.UNKNOWN,
        attributes={"user_turn_seq": user_turn_seq},
    )


def _walk_main(emitter: _Emitter, records: list[dict], subagent_map: dict[str, Path]) -> None:
    for user_turn_seq, turn_records, prompt_text, boundary_ts in _split_into_turns(records):
        turn_key = _emit_turn_node(emitter, user_turn_seq, turn_records, boundary_ts)
        if prompt_text:
            command_name = initiative.detect_command(prompt_text)
            if command_name:
                _emit_requested_skill(emitter, turn_key, user_turn_seq, command_name, boundary_ts)
        _walk_segment(emitter, turn_records, turn_key, subagent_map)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit_from_transcript(
    transcript_path: Path,
    session_id: str,
    org_id: str,
    adapter: str = "claude",
) -> Iterable[env.Envelope]:
    """Walk the transcript (and any subagent sub-transcripts it
    references) and return Envelope records covering turn/llm_call/
    invocation nodes, their edges, file_mutation/skill_resolution
    events, and usage_observed records. See the module docstring for
    the empirical basis of the modeling choices below.

    Best-effort: an unreadable or empty transcript yields no envelopes
    rather than raising (matches every other Claude hook's failure
    posture — telemetry must never break the agent loop, and this
    module is not yet wired as a hook, but keeps that discipline so
    wiring it later is a no-op change).
    """
    transcript_path = Path(transcript_path)
    records = _read_jsonl(transcript_path)
    if not records:
        return []

    ctx = _Ctx(
        org_id=org_id,
        adapter=adapter,
        session_id=session_id,
        execution_key=_execution_key(org_id, adapter, session_id),
        now_ns=time.time_ns(),
    )
    emitter = _Emitter(ctx)
    subagent_map = _discover_subagent_transcripts(transcript_path)
    _walk_main(emitter, records, subagent_map)
    return emitter.records
