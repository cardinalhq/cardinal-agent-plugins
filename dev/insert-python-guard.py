#!/usr/bin/env python3
"""Insert the shared Python-version guard into every cardinal-* script.

Motivation: macOS ships `python3` = 3.9.6 by default. Our scripts use
PEP-604 union syntax (`str | None`) and import `cardinal_core`, which
declares `requires-python = ">=3.11"`. Running them under 3.9 fails at
runtime — the user sees an opaque TypeError and the plugin never
completes. We inline a small guard block at the top of each script that
re-execs into python3.13/3.12/3.11 if any of them is on PATH, and
prints a clear install hint otherwise.

The guard block is delimited by the `cardinal:py311-guard` sentinel on
each of its lines so `dev/check-py-guard.sh` can grep for its presence
in CI.

Placement:
  - If the script has `from __future__ import ...` (which Python pins
    to the top of the module), insert AFTER the last such statement.
  - Otherwise insert BEFORE the first `import`/`from` line.

Placement uses `ast.parse` to walk top-level statements, so a
`from __future__ import annotations` line embedded inside a triple-
quoted string literal (e.g. `LAUNCHER_TEMPLATE = '''...'''` in
codex/cardinal-connect) does NOT confuse the anchor.

Idempotent: rerunning does nothing when the guard is already present.
Run:  python3 dev/insert-python-guard.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SENTINEL = "cardinal:py311-guard"

GUARD = f'''import sys                        # {SENTINEL}
if sys.version_info < (3, 11):    # {SENTINEL}
    import os, shutil             # {SENTINEL}
    for _p in ("python3.13", "python3.12", "python3.11"):
        _x = shutil.which(_p)
        if _x: os.execv(_x, [_x, __file__, *sys.argv[1:]])
    sys.stderr.write("cardinal: needs Python >=3.11 (found "
        + sys.version.split()[0] + "). Try: brew install python@3.11\\n")
    sys.exit(1)                   # {SENTINEL}

'''


def find_scripts(root: Path) -> list[Path]:
    return sorted(
        p for p in (root / "adapters").rglob("cardinal-*")
        if p.is_file() and p.parent.name in ("bin", "scripts")
    )


def find_insert_line(src: str) -> int:
    """Return the 1-based line number *after* which to insert the guard.

    Returns 0 to mean "insert before line 1" when the file has no
    imports at all (unlikely for a real script, but safe).
    """
    tree = ast.parse(src)
    last_future = None
    first_import = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            last_future = node.end_lineno
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and first_import is None:
            first_import = node.lineno
    if last_future is not None:
        return last_future
    if first_import is not None:
        return first_import - 1
    return 0


def patch(path: Path) -> str:
    """Insert (or leave alone if present) the guard. Returns an action tag."""
    src = path.read_text()
    if SENTINEL in src:
        return "already-present"
    after = find_insert_line(src)
    lines = src.splitlines(keepends=True)
    ins = after  # 1-based "after N" == 0-based index N
    # If the next line is blank, keep the blank and insert after it so the
    # guard block reads as its own paragraph.
    if ins < len(lines) and lines[ins].strip() == "":
        ins += 1
    new = "".join(lines[:ins]) + GUARD + "".join(lines[ins:])
    path.write_text(new)
    return f"inserted after real line {after}"


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    scripts = find_scripts(root)
    if not scripts:
        print("No cardinal-* scripts found under adapters/", file=sys.stderr)
        return 1
    for p in scripts:
        action = patch(p)
        print(f"{p.relative_to(root)}: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
