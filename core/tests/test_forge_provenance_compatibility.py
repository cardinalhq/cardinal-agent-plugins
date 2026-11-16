"""Local forge identity and compatible session delivery without a live server."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cardinal_core import investigation_bootstrap as boot, investigation_events as ie
from cardinal_core import session_repos as repos, storyboard_context as sc
from cardinal_core.evidence_normalizers import ToolCall


class ForgeProvenanceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.sid = "forge-provenance"
        self.inv = "inv_" + "a" * 24
        self.conn = {"origin": "https://cardinal.example.test", "org": "o1", "key": "fixture"}
        ie.write_binding(self.home, self.sid, {
            "investigation_id": self.inv, "cursor": 0, "org": "o1", "is_author": True})
        self.contexts = [{"repo": "acme/widget", "repo_host": host,
                          "repo_path": ".", "client": "codex/1", "workdir_hash": "a" * 32}
                         for host in ("one.example.test", "two.example.test")]

    def test_ordinary_observations_keep_both_forges_and_deduplicate(self):
        def git(args, cwd):
            return {("rev-parse", "--is-inside-work-tree"): "true",
                    ("remote", "get-url", "origin"): f"git@{Path(cwd).name}:Acme/Widget.git",
                    ("rev-parse", "--abbrev-ref", "HEAD"): "main",
                    ("rev-parse", "HEAD"): "b" * 40}.get(tuple(args))
        with patch.object(sc, "git", side_effect=git):
            for context in self.contexts * 2:
                cwd = str(self.home / context["repo_host"])
                call = ToolCall(runtime="codex", tool_name="exec_command", tool="exec_command",
                                source={}, session_id=self.sid, cwd=cwd,
                                tool_input={"workdir": cwd}, client="codex/1")
                repos.observe(call, self.home, env={})
        local = repos.pending(self.home, self.sid, "o1")
        self.assertEqual([(c["repo"], c.get("repo_host")) for c in local],
                         [(c["repo"], c["repo_host"]) for c in self.contexts])

    def test_refresh_delivers_all_contexts_for_modern_legacy_and_rollback(self):
        for context in self.contexts * 2:
            repos.stage(self.home, self.sid, context, runtime="codex", env={})
        local = repos.pending(self.home, self.sid, "o1")
        self.assertEqual(len(local), 2)
        for version, strict in ((2, False), (1, True), (2, True)):
            with self.subTest(version=version, strict=strict):
                sent, accepted = [], []

                def post(conn, tool, body, **kwargs):
                    sent.append(body)
                    context = body.get("context", {})
                    if strict and "repo_host" in context:
                        raise boot.sync.ServerError(400, {"error": "invalid_body", "issues": [
                            {"path": ["context"], "message": 'unknown key "repo_host"'}]}, "fixture refusal")
                    if context:
                        accepted.append(context)
                    return {"investigation_id": self.inv, "is_author": True}

                boot.refresh_path(self.home, self.sid).touch()
                with patch("cardinal_core.storyboard_discovery.read_caps_any", return_value=version), \
                        patch.object(boot.sync, "_post", side_effect=post):
                    self.assertEqual(boot.refresh_capabilities(
                        self.home, self.sid, self.conn, "codex/1"), "ok")
                omitted = {"workdir_hash"} | ({"repo_host"} if strict else set())
                self.assertEqual(accepted, [{k: v for k, v in c.items() if k not in omitted}
                                            for c in self.contexts])
                self.assertTrue(all("workdir_hash" not in b.get("context", {}) for b in sent))
                self.assertEqual(repos.pending(self.home, self.sid, "o1"), local)

    def test_workstation_hash_is_not_sent_even_without_forge_identity(self):
        context = {"repo": "acme/widget", "workdir_hash": "a" * 32}
        self.assertEqual(boot.sendable_context(self.home, self.conn, context), {"repo": "acme/widget"})
        self.assertIn("workdir_hash", context)
