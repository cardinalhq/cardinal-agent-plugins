"""build/devin.py: the release tarball runs on its own."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from support import ADAPTER_DIR, REPO_ROOT

from cardinal_devin import __version__


def _load_builder():
    spec = importlib.util.spec_from_file_location("cardinal_devin_build", REPO_ROOT / "build" / "devin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cardinal-devin-build-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_version_matches_package(self) -> None:
        self.assertEqual(builder.read_version(), __version__)

    def test_refuses_to_overwrite_source(self) -> None:
        for dest in (ADAPTER_DIR, ADAPTER_DIR / "dist", REPO_ROOT / "core" / "cardinal_core", REPO_ROOT,
                     REPO_ROOT.parent):
            with self.assertRaises(ValueError, msg=str(dest)):
                builder.build(dest)
        self.assertFalse((ADAPTER_DIR / "dist").exists())

    def test_tarball_is_self_contained(self) -> None:
        directory, tarball = builder.build(self.tmp / "out")
        stem = f"cardinal-devin-{__version__}"
        self.assertEqual(tarball.name, f"{stem}.tar.gz")
        self.assertEqual(directory.name, stem)

        with tarfile.open(tarball) as tar:
            members = {m.name: m for m in tar.getmembers()}
        names = set(members)
        for required in ("bin/cardinal-devin", "cardinal_devin/__init__.py", "cardinal_devin/cli.py",
                         "cardinal_core/__init__.py", "cardinal_core/otlp.py",
                         "playbook/decision-output.schema.json", "playbook/cardinal-decisions.md",
                         "README.md", "LICENSE"):
            self.assertIn(f"{stem}/{required}", names)
        for name in names:
            self.assertTrue(name == stem or name.startswith(stem + "/"), name)
            parts = name.split("/")
            self.assertNotIn("tests", parts, name)
            self.assertNotIn("__pycache__", parts, name)
            self.assertFalse(name.endswith((".pyc", ".pyo")), name)
        self.assertTrue(members[f"{stem}/bin/cardinal-devin"].mode & 0o111)
        self.assertEqual({m.uid for m in members.values()}, {0})

        # Extract away from the repo: the launcher's fallback (<root>/../../core)
        # does not exist there, so only the vendored cardinal_core can satisfy it.
        extract = self.tmp / "x"
        with tarfile.open(tarball) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(extract, filter="data")
            else:
                tar.extractall(extract)
        root = extract / stem
        self.assertFalse((root.parent.parent / "core").exists())
        launcher = root / "bin" / "cardinal-devin"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp), "LC_ALL": "C.UTF-8"}

        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, "-s", str(launcher), *args], capture_output=True, text=True,
                                  env=env, cwd=str(self.tmp), timeout=60, check=False)

        version = run("--version")
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), f"cardinal-devin {__version__}")
        schema = run("decision-schema")
        self.assertEqual(schema.returncode, 0, schema.stderr)
        self.assertIn("decisions", json.loads(schema.stdout)["properties"])
        # Importing the poller pulls in cardinal_core; a missing vendored copy fails here.
        status = run("status", "--state-dir", str(self.tmp / "state"))
        self.assertEqual(status.returncode, 1, status.stderr)
        self.assertIn("Not connected", status.stdout)
        self.assertNotIn("Traceback", status.stderr)


if __name__ == "__main__":
    unittest.main()
