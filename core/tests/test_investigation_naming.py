"""Provisional titles are revisited until a confirmed question write."""
import contextlib
import io
from types import SimpleNamespace
from unittest.mock import patch

from cardinal_core import investigation_cli as cli
from cardinal_core import investigation_bootstrap as boot
from cardinal_core import investigation_state_sync as sync
from cardinal_core import investigation_state as state
from cardinal_core import investigation_poller as poller
from cardinal_core import investigation_turn as turn
from cardinal_core import investigation_events as ie
from tests.test_investigation_turn import TurnBase, SID, INV, CONN


class Naming(TurnBase):
    def setUp(self):
        super().setUp()
        ie.update_fields(self.home, SID, {"question_status": "provisional"})

    def test_missed_start_guidance_is_recovered_at_tool_boundary_once(self):
        d = turn.at_tool(self.home, SID, "org", "codex", is_subagent=False)
        self.assertIn("default title", d.agent_context[0])
        self.assertIn("implementation or review tasks", d.agent_context[0])
        self.assertEqual(turn.at_tool(self.home, SID, "org", "codex", is_subagent=False).agent_context, [])

    def test_next_prompt_retries_even_when_previous_stop_was_missed(self):
        self.assertIn("default title", turn.at_prompt(self.home, SID, "codex").agent_context[0])
        self.assertIn("default title", turn.at_prompt(self.home, SID, "codex").agent_context[0])

    def test_stop_revisits_title_once_without_an_infinite_stop_loop(self):
        turn.at_tool(self.home, SID, "org", "claude", is_subagent=False)
        with patch.object(poller, "fresh", return_value=True):
            self.assertIn("default title", self.turn_end("claude").turn_end_block)
            self.assertIsNone(self.turn_end("claude", stop_hook_active=True).turn_end_block)
        self.assertIn("default title", turn.at_prompt(self.home, SID, "claude").agent_context[0])

    def test_nonblocking_runtime_reminds_at_next_prompt_and_stated_status_silences_it(self):
        with patch.object(poller, "fresh", return_value=True):
            self.assertIsNone(self.turn_end("gemini").turn_end_block)
        self.assertEqual(turn.consume_staged(self.home, SID), [])
        self.assertIn("default title", turn.at_prompt(self.home, SID, "gemini").agent_context[0])
        ie.update_fields(self.home, SID, {"question_status": "stated"})
        self.assertEqual(turn.at_prompt(self.home, SID, "gemini").agent_context, [])
        self.assertIsNone(turn.naming_reminder(self.home, SID, repeat=True))

    def test_unknown_status_joined_subagent_and_other_org_are_silent(self):
        self.assertEqual(turn.at_tool(self.home, SID, "org", "codex", is_subagent=True).agent_context, [])
        self.assertEqual(turn.at_tool(self.home, SID, "other", "codex", is_subagent=False).agent_context, [])
        for fields in ({"is_author": False}, {"is_author": True, "question_status": None}):
            ie.update_fields(self.home, SID, fields)
            self.assertIsNone(turn.naming_reminder(self.home, SID, repeat=True))

    def run_question(self, *, error=None, inv=None, json=False):
        wiring = SimpleNamespace(environ={"TEST_SESSION": SID}, cli="/opt/cardinal/scripts/cardinal-storyboard",
                                 paths=SimpleNamespace(home=self.home / ".codex"), client="codex/test",
                                 connection=lambda: dict(CONN))
        argv = ["question", "Explain title lifecycle", "--json"] if json else ["question", "Explain title lifecycle"]
        if inv:
            argv.extend(["--investigation", inv])
        args = cli.build_parser(wiring, ("TEST_SESSION",)).parse_args(argv)
        with patch.object(sync, "set_investigation_question", side_effect=error,
                          return_value={"investigation_id": inv or INV, "question_status": "stated"}) as write, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = cli.CLI(wiring, args).cmd_question(args)
        return result, write

    def test_success_updates_environment_session_before_json_return(self):
        result, write = self.run_question(json=True)
        self.assertEqual(result, 0)
        self.assertEqual(write.call_args.kwargs["session_id"], SID)
        self.assertEqual(ie.read_binding(self.home, SID)["question_status"], "stated")
        self.assertIsNone(turn.naming_reminder(self.home, SID))

    def test_failed_request_keeps_title_pending_for_retry(self):
        result, _ = self.run_question(error=state.FetchError("offline"))
        self.assertEqual(result, 1)
        self.assertEqual(ie.read_binding(self.home, SID)["question_status"], "provisional")
        self.assertIsNotNone(turn.naming_reminder(self.home, SID))

    def test_explicit_other_investigation_does_not_mark_this_session_named(self):
        result, _ = self.run_question(inv="inv_" + "f" * 24)
        self.assertEqual(result, 0)
        self.assertEqual(ie.read_binding(self.home, SID)["question_status"], "provisional")

    def test_server_refresh_recovers_status_after_external_naming(self):
        ie.update_fields(self.home, SID, {"capabilities": {}})
        boot.refresh_path(self.home, SID).touch()
        with patch.object(sync, "ensure_session_investigation", return_value={"question_status": "stated"}):
            self.assertEqual(boot.refresh_capabilities(self.home, SID, CONN, "test"), "ok")
        self.assertEqual(ie.read_binding(self.home, SID)["question_status"], "stated")
