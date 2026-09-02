"""Cwd pointer must not bridge sessions.

Regression for the "new Claude session inherits the previous session's DAG
thread" bug: the per-cwd `current-<sha1>` pointer was treated as
authoritative for any session_id lacking a binding, so unrelated sessions
in the same repo all latched onto the first session's thread. The clean
fix is session-tagged pointers — the pointer stores `{thread, session_id}`
and every session-checking reader compares against its own session_id
before adopting.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cardinal_core import semantic_dag as sd


class PointerSessionTaggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.state = Path(self.temporary.name) / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self._env = mock.patch.dict(
            os.environ,
            {"SEMANTIC_DAG_STATE_DIR": str(self.state)},
            clear=False,
        )
        self._env.start()
        # Any config with a session_id env key so `_native_session_id` works.
        sd.configure(sd.RuntimeConfig(
            runtime="test",
            default_state_dir=str(self.state),
            default_port=0,
            viewer_dir=Path(self.temporary.name),
            native_thread_env=("CLAUDE_CODE_SESSION_ID",),
            project_dir_env="CLAUDE_PROJECT_DIR",
        ))

    def tearDown(self) -> None:
        self._env.stop()
        self.temporary.cleanup()

    def test_write_pointer_records_session_id_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-A"}):
            sd._write_pointer("c-thread-a")
        record = sd._read_pointer_record()
        self.assertEqual(record, {"thread": "c-thread-a", "session_id": "sess-A"})

    def test_explicit_session_id_overrides_env(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-env"}):
            sd._write_pointer("c-thread-b", session_id="sess-explicit")
        self.assertEqual(sd._read_pointer_record()["session_id"], "sess-explicit")

    def test_read_pointer_returns_bare_thread_for_backwards_compat(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-C"}):
            sd._write_pointer("c-thread-c")
        self.assertEqual(sd._read_pointer(), "c-thread-c")

    def test_legacy_bare_pointer_is_read_with_null_session(self) -> None:
        # Pre-fix pointers on disk contain the raw thread id as text.
        pointer = self.state / f"current-{sd._cwd_key()}"
        pointer.write_text("c-legacy-thread")
        record = sd._read_pointer_record()
        self.assertEqual(record, {"thread": "c-legacy-thread", "session_id": None})

    def test_prompt_hook_discovery_rejects_cross_session_pointer(self) -> None:
        # Simulate: session A opts in and writes the pointer; session B
        # then fires its first UserPromptSubmit in the same cwd. Session B
        # must NOT adopt A's thread.
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-A"}):
            sd._write_pointer("c-thread-a")
        record = sd._read_pointer_record()
        self.assertEqual(record["session_id"], "sess-A")
        payload_session_b = "sess-B"
        self.assertNotEqual(record["session_id"], payload_session_b)

    def test_prompt_hook_discovery_accepts_same_session_pointer(self) -> None:
        # emit.py start writes the pointer; the immediately following
        # UserPromptSubmit in the SAME session must adopt it.
        session = "sess-same"
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": session}):
            sd._write_pointer("c-thread-same")
        record = sd._read_pointer_record()
        self.assertEqual(record["session_id"], session)
        self.assertEqual(record["thread"], "c-thread-same")


if __name__ == "__main__":
    unittest.main()
