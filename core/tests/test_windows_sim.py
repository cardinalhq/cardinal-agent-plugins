"""cardinal_core's Windows branches, on any OS: in an interpreter that looks
like Windows (winsim.py: no fcntl, no os.fchmod / getuid / O_NOFOLLOW /
killpg, no signal.SIGALRM), or called with `windows=True`.

A test marked expectedFailure names the issue that fixes it; that fix
removes the mark.
"""

from __future__ import annotations

import pkgutil
import sys
import unittest
from pathlib import Path

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
    # Fixed by #247: os.fchmod does not exist on Windows before Python 3.13.
    @unittest.expectedFailure
    def test_write_json_writes_the_session_file(self):
        out = self.sim("import json, pathlib, tempfile\n"
                       "from cardinal_core import investigation_events as events\n"
                       "with tempfile.TemporaryDirectory() as d:\n"
                       "    path = pathlib.Path(d) / 'binding.json'\n"
                       "    events.write_json(path, {'cursor': 0})\n"
                       "    print(json.loads(path.read_text(encoding='utf-8')))\n")
        self.assertEqual(out.strip(), "{'cursor': 0}")


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
