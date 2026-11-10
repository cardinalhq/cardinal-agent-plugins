"""run_suite.patient_rmtree: the Wine job's bounded retry of temp-directory removal.

Run: python3 -m unittest discover -s tests/windows -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_suite  # noqa: E402


def sharing_violation():
    error = PermissionError(13, "Sharing violation")
    error.winerror = 32
    return error


class PatientRmtreeTests(unittest.TestCase):
    def run_with(self, errors, wait_s=5.0):
        """Calls made and the outcome, with a fake clock that advances one second per sleep."""
        calls, now = [], [0.0]

        def rmtree(name):
            calls.append(name)
            if len(calls) <= len(errors):
                raise errors[len(calls) - 1]
            return "removed"

        retrying = run_suite.patient_rmtree(rmtree, wait_s, sleep=lambda s: now.__setitem__(0, now[0] + 1.0),
                                            clock=lambda: now[0])
        try:
            return calls, retrying("d")
        except BaseException as exc:  # noqa: BLE001
            return calls, exc

    def test_a_sharing_violation_is_retried_until_the_directory_is_free(self):
        calls, outcome = self.run_with([sharing_violation(), sharing_violation()])
        self.assertEqual((len(calls), outcome), (3, "removed"))

    def test_it_gives_up_after_the_wait_and_the_test_fails(self):
        calls, outcome = self.run_with([sharing_violation()] * 50, wait_s=3.0)
        self.assertEqual((len(calls), getattr(outcome, "winerror", None)), (4, 32))

    def test_any_other_error_is_raised_at_once(self):
        for error in (PermissionError(13, "access denied"), FileNotFoundError(2, "gone"), AssertionError("x")):
            calls, outcome = self.run_with([error])
            self.assertEqual((len(calls), outcome), (1, error))


if __name__ == "__main__":
    unittest.main()
