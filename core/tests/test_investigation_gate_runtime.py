"""Direct CLI hook routing, readiness and legacy delivery coexistence."""
import contextlib
import io
import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

from cardinal_core import investigation_gate_runtime as rt
from cardinal_core import investigation_poller as poller
from cardinal_core import investigation_turn as turn
from cardinal_core import investigation_text as text
from tests.test_investigation_gate import Fixture, POLICY, CONN, INV, SID, event, c, g, ie


class Runtime(Fixture):
    def setUp(self):
        super().setUp()
        self.cli = self.root / "plugin/scripts/cardinal-storyboard"
        self.hook = rt.hook_path(self.cli, "codex")
        self.hook.parent.mkdir(parents=True)
        self.hook.write_text("# fixture hook\n")
        self.env = {rt.MODE: "direct-cli"}
        self.pid = patch.object(rt, "runtime_pid", return_value=1234)
        self.pid.start()
        self.addCleanup(self.pid.stop)

    def probes(self, runtime="codex", path=None):
        for tool in rt.REQUIRED[runtime]:
            rt.record_ready(self.root, SID, runtime, tool, {}, path or self.hook, self.env)

    def activate(self):
        self.probes()
        return rt.activate(self.gate, POLICY, runtime="codex", cli=self.cli, environ=self.env)

    def dispatch(self, tool="apply_patch", args=None, **extra):
        payload = {"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": tool,
                   "tool_input": args or {"command": "git commit -m test"}, **extra}
        return rt.dispatch(payload, runtime="codex", home=self.root, connection=lambda: CONN,
                           client="test", hook=self.hook, environ=self.env)

    def test_unconnected_is_silent_and_disabled_bound_routes_rearm(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(rt.dispatch({"hook_event_name": "PreToolUse", "session_id": SID,
                "tool_name": "apply_patch", "tool_input": "patch"}, runtime="codex", home=self.root,
                connection=lambda: None, client="test", hook=self.hook, environ={}))
            self.activate()
            self.assertIsNone(self.dispatch())
            rt.deactivate(self.gate)
            self.server.add(event(1))
            self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(out.getvalue(), "")

    def test_enable_requires_current_all_transport_receipts_and_mode(self):
        with self.assertRaises(ValueError):
            rt.preflight(self.root, SID, "codex", self.cli, self.env)
        rt.record_ready(self.root, SID, "codex", "Bash", {}, self.hook, self.env)
        with self.assertRaisesRegex(ValueError, "apply_patch"):
            rt.preflight(self.root, SID, "codex", self.cli, self.env)
        self.probes()
        rt.preflight(self.root, SID, "codex", self.cli, self.env)
        for env in ({}, {rt.MODE: "desktop"}):
            with self.assertRaises(ValueError):
                rt.preflight(self.root, SID, "codex", self.cli, env)
        with patch.object(rt, "runtime_pid", return_value=999), self.assertRaises(ValueError):
            rt.preflight(self.root, SID, "codex", self.cli, self.env)
        self.hook.write_text("# changed hook version")
        with self.assertRaises(ValueError):
            rt.preflight(self.root, SID, "codex", self.cli, self.env)

    def test_stale_receipts_and_wrong_runtime_refuse(self):
        self.probes()
        with patch.object(rt.time, "time", return_value=time.time() + 301), self.assertRaises(ValueError):
            rt.preflight(self.root, SID, "codex", self.cli, self.env)
        with self.assertRaises(ValueError):
            rt.activate(self.gate, {**POLICY, "runtime": "claude"}, runtime="codex", cli=self.cli, environ=self.env)

    def test_enable_argv_receipt_is_one_use_and_sandbox_needs_no_ps(self):
        import shlex
        self.probes()
        argv = ["gate-enable", "--session", SID, "--gate-file", str(self.path), "--policy", "/policy.json"]
        value = {**POLICY, "cli_prefixes": [[str(self.cli)]]}
        command = shlex.join([str(self.cli), "investigation", *argv])
        rt.record_ready(self.root, SID, "codex", "Bash", {"cmd": command}, self.hook, self.env)
        with patch.object(rt, "runtime_pid", side_effect=PermissionError("sandbox ps is unavailable")):
            with self.assertRaises(ValueError):
                rt.preflight(self.root, SID, "codex", self.cli, self.env, argv=argv + ["--new-epoch"], prefixes=value["cli_prefixes"])
            rt.activate(self.gate, value, runtime="codex", cli=self.cli, environ=self.env, argv=argv)
            with self.assertRaises(ValueError):
                rt.activate(self.gate, value, runtime="codex", cli=self.cli, environ=self.env, argv=argv)

    def test_explicit_env_connection_never_reuses_a_saved_key(self):
        from cardinal_core.storyboard_agent import Wiring
        from types import SimpleNamespace
        paths = Mock()
        wiring = SimpleNamespace(paths=paths, environ={"CARDINAL_CONNECTION": "env",
                                  "CARDINAL_MCP_URL": "http://127.0.0.1:1234/api/orgs/fixture/mcp"})
        conn = Wiring.connection(wiring)
        self.assertEqual(conn["origin"], "http://127.0.0.1:1234")
        self.assertIsNone(conn["key"])
        paths.read_secrets.assert_not_called()

    def test_codex_real_bash_command_is_normalized_and_denied_with_bounded_feedback(self):
        self.activate()
        self.server.add(event(1))
        answer = self.dispatch("Bash")
        result = answer["hookSpecificOutput"]
        self.assertEqual(result["permissionDecision"], "deny")
        reason = result["permissionDecisionReason"]
        self.assertLessEqual(len(reason.encode()), 1000)
        self.assertTrue(reason.startswith(rt.PREFIX))
        self.assertEqual(self.gate.status()["after"], 0)
        self.assertIsNone(self.dispatch("Bash", {"command": "git status"}))
        self.assertIsNone(self.dispatch("functions.exec", {"code": "nested()"}))
        self.assertIsNone(self.dispatch(agent_id="subagent"))

    def test_operational_error_mode_mismatch_and_incomplete_activation_deny(self):
        self.activate()
        self.server.fail = TimeoutError()
        self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.server.fail = None
        self.env.clear()
        self.assertIsNone(self.dispatch())  # No MODE opt-in is required.
        selected = rt.route(self.root, SID)
        selected["state"] = "activating"
        c.private_write(rt.route_path(self.root, SID), selected)
        self.assertIsNone(self.dispatch())  # Interrupted activation is retried automatically.
        self.assertIsNone(self.dispatch("Bash", {"command": "cardinal-storyboard investigation gate-disable"}))

    def test_failed_activation_can_be_cancelled_without_engine_file(self):
        self.probes()
        self.server.cap["enabled"] = False
        with self.assertRaises(ValueError):
            rt.activate(self.gate, POLICY, runtime="codex", cli=self.cli, environ=self.env)
        self.assertFalse(self.path.exists())
        self.assertTrue(rt.owns_delivery(self.root, SID, INV))
        self.assertTrue(rt.deactivate(self.gate)["activation_cancelled"])
        self.assertFalse(rt.owns_delivery(self.root, SID, INV))

    def test_missing_active_engine_file_never_becomes_a_disabled_allow(self):
        self.activate()
        self.path.unlink()
        self.assertEqual(self.dispatch()["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(rt.owns_delivery(self.root, SID, INV))
        self.assertFalse(rt.deactivate(self.gate)["enabled"])
        self.assertFalse(rt.owns_delivery(self.root, SID, INV))

    def test_legacy_delivery_staged_inbox_and_poller_are_retained_until_disable(self):
        ie.write_binding(self.root, SID, {"investigation_id": INV, "cursor": 0, "org": "org", "question_status": "stated"})
        turn.stage(self.root, SID, "legacy staged content")
        self.activate()
        with patch.object(ie, "deliver_inbox") as inbox, patch.object(ie, "resurface") as resurface:
            self.assertEqual(turn.at_tool(self.root, SID, "org", "codex", is_subagent=False).agent_context, [])
            self.assertEqual(turn.at_prompt(self.root, SID, "codex").agent_context, [])
            flush = Mock()
            result = turn.at_turn_end(self.root, SID, CONN, "test", "codex", environ={}, stop_hook_active=False,
                                      deadline=time.monotonic() + 3, owner_input_flush=flush)
            self.assertIsNone(result.turn_end_block)
            flush.assert_called_once()
            inbox.assert_not_called()
            resurface.assert_not_called()
        status = {"failures": 0}
        with patch.object(ie, "poll_once") as poll, patch.object(poller.boot, "needs_retry", return_value=False):
            poller._step(self.root, SID, lambda: CONN, "test", None, status, 10, time.time())
        poll.assert_not_called()
        self.assertEqual(status["last"], "gate_owned")
        self.assertEqual(turn._read_staged(self.root, SID), ["legacy staged content"])
        rt.deactivate(self.gate)
        self.assertEqual(turn.at_prompt(self.root, SID, "codex").agent_context, ["legacy staged content"])

    def test_another_investigation_and_runtime_keep_existing_delivery(self):
        self.activate()
        self.assertFalse(rt.owns_delivery(self.root, SID, "inv_" + "b" * 24))
        self.assertFalse(rt.owns_delivery(self.root, SID, INV, "gemini"))
        self.assertTrue(rt.owns_delivery(self.root, SID, INV, "codex"))
        answer = rt.dispatch({"hook_event_name": "PreToolUse", "session_id": SID, "tool_name": "Write", "tool_input": {}},
                             runtime="claude", home=self.root, connection=lambda: CONN,
                             client="test", hook=self.hook, environ=self.env)
        self.assertEqual(answer["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_route_lock_contention_does_not_drain_legacy(self):
        self.activate()
        with c.locked(rt.route_path(self.root, SID)), rt.delivery_guard(self.root, SID, INV) as allowed:
            self.assertFalse(allowed)
        # Exceptions from a legacy body must propagate without a second yield.
        rt.deactivate(self.gate)
        with self.assertRaises(BlockingIOError), rt.delivery_guard(self.root, SID, INV):
            raise BlockingIOError()

    def test_guidance_replacement_has_one_short_rule_and_default_text_unchanged(self):
        from tests.test_investigation_text import binding
        original = text.session_text(SID, binding(), runtime="codex")
        active = text.session_text(SID, binding(), runtime="codex", coordination_gate=True)
        self.assertIn(rt.GATE_RULE, active)
        self.assertEqual(active.count(rt.GATE_RULE), 1)
        self.assertNotIn(ie.ACK_GUIDANCE, active)
        self.assertLess(len(active.encode()), len(original.encode()))
        self.assertEqual(original, text.session_text(SID, binding(), runtime="codex", coordination_gate=False))
        for changed in ({"storyboard_id": None}, {"is_author": False}):
            guide = text.session_text(SID, binding(**changed), runtime="codex", coordination_gate=True)
            self.assertEqual(guide.count(rt.GATE_RULE), 1)

    def test_failed_policy_change_preserves_both_old_and_requested_coverage(self):
        self.activate()
        # Reconfiguration may not open a window for either the old executable
        # or its requested replacement while the engine epoch is uncommitted.
        changed = {**POLICY, "git": ["/new/git"]}
        with self.assertRaises(ValueError):
            rt.activate(self.gate, changed, runtime="codex", cli=self.cli, environ=self.env)
        self.assertEqual(rt.route(self.root, SID)["state"], "activating")
        for cmd in ("git commit -m old", "/new/git commit -m new"):
            self.assertIsNone(self.dispatch("Bash", {"command": cmd}))
        recovered = rt.route(self.root, SID)
        self.assertIn("git", recovered["policy"]["git"])
        self.assertIn("/new/git", recovered["policy"]["git"])
        self.assertNotIn("previous_policy", recovered)

    def test_legacy_cursor_never_suppresses_gate_obligation(self):
        ie.write_binding(self.root, SID, {"investigation_id": INV, "cursor": 999, "org": "org"})
        self.activate()
        self.server.add(event(1))
        self.assertIsNotNone(self.dispatch())
        self.assertEqual(self.batch()["page"]["events"][0]["seq"], 1)
        self.assertEqual(ie.read_binding(self.root, SID)["cursor"], 999)

    def test_implicit_supervisor_route_uses_scoped_credential_without_owner_fallback(self):
        from types import SimpleNamespace
        from cardinal_core import investigation_cli as cli
        credential = self.root / "grant.json"
        c.private_write(credential, {"token": "fixture"})
        conn = {**CONN, "grant_id": "grt_" + "b" * 24, "token": "fixture"}
        self.server.viewer = "grantee:" + conn["grant_id"]
        gate = g.Gate(conn, self.path, session=SID, client="test", home=self.root, credential_file=credential)
        value = {**POLICY, "role": "supervisor"}
        self.probes()
        rt.activate(gate, value, runtime="codex", cli=self.cli, environ=self.env)
        wiring = SimpleNamespace(runtime="codex", environ={}, cli=str(self.cli), client="test",
                                 paths=SimpleNamespace(home=self.root / ".codex"), connection=Mock(return_value=CONN))
        with patch.object(c, "credential_connection", return_value=conn) as read, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cli_main(["gate-status", "--session", SID], wiring), 0)
        read.assert_called_once_with(credential)
        wiring.connection.assert_not_called()
        self.assertTrue(json.loads(out.getvalue())["enabled"])
