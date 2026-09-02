"""Prompt hook must not cross-attach a fresh session to a stale thread.

Regression for the "new session inherits the previous session's DAG"
report. Exercises the actual hook script end-to-end via subprocess with
the payload shape Claude Code delivers.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


HOOK = (
    Path(__file__).resolve().parents[1]
    / "skills" / "semantic-dag" / "hooks" / "prompt_hook.py"
)


class PromptHookSessionIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.cwd = Path(self.temporary.name) / "workspace"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.pointer = (
            self.state
            / f"current-{hashlib.sha1(str(self.cwd).encode()).hexdigest()[:12]}"
        )
        self.state.mkdir(parents=True, exist_ok=True)
        self.thread_dir = self.state / "threads" / "c-owning-thread"
        self.thread_dir.mkdir(parents=True, exist_ok=True)
        # Watch mode ON — otherwise the hook exits before session logic.
        (self.thread_dir / "dag.json").write_text(json.dumps({"watch_mode": True}))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run(self, session_id: str, prompt: str = "continue the task") -> tuple[int, str]:
        env = os.environ.copy()
        env["SEMANTIC_DAG_STATE_DIR"] = str(self.state)
        payload = json.dumps({
            "session_id": session_id,
            "cwd": str(self.cwd),
            "user_prompt": prompt,
        })
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        return result.returncode, result.stdout

    def _binding_for(self, session_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in session_id)[:128]
        return self.state / "bindings" / f"{safe}.json"

    def test_new_session_ignores_pointer_written_by_other_session(self) -> None:
        # Session A wrote the pointer; session B is fresh.
        self.pointer.write_text(json.dumps({
            "thread": "c-owning-thread",
            "session_id": "sess-A",
        }))
        rc, _ = self._run("sess-B")
        self.assertEqual(rc, 0)
        # No binding for B, and B did NOT emit the watch-mode context
        # (which would only fire if a thread was discovered).
        self.assertFalse(self._binding_for("sess-B").exists())

    def test_same_session_adopts_pointer_and_writes_binding(self) -> None:
        self.pointer.write_text(json.dumps({
            "thread": "c-owning-thread",
            "session_id": "sess-A",
        }))
        rc, stdout = self._run("sess-A")
        self.assertEqual(rc, 0)
        self.assertTrue(self._binding_for("sess-A").exists())
        binding = json.loads(self._binding_for("sess-A").read_text())
        self.assertEqual(binding.get("thread"), "c-owning-thread")

    def test_task_notification_prompt_does_not_open_a_turn(self) -> None:
        # A same-session pointer WOULD have caused this hook to reset the
        # DAG and add a new GOAL, but task-notification bodies are Claude
        # Code's synthetic wake-ups from backgrounded work — not user
        # turns. They must be a no-op.
        self.pointer.write_text(json.dumps({
            "thread": "c-owning-thread",
            "session_id": "sess-same",
        }))
        rc, stdout = self._run(
            "sess-same",
            prompt=(
                "task-notification task-id ab1e8e9354a503ff2 task-id "
                "output-file private tmp jobs foo.output"
            ),
        )
        self.assertEqual(rc, 0)
        # Hook returns silently — no additionalContext, no systemMessage.
        self.assertEqual(stdout.strip(), "")

    def test_legacy_bare_pointer_is_not_adopted(self) -> None:
        # Pre-fix pointers have no session_id; the fix must refuse them.
        self.pointer.write_text("c-owning-thread")
        rc, _ = self._run("sess-fresh")
        self.assertEqual(rc, 0)
        self.assertFalse(self._binding_for("sess-fresh").exists())


if __name__ == "__main__":
    unittest.main()
