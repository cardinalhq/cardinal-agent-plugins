#!/usr/bin/env python3
"""Enforce that mechanize source changes ship with adapter version bumps.

The mechanize skill is distributed inside each adapter's plugin
(cardinal-{claude,codex,cursor,gemini}-plugin). When mechanize source
changes, the release only reaches users if the affected adapter's
plugin.json version is bumped — that bump is what
`.github/workflows/release-mirrors.yml` watches to fire the mirror build.

This script is a PR-time gate: it inspects git for what changed relative
to a base ref and fails if any adapter whose mechanize files were touched
did not also have its plugin.json version bumped in the same PR.

Rules:
  - A change to `sentinels.md`, `common/mechanize/**`, or
    `build/sync_mechanize.py` affects EVERY adapter with a
    `skills/mechanize/` directory (they all resync).
  - A change to `adapters/<a>/skills/mechanize/**` affects only that
    adapter.
  - For each affected adapter, `.<agent>-plugin/plugin.json`'s `version`
    field must have changed vs the base ref.

Usage:
    python3 build/check_mechanize_release.py --base origin/main
    python3 build/check_mechanize_release.py --base HEAD~1 --head HEAD
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Adapter -> path to its plugin.json (relative to repo root).
PLUGIN_JSONS = {
    "claude": "adapters/claude/.claude-plugin/plugin.json",
    "codex": "adapters/codex/.codex-plugin/plugin.json",
    "cursor": "adapters/cursor/.cursor-plugin/plugin.json",
    "gemini": "adapters/gemini/.gemini-plugin/plugin.json",
}

SHARED_MECHANIZE_PATHS = (
    "sentinels.md",
    "common/mechanize/",
    "build/sync_mechanize.py",
)


def git_diff_names(base: str, head: str) -> list[str]:
    """Return files changed between base and head (name-only diff)."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def touches_shared(changed: list[str]) -> bool:
    return any(
        f == p or f.startswith(p) for f in changed for p in SHARED_MECHANIZE_PATHS
    )


def touches_adapter_mechanize(changed: list[str], adapter: str) -> bool:
    prefix = f"adapters/{adapter}/skills/mechanize/"
    return any(f.startswith(prefix) for f in changed)


def affected_adapters(changed: list[str]) -> set[str]:
    """Adapters whose plugin needs a version bump given these changes."""
    if touches_shared(changed):
        return {a for a in PLUGIN_JSONS if (ROOT / f"adapters/{a}/skills/mechanize").exists()}
    return {a for a in PLUGIN_JSONS if touches_adapter_mechanize(changed, a)}


def version_at(ref: str, plugin_json_path: str) -> str | None:
    """Read the `version` field from a plugin.json at a given git ref, or None if absent."""
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{plugin_json_path}"],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError:
        return None
    try:
        return json.loads(out.stdout).get("version")
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="git ref to compare against (e.g. origin/main)")
    parser.add_argument("--head", default="HEAD", help="git ref for the proposed change (default: HEAD)")
    args = parser.parse_args()

    changed = git_diff_names(args.base, args.head)
    affected = affected_adapters(changed)

    if not affected:
        print("No mechanize source changes detected — nothing to enforce.")
        return 0

    print(f"Mechanize source changes affect: {', '.join(sorted(affected))}")

    failures: list[str] = []
    for adapter in sorted(affected):
        plugin_json = PLUGIN_JSONS[adapter]
        base_v = version_at(args.base, plugin_json)
        head_v = version_at(args.head, plugin_json)
        if head_v is None:
            failures.append(f"{adapter}: {plugin_json} unreadable at head ref")
            continue
        if base_v == head_v:
            failures.append(
                f"{adapter}: {plugin_json} version is still {head_v} "
                f"(mechanize changed but plugin was not bumped — mirror release will not fire)"
            )
        else:
            print(f"  {adapter}: {base_v} -> {head_v} OK")

    if failures:
        print("\nMechanize release gate FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nBump each affected adapter's plugin.json `version` in this PR so "
            "release-mirrors.yml ships the new mechanize files.\n"
            "See build/check_mechanize_release.py for the rule.",
            file=sys.stderr,
        )
        return 1

    print("Mechanize release gate PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
