#!/usr/bin/env python3
"""Build the self-contained Devin poller release artifact.

    python3 build/devin.py [--out DIR]    # default dist/devin
    python3 build/devin.py --print-version

Produces DIR/cardinal-devin-<version>/ and
DIR/cardinal-devin-<version>.tar.gz (top-level directory
cardinal-devin-<version>/). The layout is the one bin/cardinal-devin looks
for, with core vendored next to the adapter package:

    bin/cardinal-devin
    cardinal_devin/
    cardinal_core/        vendored from core/cardinal_core
    playbook/
    README.md
    LICENSE

No pip, no network. Tests and bytecode are excluded. The Docker image
(adapters/devin/Dockerfile) copies the same set of paths.
"""

from __future__ import annotations

import argparse
import re
import shutil
import stat
import sys
import tarfile
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "adapters" / "devin"
CORE = ROOT / "core" / "cardinal_core"
NAME = "cardinal-devin"

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "tests", ".DS_Store")
_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def read_version() -> str:
    match = _VERSION_RE.search((SOURCE / "cardinal_devin" / "__init__.py").read_text(encoding="utf-8"))
    if not match:
        raise ValueError("cardinal_devin/__init__.py has no __version__")
    return match.group(1)


def _check_destination(destination: Path) -> None:
    dest = destination.resolve()
    for source in (SOURCE.resolve(), CORE.resolve()):
        if dest == source or dest.is_relative_to(source):
            raise ValueError(f"Build destination must not overwrite source: {destination}")
    if ROOT.resolve().is_relative_to(dest):
        raise ValueError(f"Build destination must not contain the repository: {destination}")


def _normalize(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build(out: Path, version: Optional[str] = None) -> Tuple[Path, Path]:
    """(artifact dir, tarball)."""
    version = version or read_version()
    stem = f"{NAME}-{version}"
    out = out.expanduser()
    directory = out / stem
    tarball = out / f"{stem}.tar.gz"
    _check_destination(out)
    _check_destination(directory)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    shutil.copytree(SOURCE / "cardinal_devin", directory / "cardinal_devin", ignore=IGNORE)
    shutil.copytree(SOURCE / "playbook", directory / "playbook", ignore=IGNORE)
    shutil.copytree(CORE, directory / "cardinal_core", ignore=IGNORE)
    (directory / "bin").mkdir()
    launcher = directory / "bin" / NAME
    shutil.copy2(SOURCE / "bin" / NAME, launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    shutil.copy2(SOURCE / "README.md", directory / "README.md")
    shutil.copy2(ROOT / "LICENSE", directory / "LICENSE")

    for required in ("cardinal_core/__init__.py", "cardinal_devin/cli.py", "playbook/decision-output.schema.json"):
        if not (directory / required).is_file():
            raise RuntimeError(f"artifact is missing {required}")

    if tarball.exists():
        tarball.unlink()
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(directory, arcname=stem, filter=_normalize)
    return directory, tarball


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the cardinal-devin release tarball.")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "devin", help="Output directory (default dist/devin).")
    parser.add_argument("--print-version", action="store_true", help="Print the adapter version and exit.")
    args = parser.parse_args(argv)
    if args.print_version:
        print(read_version())
        return 0
    try:
        _, tarball = build(args.out)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(tarball)
    return 0


if __name__ == "__main__":
    sys.exit(main())
