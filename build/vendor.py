#!/usr/bin/env python3
"""Vendor cardinal_core into adapter plugin artifacts.

Copies core/cardinal_core/ verbatim into each adapter's artifact directory
next to hooks/, so hook scripts import it via the sys.path.insert
mechanism they already use. No code transformation — vendored files are
byte-identical to core source at the pinned version (spec §Packaging).

Usage:
    python3 build/vendor.py <adapter> [<adapter> ...]
    python3 build/vendor.py --all
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_PKG = ROOT / "core" / "cardinal_core"
ADAPTERS_DIR = ROOT / "adapters"


def known_adapters() -> list[str]:
    if not ADAPTERS_DIR.exists():
        return []
    return sorted(
        p.name for p in ADAPTERS_DIR.iterdir()
        if p.is_dir() and (p / "hooks").exists()
    )


def vendor_into(adapter: str) -> Path:
    dest = ADAPTERS_DIR / adapter / "hooks" / "cardinal_core"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CORE_PKG, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("adapters", nargs="*")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    targets = known_adapters() if args.all else args.adapters
    if not targets:
        print("No adapters specified and none discovered. Adapters land in P1–P4.")
        return 0
    unknown = [a for a in targets if not (ADAPTERS_DIR / a).is_dir()]
    if unknown:
        sys.exit(f"Unknown adapter(s): {', '.join(unknown)}")
    for adapter in targets:
        dest = vendor_into(adapter)
        print(f"Vendored cardinal_core -> {dest.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
