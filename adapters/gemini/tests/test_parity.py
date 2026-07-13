"""Golden-parity tests for the migrated Gemini adapter.

The goldens in goldens/ were captured by running the PRE-MIGRATION hook
script (cardinal-gemini-plugin @ shipped v0.1.0) over the scenarios in
scenarios.py. These tests run the MIGRATED adapter hook over the same
scenarios and assert the normalized OTLP batches and hook stdout are
byte-equal.

Run from adapters/gemini/:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios as sc  # noqa: E402

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
MONOREPO_ROOT = ADAPTER_ROOT.parents[1]
HOOK = ADAPTER_ROOT / "hooks" / "cardinal-gemini-telemetry.py"
GOLDENS = Path(__file__).resolve().parent / "goldens"


def _ensure_vendored() -> None:
    """The vendored cardinal_core is a gitignored build output — create it
    on the fly so a fresh checkout can run the suite."""
    if (ADAPTER_ROOT / "hooks" / "cardinal_core" / "__init__.py").exists():
        return
    subprocess.run(
        [sys.executable, str(MONOREPO_ROOT / "build" / "vendor.py"), "gemini"],
        check=True, capture_output=True,
    )


class GoldenParityTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_vendored()

    def _run(self, scenario: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            return sc.run_scenario(HOOK, scenario, Path(tmp))

    def test_scenarios_match_goldens(self) -> None:
        names_seen = set()
        for scenario in sc.scenarios():
            with self.subTest(scenario=scenario["name"]):
                golden_path = GOLDENS / f"{scenario['name']}.json"
                self.assertTrue(golden_path.exists(),
                                f"missing golden for {scenario['name']} — "
                                "run capture_goldens.py against the shipped hook")
                golden = json.loads(golden_path.read_text())
                actual = self._run(scenario)
                self.assertEqual(golden["steps"], actual["steps"],
                                 "hook stdout/returncode diverged from golden")
                self.assertEqual(golden["batches"], actual["batches"],
                                 "normalized OTLP output diverged from golden")
                names_seen.add(scenario["name"])

        # Every committed golden must correspond to a live scenario.
        on_disk = {p.stem for p in GOLDENS.glob("*.json")}
        self.assertEqual(on_disk, names_seen,
                         "goldens on disk and scenarios out of sync")


if __name__ == "__main__":
    unittest.main()
