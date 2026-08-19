"""Mock-server round-trip test for the Phase 1 execution-graph wire path:
hooks/trace_emit.py -> cardinal_core.traces.emit_envelope_spans -> OTLP/HTTP
JSON POST to /v1/traces (docs/canonical-model.md §14,
docs/local-notes/plans/agent-execution-graph.md Phase 1). Proves the two
halves (adapter emitter, OTLP span builder) compose correctly against a
real (redacted) transcript and that the wire shape is valid OTLP —
`resourceSpans[].scopeSpans[].spans[]`, each span carrying a 16-byte hex
`traceId`, an 8-byte hex `spanId`, and the `cardinal.envelope.*` attributes
§14 specifies.

Run with: python3 -m unittest tests.test_trace_emit_wire -v
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
import sys  # noqa: E402

sys.path.insert(0, str(HOOKS_DIR))

import trace_emit as te  # noqa: E402
from cardinal_core import envelope as env  # noqa: E402
from cardinal_core.otlp import IngestConnection  # noqa: E402
from cardinal_core.traces import (  # noqa: E402
    CHUNK_MAX_SPANS,
    emit_envelope_spans,
    span_id_for_envelope,
    trace_id_from,
    trace_resource_attrs,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REAL_FIXTURE = FIXTURES_DIR / "real_transcript_p1.jsonl"

ORG_ID = "org-wire-test"
SESSION_ID = "sess-wire-test-0001"


def _spans_of(body: dict) -> list[dict]:
    return body["resourceSpans"][0]["scopeSpans"][0]["spans"]


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


class _TracesStub(BaseHTTPRequestHandler):
    """Accepts POST /v1/traces, records the parsed OTLP JSON body and a
    threadsafe span count."""

    received: list[dict] = []
    request_count = 0
    _lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw.decode("utf-8"))
        with type(self)._lock:
            type(self).request_count += 1
            type(self).received.append(body)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


class TraceEmitWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _TracesStub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.connection = IngestConnection(
            endpoint=f"http://127.0.0.1:{cls.server.server_port}",
            api_key="test-key",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        _TracesStub.received = []
        _TracesStub.request_count = 0

    def _resource(self) -> dict:
        return trace_resource_attrs(
            service_name="claude-code", adapter="claude", plugin_version="0.0.0-test",
            org_id=ORG_ID, session_id=SESSION_ID,
        )

    def test_real_transcript_round_trip(self) -> None:
        envelopes = list(
            te.emit_from_transcript(
                REAL_FIXTURE, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude",
            )
        )
        self.assertGreater(len(envelopes), 0)

        emit_envelope_spans(envelopes, self.connection, self._resource())

        received_spans = [s for body in _TracesStub.received for s in _spans_of(body)]
        self.assertEqual(len(received_spans), len(envelopes))

        # Every span is valid OTLP shape: traceId is 16 bytes hex, spanId
        # is 8 bytes hex, both matching the deterministic derivation.
        expected_trace_id = trace_id_from(ORG_ID, "claude", SESSION_ID)
        for span in received_spans:
            self.assertEqual(span["traceId"], expected_trace_id.hex())
            self.assertEqual(len(bytes.fromhex(span["traceId"])), 16)
            self.assertEqual(len(bytes.fromhex(span["spanId"])), 8)
            self.assertIn("name", span)
            self.assertIn("startTimeUnixNano", span)
            self.assertIn("endTimeUnixNano", span)

        # spanId matches the deterministic per-envelope derivation.
        for envelope, span in zip(envelopes, received_spans):
            self.assertEqual(
                span["spanId"], span_id_for_envelope(envelope, expected_trace_id).hex()
            )

        # At least one span per node kind seen in the transcript, findable
        # via cardinal.envelope.record_type + the node's node_kind carried
        # in cardinal.envelope.node_kind.
        node_kinds_emitted = {
            e.payload.node_kind.value
            for e in envelopes
            if e.record_type in (env.RecordType.NODE_OBSERVED, env.RecordType.NODE_UPDATED)
        }
        self.assertTrue(node_kinds_emitted)
        received_node_kinds = {
            _span_attrs(s)["cardinal.envelope.node_kind"]
            for s in received_spans
            if "cardinal.envelope.node_kind" in _span_attrs(s)
        }
        self.assertEqual(node_kinds_emitted, received_node_kinds)

        # cardinal.envelope.record_id is present on every span and matches
        # the envelope it was derived from (reducer idempotency key).
        for envelope, span in zip(envelopes, received_spans):
            self.assertEqual(_span_attrs(span)["cardinal.envelope.record_id"], envelope.record_id)

    def test_chunking_emits_all_250_spans_across_multiple_posts(self) -> None:
        base_envelopes = list(
            te.emit_from_transcript(
                REAL_FIXTURE, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude",
            )
        )
        self.assertGreater(len(base_envelopes), 0)

        # Synthesize exactly 250 distinct NodeObserved envelopes (distinct
        # node_key per index) so chunking behavior is exercised
        # deterministically regardless of how many envelopes the real
        # fixture itself produces.
        envelopes = []
        for i in range(250):
            payload = env.NodeObserved(
                execution_key="exec-wire-chunk",
                node_key=f"node-chunk-{i}",
                node_kind=env.NodeKind.TURN,
                node_name=f"turn-{i}",
                identity_source=env.IdentitySource.DERIVED,
                parent_source=env.ParentSource.UNKNOWN,
                timing_source=env.TimingSource.UNKNOWN,
                model_source=env.ModelSource.UNKNOWN,
                toolkit_source=env.ToolkitSource.UNKNOWN,
                usage_source=env.UsageSource.UNKNOWN,
            )
            envelopes.append(env.Envelope(
                schema_version=env.SCHEMA_VERSION,
                org_id=ORG_ID,
                adapter="claude",
                session_id=SESSION_ID,
                record_id=f"record-chunk-{i}",
                record_type=env.RecordType.NODE_OBSERVED,
                observed_ns=i,
                effective_ns=i,
                payload=payload,
            ))

        emit_envelope_spans(envelopes, self.connection, self._resource())

        received_spans = [s for body in _TracesStub.received for s in _spans_of(body)]
        self.assertEqual(len(received_spans), 250)
        self.assertGreater(
            _TracesStub.request_count, 1,
            "250 envelopes must be split across more than one POST",
        )
        # 250 / CHUNK_MAX_SPANS(100) -> ceil(250/100) = 3 POSTs.
        self.assertEqual(_TracesStub.request_count, 3)
        span_ids = {s["spanId"] for s in received_spans}
        self.assertEqual(len(span_ids), 250, "no span should be dropped or collide")
        self.assertLessEqual(
            max(len(_spans_of(body)) for body in _TracesStub.received), CHUNK_MAX_SPANS
        )


if __name__ == "__main__":
    unittest.main()
