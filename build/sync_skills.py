#!/usr/bin/env python3
"""Sync shared skill cores into each adapter's plugin artifacts.

A shared skill has one canonical core under `common/<skill>/` and a thin,
per-adapter SKILL.md under `adapters/<a>/skills/<skill>/` (what differs per
agent: where the scripts live, how to connect, agent-specific wording). Each
adapter ships as a self-contained plugin, so the core is copied — byte-identical
— next to every adapter's SKILL.md.

    common/migrate-from-grafana/{CORE.md, scripts/, references/}
        -> adapters/<a>/skills/migrate-from-grafana/
    common/canvas/{CORE.md, exemplars/}
        -> adapters/<a>/skills/canvas/   (the renderer it uses ships with storyboard)
    common/supervise/CORE.md
        -> adapters/<a>/skills/supervise/
    common/checks/CORE.md
        -> adapters/<a>/checks/skill/    (an opt-in template: `cardinal-checks on`
                                          installs it into the skills directory)

An adapter opts in by having the skill directory (with its own SKILL.md).

Usage:
    python3 build/sync_skills.py               # sync every shared skill into every opted-in adapter
    python3 build/sync_skills.py --check       # exit 1 on drift (CI)
    python3 build/sync_skills.py claude codex  # sync a subset of adapters
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = ROOT / "adapters"
COMMON_DIR = ROOT / "common"

# skill name -> files/dirs (relative to common/<skill>/) copied into each adapter.
SKILLS = {
    "storyboard": ["CORE.md", "scripts"],
    "canvas": ["CORE.md", "exemplars"],
    "migrate-from-grafana": ["CORE.md", "scripts", "references"],
    "checks": ["CORE.md"],
    "supervise": ["CORE.md"],
}
# Where a skill lives in an adapter, when not skills/<skill>/ (relative to the adapter).
DEST = {
    "checks": Path("checks") / "skill",
}

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def skill_dir(adapter: str, skill: str) -> Path:
    return ADAPTERS_DIR / adapter / DEST.get(skill, Path("skills") / skill)


def skill_adapters(skill: str) -> list[str]:
    return sorted(
        p.name for p in ADAPTERS_DIR.iterdir()
        if p.is_dir() and skill_dir(p.name, skill).is_dir()
    )


def shared_files(skill: str) -> dict[str, Path]:
    """{path relative to the skill dir: canonical source file}."""
    src_root = COMMON_DIR / skill
    out = {}
    for entry in SKILLS[skill]:
        src = src_root / entry
        if not src.exists():
            sys.exit(f"Source missing: {src.relative_to(ROOT)}")
        files = [src] if src.is_file() else sorted(
            f for f in src.rglob("*")
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc" and f.name != ".DS_Store"
        )
        for f in files:
            out[str(f.relative_to(src_root))] = f
    return out


def drift(skill: str, adapter: str) -> list[str]:
    """Shared paths that are missing, different, or stale in this adapter (empty = clean)."""
    dest = skill_dir(adapter, skill)
    expected = shared_files(skill)
    out = [rel for rel, src in expected.items()
           if not (dest / rel).is_file() or not filecmp.cmp(src, dest / rel, shallow=False)]
    for entry in SKILLS[skill]:
        d = dest / entry
        if d.is_dir():
            out += [f"{f.relative_to(dest)} (stale)" for f in sorted(d.rglob("*"))
                    if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
                    and str(f.relative_to(dest)) not in expected]
    return out


def sync_into(skill: str, adapter: str) -> None:
    dest = skill_dir(adapter, skill)
    for entry in SKILLS[skill]:
        src, dst = COMMON_DIR / skill / entry, dest / entry
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=IGNORE)
        else:
            shutil.copyfile(src, dst)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("adapters", nargs="*", help="adapters to sync (default: all opted in)")
    parser.add_argument("--check", action="store_true", help="exit 1 if any adapter is out of sync")
    args = parser.parse_args()

    unknown = [a for a in args.adapters if not (ADAPTERS_DIR / a).is_dir()]
    if unknown:
        sys.exit(f"Unknown adapter(s): {', '.join(unknown)}")

    exit_code = 0
    for skill in SKILLS:
        targets = [a for a in (args.adapters or skill_adapters(skill))
                   if skill_dir(a, skill).is_dir()]
        for adapter in targets:
            where = f"{skill_dir(adapter, skill).relative_to(ADAPTERS_DIR)}/"
            if not (skill_dir(adapter, skill) / "SKILL.md").is_file():
                print(f"MISSING SKILL.md: {where}")
                exit_code = 1
            d = drift(skill, adapter)
            if args.check:
                if d:
                    print(f"OUT OF SYNC: {where} — {', '.join(d)}")
                    exit_code = 1
                else:
                    print(f"in sync: {where}")
            elif d:
                sync_into(skill, adapter)
                print(f"synced {where} ({len(d)} file(s))")
            else:
                print(f"already in sync: {where}")
    if args.check and exit_code:
        print("\nRun `python3 build/sync_skills.py` to fix.", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
