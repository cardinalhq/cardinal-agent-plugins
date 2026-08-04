#!/usr/bin/env python3
"""Convert a sentinel-lint JSON payload into GitHub workflow annotations.

Reads the JSON payload from a file (path passed as argv[2]) and writes
``::error::`` / ``::warning::`` workflow commands to stdout so PR "Files
changed" annotations render inline. Prints a one-line summary as well.

argv:
    1. Sentinel directory (for the summary line).
    2. JSON payload path (as produced by
       ``executor.py lint --format=json``).

Exit code mirrors the lint CLI: 0 if the payload's ``passed`` is true,
else 1. The caller aggregates exit codes across all sentinel dirs.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _escape(value: str) -> str:
    """Escape a string for a GitHub workflow-command payload.

    Per the actions toolkit, ``%``, ``\r``, and ``\n`` must be percent-
    encoded so the command parser doesn't truncate at a newline.
    """
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: emit-annotations.py <sentinel-dir> <json-path>", file=sys.stderr)
        return 2

    sentinel_dir = argv[1]
    payload_path = Path(argv[2])
    try:
        data = json.loads(payload_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        # Emit an error annotation so the failure is visible in the PR
        # rather than silently swallowed as an internal action bug.
        print(
            f"::error::sentinel-lint: could not parse lint output for "
            f"{sentinel_dir}: {_escape(str(e))}"
        )
        return 1

    findings = data.get("findings") or []
    passed = bool(data.get("passed"))

    for f in findings:
        severity = f.get("severity", "FAIL")
        kind = "error" if severity == "FAIL" else "warning"
        file_ = f.get("file") or sentinel_dir
        line = f.get("line")
        code = f.get("code", "LINT")
        msg = f.get("message", "")
        fix = f.get("fix", "")
        payload = f"{code}: {msg} — fix: {fix}" if fix else f"{code}: {msg}"
        params = f"file={_escape(file_)}"
        if line is not None:
            params += f",line={int(line)}"
        print(f"::{kind} {params}::{_escape(payload)}")

    status = "PASS" if passed else "FAIL"
    fail_count = sum(1 for f in findings if f.get("severity") == "FAIL")
    warn_count = sum(1 for f in findings if f.get("severity") == "WARN")
    if not findings:
        print(f"{sentinel_dir}: PASS")
    else:
        print(f"{sentinel_dir}: {status} ({fail_count} fail, {warn_count} warn)")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
