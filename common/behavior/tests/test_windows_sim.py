"""The behavior runtime in an interpreter that looks like Windows
(core/tests/winsim.py: no fcntl and the other POSIX-only names).

A test marked expectedFailure names the issue that fixes it; that fix
removes the mark.
"""
import errno
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

BEHAVIOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BEHAVIOR.parents[1] / 'core' / 'tests'))
sys.path.insert(0, str(BEHAVIOR))
import winsim


class ImportTests(unittest.TestCase):
    def test_the_mcp_server_module_imports(self):
        res = winsim.run('import server, range_run, checks_ledger', cwd=str(BEHAVIOR))
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_the_program_lock_is_taken_and_released(self):
        res = winsim.run('import pathlib, tempfile\n'
                         'from compilation.files import program_lock\n'
                         'with tempfile.TemporaryDirectory() as d:\n'
                         '    path = pathlib.Path(d) / "p.json"\n'
                         '    with program_lock(path):\n'
                         '        print("held")\n'
                         '    with program_lock(path):\n'
                         '        print("again")\n', cwd=str(BEHAVIOR))
        self.assertEqual((res.returncode, res.stdout.split()), (0, ['held', 'again']), res.stderr)


class WindowsFlockTests(unittest.TestCase):
    """compilation/winfcntl.flock against a stand-in msvcrt."""

    def flock(self, operation, errors=()):
        from compilation import winfcntl
        errors, calls, sleeps = list(errors), [], []

        def locking(fd, mode, nbytes):
            calls.append(mode)
            if errors:
                raise errors.pop(0)

        fake = mock.Mock(LK_UNLCK=0, LK_NBLCK=2, locking=locking)
        with tempfile.TemporaryFile() as f, mock.patch.dict(sys.modules, {'msvcrt': fake}):
            winfcntl.flock(f, operation, sleep=sleeps.append)
        return calls, sleeps

    def test_a_blocking_lock_waits_out_contention_then_succeeds(self):
        from compilation import winfcntl as w
        held = [OSError(errno.EACCES, 'held'), OSError(errno.EDEADLK, 'gave up')]
        self.assertEqual(self.flock(w.LOCK_EX, held), ([2, 2, 2], [0.05, 0.05]))

    def test_any_other_error_escapes_a_blocking_lock_at_once(self):
        from compilation import winfcntl as w
        with self.assertRaises(OSError) as raised:
            self.flock(w.LOCK_EX, [OSError(errno.EINVAL, 'bad handle')] * 5)
        self.assertEqual(raised.exception.errno, errno.EINVAL)

    def test_a_held_non_blocking_lock_is_blocking_io_error(self):
        from compilation import winfcntl as w
        with self.assertRaises(BlockingIOError):
            self.flock(w.LOCK_EX | w.LOCK_NB, [PermissionError(errno.EACCES, 'held')])
        with self.assertRaises(OSError) as raised:
            self.flock(w.LOCK_EX | w.LOCK_NB, [OSError(errno.EINVAL, 'bad handle')])
        self.assertEqual((type(raised.exception), raised.exception.errno), (OSError, errno.EINVAL))
        self.assertEqual(self.flock(w.LOCK_UN), ([0], []))


class WorkerRunningTests(unittest.TestCase):
    """The callers that ask whether a range worker holds its lock, without fcntl."""

    def test_held_and_free_are_told_apart(self):
        res = winsim.run('import pathlib, tempfile\n'
                         'from compilation import winfcntl as fcntl\n'
                         'import checks_ledger, range_runner\n'
                         'with tempfile.TemporaryDirectory() as d:\n'
                         '    directory = pathlib.Path(d)\n'
                         '    runner = range_runner.RangeRunner.__new__(range_runner.RangeRunner)\n'
                         '    runner.dir = directory\n'
                         '    print(checks_ledger.range_worker_running(directory), runner.worker_running())\n'
                         '    with (directory / "lock").open("a") as held:\n'
                         '        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
                         '        print(checks_ledger.range_worker_running(directory), runner.worker_running())\n'
                         '        fcntl.flock(held, fcntl.LOCK_UN)\n'
                         '    print(checks_ledger.range_worker_running(directory), runner.worker_running())\n',
                         cwd=str(BEHAVIOR))
        self.assertEqual((res.returncode, res.stdout.split()),
                         (0, ['False', 'False', 'True', 'True', 'False', 'False']), res.stderr)


if __name__ == '__main__':
    unittest.main()
