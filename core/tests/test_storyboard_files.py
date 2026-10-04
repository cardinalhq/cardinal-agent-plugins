"""Tests for cardinal_core.storyboard_files (the files a session edited,
stamped as a storyboard act's written_from context.paths).

Run from core/:  python3 -m unittest tests.test_storyboard_files -v
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from cardinal_core import storyboard_files as sf

ORIGIN = "git@github.com:CardinalHQ/Conductor.git"
REPO = "cardinalhq/conductor"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


class StoryboardFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.repo = self.dir / "repo"
        (self.repo / "pkg" / "a").mkdir(parents=True)
        _git(self.repo, "init", "-q")
        _git(self.repo, "remote", "add", "origin", ORIGIN)
        self.state = self.dir / "state" / "storyboard-files"

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_repo_relative_paths_most_recent_first(self):
        self.assertTrue(sf.record(self.state, "s1", str(self.repo / "pkg" / "a" / "x.ts")))
        self.assertTrue(sf.record(self.state, "s1", "pkg/a/y.ts", cwd=str(self.repo)))
        self.assertTrue(sf.record(self.state, "s1", str(self.repo / "pkg" / "a" / "x.ts")))
        self.assertEqual(sf.for_repo(self.state, "s1", REPO), ["pkg/a/x.ts", "pkg/a/y.ts"])
        self.assertEqual(sf.for_repo(self.state, "s1", "other/repo"), [])
        self.assertEqual(sf.for_repo(self.state, "s2", REPO), [])
        self.assertEqual(sf.for_repo(self.state, "s1", REPO, limit=1), ["pkg/a/x.ts"])
        raw = (self.state / "s1.json").read_text()
        self.assertNotIn('"/', json.dumps(json.loads(raw)["files"]))

    def test_file_mode_and_directory_mode(self):
        sf.record(self.state, "s1", str(self.repo / "pkg" / "a" / "x.ts"))
        self.assertEqual(stat.S_IMODE((self.state / "s1.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_capped_at_200(self):
        for i in range(sf.MAX_ENTRIES + 5):
            sf.record(self.state, "s1", str(self.repo / "pkg" / "a" / f"f{i}.ts"))
        paths = sf.for_repo(self.state, "s1", REPO, limit=1000)
        self.assertEqual(len(paths), sf.MAX_ENTRIES)
        self.assertEqual(paths[0], f"pkg/a/f{sf.MAX_ENTRIES + 4}.ts")

    def test_outside_a_repo_or_the_toplevel_is_not_recorded(self):
        outside = self.dir / "elsewhere"
        outside.mkdir()
        self.assertFalse(sf.record(self.state, "s1", str(outside / "x.ts")))
        self.assertFalse(sf.record(self.state, "s1", "../elsewhere/x.ts", cwd=str(self.repo)))
        self.assertFalse(sf.record(self.state, "s1", str(self.repo / "missing-dir" / "x.ts")))
        self.assertFalse(sf.record(None, "s1", str(self.repo / "pkg" / "x.ts")))
        self.assertFalse(sf.record(self.state, None, str(self.repo / "pkg" / "x.ts")))
        self.assertFalse(sf.record(self.state, "s1", {"not": "a path"}))
        self.assertEqual(sf.for_repo(self.state, "s1", REPO), [])

    def test_no_origin_is_not_recorded(self):
        bare = self.dir / "bare"
        bare.mkdir()
        _git(bare, "init", "-q")
        self.assertFalse(sf.record(self.state, "s1", str(bare / "x.ts")))

    def test_corrupt_state_reads_as_empty_and_is_rewritten(self):
        self.state.mkdir(parents=True)
        (self.state / "s1.json").write_text("{nope")
        self.assertEqual(sf.for_repo(self.state, "s1", REPO), [])
        self.assertTrue(sf.record(self.state, "s1", str(self.repo / "pkg" / "a" / "x.ts")))
        self.assertEqual(sf.for_repo(self.state, "s1", REPO), ["pkg/a/x.ts"])

    def test_valid_rel_path(self):
        for ok in ("a.ts", "pkg/a/x.ts", ".github/workflows/ci.yml"):
            self.assertTrue(sf.valid_rel_path(ok), ok)
        for bad in ("", "/abs", "../x", "a/../b", "a//b", "./a", "a\x00b", "a\nb", "x" * 513, None, 3):
            self.assertFalse(sf.valid_rel_path(bad), bad)


if __name__ == "__main__":
    unittest.main()
