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
