"""Mock-server round-trip test for the Phase 1 execution-graph wire path:
hooks/trace_emit.py -> cardinal_core.envelope_client.emit_envelopes ->
NDJSON POST to /v1/envelopes (docs/local-notes/plans/agent-execution-graph.md
Phase 1). Proves the two halves (adapter emitter, HTTP client) compose
correctly against a real (redacted) transcript and that the wire shape
matches what Go's envelope.go unmarshaler expects — enum values as
strings, execution_key/node_key as strings (never bytes), pr_number (when
present) as int or null.

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
from cardinal_core.envelope_client import CHUNK_MAX_ENVELOPES, emit_envelopes  # noqa: E402
from cardinal_core.otlp import IngestConnection  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REAL_FIXTURE = FIXTURES_DIR / "real_transcript_p1.jsonl"

ORG_ID = "org-wire-test"
SESSION_ID = "sess-wire-test-0001"


class _EnvelopesStub(BaseHTTPRequestHandler):
    """Accepts POST /v1/envelopes, validates every NDJSON line as an
    Envelope, and records what was received in a threadsafe list."""

    received: list[dict] = []
    request_count = 0
    _lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        lines = [ln for ln in raw.decode("utf-8").split("\n") if ln.strip()]
        parsed = [json.loads(ln) for ln in lines]
        with type(self)._lock:
            type(self).request_count += 1
            for line in parsed:
                # Validate against the schema-v1 contract as the real
                # ingest handler would (envelope.from_json + validate).
                envelope = env.from_json(line)
                env.validate(envelope)
                type(self).received.append(line)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


class TraceEmitWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), _EnvelopesStub)
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
        _EnvelopesStub.received = []
        _EnvelopesStub.request_count = 0

    def test_real_transcript_round_trip(self) -> None:
        envelopes = list(
            te.emit_from_transcript(
                REAL_FIXTURE, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude",
            )
        )
        self.assertGreater(len(envelopes), 0)

        emit_envelopes(envelopes, self.connection)

        self.assertEqual(len(_EnvelopesStub.received), len(envelopes))

        # At least one envelope per node kind seen in the transcript.
        node_kinds_emitted = {
            e.payload.node_kind.value
            for e in envelopes
            if e.record_type in (env.RecordType.NODE_OBSERVED, env.RecordType.NODE_UPDATED)
        }
        self.assertTrue(node_kinds_emitted)
        received_node_kinds = {
            line["payload"]["node_kind"]
            for line in _EnvelopesStub.received
            if line["record_type"] in ("node_observed", "node_updated")
        }
        self.assertEqual(node_kinds_emitted, received_node_kinds)

        # Every received envelope validates (the stub already ran
        # env.validate() per-line; re-run here explicitly too so the
        # assertion is visible in this test's own body, not just the
        # handler's side effect).
        for line in _EnvelopesStub.received:
            env.validate(env.from_json(line))

        # Wire-shape spot checks against what Go's envelope.go unmarshaler
        # expects: enum fields are plain strings, identity fields are
        # strings (never bytes -- JSON can't carry bytes at all, so this
        # is really "json.dumps didn't choke and round-trips to str"),
        # and pr_number (when present, e.g. on a future ExecutionContext
        # envelope) is an int or null, never a string.
        for line in _EnvelopesStub.received:
            self.assertIsInstance(line["record_type"], str)
            payload = line["payload"]
            self.assertIsInstance(payload.get("execution_key", ""), str)
            if "node_kind" in payload:
                self.assertIsInstance(payload["node_kind"], str)
            if "edge_kind" in payload:
                self.assertIsInstance(payload["edge_kind"], str)
            if "event_kind" in payload:
                self.assertIsInstance(payload["event_kind"], str)
            if "node_key" in payload:
                self.assertIsInstance(payload["node_key"], str)
            if "pr_number" in payload:
                self.assertTrue(
                    payload["pr_number"] is None or isinstance(payload["pr_number"], int)
                )

    def test_chunking_emits_all_250_envelopes_across_multiple_posts(self) -> None:
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

        emit_envelopes(envelopes, self.connection)

        self.assertEqual(len(_EnvelopesStub.received), 250)
        self.assertGreater(
            _EnvelopesStub.request_count, 1,
            "250 envelopes must be split across more than one POST",
        )
        # 250 / CHUNK_MAX_ENVELOPES(100) -> ceil(250/100) = 3 POSTs.
        self.assertEqual(_EnvelopesStub.request_count, 3)
        node_keys = {line["payload"]["node_key"] for line in _EnvelopesStub.received}
        self.assertEqual(len(node_keys), 250, "no envelope should be dropped or duplicated")


if __name__ == "__main__":
    unittest.main()
