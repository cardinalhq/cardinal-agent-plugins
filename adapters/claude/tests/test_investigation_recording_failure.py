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
                **os.environ,
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
