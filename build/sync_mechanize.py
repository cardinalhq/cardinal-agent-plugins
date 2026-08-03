#!/usr/bin/env python3
"""Sync mechanize skill's shared files into each adapter's plugin artifacts.

The mechanize skill has three files that must be identical across every
adapter (Claude, Codex, Cursor, Gemini):

    sentinels.md   (canonical at repo root, doubles as the project spec)
    FINDINGS.md    (canonical at common/mechanize/)
    CORE.md        (canonical at common/mechanize/)

Each adapter's SKILL.md is written per-adapter — that's where transcript
reading (Stage 1), spill collapsing (Stage 1.5), attachment vocabulary
(Stage 4.5 addendum), and cold-subagent mechanism (Stage 5.5) diverge.
Everything else is shared and gets copied here.

Usage:
    python3 build/sync_mechanize.py               # sync all adapters that have skills/mechanize/
    python3 build/sync_mechanize.py --check       # exit 1 if any adapter is out of sync (CI)
    python3 build/sync_mechanize.py claude codex  # sync a subset
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = ROOT / "adapters"

SOURCES = {
    "sentinels.md": ROOT / "sentinels.md",
    "FINDINGS.md": ROOT / "common" / "mechanize" / "FINDINGS.md",
    "CORE.md": ROOT / "common" / "mechanize" / "CORE.md",
}


def mechanize_adapters() -> list[str]:
    """Adapters that have a mechanize skill directory (created by their SKILL.md author)."""
    if not ADAPTERS_DIR.exists():
        return []
    return sorted(
        p.name for p in ADAPTERS_DIR.iterdir()
        if p.is_dir() and (p / "skills" / "mechanize").exists()
    )


def sync_into(adapter: str) -> list[str]:
    """Copy each shared file into the adapter's skills/mechanize/. Returns names updated."""
    dest_dir = ADAPTERS_DIR / adapter / "skills" / "mechanize"
    dest_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, src in SOURCES.items():
        if not src.exists():
            sys.exit(f"Source file missing: {src.relative_to(ROOT)}")
        dst = dest_dir / name
        if dst.exists() and filecmp.cmp(src, dst, shallow=False):
            continue
        shutil.copyfile(src, dst)
        changed.append(name)
    return changed


def check(adapter: str) -> list[str]:
    """Return list of shared files that are out of sync in this adapter (empty = clean)."""
    dest_dir = ADAPTERS_DIR / adapter / "skills" / "mechanize"
    if not dest_dir.exists():
        return [f"<missing dir: {dest_dir.relative_to(ROOT)}>"]
    drift = []
    for name, src in SOURCES.items():
        dst = dest_dir / name
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            drift.append(name)
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("adapters", nargs="*", help="adapters to sync (default: all)")
    parser.add_argument("--check", action="store_true", help="exit 1 if any adapter is out of sync")
    args = parser.parse_args()

    targets = args.adapters or mechanize_adapters()
    if not targets:
        print("No adapters have skills/mechanize/ yet — nothing to sync.")
        return 0

    unknown = [a for a in targets if not (ADAPTERS_DIR / a).is_dir()]
    if unknown:
        sys.exit(f"Unknown adapter(s): {', '.join(unknown)}")

    if args.check:
        exit_code = 0
        for adapter in targets:
            drift = check(adapter)
            if drift:
                print(f"OUT OF SYNC: {adapter}/skills/mechanize/ — {', '.join(drift)}")
                exit_code = 1
            else:
                print(f"in sync: {adapter}/skills/mechanize/")
        if exit_code:
            print("\nRun `python3 build/sync_mechanize.py` to fix.", file=sys.stderr)
        return exit_code

    for adapter in targets:
        changed = sync_into(adapter)
        if changed:
            print(f"synced {adapter}/skills/mechanize/ ({', '.join(changed)})")
        else:
            print(f"already in sync: {adapter}/skills/mechanize/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
