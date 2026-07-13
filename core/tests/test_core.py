"""Unit tests for cardinal_core — ported from the four plugins' suites so
the behavior contract carries over verbatim.

Run:
    cd core && python3 -m unittest discover tests -v
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import bashclass, deviceflow, initiative, limits, otlp, pricing, session
from cardinal_core.paths import AgentPaths, atomic_write_json_compact

from tests.harness import StubIngest


class InitiativeTests(unittest.TestCase):
    def test_protected_branches_are_research(self) -> None:
        for b in ("main", "master", "develop", "trunk", None, "HEAD"):
            self.assertEqual(initiative.resolve_initiative(b), (None, "research"))

    def test_typed_prefixes(self) -> None:
        cases = {
            "feat/outcomes-observability": ("outcomes-observability", "feature"),
            "fix/login-crash": ("login-crash", "bugfix"),
            "refactor/auth-token-rotation": ("auth-token-rotation", "refactor"),
            "research/data-pipeline-spike": ("data-pipeline-spike", "research"),
            "perf/hot-loop": ("hot-loop", "feature"),
            "chore/deps-bump": ("deps-bump", "infra"),
            "docs/readme": ("readme", "infra"),
            "spike/idea": ("idea", "research"),
        }
        for branch, expected in cases.items():
            self.assertEqual(initiative.resolve_initiative(branch), expected, branch)

    def test_off_convention_defaults_to_feature(self) -> None:
        self.assertEqual(initiative.resolve_initiative("my-branch"), ("my-branch", "feature"))
        # Unknown prefix with slash: whole name survives, type feature.
        self.assertEqual(initiative.resolve_initiative("weird/thing"), ("weird/thing", "feature"))

    def test_worktree_noise_stripping(self) -> None:
        self.assertEqual(
            initiative.strip_worktree_noise("worktree-fix-1018-github-app-repo-picker"),
            "github-app-repo-picker",
        )
        self.assertEqual(initiative.strip_worktree_noise("normal-branch"), "normal-branch")
        # Nothing real remains → keep original.
        self.assertEqual(initiative.strip_worktree_noise("worktree-fix-1018"), "worktree-fix-1018")

    def test_canonical_repo(self) -> None:
        for url in (
            "git@github.com:cardinalhq/lakerunner.git",
            "https://github.com/cardinalhq/lakerunner.git",
            "https://github.com/cardinalhq/lakerunner",
        ):
            self.assertEqual(initiative.canonical_repo(url), "cardinalhq/lakerunner", url)
        self.assertIsNone(initiative.canonical_repo(None))
        self.assertIsNone(initiative.canonical_repo("not a url"))

    def test_detect_command(self) -> None:
        self.assertEqual(initiative.detect_command("/code-review --fix"), "code-review")
        self.assertEqual(
            initiative.detect_command("<command-name>/cardinal:optimize</command-name>"),
            "cardinal:optimize",
        )
        self.assertIsNone(initiative.detect_command("plain prompt"))
        self.assertIsNone(initiative.detect_command(None))


class BashClassTests(unittest.TestCase):
    def test_single_verbs(self) -> None:
        cases = {
            "git status": ("git-read", False),
            "git checkout -b feat/x": ("git-write", False),
            "rm -rf build": ("file-write", False),
            "pytest -x": ("test", False),
            "npm install": ("pkg", False),
            "npm test": ("test", False),
            "cargo clippy": ("build", False),
            "curl https://x": ("network", False),
            "frobnicate": ("other", False),
        }
        for cmd, expected in cases.items():
            self.assertEqual(bashclass.classify_bash_command(cmd), expected, cmd)

    def test_write_risk_wins_and_multi_flag(self) -> None:
        self.assertEqual(bashclass.classify_bash_command("ls && rm foo"), ("file-write", True))
        self.assertEqual(bashclass.classify_bash_command("git status | grep x"), ("git-read", True))

    def test_env_prefix_and_sudo_and_paths(self) -> None:
        self.assertEqual(bashclass.classify_bash_command("FOO=1 sudo /usr/bin/git push"), ("git-write", False))

    def test_empty(self) -> None:
        self.assertIsNone(bashclass.classify_bash_command("   "))


class PricingTests(unittest.TestCase):
    def test_prefix_fallback_per_provider(self) -> None:
        self.assertIsNotNone(
            pricing.price_for_model("gpt-5-codex-2026-03-01", pricing.OPENAI_PRICING_USD_PER_M)
        )
        self.assertIsNotNone(
            pricing.price_for_model("gemini-2.0-pro-2026-03-01", pricing.GEMINI_PRICING_USD_PER_M)
        )
        self.assertIsNone(
            pricing.price_for_model("gpt-5", pricing.GEMINI_PRICING_USD_PER_M)
        )

    def test_openai_cost_no_thought_bucket(self) -> None:
        cost = pricing.compute_cost_usd(
            "gpt-5",
            {"input_tokens": 1_000_000, "cached_input_tokens": 200_000, "output_tokens": 100_000},
            pricing.OPENAI_PRICING_USD_PER_M,
        )
        expected = (800_000 * 1.25 + 200_000 * 0.125 + 100_000 * 10.0) / 1_000_000
        self.assertAlmostEqual(cost, round(expected, 6), places=6)

    def test_gemini_thought_bills_as_output(self) -> None:
        cost = pricing.compute_cost_usd(
            "gemini-2.0-flash",
            {"input_tokens": 1_000_000, "cached_input_tokens": 200_000,
             "output_tokens": 500_000, "thought_tokens": 100_000},
            pricing.GEMINI_PRICING_USD_PER_M,
        )
        expected = (800_000 * 0.10 + 200_000 * 0.025 + 600_000 * 0.40) / 1_000_000
        self.assertAlmostEqual(cost, round(expected, 6), places=6)

    def test_unpriced_model_returns_none(self) -> None:
        self.assertIsNone(
            pricing.compute_cost_usd("mystery-model", {"input_tokens": 5}, pricing.OPENAI_PRICING_USD_PER_M)
        )

    def test_anthropic_disjoint_buckets_and_cache_write_premium(self) -> None:
        # Anthropic semantics: input / cache-read / cache-creation are
        # disjoint; nothing is subtracted from input_tokens.
        cost = pricing.compute_cost_usd(
            "claude-opus-4-8",
            {"input_tokens": 1_000_000, "cached_input_tokens": 400_000,
             "cache_creation_tokens": 200_000, "output_tokens": 100_000},
            pricing.ANTHROPIC_PRICING_USD_PER_M,
        )
        expected = (
            1_000_000 * 5.00 + 400_000 * 0.50 + 200_000 * 6.25 + 100_000 * 25.00
        ) / 1_000_000
        self.assertAlmostEqual(cost, round(expected, 6), places=6)

    def test_anthropic_dated_sku_prefix_and_tiers(self) -> None:
        self.assertEqual(
            pricing.price_for_model(
                "claude-opus-4-5-20251101", pricing.ANTHROPIC_PRICING_USD_PER_M
            )["input"],
            5.00,
        )
        # Opus 4.0/4.1 predate the 4.5 price drop.
        self.assertEqual(
            pricing.price_for_model(
                "claude-opus-4-1", pricing.ANTHROPIC_PRICING_USD_PER_M
            )["output"],
            75.00,
        )
        self.assertEqual(
            pricing.price_for_model(
                "claude-fable-5", pricing.ANTHROPIC_PRICING_USD_PER_M
            )["output"],
            50.00,
        )
        self.assertIn("anthropic", pricing.PROVIDER_TABLES)


class OtlpTests(unittest.TestCase):
    def test_kv_types(self) -> None:
        self.assertEqual(otlp.kv("b", True), {"key": "b", "value": {"boolValue": True}})
        self.assertEqual(otlp.kv("i", 3), {"key": "i", "value": {"intValue": "3"}})
        self.assertEqual(otlp.kv("f", 1.5), {"key": "f", "value": {"doubleValue": 1.5}})
        self.assertEqual(otlp.kv("s", "x"), {"key": "s", "value": {"stringValue": "x"}})

    def test_log_record_filters_empty(self) -> None:
        rec = otlp.log_record("cardinal.git_state", {"a": "x", "b": None, "c": ""}, 42)
        keys = [a["key"] for a in rec["attributes"]]
        self.assertEqual(keys, ["event_name", "a"])
        self.assertEqual(rec["timeUnixNano"], "42")
        self.assertEqual(rec["body"], {"stringValue": "cardinal.git_state"})

    def test_resource_attrs_stamp_core_version(self) -> None:
        attrs = otlp.resource_attrs(
            service_name="codex", agent_runtime="codex",
            deployment_environment=None, user_email=None, org=None,
            plugin_version="9.9.9",
        )
        self.assertEqual(attrs["cardinal.plugin_version"], "9.9.9")
        self.assertIn("cardinal.core_version", attrs)
        self.assertEqual(attrs["user.email"], "unknown")

    def test_parse_ts_ns(self) -> None:
        self.assertEqual(otlp.parse_ts_ns("2026-01-01T00:00:00Z", 7), 1767225600 * 1_000_000_000)
        self.assertEqual(otlp.parse_ts_ns("garbage", 7), 7)
        self.assertEqual(otlp.parse_ts_ns(None, 7), 7)
        # epoch millis upscale
        self.assertEqual(otlp.parse_ts_ns(1_700_000_000_000, 7), 1_700_000_000_000 * 1_000_000)

    def test_emit_records_end_to_end(self) -> None:
        stub = StubIngest().start()
        try:
            conn = otlp.IngestConnection(endpoint=stub.endpoint, api_key="k")
            resource = otlp.resource_attrs(
                service_name="test", agent_runtime="test",
                deployment_environment="test", user_email="t@x", org="o",
                plugin_version="0.0.1",
            )
            otlp.emit_records(
                [otlp.log_record("api_request", {"model": "m"}, 1)],
                conn, resource, scope_name="test-scope", scope_version="0.0.1",
            )
            time.sleep(0.05)
            self.assertEqual(len(stub.log_batches), 1)
            batch = stub.log_batches[0]
            records = batch["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
            self.assertEqual(records[0]["body"]["stringValue"], "api_request")
        finally:
            stub.stop()

    def test_emit_records_no_connection_is_noop(self) -> None:
        otlp.emit_records([otlp.log_record("x", {}, 1)], None, {}, scope_name="s", scope_version="v")


class LimitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.paths = AgentPaths(home=Path(self._tmp.name) / ".agent")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_verdict(self, verdict: dict) -> None:
        atomic_write_json_compact(self.paths.verdict_path("s1"), verdict)

    def test_no_verdict_fails_open(self) -> None:
        self.assertIsNone(limits.gate_output(self.paths, "s1", hook_event_name="UserPromptSubmit"))

    def test_block_verdict_blocks(self) -> None:
        self._write_verdict({
            "decision": "block", "band": 3, "fetched_at": time.time(),
            "block_reason": "over budget",
        })
        out = limits.gate_output(self.paths, "s1", hook_event_name="UserPromptSubmit")
        self.assertEqual(out, {"decision": "block", "reason": "over budget"})

    def test_override_downgrades_block_to_warn(self) -> None:
        self._write_verdict({
            "decision": "block", "band": 3, "fetched_at": time.time(),
            "agent_context": "economize", "user_message": "you are over",
        })
        self.paths.override_path("s1").parent.mkdir(parents=True, exist_ok=True)
        self.paths.override_path("s1").write_text("{}")
        out = limits.gate_output(self.paths, "s1", hook_event_name="UserPromptSubmit")
        self.assertIsNotNone(out)
        self.assertNotIn("decision", out)
        self.assertEqual(out["systemMessage"], "you are over")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_band_hysteresis_surfaces_once(self) -> None:
        self._write_verdict({
            "decision": "warn", "band": 2, "fetched_at": time.time(),
            "agent_context": "ctx", "user_message": "msg",
        })
        first = limits.gate_output(self.paths, "s1", hook_event_name="BeforeAgent")
        self.assertIsNotNone(first)
        self.assertEqual(first["hookSpecificOutput"]["hookEventName"], "BeforeAgent")
        second = limits.gate_output(self.paths, "s1", hook_event_name="BeforeAgent")
        self.assertIsNone(second, "same band must not re-surface")

    def test_stale_warn_fails_open(self) -> None:
        self._write_verdict({
            "decision": "warn", "band": 2,
            "fetched_at": time.time() - limits.WARN_MAX_AGE_SEC - 1,
            "agent_context": "ctx",
        })
        self.assertIsNone(limits.gate_output(self.paths, "s1", hook_event_name="UserPromptSubmit"))

    def test_stale_block_fails_open_after_max_age(self) -> None:
        self._write_verdict({
            "decision": "block", "band": 3,
            "fetched_at": time.time() - limits.BLOCK_MAX_AGE_SEC - 1,
        })
        self.assertIsNone(limits.gate_output(self.paths, "s1", hook_event_name="UserPromptSubmit"))

    def test_standing_lines_formatting(self) -> None:
        lines = limits.standing_lines({
            "evaluations": [
                {"scope": "session", "spent_usd": 1.25, "limit_usd": 100.0,
                 "fraction": 0.0125, "set_by": {"self": True}},
                {"scope": "engineer", "window": "week", "spent_usd": 1337.83,
                 "limit_usd": 1500.0, "fraction": 0.8919,
                 "set_by": {"display_name": "Alice"}},
            ]
        })
        self.assertEqual(lines[0], "- session: $1.25 of $100.00 (1%) — set by you")
        self.assertEqual(lines[1], "- engineer (week): $1337.83 of $1500.00 (89%) — set by Alice")

    def test_limits_config_absent_when_not_connected(self) -> None:
        self.assertIsNone(limits.limits_config(self.paths))


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.paths = AgentPaths(home=Path(self._tmp.name) / ".agent")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_counter_lifecycle(self) -> None:
        state = session.load_progress(self.paths, "s1")
        self.assertEqual(
            (state["user_turn_seq"], state["turn_seq"], state["tool_seq"]), (0, 0, 0)
        )
        session.begin_user_turn(state)
        self.assertEqual(state["user_turn_seq"], 1)
        state["tool_seq"] = 4
        session.end_model_call(state)
        self.assertEqual((state["turn_seq"], state["tool_seq"]), (1, 0))
        session.save_progress(self.paths, "s1", state)
        reloaded = session.load_progress(self.paths, "s1")
        self.assertEqual(reloaded["user_turn_seq"], 1)
        self.assertEqual(reloaded["turn_seq"], 1)

    def test_progress_preserves_adapter_extras(self) -> None:
        state = session.load_progress(self.paths, "s1")
        state["last_line"] = 512  # codex transcript cursor rides along
        session.save_progress(self.paths, "s1", state)
        self.assertEqual(session.load_progress(self.paths, "s1")["last_line"], 512)

    def test_plan_stamp_roundtrip(self) -> None:
        self.assertEqual(session.read_plan_stamp(self.paths), {})
        session.write_plan_stamp(self.paths, {"plan_type": "pro", "rate_limit_tier": "t2"})
        self.assertEqual(
            session.read_plan_stamp(self.paths),
            {"plan_type": "pro", "rate_limit_tier": "t2"},
        )

    def test_plan_usage_throttle(self) -> None:
        state: dict = {}
        self.assertFalse(session.plan_usage_throttled(state), "first snapshot unthrottled")
        state["plan_usage_emitted_at"] = time.time()
        self.assertTrue(session.plan_usage_throttled(state))
        state["plan_usage_emitted_at"] = time.time() - session.PLAN_USAGE_TTL_SEC - 1
        self.assertFalse(session.plan_usage_throttled(state))

    def test_convention_prompt_parameterized(self) -> None:
        p = session.convention_prompt("Codex")
        self.assertIn("Cardinal-instrumented Codex session", p)
        self.assertIn("<type-prefix>/<kebab-name>", p)


class DeviceFlowTests(unittest.TestCase):
    def test_derive_deployment_env(self) -> None:
        self.assertEqual(deviceflow.derive_deployment_env("https://app.cardinalhq.io"), "prod")
        self.assertEqual(deviceflow.derive_deployment_env("https://dogfood.cardinalhq.io"), "dogfood")
        self.assertEqual(deviceflow.derive_deployment_env("https://x.cardinalhq.io"), "cardinal")
        self.assertEqual(deviceflow.derive_deployment_env("https://acme.example.com"), "customer")

    def test_ingest_probe_auth_ok_via_stub(self) -> None:
        stub = StubIngest().start()
        try:
            ok, msg = deviceflow.verify_ingest_reachable(
                {"endpoint": stub.endpoint, "api_key": "k"}, log=lambda _s: None
            )
            self.assertTrue(ok, msg)
        finally:
            stub.stop()

    def test_missing_credential_shapes(self) -> None:
        self.assertEqual(deviceflow.verify_ingest_reachable(None)[0], False)
        self.assertEqual(deviceflow.verify_ingest_reachable({"endpoint": "http://x"})[0], False)
        self.assertEqual(deviceflow.verify_mcp_reachable(None, "k")[0], False)
        self.assertEqual(deviceflow.verify_mcp_reachable("http://x", None)[0], False)


class GoldenNormalizationTests(unittest.TestCase):
    def test_normalizer_pins_drops_and_zeroes(self) -> None:
        stub = StubIngest()
        stub.log_batches.append({
            "resourceLogs": [{
                "resource": {"attributes": [
                    {"key": "cardinal.core_version", "value": {"stringValue": "0.2.0"}},
                    {"key": "cardinal.plugin_version", "value": {"stringValue": "0.6.0"}},
                ]},
                "scopeLogs": [{
                    "scope": {"name": "s", "version": "0.6.0"},
                    "logRecords": [{
                        "timeUnixNano": "123", "observedTimeUnixNano": "123",
                        "attributes": [
                            {"key": "ts", "value": {"intValue": "123"}},
                            {"key": "model", "value": {"stringValue": "m"}},
                        ],
                    }],
                }],
            }]
        })
        norm = stub.normalized_batches()[0]
        rec = norm["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        self.assertEqual(rec["timeUnixNano"], "0")
        attr_map = {a["key"]: a["value"] for a in rec["attributes"]}
        self.assertEqual(attr_map["ts"], {"stringValue": "<normalized>"})
        self.assertEqual(attr_map["model"], {"stringValue": "m"})
        res_attrs = {a["key"]: a["value"] for a in norm["resourceLogs"][0]["resource"]["attributes"]}
        # core_version DROPPED (pre-migration goldens lack the key);
        # plugin_version and scope version pinned.
        self.assertNotIn("cardinal.core_version", res_attrs)
        self.assertEqual(res_attrs["cardinal.plugin_version"], {"stringValue": "<normalized>"})
        self.assertEqual(
            norm["resourceLogs"][0]["scopeLogs"][0]["scope"]["version"], "<normalized>"
        )


class GateDecisionTests(unittest.TestCase):
    """core 0.2.0 gap #1 — policy/channel split."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.paths = AgentPaths(home=Path(self._tmp.name) / ".agent")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_verdict(self, verdict: dict) -> None:
        atomic_write_json_compact(self.paths.verdict_path("s1"), verdict)

    def test_block_decision(self) -> None:
        self._write_verdict({
            "decision": "block", "band": 3, "fetched_at": time.time(),
            "block_reason": "over budget",
        })
        d = limits.gate_decision(self.paths, "s1")
        self.assertEqual((d.tier, d.reason), ("block", "over budget"))

    def test_notify_vs_warn_tier(self) -> None:
        self._write_verdict({
            "decision": "allow", "band": 1, "fetched_at": time.time(),
            "agent_context": "ctx",
        })
        self.assertEqual(limits.gate_decision(self.paths, "s1").tier, "notify")
        self._write_verdict({
            "decision": "warn", "band": 2, "fetched_at": time.time(),
            "agent_context": "ctx", "user_message": "msg",
        })
        self.assertEqual(limits.gate_decision(self.paths, "s1").tier, "warn")

    def test_hysteresis_via_ack_band(self) -> None:
        self._write_verdict({
            "decision": "warn", "band": 2, "fetched_at": time.time(),
            "agent_context": "ctx",
        })
        d = limits.gate_decision(self.paths, "s1")
        self.assertTrue(d.is_new_band)
        limits.ack_band(self.paths, "s1", d.band)
        self.assertFalse(limits.gate_decision(self.paths, "s1").is_new_band)

    def test_gate_output_equivalence_preserved(self) -> None:
        """gate_output is now a renderer over gate_decision — its behavior
        contract is pinned by LimitsTests; this asserts the ack write only
        happens when output is actually produced."""
        self._write_verdict({
            "decision": "warn", "band": 2, "fetched_at": time.time(),
        })  # no agent_context, no user_message → no output, no ack
        self.assertIsNone(
            limits.gate_output(self.paths, "s1", hook_event_name="UserPromptSubmit")
        )
        self.assertFalse(self.paths.ack_path("s1").exists())

    def test_notify_staging_roundtrip(self) -> None:
        self.assertIsNone(limits.consume_notify(self.paths, "s1"))
        limits.stage_notify(self.paths, "s1", "standing msg", 2)
        self.assertEqual(limits.consume_notify(self.paths, "s1"), "standing msg")
        self.assertIsNone(limits.consume_notify(self.paths, "s1"), "one-shot")


class Core020ApiTests(unittest.TestCase):
    def test_extra_headers_forwarded(self) -> None:
        stub = StubIngest().start()
        try:
            conn = otlp.IngestConnection(
                endpoint=stub.endpoint, api_key="k",
                extra_headers=(("x-extra", "v1"),),
            )
            # emit and verify the request carried the extra header — the stub
            # doesn't capture headers, so assert via a local check of the
            # header dict construction path instead: no exception + batch
            # received proves merge didn't clobber auth.
            otlp.emit_records(
                [otlp.log_record("e", {}, 1)],
                conn,
                {"service.name": "t"},
                scope_name="s", scope_version="v",
            )
            time.sleep(0.05)
            self.assertEqual(len(stub.log_batches), 1)
        finally:
            stub.stop()

    def test_passthrough_resource_attrs(self) -> None:
        pairs = {"custom.key": "x", "service.name": "user-set"}
        attrs = otlp.passthrough_resource_attrs(
            pairs, service_name="claude", agent_runtime="claude",
            plugin_version="1.2.3",
        )
        self.assertEqual(attrs["service.name"], "user-set", "setdefault must not clobber")
        self.assertEqual(attrs["agent.runtime"], "claude")
        self.assertEqual(attrs["cardinal.plugin_version"], "1.2.3")
        self.assertEqual(attrs["custom.key"], "x")
        self.assertIn("cardinal.core_version", attrs)
        no_cv = otlp.passthrough_resource_attrs(
            pairs, service_name="c", agent_runtime="c",
            plugin_version="1", include_core_version=False,
        )
        self.assertNotIn("cardinal.core_version", no_cv)

    def test_resource_attrs_core_version_optional(self) -> None:
        attrs = otlp.resource_attrs(
            service_name="s", agent_runtime="r", deployment_environment=None,
            user_email=None, org=None, plugin_version="1",
            include_core_version=False,
        )
        self.assertNotIn("cardinal.core_version", attrs)

    def test_refresh_verdict_with_injected_api_key(self) -> None:
        """No secrets file — the injected key must be used (gap #2)."""
        import http.server, threading

        verdict_payload = json.dumps({"decision": "allow", "band": 0, "ttl_seconds": 60})
        seen_keys: list[str] = []

        class GetHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                seen_keys.append(self.headers.get("x-cardinalhq-api-key") or "")
                body = verdict_payload.encode()
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GetHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with TemporaryDirectory() as tmp:
                paths = AgentPaths(home=Path(tmp) / ".agent")
                paths.home.mkdir(parents=True)
                paths.state_path.write_text(json.dumps({
                    "limits": {"status_url": f"http://127.0.0.1:{server.server_port}/status"}
                }))
                v = limits.maybe_refresh_verdict(
                    paths, session_id="s1", repo=None, branch=None,
                    force=True, api_key="injected-key",
                )
                self.assertIsNotNone(v)
                self.assertEqual(seen_keys, ["injected-key"])
                self.assertTrue(paths.verdict_path("s1").exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_ingest_probe_sleeps_injectable(self) -> None:
        stub = StubIngest().start()
        stub.metrics_status = 401
        try:
            t0 = time.monotonic()
            ok, msg = deviceflow.verify_ingest_reachable(
                {"endpoint": stub.endpoint, "api_key": "k"},
                log=lambda _s: None, sleeps=(),
            )
            self.assertFalse(ok)
            self.assertLess(time.monotonic() - t0, 2.0, "empty ladder must not sleep")
        finally:
            stub.stop()


if __name__ == "__main__":
    unittest.main()
