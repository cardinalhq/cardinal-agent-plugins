"""Codex hook/CLI integration: automatic binding, cited checkpoints and failures."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

import test_codex_storyboard as fixtures
from test_codex_storyboard import SESSION, SESSION_START, CLI, VERSION

INV = "inv_" + "a" * 24
SB = "sb_" + "b" * 24
RCPT = "rcpt_" + "c" * 24


class InvestigationTests(unittest.TestCase):
    setUp = fixtures.CodexStoryboardTests.setUp
    tearDown = fixtures.CodexStoryboardTests.tearDown
    connect = fixtures.CodexStoryboardTests.connect
    env = fixtures.CodexStoryboardTests.env
    run_hook = fixtures.CodexStoryboardTests.run_hook
    session_context = fixtures.CodexStoryboardTests.session_context

    def setup_live(self, enabled=True, author=True, owner_input=False):
        self.connect()
        self.fake.routes["ensure-session-investigation"] = (200, {
            "investigation_id": INV, "storyboard_id": SB,
            "view_url": f"{self.fake.origin}/storyboards/{SB}",
            "investigation_url": f"/storyboards/{SB}/investigation",
            "is_author": author, "capabilities": {"projection": {"enabled": enabled},
                                                   "owner_input": {"enabled": owner_input}}})

    def start(self, **env):
        return self.session_context(CARDINAL_INVESTIGATION_POLLER="0", **env)

    def cli(self, *args, data=None, **env):
        return subprocess.run([sys.executable, str(CLI), "investigation", *args,
                               "--session", SESSION], input=data, text=True, capture_output=True,
                              env=self.env(**env), cwd=str(self.repo), timeout=20)

    def test_auto_start_resume_reuses_one_storyboard_and_advertises_link(self):
        self.setup_live()
        first = self.start()
        second = self.start()
        self.assertIn(f"/storyboards/{SB}", first)
        self.assertNotIn("Wait for an affirmative reply", first)
        self.assertNotIn("Would you like me to update the storyboard visualization?", second)
        self.assertIn("first progress update", first)
        self.assertIn("written from branch fix/checkout", first)
        self.assertNotIn("pass it as session_id", first)
        self.assertIn("checkpoint", second)
        ensures = [r for r in self.fake.requests if r["path"].endswith("/ensure-session-investigation")]
        self.assertEqual(len(ensures), 1)
        self.assertEqual(ensures[0]["body"]["session_id"], SESSION)
        self.assertEqual(ensures[0]["body"]["context"]["repo"], "acme/widgets")
        self.assertEqual(ensures[0]["body"]["context"]["branch"], "fix/checkout")
        self.assertEqual(ensures[0]["body"]["context"]["client"], f"codex/{VERSION}")
        self.assertNotIn(str(self.repo), json.dumps(ensures[0]["body"]))
        self.assertEqual(ensures[0]["headers"]["x-cardinal-client"], f"codex/{VERSION}")
        self.assertNotIn("prompt", ensures[0]["body"])
        self.assertNotIn("transcript", ensures[0]["body"])
        # SessionStart itself records nothing; owner input is the prompt
        # hook's (owner_input.capture), once the server enables it.
        self.assertNotIn("record-owner-input", str(self.fake.requests))
        self.assertIn("Storyboard (private draft until published): ", first)
        self.assertIn("Investigation timeline (shows the live record before any publish): ", first)
        linked = self.cli("link", "--json")
        self.assertEqual(linked.returncode, 0, linked.stderr)
        self.assertEqual(json.loads(linked.stdout)["storyboard_id"], SB)

    def test_owner_input_is_recorded_from_the_prompt_hook_once_enabled(self):
        self.setup_live(owner_input=True)
        self.fake.routes["record-owner-input"] = (200, {"event": {
            "investigation_id": INV, "seq": 3, "type": "owner_input.recorded"}, "duplicate": False})
        self.start()
        prompt = {**SESSION_START, "hook_event_name": "UserPromptSubmit", "turn_id": "t1",
                  "prompt": "Why did checkout p99 regress?"}
        first = json.loads(self.run_hook("UserPromptSubmit", prompt, CARDINAL_INVESTIGATION_POLLER="0").stdout)
        self.assertIn("as owner input", first["systemMessage"])
        self.assertNotIn("record-owner-input", str(self.fake.requests), "disclosed first, not captured")
        self.run_hook("UserPromptSubmit", {**prompt, "prompt": "Check the cache layer first."},
                      CARDINAL_INVESTIGATION_POLLER="0")
        [record] = [r["body"] for r in self.fake.requests if r["path"].endswith("/record-owner-input")]
        self.assertEqual(record["payload"]["text"], "Check the cache layer first.")
        self.assertEqual(record["session_id"], SESSION)

    def test_bootstrap_uses_payload_cwd_and_honors_context_optout(self):
        self.setup_live()
        # The hook process runs inside self.repo; the payload names a directory
        # outside Git, and must not accidentally label it as acme/widgets.
        self.session_context({**SESSION_START, "cwd": str(self.repo.parent)}, CARDINAL_INVESTIGATION_POLLER="0")
        ensures = [r for r in self.fake.requests if r["path"].endswith("/ensure-session-investigation")]
        self.assertNotIn("repo", ensures[0]["body"]["context"])
        self.assertNotIn(str(self.repo.parent), json.dumps(ensures[0]["body"]))

    def test_bootstrap_context_optout(self):
        self.setup_live()
        self.start(CARDINAL_STORYBOARD_CONTEXT="0")
        ensure = next(r for r in self.fake.requests if r["path"].endswith("/ensure-session-investigation"))
        self.assertNotIn("context", ensure["body"])

    def test_session_guidance_declares_automatic_coordination_and_explicit_dependencies(self):
        self.setup_live()
        ctx = self.start()
        self.assertIn("Coordinate on intent at any point in your work", ctx)
        self.assertIn("create an investigation request with its assignee and intent", ctx)
        self.assertIn("complete, blocked, abstain or reject", ctx)
        self.assertIn("only exact-result acceptance or reasoned cancellation resolves the dependency", ctx)

    def test_existing_session_receives_actionable_cue_guidance_at_tool_completion(self):
        self.setup_live()
        self.start()
        sessions = self.home / ".cardinal" / "investigations" / "sessions"
        binding = json.loads((sessions / f"{SESSION}.json").read_text())
        cue = {"investigation_id": INV, "seq": 12, "type": "cue.added", "class": "control",
               "authority": "advisory", "created_at": "2026-10-12T00:47:48Z",
               "producer": {"principal": {"kind": "user", "id": "supervisor"},
                            "session_id": "supervisor-session", "is_investigation_author": True},
               "payload": {"text": "Inspect accounting headers and pod placement."}}
        inbox = sessions / f"{SESSION}.inbox.json"
        inbox.write_text(json.dumps({"investigation_id": INV, "after": binding["cursor"],
                                     "through": 12, "events": [cue]}))
        post = {**SESSION_START, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git status --short"}, "tool_response": "",
                "tool_use_id": "call_status"}
        delivered = self.run_hook("ToolEvidence", post, CARDINAL_INVESTIGATION_POLLER="0")
        self.assertIn("authority: ADVISORY", delivered.stdout)
        self.assertIn("If work is planned or underway, use accepted, not noted, even for a cue", delivered.stdout)
        self.assertIn("does not make it owner authorization", delivered.stdout)
        self.assertEqual(json.loads((sessions / f"{SESSION}.json").read_text())["cursor"], 12)
        self.assertFalse(inbox.exists())

    def test_legacy_projection_flag_does_not_change_session_authoring(self):
        self.setup_live(enabled=False)
        ctx = self.start()
        self.assertNotIn("Wait for an affirmative reply", ctx)
        self.assertNotIn("Do not offer again", ctx)
        self.assertNotIn("automatically projects", ctx)

    def test_provisional_title_is_reminded_until_cli_names_it(self):
        self.setup_live()
        self.fake.routes["ensure-session-investigation"][1]["question_status"] = "provisional"
        self.start()
        post = {**SESSION_START, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "git status --short"}, "tool_response": "",
                "tool_use_id": "call_title"}
        delivered = self.run_hook("ToolEvidence", post, CARDINAL_INVESTIGATION_POLLER="0")
        self.assertIn("default title", delivered.stdout)
        self.assertIn(str(CLI), delivered.stdout)
        self.assertNotIn("default title", self.run_hook("ToolEvidence", post,
                                                       CARDINAL_INVESTIGATION_POLLER="0").stdout)
        self.fake.routes["set-investigation-question"] = (200, {
            "investigation_id": INV, "question_status": "stated"})
        named = self.cli("question", "Fix missing investigation titles", "--json")
        self.assertEqual(named.returncode, 0, named.stderr)
        binding_path = self.home / ".cardinal" / "investigations" / "sessions" / f"{SESSION}.json"
        self.assertEqual(json.loads(binding_path.read_text())["question_status"], "stated")
        self.assertNotIn("default title", self.run_hook("ToolEvidence", post,
                                                       CARDINAL_INVESTIGATION_POLLER="0").stdout)

    def test_joined_non_author_cannot_checkpoint(self):
        self.setup_live(author=False)
        ctx = self.start()
        self.assertIn("another author's", ctx)
        self.assertNotIn("use accepted", ctx)
        before = len(self.fake.requests)
        out = self.cli("checkpoint", data='[]')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("only the investigation's author", out.stderr)
        self.assertEqual(before, len(self.fake.requests))

    def test_optout_does_not_bootstrap(self):
        self.setup_live()
        self.start(CARDINAL_STORYBOARD_SESSION_START="0")
        self.assertEqual(self.fake.requests, [])

    def test_ephemeral_thread_does_not_bootstrap_or_discover(self):
        # The Codex app's ambient suggestions run as ephemeral threads:
        # SessionStart with transcript_path null.
        self.setup_live()
        ctx = self.session_context({**SESSION_START, "transcript_path": None}, CARDINAL_INVESTIGATION_POLLER="0")
        self.assertIn("one branch = one initiative", ctx)
        self.assertNotIn("Cardinal session id", ctx)
        self.assertNotIn(f"/storyboards/{SB}", ctx)
        self.assertEqual(self.fake.requests, [])
        out = self.cli("question", "Suggest the next user message")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("has no investigation yet", out.stderr)

    def test_grantee_does_not_bootstrap_or_checkpoint(self):
        self.setup_live()
        self.start(CARDINAL_INVESTIGATION_TOKEN="not-an-author-token")
        self.assertFalse(any(r["path"].endswith("/ensure-session-investigation") for r in self.fake.requests))
        out = self.cli("checkpoint", data='[]', CARDINAL_INVESTIGATION_TOKEN="not-an-author-token")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("cannot act as the author", out.stderr)

    def test_failed_bootstrap_is_fail_open_and_backs_off(self):
        self.setup_live()
        self.fake.routes["ensure-session-investigation"] = (503, {"error": "unavailable"})
        self.start()
        self.start()
        self.assertEqual(sum(r["path"].endswith("/ensure-session-investigation") for r in self.fake.requests), 1)

    def test_capture_to_checkpoint_uploads_only_cited_evidence(self):
        self.setup_live()
        self.start()
        post = {**SESSION_START, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": "cat README.md"}, "tool_response": "documented behavior",
                "tool_use_id": "call_read_readme"}
        captured = self.run_hook("ToolEvidence", post, CARDINAL_INVESTIGATION_POLLER="0")
        import re
        evidence = re.search(r'ev_[a-f0-9]+', captured.stdout).group(0)
        self.fake.routes["upload-investigation-evidence"] = (200, {
            "investigation_id": INV, "results": [{"index": 0, "receipt_id": RCPT}]})
        self.fake.routes["checkpoint-investigation"] = (200, {
            "investigation_id": INV, "events": [{"seq": 1, "type": "finding.proposed",
                                                    "payload": {"semantic_id": "finding_readme"}}]})
        out = self.cli("checkpoint", data=json.dumps([{"type": "finding.proposed", "id": "finding_readme",
                       "statement": "README documents the behavior", "evidence": [evidence]}]))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("checkpointed #1", out.stdout)
        upload = next(r for r in self.fake.requests if r["path"].endswith("/upload-investigation-evidence"))
        self.assertEqual(len(upload["body"]["items"]), 1)
        checkpoint = next(r for r in self.fake.requests if r["path"].endswith("/checkpoint-investigation"))
        self.assertIn(RCPT, json.dumps(checkpoint["body"]))
        self.assertNotIn(evidence, json.dumps(checkpoint["body"]))
        self.assertEqual(checkpoint["body"]["client"], f"codex/{VERSION}")

    def test_invalid_checkpoint_fails_without_post(self):
        self.setup_live()
        self.start()
        before = len(self.fake.requests)
        out = self.cli("checkpoint", data='[{"type":"owner_input.recorded"}]')
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(len(self.fake.requests), before)
