"""Whether sessions record (cardinal_core.investigation_mode): the user's mode,
the per-session override, and where they take effect (bootstrap, the
binding, the poller, session start, the CLI)."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cardinal_core import investigation_agent as agent
from cardinal_core import investigation_bootstrap as boot
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_mode as mode
from cardinal_core import investigation_poller as ip

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_investigation_events import INV, SID, SID2  # noqa: E402
from test_investigation_lifecycle import LifecycleMaestro  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def bind(self, sid=SID):
        ie.write_binding(self.home, sid, {"investigation_id": INV, "cursor": 0, "org": "o1", "is_author": True})


class Modes(Base):
    def test_record_is_the_default_until_set(self):
        self.assertIsNone(mode.user_mode(self.home))
        self.assertEqual(mode.effective(self.home, SID), mode.RECORD)
        self.assertTrue(mode.recording(self.home, SID))

    def test_the_users_mode_applies_to_every_session(self):
        mode.set_user_mode(self.home, mode.OFF)
        self.assertFalse(mode.recording(self.home, SID))
        self.assertFalse(mode.recording(self.home, SID2))
        self.assertEqual(oct(mode.user_path(self.home).stat().st_mode & 0o777), "0o600")
        mode.set_user_mode(self.home, mode.RECORD)
        self.assertTrue(mode.recording(self.home, SID))

    def test_a_session_override_wins_either_way(self):
        mode.set_session_mode(self.home, SID, mode.OFF)
        self.assertFalse(mode.recording(self.home, SID))
        self.assertTrue(mode.recording(self.home, SID2))
        self.assertEqual(mode.why_off(self.home, SID), "this session")
        mode.set_user_mode(self.home, mode.OFF)
        mode.set_session_mode(self.home, SID, mode.RECORD)
        self.assertTrue(mode.recording(self.home, SID))
        self.assertFalse(mode.recording(self.home, SID2))
        self.assertEqual(mode.why_off(self.home, SID2), "your investigation mode")

    def test_unknown_or_malformed_files_mean_the_default(self):
        mode.user_path(self.home).parent.mkdir(parents=True)
        mode.user_path(self.home).write_text('{"mode": "sometimes"}')
        self.assertTrue(mode.recording(self.home, SID))
        mode.user_path(self.home).write_text("not json")
        self.assertTrue(mode.recording(self.home, SID))
        with self.assertRaises(ValueError):
            mode.set_user_mode(self.home, "sometimes")

    def test_the_session_file_is_pruned_with_the_session(self):
        self.bind()
        mode.set_session_mode(self.home, SID, mode.OFF)
        old = 1_000_000.0
        for p in ie.sessions_dir(self.home).iterdir():
            os.utime(str(p), (old, old))
        ie.prune(self.home, now=old + ie.PRUNE_AFTER + 1)
        self.assertFalse(mode.session_path(self.home, SID).exists())


class WhereItTakesEffect(Base):
    def test_a_session_that_does_not_record_reads_as_unbound_and_keeps_its_file(self):
        self.bind()
        mode.set_session_mode(self.home, SID, mode.OFF)
        self.assertIsNone(ie.read_binding(self.home, SID))
        self.assertTrue(ie.binding_path(self.home, SID).exists())
        mode.set_session_mode(self.home, SID, mode.RECORD)
        self.assertEqual(ie.read_binding(self.home, SID)["investigation_id"], INV)

    def test_bootstrap_sends_no_request_when_off(self):
        fake = LifecycleMaestro()
        self.addCleanup(fake.close)
        mode.set_user_mode(self.home, mode.OFF)
        res = boot.ensure(self.home, SID, fake.conn, "t", force=True)
        self.assertEqual(res, {"status": "off", "binding": None, "error": None})
        self.assertEqual(fake.requests, [])
        mode.set_session_mode(self.home, SID, mode.RECORD)
        self.assertEqual(boot.ensure(self.home, SID, fake.conn, "t")["status"], "ok")

    def test_the_poller_stops_when_the_session_turns_off(self):
        self.bind()
        mode.set_session_mode(self.home, SID, mode.OFF)
        why = ip._loop(self.home, SID, lambda: {}, "t", None, lambda: False, 5, 60, 60, True, 0, None)
        self.assertEqual(why, "off")

    def test_session_start_says_it_is_off_and_how_to_turn_it_on(self):
        mode.set_user_mode(self.home, mode.OFF)
        fake = LifecycleMaestro()
        self.addCleanup(fake.close)
        wiring = SimpleNamespace(runtime="codex", cli="/x/cli", environ={}, client="codex/test",
                                 paths=SimpleNamespace(home=self.home / ".codex"),
                                 connection=lambda: fake.conn, _off=lambda name: False)
        text = agent.start(wiring, SID, "startup")
        self.assertTrue(text.startswith(f"Cardinal session id for this session: {SID}."), text)
        self.assertIn("recording is off for this session (your investigation mode)", text)
        self.assertIn(f"{agent.command(wiring)} on --session {SID}", text)
        self.assertEqual(fake.requests, [])


class CLI(Base):
    def setUp(self):
        super().setUp()
        self.fake = LifecycleMaestro()
        self.addCleanup(self.fake.close)
        self.wiring = SimpleNamespace(paths=SimpleNamespace(home=self.home / ".codex"), environ={},
                                      client="codex/test", cli=str(self.home / "scripts/cli"),
                                      connection=lambda: self.fake.conn)

    def run_cli(self, *argv, legacy=False):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent.cli_main(list(argv), self.wiring, legacy=legacy)
        return code, out.getvalue(), err.getvalue()

    def test_mode_prints_and_sets_the_users_mode(self):
        self.assertEqual(self.run_cli("mode")[1], "investigation mode: record (the default; not set)\n")
        code, out, _ = self.run_cli("mode", "off")
        self.assertEqual(code, 0)
        self.assertIn("investigation mode: off", out)
        self.assertEqual(mode.user_mode(self.home), mode.OFF)
        self.assertEqual(self.run_cli("mode", "record")[1], "investigation mode: record\n")
        self.assertEqual(self.run_cli("mode", "--json")[1], '{"mode": "record", "default": false}\n')
        self.assertEqual(self.fake.requests, [])

    def test_off_then_on_for_one_session(self):
        code, out, _ = self.run_cli("off", "--session", SID)
        self.assertEqual(code, 0)
        self.assertIn("recording is off for this session", out)
        code, _, err = self.run_cli("link", "--session", SID)
        self.assertEqual(code, 1)
        self.assertIn("off for this session (this session)", err)
        self.assertEqual(self.fake.requests, [])
        code, out, _ = self.run_cli("on", "--session", SID)
        self.assertEqual(code, 0, out)
        self.assertIn(INV, out)
        self.assertTrue(mode.recording(self.home, SID))

    def test_on_records_one_session_while_the_users_mode_is_off(self):
        mode.set_user_mode(self.home, mode.OFF)
        self.assertEqual(self.run_cli("on", "--session", SID)[0], 0)
        self.assertIsNotNone(ie.read_binding(self.home, SID))
        self.assertIsNone(ie.read_binding(self.home, SID2))


if __name__ == "__main__":
    unittest.main()
