"""Reachability warnings survive evidence opt-out and are visible to the user."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "evidence-capture.py"
SID = "failure-session"
COMMAND = "python /plugin/cardinal-storyboard investigation checkpoint --session failure-session"


class RecordingFailureHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def hook(self, response=None, *, error=None, **env):
        payload = {
            "session_id": SID,
            "tool_name": "Bash",
            "tool_input": {"command": COMMAND},
            "hook_event_name": "PostToolUseFailure" if error else "PostToolUse",
            "tool_response": response,
            "error": error,
        }
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.home),
                "CARDINAL_EVIDENCE_CAPTURE": "0",
                "CARDINAL_EVIDENCE_CONTEXT": "0",
                **env,
            },
            timeout=5,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout) if result.stdout else None

    def test_exit_three_warns_additional_context_and_system_message_throttles_then_clears(self):
        result = self.hook(error="Process exited with code 3\nNOT RECORDED — Cardinal is unreachable (DNS)")
        message = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("could not record 1", message)
        self.assertEqual(result["systemMessage"], message)
        state = self.home / ".cardinal" / "investigations" / "sessions" / f"{SID}.failures.json"
        self.assertEqual(json.loads(state.read_text())["count"], 1)
        self.assertIsNone(self.hook({"exit_code": 3, "stderr": "DNS"}))
        self.assertEqual(json.loads(state.read_text())["count"], 2)
        self.assertIsNone(self.hook({"exit_code": 0, "stdout": "checkpointed #1"}))
        self.assertFalse(state.exists())
        self.assertFalse((self.home / ".cardinal" / "evidence" / SID).exists())

    def test_usage_error_is_never_recording_failure(self):
        self.assertIsNone(self.hook(error="Exit code: 2\nnot checkpointed: invalid JSON"))
        self.assertFalse((self.home / ".cardinal" / "investigations").exists())


if __name__ == "__main__":
    unittest.main()

from test_investigation_events_hook import Base as StopBase, SID as STOP_SID, INV as STOP_INV
import time


class StopReminderGuards(StopBase):
    def test_reminder_requires_matching_connection_org_and_no_grant_token(self):
        self.bind()
        binding = {
            "investigation_id": STOP_INV,
            "cursor": 5,
            "org": "o1",
            "is_author": True,
            "checked_at": time.time(),
            "capabilities": {"owner_decision": {"enabled": True}},
            "pending_decisions": [{"seq": 5, "reminders": 0, "event": {"payload": {"text": "FORGED"}}}],
        }
        for org, token in (("other-org", ""), ("o1", "scoped")):
            self.binding_file().write_text(json.dumps({**binding, "org": org}))
            result = self.hook("Stop", poll=False, env={**self.env, "CARDINAL_INVESTIGATION_TOKEN": token})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.binding()["pending_decisions"][0]["reminders"], 0)
        self.binding_file().write_text(json.dumps(binding))
        result = self.hook("Stop", poll=False)
        text = json.loads(result.stdout)["reason"]
        self.assertIn("events " + STOP_INV + " --after 4", text)
        self.assertNotIn("FORGED", text)
        self.assertNotIn("OWNER DECISION", text)
