#!/usr/bin/env python3
"""Run one unittest suite and hold it to tests/windows/known-failures.txt.

For the Windows-Python-under-Wine CI job (.github/workflows/windows-wine.yml):
a test listed in known-failures.txt for this Python version must fail,
anything else must pass. A listed test that passes fails the run too, so the
fix that makes it pass removes its line.

Usage: python tests/windows/run_suite.py core|copilot|behavior [report file]

With a report file, the report goes there instead of stdout and ends in
`exit <code>`: wine-suite.sh reads the result from that file, because Wine
can lose a process's last output and exit 0 after the interpreter aborts.
The suite name `crash` exits 0 without a report, for the CI step that checks
a crashed run cannot pass.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KNOWN = Path(__file__).with_name("known-failures.txt")
# name: (working directory, discovery start directory), as the Linux jobs run them
SUITES = {
    "core": ("core", "tests"),
    "copilot": ("adapters/copilot", "tests"),
    "behavior": (".", "common/behavior/tests"),
}
HANG_AFTER_S = 2400


def known_failures(target: str) -> set:
    """Lines are `<suite>@<python minor> <test id>  # <issue>`."""
    out = set()
    for line in KNOWN.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and line.split(None, 1)[0] == target:
            out.add(line.split(None, 1)[1].strip())
    return out


def test_id(test) -> str:
    # A failed subTest counts against its test; setUpClass / module errors
    # have no test case and keep their description.
    return getattr(test, "test_case", test).id()


class QuietResult(unittest.TextTestResult):
    def printErrors(self) -> None:  # the report prints only the failures that are new
        pass


def run(suite: str) -> tuple:
    """(exit code, report lines)."""
    target = "%s@%d.%d" % ((suite,) + sys.version_info[:2])
    cwd, start = SUITES[suite]
    os.chdir(ROOT / cwd)
    sys.path.insert(0, os.getcwd())
    faulthandler.dump_traceback_later(HANG_AFTER_S, exit=True)  # a hung test names itself

    result = unittest.TextTestRunner(verbosity=1, resultclass=QuietResult).run(
        unittest.defaultTestLoader.discover(start))
    failed = {}
    for test, trace in result.failures + result.errors:
        failed.setdefault(test_id(test), trace)
    known = known_failures(target)
    new = sorted(set(failed) - known)
    stale = sorted(known - set(failed))
    # A module that does not import, or a class whose setUpClass fails, hides
    # every test in it: count those apart from tests whose bodies ran.
    blocked = sorted(name for name in failed if name.startswith(("unittest.loader._FailedTest", "setUp")))

    out = [f"{target} on {sys.platform}: ran {result.testsRun}, failed {len(failed)} "
           f"({len(known) - len(stale)} known, {len(new)} new), {len(stale)} listed as failing but passed, "
           f"{len(result.skipped)} skipped, {len(result.unexpectedSuccesses)} unexpected successes, "
           f"{len(blocked)} modules or classes did not run at all"]
    out += [f"DID NOT RUN {name}" for name in blocked]
    out += [f"\nNEW FAILURE {name}\n{failed[name]}" for name in new]
    if new:
        out += ["known-failures.txt lines for the new failures:"] + [f"{target} {name}" for name in new]
    out += [f"PASSES NOW, remove from known-failures.txt: {target} {name}" for name in stale]
    return (1 if new or stale or result.unexpectedSuccesses else 0), out


def main() -> int:
    if sys.argv[1:2] == ["crash"]:
        os._exit(0)
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in SUITES:
        sys.exit(__doc__)
    report = Path(sys.argv[2]).resolve() if len(sys.argv) == 3 else None  # before run() changes directory
    code, lines = run(sys.argv[1])
    if report is None:
        print("\n".join(lines))
    else:
        with open(report, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + f"\nexit {code}\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
