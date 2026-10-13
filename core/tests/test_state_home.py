"""Hook state of seatbelt-sandboxed sessions (paths.state_home): inside a
whole-process seatbelt sandbox (SANDBOX=sandbox-exec, e.g. `gemini -s`) the
hooks keep their Investigation bindings and evidence spool under the
relocated, agent-writable <home>/.cache/cardinal/sandboxed.

Run from core/:  python3 -m unittest tests.test_state_home -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import evidence
from cardinal_core import investigation_events as ie
from cardinal_core import paths

BOXED = {"SANDBOX": "sandbox-exec"}
GSID = "8a6e4964-0d1f-4c55-9f0e-2b7c1d3a4e5f"


class StateRelocation(unittest.TestCase):
    def test_state_home(self):
        home = Path("/Users/x")
        self.assertEqual(paths.state_home(home, {}), home)
        self.assertEqual(paths.state_home(home, BOXED), home / ".cache" / "cardinal" / "sandboxed")
        self.assertEqual(paths.state_home(home, {"SANDBOX": "docker"}), home)
        boxed = paths.sandboxed_home(home)
        self.assertEqual(paths.state_home(boxed, BOXED), boxed)          # idempotent
        self.assertEqual(paths.state_home(boxed, {}), boxed)
        self.assertEqual(paths.unsandboxed_home(boxed), home)

    def test_hook_state_and_evidence_move_inside_the_sandbox(self):
        home = Path("/Users/x")
        with patch.dict(os.environ, BOXED):
            self.assertEqual(ie.sessions_dir(home),
                             home / ".cache/cardinal/sandboxed/.cardinal/investigations/sessions")
            self.assertEqual(evidence.default_root(home), home / ".cache/cardinal/sandboxed/.cardinal/evidence")
        with patch.dict(os.environ, {"SANDBOX": ""}):
            self.assertEqual(ie.sessions_dir(home), home / ".cardinal" / "investigations" / "sessions")
            self.assertEqual(evidence.default_root(home), home / ".cardinal" / "evidence")

    def test_checkpoint_holds_land_in_the_sandboxed_spool(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            boxed_root = paths.sandboxed_home(home) / ".cardinal" / "evidence"
            (boxed_root / GSID).mkdir(parents=True)
            with patch.dict(os.environ, BOXED):
                evidence.hold_after_checkpoint(home, GSID, env={})
            self.assertEqual(evidence.read_hold(boxed_root, GSID)["reason"], "checkpoint")
            self.assertFalse((home / ".cardinal" / "evidence").exists())

    def test_the_users_evidence_opt_out_reaches_the_sandboxed_spool(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            with patch.dict(os.environ, BOXED):
                root = evidence.default_root(home)
                self.assertFalse(evidence.capture_disabled(root, {}))
                (home / ".cardinal" / "evidence").mkdir(parents=True)
                (home / ".cardinal" / "evidence" / evidence.DISABLED_FLAG).write_text("")
                self.assertTrue(evidence.capture_disabled(root, {}))


if __name__ == "__main__":
    unittest.main()
