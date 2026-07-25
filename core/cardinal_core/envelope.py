"""Schema-v1 envelope contract for the agent execution graph (Phase 0.A).

Adapters observe execution atoms (turns, llm_calls, tool/skill/subagent/hook
invocations, artifacts) and emit them as envelopes. Envelopes carry
*observations*, not writes — ingest appends them to a raw log and a reducer
derives canonical graph state under provenance precedence. This module owns
only the wire contract: record types, the six-axis ontology, dataclass
payload shapes, and validation. It does not ingest, reduce, or transmit
anything — see docs/canonical-model.md for the full model this contract
implements, and docs/local-notes/plans/agent-execution-graph.md for the
architecture it is Phase 0 of.

Stdlib only, no adapter or network code.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Union

SCHEMA_VERSION = 1


class EnvelopeValidationError(ValueError):
    """Raised by validate() when an envelope violates the schema-v1 contract."""


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------


class RecordType(enum.StrEnum):
    NODE_OBSERVED = "node_observed"
    NODE_UPDATED = "node_updated"
    EDGE_OBSERVED = "edge_observed"
    EXECUTION_EVENT = "execution_event"
    USAGE_OBSERVED = "usage_observed"
    ARTIFACT_LINK_OBSERVED = "artifact_link_observed"
    CONTEXT_OBSERVED = "context_observed"


# ---------------------------------------------------------------------------
# Ontology — orthogonal dimensions (closed and small; see canonical-model.md)
# ---------------------------------------------------------------------------


class NodeKind(enum.StrEnum):
    TURN = "turn"
    LLM_CALL = "llm_call"
    INVOCATION = "invocation"
    ARTIFACT = "artifact"
    EVENT = "event"


class InvocationKind(enum.StrEnum):
    TOOL = "tool"
    SKILL = "skill"
    SUBAGENT = "subagent"
    HOOK = "hook"


class ToolKind(enum.StrEnum):
    """Sub-classification of `invocation_kind=tool` nodes. Extensible: new
    tool categories are added here as adapters discover them; node_kind and
    invocation_kind stay closed."""

    BUILTIN = "builtin"
    MCP = "mcp"
    SHELL = "shell"
    FILESYSTEM = "filesystem"


class ToolkitType(enum.StrEnum):
    """Product classification used by the fact table (toolkit_invocation_facts),
    distinct from the graph-level invocation_kind/tool_kind axes."""

    SKILL = "skill"
    MCP_TOOL = "mcp_tool"
    SUBAGENT = "subagent"
    BUILTIN_TOOL = "builtin_tool"


class EdgeKind(enum.StrEnum):
    PARENT_OF = "parent_of"
    INVOKED = "invoked"
    DELEGATED_TO = "delegated_to"
    CONTINUED_AS = "continued_as"
    USED_TOOLKIT = "used_toolkit"
    PRODUCED_ARTIFACT = "produced_artifact"
    CONTRIBUTED_TO = "contributed_to"
    LINKED_TO_OUTCOME = "linked_to_outcome"


class EventKind(enum.StrEnum):
    MODEL_SWITCH = "model_switch"
    CONTEXT_COMPACTION = "context_compaction"
    APPROVAL_REQUEST = "approval_request"
    PERMISSION_DENIAL = "permission_denial"
    RETRY = "retry"
    HOOK_RESULT = "hook_result"
    CONTEXT_RESET = "context_reset"
    FILE_MUTATION = "file_mutation"
    SKILL_RESOLUTION = "skill_resolution"
    EXECUTION_FAILURE = "execution_failure"
    RECORD_CONFLICT = "record_conflict"


class SkillLifecycleState(enum.StrEnum):
    """Lifecycle of a `skill` invocation node. Adoption metrics count
    EXECUTED only by default (see canonical-model.md §Skill lifecycle)."""

    REQUESTED = "requested"
    RESOLVED = "resolved"
    EXECUTED = "executed"


# ---------------------------------------------------------------------------
# Provenance — six independent axes, present on every node
# ---------------------------------------------------------------------------


class IdentitySource(enum.StrEnum):
    NATIVE = "native"
    DERIVED = "derived"
    SYNTHETIC = "synthetic"


class ParentSource(enum.StrEnum):
    NATIVE = "native"
    TRANSCRIPT = "transcript"
    TEMPORAL = "temporal"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class TimingSource(enum.StrEnum):
    NATIVE = "native"
    RECONSTRUCTED = "reconstructed"
    ESTIMATED = "estimated"
    MARKER = "marker"
    UNKNOWN = "unknown"


class ModelSource(enum.StrEnum):
    EXPLICIT = "explicit"
    INHERITED = "inherited"
    SESSION_DEFAULT = "session_default"
    UNKNOWN = "unknown"


class ToolkitSource(enum.StrEnum):
    NATIVE = "native"
    COMMAND_PARSE = "command_parse"
    PROMPT_INFERENCE = "prompt_inference"
    UNKNOWN = "unknown"


class UsageSource(enum.StrEnum):
    NATIVE = "native"
    ALLOCATED = "allocated"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Context provenance — a single axis for ExecutionContext observations.
# Distinct from the six-axis node provenance above: a context observation
# describes execution-wide, inheritable facts (repo, PR, initiative,
# engineer, ...), not a single node, so it carries one source rather than
# six independent ones.
# ---------------------------------------------------------------------------


class ContextSource(enum.StrEnum):
    SESSION_START = "session_start"
    GIT_STATE = "git_state"
    PR_CREATED = "pr_created"
    OUTCOME_RESOLVED = "outcome_resolved"
    INITIATIVE_MATCHED = "initiative_matched"
    USER_SUPPLIED = "user_supplied"
    UNKNOWN = "unknown"


# Precedence for the reducer (implemented in 0.2.b): per field, the
# observation with the higher-precedence source wins; equal precedence with
# a different value keeps the first value and emits
# execution_events(event_kind='record_conflict'). See
# docs/canonical-model.md §Execution context and inheritance.
CONTEXT_SOURCE_PRECEDENCE: dict[ContextSource, int] = {
    ContextSource.PR_CREATED: 60,
    ContextSource.OUTCOME_RESOLVED: 60,
    ContextSource.INITIATIVE_MATCHED: 50,
    ContextSource.USER_SUPPLIED: 40,
    ContextSource.GIT_STATE: 30,
    ContextSource.SESSION_START: 20,
    ContextSource.UNKNOWN: 10,
}


# ---------------------------------------------------------------------------
# Record payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _NodePayloadBase:
    """Shared shape of NodeObserved/NodeUpdated. Both carry all six
    provenance axes — node identity is the only record type that does."""

    execution_key: str
    node_key: str
    node_kind: NodeKind
    node_name: str
    identity_source: IdentitySource
    parent_source: ParentSource
    timing_source: TimingSource
    model_source: ModelSource
    toolkit_source: ToolkitSource
    usage_source: UsageSource
    invocation_kind: InvocationKind | None = None
    tool_kind: ToolKind | None = None
    start_ns: int | None = None
    end_ns: int | None = None
    orchestrator_model: str | None = None
    request_model: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NodeObserved(_NodePayloadBase):
    """First observation of a node."""


@dataclass(frozen=True)
class NodeUpdated(_NodePayloadBase):
    """A later observation revising fields on an already-observed node
    (e.g. end_ns filled in once a tool call completes)."""


@dataclass(frozen=True)
class EdgeObserved:
    execution_key: str
    source_node_key: str
    target_node_key: str
    edge_kind: EdgeKind
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionEvent:
    execution_key: str
    event_kind: EventKind
    event_ns: int
    related_node_key: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageObserved:
    execution_key: str
    node_key: str
    input_tokens: int
    output_tokens: int
    usage_source: UsageSource
    cached_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class ArtifactLinkObserved:
    execution_key: str
    node_key: str
    artifact_kind: str
    artifact_ref: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionContext:
    """Execution-wide, inheritable context — set once per execution and
    inherited by every node in it (see docs/canonical-model.md §Execution
    context and inheritance). Transport-level identity (org_id, adapter,
    session_id) already lives on the Envelope wrapper and is deliberately
    NOT duplicated here.

    A single RecordType (CONTEXT_OBSERVED) covers both the first observation
    and every later enrichment (e.g. a PR opening mid-session, an outcome
    resolving after the fact) — the reducer (0.2.b) applies
    CONTEXT_SOURCE_PRECEDENCE per field, the same "observation, not write"
    model used for nodes.

    `record_id_for` dedup: pass the full to_json(envelope)["payload"] dict
    (execution_key + context_source + every content field, canonically
    sorted) to `record_id_for`. Two observations with identical
    execution_key, context_source, and field values hash identically and
    dedupe; a different context_source or any different field value changes
    the hash and is treated as a genuine new observation.
    """

    execution_key: str
    context_source: ContextSource

    # Engineer / org placement.
    actor_id: str | None = None  # engineer identity (email/ID); see also
    # OTel resource attr enduser.id — distinct from adapter-side user_email.
    workspace_id: str | None = None
    team_id: str | None = None

    # Repository / VCS state.
    repository_id: str | None = None  # Cardinal-canonical ID, if known
    repository_name: str | None = None  # "owner/repo"
    repository_url: str | None = None  # must be userinfo-scrubbed already
    branch: str | None = None
    commit_sha: str | None = None

    # PR / initiative / outcome linkage.
    pr_id: str | None = None  # Cardinal-canonical ID, if known
    pr_number: int | None = None  # GitHub-style number
    initiative_id: str | None = None
    outcome_id: str | None = None

    # Environment / runtime.
    environment: str | None = None  # e.g. "dev", "prod-shadow"
    agent_runtime_version: str | None = None  # e.g. Claude Code version
    plugin_version: str | None = None  # Cardinal plugin version

    attributes: dict[str, Any] = field(default_factory=dict)


RecordPayload = Union[
    NodeObserved,
    NodeUpdated,
    EdgeObserved,
    ExecutionEvent,
    UsageObserved,
    ArtifactLinkObserved,
    ExecutionContext,
]


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Envelope:
    """The outer wrapper adapters emit. `record_id` is deterministic
    (see record_id_for) so a retried emission is ingest-idempotent."""

    schema_version: int
    org_id: str
    adapter: str  # "claude", "codex", "cursor", "gemini", "omnigent"
    session_id: str  # adapter-native, verbatim
    record_id: str
    record_type: RecordType
    observed_ns: int
    effective_ns: int
    payload: RecordPayload


_PAYLOAD_CLASS_FOR_RECORD_TYPE: dict[RecordType, type] = {
    RecordType.NODE_OBSERVED: NodeObserved,
    RecordType.NODE_UPDATED: NodeUpdated,
    RecordType.EDGE_OBSERVED: EdgeObserved,
    RecordType.EXECUTION_EVENT: ExecutionEvent,
    RecordType.USAGE_OBSERVED: UsageObserved,
    RecordType.ARTIFACT_LINK_OBSERVED: ArtifactLinkObserved,
    RecordType.CONTEXT_OBSERVED: ExecutionContext,
}

_NODE_ENUM_FIELDS: dict[str, type[enum.Enum]] = {
    "node_kind": NodeKind,
    "invocation_kind": InvocationKind,
    "tool_kind": ToolKind,
    "identity_source": IdentitySource,
    "parent_source": ParentSource,
    "timing_source": TimingSource,
    "model_source": ModelSource,
    "toolkit_source": ToolkitSource,
    "usage_source": UsageSource,
}

_ENUM_FIELDS_FOR_PAYLOAD_CLASS: dict[type, dict[str, type[enum.Enum]]] = {
    NodeObserved: _NODE_ENUM_FIELDS,
    NodeUpdated: _NODE_ENUM_FIELDS,
    EdgeObserved: {"edge_kind": EdgeKind},
    ExecutionEvent: {"event_kind": EventKind},
    UsageObserved: {"usage_source": UsageSource},
    ArtifactLinkObserved: {},
    ExecutionContext: {"context_source": ContextSource},
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


# Mirrors cardinal_core.redaction._USERINFO_RE (core/cardinal_core/redaction.py).
# Kept as a local, dependency-free copy — this module stays stdlib-only and is
# mirrored independently into every adapter. This is a validation safety net
# only: callers are expected to have already run strip_url_userinfo() before
# emitting; if the two patterns ever need to diverge, update both together.
_UNSTRIPPED_USERINFO_RE = re.compile(r"://[^/@\s]+@")

# Fields on ExecutionContext considered "inheritable content" for the
# not-empty check — everything except execution_key/context_source
# (identity/provenance, not content) and attributes (checked separately).
_EXECUTION_CONTEXT_INHERITABLE_FIELDS = (
    "actor_id",
    "workspace_id",
    "team_id",
    "repository_id",
    "repository_name",
    "repository_url",
    "branch",
    "commit_sha",
    "pr_id",
    "pr_number",
    "initiative_id",
    "outcome_id",
    "environment",
    "agent_runtime_version",
    "plugin_version",
)

_JSON_PRIMITIVE_TYPES = (str, int, float, bool, type(None))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EnvelopeValidationError(message)


def _enum_value(value: Any, enum_cls: type[enum.Enum], field_name: str) -> enum.Enum:
    """Coerce value to enum_cls; raises on any value the enum doesn't carry."""
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError) as exc:
        raise EnvelopeValidationError(
            f"{field_name}={value!r} is not a valid {enum_cls.__name__}"
        ) from exc


def _validate_node_payload(payload: _NodePayloadBase) -> None:
    _require(bool(payload.execution_key), "execution_key is required")
    _require(bool(payload.node_key), "node_key is required")
    _require(bool(payload.node_name), "node_name is required")
    node_kind = _enum_value(payload.node_kind, NodeKind, "node_kind")

    invocation_kind: InvocationKind | None = None
    if payload.invocation_kind is not None:
        invocation_kind = _enum_value(payload.invocation_kind, InvocationKind, "invocation_kind")  # type: ignore[assignment]
        _require(
            node_kind == NodeKind.INVOCATION,
            "invocation_kind may only be set when node_kind == invocation",
        )

    if payload.tool_kind is not None:
        _enum_value(payload.tool_kind, ToolKind, "tool_kind")
        _require(
            invocation_kind == InvocationKind.TOOL,
            "tool_kind may only be set when invocation_kind == tool",
        )

    for field_name, enum_cls in (
        ("identity_source", IdentitySource),
        ("parent_source", ParentSource),
        ("timing_source", TimingSource),
        ("model_source", ModelSource),
        ("toolkit_source", ToolkitSource),
        ("usage_source", UsageSource),
    ):
        value = getattr(payload, field_name)
        _require(
            value is not None,
            f"{field_name} is required on node payloads (all six provenance axes)",
        )
        _enum_value(value, enum_cls, field_name)


def _validate_edge_payload(payload: EdgeObserved) -> None:
    _require(bool(payload.execution_key), "execution_key is required")
    _require(bool(payload.source_node_key), "source_node_key is required")
    _require(bool(payload.target_node_key), "target_node_key is required")
    _enum_value(payload.edge_kind, EdgeKind, "edge_kind")


def _validate_execution_event_payload(payload: ExecutionEvent) -> None:
    _require(bool(payload.execution_key), "execution_key is required")
    _enum_value(payload.event_kind, EventKind, "event_kind")
    _require(payload.event_ns is not None, "event_ns is required")


def _validate_usage_payload(payload: UsageObserved) -> None:
    _require(bool(payload.execution_key), "execution_key is required")
    _require(bool(payload.node_key), "node_key is required")
    _require(payload.input_tokens is not None, "input_tokens is required")
    _require(payload.output_tokens is not None, "output_tokens is required")
    _enum_value(payload.usage_source, UsageSource, "usage_source")


def _validate_artifact_link_payload(payload: ArtifactLinkObserved) -> None:
    _require(bool(payload.execution_key), "execution_key is required")
    _require(bool(payload.node_key), "node_key is required")
    _require(bool(payload.artifact_kind), "artifact_kind is required")
    _require(bool(payload.artifact_ref), "artifact_ref is required")


def _validate_context_attribute_value(key: str, value: Any, *, allow_nested_dict: bool) -> None:
    """attributes values must be JSON-serializable primitives: no bytes, and
    dicts may nest at most one level (a dict of primitives is fine; a dict
    whose values are themselves dicts is not)."""
    if isinstance(value, bytes):
        raise EnvelopeValidationError(f"attributes[{key!r}] must not be bytes")
    if isinstance(value, _JSON_PRIMITIVE_TYPES):
        return
    if isinstance(value, list):
        for item in value:
            _validate_context_attribute_value(key, item, allow_nested_dict=False)
        return
    if isinstance(value, dict):
        _require(
            allow_nested_dict,
            f"attributes[{key!r}] nests a dict beyond one level, which is not allowed",
        )
        for inner_key, inner_value in value.items():
            _validate_context_attribute_value(
                f"{key}.{inner_key}", inner_value, allow_nested_dict=False
            )
        return
    raise EnvelopeValidationError(
        f"attributes[{key!r}]={value!r} ({type(value).__name__}) is not a "
        "JSON-serializable primitive"
    )


def _validate_context_attributes(attributes: dict[str, Any]) -> None:
    _require(isinstance(attributes, dict), "attributes must be a dict")
    for key, value in attributes.items():
        _validate_context_attribute_value(key, value, allow_nested_dict=True)


def _validate_execution_context_payload(payload: ExecutionContext) -> None:
    _require(bool(payload.execution_key), "execution_key is required")
    _enum_value(payload.context_source, ContextSource, "context_source")

    has_signal = bool(payload.attributes) or any(
        getattr(payload, name) is not None for name in _EXECUTION_CONTEXT_INHERITABLE_FIELDS
    )
    _require(
        has_signal,
        "ExecutionContext observation must populate at least one inheritable "
        "field or attributes entry — empty context observations carry no signal",
    )

    if payload.repository_url is not None:
        _require(
            _UNSTRIPPED_USERINFO_RE.search(payload.repository_url) is None,
            "repository_url must be userinfo-scrubbed (no user[:pass]@ before "
            f"the host); run strip_url_userinfo() before emitting: "
            f"{payload.repository_url!r}",
        )

    if payload.pr_number is not None:
        _require(
            isinstance(payload.pr_number, int)
            and not isinstance(payload.pr_number, bool)
            and payload.pr_number > 0,
            f"pr_number must be a positive int, got {payload.pr_number!r}",
        )

    _validate_context_attributes(payload.attributes)


def validate(envelope: Envelope) -> None:
    """Raises EnvelopeValidationError on any schema-v1 contract violation.
    See docs/canonical-model.md for the full rule set this enforces."""
    _require(
        envelope.schema_version == SCHEMA_VERSION,
        f"schema_version must be {SCHEMA_VERSION}, got {envelope.schema_version!r}",
    )
    _require(bool(envelope.org_id), "org_id is required")
    _require(bool(envelope.adapter), "adapter is required")
    _require(bool(envelope.session_id), "session_id is required")
    _require(bool(envelope.record_id), "record_id is required")

    record_type = _enum_value(envelope.record_type, RecordType, "record_type")
    expected_cls = _PAYLOAD_CLASS_FOR_RECORD_TYPE[record_type]
    _require(
        isinstance(envelope.payload, expected_cls),
        f"record_type={record_type.value} requires payload {expected_cls.__name__}, "
        f"got {type(envelope.payload).__name__}",
    )

    payload = envelope.payload
    if isinstance(payload, (NodeObserved, NodeUpdated)):
        _validate_node_payload(payload)
    elif isinstance(payload, EdgeObserved):
        _validate_edge_payload(payload)
    elif isinstance(payload, ExecutionEvent):
        _validate_execution_event_payload(payload)
    elif isinstance(payload, UsageObserved):
        _validate_usage_payload(payload)
    elif isinstance(payload, ArtifactLinkObserved):
        _validate_artifact_link_payload(payload)
    elif isinstance(payload, ExecutionContext):
        _validate_execution_context_payload(payload)
    else:
        raise EnvelopeValidationError(f"unrecognized payload type: {type(payload).__name__}")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _dataclass_to_json(obj: Any, enum_fields: dict[str, type[enum.Enum]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if f.name in enum_fields and value is not None:
            value = value.value
        out[f.name] = value
    return out


def _dataclass_from_json(
    cls: type, data: dict[str, Any], enum_fields: dict[str, type[enum.Enum]]
) -> Any:
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name in enum_fields and value is not None:
            try:
                value = enum_fields[f.name](value)
            except ValueError as exc:
                raise EnvelopeValidationError(
                    f"{f.name}={value!r} is not a valid {enum_fields[f.name].__name__}"
                ) from exc
        kwargs[f.name] = value
    return cls(**kwargs)


def to_json(envelope: Envelope) -> dict[str, Any]:
    """Envelope -> plain-dict, JSON-safe (enums as their string values)."""
    payload_cls = type(envelope.payload)
    enum_fields = _ENUM_FIELDS_FOR_PAYLOAD_CLASS[payload_cls]
    return {
        "schema_version": envelope.schema_version,
        "org_id": envelope.org_id,
        "adapter": envelope.adapter,
        "session_id": envelope.session_id,
        "record_id": envelope.record_id,
        "record_type": envelope.record_type.value,
        "observed_ns": envelope.observed_ns,
        "effective_ns": envelope.effective_ns,
        "payload": _dataclass_to_json(envelope.payload, enum_fields),
    }


def from_json(data: dict[str, Any]) -> Envelope:
    """Plain-dict -> Envelope. Raises EnvelopeValidationError on an unknown
    record_type or an enum field carrying a value outside its enum."""
    try:
        record_type = RecordType(data["record_type"])
    except (KeyError, ValueError) as exc:
        raise EnvelopeValidationError(
            f"invalid record_type: {data.get('record_type')!r}"
        ) from exc
    payload_cls = _PAYLOAD_CLASS_FOR_RECORD_TYPE[record_type]
    enum_fields = _ENUM_FIELDS_FOR_PAYLOAD_CLASS[payload_cls]
    payload = _dataclass_from_json(payload_cls, data.get("payload") or {}, enum_fields)
    return Envelope(
        schema_version=int(data["schema_version"]),
        org_id=str(data["org_id"]),
        adapter=str(data["adapter"]),
        session_id=str(data["session_id"]),
        record_id=str(data["record_id"]),
        record_type=record_type,
        observed_ns=int(data["observed_ns"]),
        effective_ns=int(data["effective_ns"]),
        payload=payload,
    )


def record_id_for(payload_dict: dict[str, Any]) -> str:
    """Deterministic record_id: SHA-256 hex over canonical JSON of a
    JSON-safe payload dict (e.g. the output of to_json(envelope)["payload"]
    plus whatever identity fields the caller wants folded in). Same input
    always produces the same id, so a retried emission is ingest-idempotent."""
    canonical = json.dumps(payload_dict, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
