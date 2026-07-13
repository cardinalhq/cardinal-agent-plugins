#!/usr/bin/env python3
"""Capture golden fixtures by running a hook script over the shared
scenarios. Goldens MUST be captured from the pre-migration shipped hook
(cardinal-gemini-plugin repo) — goldens produced from the migrated code
prove nothing.

Usage:
    python3 capture_goldens.py --hook /path/to/cardinal-gemini-telemetry.py \
        [--out goldens/]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scenarios as sc  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hook", required=True, type=Path,
                        help="Path to the PRE-MIGRATION telemetry hook script.")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent / "goldens")
    args = parser.parse_args()

    hook = args.hook.resolve()
    if not hook.exists():
        sys.exit(f"hook script not found: {hook}")
    args.out.mkdir(parents=True, exist_ok=True)

    for scenario in sc.scenarios():
        with tempfile.TemporaryDirectory() as tmp:
            result = sc.run_scenario(hook, scenario, Path(tmp))
        target = args.out / f"{scenario['name']}.json"
        target.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n")
        n_records = sum(
            len(sl.get("logRecords", []))
            for b in result["batches"]
            for rl in b.get("resourceLogs", [])
            for sl in rl.get("scopeLogs", [])
        )
        print(f"{scenario['name']}: {len(result['steps'])} steps, "
              f"{len(result['batches'])} batches, {n_records} records -> {target.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
