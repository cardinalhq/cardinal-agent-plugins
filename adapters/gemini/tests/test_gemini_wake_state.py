"""The Gemini hook's per-turn sandbox record and relocated Cardinal state
(docs/specs/resume-and-wake.md §5.3 Gemini): a BeforeAgent inside Gemini's
seatbelt sandbox (SANDBOX=sandbox-exec) counts a sandboxed turn under
~/.cache/cardinal/sandboxed/.cardinal, one outside counts an unsandboxed
turn under ~/.cardinal. HOME is a temp dir; no network, no Gemini CLI.

Run from adapters/gemini/:
    python3 -m unittest tests.test_gemini_wake_state -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "cardinal-gemini-telemetry.py"
MONOREPO_ROOT = ROOT.parents[1]
SID = "8a6e4964-0d1f-4c55-9f0e-2b7c1d3a4e5f"


def setUpModule() -> None:  # noqa: N802
    subprocess.run([sys.executable, str(MONOREPO_ROOT / "build" / "vendor.py"), "gemini"], check=True,
                   capture_output=True)


class SandboxTurns(unittest.TestCase):
    def run_before_agent(self, home: Path, **extra: str) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CARDINAL_", "GEMINI_")) and k != "SANDBOX"}
        env.update(HOME=str(home), **extra)
        payload = {"session_id": SID, "hook_event_name": "BeforeAgent", "prompt": "hello", "cwd": str(home)}
        r = subprocess.run([sys.executable, str(HOOK), "--event", "BeforeAgent"], input=json.dumps(payload).encode(),
                           capture_output=True, timeout=20, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def turns(self, state_home: Path) -> dict:
        p = state_home / ".cardinal" / "investigations" / "sessions" / f"{SID}.sandbox-turns.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def test_turns_are_counted_where_their_process_can_write(self):
        with TemporaryDirectory() as t:
            home = Path(t)
            boxed = home / ".cache" / "cardinal" / "sandboxed"
            self.run_before_agent(home, SANDBOX="sandbox-exec")
            self.run_before_agent(home, SANDBOX="sandbox-exec")
            self.assertEqual(self.turns(boxed).get("sandboxed"), 2)
            self.assertEqual(self.turns(home), {})
            self.run_before_agent(home)
            self.assertEqual(self.turns(home).get("unsandboxed"), 1)
            self.assertEqual(self.turns(boxed).get("unsandboxed"), 0)
            self.assertFalse((home / ".cardinal" / "wake").exists())   # hooks never write the waker's state


if __name__ == "__main__":
    unittest.main()
