#!/usr/bin/env python3
"""Detect omnigent policy-contract drift against the verified pin.

The omnigent adapter (adapters/omnigent) is verified against a specific
upstream commit (OMNIGENT_VERIFIED_COMMIT in cardinal_omnigent/__init__.py).
omnigent is alpha — the policy contract can churn without notice. This
script clones upstream, diffs the CONTRACT SURFACE between the pinned
commit and origin/main, and exits non-zero when they differ, so churn is
detected in CI instead of discovered in prod.

Contract surface (the files the adapter's behavior was derived from):
  - omnigent/policies/schema.py    PolicyEvent / EventContext wire shape
  - omnigent/policies/types.py     Phase enum, FAIL_CLOSED_PHASES,
                                   EvaluationContext, PolicyResult
  - omnigent/policies/function.py  FunctionPolicy._build_event (the dict
                                   our accessors actually read)
  - omnigent/policies/registry.py  POLICY_REGISTRY resolution

On failure: re-verify the adapter against upstream HEAD (see
docs/specs/omnigent-adapter.md §Verified integration facts), update the
adapter if the contract moved, then bump OMNIGENT_VERIFIED_COMMIT.

Usage: python3 build/omnigent_drift.py [--repo-url URL] [--ref REF]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_INIT = REPO_ROOT / "adapters" / "omnigent" / "cardinal_omnigent" / "__init__.py"
DEFAULT_REPO_URL = "https://github.com/omnigent-ai/omnigent.git"

CONTRACT_FILES = (
    "omnigent/policies/schema.py",
    "omnigent/policies/types.py",
    "omnigent/policies/function.py",
    "omnigent/policies/registry.py",
)


def pinned_commit() -> str:
    text = ADAPTER_INIT.read_text(encoding="utf-8")
    match = re.search(r'OMNIGENT_VERIFIED_COMMIT\s*=\s*"([0-9a-f]{7,40})"', text)
    if match is None:
        sys.exit(f"error: OMNIGENT_VERIFIED_COMMIT not found in {ADAPTER_INIT}")
    return match.group(1)


def run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"error: {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--ref", default="origin/main",
                        help="upstream ref to compare the pin against")
    opts = parser.parse_args()

    pin = pinned_commit()
    print(f"pinned commit : {pin}")
    print(f"compare ref   : {opts.ref}")

    with tempfile.TemporaryDirectory(prefix="omnigent-drift-") as tmp:
        clone = Path(tmp) / "omnigent"
        run(["git", "clone", "--quiet", "--filter=blob:none", opts.repo_url,
             str(clone)], cwd=Path(tmp))
        head = run(["git", "rev-parse", "--short", opts.ref], cwd=clone).strip()
        print(f"upstream head : {head}")

        # Verify the pin exists upstream (a force-push that drops it is
        # itself a reportable event).
        proc = subprocess.run(["git", "cat-file", "-e", f"{pin}^{{commit}}"],
                              cwd=clone, capture_output=True)
        if proc.returncode != 0:
            print(f"DRIFT: pinned commit {pin} no longer exists upstream "
                  "(force-push?). Re-verify the contract and re-pin.")
            return 1

        diff = run(["git", "diff", "--stat", f"{pin}..{opts.ref}", "--",
                    *CONTRACT_FILES], cwd=clone)
        if not diff.strip():
            print(f"OK: contract surface unchanged between {pin} and {head}.")
            return 0

        print("DRIFT: omnigent policy contract changed since the verified pin.\n")
        print(diff)
        print(run(["git", "diff", f"{pin}..{opts.ref}", "--", *CONTRACT_FILES],
                  cwd=clone))
        print("Re-verify adapters/omnigent against upstream (spec: "
              "docs/specs/omnigent-adapter.md), update the adapter if the "
              "contract moved, then bump OMNIGENT_VERIFIED_COMMIT to "
              f"{head}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
