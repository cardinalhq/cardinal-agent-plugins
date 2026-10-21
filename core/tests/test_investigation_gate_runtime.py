"""Explicit intent coordination, waiting and legacy delivery coexistence."""
import contextlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cardinal_core import investigation_cli as cli
from cardinal_core import investigation_gate_runtime as rt
from cardinal_core import investigation_turn as turn
from cardinal_core import investigation_text as text
from tests.test_investigation_gate import Fixture, POLICY, CONN, INV, SID, event, c, g, ie
from tests.test_investigation_requests import created, response, resolved

PURPOSE = "Choose the next hypothesis"


class Runtime(Fixture):
    def setUp(self):
        super().setUp()
        self.cli = self.root / "plugin/scripts/cardinal-storyboard"
        self.hook = rt.hook_path(self.cli, "codex")
        self.wiring = SimpleNamespace(runtime="codex", environ={}, cli=str(self.cli), client="test",
            paths=SimpleNamespace(home=self.root / ".codex"), connection=Mock(return_value=CONN))

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = cli.cli_main([*args, "--session", SID, "--investigation", INV], self.wiring)
        return status, json.loads(out.getvalue())

    def prepare(self):
        return rt.prepare_intent(session=SID, runtime="codex", home=self.root, conn=CONN,
            client="test", cli=self.cli)[0]

    def test_sync_from_any_process_has_no_mode_probes_or_action_policy(self):
        status, result = self.run_cli("sync", "--intent", "Explain a scientific tradeoff")
        self.assertEqual((status, result["status"]), (0, "ready"))
        selected = rt.route(self.root, SID)
        self.assertEqual(selected["policy"]["git"], [])
        self.assertEqual(selected["policy"]["cli_prefixes"], [])
        self.assertTrue(all(b.get("request_protocol") == 2 for op, b in self.server.requests if op == "read-investigation-events"))

    def test_stale_hooks_never_intercept_ordinary_tools_or_contact_server(self):
        self.server.add(event(1))
        for tool in ("apply_patch", "Bash", "Write", "reason", "arbitrary-tool"):
            self.assertIsNone(rt.dispatch({"session_id": SID, "tool_name": tool},
                connection=Mock(side_effect=AssertionError("hook must not read connection"))))
        self.assertEqual(self.server.requests, [])

    def test_new_feedback_is_bounded_and_requires_explicit_review(self):
        self.server.add(event(1))
        status, result = self.run_cli("sync", "--intent", "Answer the user")
        self.assertEqual((status, result["status"]), (3, "needs_feedback"))
        self.assertLessEqual(len(result["message"].encode()), 1000)
        selected = rt.route(self.root, SID, batch=result["batch"])
        gate = g.Gate(CONN, selected["gate_file"], session=SID, client="test", home=self.root)
        self.gate, self.path = gate, gate.path
        batch, hashes = self.delivered()
        with self.assertRaises(ValueError): gate.reviewed(batch, hashes)
        self.acknowledge(1)
        gate.reviewed(batch, hashes)
        self.assertEqual(self.run_cli("sync", "--intent", "Answer the user")[1]["status"], "ready")

    def test_waiting_consumer_does_not_hide_another_consumers_feedback(self):
        args = SimpleNamespace(intent=PURPOSE, requires=[1], intent_connection=c.identity(CONN))
        first = SimpleNamespace(conn=CONN, synchronize=Mock(return_value=g.Result("defer", "Pending", reason="waiting")))
        second = SimpleNamespace(conn=CONN, synchronize=Mock(return_value=g.Result("defer", "New information", "later-batch")))
        service = cli.CLI(self.wiring, SimpleNamespace(credential_file=None))
        result = service._sync_intent(args, [first, second])
        self.assertEqual((result["status"], result["batch"]), ("needs_feedback", "later-batch"))
        first.synchronize.assert_called_once_with(PURPOSE, [1])
        second.synchronize.assert_called_once_with(PURPOSE, [1])
        self.assertFalse(result["released"])

    def test_handled_blocked_progress_cannot_hide_another_completed_result(self):
        args = SimpleNamespace(intent=PURPOSE, requires=[1], intent_connection=c.identity(CONN), after_response=2)
        first = SimpleNamespace(conn=CONN, synchronize=Mock(return_value=g.Result("defer", "Blocked", reason="needs_attention",
            requests=({"request": 1, "status": "blocked", "response": 2},))))
        second = SimpleNamespace(conn=CONN, synchronize=Mock(return_value=g.Result("defer", "Complete", reason="needs_attention",
            requests=({"request": 3, "status": "complete", "response": 4},))))
        service = cli.CLI(self.wiring, SimpleNamespace(credential_file=None))
        result = service._sync_intent(args, [first, second])
        self.assertEqual((result["status"], result["requests"][0]["request"]), ("needs_attention", 3))

    def test_timeout_retains_requirement_and_restarts_without_request_id(self):
        self.server.add(created(legacy=True))
        status, result = self.run_cli("wait", "--intent", PURPOSE, "--request", "1", "--timeout", "0.03")
        self.assertEqual((status, result["status"], result["released"]), (3, "timeout", False))
        self.assertEqual(self.run_cli("sync", "--intent", PURPOSE)[1]["status"], "waiting")
        self.assertEqual(self.run_cli("sync", "--intent", "Unrelated explanation")[1]["status"], "ready")

    def test_wait_interrupts_on_feedback_and_never_treats_delivery_as_acceptance(self):
        self.server.add(created())
        with patch.object(cli.time, "sleep", side_effect=lambda _: self.server.add(response(2))):
            status, result = self.run_cli("wait", "--intent", PURPOSE, "--timeout", "1")
        self.assertEqual((status, result["status"]), (3, "needs_feedback"))
        selected = rt.route(self.root, SID)
        self.gate = g.Gate(CONN, selected["gate_file"], session=SID, client="test", home=self.root)
        self.path = self.gate.path
        batch, hashes = self.delivered(); self.gate.reviewed(batch, hashes)
        self.assertEqual(self.run_cli("wait", "--intent", PURPOSE)[1]["status"], "needs_attention")
        self.server.add(resolved(3))
        self.assertEqual(self.run_cli("wait", "--intent", PURPOSE)[1]["status"], "ready")

    def test_incoming_wait_interrupts_on_assignment(self):
        with patch.object(cli.time, "sleep", side_effect=lambda _: self.server.add(created(requester="peer", assignee=SID))):
            status, result = self.run_cli("wait", "--intent", "Await the next assignment", "--incoming", "--timeout", "1")
        self.assertEqual((status, result["status"]), (3, "needs_feedback"))

    def test_new_request_uses_purpose_without_action_categories(self):
        with patch.object(ie, "append_event", return_value={"event": {"seq": 1}}) as append:
            self.run_cli("request", "--intent", "Select a diagnosis", "--assignee", "user:owner",
                "--assignee-session", "peer", "--text", "Evaluate the competing explanations")
        payload = append.call_args.args[3]
        self.assertEqual(payload["intent"], "Select a diagnosis")
        self.assertNotIn("actions", payload)

    def test_missing_intent_capability_and_missing_active_state_never_clear(self):
        original = self.server.__call__
        def old_server(*args, **kw):
            result = original(*args, **kw)
            result.get("capabilities", {}).pop("coordination_intents", None)
            return result
        with patch.object(ie.sync, "_post", side_effect=old_server), self.assertRaises(ValueError):
            self.prepare()
        gate = self.prepare()
        gate.path.unlink()
        with self.assertRaisesRegex(ValueError, "state is missing"):
            self.prepare()

    def test_delivery_notifies_normally_except_during_explicit_batch_handling(self):
        ie.write_binding(self.root, SID, {"investigation_id": INV, "cursor": 0, "org": "org", "question_status": "stated"})
        gate = self.prepare()
        self.assertFalse(rt.owns_delivery(self.root, SID, INV))
        self.server.add(event(1))
        gate.synchronize("Read the new feedback")
        turn.stage(self.root, SID, "legacy staged content")
        self.assertTrue(rt.owns_delivery(self.root, SID, INV))
        self.assertEqual(turn.at_prompt(self.root, SID, "codex").agent_context, [])
        self.assertEqual(turn._read_staged(self.root, SID), ["legacy staged content"])
        self.assertFalse(rt.owns_delivery(self.root, SID, "inv_" + "b" * 24))
        self.assertFalse(rt.owns_delivery(self.root, SID, INV, "gemini"))

    def test_route_lock_contention_does_not_drain_feedback(self):
        self.prepare()
        with c.locked(rt.route_path(self.root, SID)), rt.delivery_guard(self.root, SID, INV) as allowed:
            self.assertFalse(allowed)
        with self.assertRaises(BlockingIOError), rt.delivery_guard(self.root, SID, INV):
            raise BlockingIOError()

    def test_guidance_is_shared_and_not_action_specific(self):
        from tests.test_investigation_text import binding
        for runtime in ("codex", "claude"):
            guide = text.session_text(SID, binding(), runtime=runtime, coordination_gate=True)
            self.assertEqual(guide.count(rt.GATE_RULE), 1)
            self.assertNotIn("covered actions", guide)
            self.assertIn("sync --intent", guide)
            self.assertIn("wait --intent", guide)

    def test_legacy_cursor_never_suppresses_explicit_catchup(self):
        ie.write_binding(self.root, SID, {"investigation_id": INV, "cursor": 999, "org": "org"})
        self.server.add(event(1))
        self.assertEqual(self.run_cli("sync", "--intent", "Reassess the plan")[1]["status"], "needs_feedback")
        self.assertEqual(ie.read_binding(self.root, SID)["cursor"], 999)
