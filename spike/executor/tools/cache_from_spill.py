"""Compact a spilled Cardinal MCP metric-query result into the tool-cache.

Usage:
    python tools/cache_from_spill.py <spill-file> <run-dir>/<node-id>.json

Extracts only summary + series_total + data_points_total + ddsketches from
the (potentially huge) spill file. Discards the full data_points array so
downstream nodes read a compact response.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    src = Path(argv[1])
    dst = Path(argv[2])
    payload = json.loads(src.read_text())
    compact = {
        "summary": payload.get("summary"),
        "series_total": int(payload.get("series_total") or payload.get("series_returned") or 0),
        "data_points_total": int(payload.get("data_points_total") or payload.get("data_points_returned") or 0),
        "ddsketches": payload.get("ddsketches") or {},
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(compact, indent=2))
    print(f"wrote {dst} (series={compact['series_total']}, points={compact['data_points_total']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
