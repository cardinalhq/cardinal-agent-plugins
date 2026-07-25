"""OTLP/HTTP trace (ResourceSpans) building and emission for schema-v1
envelopes — docs/canonical-model.md §14 "OTLP wire format — envelopes as
spans", replacing the custom `/v1/envelopes` HTTP endpoint that Phase 1
mistakenly built (see `docs/local-notes/plans/agent-execution-graph.md`).

Envelopes describe span-shaped execution atoms (a node has a start/end, an
edge/event/usage/artifact/context observation is a zero-duration fact), so
they ride the OTLP **traces** signal — the same intake endpoint the plugin
already uses for logs (`otlp.py::emit_records` -> `/v1/logs`), just a
different signal (`/v1/traces`). This module mirrors otlp.py's connection
abstraction, auth, chunking, and silent-failure contract exactly; it does
not introduce a new transport.

Identity (trace_id/span_id) is a local, non-secret SHA-256 digest, not an
HMAC: trace_id/span_id are public on the OTLP wire (any backend that
receives the trace sees them) and only need to *identify*, not be
unforgeable — auth already happens via the ingest API key on the POST
itself. Lakerunner recomputes the same digest from
(org_id, adapter, session_id) / (trace_id, namespace, seed) for lookup, so
no shared secret needs to cross the wire or be distributed to adapters.

Stdlib only, no adapter-specific code.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from typing import Any, Iterable

from . import CORE_VERSION
from .envelope import (
    ArtifactLinkObserved,
    EdgeObserved,
    Envelope,
    ExecutionContext,
    ExecutionEvent,
    NodeObserved,
    NodeUpdated,
    RecordType,
    UsageObserved,
)
from .otlp import DEFAULT_TIMEOUT_SEC, IngestConnection, kv

TRACES_PATH = "/v1/traces"
SCOPE_NAME = "cardinal.execution_graph"

# Chunking bounds mirror the deleted envelope_client.py precedent this
# module replaces: a batch never exceeds CHUNK_MAX_SPANS spans, and if
# even that many spans would still serialize past CHUNK_MAX_BYTES
# (~1MB), the batch is bisected further. Avoids upstream buffer limits at
# the ingest handler.
CHUNK_MAX_SPANS = 100
CHUNK_MAX_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# Identity — trace_id / span_id (docs/canonical-model.md §14)
# ---------------------------------------------------------------------------


def trace_id_from(org_id: str, adapter: str, session_id: str) -> bytes:
    """16-byte trace_id = SHA-256(f"{org_id}|{adapter}|{session_id}")[:16].
    Deterministic and non-secret (see module docstring) — the same
    (org_id, adapter, session_id) always yields the same trace_id, so every
    envelope for one execution lands on one OTLP trace."""
    digest = hashlib.sha256(
        f"{org_id}|{adapter}|{session_id}".encode("utf-8", "surrogateescape")
    ).digest()
    return digest[:16]


# Per-record-type span_id seed namespace (docs/canonical-model.md §14).
_SPAN_ID_NAMESPACE: dict[RecordType, str] = {
    RecordType.NODE_OBSERVED: "node",
    RecordType.NODE_UPDATED: "node",
    RecordType.EDGE_OBSERVED: "edge",
    RecordType.EXECUTION_EVENT: "event",
    RecordType.USAGE_OBSERVED: "usage",
    RecordType.ARTIFACT_LINK_OBSERVED: "artifact",
    RecordType.CONTEXT_OBSERVED: "context",
}


def span_id_for_envelope(envelope: Envelope, trace_id: bytes) -> bytes:
    """8-byte span_id, deterministic per docs/canonical-model.md §14.

    Node payloads (`node_observed`/`node_updated`) key off `node_key`, NOT
    `record_id` — so repeated observations of the SAME node land on the
    SAME span_id by design (see the "Design note" in §14: multiple
    observations of one node produce multiple OTLP spans sharing a
    span_id, distinguished by `record_id`; the reducer, not the OTLP wire,
    is the authority for dedup/precedence — this is a legal, accepted
    OTLP trade-off). Every other record type keys off `record_id`, since
    those record types have no separate stable identity of their own
    beyond the observation itself.
    """
    namespace = _SPAN_ID_NAMESPACE[envelope.record_type]
    if isinstance(envelope.payload, (NodeObserved, NodeUpdated)):
        seed = envelope.payload.node_key
    else:
        seed = envelope.record_id
    digest = hashlib.sha256(
        f"{trace_id.hex()}|{namespace}|{seed}".encode("utf-8", "surrogateescape")
    ).digest()
    return digest[:8]


# ---------------------------------------------------------------------------
# Attribute flattening helpers
# ---------------------------------------------------------------------------


def _flatten(prefix: str, value: Any, out: list[dict[str, Any]], *, allow_nested: bool = True) -> None:
    """Expand one attributes-dict entry into one-or-more OTLP KeyValue
    dicts. Scalars become `prefix`; a dict nests one level as
    `prefix.subkey` (matching envelope.py's own one-level nesting
    allowance for context attributes); anything deeper, and lists, are
    JSON-encoded to a string rather than dropped or raising."""
    if value is None or value == "":
        return
    if isinstance(value, dict):
        if not allow_nested:
            out.append(kv(prefix, json.dumps(value, sort_keys=True, default=str)))
            return
        for k, v in value.items():
            _flatten(f"{prefix}.{k}", v, out, allow_nested=False)
        return
    if isinstance(value, list):
        out.append(kv(prefix, json.dumps(value, default=str)))
        return
    out.append(kv(prefix, value))


def _attributes_dict_to_kv(prefix: str, attributes: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k, v in (attributes or {}).items():
        _flatten(f"{prefix}.{k}", v, out)
    return out


def _common_attrs(envelope: Envelope) -> list[dict[str, Any]]:
    """Attributes present on every envelope-carrying span (§14 "Common
    span attributes")."""
    return [
        kv("cardinal.envelope.record_type", envelope.record_type.value),
        kv("cardinal.envelope.record_id", envelope.record_id),
        kv("cardinal.envelope.schema_version", envelope.schema_version),
        kv("cardinal.envelope.observed_ns", envelope.observed_ns),
        kv("cardinal.envelope.effective_ns", envelope.effective_ns),
    ]


# ---------------------------------------------------------------------------
# Per-record-type span shape (docs/canonical-model.md §14)
# ---------------------------------------------------------------------------

_PROVENANCE_FIELDS = (
    "identity_source",
    "parent_source",
    "timing_source",
    "model_source",
    "toolkit_source",
    "usage_source",
)


def _node_span_fields(envelope: Envelope) -> tuple[str, int, int, list[dict[str, Any]]]:
    payload = envelope.payload
    assert isinstance(payload, (NodeObserved, NodeUpdated))
    start_ns = payload.start_ns if payload.start_ns is not None else envelope.observed_ns
    end_ns = payload.end_ns if payload.end_ns is not None else start_ns

    attrs: list[dict[str, Any]] = [kv("cardinal.envelope.node_kind", payload.node_kind.value)]
    if payload.invocation_kind is not None:
        attrs.append(kv("cardinal.envelope.invocation_kind", payload.invocation_kind.value))
    if payload.tool_kind is not None:
        attrs.append(kv("cardinal.envelope.tool_kind", payload.tool_kind.value))
    for field_name in _PROVENANCE_FIELDS:
        value = getattr(payload, field_name)
        attrs.append(kv(f"cardinal.envelope.provenance.{field_name}", value.value))
    # SemConv-mapped fields per §12.
    if payload.request_model:
        attrs.append(kv("gen_ai.request.model", payload.request_model))
    if payload.orchestrator_model:
        attrs.append(kv("cardinal.orchestrator.model", payload.orchestrator_model))
    attrs.extend(_attributes_dict_to_kv("cardinal.envelope.attributes", payload.attributes))
    return payload.node_name, start_ns, end_ns, attrs


def _edge_span_fields(envelope: Envelope) -> tuple[str, int, int, list[dict[str, Any]]]:
    payload = envelope.payload
    assert isinstance(payload, EdgeObserved)
    ts = envelope.observed_ns
    name = f"edge:{payload.edge_kind.value}"
    attrs = [
        kv("cardinal.envelope.edge.source_node_key", payload.source_node_key),
        kv("cardinal.envelope.edge.target_node_key", payload.target_node_key),
        kv("cardinal.envelope.edge.kind", payload.edge_kind.value),
    ]
    attrs.extend(_attributes_dict_to_kv("cardinal.envelope.edge.attributes", payload.attributes))
    return name, ts, ts, attrs


def _event_span_fields(envelope: Envelope) -> tuple[str, int, int, list[dict[str, Any]]]:
    payload = envelope.payload
    assert isinstance(payload, ExecutionEvent)
    ts = payload.event_ns
    name = f"event:{payload.event_kind.value}"
    attrs = [
        kv("cardinal.envelope.event.kind", payload.event_kind.value),
        kv("cardinal.envelope.event.ns", payload.event_ns),
    ]
    if payload.related_node_key:
        attrs.append(kv("cardinal.envelope.event.related_node_key", payload.related_node_key))
    attrs.extend(_attributes_dict_to_kv("cardinal.envelope.event.attributes", payload.attributes))
    return name, ts, ts, attrs


def _usage_span_fields(envelope: Envelope) -> tuple[str, int, int, list[dict[str, Any]]]:
    payload = envelope.payload
    assert isinstance(payload, UsageObserved)
    ts = envelope.effective_ns
    attrs = [
        kv("cardinal.envelope.usage.node_key", payload.node_key),
        kv("cardinal.envelope.usage.input_tokens", payload.input_tokens),
        kv("cardinal.envelope.usage.output_tokens", payload.output_tokens),
        kv("cardinal.envelope.usage.source", payload.usage_source.value),
    ]
    if payload.cached_tokens is not None:
        attrs.append(kv("cardinal.envelope.usage.cached_tokens", payload.cached_tokens))
    if payload.cost_usd is not None:
        attrs.append(kv("cardinal.envelope.usage.cost_usd", payload.cost_usd))
    return "usage", ts, ts, attrs


def _artifact_span_fields(envelope: Envelope) -> tuple[str, int, int, list[dict[str, Any]]]:
    payload = envelope.payload
    assert isinstance(payload, ArtifactLinkObserved)
    ts = envelope.effective_ns
    name = f"artifact:{payload.artifact_kind}"
    attrs = [
        kv("cardinal.envelope.artifact.kind", payload.artifact_kind),
        kv("cardinal.envelope.artifact.ref", payload.artifact_ref),
        kv("cardinal.envelope.artifact.node_key", payload.node_key),
    ]
    attrs.extend(_attributes_dict_to_kv("cardinal.envelope.artifact.attributes", payload.attributes))
    return name, ts, ts, attrs


# ExecutionContext inheritable fields with a §12 SemConv mapping. Every
# populated inheritable field is ALSO written as cardinal.envelope.context.
# <field> (see _context_span_fields) — this table adds the SemConv name
# alongside it so both an OTel-native consumer and lakerunner's round-trip
# read the same span (§14: "write BOTH").
_CONTEXT_SEMCONV_SPAN_ATTRS: dict[str, str] = {
    "actor_id": "enduser.id",
    "workspace_id": "cardinal.workspace.id",
    "team_id": "cardinal.team.id",
    "environment": "deployment.environment.name",
    "agent_runtime_version": "cardinal.agent.runtime.version",
    "plugin_version": "service.version",
    "repository_name": "vcs.repository.name",
    "repository_url": "vcs.repository.url.full",
    "branch": "vcs.ref.head.name",
    "commit_sha": "vcs.ref.head.revision",
    "pr_number": "cardinal.pr.number",
    "pr_id": "cardinal.pr.id",
    "initiative_id": "cardinal.initiative.id",
    "outcome_id": "cardinal.outcome.id",
}

_CONTEXT_INHERITABLE_FIELDS = (
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


def _context_span_fields(envelope: Envelope) -> tuple[str, int, int, list[dict[str, Any]]]:
    payload = envelope.payload
    assert isinstance(payload, ExecutionContext)
    ts = envelope.effective_ns
    name = f"context:{payload.context_source.value}"
    attrs: list[dict[str, Any]] = []
    for field_name in _CONTEXT_INHERITABLE_FIELDS:
        value = getattr(payload, field_name)
        if value is None:
            continue
        attrs.append(kv(f"cardinal.envelope.context.{field_name}", value))
        semconv_key = _CONTEXT_SEMCONV_SPAN_ATTRS.get(field_name)
        if semconv_key is not None:
            attrs.append(kv(semconv_key, value))
    attrs.extend(_attributes_dict_to_kv("cardinal.envelope.context.attributes", payload.attributes))
    return name, ts, ts, attrs


_FIELD_BUILDERS = {
    RecordType.NODE_OBSERVED: _node_span_fields,
    RecordType.NODE_UPDATED: _node_span_fields,
    RecordType.EDGE_OBSERVED: _edge_span_fields,
    RecordType.EXECUTION_EVENT: _event_span_fields,
    RecordType.USAGE_OBSERVED: _usage_span_fields,
    RecordType.ARTIFACT_LINK_OBSERVED: _artifact_span_fields,
    RecordType.CONTEXT_OBSERVED: _context_span_fields,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def envelope_to_span(envelope: Envelope) -> dict[str, Any]:
    """Return a single OTLP Span dict (protobuf-JSON shape) for one
    envelope, per docs/canonical-model.md §14.

    `parent_span_id` is intentionally never set here: this function
    operates on ONE envelope at a time with no visibility into the
    accumulated edge set for the execution, so it cannot tell whether a
    node has a natural `parent_of`-derivable predecessor without a second,
    graph-aware pass. Per §14 this is fine — `parent_span_id` is optional
    and lakerunner reconstructs edges from `edge_observed` spans
    regardless. Left as a documented follow-up rather than a partial/lossy
    attempt at cross-envelope correlation in a single-envelope function.
    """
    trace_id = trace_id_from(envelope.org_id, envelope.adapter, envelope.session_id)
    span_id = span_id_for_envelope(envelope, trace_id)
    builder = _FIELD_BUILDERS[envelope.record_type]
    name, start_ns, end_ns, type_attrs = builder(envelope)
    attrs = _common_attrs(envelope) + type_attrs
    return {
        "traceId": trace_id.hex(),
        "spanId": span_id.hex(),
        "name": name,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
    }


def trace_resource_attrs(
    *,
    service_name: str,
    adapter: str,
    plugin_version: str,
    org_id: str,
    session_id: str,
) -> dict[str, str]:
    """Resource attributes for one execution's ResourceSpans batch — constant
    across every span in the batch, per docs/canonical-model.md §14. `
    service_name` is the adapter's product name (e.g. "claude-code"), kept
    distinct from the `adapter` identifier ("claude") since the two differ."""
    return {
        "service.name": service_name,
        "service.version": plugin_version,
        "cardinal.org.id": org_id,
        "cardinal.adapter": adapter,
        "cardinal.session.id": session_id,
        "cardinal.plugin_version": plugin_version,
        "cardinal.core_version": CORE_VERSION,
    }


def _split_by_bytes(spans: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    """Bisects `spans` until each piece serializes under CHUNK_MAX_BYTES,
    or holds a single span (a single oversized span is sent as-is rather
    than dropped)."""
    if len(spans) <= 1:
        yield spans
        return
    size = len(json.dumps(spans, separators=(",", ":"), default=str).encode("utf-8"))
    if size <= CHUNK_MAX_BYTES:
        yield spans
        return
    mid = len(spans) // 2
    yield from _split_by_bytes(spans[:mid])
    yield from _split_by_bytes(spans[mid:])


def _chunk_spans(spans: list[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(spans), CHUNK_MAX_SPANS):
        yield from _split_by_bytes(spans[start : start + CHUNK_MAX_SPANS])


def _post_resource_spans(
    spans: list[dict[str, Any]],
    connection: IngestConnection,
    resource: dict[str, str],
    timeout: float,
) -> None:
    if not spans:
        return
    body = {
        "resourceSpans": [
            {
                "resource": {"attributes": [kv(k, v) for k, v in resource.items()]},
                "scopeSpans": [
                    {
                        "scope": {"name": SCOPE_NAME, "version": CORE_VERSION},
                        "spans": spans,
                    }
                ],
            }
        ]
    }
    headers = {
        "content-type": "application/json",
        **dict(connection.extra_headers),
        connection.api_header: connection.api_key,
    }
    req = urllib.request.Request(
        connection.endpoint + TRACES_PATH,
        data=json.dumps(body, default=str).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def emit_envelope_spans(
    envelopes: list[Envelope],
    connection: IngestConnection | None,
    resource: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> None:
    """POST OTLP/HTTP JSON `resourceSpans` built from `envelopes` to
    `<connection.endpoint>/v1/traces` — the same OTLP intake endpoint the
    plugin already uses for logs (`otlp.py::emit_records` -> `/v1/logs`),
    a different signal.

    Best-effort silent, matching otlp.py::emit_records exactly: a None
    connection or empty `envelopes` is a no-op, and network/timeout errors
    are swallowed — telemetry must never break the agent loop. Large
    batches are chunked (~1MB or CHUNK_MAX_SPANS spans per POST, whichever
    binds first); each chunk is POSTed independently so one failed chunk
    drops only its own slice.
    """
    if not envelopes or connection is None:
        return
    spans = [envelope_to_span(e) for e in envelopes]
    for chunk in _chunk_spans(spans):
        _post_resource_spans(chunk, connection, resource, timeout)
