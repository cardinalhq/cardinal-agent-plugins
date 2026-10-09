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

    def setup_live(self, enabled=True, author=True):
        self.connect()
        self.fake.routes["ensure-session-investigation"] = (200, {
            "investigation_id": INV, "storyboard_id": SB,
            "view_url": f"{self.fake.origin}/storyboards/{SB}",
            "investigation_url": f"/storyboards/{SB}/investigation",
            "is_author": author, "capabilities": {"projection": {"enabled": enabled},
                                                   "owner_input": {"enabled": False}}})

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
        self.assertIn("Wait for an affirmative reply", first)
        self.assertIn("Would you like me to update the storyboard visualization?", second)
        self.assertIn("first progress update", first)
        self.assertIn("written from branch fix/checkout", first)
        self.assertNotIn("pass it as session_id", first)
        self.assertIn("checkpoint", second)
        ensures = [r for r in self.fake.requests if r["path"].endswith("/ensure-session-investigation")]
        self.assertEqual(len(ensures), 1)
        self.assertEqual(ensures[0]["body"]["session_id"], SESSION)
        self.assertEqual(ensures[0]["headers"]["x-cardinal-client"], f"codex/{VERSION}")
        self.assertNotIn("prompt", ensures[0]["body"])
        self.assertNotIn("transcript", ensures[0]["body"])
        self.assertNotIn("record-owner-input", str(self.fake.requests))
        linked = self.cli("link", "--json")
        self.assertEqual(linked.returncode, 0, linked.stderr)
        self.assertEqual(json.loads(linked.stdout)["storyboard_id"], SB)

    def test_legacy_projection_flag_does_not_change_session_authoring(self):
        self.setup_live(enabled=False)
        ctx = self.start()
        self.assertIn("Wait for an affirmative reply", ctx)
        self.assertIn("Do not offer again", ctx)
        self.assertNotIn("automatically projects", ctx)

    def test_joined_non_author_cannot_checkpoint(self):
        self.setup_live(author=False)
        self.assertIn("another author's", self.start())
        before = len(self.fake.requests)
        out = self.cli("checkpoint", data='[]')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("only the investigation's author", out.stderr)
        self.assertEqual(before, len(self.fake.requests))

    def test_optout_does_not_bootstrap(self):
        self.setup_live()
        self.start(CARDINAL_STORYBOARD_SESSION_START="0")
        self.assertEqual(self.fake.requests, [])

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
