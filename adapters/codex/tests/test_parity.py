"""Parity + behavioral tests for the migrated Codex adapter.

Golden parity: runs the migrated hook against the SAME fixtures used by
capture_goldens.py (which ran the pre-migration shipped hook) and asserts
normalized OTLP/stdout output is byte-equal to tests/goldens/.

Behavioral tests are ported from the source repo's test_cardinal_plugin.py.

Run from adapters/codex/:
    python3 -m unittest tests.test_parity -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent   # adapters/codex
REPO_ROOT = ROOT.parent.parent
HOOK = ROOT / "hooks" / "cardinal-codex-telemetry.py"
SCRIPTS = ROOT / "scripts"
CONNECT = SCRIPTS / "cardinal-connect"
STATUS = SCRIPTS / "cardinal-status"
DISCONNECT = SCRIPTS / "cardinal-disconnect"
GOLDENS = Path(__file__).resolve().parent / "goldens"


def setUpModule() -> None:  # noqa: N802
    """Vendor cardinal_core next to the hooks (build output, gitignored)."""
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "codex"],
        check=True, capture_output=True,
    )


def golden(name: str):
    return json.loads((GOLDENS / f"{name}.json").read_text())


class GoldenSmokeTests(unittest.TestCase):
    def test_goldens_exist(self) -> None:
        names = {p.stem for p in GOLDENS.glob("*.json")}
        self.assertEqual(len(names), 10, names)


class GoldenParityTests(unittest.TestCase):
    """The migrated hook must emit byte-equal normalized output to the
    goldens captured from the pre-migration shipped hook."""

    def assert_matches(self, results: dict) -> None:
        for name, data in sorted(results.items()):
            with self.subTest(golden=name):
                self.assertEqual(data, golden(name))

    def test_telemetry_scenarios(self) -> None:
        # stop_first, stop_second, user_prompt_submit, subagent_stop
        self.assert_matches(fixtures.scenario_telemetry(HOOK))

    def test_session_start_scenarios(self) -> None:
        self.assert_matches(fixtures.scenario_session_start(HOOK))

    def test_gate_scenarios(self) -> None:
        self.assert_matches(fixtures.scenario_gate(HOOK))


if __name__ == "__main__":
    unittest.main()
