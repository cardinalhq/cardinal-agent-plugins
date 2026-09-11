"""Tests for cardinal_core.decisions (cardinal.decision telemetry).

Run from core/:  python3 -m unittest tests.test_decisions -v
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cardinal_core import decisions
from cardinal_core.otlp import log_record


def _files(prefix: str, n: int, ext: str = ".go") -> list[str]:
    return [f"{prefix}/f{i}{ext}" for i in range(n)]


# svc splits (121 files > cap 100): api and db become dir domains; util
# (8) plus three direct files sweep into "svc (misc)" (11 >= min 10).
SERVICE_TREE = (
    _files("svc/api", 60) + _files("svc/db", 50) + _files("svc/util", 8)
    + ["svc/main.go", "svc/a.go", "svc/b.go"] + _files("tools", 12, ".py")
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(root: Path, files: list[str]) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
    ).stdout.strip()


class BuildDecisionTest(unittest.TestCase):
    def test_requires_choice(self):
        with self.assertRaises(decisions.DecisionError):
            decisions.build_decision(choice="   ")

    def test_rejects_unknown_decided_by(self):
        with self.assertRaises(decisions.DecisionError):
            decisions.build_decision(choice="Use X", decided_by="robot")

    def test_derives_id_and_caps_fields(self):
        d = decisions.build_decision(
            choice="Use mutation-run S3 LSM",
            question="How do we index billions of trace IDs?",
            rationale="word " * 400,
            alternatives=["Per-ID rows in Postgres", "  ", "x" * 500],
        )
        self.assertEqual(d["id"], "use-mutation-run-s3-lsm")
        self.assertEqual(d["decided_by"], "agent")
        self.assertLessEqual(len(d["rationale"]), decisions.MAX_RATIONALE)
        self.assertTrue(d["rationale"].endswith("…"))
        self.assertEqual(len(d["alternatives"]), 2, "blank alternatives are dropped")
        self.assertLessEqual(len(d["alternatives"][1]), decisions.MAX_ALTERNATIVE)

    def test_auto_id_does_not_replace_a_different_decision(self):
        existing = [{"id": "use-x", "choice": "Use Y"}]
        d = decisions.build_decision(choice="Use X", existing=existing)
        self.assertEqual(d["id"], "use-x-2")

    def test_explicit_id_is_a_revision(self):
        existing = [{"id": "use-x", "choice": "Use Y"}]
        d = decisions.build_decision(choice="Use X", decision_id="use-x", existing=existing)
        self.assertEqual(d["id"], "use-x")

    def test_links(self):
        d = decisions.build_decision(
            choice="Use X", follows_from=["a"], refines=["b", "b"], supersedes=["c"],
        )
        self.assertEqual(d["links"], [
            {"relation": "follows_from", "to": "a"},
            {"relation": "refines", "to": "b"},
            {"relation": "supersedes", "to": "c"},
        ])

    def test_rejects_self_link_and_bad_ids(self):
        with self.assertRaises(decisions.DecisionError):
            decisions.build_decision(choice="Use X", decision_id="x", supersedes=["x"])
        with self.assertRaises(decisions.DecisionError):
            decisions.build_decision(choice="Use X", refines=["Not An Id!"])
        with self.assertRaises(decisions.DecisionError):
            decisions.build_decision(choice="Use X", decision_id="-bad")


class AnchorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.root, "pkg", "sub"))

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self, spec: str, cwd: str | None = None) -> dict:
        return decisions.parse_anchor(spec, self.root, cwd or self.root)

    def test_file_and_directory(self):
        self.assertEqual(self.parse("pkg/a.py"), {"kind": "file", "identifier": "pkg/a.py", "path": "pkg/a.py"})
        self.assertEqual(self.parse("pkg/sub/")["kind"], "directory")
        self.assertEqual(self.parse("pkg/sub/")["path"], "pkg/sub")

    def test_paths_are_relative_to_repo_root(self):
        cwd = os.path.join(self.root, "pkg")
        self.assertEqual(self.parse("sub/b.py", cwd=cwd)["path"], "pkg/sub/b.py")
        self.assertEqual(self.parse(os.path.join(self.root, "pkg", "c.py"))["path"], "pkg/c.py")

    def test_symbol_forms(self):
        self.assertEqual(
            self.parse("pkg/a.py::Store.put"),
            {"kind": "symbol", "identifier": "Store.put", "path": "pkg/a.py"},
        )
        self.assertEqual(
            self.parse("symbol:lkrn::segfilter::insert"),
            {"kind": "symbol", "identifier": "lkrn::segfilter::insert"},
        )
        self.assertEqual(
            self.parse("config:LAKERUNNER_X@pkg/config.py"),
            {"kind": "config", "identifier": "LAKERUNNER_X", "path": "pkg/config.py"},
        )

    def test_rejects_empty(self):
        with self.assertRaises(decisions.DecisionError):
            self.parse("  ")
        with self.assertRaises(decisions.DecisionError):
            self.parse("schema:")


class D18Test(unittest.TestCase):
    def test_source_classification(self):
        for path in ("svc/api/h.go", "scripts/deploy.sh", "lrdb/queries/x.sql", ".github/x/y.py"):
            self.assertTrue(decisions.is_source_file(path), path)
        for path in (
            "README.md", "docs/guide.md", "setup.py", "config/app.yaml", "tests/test_a.py",
            "pkg/examples/demo.go", "node_modules/x/index.js", ".hidden/x.py", "go.sum",
            "web/app.min.js", "assets/logo.png",
        ):
            self.assertFalse(decisions.is_source_file(path), path)

    def test_domains(self):
        domains = decisions.d18_domains(SERVICE_TREE, cap=100, min_=10)
        self.assertEqual(
            [(d["name"], d["kind"], d["file_count"]) for d in domains],
            [("svc/api", "dir", 60), ("svc/db", "dir", 50), ("tools", "dir", 12), ("svc (misc)", "remainder", 11)],
        )
        misc = domains[-1]
        self.assertEqual(misc["covers"], ["svc/*", "svc/util/**"])

    def test_autoscale(self):
        self.assertEqual(decisions.d18_autoscale(10, 100, 5000), (10, 100))
        self.assertEqual(decisions.d18_autoscale(10, 100, 128), (5, 100))
        self.assertEqual(decisions.d18_autoscale(10, 100, 20), (1, 50))

    def test_match_files(self):
        domains = decisions.d18_domains(SERVICE_TREE, cap=100, min_=10)
        m = lambda p: decisions.match_clusters(domains, p)  # noqa: E731
        self.assertEqual(m("svc/api/f1.go"), ["d18:svc/api"])
        self.assertEqual(m("svc/api/brand_new.go"), ["d18:svc/api"], "new files match by cover")
        self.assertEqual(m("svc/main.go"), ["d18:svc (misc)"])
        self.assertEqual(m("svc/util/f1.go"), ["d18:svc (misc)"])
        self.assertEqual(m("docs/guide.md"), [])

    def test_match_directories(self):
        domains = decisions.d18_domains(SERVICE_TREE, cap=100, min_=10)
        self.assertEqual(
            decisions.match_clusters(domains, "svc", is_dir=True),
            ["d18:svc/api", "d18:svc/db", "d18:svc (misc)"],
        )
        self.assertEqual(decisions.match_clusters(domains, "svc/api/", is_dir=True), ["d18:svc/api"])
        self.assertEqual(decisions.match_clusters(domains, "", is_dir=True), [])

    def test_code_clusters_from_committed_tree_with_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            sha = _make_repo(repo, SERVICE_TREE + ["README.md"])
            cache = Path(tmp) / "cache"
            anchors = [
                {"kind": "file", "identifier": "svc/api/f3.go", "path": "svc/api/f3.go"},
                {"kind": "file", "identifier": "svc/util/f1.go", "path": "svc/util/f1.go"},
                {"kind": "symbol", "identifier": "Main", "path": "svc/main.go"},
                {"kind": "config", "identifier": "LAKERUNNER_X"},
            ]
            ids, scheme = decisions.code_clusters(anchors, str(repo), sha, cache)
            # 133 source files (README.md is not source) → autoscaled min 5:
            # svc/util (8) becomes its own domain and the three direct svc/*.go
            # files are too few for a remainder, so svc/main.go is unclustered.
            self.assertEqual(scheme, "d18-v1:cap=100,min=5")
            self.assertEqual(ids, ["d18:svc/api", "d18:svc/util"])
            self.assertEqual(len(list(cache.glob("d18-*.json"))), 1)

            with mock.patch.object(decisions, "git", return_value=None):
                again = decisions.code_clusters(anchors, str(repo), sha, cache)
            self.assertEqual(again, (ids, scheme), "second lookup is served from cache")

    def test_code_clusters_without_repo_or_paths(self):
        cache = Path(tempfile.mkdtemp())
        self.assertEqual(decisions.code_clusters([], "/r", "abc", cache), ([], None))
        self.assertEqual(
            decisions.code_clusters([{"kind": "file", "identifier": "a", "path": "a"}], None, None, cache),
            ([], None),
        )


class ResolvePrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.calls = self.dir / "calls"
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.cache = self.dir / "cache"

    def tearDown(self):
        self.tmp.cleanup()

    def _stub_gh(self, body: str, rc: int = 0) -> None:
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\necho call >> {self.calls}\ncat <<'EOF'\n{body}\nEOF\nexit {rc}\n")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    def _resolve(self, branch: str, now: float):
        with mock.patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin:/bin"}):
            return decisions.resolve_pr(str(self.dir), "acme/widgets", branch, self.cache, now=now)

    def _calls(self) -> int:
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_resolves_and_caches(self):
        self._stub_gh('{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}')
        self.assertEqual(self._resolve("feat/x", 1000.0), (42, "https://github.com/acme/widgets/pull/42"))
        self.assertEqual(self._resolve("feat/x", 1100.0), (42, "https://github.com/acme/widgets/pull/42"))
        self.assertEqual(self._calls(), 1)
        self._resolve("feat/x", 1000.0 + decisions.PR_CACHE_TTL_SEC + 1)
        self.assertEqual(self._calls(), 2, "expired entries are re-checked")

    def test_miss_is_rechecked_sooner(self):
        self._stub_gh("no pull requests found", rc=1)
        self.assertEqual(self._resolve("feat/x", 1000.0), (None, None))
        self._resolve("feat/x", 1000.0 + decisions.PR_NEGATIVE_TTL_SEC + 1)
        self.assertEqual(self._calls(), 2)

    def test_protected_branches_skip_gh(self):
        self._stub_gh('{"number": 1}')
        self.assertEqual(self._resolve("main", 1000.0), (None, None))
        self.assertEqual(self._calls(), 0)


class LedgerAndSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_off_by_default_and_override(self):
        self.assertFalse(decisions.is_enabled(self.runtime))
        decisions.set_enabled(self.runtime, True)
        self.assertTrue(decisions.is_enabled(self.runtime))
        self.assertFalse(decisions.is_enabled(self.runtime, "0"))
        decisions.set_enabled(self.runtime, False)
        self.assertTrue(decisions.is_enabled(self.runtime, "on"))
        self.assertFalse(decisions.is_enabled(self.runtime, "maybe"), "unknown values defer to the switch")

    def test_ledger_marks_superseded(self):
        first = decisions.build_decision(choice="Exact trace directory", decision_id="dir")
        decisions.record_in_ledger(self.runtime, "sess/1", first)
        second = decisions.build_decision(choice="Mutation-run S3 LSM", decision_id="lsm", supersedes=["dir"])
        entries = decisions.record_in_ledger(self.runtime, "sess/1", second)
        self.assertEqual([e["id"] for e in entries], ["dir", "lsm"])
        self.assertEqual(entries[0]["superseded_by"], "lsm")
        self.assertEqual(decisions.read_ledger(self.runtime, "sess/1"), entries)
        rendered = decisions.render_ledger(entries)
        self.assertIn("- lsm: Mutation-run S3 LSM", rendered)
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", rendered)
        self.assertEqual(decisions.render_ledger([]), "(none yet)")


class AttributesTest(unittest.TestCase):
    def test_json_encodes_lists_and_drops_empties(self):
        d = decisions.build_decision(
            choice="Use X", question="Which?", alternatives=["Y"], refines=["a"],
            anchors=[{"kind": "file", "identifier": "a.py", "path": "a.py"}],
        )
        attrs = decisions.decision_attributes(
            session_id="s1", decision=d, code_clusters=["d18:pkg"], cluster_scheme="d18-v1:cap=100,min=10",
            repo="acme/widgets", branch="feat/x", head_sha="abc", pr_number=7,
        )
        self.assertEqual(json.loads(attrs["cardinal.decision.alternatives"]), ["Y"])
        self.assertEqual(json.loads(attrs["cardinal.decision.links"]), [{"relation": "refines", "to": "a"}])
        self.assertEqual(json.loads(attrs["cardinal.decision.code_clusters"]), ["d18:pkg"])

        record = log_record(decisions.DECISION_EVENT, attrs, 1)
        keys = {kv["key"] for kv in record["attributes"]}
        self.assertIn("cardinal.pr_number", keys)
        self.assertNotIn("cardinal.pr_url", keys, "None values are dropped")
        self.assertNotIn("cardinal.decision.rationale", keys)

    def test_scheme_only_with_clusters(self):
        d = decisions.build_decision(choice="Use X")
        attrs = decisions.decision_attributes(session_id="s1", decision=d, cluster_scheme="d18-v1:cap=100,min=10")
        self.assertIsNone(attrs["cardinal.decision.cluster_scheme"])
        self.assertIsNone(attrs["cardinal.decision.alternatives"])


if __name__ == "__main__":
    unittest.main()
