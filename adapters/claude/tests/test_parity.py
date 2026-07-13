"""Golden parity: the migrated claude adapter must emit byte-equal OTLP
(and hook stdout) to the goldens captured from the SHIPPED plugin.

Goldens were produced by capture_goldens.py running the pre-migration
hooks at cardinal-claude-plugin/plugins/cardinal/hooks against the exact
scenarios in fixtures.py. This test replays the identical scenarios
against adapters/claude/hooks (with cardinal_core vendored — run
`python3 build/vendor.py claude` first) and compares after normalizing
only the volatile fields (timestamps, ts, cardinal.core_version,
cardinal.plugin_version, scope version, cardinal.cwd).

Run: python3 -m unittest test_parity -v   (from this directory)
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fixtures import ADAPTER_HOOKS_DIR, GOLDENS_DIR, SCENARIOS


class TestGoldenParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (ADAPTER_HOOKS_DIR / "cardinal_core" / "otlp.py").exists():
            raise unittest.SkipTest(
                "cardinal_core not vendored — run: python3 build/vendor.py claude"
            )

    def _assert_parity(self, name: str) -> None:
        golden_path = GOLDENS_DIR / f"{name}.json"
        self.assertTrue(golden_path.exists(), f"missing golden: {golden_path}")
        golden = json.loads(golden_path.read_text())
        with tempfile.TemporaryDirectory(prefix=f"parity-{name}-") as tmp:
            result = SCENARIOS[name](ADAPTER_HOOKS_DIR, Path(tmp))
        self.assertEqual(
            golden, result,
            f"scenario '{name}' diverged from the shipped plugin's golden",
        )


def _make_test(name: str):
    def test(self: TestGoldenParity) -> None:
        self._assert_parity(name)
    test.__name__ = f"test_{name}"
    test.__doc__ = f"byte-parity with shipped plugin: {name}"
    return test


for _name in SCENARIOS:
    setattr(TestGoldenParity, f"test_{_name}", _make_test(_name))


if __name__ == "__main__":
    unittest.main()
