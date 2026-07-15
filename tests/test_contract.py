"""Cross-adapter contract test — the executable replacement for the
"keeping the repos in lockstep" prose sections of the old parity specs.

Reads every adapter's committed golden fixtures and asserts two things:

1. **Event coverage**: each adapter's goldens contain exactly the Cardinal
   events that adapter is contracted to emit (EXPECTED_EVENTS). An adapter
   silently dropping an event — or growing a new one nobody reviewed —
   fails here.
2. **Required keys**: for each shared event, the attribute keys every
   emitting adapter must carry (REQUIRED_KEYS), after normalizing Claude's
   dotted spelling (cardinal.head_sha) to the underscore form the ingest
   pipeline produces (cardinal_head_sha).

Documented, deliberate asymmetries (do NOT "fix" these here — they're the
product surface, see docs/specs/agent-core.md §Adapter variance table):
- claude: no tool_result / api_request (Claude Code's native OTel emits
  them); dotted attribute keys; OAuth plan fields on plan_state/plan_usage.
- cursor: no api_request / cardinal.turn_usage (product gap — no per-call
  token counts); extra turn_thought / turn_response events; plan_usage is
  the compact slice only.
- gemini: thought_tokens on usage events; plan_usage is the compact slice.
- codex: transcript-derived; plan_usage is the rate-limit slice.

Run from repo root:  python3 -m unittest discover tests
"""

from __future__ import annotations

import glob
import json
import os
import unittest
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ADAPTERS = ("claude", "codex", "cursor", "gemini")

# omnigent is deliberately NOT in ADAPTERS (toolkit-hive-mind.md PLG.1):
# it has no committed adapters/omnigent/tests/goldens/*.json for
# load_adapter_events' glob to find, its cardinal.git_state structurally
# lacks 3 REQUIRED_KEYS (no workspace in the policy contract), and its
# cardinal.subagent_usage has no subagent_type equivalent (see
# SUBAGENT_TYPE_ADAPTERS below). None of the three is closeable by adding
# an emit. It keeps its own suite (adapters/omnigent/tests/test_omnigent.py,
# which imports REQUIRED_KEYS from this module directly) and is covered by
# the capability-identity fields that DO apply to it there. Full writeup:
# docs/specs/subagent-telemetry-enrichment.md §8.

# Which Cardinal events each adapter's goldens must contain.
EXPECTED_EVENTS: dict[str, set[str]] = {
    "claude": {
        "cardinal.git_state", "cardinal.turn_tool", "cardinal.turn_usage",
        "cardinal.subagent_usage", "cardinal.plan_state", "cardinal.plan_usage",
    },
    "codex": {
        "cardinal.git_state", "cardinal.turn_tool", "cardinal.turn_usage",
        "cardinal.subagent_usage", "cardinal.plan_state", "cardinal.plan_usage",
        "api_request", "tool_result",
    },
    "cursor": {
        "cardinal.git_state", "cardinal.turn_tool", "cardinal.subagent_usage",
        "cardinal.plan_usage", "cardinal.turn_thought", "cardinal.turn_response",
        "tool_result",
    },
    "gemini": {
        "cardinal.git_state", "cardinal.turn_tool", "cardinal.turn_usage",
        "cardinal.subagent_usage", "cardinal.plan_state", "cardinal.plan_usage",
        "api_request", "tool_result",
    },
}

# Keys that MUST be present (post dot→underscore normalization) in every
# adapter that emits the event. Union across an adapter's golden records
# for that event — optional attrs (target, bash_class, cost_usd) are
# intentionally absent here.
REQUIRED_KEYS: dict[str, set[str]] = {
    "cardinal.git_state": {
        "session_id", "cardinal_branch", "cardinal_head_sha", "cardinal_cwd",
        "cardinal_initiative_type", "cardinal_remote_url", "cardinal_repo",
    },
    "cardinal.turn_tool": {
        "session_id", "tool_name", "tool_seq", "turn_seq", "user_turn_seq", "ts",
    },
    "cardinal.turn_usage": {
        "session_id", "model", "input_tokens", "output_tokens", "turn_seq", "ts",
    },
    "api_request": {
        "session_id", "model", "input_tokens", "output_tokens", "agent_runtime",
    },
    "tool_result": {
        "session_id", "tool_name", "success", "agent_runtime",
    },
    "cardinal.subagent_usage": {
        # model: the latent-subagent-mining clustering signal — dominant
        # model by worked tokens (claude), payload-probed (codex/cursor/
        # gemini), engine-injected context.model (omnigent).
        "session_id", "subagent_description", "model",
    },
    "cardinal.plan_state": {
        "session_id", "plan_type", "ts",
    },
    "cardinal.plan_usage": {
        "session_id",
    },
}

# Capability-identity fields the lakerunner identity extractor (T1.2, see
# docs/specs/toolkit-hive-mind.md) keys on. These are NOT folded into
# REQUIRED_KEYS above because that dict is shared verbatim with
# adapters/omnigent/tests/test_omnigent.py, and each field below is
# adapter-scoped rather than universal (see docs/specs/
# subagent-telemetry-enrichment.md §7 for the per-adapter emission
# evidence and why each asymmetry is deliberate, not a gap).

# claude keeps the raw qualified MCP name on tool_name only — no split.
# Its extractor instead reads mcp_server_name off Claude Code's own native
# tool_result/tool_parameters event, which this repo does not emit
# (turn-usage.py has no mcp__ branch). codex/cursor/gemini split into
# mcp_server_name + mcp_tool_name alongside the raw name.
MCP_SPLIT_ADAPTERS = {"codex", "cursor", "gemini"}
MCP_SPLIT_KEYS = {"mcp_server_name", "mcp_tool_name"}

# subagent_type: emitted by all 4 CLI adapters on cardinal.subagent_usage
# (probed on codex/cursor/gemini — their SubagentStop-equivalent payload
# shape is unconfirmed in the wild; see the CARDINAL_*_DEBUG_PAYLOADS
# capture affordances). Omnigent has no type-taxonomy field to probe for
# (structural, not a probing gap) and is excluded from this set.
SUBAGENT_TYPE_ADAPTERS = {"claude", "codex", "cursor", "gemini"}

# cardinal_command on cardinal.git_state, sourced from the shared
# cardinal_core.initiative.detect_command. Present only on the subset of
# records where a slash command actually fired (detect_command returns
# None otherwise, and otlp.log_record drops None attrs) — so this is
# checked as present-somewhere-in-the-goldens, matching how
# test_shared_events_agree_on_initiative_keys already treats it as
# allowed-optional rather than required-on-every-record.
COMMAND_IDENTITY_ADAPTERS = set(ADAPTERS)


def _walk_records(node):
    if isinstance(node, dict):
        if "logRecords" in node:
            yield from node["logRecords"]
        for v in node.values():
            yield from _walk_records(v)
    elif isinstance(node, list):
        for x in node:
            yield from _walk_records(x)


def _normalize_key(key: str) -> str:
    """Claude emits dotted keys; the ingest pipeline normalizes to
    underscores. Compare in the normalized space."""
    return key.replace(".", "_")


def load_adapter_events(adapter: str) -> dict[str, set[str]]:
    """event_name -> union of normalized attribute keys across the
    adapter's committed goldens."""
    events: dict[str, set[str]] = defaultdict(set)
    pattern = os.path.join(ROOT, "adapters", adapter, "tests", "goldens", "*.json")
    for path in glob.glob(pattern):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for rec in _walk_records(data):
            name = rec.get("body", {}).get("stringValue")
            if not name:
                continue
            events[name] |= {
                _normalize_key(a["key"]) for a in rec.get("attributes", [])
            }
    return dict(events)


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.by_adapter = {a: load_adapter_events(a) for a in ADAPTERS}
        for a in ADAPTERS:
            assert cls.by_adapter[a], (
                f"no golden records found for adapter {a!r} — goldens are "
                "committed fixtures; did a checkout/merge lose them?"
            )

    def test_event_coverage_matches_matrix(self) -> None:
        for adapter in ADAPTERS:
            emitted = {
                e for e in self.by_adapter[adapter]
                if e.startswith("cardinal.") or e in ("api_request", "tool_result")
            }
            with self.subTest(adapter=adapter):
                missing = EXPECTED_EVENTS[adapter] - emitted
                unexpected = emitted - EXPECTED_EVENTS[adapter]
                self.assertFalse(
                    missing,
                    f"{adapter} goldens lost contracted events: {sorted(missing)}",
                )
                self.assertFalse(
                    unexpected,
                    f"{adapter} goldens grew unreviewed events: {sorted(unexpected)} "
                    "— if intentional, update EXPECTED_EVENTS and the variance "
                    "table in docs/specs/agent-core.md",
                )

    def test_required_keys_present(self) -> None:
        for adapter in ADAPTERS:
            for event, keys in self.by_adapter[adapter].items():
                required = REQUIRED_KEYS.get(event)
                if required is None:
                    continue
                with self.subTest(adapter=adapter, event=event):
                    missing = required - keys
                    self.assertFalse(
                        missing,
                        f"{adapter}/{event} goldens missing required keys: "
                        f"{sorted(missing)}",
                    )

    def test_capability_identity_fields(self) -> None:
        """The lakerunner identity extractor (T1.2) parity gate: assert
        the capability-identity fields it depends on are present in each
        adapter's goldens, scoped to the adapters that actually emit them
        (docs/specs/subagent-telemetry-enrichment.md §7-§8)."""
        for adapter in MCP_SPLIT_ADAPTERS:
            keys = self.by_adapter[adapter].get("cardinal.turn_tool", set())
            with self.subTest(adapter=adapter, event="cardinal.turn_tool"):
                missing = MCP_SPLIT_KEYS - keys
                self.assertFalse(
                    missing,
                    f"{adapter}/cardinal.turn_tool goldens missing MCP split "
                    f"keys: {sorted(missing)}",
                )

        for adapter in SUBAGENT_TYPE_ADAPTERS:
            keys = self.by_adapter[adapter].get("cardinal.subagent_usage", set())
            with self.subTest(adapter=adapter, event="cardinal.subagent_usage"):
                self.assertIn(
                    "subagent_type", keys,
                    f"{adapter}/cardinal.subagent_usage goldens missing "
                    "subagent_type",
                )

        for adapter in COMMAND_IDENTITY_ADAPTERS:
            keys = self.by_adapter[adapter].get("cardinal.git_state", set())
            with self.subTest(adapter=adapter, event="cardinal.git_state"):
                self.assertIn(
                    "cardinal_command", keys,
                    f"{adapter}/cardinal.git_state goldens missing "
                    "cardinal_command (no fixture with a slash command?)",
                )

    def test_shared_events_agree_on_initiative_keys(self) -> None:
        """The initiative attribution keys are the product's backbone —
        assert all four adapters emit the identical normalized set on
        git_state (name may be absent on protected-branch fixtures, so it
        is checked as allowed-optional, not required)."""
        allowed = REQUIRED_KEYS["cardinal.git_state"] | {
            "cardinal_initiative_name", "cardinal_command",
            "plan_type", "rate_limit_tier", "event_name", "session_id",
        }
        for adapter in ADAPTERS:
            keys = self.by_adapter[adapter].get("cardinal.git_state", set())
            with self.subTest(adapter=adapter):
                extra = keys - allowed
                self.assertFalse(
                    extra,
                    f"{adapter}/cardinal.git_state grew unreviewed keys: {sorted(extra)}",
                )


if __name__ == "__main__":
    unittest.main()
