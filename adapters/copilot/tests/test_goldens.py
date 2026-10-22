"""The Copilot hook still produces its committed goldens (tests/goldens/).

The goldens were captured from this adapter (tests/capture_goldens.py: there
is no pre-migration Copilot plugin to compare against). A change to what the
hook emits fails here until the goldens are re-captured and the diff
reviewed; tests/test_contract.py at the repo root reads the same files.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_goldens  # noqa: E402


class GoldenTests(unittest.TestCase):
    def test_hook_output_matches_committed_goldens(self):
        captured = capture_goldens.capture()
        committed = {p.stem: json.loads(p.read_text()) for p in sorted(capture_goldens.GOLDENS_DIR.glob("*.json"))}
        self.assertEqual(sorted(captured), sorted(committed),
                         "golden set changed: run tests/capture_goldens.py and review the diff")
        for name in sorted(captured):
            with self.subTest(golden=name):
                self.assertEqual(captured[name], committed[name],
                                 f"{name} differs from tests/goldens/{name}.json: run tests/capture_goldens.py "
                                 "and review the diff")

    def test_every_golden_step_emitted_something(self):
        for path in sorted(capture_goldens.GOLDENS_DIR.glob("*.json")):
            with self.subTest(golden=path.stem):
                self.assertTrue(json.loads(path.read_text())["batches"], f"{path.name} has no OTLP batch")


if __name__ == "__main__":
    unittest.main()
