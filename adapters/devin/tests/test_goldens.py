from __future__ import annotations

import json
import logging
import unittest

import capture_goldens
import support


class GoldenTests(unittest.TestCase):
    def test_goldens_match_emitter(self) -> None:
        logger = logging.getLogger("cardinal_devin")
        level = logger.level
        logger.setLevel(logging.ERROR)
        self.addCleanup(logger.setLevel, level)
        for name in capture_goldens.SCENARIOS:
            with self.subTest(golden=name):
                path = support.GOLDENS / f"{name}.json"
                self.assertTrue(path.exists(), f"missing {path}; run tests/capture_goldens.py")
                expected = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    capture_goldens.capture(name), expected,
                    f"{name} drifted; if intended, re-run adapters/devin/tests/capture_goldens.py",
                )


if __name__ == "__main__":
    unittest.main()
