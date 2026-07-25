"""HTTP client for POSTing schema-v1 envelopes to lakerunner's
`/v1/envelopes` (Phase 1 of docs/local-notes/plans/agent-execution-graph.md).

Mirrors otlp.py::emit_records: connection facts are an argument (no module
state), auth is `connection.api_header: connection.api_key` plus whatever
rides in `connection.extra_headers`, and failures are best-effort and
silent — telemetry must never break the agent loop. The wire shape differs
from otlp.py's OTLP-JSON body: this POSTs NDJSON (one `to_json(envelope)`
per line), matching lakerunner's `/v1/envelopes` ingest contract rather
than the OTLP `/v1/logs` one.

Stdlib only, no adapter-specific code.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterable

from .envelope import Envelope, to_json
from .otlp import DEFAULT_TIMEOUT_SEC, IngestConnection

ENVELOPES_PATH = "/v1/envelopes"
CONTENT_TYPE = "application/x-ndjson"

# Chunking bounds (task brief): a batch never exceeds CHUNK_MAX_ENVELOPES
# envelopes, and if even that many envelopes would still serialize past
# CHUNK_MAX_BYTES (~1MB), the batch is bisected further. Avoids upstream
# buffer limits at lakerunner's ingest handler.
CHUNK_MAX_ENVELOPES = 100
CHUNK_MAX_BYTES = 1_000_000


def _line_for(envelope: Envelope) -> str:
    return json.dumps(to_json(envelope), separators=(",", ":"), default=str)


def _split_by_bytes(lines: list[str]) -> Iterable[list[str]]:
    """Bisects `lines` until each piece's NDJSON body fits under
    CHUNK_MAX_BYTES, or holds a single line (a single oversized envelope
    is sent as-is rather than dropped)."""
    if len(lines) <= 1:
        yield lines
        return
    size = sum(len(line.encode("utf-8")) + 1 for line in lines)
    if size <= CHUNK_MAX_BYTES:
        yield lines
        return
    mid = len(lines) // 2
    yield from _split_by_bytes(lines[:mid])
    yield from _split_by_bytes(lines[mid:])


def _chunk_lines(lines: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(lines), CHUNK_MAX_ENVELOPES):
        yield from _split_by_bytes(lines[start : start + CHUNK_MAX_ENVELOPES])


def _post_ndjson(lines: list[str], connection: IngestConnection, timeout: float) -> None:
    if not lines:
        return
    body = ("\n".join(lines) + "\n").encode("utf-8")
    headers = {
        "content-type": CONTENT_TYPE,
        **dict(connection.extra_headers),
        connection.api_header: connection.api_key,
    }
    req = urllib.request.Request(
        connection.endpoint + ENVELOPES_PATH,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def emit_envelopes(
    envelopes: list[Envelope],
    connection: IngestConnection | None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> None:
    """POST an NDJSON body of `envelopes` to `<connection.endpoint>/v1/envelopes`.

    One `to_json(envelope)` per line. Best-effort silent: network/timeout
    errors are swallowed (matches otlp.py::emit_records) — telemetry must
    not break the agent loop. A None connection or empty `envelopes` is a
    no-op. Large batches are chunked (see CHUNK_MAX_ENVELOPES /
    CHUNK_MAX_BYTES above); each chunk is POSTed independently, so one
    failed chunk drops only its own slice.
    """
    if not envelopes or connection is None:
        return
    lines = [_line_for(e) for e in envelopes]
    for chunk in _chunk_lines(lines):
        _post_ndjson(chunk, connection, timeout)
