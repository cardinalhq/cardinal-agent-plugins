"""Finding a woken run's surviving processes through the sandbox itself
(review 6, closing the `env -u TMPDIR setsid ...` gap). A process a woken
Codex run starts cannot leave Codex's Seatbelt sandbox, and that sandbox has
this wake's private TMPDIR as a writable root; nothing the agent does
(clearing TMPDIR, setsid, chdir, re-exec) removes it. So a process is this
wake's exactly when it is sandboxed AND its sandbox allows writing the wake's
TMPDIR directory. macOS answers both through sandbox_check:
  sandbox_check(pid, NULL, 0) -> 1 sandboxed;
  sandbox_check(pid, "file-write-create", SANDBOX_FILTER_PATH|NO_REPORT, dir) -> 0 allowed.

Fakes for the waker paths (World.sandboxed_procs); unit tests of
sandboxed_procs / wake_owned with the OS calls mocked; one real macOS check
that starts only /bin/sleep (plain and under sandbox-exec with a profile
whose only writable root is a temp dir).

Run from core/:  python3 -m unittest tests.test_session_wake_survivors -v
"""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from cardinal_core import session_wake as w

from tests.test_session_wake import SID, Base, ack, decision, turn


class Sweep(Base):
    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]
        self.run_pass()
        self.child = self.world.launches[0]
        self.tmp = self.child["env"]["TMPDIR"]
        self.world.events.append(ack(6, 5))

    def exit_and_settle(self, **kw):
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass(**kw)

    def test_a_survivor_is_stopped_and_the_run_judged_only_after(self):
        self.world.boxed = [4242]
        self.exit_and_settle()
        self.assertEqual(self.world.killed, [(4242, signal.SIGTERM)])
        since, dirs, canary = self.world.boxed_calls[-1]
        self.assertEqual(dirs, [self.tmp])                          # probed against the wake's TMPDIR
        self.assertEqual(canary, os.path.dirname(self.tmp))         # canary = the TMPDIR's parent
        self.assertEqual(since, self.world.starts[self.child["pid"]])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.assertFalse(os.path.exists(self.tmp))                   # removed only once none is left

    def test_the_waker_itself_is_never_a_survivor(self):
        self.world.boxed = [os.getpid()]
        self.exit_and_settle()
        self.assertEqual(self.world.killed, [])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_an_unanswerable_check_keeps_the_wake_in_flight(self):
        self.world.boxed = None
        self.exit_and_settle()
        self.assertEqual(self.state(5), w.RESUMING)
        self.assertIn("could not be checked", self.ledger()["5"]["lingering"])

    def test_it_is_bounded_to_fail_after_the_timeout(self):
        self.world.boxed = None
        self.exit_and_settle()
        self.world.t += w.WAKE_TIMEOUT + 10
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        self.assertTrue(os.path.isdir(self.tmp))                     # kept, path recorded
        self.assertIn(self.tmp, self.ledger()["5"]["reason"])
        self.assertTrue(w.disabled_reason(self.home, "codex"))       # launch disabled


class PreClaim(Base):
    def wedge(self):
        """Wake #5, then a survivor that will not clear: #5 wedges to FAILED after the timeout, its
        TMPDIR kept. A later decision then meets the pre-claim survivor scan over that TMPDIR."""
        self.world.events = [decision(5)]
        self.run_pass()
        tmp = self.world.launches[0]["env"]["TMPDIR"]
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.world.boxed = None                                      # the sandbox can never be asked
        self.run_pass()
        self.world.t += w.WAKE_TIMEOUT + 10
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        self.assertTrue(os.path.isdir(tmp))
        return tmp

    def test_a_process_of_an_earlier_wake_makes_the_session_live(self):
        tmp = self.wedge()
        self.world.boxed = [7777]                                    # a survivor of #5 is still running there
        self.world.events.append(decision(7))
        self.world.t += w.LIVE_WINDOW + 1
        self.world.boxed_calls.clear()
        self.run_pass()
        self.assertTrue(any(tmp in dirs for _, dirs, _canary in self.world.boxed_calls))
        self.assertEqual(len(self.world.launches), 1)                # #7 does not launch
        self.assertIn("earlier wake is still running", self.ledger()["7"]["reason"])

    def test_an_unanswerable_check_is_live(self):
        self.wedge()
        self.world.boxed = None
        self.world.events.append(decision(7))
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertIn("could not be checked", self.ledger()["7"]["reason"])

    def test_no_tmpdir_no_scan_and_the_session_can_launch(self):
        # A session with no codex entry carrying a tmp_dir is not scanned; the first wake proceeds.
        self.world.events = [decision(5)]
        self.world.boxed = None   # would make it live IF it were scanned
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertFalse(any(c for c in self.world.boxed_calls if c[1]))   # no non-empty scan before the claim


class ScanUnit(unittest.TestCase):
    """sandboxed_procs / wake_owned with the macOS calls mocked."""

    def run_scan(self, rows, null, path, *, since=100.0, dirs=None, canary="/c", now=1000.0, exists=True):
        import ctypes

        def check_null(pid, op, kind):
            ans, err = null.get(pid, (0, 0))
            ctypes.set_errno(err)
            return ans

        def check_path(pid, op, t, p):
            ans, err = path.get((pid, p.decode()), (1, 0))
            ctypes.set_errno(err)
            return ans
        ps = "\n".join(f"{pid} {uid} {etime}" for pid, uid, etime in rows) + "\n"
        with patch.object(w.sys, "platform", "darwin"), \
                patch.object(w, "_mac_calls", lambda: (check_null, check_path)), \
                patch.object(w.time, "time", lambda: now), \
                patch.object(w.os.path, "isdir", lambda d: exists), \
                patch.object(w.os.path, "realpath", lambda d: d), \
                patch.object(w.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, ps, "")), \
                patch.object(w.os, "getuid", lambda: 501), patch.object(w.os, "getpid", lambda: 1):
            return w.sandboxed_procs(since, dirs if dirs is not None else ["/t"], canary)

    def test_sandboxed_allowed_on_tmpdir_and_denied_on_canary_is_ours(self):
        rows = [(10, 501, "01:00"),    # sandboxed, allowed /t, denied canary -> ours
                (11, 501, "01:00"),    # sandboxed, denied /t -> not ours (another wake / Codex session)
                (12, 501, "01:00"),    # not sandboxed, allowed everything (editor) -> not ours
                (16, 501, "01:00"),    # sandboxed, allowed /t AND allowed canary (Apple, broad $HOME) -> not ours
                (13, 0, "01:00"),      # another user
                (14, 501, "20:00"),    # started before `since`
                (15, 501, "01:00")]    # gone mid-check
        null = {10: (1, 0), 11: (1, 0), 12: (0, 0), 16: (1, 0), 14: (1, 0), 15: (1, 0)}
        path = {(10, "/t"): (0, 0), (10, "/c"): (1, 0),
                (11, "/t"): (1, 0),
                (16, "/t"): (0, 0), (16, "/c"): (0, 0),
                (15, "/t"): (1, errno.ESRCH)}
        self.assertEqual(self.run_scan(rows, null, path), [10])

    def test_allowed_on_both_tmpdir_and_canary_is_not_a_survivor(self):
        # Review 7 F1: an Apple agent whose profile allows writes broadly under $HOME is allowed on
        # the TMPDIR too, but the canary rules it out.
        null = {20: (1, 0)}
        path = {(20, "/t"): (0, 0), (20, "/c"): (0, 0)}
        self.assertEqual(self.run_scan([(20, 501, "01:00")], null, path), [])

    def test_several_tmpdirs_any_match(self):
        null = {21: (1, 0)}
        path = {(21, "/a"): (1, 0), (21, "/b"): (0, 0), (21, "/c"): (1, 0)}
        self.assertEqual(self.run_scan([(21, 501, "01:00")], null, path, dirs=["/a", "/b"]), [21])

    def test_missing_tmpdirs_are_dropped(self):
        self.assertEqual(self.run_scan([(30, 501, "01:00")], {30: (1, 0)}, {}, exists=False), [])

    def test_a_missing_canary_fails_closed(self):
        # Review 7 F1: a missing canary would answer "denied" for everyone (false positives), so the
        # probe fails closed. Here isdir is True for the tmpdir but the canary is None/empty.
        null = {31: (1, 0)}
        path = {(31, "/t"): (0, 0)}
        self.assertIsNone(self.run_scan([(31, 501, "01:00")], null, path, canary=None))
        self.assertIsNone(self.run_scan([(31, 501, "01:00")], null, path, canary=""))

    def test_an_odd_sandbox_answer_fails_closed(self):
        rows = [(40, 501, "01:00")]
        self.assertIsNone(self.run_scan(rows, {40: (1, errno.EINVAL)}, {}))            # NULL check errored
        self.assertIsNone(self.run_scan(rows, {40: (2, 0)}, {}))                        # neither 0 nor 1
        self.assertIsNone(self.run_scan(rows, {40: (1, 0)}, {(40, "/t"): (-1, 0)}))     # path answer -1
        self.assertIsNone(self.run_scan(rows, {40: (1, 0)}, {(40, "/t"): (0, 0), (40, "/c"): (-1, 0)}))  # canary -1
        self.assertIsNone(self.run_scan([(41, 501, "bad")], {41: (1, 0)}, {}))          # unreadable etime

    def test_a_process_gone_at_the_null_check_is_skipped(self):
        self.assertEqual(self.run_scan([(50, 501, "01:00")], {50: (1, errno.ESRCH)}, {}), [])

    def test_wake_owned_direct(self):
        import ctypes

        def null(pid, op, kind):
            ctypes.set_errno(0)
            return 1 if pid != 9 else 0

        def path(pid, op, t, p):
            ctypes.set_errno(0)
            if p == b"/c":
                return 0 if pid == 6 else 1           # pid 6's sandbox grants the canary (Apple-like)
            return 0 if (pid in (6, 7) and p == b"/t") else 1
        calls = (null, path)
        self.assertIs(w.wake_owned(7, ["/t"], "/c", calls), True)
        self.assertIs(w.wake_owned(6, ["/t"], "/c", calls), False)   # granted /t but also the canary
        self.assertIs(w.wake_owned(8, ["/t"], "/c", calls), False)   # sandboxed but not granted /t
        self.assertIs(w.wake_owned(9, ["/t"], "/c", calls), False)   # not sandboxed

    def test_not_macos_or_no_calls_is_none(self):
        with patch.object(w.sys, "platform", "linux"):
            self.assertIsNone(w.sandboxed_procs(0.0, ["/t"], "/c"))
        with patch.object(w.sys, "platform", "darwin"), patch.object(w, "_mac_calls", lambda: None), \
                patch.object(w.os.path, "isdir", lambda d: True):
            self.assertIsNone(w.sandboxed_procs(0.0, ["/t"], "/c"))

    def test_etime(self):
        self.assertEqual(w._etime_seconds("01:02"), 62)
        self.assertEqual(w._etime_seconds("2-03:01:02"), 2 * 86400 + 3 * 3600 + 62)
        self.assertIsNone(w._etime_seconds("x"))


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"), "macOS")
class RealMac(unittest.TestCase):
    """The real sandbox_check (both ctypes forms) on /bin/sleep processes."""

    def test_the_tmpdir_writable_root_probe_matrix(self):
        # `root` stands in for ~/.cardinal/wake/tmp (the canary); DIR_A is this wake's TMPDIR under
        # it; DIR_B is a sibling (another wake's TMPDIR). All exist (a missing path answers denied).
        root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        a, b = os.path.join(root, "a"), os.path.join(root, "b")
        os.mkdir(a)
        os.mkdir(b)
        # A Codex-like sandbox: writable root = this TMPDIR (DIR_A) only. A survivor's worst case:
        # env cleared, new session, chdir elsewhere.
        codex_prof = f'(version 1)(allow default)(deny file-write*)(allow file-write* (subpath "{a}"))'
        boxed = subprocess.Popen(["/usr/bin/env", "-u", "TMPDIR", "/usr/bin/sandbox-exec", "-p", codex_prof,
                                  "/bin/sleep", "20"], cwd="/", start_new_session=True)
        # An Apple-like sandbox: writes allowed broadly (the whole root tree, so DIR_A AND the canary).
        apple_prof = f'(version 1)(allow default)(deny file-write*)(allow file-write* (subpath "{root}"))'
        apple = subprocess.Popen(["/usr/bin/sandbox-exec", "-p", apple_prof, "/bin/sleep", "20"], cwd="/")
        plain = subprocess.Popen(["/bin/sleep", "20"], cwd=a)
        for p in (boxed, apple, plain):
            self.addCleanup(p.wait)
            self.addCleanup(p.kill)
        time.sleep(1.0)
        calls = w._mac_calls()
        self.assertIsNotNone(calls)
        check_null, check_path = calls
        T = w._SANDBOX_FILTER_PATH | w._SANDBOX_CHECK_NO_REPORT
        self.assertEqual(check_null(boxed.pid, None, 0), 1)                              # (a)/(d) sandboxed
        self.assertEqual(check_null(plain.pid, None, 0), 0)                              # (b) not
        self.assertEqual(check_path(boxed.pid, w._SANDBOX_WRITE_OP, T, a.encode()), 0)   # (a) DIR_A allowed
        self.assertEqual(check_path(boxed.pid, w._SANDBOX_WRITE_OP, T, b.encode()), 1)   # (a) sibling denied
        self.assertEqual(check_path(boxed.pid, w._SANDBOX_WRITE_OP, T, root.encode()), 1)  # (a) canary denied
        self.assertEqual(check_path(plain.pid, w._SANDBOX_WRITE_OP, T, b.encode()), 0)   # (b) plain: allowed
        self.assertEqual(check_path(apple.pid, w._SANDBOX_WRITE_OP, T, a.encode()), 0)   # Apple: DIR_A allowed
        self.assertEqual(check_path(apple.pid, w._SANDBOX_WRITE_OP, T, root.encode()), 0)  # Apple: canary allowed too
        # sandboxed_procs is EXACTLY the Codex-like survivor: the Apple-like one is filtered by the
        # canary, the plain one by the NULL check, the sibling by the grant (review 7 F1).
        self.assertEqual(w.sandboxed_procs(time.time() - 60, [a], root), [boxed.pid])
        self.assertEqual(w.sandboxed_procs(time.time() - 60, [b], root), [])


if __name__ == "__main__":
    unittest.main()
