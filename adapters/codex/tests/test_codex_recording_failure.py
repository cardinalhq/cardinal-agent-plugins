"""Codex failure visibility and the missing-core launcher contract."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import test_codex_storyboard as fixtures
from test_codex_storyboard import SESSION, SESSION_START, CLI


class FailureHook(unittest.TestCase):
    setUp = fixtures.CodexStoryboardTests.setUp
    tearDown = fixtures.CodexStoryboardTests.tearDown
    env = fixtures.CodexStoryboardTests.env
    run_hook = fixtures.CodexStoryboardTests.run_hook

    def test_detection_runs_with_capture_and_context_off_then_success_clears(self):
        payload = {
            **SESSION_START,
            "tool_name": "Bash",
            "hook_event_name": "PostToolUse",
            "tool_input": {
                "command": f"python /plugin/cardinal-storyboard investigation question Why --session {SESSION}"
            },
            "tool_response": "Process exited with code 3\nNOT RECORDED — DNS failed",
        }
        result = self.run_hook(
            "ToolEvidence", payload, CARDINAL_EVIDENCE_CAPTURE="0", CARDINAL_EVIDENCE_CONTEXT="0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "could not record 1", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        )
        state = self.home / ".cardinal" / "investigations" / "sessions" / f"{SESSION}.failures.json"
        self.assertEqual(json.loads(state.read_text())["count"], 1)
        payload["tool_response"] = 'Process exited with code 0\ninvestigation now asks: "Why"'
        result = self.run_hook(
            "ToolEvidence", payload, CARDINAL_EVIDENCE_CAPTURE="0", CARDINAL_EVIDENCE_CONTEXT="0"
        )
        self.assertFalse(state.exists())
        self.assertEqual(result.stdout, "")

    def test_stop_does_not_deliver_owner_context_in_grantee_or_optout_mode(self):
        directory = self.home / ".cardinal" / "investigations" / "sessions"
        directory.mkdir(parents=True)
        decision = {
            "investigation_id": "inv_" + "a" * 24,
            "seq": 1,
            "class": "owner",
            "type": "owner.decided",
            "payload": {"text": "Proceed"},
            "producer": {"principal": {"kind": "user", "id": "owner"}},
        }
        binding = {
            "investigation_id": decision["investigation_id"],
            "org": "org-test",
            "cursor": 1,
            "is_author": True,
            "capabilities": {"owner_decision": {"enabled": True}},
            "pending_decisions": [{"seq": 1, "event": decision, "reminders": 0}],
        }
        file = directory / f"{SESSION}.json"
        file.write_text(json.dumps(binding))
        for env in ({"CARDINAL_INVESTIGATION_TOKEN": "scoped"}, {"CARDINAL_STORYBOARD_SESSION_START": "0"}):
            result = self.run_hook("Stop", SESSION_START, **env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(file.read_text())["pending_decisions"][0]["reminders"], 0)


class MissingCore(unittest.TestCase):
    def test_only_context_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "hooks").mkdir()
            script = root / "scripts" / "cardinal-storyboard"
            shutil.copyfile(CLI, script)
            for args in [
                ("context",),
                ("discover",),
                ("investigation", "checkpoint"),
                ("receipt", "rcpt_" + "a" * 24),
            ]:
                result = subprocess.run(
                    [sys.executable, "-I", str(script), *args],
                    capture_output=True,
                    text=True,
                    cwd=tmp,
                    env={**os.environ, "HOME": tmp},
                    timeout=5,
                )
                if args == ("context",):
                    self.assertEqual(
                        (result.returncode, result.stdout, result.stderr), (0, '{"context":{}}\n', "")
                    )
                else:
                    self.assertEqual(result.returncode, 3)
                    self.assertEqual(result.stdout, "")
                    self.assertTrue(
                        result.stderr.startswith("NOT RECORDED — the Cardinal plugin core is missing")
                    )


if __name__ == "__main__":
    unittest.main()
