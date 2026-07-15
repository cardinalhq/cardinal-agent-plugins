"""cardinal-omnigent-policy — Cardinal policy modules for omnigent.

Unlike the four CLI adapters, this package consumes cardinal-agent-core
as a real pip dependency (not vendored) and runs inside the omnigent
SERVER process, one instance covering every harness omnigent drives
(Claude, Codex, Cursor, Hermes, Pi, OpenCode, custom YAML agents).

Registration: list `cardinal_omnigent` under `policy_modules:` in the
omnigent server config; omnigent scans the module for POLICY_REGISTRY.
Both policies accept an optional `config` second argument (omnigent
resolves arity); the factory forms (`make_telemetry_policy`,
`make_spend_limits_policy`) exist for omnigent's
`function: {path, arguments}` closure registration.

The integration surface was verified against omnigent commit
OMNIGENT_VERIFIED_COMMIT — omnigent is alpha, so every event-field read
in this package goes through the defensive accessors in `_events.py`,
and the contract must be re-verified on omnigent upgrades.
"""

from __future__ import annotations

OMNIGENT_VERIFIED_COMMIT = "2b3b54a4"
PLUGIN_VERSION = "0.3.0"

from .spend_limits import make_spend_limits_policy, spend_limits_policy  # noqa: E402
from .telemetry import make_telemetry_policy, telemetry_policy  # noqa: E402

POLICY_REGISTRY = [telemetry_policy, spend_limits_policy]

__all__ = [
    "OMNIGENT_VERIFIED_COMMIT",
    "PLUGIN_VERSION",
    "POLICY_REGISTRY",
    "telemetry_policy",
    "make_telemetry_policy",
    "spend_limits_policy",
    "make_spend_limits_policy",
]
