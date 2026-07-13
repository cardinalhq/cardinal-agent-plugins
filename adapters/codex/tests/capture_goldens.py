#!/usr/bin/env python3
"""Capture golden OTLP fixtures from the PRE-MIGRATION Codex plugin.

Goldens must come from the shipped code, never from the migrated adapter —
goldens produced by the code under test prove nothing. Run once against the
source checkout, commit the outputs, then never re-run unless the fixtures
themselves change (in which case the source hook is the authority again):

    python3 tests/capture_goldens.py \
        --hook /path/to/cardinal-codex-plugin/plugins/cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py

Writes one JSON file per scenario key into tests/goldens/.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

DEFAULT_SOURCE_HOOK = (
    "/Users/ruchirj/workspace/cardinal-codex-plugin/plugins/"
    "cardinal-codex-plugin/hooks/cardinal-codex-telemetry.py"
)
GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hook", default=DEFAULT_SOURCE_HOOK,
                        help="Path to the PRE-MIGRATION telemetry hook script.")
    args = parser.parse_args()

    hook = Path(args.hook)
    if not hook.exists():
        sys.exit(f"source hook not found: {hook}")

    results = fixtures.run_all(hook)
    GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(results.items()):
        path = GOLDENS_DIR / f"{name}.json"
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
