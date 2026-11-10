"""cardinal_core's Windows branches, on any OS: in an interpreter that looks
like Windows (winsim.py: no fcntl, no os.fchmod / getuid / O_NOFOLLOW /
killpg, no signal.SIGALRM), or called with `windows=True`.

A test marked expectedFailure names the issue that fixes it; that fix
removes the mark.
"""

from __future__ import annotations

import errno
import json
import os
import pkgutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from cardinal_core import paths
from tests import winsim

CORE = Path(__file__).resolve().parents[1]


class WindowsSimCase(unittest.TestCase):
    def sim(self, code: str) -> str:
        res = winsim.run(code, cwd=str(CORE))
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout


class ImportTests(WindowsSimCase):
    def test_every_core_module_imports(self):
        names = sorted(m.name for m in pkgutil.iter_modules([str(CORE / "cardinal_core")]))
        self.assertIn("investigation_events", names)
        self.sim("import importlib\n"
                 f"for name in {names!r}:\n"
                 "    importlib.import_module('cardinal_core.' + name)\n")


class FileHandlingTests(WindowsSimCase):
    def test_write_json_writes_the_session_file(self):  # os.fchmod is missing before Python 3.13
        out = self.sim("import json, pathlib, tempfile\n"
                       "from cardinal_core import investigation_events as events\n"
                       "with tempfile.TemporaryDirectory() as d:\n"
                       "    path = pathlib.Path(d) / 'binding.json'\n"
                       "    events.write_json(path, {'cursor': 0})\n"
                       "    print(json.loads(path.read_text(encoding='utf-8')))\n")
        self.assertEqual(out.strip(), "{'cursor': 0}")

    def test_a_session_binds_and_a_second_holder_is_locked_out(self):
        out = self.sim("import pathlib, tempfile\n"
                       "from cardinal_core import investigation_events as events\n"
                       "sid, inv = '5b7c1d2e-3f40-4a5b-8c6d-7e8f9a0b1c2d', 'inv_' + 'a' * 24\n"
                       "with tempfile.TemporaryDirectory() as d:\n"
                       "    home = pathlib.Path(d)\n"
                       "    print(events.bind(home, sid, inv, 'test')[1], events.read_binding(home, sid)['cursor'])\n"
                       "    with events.locked(home, sid) as first:\n"
                       "        with events.locked(home, sid, wait=0.1) as second:\n"
                       "            print(first, second)\n"
                       "    with events.locked(home, sid) as again:\n"
                       "        print(again)\n")
        self.assertEqual(out.split(), ["True", "0", "True", "False", "True"])

    def test_grant_files_are_read_written_and_locked(self):
        # No os.getuid / O_NOFOLLOW / fcntl: the owner and mode checks do not apply.
        out = self.sim("import pathlib, tempfile\n"
                       "from cardinal_core import investigation_consumer as consumer\n"
                       "with tempfile.TemporaryDirectory() as d:\n"
                       "    directory = consumer.private_directory(pathlib.Path(d) / 'supervise')\n"
                       "    path = directory / 'cursor.json'\n"
                       "    consumer.private_write(path, {'after': 3}, create=True)\n"
                       "    consumer.private_write(path, {'after': 4})\n"
                       "    with consumer.locked(path):\n"
                       "        print(consumer.private_read(path))\n"
                       "    consumer.fsync_directory(directory)\n")
        self.assertEqual(out.strip(), "{'after': 4}")

    def test_a_second_holder_of_a_consumer_lock_gets_blocking_io_error(self):
        out = self.sim("import pathlib, tempfile\n"
                       "from cardinal_core import investigation_consumer as consumer\n"
                       "with tempfile.TemporaryDirectory() as d:\n"
                       "    path = pathlib.Path(d) / 'cursor.json'\n"
                       "    with consumer.locked(path):\n"
                       "        try:\n"
                       "            with consumer.locked(path):\n"
                       "                print('second holder got in')\n"
                       "        except BlockingIOError:\n"
                       "            print('held')\n"
                       "    with consumer.locked(path):\n"
                       "        print('free')\n")
        self.assertEqual(out.split(), ["held", "free"])


class Utf8Tests(unittest.TestCase):
    """State files are UTF-8 whatever the default encoding is (cp1252 on
    Windows before Python 3.15), and a non-UTF-8 stdin is read as UTF-8."""
    TEXT = "say \u201chi\u201d \U0001f642"

    def test_json_state_round_trips_non_ascii_text(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_bytes(json.dumps({"text": self.TEXT}, ensure_ascii=False).encode("utf-8"))
            self.assertEqual(paths.read_json(path), {"text": self.TEXT})
            paths.atomic_write(path, json.dumps({"text": self.TEXT}, ensure_ascii=False))
            self.assertEqual(json.loads(path.read_bytes().decode("utf-8")), {"text": self.TEXT})
            self.assertEqual(json.loads(paths.backup(path).read_bytes().decode("utf-8")), {"text": self.TEXT})

    def test_a_cp1252_stdin_is_read_as_utf8(self):
        res = subprocess.run([sys.executable, "-c", "import sys; from cardinal_core.paths import utf8_stdio; "
                              "utf8_stdio(); sys.stdout.buffer.write(sys.stdin.read().encode('utf-8'))"],
                             input=self.TEXT.encode("utf-8"), capture_output=True, cwd=str(CORE),
                             env={**os.environ, "PYTHONIOENCODING": "cp1252"}, timeout=60)
        self.assertEqual((res.returncode, res.stdout.decode("utf-8")), (0, self.TEXT), res.stderr)


class WindowsFlockTests(unittest.TestCase):
    """_winfcntl.flock against a stand-in msvcrt."""

    def flock(self, operation: int, error: Optional[OSError] = None) -> list:
        from cardinal_core import _winfcntl
        calls = []

        def locking(fd, mode, nbytes):
            calls.append((mode, nbytes))
            if error:
                raise error

        fake = mock.Mock(LK_UNLCK=0, LK_NBLCK=2, locking=locking)
        with tempfile.TemporaryFile() as f, mock.patch.dict(sys.modules, {"msvcrt": fake}):
            _winfcntl.flock(f.fileno(), operation)
        return calls

    def test_lock_and_unlock_are_one_byte_region_calls(self):
        from cardinal_core import _winfcntl as w
        self.assertEqual(self.flock(w.LOCK_EX | w.LOCK_NB), [(2, 1)])
        self.assertEqual(self.flock(w.LOCK_UN), [(0, 1)])

    def test_a_held_lock_is_blocking_io_error_and_other_errors_pass_through(self):
        from cardinal_core import _winfcntl as w
        for held in (PermissionError(errno.EACCES, "held"), OSError(0, "Error")):  # the second is Wine's report
            with self.subTest(error=held), self.assertRaises(BlockingIOError):  # what flock raises, callers catch
                self.flock(w.LOCK_EX | w.LOCK_NB, held)
        with self.assertRaises(OSError) as raised:
            self.flock(w.LOCK_EX | w.LOCK_NB, OSError(errno.EINVAL, "bad handle"))
        self.assertEqual((type(raised.exception), raised.exception.errno), (OSError, errno.EINVAL))
        with self.assertRaises(PermissionError):  # unlock errors are not contention
            self.flock(w.LOCK_UN, PermissionError(errno.EACCES, "not locked"))

    def test_a_blocking_lock_is_refused_not_waited_for(self):
        from cardinal_core import _winfcntl as w
        with self.assertRaises(NotImplementedError):
            self.flock(w.LOCK_EX)


class AtomicReplaceTests(unittest.TestCase):
    def test_windows_retries_while_the_destination_is_open_elsewhere(self):
        calls = []

        def busy_twice(src, dst):
            calls.append((src, dst))
            if len(calls) < 3:
                raise PermissionError(13, "sharing violation")

        with mock.patch.object(paths.os, "replace", busy_twice), mock.patch.object(paths.time, "sleep"):
            paths.replace("a.tmp", "a", windows=True)
        self.assertEqual(calls, [("a.tmp", "a")] * 3)

    def test_elsewhere_it_is_os_replace_once(self):
        with mock.patch.object(paths.os, "replace", side_effect=PermissionError(13, "denied")) as replace:
            with self.assertRaises(PermissionError):
                paths.replace("a.tmp", "a", windows=False)
        self.assertEqual(replace.call_count, 1)


class ShellQuoteTests(unittest.TestCase):
    def test_windows_arguments_are_powershell_literals(self):
        self.assertEqual(paths.shell_quote(r"C:\Users\O'Brien folder\permit.json", windows=True),
                         r"'C:\Users\O''Brien folder\permit.json'")
        # Always quoted: `a,b` and a leading `@` are operators in PowerShell.
        self.assertEqual(paths.shell_quote("inv_abc123", windows=True), "'inv_abc123'")
        self.assertEqual(paths.shell_quote("", windows=True), "''")
        # PowerShell ends a literal at typographic quotes too.
        self.assertEqual(paths.shell_quote("a\u2019b\u2018c", windows=True), "'a\u2019\u2019b\u2018\u2018c'")

    def test_elsewhere_it_is_shlex_quote(self):
        self.assertEqual(paths.shell_quote("/tmp/my grant.json", windows=False), "'/tmp/my grant.json'")


class ScriptCommandTests(unittest.TestCase):
    def test_the_windows_form_is_powershell_naming_the_running_interpreter(self):
        script = r"C:\Users\it's me\plugin\scripts\cardinal-storyboard"
        self.assertEqual(paths.script_command(script, "python3 x", windows=True),
                         f"& '{sys.executable}' " + r"'C:\Users\it''s me\plugin\scripts\cardinal-storyboard'")

    def test_elsewhere_the_command_is_the_callers(self):
        self.assertEqual(paths.script_command("/p/cardinal-storyboard", 'python3 "/p/cardinal-storyboard"',
                                              windows=False), 'python3 "/p/cardinal-storyboard"')


if __name__ == "__main__":
    unittest.main()
