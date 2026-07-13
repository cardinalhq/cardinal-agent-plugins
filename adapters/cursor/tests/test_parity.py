"""Parity + behavioral tests for the migrated Cursor adapter.

Two layers:

1. GoldenParityTests — runs the migrated hook script against the SAME
   synthetic fixtures used to capture tests/goldens/*.json from the
   pre-migration shipped plugin (cardinal-cursor-plugin v0.2.0), and
   asserts byte-equal normalized OTLP output and hook stdout.

2. Behavioral tests ported from the source repo's
   tests/test_cardinal_plugin.py — contract-parity fixtures (initiative,
   worktree stripping, command detection, bash classes), Cursor tool
   normalization, the Divergence-E three-tier limits gate (block /
   override-downgrade / notify staging / strict-warn escalation /
   hysteresis), notify consumption, JSON managed-block round-trips for
   mcp.json + hooks.json, resource stamping, and the length-only
   thought/response emissions.

Run:  python3 -m unittest discover -s adapters/cursor/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = TESTS_DIR.parent
REPO_ROOT = ADAPTER_DIR.parent.parent
HOOKS_DIR = ADAPTER_DIR / "hooks"
SCRIPTS_DIR = ADAPTER_DIR / "scripts"
HOOK_SCRIPT = HOOKS_DIR / "cardinal-cursor-telemetry.py"

sys.path.insert(0, str(TESTS_DIR))
import fixtures  # noqa: E402


def _ensure_vendored_core() -> None:
    """The vendored hooks/cardinal_core copy is a build output (gitignored);
    materialize it before importing the hook module."""
    if (HOOKS_DIR / "cardinal_core" / "__init__.py").exists():
        return
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "cursor"],
        check=True,
        capture_output=True,
    )


_ensure_vendored_core()


def _load_module(name: str, path: Path):
    """Load a Python file whether or not it has a .py extension. Cursor
    scripts (`cardinal-connect`, etc.) have no extension by design."""
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(HOOKS_DIR))
        except ValueError:
            pass
    return module


class _CursorHome:
    """Scoped ~/.cursor override so tests never touch the real dotdir."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cardinal-cursor-test-"))
        self.cursor = self.tmp / ".cursor"
        self.cursor.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.tmp)})
        self._env.start()

    def close(self) -> None:
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Golden parity — the migration's core guarantee
# ---------------------------------------------------------------------------

class GoldenParityTests(unittest.TestCase):
    """The migrated hook must emit byte-equal normalized OTLP (and hook
    stdout) to the goldens captured from the pre-migration shipped
    plugin. Fixtures, sandbox, and normalization are shared via
    fixtures.py; the goldens themselves are frozen in tests/goldens/."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.stub = fixtures.StubIngest().start()
        cls.tmp = tempfile.TemporaryDirectory(prefix="cardinal-cursor-parity-")
        sandbox = fixtures.build_sandbox(Path(cls.tmp.name), cls.stub.endpoint)
        cls.results = fixtures.run_all_steps(HOOK_SCRIPT, cls.stub, sandbox)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.stub.stop()
        cls.tmp.cleanup()

    def _golden(self, name: str) -> dict:
        return json.loads((fixtures.GOLDENS_DIR / f"{name}.json").read_text())

    def _assert_step(self, name: str) -> None:
        got = json.loads(json.dumps(self.results[name]))
        self.assertEqual(got, self._golden(name))

    def test_all_fixture_steps_present(self) -> None:
        golden_names = sorted(p.stem for p in fixtures.GOLDENS_DIR.glob("*.json"))
        self.assertEqual(sorted(self.results), golden_names)

    def test_01_session_start_convention_context(self) -> None:
        self._assert_step("01-sessionStart")

    def test_02_before_submit_prompt_git_state(self) -> None:
        self._assert_step("02-beforeSubmitPrompt-plain")

    def test_03_post_tool_use_shell_compound_bash_class(self) -> None:
        self._assert_step("03-postToolUse-shell-compound")

    def test_04_post_tool_use_mcp_qualified_name(self) -> None:
        self._assert_step("04-postToolUse-mcp")

    def test_05_post_tool_use_file_target_and_notify_surfacing(self) -> None:
        self._assert_step("05-postToolUse-read-file-notify")

    def test_06_before_submit_prompt_slash_command(self) -> None:
        self._assert_step("06-beforeSubmitPrompt-command")

    def test_07_subagent_stop_usage(self) -> None:
        self._assert_step("07-subagentStop")

    def test_08_after_agent_thought_length_only(self) -> None:
        self._assert_step("08-afterAgentThought")

    def test_09_after_agent_response_length_only(self) -> None:
        self._assert_step("09-afterAgentResponse")

    def test_10_pre_compact_plan_usage_slice(self) -> None:
        self._assert_step("10-preCompact")


# ---------------------------------------------------------------------------
# Behavioral tests ported from the source repo's test_cardinal_plugin.py
# ---------------------------------------------------------------------------

class ContractParityTests(unittest.TestCase):
    """These fixtures MUST mirror cardinal-codex-plugin's suite verbatim.
    Any diff here is a spec violation — fix the adapter, not the test."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry", HOOK_SCRIPT)

    def test_resolve_initiative_branch_fixtures(self) -> None:
        cases = [
            ("main", (None, "research")),
            ("master", (None, "research")),
            ("develop", (None, "research")),
            ("HEAD", (None, "research")),
            ("feat/outcomes-observability", ("outcomes-observability", "feature")),
            ("feature/outcomes", ("outcomes", "feature")),
            ("perf/render-fast", ("render-fast", "feature")),
            ("fix/login-crash", ("login-crash", "bugfix")),
            ("bugfix/login", ("login", "bugfix")),
            ("refactor/auth-token-rotation", ("auth-token-rotation", "refactor")),
            ("cleanup/dead-code", ("dead-code", "refactor")),
            ("infra/observability", ("observability", "infra")),
            ("chore/upgrade-deps", ("upgrade-deps", "infra")),
            ("test/gate-suite", ("gate-suite", "infra")),
            ("ci/pin-actions", ("pin-actions", "infra")),
            ("build/docker", ("docker", "infra")),
            ("docs/telemetry", ("telemetry", "infra")),
            ("research/data-pipeline-spike", ("data-pipeline-spike", "research")),
            ("spike/prototype", ("prototype", "research")),
            ("weird-branch-name", ("weird-branch-name", "feature")),
            ("someone/foo/bar", ("someone/foo/bar", "feature")),
        ]
        for branch, expected in cases:
            with self.subTest(branch=branch):
                self.assertEqual(self.hook.resolve_initiative(branch), expected)

    def test_worktree_noise_stripping(self) -> None:
        cases = [
            ("worktree-fix-1018-github-app-repo-picker", "github-app-repo-picker"),
            ("worktree-feat-42-outcomes-observability", "outcomes-observability"),
            ("worktree-bug-100-login", "login"),
            ("worktree-1234-simple", "simple"),
            ("worktree-pr-77-review-comments", "review-comments"),
            ("worktree-fix-only", "only"),
            ("regular-branch", "regular-branch"),
            ("feat/scope", "feat/scope"),
            ("worktree-fix-1234", "worktree-fix-1234"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.hook.strip_worktree_noise(raw), expected)

    def test_detect_command_forms(self) -> None:
        self.assertEqual(self.hook.detect_command("/code-review --fix"), "code-review")
        self.assertEqual(self.hook.detect_command("  /docs help"), "docs")
        self.assertIsNone(self.hook.detect_command("please run /docs later"))
        self.assertEqual(self.hook.detect_command("<command-name>/simplify</command-name>"), "simplify")
        self.assertEqual(self.hook.detect_command("<command-name>verify</command-name>"), "verify")
        self.assertIsNone(self.hook.detect_command(None))
        self.assertIsNone(self.hook.detect_command(42))

    def test_bash_class_fixtures(self) -> None:
        cases = [
            ("ls -la", ("file-read", False)),
            ("rm -rf build/", ("file-write", False)),
            ("git status", ("git-read", False)),
            ("git commit -m foo", ("git-write", False)),
            ("git status && git commit", ("git-write", True)),
            ("pytest -k thing", ("test", False)),
            ("make build && make test", ("build", False)),
            ("tsc && pytest -k thing", ("build", True)),
            ("curl https://example.com", ("network", False)),
            ("sudo apt-get install foo", ("pkg", False)),
            ("cd /tmp", ("other", False)),
            ("FOO=bar rm file", ("file-write", False)),
            ("go test ./...", ("test", False)),
            ("go build ./...", ("build", False)),
            ("npm install", ("pkg", False)),
            ("cargo add serde", ("pkg", False)),
            ("cat foo | grep bar", ("file-read", False)),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(self.hook.classify_bash_command(command), expected)


class ToolNormalizationTests(unittest.TestCase):
    """Cursor-shaped tool inputs → normalized (display_name, params, target)."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_norm", HOOK_SCRIPT)

    def test_shell_tool_names_route_to_bash(self) -> None:
        for raw in ("run_terminal_cmd", "run_shell_command", "shell", "terminal"):
            display, extra, _ = self.hook.normalize_tool_name(raw, {"command": "ls -la"})
            self.assertEqual(display, "Bash")
            self.assertEqual(extra["full_command"], "ls -la")
            self.assertEqual(extra["bash_command"], "ls")

    def test_mcp_prefixed_names_split(self) -> None:
        display, extra, _ = self.hook.normalize_tool_name("mcp__cardinal__lakerunner__list_services", {})
        self.assertEqual(display, "mcp_tool")
        self.assertEqual(extra["mcp_server_name"], "cardinal")
        self.assertEqual(extra["mcp_tool_name"], "lakerunner__list_services")

    def test_unknown_tool_passes_through(self) -> None:
        display, extra, _ = self.hook.normalize_tool_name("my_custom_tool", {"path": "/foo"})
        self.assertEqual(display, "my_custom_tool")
        self.assertEqual(extra, {})


class LimitsGateTests(unittest.TestCase):
    """Three-tier resolution from docs/specs/cursor-parity.md Divergence E,
    now built on core limits primitives with the Cursor channel mapping
    kept adapter-side."""

    def setUp(self) -> None:
        self.home = _CursorHome()
        # Fresh load so the module-level PATHS picks up the patched HOME.
        self.hook = _load_module("cursor_telemetry_gate", HOOK_SCRIPT)

        state = {
            "ingest_endpoint": "https://ingest.example",
            "limits": {"status_url": "https://limits.example/status", "enabled": True},
        }
        (self.home.cursor / "cardinal.json").write_text(json.dumps(state))
        (self.home.cursor / "cardinal-secrets.json").write_text(
            json.dumps({"ingest_api_key": "abc"})
        )
        self.conv = "conv-1"

    def tearDown(self) -> None:
        self.home.close()

    def _write_verdict(self, verdict: dict) -> None:
        v = dict(verdict)
        v.setdefault("fetched_at", time.time())
        path = self.hook.PATHS.verdict_path(self.conv)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(v))

    def test_block_verdict_emits_continue_false_and_user_message(self) -> None:
        self._write_verdict({"decision": "block", "band": 3,
                             "user_message": "You've hit the session cap.",
                             "block_reason": "Session cap reached."})
        out = self.hook.limits_gate_output(self.conv)
        self.assertEqual(out, {"continue": False, "user_message": "Session cap reached."})

    def test_block_override_downgrades_to_notify_staging(self) -> None:
        self._write_verdict({"decision": "block", "band": 3,
                             "block_reason": "Session cap.",
                             "user_message": "You've hit the cap.",
                             "agent_context": "Budget context here."})
        override = self.hook.PATHS.override_path(self.conv)
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("{}")

        out = self.hook.limits_gate_output(self.conv)
        self.assertIsNone(out)  # No block emitted; staged for postToolUse.
        staged = json.loads(self.hook.notify_path(self.conv).read_text())
        self.assertIn("Budget context here.", staged["message"])
        self.assertIn("You've hit the cap.", staged["message"])

    def test_notify_band_stages_agent_context_only(self) -> None:
        self._write_verdict({"decision": "allow", "band": 1,
                             "agent_context": "You are at 60% of session budget."})
        out = self.hook.limits_gate_output(self.conv)
        self.assertIsNone(out)
        staged = json.loads(self.hook.notify_path(self.conv).read_text())
        self.assertEqual(staged["message"], "You are at 60% of session budget.")

    def test_strict_warn_escalates_to_block(self) -> None:
        self._write_verdict({"decision": "warn", "band": 2,
                             "user_message": "Careful — approaching cap."})
        with mock.patch.dict(os.environ, {"CARDINAL_CURSOR_STRICT_WARN": "1"}):
            out = self.hook.limits_gate_output(self.conv)
        self.assertIsNotNone(out)
        self.assertFalse(out["continue"])
        self.assertIn("Careful — approaching cap.", out["user_message"])

    def test_warn_hysteresis_only_stages_once_per_band(self) -> None:
        self._write_verdict({"decision": "warn", "band": 2,
                             "user_message": "Slow down.", "agent_context": "ctx"})
        self.assertIsNone(self.hook.limits_gate_output(self.conv))
        self.assertTrue(self.hook.notify_path(self.conv).exists())
        # Simulate the notify being consumed by postToolUse.
        self.hook.notify_path(self.conv).unlink()
        # Same band, second turn: ack already recorded → no re-stage.
        self.assertIsNone(self.hook.limits_gate_output(self.conv))
        self.assertFalse(self.hook.notify_path(self.conv).exists())

    def test_stale_warn_verdict_fails_open(self) -> None:
        self._write_verdict({"decision": "warn", "band": 2,
                             "user_message": "old", "agent_context": "old",
                             "fetched_at": time.time() - 11 * 60})
        self.assertIsNone(self.hook.limits_gate_output(self.conv))
        self.assertFalse(self.hook.notify_path(self.conv).exists())


class NotifyConsumeTests(unittest.TestCase):
    """postToolUse picks up the staged notify once, then removes it."""

    def setUp(self) -> None:
        self.home = _CursorHome()
        self.hook = _load_module("cursor_telemetry_nc", HOOK_SCRIPT)

    def tearDown(self) -> None:
        self.home.close()

    def test_consume_notify_reads_once(self) -> None:
        conv = "conv-2"
        path = self.hook.notify_path(conv)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"message": "hello"}))
        self.assertEqual(self.hook.consume_notify(conv), "hello")
        # File is gone; second consume returns None.
        self.assertIsNone(self.hook.consume_notify(conv))

    def test_consume_notify_missing_is_none(self) -> None:
        self.assertIsNone(self.hook.consume_notify("nonexistent"))


class JsonManagedBlockTests(unittest.TestCase):
    """Round-trip: connect writes → disconnect strips → foreign content
    preserved."""

    def setUp(self) -> None:
        self.home = _CursorHome()
        self.connect = _load_module("cardinal_connect", SCRIPTS_DIR / "cardinal-connect")
        self.disconnect = _load_module("cardinal_disconnect", SCRIPTS_DIR / "cardinal-disconnect")

    def tearDown(self) -> None:
        self.home.close()

    def test_mcp_write_then_strip_preserves_foreign_entries(self) -> None:
        path = self.home.cursor / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"other": {"url": "https://foreign"}}}))

        self.connect.write_mcp_config(path, "https://mcp.example", "key")
        data = json.loads(path.read_text())
        self.assertIn("other", data["mcpServers"])
        self.assertTrue(data["mcpServers"]["cardinal"]["cardinalManaged"])

        self.disconnect.remove_mcp_entry(path)
        data = json.loads(path.read_text())
        self.assertIn("other", data["mcpServers"])
        self.assertNotIn("cardinal", data["mcpServers"])

    def test_unmanaged_cardinal_entry_is_refused(self) -> None:
        path = self.home.cursor / "mcp.json"
        path.write_text(json.dumps({
            "mcpServers": {"cardinal": {"url": "https://user-wrote-this"}}
        }))
        with self.assertRaises(SystemExit):
            self.connect.write_mcp_config(path, "https://mcp.example", "key")

    def test_hooks_write_then_strip_preserves_foreign_hooks(self) -> None:
        path = self.home.cursor / "hooks.json"
        path.write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": "echo foreign", "type": "command"}]
            }
        }))
        self.connect.write_hooks_config(path)
        data = json.loads(path.read_text())
        session_hooks = data["hooks"]["sessionStart"]
        cmds = [h["command"] for h in session_hooks]
        self.assertTrue(any("echo foreign" in c for c in cmds))
        self.assertTrue(any("cardinal-cursor-plugin" in c for c in cmds))

        self.disconnect.remove_hooks_config(path)
        data = json.loads(path.read_text())
        cmds = [h["command"] for h in data["hooks"].get("sessionStart", [])]
        self.assertTrue(any("echo foreign" in c for c in cmds))
        self.assertFalse(any("cardinal-cursor-plugin" in c for c in cmds))

    def test_hooks_registers_all_managed_events(self) -> None:
        path = self.home.cursor / "hooks.json"
        self.connect.write_hooks_config(path)
        data = json.loads(path.read_text())
        events = set(data["hooks"].keys())
        self.assertEqual(events, {
            "sessionStart", "beforeSubmitPrompt", "postToolUse",
            "preCompact", "stop", "subagentStop",
            "afterAgentResponse", "afterAgentThought",
        })


class ManifestTests(unittest.TestCase):
    def test_plugin_json_shape(self) -> None:
        manifest = json.loads((ADAPTER_DIR / ".cursor-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "cardinal-cursor-plugin")
        self.assertRegex(manifest["version"], r"^\d+\.\d+\.\d+")
        self.assertEqual(manifest["license"], "Apache-2.0")

    def test_plugin_version_loads_from_manifest(self) -> None:
        for mod in list(sys.modules):
            if mod == "_plugin_version":
                del sys.modules[mod]
        pv = _load_module("_plugin_version", HOOKS_DIR / "_plugin_version.py")
        pv.plugin_version.cache_clear()
        version = pv.plugin_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+")


class ResourceAttrsTests(unittest.TestCase):
    """Base OTel resource attributes stamp Cursor identity from the hook
    payload's base fields (model, model_id, model_params, cursor_version)."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_ra", HOOK_SCRIPT)

    def test_base_attrs_without_payload(self) -> None:
        attrs = self.hook.resource_attrs({"deployment_environment": "prod",
                                          "user_email": "a@b.com",
                                          "org_slug": "acme"})
        self.assertEqual(attrs["service.name"], "cursor")
        self.assertEqual(attrs["deployment.environment"], "prod")
        self.assertEqual(attrs["user.email"], "a@b.com")
        self.assertEqual(attrs["cardinal.org"], "acme")
        self.assertNotIn("cursor.model", attrs)
        self.assertNotIn("cursor.version", attrs)

    def test_payload_stamps_model_and_version(self) -> None:
        payload = {
            "model": "claude-3.5-sonnet",
            "model_id": "anthropic/claude-3.5-sonnet",
            "model_params": {"temperature": 0.2, "max_tokens": 4096},
            "cursor_version": "0.44.11",
        }
        attrs = self.hook.resource_attrs({}, payload)
        self.assertEqual(attrs["cursor.model"], "claude-3.5-sonnet")
        self.assertEqual(attrs["cursor.model_id"], "anthropic/claude-3.5-sonnet")
        self.assertEqual(attrs["cursor.version"], "0.44.11")
        self.assertEqual(json.loads(attrs["cursor.model_params"]),
                         {"temperature": 0.2, "max_tokens": 4096})

    def test_payload_string_model_params_passthrough(self) -> None:
        attrs = self.hook.resource_attrs({}, {"model_params": "opaque"})
        self.assertEqual(attrs["cursor.model_params"], "opaque")

    def test_payload_missing_fields_skipped(self) -> None:
        attrs = self.hook.resource_attrs({}, {"model": ""})
        self.assertNotIn("cursor.model", attrs)


class PreCompactEmitTests(unittest.TestCase):
    """preCompact payload → cardinal.plan_usage (context-window slice)."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_pc", HOOK_SCRIPT)

    def test_pre_compact_emits_plan_usage(self) -> None:
        payload = {
            "conversation_id": "conv-1",
            "trigger": "auto",
            "context_usage_percent": 87,
            "context_tokens": 174_000,
            "context_window_size": 200_000,
            "message_count": 42,
            "messages_to_compact": 30,
            "is_first_compaction": True,
            "model": "claude-3.5-sonnet",
            "cursor_version": "0.44.11",
        }
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda records, payload=None: captured.append((records, payload))):
            self.hook.handle_pre_compact(payload)
        self.assertEqual(len(captured), 1)
        records, forwarded_payload = captured[0]
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["body"]["stringValue"], "cardinal.plan_usage")
        attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
        self.assertEqual(attrs["plan.compact_trigger"], "auto")
        self.assertEqual(attrs["plan.context_tokens"], "174000")
        self.assertEqual(attrs["plan.context_window"], "200000")
        self.assertEqual(attrs["plan.messages_to_compact"], "30")
        self.assertTrue(attrs["plan.is_first_compaction"])
        self.assertIs(forwarded_payload, payload)

    def test_pre_compact_no_conv_id_no_emit(self) -> None:
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda *a, **kw: captured.append(a)):
            self.hook.handle_pre_compact({"trigger": "auto"})
        self.assertEqual(captured, [])


class ThoughtResponseEmitTests(unittest.TestCase):
    """afterAgentThought / afterAgentResponse emit length-only events."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_tr", HOOK_SCRIPT)

    def test_thought_emits_duration_and_len_never_text(self) -> None:
        payload = {
            "conversation_id": "conv-1",
            "duration_ms": 1234,
            "text": "internal chain-of-thought, do not emit",
        }
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda records, payload=None: captured.append(records)):
            self.hook.handle_after_agent_thought(payload)
        self.assertEqual(len(captured), 1)
        rec = captured[0][0]
        self.assertEqual(rec["body"]["stringValue"], "cardinal.turn_thought")
        attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
        self.assertEqual(attrs["thought.duration_ms"], "1234")
        self.assertEqual(attrs["thought.text_len"], str(len(payload["text"])))
        for a in rec["attributes"]:
            for v in a["value"].values():
                self.assertNotIn("chain-of-thought", str(v))

    def test_response_emits_text_len_never_text(self) -> None:
        payload = {
            "conversation_id": "conv-1",
            "text": "final response body containing user code",
        }
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda records, payload=None: captured.append(records)):
            self.hook.handle_after_agent_response(payload)
        self.assertEqual(len(captured), 1)
        rec = captured[0][0]
        self.assertEqual(rec["body"]["stringValue"], "cardinal.turn_response")
        attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
        self.assertEqual(attrs["response.text_len"], str(len(payload["text"])))
        for a in rec["attributes"]:
            for v in a["value"].values():
                self.assertNotIn("user code", str(v))

    def test_response_no_conv_id_no_emit(self) -> None:
        captured: list = []
        with mock.patch.object(self.hook, "emit_records",
                               side_effect=lambda *a, **kw: captured.append(a)):
            self.hook.handle_after_agent_response({"text": "x"})
        self.assertEqual(captured, [])


class SubagentTests(unittest.TestCase):
    """Cursor's documented subagentStop payload keys become subagent_usage."""

    def setUp(self) -> None:
        self.hook = _load_module("cursor_telemetry_sa", HOOK_SCRIPT)

    def test_description_prefers_description_then_task_then_summary(self) -> None:
        f = self.hook.subagent_description_from_payload
        self.assertEqual(f({"description": "primary", "task": "t", "summary": "s"}), "primary")
        self.assertEqual(f({"task": "t", "summary": "s"}), "t")
        self.assertEqual(f({"summary": "s"}), "s")
        self.assertIsNone(f({}))

    def test_description_truncated_to_160_chars(self) -> None:
        long = "x" * 500
        self.assertEqual(len(self.hook.subagent_description_from_payload({"description": long})), 160)


if __name__ == "__main__":
    unittest.main()
