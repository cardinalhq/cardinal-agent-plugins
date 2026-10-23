"""The Copilot hook script's event handling: unconnected mode, telemetry
when connected, the spend-limits gate, MCP tool-name canonicalization, and
robustness.

  - Unconnected (no ~/.copilot/cardinal.json): sessionStart prints only the
    connect hint; postToolUse / postToolUseFailure capture evidence locally;
    every other event is silent and nothing reaches the network.
  - Connected: userPromptSubmitted emits cardinal.git_state; postToolUse
    emits cardinal.turn_tool + tool_result (inline here); preCompact emits
    cardinal.plan_usage; subagentStop emits cardinal.subagent_usage.
  - Spend limits: a block (or a warn under CARDINAL_COPILOT_STRICT_WARN=1)
    denies on preToolUse (context stamping is test_copilot_investigation.py's); a warn/notify is staged on userPromptSubmitted and
    surfaces once on the next postToolUse.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_hooks.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from fixtures import ADAPTER, HOOK, SID, VERSION, HookCase, clean_env, load_script

HOOKS_JSON = ADAPTER / "hooks" / "hooks.json"


class UnconnectedTests(HookCase):
    def test_session_start_prints_only_the_connect_hint(self):
        first = self.context(self.run_hook("sessionStart", self.payload(source="startup")))
        self.assertTrue(first.startswith("Tell the user once, briefly: Cardinal is not connected"), first)
        self.assertIn("scripts/cardinal-connect", first)
        self.assertNotIn("Cardinal-instrumented", first)
        second = self.context(self.run_hook("sessionStart", self.payload(source="new")))
        self.assertNotIn("Tell the user once", second)
        self.assertIn("Mention it only if the user asks", second)
        self.assertTrue((self.home / ".cardinal" / "connect-hint-copilot").exists())
        self.assertEqual(self.stub.log_batches, [])

    def test_resume_never_tells_the_user(self):
        ctx = self.context(self.run_hook("sessionStart", self.payload(source="resume")))
        self.assertNotIn("Tell the user once", ctx)
        self.assertFalse((self.home / ".cardinal" / "connect-hint-copilot").exists())

    def test_silent_events_print_and_send_nothing(self):
        for event, payload in (
            ("userPromptSubmitted", self.payload(prompt="fix the build")),
            ("preToolUse", self.payload(toolName="bash", toolArgs={"command": "ls"})),
            ("agentStop", self.payload(stopReason="end_turn")),
            ("subagentStart", self.payload(agentName="explore", agentDisplayName="Explore")),
            ("preToolUse", self.payload(toolName="cardinal-storyboard__create", toolArgs={"question": "q"})),
            ("subagentStop", self.payload(agentName="explore", agentType="explore")),
            ("preCompact", self.payload(trigger="auto")),
        ):
            with self.subTest(event=event):
                res = self.run_hook(event, payload)
                self.assertEqual(res.stdout, "")
                self.assertEqual(res.stderr, "")
        self.assertEqual(self.stub.log_batches, [])
        self.assertFalse((self.copilot / "cardinal").exists())

    def test_tool_calls_are_captured_locally(self):
        ctx = self.context(self.run_hook("postToolUse", self.tool(args={"command": "npm test"}, text="12 passing")))
        self.assertRegex(ctx, r"^\[evidence:ev_[0-9a-f]{12}\]")
        spool = self.home / ".cardinal" / "evidence" / SID
        self.assertEqual(len(list(spool.glob("ev_*.json"))), 1)
        failed = self.payload(toolName="bash", toolArgs={"command": "make"}, error="exit code 2")
        ctx = self.context(self.run_hook("postToolUseFailure", failed))
        self.assertRegex(ctx, r"^\[evidence:ev_[0-9a-f]{12}\]$")
        self.assertEqual(len(list(spool.glob("ev_*.json"))), 2)
        self.assertEqual(self.stub.log_batches, [])
        self.assertFalse((self.copilot / "cardinal").exists())

    def test_mcp_env_alone_does_not_count_as_connected(self):
        # The plugin's own rule is the state file; an exported key is not a
        # connection (the developer's shell may export one).
        res = self.run_hook("userPromptSubmitted", self.payload(prompt="hi"),
                            CARDINAL_MCP_API_KEY="ck_env", CARDINAL_MCP_URL="http://127.0.0.1:9/mcp")
        self.assertEqual(res.stdout, "")
        self.assertEqual(self.stub.log_batches, [])


class ConnectedTelemetryTests(HookCase):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_user_prompt_emits_git_state_with_the_initiative(self):
        res = self.run_hook("userPromptSubmitted", self.payload(prompt="/cardinal-status check it"))
        self.assertEqual(res.stdout, "")
        [(resource, attrs)] = self.records("cardinal.git_state")
        self.assertEqual(attrs["session_id"], SID)
        self.assertEqual(attrs["cardinal_branch"], "feat/copilot-thing")
        self.assertEqual(attrs["cardinal_initiative_name"], "copilot-thing")
        self.assertEqual(attrs["cardinal_initiative_type"], "feature")
        self.assertEqual(attrs["cardinal_repo"], "acme/widgets")
        self.assertEqual(attrs["cardinal_cwd"], str(self.repo))
        self.assertEqual(len(attrs["cardinal_head_sha"]), 40)
        self.assertEqual(resource["service.name"], "copilot")
        self.assertEqual(resource["agent.runtime"], "copilot")
        self.assertEqual(resource["user.email"], "dev@example.com")
        self.assertEqual(resource["cardinal.org"], "acme")
        self.assertEqual(resource["cardinal.plugin_version"], VERSION)
        self.assertEqual(self.stub.log_batches[0]["resourceLogs"][0]["scopeLogs"][0]["scope"]["name"],
                         "cardinal-copilot-plugin")

    def test_user_prompt_outside_a_repo_emits_nothing(self):
        plain = self.base / "plain"
        plain.mkdir()
        self.run_hook("userPromptSubmitted", self.payload(prompt="hi", cwd=str(plain)), cwd=plain)
        self.assertEqual(self.records("cardinal.git_state"), [])

    def test_bash_tool_emits_turn_tool_and_tool_result(self):
        self.run_hook("userPromptSubmitted", self.payload(prompt="run the tests"))
        ctx = self.context(self.run_hook("postToolUse", self.tool(args={"command": "pytest -q"},
                                                                  text="3 passed")))
        self.assertRegex(ctx, r"^\[evidence:ev_[0-9a-f]{12}\]")
        [(resource, turn)] = self.records("cardinal.turn_tool")
        self.assertEqual(turn["tool_name"], "Bash")
        self.assertEqual(turn["bash_class"], "test")
        self.assertEqual(turn["session_id"], SID)
        self.assertEqual(int(turn["user_turn_seq"]), 1)
        self.assertEqual(int(turn["tool_seq"]), 0)
        self.assertEqual(resource["agent.runtime"], "copilot")
        [(_, result)] = self.records("tool_result")
        self.assertEqual(result["tool_name"], "Bash")
        self.assertEqual(result["success"], "true")
        self.assertEqual(result["agent_runtime"], "copilot")
        self.assertEqual(json.loads(result["tool_input"]), {"command": "pytest -q"})
        # The tool sequence advances per call.
        self.run_hook("postToolUse", self.tool(args={"command": "tsc && pytest"}))
        turn2 = self.records("cardinal.turn_tool")[-1][1]
        self.assertEqual(int(turn2["tool_seq"]), 1)
        self.assertEqual(turn2["bash_class"], "build")
        self.assertTrue(turn2["bash_multi"])

    def test_tool_args_as_a_json_string(self):
        self.run_hook("postToolUse", self.tool(args=json.dumps({"command": "git status"})))
        [(_, result)] = self.records("tool_result")
        self.assertEqual(json.loads(result["tool_input"]), {"command": "git status"})
        self.assertEqual(self.records("cardinal.turn_tool")[0][1]["tool_name"], "Bash")

    def test_success_from_result_type_and_exit_code_text(self):
        cases = [
            ({"result_type": "success", "text": "fine"}, "true"),
            ({"result_type": "failure", "text": "boom"}, "false"),
            ({"result_type": "rejected", "text": None}, "false"),
            ({"result_type": "denied", "text": None}, "false"),
            ({"result_type": "success", "text": "<exited with exit code 1>"}, "false"),
            ({"result_type": "success", "text": "build ok\nexit code 0"}, "true"),
            ({"result_type": "success", "text": "Process exited with code 127"}, "false"),
        ]
        for i, (kw, want) in enumerate(cases):
            with self.subTest(case=kw):
                self.run_hook("postToolUse", self.tool(args={"command": f"step {i}"}, **kw))
                self.assertEqual(self.records("tool_result")[-1][1]["success"], want)

    def test_snake_case_fallbacks(self):
        payload = {"session_id": SID, "cwd": str(self.repo), "tool_name": "view",
                   "tool_input": {"path": "README.md"},
                   "tool_result": {"result_type": "success", "text_result_for_llm": "fixture"}}
        self.run_hook("postToolUse", payload)
        [(_, turn)] = self.records("cardinal.turn_tool")
        self.assertEqual(turn["tool_name"], "Read")
        self.assertEqual(turn["target"], "README.md")

    def test_mcp_servers_are_split_longest_name_first(self):
        cases = [
            ("cardinal-behavior-get_behavior_sdk", "cardinal-behavior", "get_behavior_sdk"),
            ("cardinal-lakerunner__list_services", "cardinal", "lakerunner__list_services"),
            ("github-mcp-server-search_code", "github-mcp-server", "search_code"),
        ]
        for raw, server, tool in cases:
            with self.subTest(raw=raw):
                self.run_hook("postToolUse", self.tool(name=raw, args={"q": "x"}, text='{"ok": true}'))
                turn = self.records("cardinal.turn_tool")[-1][1]
                self.assertEqual(turn["tool_name"], f"mcp__{server}__{tool}")
                self.assertEqual(turn["mcp_server_name"], server)
                self.assertEqual(turn["mcp_tool_name"], tool)
                result = self.records("tool_result")[-1][1]
                self.assertEqual(result["tool_name"], "mcp_tool")
                self.assertEqual(result["mcp_server_name"], server)
                self.assertEqual(result["mcp_tool_name"], tool)

    def test_a_server_named_in_mcp_config_is_split(self):
        # Unknown until configured: the raw name is kept.
        self.run_hook("postToolUse", self.tool(name="my-srv-query", args={"q": "up"}))
        self.assertEqual(self.records("cardinal.turn_tool")[-1][1]["tool_name"], "my-srv-query")
        (self.copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {
            "my": {"command": "x"}, "my-srv": {"type": "http", "url": "https://example.com/mcp"}}}))
        self.run_hook("postToolUse", self.tool(name="my-srv-query", args={"q": "up"}))
        turn = self.records("cardinal.turn_tool")[-1][1]
        self.assertEqual((turn["mcp_server_name"], turn["mcp_tool_name"]), ("my-srv", "query"))

    def test_workspace_and_copilot_home_servers_are_split(self):
        (self.repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"ws-srv": {"command": "x"}}}))
        self.run_hook("postToolUse", self.tool(name="ws-srv-do_it", args={}))
        self.assertEqual(self.records("cardinal.turn_tool")[-1][1]["mcp_server_name"], "ws-srv")
        alt = self.base / "copilot-home"
        alt.mkdir()
        (alt / "mcp-config.json").write_text(json.dumps({"mcpServers": {"alt-srv": {"command": "x"}}}))
        self.run_hook("postToolUse", self.tool(name="alt-srv-get", args={}), COPILOT_HOME=str(alt))
        self.assertEqual(self.records("cardinal.turn_tool")[-1][1]["mcp_server_name"], "alt-srv")

    def test_pre_compact_emits_plan_usage(self):
        self.assertEqual(self.run_hook("preCompact", self.payload(trigger="auto",
                                                                  customInstructions="")).stdout, "")
        [(_, attrs)] = self.records("cardinal.plan_usage")
        self.assertEqual(attrs["session_id"], SID)
        self.assertEqual(attrs["plan.compact_trigger"], "auto")

    def test_subagent_stop_emits_subagent_usage(self):
        payload = self.payload(agentName="explore", agentType="explore", agentDisplayName="  Explore agent ",
                               stopReason="end_turn")
        self.assertEqual(self.run_hook("subagentStop", payload).stdout, "")
        [(_, attrs)] = self.records("cardinal.subagent_usage")
        self.assertEqual(attrs["session_id"], SID)
        self.assertEqual(attrs["agent_runtime"], "copilot")
        self.assertEqual(attrs["subagent_type"], "explore")
        self.assertEqual(attrs["subagent_description"], "Explore agent")
        self.assertEqual(attrs["subagent_status"], "end_turn")

    def test_agent_stop_prints_nothing(self):
        for payload in (self.payload(stopReason="end_turn"), self.payload(stopHookActive=True), {}):
            self.assertEqual(self.run_hook("agentStop", payload).stdout, "")

    def test_missing_session_id_sends_nothing(self):
        for event, payload in (("userPromptSubmitted", {"cwd": str(self.repo), "prompt": "hi"}),
                               ("preCompact", {"trigger": "manual"}),
                               ("subagentStop", {"agentName": "x"})):
            self.run_hook(event, payload)
        self.run_hook("postToolUse", {"cwd": str(self.repo), "toolName": "bash", "toolArgs": {"command": "ls"},
                                      "toolResult": {"resultType": "success", "textResultForLlm": "x"}})
        self.assertEqual(self.stub.log_batches, [])


class SpendLimitTests(HookCase):
    def setUp(self):
        super().setUp()
        self.connect()
        self.verdict = self.copilot / "cardinal" / "limits" / f"{SID}.verdict.json"
        self.verdict.parent.mkdir(parents=True)

    def write_verdict(self, **verdict) -> None:
        verdict.setdefault("fetched_at", time.time())
        self.verdict.write_text(json.dumps(verdict))

    def pre_tool(self, **env) -> subprocess.CompletedProcess:
        return self.run_hook("preToolUse", self.payload(toolName="bash", toolArgs={"command": "ls"}), **env)

    def test_block_denies_tool_calls(self):
        self.write_verdict(decision="block", band=3, block_reason="Spend limit reached for acme/widgets.")
        out = json.loads(self.pre_tool().stdout)
        self.assertEqual(out, {"permissionDecision": "deny",
                               "permissionDecisionReason": "Spend limit reached for acme/widgets."})
        # Every call while it is in force.
        self.assertEqual(json.loads(self.pre_tool().stdout)["permissionDecision"], "deny")

    def test_no_verdict_or_an_override_allows(self):
        self.assertEqual(self.pre_tool().stdout, "")
        self.write_verdict(decision="block", band=3, block_reason="Spend limit reached.")
        (self.verdict.parent / f"{SID}.override.json").write_text("{}")
        self.assertEqual(self.pre_tool().stdout, "")

    def test_stale_block_allows(self):
        self.write_verdict(decision="block", band=3, block_reason="old", fetched_at=time.time() - 30 * 86400)
        self.assertEqual(self.pre_tool().stdout, "")

    def test_ingest_only_connection_still_stamps_nothing_for_other_tools(self):
        # Connected, no block: a non-storyboard tool gets no answer at all.
        for name in ("bash", "view", "cardinal-lakerunner__list_services"):
            res = self.run_hook("preToolUse", self.payload(toolName=name, toolArgs={}))
            self.assertEqual(res.stdout, "")

    def test_block_is_ignored_while_unconnected(self):
        self.write_verdict(decision="block", band=3, block_reason="Spend limit reached.")
        (self.copilot / "cardinal.json").unlink()
        self.assertEqual(self.pre_tool().stdout, "")

    def test_warn_allows_unless_strict(self):
        self.write_verdict(decision="warn", band=2, user_message="You are at 90% of the budget.",
                           agent_context="Economize.")
        self.assertEqual(self.pre_tool().stdout, "")
        out = json.loads(self.pre_tool(CARDINAL_COPILOT_STRICT_WARN="1").stdout)
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(out["permissionDecisionReason"], "You are at 90% of the budget.")

    def test_strict_warn_without_a_user_message_names_the_escalation(self):
        self.write_verdict(decision="warn", band=2, agent_context="Economize.", block_reason="Budget nearly spent.")
        out = json.loads(self.pre_tool(CARDINAL_COPILOT_STRICT_WARN="1").stdout)
        self.assertEqual(out["permissionDecisionReason"],
                         "[warn escalated to block via CARDINAL_COPILOT_STRICT_WARN]\nBudget nearly spent.")

    def test_warn_is_staged_on_prompt_and_surfaces_once_at_the_next_tool(self):
        self.write_verdict(decision="warn", band=2, user_message="You are at 90% of the budget.",
                           agent_context="Economize: prefer cheap tools.")
        self.assertEqual(self.run_hook("userPromptSubmitted", self.payload(prompt="go")).stdout, "")
        ctx = self.context(self.run_hook("postToolUse", self.tool()))
        self.assertIn("Economize: prefer cheap tools.\n\nYou are at 90% of the budget.", ctx)
        self.assertRegex(ctx, r"^\[evidence:ev_")
        self.assertNotIn("90% of the budget", self.context(self.run_hook("postToolUse", self.tool())))
        # Hysteresis: the same band is not staged again.
        self.run_hook("userPromptSubmitted", self.payload(prompt="again"))
        self.assertNotIn("90% of the budget", self.context(self.run_hook("postToolUse", self.tool())))
        # A higher band is.
        self.write_verdict(decision="warn", band=3, user_message="You are at 99% of the budget.")
        self.run_hook("userPromptSubmitted", self.payload(prompt="more"))
        self.assertIn("99% of the budget", self.context(self.run_hook("postToolUse", self.tool())))

    def test_notify_stages_agent_context_only(self):
        self.write_verdict(decision="notify", band=1, user_message="NOT-FOR-NOTIFY", agent_context="At 60%.")
        self.run_hook("userPromptSubmitted", self.payload(prompt="go"))
        ctx = self.context(self.run_hook("postToolUseFailure", self.payload(
            toolName="bash", toolArgs={"command": "make"}, error="exit code 2")))
        # postToolUseFailure does not consume the notify; postToolUse does.
        self.assertNotIn("At 60%.", ctx)
        ctx = self.context(self.run_hook("postToolUse", self.tool()))
        self.assertIn("At 60%.", ctx)
        self.assertNotIn("NOT-FOR-NOTIFY", ctx)

    def test_strict_warn_is_not_staged(self):
        self.write_verdict(decision="warn", band=2, user_message="You are at 90% of the budget.")
        self.run_hook("userPromptSubmitted", self.payload(prompt="go"), CARDINAL_COPILOT_STRICT_WARN="1")
        self.assertFalse((self.verdict.parent / f"{SID}.notify.json").exists())


class RobustnessTests(HookCase):
    def test_bad_input_exits_zero_silently(self):
        self.connect()
        events = ("sessionStart", "userPromptSubmitted", "preToolUse", "postToolUse", "postToolUseFailure",
                  "agentStop", "subagentStart", "subagentStop", "preCompact")
        raws = ("", "[1, 2]", "null", json.dumps({"toolName": 7, "sessionId": 3}))
        cases = [(event, "not json") for event in events]
        cases += [(event, raw) for event in ("preToolUse", "postToolUse") for raw in raws]
        for event, raw in cases:
            with self.subTest(raw=raw, event=event):
                res = self.run_hook(event, raw=raw)
                if event != "sessionStart":
                    self.assertEqual(res.stdout, "")
                self.assertEqual(res.stderr, "")

    def test_unknown_event_role_or_flag_exits_zero_silently(self):
        self.connect()
        payload = self.tool(name="cardinal-storyboard__preview")
        for args in (("--event", "sessionEnd"), ("--event", "errorOccurred"), ("--event", "postToolUse", "--role", "x"),
                     ("--event", "preToolUse", "--role", "authoring"), ()):
            with self.subTest(args=args):
                res = self.run_hook(None, payload, *args)
                self.assertEqual((res.stdout, res.stderr), ("", ""))
        # An unknown flag: argparse's usage line on stderr, still exit 0.
        self.assertEqual(self.run_hook(None, payload, "--bogus").stdout, "")
        self.assertEqual(self.stub.log_batches, [])

    def test_event_from_the_payload_when_no_flag(self):
        self.connect()
        res = self.run_hook(None, self.payload(hookEventName="preToolUse", toolName="bash"))
        self.assertEqual(res.stdout, "")
        self.run_hook(None, self.payload(hook_event_name="preCompact", trigger="manual"))
        self.assertEqual(len(self.records("cardinal.plan_usage")), 1)

    def test_unreachable_ingest_fails_open(self):
        self.connect(ingest_endpoint="http://127.0.0.1:9")
        res = self.run_hook("postToolUse", self.tool())
        self.assertRegex(self.context(res), r"^\[evidence:ev_")
        self.assertEqual(self.run_hook("userPromptSubmitted", self.payload(prompt="hi")).stdout, "")

    def test_corrupt_state_files_fail_open(self):
        (self.copilot / "cardinal.json").write_text("{not json")
        (self.copilot / "cardinal-secrets.json").write_text("[]")
        for event in ("sessionStart", "userPromptSubmitted", "preToolUse", "postToolUse", "agentStop",
                      "subagentStart", "subagentStop", "preCompact"):
            with self.subTest(event=event):
                self.run_hook(event, self.tool())

    def stripped_plugin(self) -> Path:
        """A plugin copy whose hooks/ lacks the vendored cardinal_core."""
        plugin = self.base / "stripped"
        (plugin / "hooks").mkdir(parents=True)
        for f in (ADAPTER / "hooks").iterdir():
            if f.is_file():
                shutil.copy(f, plugin / "hooks" / f.name)
        shutil.copytree(ADAPTER / ".plugin", plugin / ".plugin")
        return plugin

    def test_registered_commands_exit_zero_when_core_is_missing(self):
        # A non-zero preToolUse exit denies the tool on Copilot. Run every
        # registered bash command, as Copilot would, against a plugin copy
        # whose vendored core is gone: the `|| true` must keep it at 0.
        plugin = self.stripped_plugin()
        entries = json.loads(HOOKS_JSON.read_text())["hooks"]
        for event, items in entries.items():
            for entry in items:
                with self.subTest(command=entry["bash"]):
                    env = clean_env(self.home, PATH=self.path, CARDINAL_COPILOT_PLUGIN_ROOT=str(plugin))
                    res = subprocess.run(["bash", "-c", entry["bash"]], input=json.dumps(self.tool()),
                                         capture_output=True, text=True, env=env, timeout=30)
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertEqual(res.stdout, "")

    def test_script_itself_exits_zero_when_core_is_missing(self):
        # Without relying on the `|| true`: the script alone exits 0 with no
        # output when its vendored core cannot be imported.
        plugin = self.stripped_plugin()
        for event in ("preToolUse", "postToolUse", "sessionStart"):
            with self.subTest(event=event):
                res = subprocess.run([sys.executable, str(plugin / "hooks" / "cardinal-copilot-telemetry.py"),
                                      "--event", event], input=json.dumps(self.tool()), capture_output=True,
                                     text=True, env=clean_env(self.home, PATH=self.path), timeout=30)
                self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def test_registered_pre_tool_use_command_does_not_deny_on_crash(self):
        # The same through the real plugin root, with a payload that makes
        # nothing to deny: exit 0, no decision printed.
        [entry] = json.loads(HOOKS_JSON.read_text())["hooks"]["preToolUse"]
        env = clean_env(self.home, PATH=self.path, CARDINAL_COPILOT_PLUGIN_ROOT=str(ADAPTER))
        res = subprocess.run(["bash", "-c", entry["bash"]], input="{garbage", capture_output=True, text=True,
                             env=env, timeout=30)
        self.assertEqual((res.returncode, res.stdout), (0, ""))


class ToolNameUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hook = load_script(HOOK, "copilot_hook_units")

    def test_canonical_tool_name(self):
        c = self.hook.canonical_tool_name
        with mock.patch.object(self.hook, "configured_mcp_servers", return_value=["my-srv", "my"]):
            self.assertEqual(c("bash"), "bash")
            self.assertEqual(c("view"), "view")
            self.assertEqual(c("mcp__x__y"), "mcp__x__y")
            self.assertEqual(c("cardinal-behavior-get_behavior_sdk"), "mcp__cardinal-behavior__get_behavior_sdk")
            self.assertEqual(c("cardinal-storyboard__preview"), "mcp__cardinal__storyboard__preview")
            self.assertEqual(c("my-srv-query"), "mcp__my-srv__query")
            self.assertEqual(c("my-other"), "mcp__my__other")
            self.assertEqual(c("cardinal-"), "cardinal-")
            self.assertEqual(c("unknown-tool"), "unknown-tool")
            self.assertEqual(c(None), "")
            self.assertEqual(c(7), "")

    def test_configured_mcp_servers_never_raises(self):
        with mock.patch.dict(os.environ, {"COPILOT_HOME": "relative/ignored"}):
            self.assertIsInstance(self.hook.configured_mcp_servers("/nonexistent"), list)

    def test_edited_paths(self):
        e = self.hook.edited_paths
        self.assertEqual(e("edit", {"path": "/abs/a.py"}, "/repo"), ["/abs/a.py"])
        self.assertEqual(e("create", {"path": "b.py"}, "/repo"), ["/repo/b.py"])
        self.assertEqual(e("str_replace_editor", {"path": "c.py", "old_str": "x"}, "/repo"), ["/repo/c.py"])
        patch = ("*** Begin Patch\n*** Update File: svc/a.go\n@@\n-x\n+y\n*** Add File: svc/b.go\n+z\n"
                 "*** Delete File: /abs/old.go\n*** Update File: svc/a.go\n*** End Patch\n")
        self.assertEqual(e("apply_patch", patch, "/repo"), ["/repo/svc/a.go", "/repo/svc/b.go", "/abs/old.go"])
        self.assertEqual(e("apply_patch", {"input": patch}, "/repo"),
                         ["/repo/svc/a.go", "/repo/svc/b.go", "/abs/old.go"])
        self.assertEqual(e("bash", {"path": "x"}, "/repo"), [])
        self.assertEqual(e("view", {"path": "x"}, "/repo"), [])
        self.assertEqual(e("edit", "not a dict", "/repo"), [])

    def test_result_success(self):
        s = self.hook.result_success
        self.assertEqual(s({}), "true")
        self.assertEqual(s({"resultType": "success"}), "true")
        self.assertEqual(s({"resultType": "failure"}), "false")
        self.assertEqual(s({"textResultForLlm": "Exit code: 3"}), "false")
        self.assertEqual(s({"textResultForLlm": "exited with exit code 0"}), "true")

    def test_decode_json_container(self):
        d = self.hook._decode_json_container
        self.assertEqual(d('{"a": 1}'), {"a": 1})
        self.assertEqual(d(" [1] "), [1])
        for keep in ("plain", "42", '"q"', "{bad", "", None, {"a": 1}):
            self.assertEqual(d(keep), keep)


if __name__ == "__main__":
    unittest.main()
