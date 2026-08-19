"""Unit tests for cardinal_core.traces — the OTLP `/v1/traces` projection
of schema-v1 envelopes (docs/canonical-model.md §14), replacing the custom
`/v1/envelopes` HTTP endpoint (envelope_client.py, deleted).

Run:
    cd core && python3 -m unittest tests.test_traces -v
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cardinal_core import envelope as env
from cardinal_core.otlp import IngestConnection
from cardinal_core.traces import (
    CHUNK_MAX_SPANS,
    emit_envelope_spans,
    envelope_to_span,
    span_id_for_envelope,
    trace_id_from,
    trace_resource_attrs,
)

ORG_ID = "org-1"
ADAPTER = "claude"
SESSION_ID = "sess-1"


def _span_attrs(span: dict) -> dict:
    out = {}
    for kv in span["attributes"]:
        v = kv["value"]
        if "stringValue" in v:
            out[kv["key"]] = v["stringValue"]
        elif "intValue" in v:
            out[kv["key"]] = int(v["intValue"])
        elif "doubleValue" in v:
            out[kv["key"]] = v["doubleValue"]
        elif "boolValue" in v:
            out[kv["key"]] = v["boolValue"]
    return out


def _envelope(record_type: env.RecordType, payload, *, record_id: str = "rec-1") -> env.Envelope:
    return env.Envelope(
        schema_version=env.SCHEMA_VERSION,
        org_id=ORG_ID,
        adapter=ADAPTER,
        session_id=SESSION_ID,
        record_id=record_id,
        record_type=record_type,
        observed_ns=1_000,
        effective_ns=1_100,
        payload=payload,
    )


def _node_payload(**overrides) -> env.NodeObserved:
    kwargs = dict(
        execution_key="exec-1",
        node_key="node-1",
        node_kind=env.NodeKind.INVOCATION,
        node_name="Read",
        invocation_kind=env.InvocationKind.TOOL,
        tool_kind=env.ToolKind.BUILTIN,
        identity_source=env.IdentitySource.NATIVE,
        parent_source=env.ParentSource.TRANSCRIPT,
        timing_source=env.TimingSource.RECONSTRUCTED,
        model_source=env.ModelSource.INHERITED,
        toolkit_source=env.ToolkitSource.NATIVE,
        usage_source=env.UsageSource.UNKNOWN,
        start_ns=2_000,
        end_ns=3_000,
        orchestrator_model="claude-opus-4-7",
        attributes={"file_path": "/repo/a.py", "nested": {"a": 1, "b": "x"}},
    )
    kwargs.update(overrides)
    return env.NodeObserved(**kwargs)


class EnvelopeToSpanTests(unittest.TestCase):
    def test_trace_id_deterministic_and_16_bytes(self) -> None:
        t1 = trace_id_from(ORG_ID, ADAPTER, SESSION_ID)
        t2 = trace_id_from(ORG_ID, ADAPTER, SESSION_ID)
        self.assertEqual(t1, t2)
        self.assertEqual(len(t1), 16)
        # Different session -> different trace_id.
        self.assertNotEqual(t1, trace_id_from(ORG_ID, ADAPTER, "other-session"))

    def test_node_span_id_keys_off_node_key_not_record_id(self) -> None:
        trace_id = trace_id_from(ORG_ID, ADAPTER, SESSION_ID)
        e1 = _envelope(env.RecordType.NODE_OBSERVED, _node_payload(), record_id="rec-a")
        e2 = _envelope(env.RecordType.NODE_UPDATED, _node_payload(), record_id="rec-b")
        # Same node_key, different record_type/record_id -> same span_id
        # (design note in §14: repeat observations of one node share a
        # span_id, distinguished by record_id on the span attributes).
        self.assertEqual(
            span_id_for_envelope(e1, trace_id), span_id_for_envelope(e2, trace_id)
        )
        self.assertEqual(len(span_id_for_envelope(e1, trace_id)), 8)

    def test_non_node_span_id_keys_off_record_id(self) -> None:
        trace_id = trace_id_from(ORG_ID, ADAPTER, SESSION_ID)
        payload = env.EdgeObserved(
            execution_key="exec-1", source_node_key="a", target_node_key="b",
            edge_kind=env.EdgeKind.PARENT_OF,
        )
        e1 = _envelope(env.RecordType.EDGE_OBSERVED, payload, record_id="rec-a")
        e2 = _envelope(env.RecordType.EDGE_OBSERVED, payload, record_id="rec-b")
        self.assertNotEqual(
            span_id_for_envelope(e1, trace_id), span_id_for_envelope(e2, trace_id)
        )

    def test_node_observed_span_shape(self) -> None:
        envelope = _envelope(env.RecordType.NODE_OBSERVED, _node_payload())
        span = envelope_to_span(envelope)
        self.assertEqual(len(bytes.fromhex(span["traceId"])), 16)
        self.assertEqual(len(bytes.fromhex(span["spanId"])), 8)
        self.assertEqual(span["name"], "Read")
        self.assertEqual(span["startTimeUnixNano"], "2000")
        self.assertEqual(span["endTimeUnixNano"], "3000")
        attrs = _span_attrs(span)
        self.assertEqual(attrs["cardinal.envelope.record_type"], "node_observed")
        self.assertEqual(attrs["cardinal.envelope.record_id"], "rec-1")
        self.assertEqual(attrs["cardinal.envelope.schema_version"], 1)
        # Reducer needs execution_key + node_key on the wire — span_id is a
        # truncated hash and not reversible. Without these, downstream
        # taps cannot reconstruct the envelope.
        self.assertEqual(attrs["cardinal.envelope.execution_key"], "exec-1")
        self.assertEqual(attrs["cardinal.envelope.node_key"], "node-1")
        self.assertEqual(attrs["cardinal.envelope.node_kind"], "invocation")
        self.assertEqual(attrs["cardinal.envelope.invocation_kind"], "tool")
        self.assertEqual(attrs["cardinal.envelope.tool_kind"], "builtin")
        self.assertEqual(attrs["cardinal.envelope.provenance.identity_source"], "native")
        self.assertEqual(attrs["cardinal.envelope.provenance.usage_source"], "unknown")
        self.assertEqual(attrs["cardinal.orchestrator.model"], "claude-opus-4-7")
        self.assertNotIn("gen_ai.request.model", attrs)  # request_model unset
        self.assertEqual(attrs["cardinal.envelope.attributes.file_path"], "/repo/a.py")
        self.assertEqual(attrs["cardinal.envelope.attributes.nested.a"], 1)
        self.assertEqual(attrs["cardinal.envelope.attributes.nested.b"], "x")

    def test_node_span_falls_back_to_observed_ns_when_no_start(self) -> None:
        payload = _node_payload(start_ns=None, end_ns=None)
        envelope = _envelope(env.RecordType.NODE_OBSERVED, payload)
        span = envelope_to_span(envelope)
        self.assertEqual(span["startTimeUnixNano"], "1000")
        self.assertEqual(span["endTimeUnixNano"], "1000")

    def test_llm_call_uses_request_model_semconv(self) -> None:
        payload = env.NodeObserved(
            execution_key="exec-1", node_key="node-llm", node_kind=env.NodeKind.LLM_CALL,
            node_name="claude-opus-4-7", request_model="claude-opus-4-7",
            identity_source=env.IdentitySource.NATIVE, parent_source=env.ParentSource.TRANSCRIPT,
            timing_source=env.TimingSource.MARKER, model_source=env.ModelSource.EXPLICIT,
            toolkit_source=env.ToolkitSource.UNKNOWN, usage_source=env.UsageSource.NATIVE,
            start_ns=5_000,
        )
        span = envelope_to_span(_envelope(env.RecordType.NODE_OBSERVED, payload))
        attrs = _span_attrs(span)
        self.assertEqual(attrs["gen_ai.request.model"], "claude-opus-4-7")
        self.assertNotIn("cardinal.orchestrator.model", attrs)

    def test_edge_observed_span_shape(self) -> None:
        payload = env.EdgeObserved(
            execution_key="exec-1", source_node_key="src", target_node_key="tgt",
            edge_kind=env.EdgeKind.USED_TOOLKIT,
            attributes={"toolkit_type": "mcp_tool", "toolkit_name": "list_services"},
        )
        envelope = _envelope(env.RecordType.EDGE_OBSERVED, payload)
        span = envelope_to_span(envelope)
        self.assertEqual(span["name"], "edge:used_toolkit")
        self.assertEqual(span["startTimeUnixNano"], span["endTimeUnixNano"])
        attrs = _span_attrs(span)
        self.assertEqual(attrs["cardinal.envelope.edge.source_node_key"], "src")
        self.assertEqual(attrs["cardinal.envelope.edge.target_node_key"], "tgt")
        self.assertEqual(attrs["cardinal.envelope.edge.kind"], "used_toolkit")
        self.assertEqual(attrs["cardinal.envelope.edge.attributes.toolkit_name"], "list_services")

    def test_execution_event_span_shape(self) -> None:
        payload = env.ExecutionEvent(
            execution_key="exec-1", event_kind=env.EventKind.FILE_MUTATION,
            event_ns=4_200, related_node_key="node-1",
            attributes={"tool_name": "Edit", "file_path": "/repo/a.py"},
        )
        envelope = _envelope(env.RecordType.EXECUTION_EVENT, payload)
        span = envelope_to_span(envelope)
        self.assertEqual(span["name"], "event:file_mutation")
        self.assertEqual(span["startTimeUnixNano"], "4200")
        self.assertEqual(span["endTimeUnixNano"], "4200")
        attrs = _span_attrs(span)
        self.assertEqual(attrs["cardinal.envelope.event.kind"], "file_mutation")
        self.assertEqual(attrs["cardinal.envelope.event.related_node_key"], "node-1")
        self.assertEqual(attrs["cardinal.envelope.event.ns"], 4200)
        self.assertEqual(attrs["cardinal.envelope.event.attributes.tool_name"], "Edit")

    def test_usage_observed_span_shape(self) -> None:
        payload = env.UsageObserved(
            execution_key="exec-1", node_key="node-1", input_tokens=100, output_tokens=50,
            usage_source=env.UsageSource.NATIVE, cached_tokens=10, cost_usd=0.0042,
        )
        envelope = _envelope(env.RecordType.USAGE_OBSERVED, payload)
        span = envelope_to_span(envelope)
        self.assertEqual(span["name"], "usage")
        self.assertEqual(span["startTimeUnixNano"], span["endTimeUnixNano"])
        attrs = _span_attrs(span)
        self.assertEqual(attrs["cardinal.envelope.usage.node_key"], "node-1")
        self.assertEqual(attrs["cardinal.envelope.usage.input_tokens"], 100)
        self.assertEqual(attrs["cardinal.envelope.usage.output_tokens"], 50)
        self.assertEqual(attrs["cardinal.envelope.usage.cached_tokens"], 10)
        self.assertAlmostEqual(attrs["cardinal.envelope.usage.cost_usd"], 0.0042)
        self.assertEqual(attrs["cardinal.envelope.usage.source"], "native")

    def test_artifact_link_observed_span_shape(self) -> None:
        payload = env.ArtifactLinkObserved(
            execution_key="exec-1", node_key="node-1", artifact_kind="file",
            artifact_ref="/repo/a.py", attributes={"lines_changed": 12},
        )
        envelope = _envelope(env.RecordType.ARTIFACT_LINK_OBSERVED, payload)
        span = envelope_to_span(envelope)
        self.assertEqual(span["name"], "artifact:file")
        attrs = _span_attrs(span)
        self.assertEqual(attrs["cardinal.envelope.artifact.kind"], "file")
        self.assertEqual(attrs["cardinal.envelope.artifact.ref"], "/repo/a.py")
        self.assertEqual(attrs["cardinal.envelope.artifact.node_key"], "node-1")
        self.assertEqual(attrs["cardinal.envelope.artifact.attributes.lines_changed"], 12)

    def test_every_record_type_validates_and_produces_a_span(self) -> None:
        """Every RecordType value must be dispatchable — a KeyError here
        would mean §14's per-record-type table and the code drifted."""
        node_env = _envelope(env.RecordType.NODE_OBSERVED, _node_payload())
        edge_env = _envelope(env.RecordType.EDGE_OBSERVED, env.EdgeObserved(
            execution_key="exec-1", source_node_key="a", target_node_key="b",
            edge_kind=env.EdgeKind.PARENT_OF,
        ))
        event_env = _envelope(env.RecordType.EXECUTION_EVENT, env.ExecutionEvent(
            execution_key="exec-1", event_kind=env.EventKind.RETRY, event_ns=1,
        ))
        usage_env = _envelope(env.RecordType.USAGE_OBSERVED, env.UsageObserved(
            execution_key="exec-1", node_key="node-1", input_tokens=1, output_tokens=1,
            usage_source=env.UsageSource.NATIVE,
        ))
        artifact_env = _envelope(env.RecordType.ARTIFACT_LINK_OBSERVED, env.ArtifactLinkObserved(
            execution_key="exec-1", node_key="node-1", artifact_kind="pr", artifact_ref="123",
        ))
        node_updated_env = _envelope(
            env.RecordType.NODE_UPDATED, env.NodeUpdated(**dataclasses.asdict(_node_payload()))
        )

        for e in (node_env, edge_env, event_env, usage_env, artifact_env, node_updated_env):
            env.validate(e)  # sanity: every synthesized envelope is contract-valid
            span = envelope_to_span(e)
            self.assertIn("traceId", span)
            self.assertIn("spanId", span)
            self.assertIn("name", span)


class _StubTracesServer:
    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.requests: list[dict] = []

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "_StubTracesServer":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                stub.requests.append({
                    "path": self.path,
                    "content_type": self.headers.get("content-type"),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw.decode("utf-8")),
                })
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def _node_envelope(seed: str) -> env.Envelope:
    payload = env.NodeObserved(
        execution_key="exec-key", node_key=f"node-{seed}", node_kind=env.NodeKind.TURN,
        node_name=f"turn-{seed}",
        identity_source=env.IdentitySource.DERIVED, parent_source=env.ParentSource.UNKNOWN,
        timing_source=env.TimingSource.UNKNOWN, model_source=env.ModelSource.UNKNOWN,
        toolkit_source=env.ToolkitSource.UNKNOWN, usage_source=env.UsageSource.UNKNOWN,
    )
    return env.Envelope(
        schema_version=env.SCHEMA_VERSION, org_id=ORG_ID, adapter=ADAPTER, session_id=SESSION_ID,
        record_id=f"record-{seed}", record_type=env.RecordType.NODE_OBSERVED,
        observed_ns=1, effective_ns=1, payload=payload,
    )


class EmitEnvelopeSpansTests(unittest.TestCase):
    def _resource(self) -> dict:
        return trace_resource_attrs(
            service_name="claude-code", adapter=ADAPTER, plugin_version="1.2.3",
            org_id=ORG_ID, session_id=SESSION_ID,
        )

    def test_emit_end_to_end(self) -> None:
        stub = _StubTracesServer().start()
        try:
            conn = IngestConnection(endpoint=stub.endpoint, api_key="k")
            envelopes = [_node_envelope("1"), _node_envelope("2")]
            emit_envelope_spans(envelopes, conn, self._resource())
            time.sleep(0.05)
            self.assertEqual(len(stub.requests), 1)
            req = stub.requests[0]
            self.assertEqual(req["path"], "/v1/traces")
            self.assertEqual(req["content_type"], "application/json")
            self.assertEqual(req["headers"].get("x-cardinalhq-api-key"), "k")
            body = req["body"]
            resource_spans = body["resourceSpans"][0]
            resource_attrs = {a["key"]: a["value"] for a in resource_spans["resource"]["attributes"]}
            self.assertEqual(resource_attrs["cardinal.org.id"], {"stringValue": ORG_ID})
            self.assertEqual(resource_attrs["service.name"], {"stringValue": "claude-code"})
            spans = resource_spans["scopeSpans"][0]["spans"]
            self.assertEqual(len(spans), 2)
        finally:
            stub.stop()

    def test_no_connection_is_noop(self) -> None:
        emit_envelope_spans([_node_envelope("x")], None, self._resource())

    def test_no_envelopes_is_noop(self) -> None:
        stub = _StubTracesServer().start()
        try:
            conn = IngestConnection(endpoint=stub.endpoint, api_key="k")
            emit_envelope_spans([], conn, self._resource())
            time.sleep(0.05)
            self.assertEqual(len(stub.requests), 0)
        finally:
            stub.stop()

    def test_extra_headers_forwarded(self) -> None:
        stub = _StubTracesServer().start()
        try:
            conn = IngestConnection(
                endpoint=stub.endpoint, api_key="k", extra_headers=(("x-extra", "v1"),),
            )
            emit_envelope_spans([_node_envelope("1")], conn, self._resource())
            time.sleep(0.05)
            self.assertEqual(stub.requests[0]["headers"].get("x-extra"), "v1")
        finally:
            stub.stop()

    def test_chunking_splits_large_batch_into_multiple_posts(self) -> None:
        stub = _StubTracesServer().start()
        try:
            conn = IngestConnection(endpoint=stub.endpoint, api_key="k")
            envelopes = [_node_envelope(str(i)) for i in range(250)]
            emit_envelope_spans(envelopes, conn, self._resource())
            time.sleep(0.1)
            self.assertGreater(len(stub.requests), 1)
            total = sum(
                len(r["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"])
                for r in stub.requests
            )
            self.assertEqual(total, 250)
            for r in stub.requests:
                spans = r["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
                self.assertLessEqual(len(spans), CHUNK_MAX_SPANS)
            span_ids = {
                s["spanId"]
                for r in stub.requests
                for s in r["body"]["resourceSpans"][0]["scopeSpans"][0]["spans"]
            }
            self.assertEqual(len(span_ids), 250)
        finally:
            stub.stop()

    def test_network_failure_is_silent(self) -> None:
        conn = IngestConnection(endpoint="http://127.0.0.1:1", api_key="k")
        emit_envelope_spans([_node_envelope("1")], conn, self._resource(), timeout=0.5)


if __name__ == "__main__":
    unittest.main()
