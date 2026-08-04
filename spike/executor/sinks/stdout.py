"""stdout findings sink — Phase 1 default.

Serializes the finding as JSON and prints it on a single line prefixed
with ``FINDING`` so downstream tail/grep tooling can parse the stream.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import Sink, SinkResult, sink


@sink("stdout")
class StdoutSink(Sink):
    id = "stdout"

    def deliver(self, finding: dict[str, Any], params: dict[str, Any]) -> SinkResult:
        stream = sys.stdout
        stream.write("FINDING " + json.dumps(finding, default=str) + "\n")
        stream.flush()
        return SinkResult(delivered=True)
