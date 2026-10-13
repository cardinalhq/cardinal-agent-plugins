"""Evidence holds: the .hold marker (evidence.set_hold / release_hold /
read_hold), and `cardinal-evidence hold` / `status` / `promote` as the user
sees them (evidence_promote). gc's handling of holds is in
test_evidence_spool_bounds.HoldGcTests.

Run with: cd core && python3 -m unittest tests.test_evidence_hold -v
"""

from __future__ import annotations

import io
import json
import os
import stat
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cardinal_core import evidence
from cardinal_core import evidence_promote as promote

SID = "11111111-2222-3333-4444-555555555555"
SB = "sb_" + "a" * 24


class _Home(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = evidence.default_root(self.home)
        evidence.ensure_root(self.root)
        self.now = time.time()

    def put(self, session: str, i: int, size: int = 100) -> Path:
        d = self.root / session
        d.mkdir(exist_ok=True, mode=0o700)
        p = d / f"ev_{i:012x}.json"
        p.write_bytes(b"x" * size)
        return p


class SetHoldTests(_Home):
    def test_marker_is_private_atomic_json_within_retention(self):
        self.put(SID, 1)
        h = evidence.set_hold(self.root, SID, reason="manual", seconds=3 * 86400, now=self.now)
        path = self.root / SID / evidence.HOLD_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        body = json.loads(path.read_text())
        self.assertEqual(sorted(body), ["reason", "set_at", "until"])
        self.assertEqual(body["reason"], "manual")
        self.assertAlmostEqual(h["until"] - h["set_at"], 3 * 86400, delta=1)
        self.assertEqual(evidence.read_hold(self.root, SID, now=self.now)["reason"], "manual")
        self.assertEqual([p.name for p in (self.root / SID).iterdir() if p.name.endswith(".tmp")], [])
        # Replaced, not merged.
        evidence.set_hold(self.root, SID, reason="checkpoint", now=self.now)
        self.assertEqual(evidence.read_hold(self.root, SID, now=self.now)["reason"], "checkpoint")
        self.assertTrue(evidence.release_hold(self.root, SID))
        self.assertFalse(evidence.release_hold(self.root, SID))
        self.assertIsNone(evidence.read_hold(self.root, SID))

    def test_refusals(self):
        self.put(SID, 1)
        for kw in ({"reason": "forever"}, {"reason": "manual", "seconds": evidence.MAX_HOLD_S + 1},
                   {"reason": "manual", "seconds": 0}, {"reason": "manual", "seconds": True}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                evidence.set_hold(self.root, SID, **kw)
        for bad in ("../x", "", None, evidence.NO_SESSION):
            with self.subTest(sid=bad), self.assertRaises(ValueError):
                evidence.set_hold(self.root, bad, reason="manual")
        # No spool directory: nothing to protect.
        self.assertIsNone(evidence.set_hold(self.root, "other", reason="manual"))
        self.assertFalse((self.root / "other").exists())
        self.assertIsNotNone(evidence.set_hold(self.root, "other", reason="wake", create=True))

    def test_a_hold_from_the_future_holds_nothing(self):
        self.put(SID, 1)
        evidence.set_hold(self.root, SID, reason="manual", now=self.now + 2 * evidence.HOLD_CLOCK_SKEW_S)
        self.assertIsNone(evidence.read_hold(self.root, SID, now=self.now))

    def test_hold_after_checkpoint_fails_open(self):
        evidence.hold_after_checkpoint(self.home, SID, env={})          # no spool directory: nothing
        self.assertFalse((self.root / SID).exists())
        self.put(SID, 1)
        evidence.hold_after_checkpoint(self.home, SID, env={"CARDINAL_EVIDENCE_CAPTURE": "0"})
        self.assertIsNone(evidence.read_hold(self.root, SID))
        evidence.hold_after_checkpoint(self.home, "../../etc", env={})  # a bad id raises inside: swallowed
        evidence.hold_after_checkpoint(self.home, SID, env={})
        self.assertEqual(evidence.read_hold(self.root, SID)["reason"], "checkpoint")


def _adapter() -> promote.PromoteAdapter:
    return promote.PromoteAdapter(runtime="test", client="test", connection=lambda home, env: {})


class CliTests(_Home):
    def run_cli(self, *argv) -> tuple:
        # The handlers bind sys.stdout / sys.stderr as defaults: pass them in.
        out, err = io.StringIO(), io.StringIO()
        env = {"HOME": str(self.home), "CARDINAL_EVIDENCE_MAX_MB": "10"}
        with mock.patch.dict(os.environ, env), redirect_stderr(io.StringIO()):
            args = promote.build_parser().parse_args(list(argv))
            code = promote.HANDLERS[args.command](args, _adapter(), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_hold_and_release(self):
        self.put(SID, 1)
        code, out, _ = self.run_cli("hold", SID, "--days", "3")
        self.assertEqual(code, 0)
        self.assertIn("protected while space allows until", out)
        self.assertIn("at most 5.0 MiB of the 10.0 MiB spool", out)
        self.assertIn("still expire 14 days after capture", out)
        for word in ("retained", "durable"):
            self.assertNotIn(word, out)
        h = evidence.read_hold(self.root, SID)
        self.assertEqual(h["reason"], "manual")
        self.assertAlmostEqual(h["until"] - h["set_at"], 3 * 86400, delta=1)
        code, out, _ = self.run_cli("hold", SID)
        h = evidence.read_hold(self.root, SID)
        self.assertAlmostEqual(h["until"] - h["set_at"], 14 * 86400, delta=1)
        code, out, _ = self.run_cli("hold", SID, "--release")
        self.assertEqual(code, 0)
        self.assertIn("Released the hold", out)
        self.assertIsNone(evidence.read_hold(self.root, SID))
        code, out, _ = self.run_cli("hold", SID, "--release")
        self.assertIn("had no hold", out)

    def test_hold_refusals(self):
        self.put(SID, 1)
        code, _, err = self.run_cli("hold", SID, "--days", "15")
        self.assertEqual(code, promote.EXIT_FAILED)
        self.assertIn("--days is 1 to 14", err)
        code, _, err = self.run_cli("hold", SID, "--days", "0")
        self.assertEqual(code, promote.EXIT_FAILED)
        code, _, err = self.run_cli("hold", "nope/../x")
        self.assertIn("not a session id", err)
        code, _, err = self.run_cli("hold", "absent")
        self.assertEqual(code, promote.EXIT_FAILED)
        self.assertIn("no captured evidence for session absent", err)
        with self.assertRaises(SystemExit):
            self.run_cli("hold", SID, "--days", "2", "--release")

    def test_status_lists_holds_bytes_and_breaches(self):
        code, out, _ = self.run_cli("status")
        self.assertIn("holds: none", out)
        self.put(SID, 1, 2048)
        self.put("other", 2, 10)
        evidence.set_hold(self.root, SID, reason="checkpoint")
        evidence._record_breach(self.root, "other", ["ev_000000000002"], self.now)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("holds: 1 session(s), 0.0 MiB protected while space allows (at most 5.0 MiB in all", out)
        self.assertIn(f"  {SID}: held until ", out)
        self.assertIn("(checkpoint)", out)
        self.assertIn("  other: no hold; 1 entries evicted under disk pressure despite a hold", out)

    def test_promote_of_an_id_evicted_despite_a_hold_says_so(self):
        self.put(SID, 1)
        gone = "ev_" + "d" * 12
        evidence._record_breach(self.root, SID, [gone], self.now)
        code, out, _ = self.run_cli("promote", "--storyboard", SB, gone)
        self.assertEqual(code, promote.EXIT_FAILED)
        self.assertIn(f"{gone}: error not_in_spool: evicted under disk pressure despite a hold on session {SID}",
                      out)
        self.assertIn("storyboard__record_evidence", out)
        other = "ev_" + "e" * 12
        code, out, _ = self.run_cli("promote", "--storyboard", SB, other)
        self.assertIn("removed after 14 days", out)
        # A marker whose id list was cut cannot rule an id in or out.
        breach = self.root / SID / evidence.HOLD_BREACHED
        body = json.loads(breach.read_text())
        body["entries_removed"] = 5000
        breach.write_text(json.dumps(body))
        code, out, _ = self.run_cli("promote", "--storyboard", SB, other)
        self.assertIn("may have been one of them", out)


if __name__ == "__main__":
    unittest.main()
