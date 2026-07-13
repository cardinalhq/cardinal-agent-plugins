#!/usr/bin/env python3
"""Capture golden OTLP fixtures for the claude adapter.

Runs every fixture scenario against a hooks directory — by contract the
SHIPPED plugin's hooks (pre-migration source of truth) — and writes the
normalized results to tests/goldens/<scenario>.json. test_parity.py then
replays the identical scenarios against the migrated adapter hooks and
asserts equality.

Usage:
    python3 capture_goldens.py --hooks-dir /path/to/shipped/plugins/cardinal/hooks
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import GOLDENS_DIR, SCENARIOS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hooks-dir", required=True,
                        help="hooks/ directory of the SHIPPED plugin")
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    hooks_dir = Path(args.hooks_dir).resolve()
    if not (hooks_dir / "git-state.py").exists():
        sys.exit(f"not a cardinal hooks dir: {hooks_dir}")

    GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    names = args.only or list(SCENARIOS)
    for name in names:
        fn = SCENARIOS[name]
        with tempfile.TemporaryDirectory(prefix=f"golden-{name}-") as tmp:
            result = fn(hooks_dir, Path(tmp))
        out = GOLDENS_DIR / f"{name}.json"
        out.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n")
        n_batches = len(result["batches"])
        n_records = sum(
            len(sl["logRecords"])
            for b in result["batches"]
            for rl in b.get("resourceLogs", [])
            for sl in rl.get("scopeLogs", [])
        )
        print(f"  {name}: {n_batches} batch(es), {n_records} record(s), "
              f"stdout={'yes' if result['stdout'] else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
