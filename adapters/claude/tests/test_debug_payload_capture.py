"""Tests for CARDINAL_CLAUDE_DEBUG_PAYLOADS capture (Phase 0.D of the
Agent Execution Graph plan — docs/local-notes/plans/agent-execution-graph.md
+ docs/local-notes/plans/phase0-fixture-inventory.md, which flagged Claude
as the one adapter where `dump_debug_payload` was never wired into any
hook).

Every hook under hooks/ is expected to call the shared
adapters/claude/hooks/_debug_capture.dump_if_enabled(event, payload) as
the FIRST thing after parsing stdin. These tests run each hook as a real
subprocess (exactly how Claude Code invokes it), the same harness pattern
tests/fixtures.py already uses, with HOME pointed at a temp dir so the
dump lands at <tmp>/.claude/cardinal/telemetry/debug/.

Deliberately minimal payloads: each one is crafted to make the hook exit
(silently, returncode 0) shortly after the dump point, without needing a
live OTLP stub, an OAuth stub, or the `invariant` checkout — proving the
capture happens BEFORE the hook's normal processing/network/subprocess
work, not as a side effect of it completing successfully.

Run with: python3 -m unittest tests.test_debug_payload_capture -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import ADAPTER_HOOKS_DIR, base_env, run_hook  # noqa: E402

DEBUG_ENV = "CARDINAL_CLAUDE_DEBUG_PAYLOADS"

# (hook filename, hook_event_name the hook dumps under, a minimal payload
# that lets the hook exit cleanly right after the dump point).
HOOKS: list[tuple[str, str, dict]] = [
    ("git-state.py", "UserPromptSubmit", {"session_id": "s1", "cwd": "/nonexistent", "prompt": "hi"}),
    ("limits-gate.py", "UserPromptSubmit", {"session_id": "s1"}),
    ("initiative-convention.py", "SessionStart", {"session_id": "s1", "cwd": "/nonexistent"}),
    ("plan-state.py", "SessionStart", {"session_id": "s1"}),
    ("invariant-check.py", "PreToolUse", {"tool_name": "Edit"}),
    ("subagent-usage.py", "PostToolUse", {"session_id": "s1", "tool_name": "Other"}),
    ("turn-usage.py", "Stop", {"session_id": "s1"}),
    ("plan-usage.py", "Stop", {"session_id": "s1"}),
]


class DebugPayloadCaptureTests(unittest.TestCase):
    def _run(self, hook_name: str, payload: dict, *, enabled: bool):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        env = base_env(home)
        if enabled:
            env[DEBUG_ENV] = "1"
        proc = run_hook(ADAPTER_HOOKS_DIR / hook_name, payload, env)
        debug_dir = home / ".claude" / "cardinal" / "telemetry" / "debug"
        return proc, debug_dir

    def test_each_hook_dumps_when_flag_enabled(self):
        for hook_name, event, payload in HOOKS:
            with self.subTest(hook=hook_name):
                proc, debug_dir = self._run(hook_name, payload, enabled=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                dumps = list(debug_dir.glob(f"{event}-*.json"))
                self.assertEqual(
                    len(dumps), 1,
                    f"{hook_name}: expected one {event}-*.json, found "
                    f"{list(debug_dir.iterdir()) if debug_dir.exists() else 'no debug dir'}",
                )
                self.assertEqual(json.loads(dumps[0].read_text()), payload)

    def test_each_hook_silent_without_flag(self):
        for hook_name, _event, payload in HOOKS:
            with self.subTest(hook=hook_name):
                proc, debug_dir = self._run(hook_name, payload, enabled=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(
                    debug_dir.exists(),
                    f"{hook_name}: debug dir must not exist without {DEBUG_ENV}=1",
                )

    def test_known_secret_pattern_is_scrubbed_at_capture_time(self):
        # ghp_ + 36 alnum chars is the GITHUB_PAT pattern
        # (cardinal_core.redaction.KNOWN_SECRET_PATTERNS).
        token = "ghp_" + "a" * 36
        payload = {"session_id": "s1", "cwd": "/nonexistent", "prompt": f"use {token} to auth"}
        proc, debug_dir = self._run("git-state.py", payload, enabled=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dumps = list(debug_dir.glob("UserPromptSubmit-*.json"))
        self.assertEqual(len(dumps), 1)
        text = dumps[0].read_text()
        self.assertNotIn(token, text)
        self.assertIn("<redacted:GITHUB_PAT>", text)

    def test_payload_structure_is_preserved(self):
        payload = {
            "session_id": "s1",
            "tool_name": "Other",
            "tool_input": {
                "nested": {"list": [1, 2, "three"], "flag": True, "missing": None},
            },
        }
        proc, debug_dir = self._run("subagent-usage.py", payload, enabled=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        dumps = list(debug_dir.glob("PostToolUse-*.json"))
        self.assertEqual(len(dumps), 1)
        loaded = json.loads(dumps[0].read_text())
        self.assertEqual(loaded, payload)
        self.assertEqual(loaded["tool_input"]["nested"]["list"], [1, 2, "three"])
        self.assertIs(loaded["tool_input"]["nested"]["flag"], True)
        self.assertIsNone(loaded["tool_input"]["nested"]["missing"])


if __name__ == "__main__":
    unittest.main()
