"""Tests for cardinal_core.storyboard_context (the `context` a storyboard is
created, found and extended with).

Run from core/:  python3 -m unittest tests.test_storyboard_context -v
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from cardinal_core import storyboard_context as sc

CLIENT = "claude-code/0.36.0"
ORIGIN = "git@github.com:CardinalHQ/Conductor.git"
ALLOWED_OUTSIDE_GIT = {"workdir_hash", "client", "actor_email"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_repo(root: Path, branch: str = "feat/rollback") -> str:
    _git(root, "init", "-q")
    _git(root, "checkout", "-q", "-b", branch)
    _git(root, "remote", "add", "origin", ORIGIN)
    (root / "pkg" / "a").mkdir(parents=True)
    (root / "pkg" / "a" / "f.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return _git(root, "rev-parse", "HEAD")


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # realpath: macOS hands out /var/... which is a symlink to /private/var/...
        self.dir = Path(os.path.realpath(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def assert_no_paths(self, ctx: dict):
        for key, value in ctx.items():
            text = str(value)
            self.assertFalse(text.startswith("/"), f"{key}={value!r} is an absolute path")
            self.assertNotIn(str(self.dir), text, key)
            self.assertNotIn(self.tmp.name, text, key)


class CollectInRepoTests(_TmpCase):
    def setUp(self):
        super().setUp()
        self.head = _make_repo(self.dir)

    def test_repo_is_canonical_and_lowercased(self):
        ctx = sc.collect(str(self.dir), client=CLIENT)
        self.assertEqual(ctx["repo"], "cardinalhq/conductor")
        self.assertEqual(ctx["branch"], "feat/rollback")
        self.assertEqual(ctx["head_sha"], self.head)
        self.assertEqual(ctx["client"], CLIENT)

    def test_repo_path_is_relative_to_the_toplevel(self):
        self.assertEqual(sc.collect(str(self.dir / "pkg" / "a"), client=CLIENT)["repo_path"], "pkg/a")
        self.assertEqual(sc.collect(str(self.dir), client=CLIENT)["repo_path"], ".")

    def test_detached_head_has_no_branch(self):
        _git(self.dir, "checkout", "-q", "--detach")
        ctx = sc.collect(str(self.dir), client=CLIENT)
        self.assertNotIn("branch", ctx)
        self.assertEqual(ctx["head_sha"], self.head)

    def test_no_value_is_an_absolute_path(self):
        ctx = sc.collect(str(self.dir / "pkg" / "a"), client=CLIENT, actor_email="Dev@Example.com",
                         pr_resolver=lambda *_: (10, "https://github.com/cardinalhq/conductor/pull/10"))
        self.assert_no_paths(ctx)
        self.assertEqual(ctx["actor_email"], "dev@example.com")

    def test_workdir_hash_is_32_hex_and_per_directory(self):
        a = sc.collect(str(self.dir), client=CLIENT)["workdir_hash"]
        b = sc.collect(str(self.dir / "pkg" / "a"), client=CLIENT)["workdir_hash"]
        self.assertRegex(a, r"^[0-9a-f]{32}$")
        self.assertRegex(b, r"^[0-9a-f]{32}$")
        self.assertNotEqual(a, b)
        self.assertEqual(a, sc.collect(str(self.dir), client=CLIENT)["workdir_hash"])
        # Machine-scoped: the same directory on another host is another directory.
        self.assertNotEqual(sc.workdir_hash(str(self.dir), "host-a"), sc.workdir_hash(str(self.dir), "host-b"))

    def test_pr_comes_from_the_resolver(self):
        seen = []

        def resolver(cwd, repo, branch):
            seen.append((repo, branch))
            return 10, "https://github.com/cardinalhq/conductor/pull/10"

        ctx = sc.collect(str(self.dir), client=CLIENT, pr_resolver=resolver)
        self.assertEqual(seen, [("cardinalhq/conductor", "feat/rollback")])
        self.assertEqual(ctx["pr_number"], 10)
        self.assertEqual(ctx["pr_url"], "https://github.com/cardinalhq/conductor/pull/10")

    def test_a_failing_resolver_drops_only_the_pr(self):
        def boom(*_):
            raise RuntimeError("gh exploded")

        ctx = sc.collect(str(self.dir), client=CLIENT, pr_resolver=boom)
        self.assertNotIn("pr_number", ctx)
        self.assertNotIn("pr_url", ctx)
        self.assertEqual(ctx["repo"], "cardinalhq/conductor")

    def test_invalid_pr_values_are_dropped(self):
        for found in ((0, None), (True, None), ("10", None), (2 ** 31, None), None, (1, 2, 3)):
            ctx = sc.collect(str(self.dir), client=CLIENT, pr_resolver=lambda *_: found)
            self.assertNotIn("pr_number", ctx, found)
        ctx = sc.collect(str(self.dir), client=CLIENT, pr_resolver=lambda *_: (7, "http://insecure/pull/7"))
        self.assertEqual(ctx["pr_number"], 7)
        self.assertNotIn("pr_url", ctx)

    def test_no_resolver_no_pr(self):
        self.assertNotIn("pr_number", sc.collect(str(self.dir), client=CLIENT))

    def test_origin_that_is_not_a_repo_url_drops_repo(self):
        _git(self.dir, "remote", "set-url", "origin", "/srv/git/local.git")
        ctx = sc.collect(str(self.dir), client=CLIENT, pr_resolver=lambda *_: (10, None))
        self.assertNotIn("repo", ctx)
        self.assertNotIn("pr_number", ctx, "pr_number requires repo")
        self.assertEqual(ctx["branch"], "feat/rollback")


class CollectOutsideGitTests(_TmpCase):
    def test_non_git_dir_has_only_machine_fields(self):
        ctx = sc.collect(str(self.dir), client=CLIENT, actor_email="dev@example.com",
                         pr_resolver=lambda *_: (10, None))
        self.assertLessEqual(set(ctx), ALLOWED_OUTSIDE_GIT)
        self.assertRegex(ctx["workdir_hash"], r"^[0-9a-f]{32}$")
        self.assert_no_paths(ctx)

    def test_missing_directory_never_raises(self):
        ctx = sc.collect(str(self.dir / "gone"), client=CLIENT)
        self.assertLessEqual(set(ctx), ALLOWED_OUTSIDE_GIT)

    def test_bad_client_and_email_are_omitted(self):
        ctx = sc.collect(str(self.dir), client="claude code/../../x", actor_email="unknown")
        self.assertNotIn("client", ctx)
        self.assertNotIn("actor_email", ctx)


class ContractTests(unittest.TestCase):
    def test_fields_match_the_server_and_never_carry_cwd(self):
        # conductor packages/maestro/src/storyboard/context.ts CONTEXT_FIELDS.
        self.assertEqual(
            sc.CONTEXT_FIELDS,
            ("repo", "repo_path", "branch", "pr_number", "pr_url", "head_sha", "workdir_hash", "client",
             "actor_email"),
        )
        self.assertNotIn("cwd", sc.CONTEXT_FIELDS)

    def test_output_keys_are_context_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = sc.collect(tmp, client=CLIENT, actor_email="a@b.co")
        self.assertLessEqual(set(ctx), set(sc.CONTEXT_FIELDS))
        self.assertTrue(all(v is not None for v in ctx.values()))

    def test_default_pr_resolver_uses_the_decisions_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Protected branch: resolve_pr short-circuits without running gh.
            resolve = sc.default_pr_resolver(Path(tmp))
            self.assertEqual(resolve(tmp, "cardinalhq/conductor", "main"), (None, None))
        self.assertTrue(re.match(r"^[0-9a-f]{32}$", sc.workdir_hash("/x", "h")))



class AssociationsContextTests(_TmpCase):
    def test_protected_branch_is_omitted_and_no_pr_lookup(self):
        for branch in ("main", "master", "develop", "trunk"):
            root = self.dir / branch
            root.mkdir()
            _make_repo(root, branch)
            seen = []
            ctx = sc.collect(str(root), client=CLIENT, pr_resolver=lambda *a: seen.append(a) or (1, None))
            self.assertNotIn("branch", ctx, branch)
            self.assertNotIn("pr_number", ctx, branch)
            self.assertEqual(seen, [], branch)
            self.assertIn("head_sha", ctx)

    def test_paths_only_with_edited_paths_and_a_repo(self):
        _make_repo(self.dir)
        self.assertNotIn("paths", sc.collect(str(self.dir), client=CLIENT))
        asked = []

        def edited(repo):
            asked.append(repo)
            return ["pkg/a/f.txt", "/etc/passwd", "../x", "pkg/a/f.txt", "b.ts"]

        ctx = sc.collect(str(self.dir), client=CLIENT, edited_paths=edited)
        self.assertEqual(asked, ["cardinalhq/conductor"])
        self.assertEqual(ctx["paths"], ["pkg/a/f.txt", "b.ts"])
        self.assertEqual(sc.collect(str(self.dir), client=CLIENT, edited_paths=lambda r: [])
                         .get("paths"), None)
        self.assertNotIn("paths", sc.collect(str(self.dir), client=CLIENT, edited_paths=lambda r: 1 / 0))
        many = sc.collect(str(self.dir), client=CLIENT, edited_paths=lambda r: [f"f{i}.ts" for i in range(80)])
        self.assertEqual(len(many["paths"]), sc.MAX_PATHS)

    def test_no_paths_outside_git(self):
        ctx = sc.collect(str(self.dir), client=CLIENT, edited_paths=lambda r: ["a.ts"])
        self.assertNotIn("paths", ctx)


if __name__ == "__main__":
    unittest.main()
