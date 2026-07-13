"""Golden-fixture harness: a stub Cardinal ingest server that records the
exact OTLP bodies POSTed to it.

P0 ships the harness + core-level usage; P1–P4 use it to capture each
plugin's pre-migration goldens and assert byte-equal post-migration output
(spec §Test strategy). Pattern ported from the codex plugin's
test_cardinal_plugin.py StubCardinal.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class StubIngest:
    """Records every /v1/logs body; answers /v1/metrics probes."""

    def __init__(self) -> None:
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0
        self.log_batches: list[dict[str, Any]] = []
        self.metrics_status = 400  # "auth OK" per probe semantics

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "StubIngest":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.path == "/v1/logs":
                    try:
                        stub.log_batches.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
                    self.send_response(200)
                elif self.path == "/v1/metrics":
                    self.send_response(stub.metrics_status)
                else:
                    self.send_response(404)
                self.end_headers()

            def log_message(self, *args: Any) -> None:
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

    def normalized_batches(self) -> list[dict[str, Any]]:
        """Batches with volatile fields normalized for golden comparison
        (core 0.2.0 gap #6 — every P1–P4 agent re-implemented these two
        rules locally):
        - timestamps zeroed; `ts` attribute value pinned
        - `cardinal.core_version` attribute DROPPED (pre-migration goldens
          lack the key entirely; pinning can't reconcile presence/absence)
        - `cardinal.plugin_version` attribute value pinned, and scope
          version pinned (release bumps must not break goldens)
        Everything else — event names, attribute keys and values, record
        ordering — must be byte-stable across a migration."""
        out = []
        for batch in self.log_batches:
            out.append(_normalize(batch))
        return out


_PINNED_ATTR_KEYS = ("ts", "cardinal.plugin_version")
_DROPPED_ATTR_KEYS = ("cardinal.core_version",)


def _normalize(node: Any) -> Any:
    if isinstance(node, dict):
        result = {}
        for k, v in node.items():
            if k in ("timeUnixNano", "observedTimeUnixNano"):
                result[k] = "0"
            elif k == "scope" and isinstance(v, dict) and "version" in v:
                result[k] = {**_normalize(v), "version": "<normalized>"}
            elif k == "attributes" and isinstance(v, list):
                kept = []
                for a in (_normalize(x) for x in v):
                    if isinstance(a, dict) and a.get("key") in _DROPPED_ATTR_KEYS:
                        continue
                    if isinstance(a, dict) and a.get("key") in _PINNED_ATTR_KEYS:
                        a = {**a, "value": {"stringValue": "<normalized>"}}
                    kept.append(a)
                result[k] = kept
            else:
                result[k] = _normalize(v)
        return result
    if isinstance(node, list):
        return [_normalize(x) for x in node]
    return node
