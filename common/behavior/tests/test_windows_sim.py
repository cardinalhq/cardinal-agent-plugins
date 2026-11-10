"""The behavior runtime in an interpreter that looks like Windows
(core/tests/winsim.py: no fcntl and the other POSIX-only names).

A test marked expectedFailure names the issue that fixes it; that fix
removes the mark.
"""
from pathlib import Path
import sys
import unittest

BEHAVIOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BEHAVIOR.parents[1] / 'core' / 'tests'))
import winsim


class ImportTests(unittest.TestCase):
    # Fixed by #246: fcntl is imported at module top.
    @unittest.expectedFailure
    def test_the_mcp_server_module_imports(self):
        res = winsim.run('import server, range_run, checks_ledger', cwd=str(BEHAVIOR))
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == '__main__':
    unittest.main()
