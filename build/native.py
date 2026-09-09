#!/usr/bin/env python3
"""Build self-contained npm/Pi artifacts; no pip install or network at build time."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent
ADAPTERS = ("opencode", "pi")


def build(adapter: str, destination: Path) -> Path:
    if adapter not in ADAPTERS:
        raise ValueError(f"Unknown native adapter: {adapter}")
    source = ROOT / "adapters" / adapter
    if destination.resolve() == source.resolve() or ROOT.resolve().is_relative_to(destination.resolve()):
        raise ValueError("Build destination must not overwrite source")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("node_modules", "tests", "lib", "__pycache__"))
    shutil.copytree(ROOT / "common" / "native", destination / "lib", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    shutil.copytree(ROOT / "core" / "cardinal_core", destination / "lib" / "cardinal_core", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "LICENSE", destination / "LICENSE")
    # Running Python smoke tests must not accidentally add local bytecode to
    # the tarball; nested npm ignores apply even beneath an explicit files dir.
    (destination / "lib" / ".npmignore").write_text("__pycache__/\n*.pyc\n*.pyo\n")
    version = json.loads((source / "package.json").read_text())["version"]
    bridge = destination / "lib" / "bridge.js"
    bridge.write_text(bridge.read_text().replace('"--runtime", runtime, command', f'"--runtime", runtime, "--version", "{version}", command'))
    # npm pack from the source directory must fail rather than omit the core.
    (destination / "check-package.js").write_text(
        'import { accessSync } from "node:fs";\n'
        'accessSync(new URL("./lib/cardinal_core/__init__.py", import.meta.url));\n'
        'accessSync(new URL("./lib/cardinal_native.py", import.meta.url));\n'
    )
    # prepack is also executed when repacking an installed tarball.
    manifest_path = destination / "package.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].append("check-package.js")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    for path in (destination / "bin").glob("*.js"):
        path.chmod(0o755)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # Python 3.9 applies choices to [] for an empty nargs='*' positional.
    parser.add_argument("adapters", nargs="*", metavar="ADAPTER")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "native")
    args = parser.parse_args()
    unknown = set(args.adapters) - set(ADAPTERS)
    if unknown:
        parser.error("unknown adapter(s): " + ", ".join(sorted(unknown)))
    for adapter in args.adapters or ADAPTERS:
        print(build(adapter, args.out / adapter))


if __name__ == "__main__":
    main()
