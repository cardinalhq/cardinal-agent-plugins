"""Survivor signal 2 and 3 (closing review 5's documented gap: `env -u
TMPDIR setsid ...`). A process a woken run leaves behind is still in Codex's
Seatbelt sandbox whatever its environment or process group, so the waker
also asks macOS: sandbox_check(pid, NULL, 0) for the user's processes born
after the wake, with their cwd / executable / open files in the workspace
or the wake's TMPDIR, or orphaned during the wake. The same scan is a
liveness signal before a claim.

Fakes for the waker paths (World.sandboxed_procs); unit tests of
sandboxed_procs with its OS calls mocked; one real macOS check that starts
only /bin/sleep (plain and under sandbox-exec), never an agent.

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

    def exit_and_settle(self):
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()

    def test_a_survivor_that_cleared_tmpdir_and_left_the_group_is_stopped(self):
        # No marker in its environment, another process group: only the sandbox signal sees it.
        self.world.envrows = [(4242, 4242, "/bin/sleep 1000 PATH=/usr/bin HOME=/x")]
        self.world.boxed = [(4242, 1)]
        self.exit_and_settle()
        self.assertEqual(self.world.killed, [(4242, signal.SIGTERM)])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        since, roots, until = self.world.boxed_calls[-1]
        self.assertEqual(roots, [os.path.realpath(str(self.repo)), self.tmp])
        self.assertEqual(since, self.world.starts[self.child["pid"]])
        self.assertIsNotNone(until)                                  # the orphan window ends at the first sweep

    def test_its_children_are_stopped_too(self):
        self.world.boxed = [(4242, 1), (4243, 4242)]
        self.exit_and_settle()
        self.assertEqual(sorted(p for p, _ in self.world.killed), [4242, 4243])

    def test_a_sandboxed_process_under_a_live_parent_is_waited_for_never_signalled(self):
        # The owner's own Codex, started in the same repository during the wake, runs a sandboxed command.
        self.world.boxed = [(5555, 5000)]
        self.exit_and_settle()
        self.assertEqual(self.world.killed, [])
        self.assertEqual(self.state(5), w.RESUMING)                  # in flight: no concurrent wake
        self.assertTrue(os.path.isdir(self.tmp))
        self.world.boxed = []
        self.world.t += 60
        self.run_pass()
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_an_unanswerable_sandbox_check_keeps_the_wake_in_flight(self):
        self.world.boxed = None
        self.exit_and_settle()
        self.assertEqual(self.state(5), w.RESUMING)
        self.assertIn("could not be listed or checked", self.ledger()["5"]["lingering"])


class PreClaim(Base):
    def test_a_sandboxed_process_in_the_workspace_makes_the_session_live(self):
        self.world.boxed = [(7777, 1)]
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("a sandboxed process is running in its working directory", self.notified_only())
        since, roots, until = self.world.boxed_calls[0]
        self.assertEqual((since, roots, until), (0.0, [os.path.realpath(str(self.repo))], None))

    def test_since_is_the_sessions_last_wake(self):
        self.world.events = [decision(5)]
        self.run_pass()
        launched = self.ledger()["5"]["child"]["launched_at"]
        self.append_turns(SID, turn())
        self.world.events.append(ack(6, 5))
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.world.events.append(decision(7))
        self.world.t += w.LIVE_WINDOW + 1
        self.world.boxed_calls.clear()
        self.run_pass()
        self.assertEqual(self.world.boxed_calls[0][0], launched)
        self.assertEqual(len(self.world.launches), 2)

    def test_an_unanswerable_check_is_live(self):
        self.world.boxed = None
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("sandboxed processes could not be checked", self.notified_only())


class ScanUnit(unittest.TestCase):
    """sandboxed_procs with its OS calls mocked."""

    def run_scan(self, rows, boxed, paths, *, since=100.0, roots=("/ws",), until=None, now=1000.0):
        import ctypes
        state = {}

        def check(pid, op, kind):
            ans, err = boxed.get(pid, (0, 0))
            ctypes.set_errno(err)
            return ans

        def proc_paths(pid, calls):
            v = paths[pid]
            if isinstance(v, BaseException):
                raise v
            return v
        ps = "\n".join(f"{pid} {ppid} {uid} {etime} {comm}" for pid, ppid, uid, etime, comm in rows) + "\n"
        with patch.object(w.sys, "platform", "darwin"), \
                patch.object(w, "_mac_calls", lambda: (check, None, None, None)), \
                patch.object(w, "_proc_paths", proc_paths), patch.object(w.time, "time", lambda: now), \
                patch.object(w.subprocess, "run",
                             lambda *a, **k: subprocess.CompletedProcess(a, 0, ps, "")), \
                patch.object(w.os, "getuid", lambda: 501), patch.object(w.os, "getpid", lambda: 1):
            state["out"] = w.sandboxed_procs(since, list(roots), until)
        return state["out"]

    def test_the_rules(self):
        uid = 501
        rows = [(10, 1, uid, "01:00", "/bin/sleep"),            # sandboxed, cwd in ws, started at 940
                (11, 1, uid, "01:00", "/bin/sleep"),            # not sandboxed (the owner's editor)
                (12, 1, 0, "01:00", "/bin/sleep"),              # another user
                (13, 1, uid, "20:00", "/bin/sleep"),            # started before since (1000 - 1200 < 100)
                (14, 9, uid, "01:00", "/bin/zsh"),              # sandboxed, elsewhere, has a parent
                (15, 1, uid, "01:00", "/bin/sleep"),            # sandboxed, gone
                (16, 1, uid, "01:00", "/usr/libexec/dmd"),      # sandboxed Apple service, EPERM
                (17, 1, uid, "01:00", "/Applications/A.app/Contents/MacOS/A")]   # sandboxed app, elsewhere
        boxed = {10: (1, 0), 13: (1, 0), 14: (1, 0), 15: (1, 0), 16: (1, 0), 17: (1, 0)}
        paths = {10: ["/ws/sub", "/bin/sleep"], 14: ["/", "/bin/zsh"], 15: w._Gone(),
                 16: OSError(errno.EPERM, "x"), 17: ["/", "/Applications/A.app/Contents/MacOS/A"]}
        self.assertEqual(self.run_scan(rows, boxed, paths), [(10, 1)])

    def test_an_orphan_born_during_the_wake_is_found_wherever_it_is(self):
        rows = [(20, 1, 501, "01:00", "/bin/sleep"), (21, 1, 501, "01:00", "/Applications/A.app/Contents/MacOS/A"),
                (22, 1, 501, "01:00", "/bin/sleep")]
        boxed = {20: (1, 0), 21: (1, 0), 22: (1, 0)}
        paths = {20: ["/", "/bin/sleep"], 21: ["/", "/Applications/A.app/Contents/MacOS/A"],
                 22: ["/", "/bin/sleep"]}
        self.assertEqual(self.run_scan(rows, boxed, paths, until=990.0), [(20, 1), (22, 1)])
        self.assertEqual(self.run_scan(rows, boxed, paths, until=900.0), [])     # born after the window
        self.assertEqual(self.run_scan(rows, boxed, paths), [])                  # no window: only the workspace

    def test_what_cannot_be_read_fails_closed(self):
        rows = [(30, 1, 501, "01:00", "/opt/homebrew/bin/node")]
        self.assertIsNone(self.run_scan(rows, {30: (1, 0)}, {30: OSError(errno.EPERM, "x")}))
        self.assertIsNone(self.run_scan(rows, {30: (1, errno.EINVAL)}, {}))
        self.assertIsNone(self.run_scan([(31, 1, 501, "garbage", "/bin/sleep")], {}, {}))
        self.assertEqual(self.run_scan(rows, {30: (1, errno.ESRCH)}, {}), [])   # gone meanwhile

    def test_no_macos_or_no_calls_is_none(self):
        with patch.object(w.sys, "platform", "linux"):
            self.assertIsNone(w.sandboxed_procs(0.0, ["/ws"]))
        with patch.object(w.sys, "platform", "darwin"), patch.object(w, "_mac_calls", lambda: None):
            self.assertIsNone(w.sandboxed_procs(0.0, ["/ws"]))

    def test_etime(self):
        self.assertEqual(w._etime_seconds("01:02"), 62)
        self.assertEqual(w._etime_seconds("03:01:02"), 3 * 3600 + 62)
        self.assertEqual(w._etime_seconds("2-03:01:02"), 2 * 86400 + 3 * 3600 + 62)
        self.assertIsNone(w._etime_seconds("x"))


@unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"), "macOS")
class RealMac(unittest.TestCase):
    """The real calls: /bin/sleep under sandbox-exec (allow-default profile) and plain."""

    def test_sandbox_check_and_the_workspace_signal(self):
        ws = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(ws, ignore_errors=True))
        since = time.time() - 1
        sb = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)"]
        boxed = subprocess.Popen(["/usr/bin/env", "-u", "TMPDIR"] + sb + ["/bin/sleep", "20"], cwd=ws,
                                 start_new_session=True)
        plain = subprocess.Popen(["/bin/sleep", "20"], cwd=ws)
        for p in (boxed, plain):
            self.addCleanup(p.wait)
            self.addCleanup(p.kill)
        time.sleep(1.0)
        found = w.sandboxed_procs(since, [ws])
        self.assertIsNotNone(found)
        pids = {p for p, _ in found}
        self.assertIn(boxed.pid, pids)
        self.assertNotIn(plain.pid, pids)
        calls = w._mac_calls()
        self.assertEqual(calls[0](boxed.pid, None, 0), 1)
        self.assertEqual(calls[0](plain.pid, None, 0), 0)


if __name__ == "__main__":
    unittest.main()
