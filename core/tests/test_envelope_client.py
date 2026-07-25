"""Tests for cardinal_core.envelope_client (Phase 1 execution-graph HTTP
client — docs/local-notes/plans/agent-execution-graph.md). Mirrors the
otlp.py::emit_records test shape in test_core.py::OtlpTests, but the wire
format here is NDJSON against /v1/envelopes, not OTLP-JSON against
/v1/logs.

Run: cd core && python3 -m unittest tests.test_envelope_client -v
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cardinal_core import envelope as env
from cardinal_core.envelope_client import CHUNK_MAX_ENVELOPES, emit_envelopes
from cardinal_core.otlp import IngestConnection


class _StubEnvelopesServer:
    """Records every /v1/envelopes POST: raw body, headers, and the
    parsed NDJSON lines."""

    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.requests: list[dict] = []

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "_StubEnvelopesServer":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                lines = [ln for ln in raw.decode("utf-8").split("\n") if ln.strip()]
                stub.requests.append({
                    "path": self.path,
                    "content_type": self.headers.get("content-type"),
                    # Header names lowercased: email.message.Message keys
                    # are case-insensitive but a plain dict lookup isn't,
                    # and http.client may capitalize names on the wire.
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "lines": lines,
                    "parsed": [json.loads(ln) for ln in lines],
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


def _node_envelope(seed: str, *, org_id: str = "org-1", session_id: str = "sess-1") -> env.Envelope:
    payload = env.NodeObserved(
        execution_key="exec-key",
        node_key=f"node-{seed}",
        node_kind=env.NodeKind.TURN,
        node_name=f"turn-{seed}",
        identity_source=env.IdentitySource.DERIVED,
        parent_source=env.ParentSource.UNKNOWN,
        timing_source=env.TimingSource.UNKNOWN,
        model_source=env.ModelSource.UNKNOWN,
        toolkit_source=env.ToolkitSource.UNKNOWN,
        usage_source=env.UsageSource.UNKNOWN,
    )
    return env.Envelope(
        schema_version=env.SCHEMA_VERSION,
        org_id=org_id,
        adapter="claude",
        session_id=session_id,
        record_id=f"record-{seed}",
        record_type=env.RecordType.NODE_OBSERVED,
        observed_ns=1,
        effective_ns=1,
        payload=payload,
    )


class EnvelopeClientTests(unittest.TestCase):
    def test_emit_envelopes_end_to_end(self) -> None:
        stub = _StubEnvelopesServer().start()
        try:
            conn = IngestConnection(endpoint=stub.endpoint, api_key="k")
            envelopes = [_node_envelope("1"), _node_envelope("2")]
            emit_envelopes(envelopes, conn)
            time.sleep(0.05)
            self.assertEqual(len(stub.requests), 1)
            req = stub.requests[0]
            self.assertEqual(req["path"], "/v1/envelopes")
            self.assertEqual(req["content_type"], "application/x-ndjson")
            self.assertEqual(req["headers"].get("x-cardinalhq-api-key"), "k")
            self.assertEqual(len(req["parsed"]), 2)
            for line in req["parsed"]:
                self.assertEqual(line["record_type"], "node_observed")
                env.validate(env.from_json(line))
        finally:
            stub.stop()

    def test_no_connection_is_noop(self) -> None:
        # Must not raise even though there is nowhere to send the body.
        emit_envelopes([_node_envelope("x")], None)

    def test_no_envelopes_is_noop(self) -> None:
        stub = _StubEnvelopesServer().start()
        try:
            conn = IngestConnection(endpoint=stub.endpoint, api_key="k")
            emit_envelopes([], conn)
            time.sleep(0.05)
            self.assertEqual(len(stub.requests), 0)
        finally:
            stub.stop()

    def test_extra_headers_forwarded(self) -> None:
        stub = _StubEnvelopesServer().start()
        try:
            conn = IngestConnection(
                endpoint=stub.endpoint, api_key="k",
                extra_headers=(("x-extra", "v1"),),
            )
            emit_envelopes([_node_envelope("1")], conn)
            time.sleep(0.05)
            self.assertEqual(stub.requests[0]["headers"].get("x-extra"), "v1")
        finally:
            stub.stop()

    def test_chunking_splits_large_batch_into_multiple_posts(self) -> None:
        stub = _StubEnvelopesServer().start()
        try:
            conn = IngestConnection(endpoint=stub.endpoint, api_key="k")
            envelopes = [_node_envelope(str(i)) for i in range(250)]
            emit_envelopes(envelopes, conn)
            time.sleep(0.1)
            # 250 envelopes / CHUNK_MAX_ENVELOPES(100) -> 3 POSTs (100, 100, 50).
            self.assertGreater(len(stub.requests), 1)
            total = sum(len(r["parsed"]) for r in stub.requests)
            self.assertEqual(total, 250)
            for r in stub.requests:
                self.assertLessEqual(len(r["parsed"]), CHUNK_MAX_ENVELOPES)
            # No duplicate/missing node_keys across the split.
            seen = {line["payload"]["node_key"] for r in stub.requests for line in r["parsed"]}
            self.assertEqual(len(seen), 250)
        finally:
            stub.stop()

    def test_network_failure_is_silent(self) -> None:
        # Nothing listening on this port -> urlopen raises URLError, which
        # emit_envelopes must swallow (best-effort, matches otlp.py).
        conn = IngestConnection(endpoint="http://127.0.0.1:1", api_key="k")
        emit_envelopes([_node_envelope("1")], conn, timeout=0.5)


if __name__ == "__main__":
    unittest.main()
