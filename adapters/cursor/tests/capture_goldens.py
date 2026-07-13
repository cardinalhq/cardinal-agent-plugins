#!/usr/bin/env python3
"""Capture golden OTLP fixtures from the PRE-MIGRATION shipped Cursor
plugin (cardinal-cursor-plugin v0.2.0).

Runs the existing hook script — never the migrated adapter — against the
shared synthetic fixtures and freezes normalized output into
tests/goldens/<step>.json. The parity test then asserts the migrated
adapter reproduces these bytes exactly.

Usage:
    python3 adapters/cursor/tests/capture_goldens.py \
        [--source /path/to/cardinal-cursor-plugin/plugins/cardinal-cursor-plugin]

The source tree is read-only; default location can be overridden with
--source or CARDINAL_CURSOR_SOURCE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

DEFAULT_SOURCE = os.environ.get(
    "CARDINAL_CURSOR_SOURCE",
    str(Path.home() / "workspace" / "cardinal-cursor-plugin"
        / "plugins" / "cardinal-cursor-plugin"),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    args = parser.parse_args()

    source = Path(args.source)
    script = source / "hooks" / "cardinal-cursor-telemetry.py"
    if not script.exists():
        sys.exit(f"shipped hook script not found: {script}")

    stub = fixtures.StubIngest().start()
    try:
        with tempfile.TemporaryDirectory(prefix="cardinal-cursor-golden-") as tmp:
            sandbox = fixtures.build_sandbox(Path(tmp), stub.endpoint)
            results = fixtures.run_all_steps(script, stub, sandbox)
    finally:
        stub.stop()

    fixtures.GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    for name, blob in results.items():
        path = fixtures.GOLDENS_DIR / f"{name}.json"
        path.write_text(json.dumps(blob, indent=2, sort_keys=False) + "\n")
        n_records = sum(
            len(sl.get("logRecords", []))
            for b in blob["batches"]
            for rl in b.get("resourceLogs", [])
            for sl in rl.get("scopeLogs", [])
        )
        print(f"wrote {path.name}: {len(blob['batches'])} batch(es), "
              f"{n_records} record(s), stdout={'yes' if blob['stdout'] else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
