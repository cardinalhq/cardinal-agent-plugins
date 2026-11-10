#!/usr/bin/env python3
"""Run one unittest suite and hold it to tests/windows/known-failures.txt.

For the Windows-Python-under-Wine CI job (.github/workflows/windows-wine.yml):
a test listed in known-failures.txt must fail, anything else must pass. A
listed test that passes fails the run too, so the fix that makes it pass
removes its line.

Usage: python tests/windows/run_suite.py core|copilot|behavior [status file]

The exit code is also written to the status file, last: wine-suite.sh trusts
that file, not Wine's exit status. The suite name `crash` exits 0 without
writing it, for the CI step that checks a crashed run cannot pass.
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
HANG_AFTER_S = 1500


def known_failures(suite: str, version: str) -> set:
    """Lines are `<suite> <test id>  # <issue>`; `<suite>@3.9` limits a line
    to one Python minor version."""
    out = set()
    for line in KNOWN.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        target, test_id = line.split(None, 1)
        name, _, only = target.partition("@")
        if name == suite and only in ("", version):
            out.add(test_id.strip())
    return out


def test_id(test) -> str:
    # A failed subTest counts against its test; setUpClass / module errors
    # have no test case and keep their description.
    return getattr(test, "test_case", test).id()


def main() -> int:
    if sys.argv[1:2] == ["crash"]:
        os._exit(0)
    if len(sys.argv) not in (2, 3) or sys.argv[1] not in SUITES:
        sys.exit(__doc__)
    suite = sys.argv[1]
    version = "%d.%d" % sys.version_info[:2]
    cwd, start = SUITES[suite]
    os.chdir(ROOT / cwd)
    sys.path.insert(0, os.getcwd())
    faulthandler.dump_traceback_later(HANG_AFTER_S, exit=True)  # a hung test names itself

    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(start))
    failed = {}
    for test, trace in result.failures + result.errors:
        failed.setdefault(test_id(test), trace)
    known = known_failures(suite, version)
    new = sorted(set(failed) - known)
    stale = sorted(known - set(failed))

    # A module that does not import, or a class whose setUpClass fails, hides
    # every test in it: count those apart from tests whose bodies ran.
    blocked = sorted(name for name in failed if name.startswith(("unittest.loader._FailedTest", "setUp")))
    print(f"\n{suite} on {sys.platform} Python {version}: ran {result.testsRun}, "
          f"failed {len(failed)} ({len(known) - len(stale)} known, {len(new)} new), "
          f"{len(stale)} listed as failing but passed, {len(result.skipped)} skipped, "
          f"{len(blocked)} modules or classes did not run at all")
    for name in blocked:
        print(f"=== DID NOT RUN {name}")
    for name in new:
        print(f"\n=== NEW FAILURE {name}\n{failed[name]}")
    if new:
        print("=== known-failures.txt lines for the new failures")
        for name in new:
            print(f"{suite} {name}")
    for name in stale:
        print(f"=== PASSES NOW, remove from known-failures.txt: {suite} {name}")
    return 1 if new or stale or result.unexpectedSuccesses else 0


if __name__ == "__main__":
    status_file = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None  # before main() changes directory
    code = main()
    if status_file:
        status_file.write_text(str(code), encoding="utf-8")
    sys.exit(code)
