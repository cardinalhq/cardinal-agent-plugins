"""cardinal-omnigent-policy tests.

Telemetry assertions compare emitted event names + attribute keys against
REQUIRED_KEYS loaded from the repo-root cross-adapter contract test
(tests/test_contract.py) — the executable Cardinal contract.

One documented divergence: omnigent policies never see the workspace
(no cwd/repo/branch anywhere in the policy contract — spec §Verified
integration facts), so cardinal.git_state cannot carry cardinal_head_sha,
cardinal_cwd, or cardinal_remote_url; repo/branch ride the labels
convention instead. WORKSPACE_KEYS below subtracts exactly those from the
contract's git_state requirement.

omnigent is deliberately NOT in tests/test_contract.py's ADAPTERS (see
docs/specs/subagent-telemetry-enrichment.md §7-§8, toolkit-hive-mind.md
PLG.1): beyond the workspace-key gap above, omnigent's cardinal.subagent_usage
has no subagent_type equivalent (only subagent_description — a structural
difference, the engine has no type taxonomy to probe), and there is no
committed adapters/omnigent/tests/goldens/*.json for the shared test's
glob to find. The capability-identity fields the lakerunner extractor DOES
key on for omnigent — the MCP split on cardinal.turn_tool, subagent_description
+ model on cardinal.subagent_usage, cardinal_command on cardinal.git_state —
are asserted below (see TelemetryToolTests.test_mcp_tool_call_keeps_qualified_name,
SubagentUsageTests.test_codex_native_child_emits_subagent_usage,
TelemetryGitStateTests.test_slash_command_detected_from_prompt).

Run from adapters/omnigent:
    PYTHONPATH=../../core:. python3 -m unittest discover tests
(fixtures.py also bootstraps sys.path, so a bare discover works too.)
"""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import fixtures

from cardinal_core import limits, pricing
from cardinal_core.paths import AgentPaths, atomic_write_json_compact

import cardinal_omnigent
from cardinal_omnigent import _identity, spend_limits, telemetry
from cardinal_omnigent import connect as omniconnect

StubIngest = fixtures.load_stub_ingest()
CONTRACT = fixtures.load_contract_module()
REQUIRED_KEYS: dict[str, set[str]] = CONTRACT.REQUIRED_KEYS

# git_state keys that are structurally unavailable to a server-side
# policy (documented divergence — see module docstring).
WORKSPACE_KEYS = {"cardinal_head_sha", "cardinal_cwd", "cardinal_remote_url"}


def records_from(stub) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for batch in stub.log_batches:
        for rl in batch.get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                out.extend(sl.get("logRecords", []))
    return out


def attrs_of(record: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in record.get("attributes", []):
        value = a.get("value", {})
        if "stringValue" in value:
            out[a["key"]] = value["stringValue"]
        elif "intValue" in value:
            out[a["key"]] = int(value["intValue"])
        elif "doubleValue" in value:
            out[a["key"]] = value["doubleValue"]
        elif "boolValue" in value:
            out[a["key"]] = value["boolValue"]
    return out


def resource_attrs_of(stub, batch_index: int = 0) -> dict[str, Any]:
    rl = stub.log_batches[batch_index]["resourceLogs"][0]
    return {a["key"]: a["value"].get("stringValue") for a in rl["resource"]["attributes"]}


def by_name(stub) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rec in records_from(stub):
        grouped.setdefault(rec["body"]["stringValue"], []).append(rec)
    return grouped


class StubBackedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.stub = StubIngest().start()
        self.addCleanup(self.stub.stop)
        self.config = {
            "ingest_endpoint": self.stub.endpoint,
            "ingest_api_key": "test-key",
            "deployment_environment": "dogfood",
            "org": "cardinalhq",
        }
        telemetry._SESSIONS.clear()
        self.addCleanup(telemetry._SESSIONS.clear)
        _identity._MEMO.clear()
        self.addCleanup(_identity._MEMO.clear)


class TelemetryGitStateTests(StubBackedTestCase):
    def test_labeled_request_emits_git_state_with_initiative(self) -> None:
        event = fixtures.request_event(labels={
            "cardinal.repo": "cardinalhq/lakerunner",
            "cardinal.branch": "feat/outcomes-observability",
        })
        self.assertIsNone(telemetry.telemetry_policy(event, self.config))
        recs = by_name(self.stub)["cardinal.git_state"]
        self.assertEqual(len(recs), 1)
        attrs = attrs_of(recs[0])
        self.assertEqual(attrs["cardinal_repo"], "cardinalhq/lakerunner")
        self.assertEqual(attrs["cardinal_branch"], "feat/outcomes-observability")
        self.assertEqual(attrs["cardinal_initiative_name"], "outcomes-observability")
        self.assertEqual(attrs["cardinal_initiative_type"], "feature")
        # Contract keys, minus the workspace facts a server-side policy
        # structurally cannot have (documented divergence).
        required = REQUIRED_KEYS["cardinal.git_state"] - WORKSPACE_KEYS
        self.assertFalse(required - set(attrs))

    def test_unlabeled_session_is_research_with_no_name(self) -> None:
        self.assertIsNone(telemetry.telemetry_policy(fixtures.request_event(), self.config))
        attrs = attrs_of(by_name(self.stub)["cardinal.git_state"][0])
        self.assertNotIn("cardinal_initiative_name", attrs)
        self.assertEqual(attrs["cardinal_initiative_type"], "research")
        self.assertNotIn("cardinal_branch", attrs)

    def test_slash_command_detected_from_prompt(self) -> None:
        event = fixtures.request_event(prompt="/code-review --fix")
        telemetry.telemetry_policy(event, self.config)
        attrs = attrs_of(by_name(self.stub)["cardinal.git_state"][0])
        self.assertEqual(attrs["cardinal_command"], "code-review")


class TelemetryUsageTests(StubBackedTestCase):
    def test_llm_response_emits_api_request_and_turn_usage(self) -> None:
        result = telemetry.telemetry_policy(fixtures.llm_response_event(), self.config)
        grouped = by_name(self.stub)
        self.assertEqual(set(grouped), {"api_request", "cardinal.turn_usage"})

        api = attrs_of(grouped["api_request"][0])
        self.assertFalse(REQUIRED_KEYS["api_request"] - set(api))
        self.assertEqual(api["agent_runtime"], "omnigent")
        self.assertEqual(api["model"], "claude-sonnet-4-5")
        self.assertEqual(api["input_tokens"], 1200)
        self.assertEqual(api["output_tokens"], 300)
        self.assertEqual(api["cache_read_tokens"], 900)

        turn = attrs_of(grouped["cardinal.turn_usage"][0])
        self.assertFalse(REQUIRED_KEYS["cardinal.turn_usage"] - set(turn))
        # Per-turn granularity marker — the documented divergence from the
        # CLI adapters' per-model-call events.
        self.assertEqual(turn["usage_granularity"], "turn")
        self.assertEqual(turn["turn_seq"], 0)
        self.assertIsNone(result)  # no session cost total → nothing to anchor

    def test_turn_counters_order_the_session(self) -> None:
        sid = "omni-seq"
        telemetry.telemetry_policy(fixtures.request_event(sid), self.config)
        telemetry.telemetry_policy(fixtures.llm_response_event(sid), self.config)
        telemetry.telemetry_policy(fixtures.tool_call_shell_event(sid), self.config)
        telemetry.telemetry_policy(fixtures.tool_call_shell_event(sid), self.config)
        telemetry.telemetry_policy(fixtures.llm_response_event(sid), self.config)
        grouped = by_name(self.stub)
        turns = [attrs_of(r) for r in grouped["cardinal.turn_usage"]]
        self.assertEqual([t["turn_seq"] for t in turns], [0, 1])
        self.assertEqual([t["user_turn_seq"] for t in turns], [1, 1])
        tools = [attrs_of(r) for r in grouped["cardinal.turn_tool"]]
        self.assertEqual([t["tool_seq"] for t in tools], [0, 1])
        self.assertEqual([t["turn_seq"] for t in tools], [1, 1])

    def test_cost_from_session_total_delta_and_state_updates(self) -> None:
        sid = "omni-cost"
        first = telemetry.telemetry_policy(
            fixtures.llm_response_event(sid, total_cost_usd=0.05), self.config)
        second = telemetry.telemetry_policy(
            fixtures.llm_response_event(sid, total_cost_usd=0.12), self.config)
        turns = [attrs_of(r) for r in by_name(self.stub)["cardinal.turn_usage"]]
        self.assertAlmostEqual(turns[0]["cost_usd"], 0.05)
        self.assertAlmostEqual(turns[1]["cost_usd"], 0.07)
        # The anchor rides back through omnigent state_updates on ALLOW.
        for result, total in ((first, 0.05), (second, 0.12)):
            self.assertEqual(result["result"], "ALLOW")
            self.assertEqual(result["state_updates"], [
                {"action": "set", "key": telemetry.COST_ANCHOR_KEY, "value": total},
            ])

    def test_cost_anchor_read_from_engine_session_state(self) -> None:
        event = fixtures.llm_response_event(
            "omni-state", total_cost_usd=0.15,
            session_state={telemetry.COST_ANCHOR_KEY: 0.10},
        )
        telemetry.telemetry_policy(event, self.config)
        turn = attrs_of(by_name(self.stub)["cardinal.turn_usage"][0])
        self.assertAlmostEqual(turn["cost_usd"], 0.05)

    def test_pricing_fallback_when_no_session_total(self) -> None:
        usage = {**fixtures.DEFAULT_TURN_USAGE, "model": "gpt-5-codex"}
        telemetry.telemetry_policy(
            fixtures.llm_response_event("omni-price", usage=usage), self.config)
        expected = pricing.compute_cost_usd(
            "gpt-5-codex",
            {"input_tokens": 1200, "cached_input_tokens": 900, "output_tokens": 300},
            pricing.OPENAI_PRICING_USD_PER_M,
        )
        turn = attrs_of(by_name(self.stub)["cardinal.turn_usage"][0])
        self.assertAlmostEqual(turn["cost_usd"], expected)

    def test_anthropic_model_priced_from_core_table(self) -> None:
        # core 0.3.0: Anthropic SKUs price from ANTHROPIC_PRICING_USD_PER_M
        # (disjoint cache buckets) when the server gives no cost total.
        telemetry.telemetry_policy(fixtures.llm_response_event("omni-anth"), self.config)
        expected = pricing.compute_cost_usd(
            "claude-sonnet-4-5",
            {"input_tokens": 1200, "cached_input_tokens": 900,
             "cache_creation_tokens": 100, "output_tokens": 300},
            pricing.ANTHROPIC_PRICING_USD_PER_M,
        )
        turn = attrs_of(by_name(self.stub)["cardinal.turn_usage"][0])
        self.assertAlmostEqual(turn["cost_usd"], expected)

    def test_platform_prefixed_claude_sku_priced(self) -> None:
        usage = {**fixtures.DEFAULT_TURN_USAGE, "model": "databricks-claude-opus-4-8"}
        telemetry.telemetry_policy(
            fixtures.llm_response_event("omni-dbx", usage=usage), self.config)
        turn = attrs_of(by_name(self.stub)["cardinal.turn_usage"][0])
        self.assertIn("cost_usd", turn)
        self.assertEqual(turn["model"], "databricks-claude-opus-4-8")

    def test_unpriced_model_omits_cost(self) -> None:
        usage = {**fixtures.DEFAULT_TURN_USAGE, "model": "mystery-llm-9000"}
        telemetry.telemetry_policy(
            fixtures.llm_response_event("omni-nocost", usage=usage), self.config)
        turn = attrs_of(by_name(self.stub)["cardinal.turn_usage"][0])
        self.assertNotIn("cost_usd", turn)


class TelemetryToolTests(StubBackedTestCase):
    def test_shell_tool_call_classified_by_core_bashclass(self) -> None:
        event = fixtures.tool_call_shell_event(command="git status && ls -la")
        self.assertIsNone(telemetry.telemetry_policy(event, self.config))
        attrs = attrs_of(by_name(self.stub)["cardinal.turn_tool"][0])
        self.assertFalse(REQUIRED_KEYS["cardinal.turn_tool"] - set(attrs))
        self.assertEqual(attrs["tool_name"], "bash")
        self.assertEqual(attrs["bash_class"], "git-read")  # git-read outranks file-read
        self.assertTrue(attrs["bash_multi"])

    def test_mcp_tool_call_keeps_qualified_name(self) -> None:
        telemetry.telemetry_policy(fixtures.tool_call_mcp_event(), self.config)
        attrs = attrs_of(by_name(self.stub)["cardinal.turn_tool"][0])
        self.assertEqual(attrs["tool_name"], "mcp__lakerunner__execute_logs_query")
        self.assertEqual(attrs["mcp_server_name"], "lakerunner")
        self.assertEqual(attrs["mcp_tool_name"], "execute_logs_query")
        self.assertNotIn("bash_class", attrs)

    def test_tool_result_success_and_failure(self) -> None:
        telemetry.telemetry_policy(fixtures.tool_result_event(success=True), self.config)
        telemetry.telemetry_policy(
            fixtures.tool_result_event(success=None, exit_code=2), self.config)
        results = [attrs_of(r) for r in by_name(self.stub)["tool_result"]]
        self.assertFalse(REQUIRED_KEYS["tool_result"] - set(results[0]))
        self.assertEqual(results[0]["success"], "true")
        self.assertEqual(results[1]["success"], "false")
        self.assertEqual(results[0]["agent_runtime"], "omnigent")

    def test_mcp_tool_result_normalized_name(self) -> None:
        event = fixtures.tool_result_event(
            target="mcp__lakerunner__execute_logs_query", success=True)
        telemetry.telemetry_policy(event, self.config)
        attrs = attrs_of(by_name(self.stub)["tool_result"][0])
        self.assertEqual(attrs["tool_name"], "mcp_tool")


class TelemetryIdentityTests(StubBackedTestCase):
    def test_user_email_from_actor_run_as_per_event(self) -> None:
        telemetry.telemetry_policy(
            fixtures.request_event(run_as="alice@example.com"), self.config)
        telemetry.telemetry_policy(
            fixtures.request_event("omni-2", run_as="bob@example.com"), self.config)
        self.assertEqual(resource_attrs_of(self.stub, 0)["user.email"], "alice@example.com")
        self.assertEqual(resource_attrs_of(self.stub, 1)["user.email"], "bob@example.com")

    def test_resource_attrs_runtime_and_harness(self) -> None:
        telemetry.telemetry_policy(fixtures.request_event(harness="codex"), self.config)
        res = resource_attrs_of(self.stub)
        self.assertEqual(res["agent.runtime"], "omnigent")
        self.assertEqual(res["cardinal.omnigent_harness"], "codex")
        self.assertEqual(res["service.name"], "omnigent")
        self.assertEqual(res["deployment.environment"], "dogfood")
        self.assertEqual(res["cardinal.org"], "cardinalhq")
        self.assertEqual(res["cardinal.plugin_version"], cardinal_omnigent.PLUGIN_VERSION)


class SessionIdentityMintTests(StubBackedTestCase):
    """omnigent's contract has no session id — the adapter mints one
    into engine session_state (see _identity.py)."""

    def test_mint_rides_allow_state_updates_and_keys_emission(self) -> None:
        result = telemetry.telemetry_policy(
            fixtures.request_event(session_id=None), self.config)
        self.assertEqual(result["result"], "ALLOW")
        update = result["state_updates"][0]
        self.assertEqual(update["action"], "set")
        self.assertEqual(update["key"], _identity.SESSION_ID_KEY)
        minted = update["value"]
        self.assertTrue(minted.startswith("omni-"))
        attrs = attrs_of(by_name(self.stub)["cardinal.git_state"][0])
        self.assertEqual(attrs["session_id"], minted)

    def test_persisted_id_wins_and_stops_updates(self) -> None:
        self.assertIsNone(telemetry.telemetry_policy(
            fixtures.request_event("omni-persisted"), self.config))
        attrs = attrs_of(by_name(self.stub)["cardinal.git_state"][0])
        self.assertEqual(attrs["session_id"], "omni-persisted")

    def test_llm_client_memo_keeps_policies_on_one_id(self) -> None:
        # Same engine (same llm_client object) → telemetry and
        # spend_limits mint the SAME id even before state persists.
        client = object()
        tel = telemetry.telemetry_policy(
            fixtures.request_event(session_id=None, llm_client=client),
            self.config)
        with TemporaryDirectory() as tmp:
            gate = spend_limits.spend_limits_policy(
                fixtures.request_event(session_id=None, llm_client=client),
                {"state_dir": tmp})
        tel_id = tel["state_updates"][0]["value"]
        self.assertEqual(gate["result"], "ALLOW")
        self.assertEqual(gate["state_updates"][0]["value"], tel_id)

    def test_distinct_engines_mint_distinct_ids(self) -> None:
        # Hold both client refs — a freed object's id() is reused, which
        # is exactly why the memo carries a TTL in production.
        client_a, client_b = object(), object()
        a = telemetry.telemetry_policy(
            fixtures.request_event(session_id=None, llm_client=client_a),
            self.config)
        b = telemetry.telemetry_policy(
            fixtures.request_event(session_id=None, llm_client=client_b),
            self.config)
        self.assertNotEqual(a["state_updates"][0]["value"],
                            b["state_updates"][0]["value"])

    def test_anchor_and_mint_ride_the_same_allow(self) -> None:
        result = telemetry.telemetry_policy(
            fixtures.llm_response_event(None, total_cost_usd=0.05), self.config)
        keys = [u["key"] for u in result["state_updates"]]
        self.assertEqual(keys, [_identity.SESSION_ID_KEY, telemetry.COST_ANCHOR_KEY])


class SubagentUsageTests(StubBackedTestCase):
    def test_codex_native_child_emits_subagent_usage(self) -> None:
        event = fixtures.response_event(
            "omni-child-1",
            labels={
                telemetry.WRAPPER_LABEL: "codex-native-ui-subagent",
                telemetry.CODEX_THREAD_ID_LABEL: "thread-42",
                telemetry.CODEX_PARENT_THREAD_ID_LABEL: "thread-root",
                telemetry.CODEX_NICKNAME_LABEL: "researcher",
            },
            model="gpt-5-codex",
            total_cost_usd=0.42,
        )
        self.assertIsNone(telemetry.telemetry_policy(event, self.config))
        recs = by_name(self.stub)["cardinal.subagent_usage"]
        attrs = attrs_of(recs[0])
        self.assertFalse(REQUIRED_KEYS["cardinal.subagent_usage"] - set(attrs))
        self.assertEqual(attrs["session_id"], "omni-child-1")
        self.assertEqual(attrs["subagent_description"], "researcher")
        self.assertEqual(attrs["subagent_id"], "thread-42")
        self.assertEqual(attrs["parent_thread_id"], "thread-root")
        self.assertEqual(attrs["model"], "gpt-5-codex")
        self.assertEqual(attrs["input_tokens"], 5000)
        self.assertAlmostEqual(attrs["cost_usd"], 0.42)
        self.assertEqual(attrs["usage_scope"], "session_cumulative")

    def test_cardinal_spec_stamp_marks_native_workflow_child(self) -> None:
        event = fixtures.response_event(
            "omni-child-2",
            labels={telemetry.CARDINAL_SUBAGENT_LABEL: "code-reviewer"},
        )
        telemetry.telemetry_policy(event, self.config)
        attrs = attrs_of(by_name(self.stub)["cardinal.subagent_usage"][0])
        self.assertEqual(attrs["subagent_description"], "code-reviewer")

    def test_top_level_response_emits_nothing(self) -> None:
        self.assertIsNone(telemetry.telemetry_policy(
            fixtures.response_event("omni-top"), self.config))
        self.assertEqual(self.stub.log_batches, [])


class BranchSniffTests(StubBackedTestCase):
    def test_branch_create_emits_git_state_and_sticks(self) -> None:
        sid = "omni-sniff"
        telemetry.telemetry_policy(fixtures.tool_call_shell_event(
            sid, command="git checkout -b feat/omnigent-parity"), self.config)
        telemetry.telemetry_policy(fixtures.request_event(sid), self.config)
        states = [attrs_of(r) for r in by_name(self.stub)["cardinal.git_state"]]
        self.assertEqual(len(states), 2)  # boundary emission + next request
        for attrs in states:
            self.assertEqual(attrs["cardinal_branch"], "feat/omnigent-parity")
            self.assertEqual(attrs["cardinal_initiative_name"], "omnigent-parity")
            self.assertEqual(attrs["cardinal_initiative_type"], "feature")
            self.assertEqual(attrs["cardinal_branch_source"], "tool_sniff")

    def test_git_switch_c_detected(self) -> None:
        telemetry.telemetry_policy(fixtures.tool_call_shell_event(
            "omni-sw", command="git switch -c fix/login-crash"), self.config)
        attrs = attrs_of(by_name(self.stub)["cardinal.git_state"][0])
        self.assertEqual(attrs["cardinal_initiative_type"], "bugfix")

    def test_labels_branch_outranks_sniffed(self) -> None:
        sid = "omni-lab"
        labels = {"cardinal.branch": "feat/labeled"}
        telemetry.telemetry_policy(fixtures.tool_call_shell_event(
            sid, command="git checkout -b feat/sniffed", labels=labels),
            self.config)
        telemetry.telemetry_policy(
            fixtures.request_event(sid, labels=labels), self.config)
        states = [attrs_of(r) for r in by_name(self.stub)["cardinal.git_state"]]
        self.assertEqual(len(states), 1)  # no boundary emission under labels
        self.assertEqual(states[0]["cardinal_branch"], "feat/labeled")
        self.assertNotIn("cardinal_branch_source", states[0])

    def test_plain_checkout_not_sniffed(self) -> None:
        telemetry.telemetry_policy(fixtures.tool_call_shell_event(
            "omni-amb", command="git checkout main -- file.txt"), self.config)
        self.assertNotIn("cardinal.git_state", by_name(self.stub))


class ContextModelFallbackTests(StubBackedTestCase):
    def test_context_model_used_when_data_has_none(self) -> None:
        usage = {k: v for k, v in fixtures.DEFAULT_TURN_USAGE.items()
                 if k != "model"}
        event = fixtures.llm_response_event(
            "omni-mdl", usage=usage, model=None)
        event["data"]["model"] = None
        event["context"]["model"] = "databricks-claude-opus-4-8"
        telemetry.telemetry_policy(event, self.config)
        turn = attrs_of(by_name(self.stub)["cardinal.turn_usage"][0])
        self.assertEqual(turn["model"], "databricks-claude-opus-4-8")


class TelemetryNeverRaisesTests(unittest.TestCase):
    """Fail-open: telemetry must not break the agent loop, whatever the
    engine hands us."""

    def test_garbage_events_abstain(self) -> None:
        for event in (
            None,
            {},
            {"type": "llm_response"},                     # no context → no identity
            {"type": "unknown-phase", "context": {}},
            object(),
        ):
            with self.subTest(event=event):
                self.assertIsNone(telemetry.telemetry_policy(event, None))
                self.assertIsNone(telemetry.telemetry_policy(event, {"bad": "config"}))

    def test_degenerate_payloads_still_mint_but_emit_nothing(self) -> None:
        # Handled phase + context present but payload unusable: the only
        # output is the identity mint (ALLOW + state_updates), no records.
        stub = StubIngest().start()
        self.addCleanup(stub.stop)
        telemetry._SESSIONS.clear()
        _identity._MEMO.clear()
        config = {"ingest_endpoint": stub.endpoint, "ingest_api_key": "k"}
        for event in (
            fixtures.llm_response_event(None, usage={}),          # empty usage
            fixtures.make_event("tool_call", {}, session_id=None),  # no target
        ):
            with self.subTest(event=event):
                result = telemetry.telemetry_policy(event, config)
                self.assertEqual(result["result"], "ALLOW")
                self.assertEqual(
                    result["state_updates"][0]["key"], _identity.SESSION_ID_KEY)
        self.assertEqual(stub.log_batches, [])

    def test_object_shaped_event_works(self) -> None:
        # Alpha contract: the accessors must handle attribute-shaped
        # events (future pydantic-ification) like the dict wire shape.
        stub = StubIngest().start()
        self.addCleanup(stub.stop)
        telemetry._SESSIONS.clear()

        class Ctx:
            actor = {"run_as": "carol@example.com"}
            labels = {"cardinal.branch": "fix/login-crash"}
            usage: dict = {}
            harness = "cursor"
            model = None

        class Event:
            type = "request"
            context = Ctx()
            data = "hi"
            session_state = {fixtures.SESSION_ID_KEY: "obj-sess"}

        config = {"ingest_endpoint": stub.endpoint, "ingest_api_key": "k"}
        self.assertIsNone(telemetry.telemetry_policy(Event(), config))
        attrs = attrs_of(by_name(stub)["cardinal.git_state"][0])
        self.assertEqual(attrs["session_id"], "obj-sess")
        self.assertEqual(attrs["cardinal_initiative_name"], "login-crash")
        self.assertEqual(attrs["cardinal_initiative_type"], "bugfix")

    def test_unreachable_endpoint_is_silent(self) -> None:
        config = {"ingest_endpoint": "http://127.0.0.1:9", "ingest_api_key": "k"}
        self.assertIsNone(telemetry.telemetry_policy(fixtures.request_event(), config))

    def test_emitter_bug_abstains(self) -> None:
        original = telemetry._handle_request
        telemetry._handle_request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))
        try:
            self.assertIsNone(telemetry.telemetry_policy(fixtures.request_event(), {}))
        finally:
            telemetry._handle_request = original


class SpendLimitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = AgentPaths(home=Path(self.tmp.name))
        self.config = {"state_dir": self.tmp.name}
        self.sid = "omni-limits"

    def write_verdict(self, blob: dict[str, Any]) -> None:
        atomic_write_json_compact(self.paths.verdict_path(self.sid), blob)

    def gate(self, event=None):
        return spend_limits.spend_limits_policy(
            event or fixtures.request_event(self.sid), self.config)

    def test_no_verdict_abstains(self) -> None:
        self.assertIsNone(self.gate())

    def test_non_request_phase_abstains(self) -> None:
        self.write_verdict(fixtures.verdict("block", 3, block_reason="over budget"))
        result = spend_limits.spend_limits_policy(
            fixtures.llm_response_event(self.sid), self.config)
        self.assertIsNone(result)

    def test_block_denies_with_server_reason(self) -> None:
        self.write_verdict(fixtures.verdict(
            "block", 3, block_reason="Initiative budget exhausted: $50 of $50."))
        result = self.gate()
        self.assertEqual(result["result"], "DENY")
        self.assertEqual(result["reason"], "Initiative budget exhausted: $50 of $50.")

    def test_warn_allows_with_standing_and_ack_hysteresis(self) -> None:
        self.write_verdict(fixtures.verdict("warn", 1))
        first = self.gate()
        self.assertEqual(first["result"], "ALLOW")
        self.assertIn("80%", first["reason"])
        self.assertEqual(first["set_labels"][spend_limits.BAND_LABEL], "1")
        self.assertEqual(first["data"]["cardinal_tier"], "warn")
        # Same band again → hysteresis suppresses (band was acked).
        self.assertIsNone(self.gate())
        # Band rises → surfaced again.
        self.write_verdict(fixtures.verdict("warn", 2))
        second = self.gate()
        self.assertEqual(second["result"], "ALLOW")
        self.assertEqual(second["set_labels"][spend_limits.BAND_LABEL], "2")

    def test_notify_tier_surfaces_once(self) -> None:
        self.write_verdict(fixtures.verdict("notify", 1, user_message=None))
        result = self.gate()
        self.assertEqual(result["result"], "ALLOW")
        self.assertEqual(result["data"]["cardinal_tier"], "notify")
        self.assertIsNone(self.gate())

    def test_override_downgrades_block_to_warn(self) -> None:
        self.write_verdict(fixtures.verdict("block", 3, block_reason="stop"))
        atomic_write_json_compact(self.paths.override_path(self.sid), {"by": "admin"})
        result = self.gate()
        self.assertEqual(result["result"], "ALLOW")
        self.assertEqual(result["data"]["cardinal_tier"], "warn")

    def test_session_cap_denies_from_context_usage(self) -> None:
        config = {**self.config, "session_cost_limit_usd": 1.0}
        over = fixtures.request_event(self.sid, total_cost_usd=1.5)
        result = spend_limits.spend_limits_policy(over, config)
        self.assertEqual(result["result"], "DENY")
        self.assertIn("$1.50", result["reason"])
        under = fixtures.request_event(self.sid, total_cost_usd=0.4)
        self.assertIsNone(spend_limits.spend_limits_policy(under, config))

    def test_stale_warn_verdict_fails_open(self) -> None:
        self.write_verdict(fixtures.verdict(
            "warn", 2, fetched_at=time.time() - limits.WARN_MAX_AGE_SEC - 60))
        self.assertIsNone(self.gate())

    def test_internal_error_abstains_not_denies(self) -> None:
        # Deliberate choice (module docstring): omnigent fails REQUEST
        # closed when a policy RAISES; our own bugs must abstain instead —
        # never self-DENY, never crash an org's fleet.
        original = limits.gate_decision
        limits.gate_decision = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))
        try:
            self.assertIsNone(self.gate())
        finally:
            limits.gate_decision = original

    def test_factory_form_closes_over_config(self) -> None:
        self.write_verdict(fixtures.verdict("block", 3, block_reason="stop"))
        policy = spend_limits.make_spend_limits_policy(self.config)
        result = policy(fixtures.request_event(self.sid))
        self.assertEqual(result["result"], "DENY")


class ConnectMergeTests(unittest.TestCase):
    CFG = {"ingest_endpoint": "https://intake.example", "ingest_api_key": "k",
           "state_dir": "~/.omnigent"}

    def test_merge_appends_managed_block_and_registration(self) -> None:
        merged, included = omniconnect.merge_config_text("server:\n  port: 8080\n", self.CFG)
        self.assertTrue(included)
        self.assertIn(omniconnect.BEGIN_MARKER, merged)
        self.assertIn("policy_modules:\n  - cardinal_omnigent", merged)
        self.assertIn('ingest_endpoint: "https://intake.example"', merged)
        self.assertTrue(merged.startswith("server:\n  port: 8080\n"))

    def test_merge_is_idempotent(self) -> None:
        once, _ = omniconnect.merge_config_text("server: {}\n", self.CFG)
        twice, _ = omniconnect.merge_config_text(once, self.CFG)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(omniconnect.BEGIN_MARKER), 1)

    def test_existing_policy_modules_not_duplicated(self) -> None:
        existing = "policy_modules:\n  - my_org.policies\n"
        merged, included = omniconnect.merge_config_text(existing, self.CFG)
        self.assertFalse(included)
        self.assertEqual(merged.count("policy_modules:"), 1)
        self.assertIn("my_org.policies", merged)
        self.assertIn("cardinal:", merged)


class RegistryTests(unittest.TestCase):
    def test_policy_registry_and_pin(self) -> None:
        self.assertEqual(cardinal_omnigent.POLICY_REGISTRY,
                         [telemetry.telemetry_policy, spend_limits.spend_limits_policy])
        self.assertEqual(cardinal_omnigent.OMNIGENT_VERIFIED_COMMIT, "2b3b54a4")

    def test_registry_policies_accept_config_by_arity(self) -> None:
        import inspect
        for policy in cardinal_omnigent.POLICY_REGISTRY:
            params = list(inspect.signature(policy).parameters)
            self.assertEqual(params[0], "event")
            self.assertEqual(params[1], "config")


if __name__ == "__main__":
    unittest.main()
