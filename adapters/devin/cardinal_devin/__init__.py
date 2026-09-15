"""Cardinal telemetry for Devin (Cognition).

Devin runs in Cognition's cloud and has no plugin or hook surface, so
this adapter is a server-side poller: it reads Devin's REST API with an
org service-user key and emits Cardinal OTLP logs (`cardinal.git_state`,
`cardinal.decision`). See docs/specs/devin-adapter.md.

Python 3.9+, stdlib only.
"""

from __future__ import annotations

import logging

logging.getLogger("cardinal_devin").addHandler(logging.NullHandler())

__version__ = "0.2.1"

SERVICE_NAME = "devin"
AGENT_RUNTIME = "devin"
SCOPE_NAME = "cardinal-devin-poller"
