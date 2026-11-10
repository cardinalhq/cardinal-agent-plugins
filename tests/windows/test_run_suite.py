"""run_suite.cleanup_only: which errors the Wine job reports without counting.

Run: python3 -m unittest discover -s tests/windows -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_suite  # noqa: E402


def sharing_violation(*args, **kwargs):
    error = PermissionError(13, "Sharing violation")
    error.winerror = 32
    raise error


def caught(function) -> BaseException:
    try:
        function()
    except BaseException as exc:  # noqa: BLE001
        return exc
    raise AssertionError("nothing raised")


class CleanupOnlyTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(tempfile.TemporaryDirectory, "_rmtree", sharing_violation)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_temp_directory_that_cannot_be_removed_is_not_counted(self):
        self.assertTrue(run_suite.cleanup_only(caught(tempfile.TemporaryDirectory().cleanup)))

        def at_exit():
            with tempfile.TemporaryDirectory():
                pass
        self.assertTrue(run_suite.cleanup_only(caught(at_exit)))

    def test_a_body_failure_before_the_cleanup_error_is_counted(self):
        def body():
            with tempfile.TemporaryDirectory():
                raise AssertionError("the test body failed")
        error = caught(body)
        self.assertEqual(getattr(error, "winerror", None), 32)  # the cleanup error, chained to the body's
        self.assertFalse(run_suite.cleanup_only(error))

    def test_a_sharing_violation_outside_temp_directory_cleanup_is_counted(self):
        self.assertFalse(run_suite.cleanup_only(caught(sharing_violation)))
        self.assertFalse(run_suite.cleanup_only(AssertionError("1 != 2")))

    def test_the_result_keeps_real_errors_and_sets_cleanup_errors_aside(self):
        class Sample(unittest.TestCase):
            def test_cleanup_only(self):
                self.addCleanup(tempfile.TemporaryDirectory().cleanup)

            def test_body_then_cleanup(self):
                with tempfile.TemporaryDirectory():
                    self.fail("the test body failed")

        result = run_suite.QuietResult(None, False, 0)
        unittest.defaultTestLoader.loadTestsFromTestCase(Sample).run(result)
        self.assertEqual([t._testMethodName for t in result.unremoved], ["test_cleanup_only"])
        self.assertEqual([t._testMethodName for t, _ in result.errors + result.failures], ["test_body_then_cleanup"])


if __name__ == "__main__":
    unittest.main()
