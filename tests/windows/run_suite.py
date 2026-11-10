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
import traceback
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
    """Lines are `<suite>@<python minor> <test id>  # <issue>: <cause>`."""
    out = set()
    for line in KNOWN.read_text(encoding="utf-8").splitlines():
        words = line.split("#", 1)[0].split(None, 1)
        if len(words) == 2 and words[0] == target:
            out.add(words[1].strip())
    return out


def test_id(test) -> str:
    # A failed subTest counts against its test; setUpClass / module errors
    # have no test case and keep their description.
    return getattr(test, "test_case", test).id()


def cleanup_only(exc: BaseException) -> bool:
    """A TemporaryDirectory that could not be removed, and nothing else:
    every exception in the chain is `PermissionError [WinError 32]` (sharing
    violation; tempfile retries, so they chain) and the traceback passes
    through TemporaryDirectory.cleanup / __exit__. Under Wine this comes and
    goes between identical runs (a child process's directory is released
    late), so it cannot be held to the list; it is reported and tracked in
    issue #255. A failure of the test body chained before it, or a sharing
    violation anywhere else, does not match and is counted."""
    first = exc
    while exc is not None:
        if not isinstance(exc, PermissionError) or getattr(exc, "winerror", None) != 32:
            return False
        exc = exc.__cause__ or exc.__context__
    return any(os.path.basename(frame.f_code.co_filename) == "tempfile.py"
               and frame.f_code.co_name in ("cleanup", "__exit__")
               for frame, _ in traceback.walk_tb(first.__traceback__))


class QuietResult(unittest.TextTestResult):
    unremoved = ()  # tests whose only error was cleanup_only

    def addError(self, test, err) -> None:
        if cleanup_only(err[1]):
            self.unremoved = (*self.unremoved, test)
        else:
            super().addError(test, err)

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
    unremoved = {test_id(test) for test in result.unremoved}
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
    out += [f"TEMP DIRECTORY NOT REMOVED (WinError 32, not counted, #255) {name}" for name in sorted(unremoved)]
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
