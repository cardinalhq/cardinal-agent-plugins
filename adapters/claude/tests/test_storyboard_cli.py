"""bin/cardinal-storyboard context: the launcher around
cardinal_core.storyboard_context.

Runs the CLI as a subprocess with HOME pointed at a temp dir and a stub `gh`
first on PATH (no network), inside a temp git repo.
Requires cardinal_core vendored: python3 build/vendor.py claude
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CLI = PLUGIN_ROOT / "bin" / "cardinal-storyboard"
VENDORED = PLUGIN_ROOT / "hooks" / "cardinal_core" / "storyboard_context.py"
PLUGIN_VERSION = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false",
                    *args], cwd=repo, check=True, capture_output=True)


class ContextCliTests(unittest.TestCase):
    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))
        self.home, self.repo, stub = base / "home", base / "work", base / "stub"
        for d in (self.home / ".claude", self.repo / "svc", stub):
            d.mkdir(parents=True)
        gh = stub / "gh"
        gh.write_text('#!/bin/sh\necho \'{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}\'\n')
        gh.chmod(0o755)
        self.path = f"{stub}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}"
        _git(self.repo, "init", "-q")
        _git(self.repo, "checkout", "-q", "-b", "fix/checkout")
        _git(self.repo, "remote", "add", "origin", "https://github.com/Acme/Widgets.git")
        (self.repo / "svc" / "x.txt").write_text("x\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, cwd=None):
        env = {"HOME": str(self.home), "PATH": self.path}
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60,
                              env=env, cwd=str(cwd or self.repo))

    def context(self, *args, cwd=None) -> dict:
        res = self.run_cli("context", *args, cwd=cwd)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.count("\n"), 1, "one line")
        body = json.loads(res.stdout)
        self.assertEqual(list(body), ["context"])
        return body["context"]

    def test_prints_the_context_for_the_current_directory(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "OTEL_RESOURCE_ATTRIBUTES": "user.email=Dev@Example.com,cardinal.org=o1"}}))
        ctx = self.context(cwd=self.repo / "svc")
        self.assertEqual(ctx["repo"], "acme/widgets")
        self.assertEqual(ctx["repo_path"], "svc")
        self.assertEqual(ctx["branch"], "fix/checkout")
        self.assertEqual((ctx["pr_number"], ctx["pr_url"]), (42, "https://github.com/acme/widgets/pull/42"))
        self.assertEqual(ctx["client"], f"claude-code/{PLUGIN_VERSION}")
        self.assertEqual(ctx["actor_email"], "dev@example.com")
        self.assertRegex(ctx["workdir_hash"], r"^[0-9a-f]{32}$")
        self.assertRegex(ctx["head_sha"], r"^[0-9a-f]{40}$")

    def test_paths_are_the_files_this_session_edited(self):
        files = self.home / ".claude" / "cardinal" / "storyboard-files"
        files.mkdir(parents=True)
        (files / "sess-1.json").write_text(json.dumps({"files": [
            {"repo": "acme/widgets", "path": "svc/x.txt"}, {"repo": "other/repo", "path": "y.ts"}]}))
        self.assertNotIn("paths", self.context())
        self.assertEqual(self.context("--session-id", "sess-1")["paths"], ["svc/x.txt"])
        self.assertNotIn("paths", self.context("--session-id", "sess-2"))

    def test_protected_branch_is_not_sent(self):
        _git(self.repo, "checkout", "-q", "-b", "main")
        ctx = self.context()
        self.assertNotIn("branch", ctx)
        self.assertNotIn("pr_number", ctx)

    def test_output_never_contains_an_absolute_path(self):
        res = self.run_cli("context", "--cwd", str(self.repo / "svc"), cwd=self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(self.tmp.name, res.stdout)
        self.assertNotIn(os.path.realpath(self.tmp.name), res.stdout)
        for value in json.loads(res.stdout)["context"].values():
            self.assertFalse(str(value).startswith("/"), value)

    def test_actor_email_never_comes_from_git_config(self):
        _git(self.repo, "config", "user.email", "git-only@example.com")
        self.assertNotIn("actor_email", self.context())

    def test_outside_git_and_on_a_bad_cwd_it_still_exits_zero(self):
        ctx = self.context(cwd=self.home)
        self.assertLessEqual(set(ctx), {"workdir_hash", "client", "actor_email"})
        ctx = self.context("--cwd", str(self.home / "missing"), cwd=self.home)
        self.assertLessEqual(set(ctx), {"workdir_hash", "client", "actor_email"})

    def test_discover_json_exits_zero_when_the_network_fails(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": "http://127.0.0.1:9/api/orgs/o1/mcp", "CARDINAL_MCP_API_KEY": "ck"}}))
        res = self.run_cli("discover", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), {"block": None})
        res = self.run_cli("discover")
        self.assertEqual((res.returncode, res.stdout), (0, ""))

    def test_discover_json_unconnected_prints_null(self):
        res = self.run_cli("discover", "--json", "--cwd", str(self.repo / "svc"), cwd=self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), {"block": None})

    def test_discover_prints_the_block(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        sid = "sb_0123456789abcdef01234567"
        answers = {
            "find": {"matches": [{"storyboard_id": sid, "question": "Q?", "status": "published", "act_count": 1,
                                  "match": "branch", "context": {"repo": "acme/widgets", "branch": "fix/checkout"}}]},
            "get": {"storyboard_id": sid, "scenes": [{"act": 1, "act_status": "published", "state": "open",
                                                      "title": "T", "statement": "S"}]},
        }

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("content-length") or 0))
                data = json.dumps(answers[self.path.rsplit("/", 1)[-1]]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": f"http://127.0.0.1:{server.server_port}/api/orgs/o1/mcp",
            "CARDINAL_MCP_API_KEY": "ck"}}))
        res = self.run_cli("discover", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        block = json.loads(res.stdout)["block"]
        self.assertIn(f"{sid} · written from branch fix/checkout (subject not confirmed) · published, 1 act", block)
        self.assertIn("- [open] T: S", block)
        res = self.run_cli("discover")
        self.assertEqual(res.stdout, block + "\n")

    def test_help_and_usage(self):
        res = self.run_cli("--help")
        self.assertEqual(res.returncode, 0)
        self.assertIn("context", res.stdout)
        self.assertIn("discover", res.stdout)
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertTrue(os.access(CLI, os.X_OK))


if __name__ == "__main__":
    unittest.main()


class StateCliTests(unittest.TestCase):
    """state init / check, offline (--from-get): the InvestigationState file."""

    SB = "sb_0123456789abcdef01234567"
    R1 = "rcpt_" + "1" * 24

    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        (self.base / "home").mkdir()
        self.get = self.base / "get.json"
        self.get.write_text(json.dumps({
            "storyboard_id": self.SB, "question": "Why?", "window": {"start": "a", "end": "b"},
            "acts": [{"number": 1, "status": "published", "context": {"repo": "o/r"}}],
            "receipt_tiers": {self.R1: "captured"},
            "scenes": [{"act": 1, "act_status": "published", "id": "s1", "state": "supported", "title": "T",
                        "statement": "S.", "receipt_ids": [self.R1], "claims": [],
                        "open_questions": [{"id": "q", "text": "Open?"}]}],
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = {"HOME": str(self.base / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60, env=env)

    def test_init_writes_the_default_path_and_prints_the_guide(self):
        res = self.run_cli("state", "init", self.SB, "--from-get", str(self.get))
        self.assertEqual(res.returncode, 0, res.stderr)
        path = self.base / "home" / ".claude" / "cardinal" / "investigation-state" / f"{self.SB}.json"
        self.assertEqual(res.stdout.splitlines()[0], str(path))
        self.assertIn("Re-type, under the same id", res.stdout)
        state = json.loads(path.read_text())
        self.assertEqual(state["schema"], "investigation-state/v1.1")
        self.assertEqual(state["open_questions"][0]["id"], "s1/q")

    def test_init_refuses_to_overwrite_and_refresh_keeps_authored(self):
        out = self.base / "st.json"
        self.assertEqual(self.run_cli("state", "init", self.SB, "--out", str(out), "--from-get", str(self.get)).returncode, 0)
        state = json.loads(out.read_text())
        state["constraints"] = [{"id": "c1", "statement": "Keep it exact.", "authored_by": "unknown", "authority": "unknown"}]
        out.write_text(json.dumps(state))
        again = self.run_cli("state", "init", self.SB, "--out", str(out), "--from-get", str(self.get))
        self.assertEqual(again.returncode, 1)
        self.assertIn("--refresh", again.stderr)
        res = self.run_cli("state", "init", self.SB, "--out", str(out), "--refresh", "--from-get", str(self.get))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(out.read_text())["constraints"][0]["id"], "c1")

    def test_check_reports_errors_and_exits_one(self):
        out = self.base / "st.json"
        self.run_cli("state", "init", self.SB, "--out", str(out), "--from-get", str(self.get))
        ok = self.run_cli("state", "check", str(out), "--from-get", str(self.get))
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertTrue(ok.stdout.splitlines()[-1].startswith("ok: 1 findings"))
        self.assertIn("warning: 1 receipt(s) have no `what`", ok.stdout)
        state = json.loads(out.read_text())
        state["open_questions"] = []
        out.write_text(json.dumps(state))
        bad = self.run_cli("state", "check", str(out), "--from-get", str(self.get))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("error: open question s1/q was dropped", bad.stdout)

    def test_init_without_a_connection_fails_cleanly(self):
        res = self.run_cli("state", "init", self.SB)
        self.assertEqual(res.returncode, 1)
        self.assertIn("not connected", res.stderr)

    def test_check_verifies_owner_quotes_against_the_acts_transcript(self):
        sid = "11111111-2222-3333-4444-555555555555"
        got = json.loads(self.get.read_text())
        got["acts"][0]["session_id"] = sid
        self.get.write_text(json.dumps(got))
        proj = self.base / "home" / ".claude" / "projects" / "-repo"
        proj.mkdir(parents=True)
        (proj / f"{sid}.jsonl").write_text(json.dumps({
            "type": "user", "timestamp": "2026-10-01T01:00:00.000Z",
            "message": {"role": "user", "content": "Keep every query exact."}}) + "\n")
        out = self.base / "st.json"
        self.run_cli("state", "init", self.SB, "--out", str(out), "--from-get", str(self.get))
        state = json.loads(out.read_text())
        state["actors"] = [{"id": "owner", "role": "owner"}]
        quote = {"kind": "message", "from": "owner", "at": "2026-10-01T01:00:00.000Z", "quote": "Keep every query exact."}
        state["constraints"] = [{"id": "c1", "statement": "Queries stay exact.", "authored_by": "owner",
                                 "authority": "owner_stated", "source": [quote]}]
        out.write_text(json.dumps(state))
        ok = self.run_cli("state", "check", str(out), "--from-get", str(self.get))
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertIn("1 owner / 0 agent quotes verified", ok.stdout)
        quote["quote"] = "Keep every query exact, forever."
        out.write_text(json.dumps(state))
        bad = self.run_cli("state", "check", str(out), "--from-get", str(self.get))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("quote not verified", bad.stdout)

    def test_downgrade_keeps_content_and_removal_is_refused(self):
        out = self.base / "st.json"
        self.run_cli("state", "init", self.SB, "--out", str(out), "--from-get", str(self.get))
        state = json.loads(out.read_text())
        state["actors"] = [{"id": "owner", "role": "owner"}, {"id": "agent", "role": "agent"}]
        state["constraints"] = [{"id": "c1", "statement": "Only the owner merges.", "authored_by": "agent",
                                 "authority": "owner_ratified", "source": [{"kind": "doc", "path": "d10.md"}]}]
        out.write_text(json.dumps(state))
        bad = self.run_cli("state", "check", str(out), "--from-get", str(self.get))
        self.assertEqual(bad.returncode, 1)
        res = self.run_cli("state", "check", str(out), "--from-get", str(self.get), "--downgrade")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("downgraded: constraint c1: owner_ratified -> agent_interpretation", res.stdout)
        kept = json.loads(out.read_text())["constraints"][0]
        self.assertEqual((kept["statement"], kept["authority"]), ("Only the owner merges.", "agent_interpretation"))
        state = json.loads(out.read_text())
        state["constraints"] = []
        out.write_text(json.dumps(state))
        gone = self.run_cli("state", "check", str(out), "--from-get", str(self.get))
        self.assertEqual(gone.returncode, 1)
        self.assertIn("c1 was removed", gone.stdout)
        allowed = self.run_cli("state", "check", str(out), "--from-get", str(self.get), "--allow-remove", "c1")
        self.assertEqual(allowed.returncode, 0, allowed.stdout)


class StateServerCliTests(unittest.TestCase):
    """state publish / pull against a fake maestro: the server copy is
    canonical after a publish (version-based), quotes go up attested."""

    SB = "sb_0123456789abcdef01234567"
    R1 = "rcpt_" + "1" * 24
    SID = "11111111-2222-3333-4444-555555555555"
    QUOTE = {"kind": "message", "from": "owner", "at": "2026-10-01T01:00:00.000Z", "quote": "Keep every query exact."}

    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.tmp = TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        home = self.base / "home"
        proj = home / ".claude" / "projects" / "-repo"
        proj.mkdir(parents=True)
        (proj / f"{self.SID}.jsonl").write_text(json.dumps({
            "type": "user", "timestamp": self.QUOTE["at"],
            "message": {"role": "user", "content": "Keep every query exact."}}) + "\n")
        get = {
            "storyboard_id": self.SB, "question": "Why?", "window": {"start": "a", "end": "b"},
            "acts": [{"number": 1, "status": "published", "context": {"repo": "o/r"}, "session_id": self.SID}],
            "receipt_tiers": {self.R1: "captured"},
            "scenes": [{"act": 1, "act_status": "published", "id": "s1", "state": "supported", "title": "T",
                        "statement": "S.", "receipt_ids": [self.R1], "claims": [], "open_questions": []}],
        }
        server_copy = self.server_copy = {}
        seen = self.seen = []
        mode = self.mode = {"old_server": False}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                tool = self.path.rsplit("/", 1)[-1]
                seen.append((tool, body))
                status, out = 200, None
                if tool == "get":
                    out = get
                elif mode["old_server"]:
                    # A Maestro before the state routes: the plugin key's allowlist refuses them.
                    status, out = 403, {"error": "insufficient_scope"}
                elif tool == "get-state":
                    out = (dict(server_copy, storyboard_id=body["storyboard_id"], current=True,
                                trust={"client_attested_authority": []}) if server_copy
                           else None)
                    if out is None:
                        status, out = 404, {"error": "state_not_found"}
                elif tool == "put-state":
                    if body["base_version"] != server_copy.get("version", 0):
                        status, out = 409, {"error": "version_conflict", "current_version": server_copy.get("version", 0)}
                    else:
                        server_copy.update(version=server_copy.get("version", 0) + 1, etag="e" * 64, state=body["state"])
                        out = {"storyboard_id": body["storyboard_id"], "version": server_copy["version"], "etag": "e" * 64,
                               "warnings": [], "trust": {"client_attested_authority": [{"where": "constraint c1", "claim": "x"}]}}
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": f"http://127.0.0.1:{server.server_port}/api/orgs/o1/mcp", "CARDINAL_MCP_API_KEY": "ck"}}))
        self.path = self.base / "st.json"
        self.assertEqual(self.run_cli("state", "init", self.SB, "--out", str(self.path)).returncode, 0)
        state = json.loads(self.path.read_text())
        state["actors"] = [{"id": "owner", "role": "owner"}]
        state["constraints"] = [{"id": "c1", "statement": "Queries stay exact.", "authored_by": "owner",
                                 "authority": "owner_stated", "source": [self.QUOTE]}]
        self.path.write_text(json.dumps(state))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = {"HOME": str(self.base / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60, env=env)

    def test_publish_attests_quotes_and_records_the_server_version(self):
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("published: version 1", res.stdout)
        self.assertIn("server copy is now canonical", res.stdout)
        put = [b for t, b in self.seen if t == "put-state"][-1]
        self.assertEqual(put["base_version"], 0)
        self.assertEqual(put["attestation"]["quotes"][0]["role"], "owner")
        self.assertEqual(put["attestation"]["quotes"][0]["session"], self.SID)
        meta = json.loads(Path(str(self.path) + ".server").read_text())
        self.assertEqual((meta["storyboard_id"], meta["version"]), (self.SB, 1))
        # The next publish is based on the server version.
        state = json.loads(self.path.read_text())
        state["status"] = "blocked"
        self.path.write_text(json.dumps(state))
        self.assertEqual(self.run_cli("state", "publish", str(self.path)).returncode, 0)
        self.assertEqual([b for t, b in self.seen if t == "put-state"][-1]["base_version"], 1)

    def test_a_publish_based_on_a_stale_copy_is_refused_and_pull_restores_the_server_copy(self):
        self.assertEqual(self.run_cli("state", "publish", str(self.path)).returncode, 0)
        other = self.base / "other.json"
        other.write_text(self.path.read_text())  # a copy that never saw version 1
        res = self.run_cli("state", "publish", str(other))
        self.assertEqual(res.returncode, 1)
        self.assertIn("the server copy is at version 1 and this file is based on version 0", res.stdout)
        # Local edits are not silently replaced by a pull…
        state = json.loads(other.read_text())
        state["status"] = "blocked"
        other.write_text(json.dumps(state))
        refused = self.run_cli("state", "pull", self.SB, "--out", str(other))
        self.assertEqual(refused.returncode, 1)
        self.assertIn("differs from the server copy", refused.stderr)
        # …but --force (or a fresh path) takes the canonical copy.
        ok = self.run_cli("state", "pull", self.SB, "--out", str(other), "--force")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(other.read_text())["status"], json.loads(self.path.read_text())["status"])
        self.assertEqual(json.loads(Path(str(other) + ".server").read_text())["version"], 1)

    def test_nothing_that_fails_locally_is_sent(self):
        state = json.loads(self.path.read_text())
        state["constraints"][0]["source"][0]["quote"] = "Keep every query exact, forever."
        self.path.write_text(json.dumps(state))
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 1)
        self.assertIn("not published", res.stdout)
        self.assertFalse([t for t, _ in self.seen if t == "put-state"])

    def test_an_unknown_field_is_refused_by_check_and_never_sent_by_publish(self):
        ok = self.run_cli("state", "check", str(self.path))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        for where, field in (((), "summary"), (("source",), "investigation_id")):
            state = json.loads(self.path.read_text())
            x = state
            for k in where:
                x = x[k]
            x[field] = "x"
            self.path.write_text(json.dumps(state))
            bad = self.run_cli("state", "check", str(self.path))
            self.assertEqual(bad.returncode, 1, bad.stdout)
            self.assertIn(f"unknown fields ['{field}']", bad.stdout)
            res = self.run_cli("state", "publish", str(self.path))
            self.assertEqual(res.returncode, 1)
            self.assertIn("not published", res.stdout)
            self.assertIn(f"unknown fields ['{field}']", res.stdout)
            self.assertFalse([t for t, _ in self.seen if t == "put-state"])
            del x[field]
            self.path.write_text(json.dumps(state))

    def test_a_server_without_the_state_routes_says_so_and_leaves_the_file_alone(self):
        self.mode["old_server"] = True
        before = self.path.read_text()
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 1)
        self.assertIn("does not store InvestigationState yet", res.stdout)
        self.assertNotIn("insufficient_scope", res.stdout)
        self.assertEqual(self.path.read_text(), before)
        self.assertFalse(Path(str(self.path) + ".server").exists())
        pulled = self.run_cli("state", "pull", self.SB, "--out", str(self.base / "p.json"))
        self.assertEqual(pulled.returncode, 1)
        self.assertIn("does not store InvestigationState yet", pulled.stderr)
        self.assertFalse((self.base / "p.json").exists())


class InvestigationCliTests(unittest.TestCase):
    """investigation create / attach / show and an investigation-bound state
    (state init / check / publish / pull --investigation) against a fake
    maestro; a storyboard-bound state stays exactly plugin 0.40.0's."""

    SB = "sb_0123456789abcdef01234567"
    INV = "inv_" + "a" * 24
    R1 = "rcpt_" + "1" * 24
    SID = "11111111-2222-3333-4444-555555555555"
    QUOTE = {"kind": "message", "from": "owner", "at": "2026-10-01T01:00:00.000Z", "quote": "Keep every query exact."}
    WINDOW = {"start": "2026-10-01T00:00:00.000Z", "end": "2026-10-01T01:00:00.000Z"}

    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.tmp = TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        home = self.base / "home"
        proj = home / ".claude" / "projects" / "-repo"
        proj.mkdir(parents=True)
        # This session's transcript: before any storyboard, the oracle for owner quotes.
        (proj / f"{self.SID}.jsonl").write_text(json.dumps({
            "type": "user", "timestamp": self.QUOTE["at"],
            "message": {"role": "user", "content": "Keep every query exact."}}) + "\n")
        get = {
            "storyboard_id": self.SB, "question": "Why?", "window": self.WINDOW,
            "acts": [{"number": 1, "status": "published", "context": {"repo": "o/r"}, "session_id": self.SID}],
            "receipt_tiers": {self.R1: "captured"},
            "scenes": [{"act": 1, "act_status": "published", "id": "s1", "state": "supported", "title": "T",
                        "statement": "S.", "receipt_ids": [self.R1], "claims": [],
                        "open_questions": [{"id": "q", "text": "Open?"}]}],
        }
        invs = self.invs = {}
        sb_states = self.sb_states = {}
        seen = self.seen = []
        mode = self.mode = {"server": "new"}
        inv_id = self.INV

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                tool = self.path.rsplit("/", 1)[-1]
                seen.append((tool, body))
                status, out = self.answer(tool, body)
                data = json.dumps(out).encode() if out is not None else b"<html>Cannot POST</html>"
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def answer(self, tool, body):
                if tool == "get":
                    return 200, get
                if mode["server"] == "pre-state":
                    # A Maestro before the state routes: the plugin key's allowlist refuses them.
                    return 403, {"error": "insufficient_scope"}
                if tool in ("create-investigation", "get-investigation", "attach-storyboard") and mode["server"] == "state-only":
                    return 404, None  # maestro v1.99.11: no such route
                if "investigation_id" in body and mode["server"] == "state-only":
                    return 400, {"error": "invalid_body", "issues": [{"message": "Unrecognized key(s) in object: 'investigation_id'"}]}
                if tool == "create-investigation":
                    invs[inv_id] = {"question": body["question"], "window": body.get("window"), "storyboard_id": None, "state": None}
                    return 200, {"investigation_id": inv_id, "author": {"kind": "user", "id": "u1"}, "created_at": "t"}
                if "investigation_id" in body and body["investigation_id"] not in invs:
                    return 404, {"error": "investigation_not_found"}
                inv = invs.get(body.get("investigation_id"))
                if tool == "get-investigation":
                    st = inv["state"]
                    return 200, dict({k: inv[k] for k in ("question", "window", "storyboard_id")}, investigation_id=inv_id,
                                     state=st and {"version": st["version"], "etag": st["etag"], "current": True})
                if tool == "attach-storyboard":
                    if mode["server"] == "not-author":
                        return 403, {"error": "not_investigation_author"}
                    inv["storyboard_id"] = body["storyboard_id"]
                    return 200, {"investigation_id": inv_id, "storyboard_id": body["storyboard_id"]}
                if tool == "put-state":
                    if mode["server"] == "not-author":
                        return 403, {"error": "not_investigation_author"}
                    copy = inv["state"] if inv else sb_states.get(body["storyboard_id"])
                    if body["base_version"] != (copy or {}).get("version", 0):
                        return 409, {"error": "version_conflict", "current_version": (copy or {}).get("version", 0)}
                    new = {"version": (copy or {}).get("version", 0) + 1, "etag": "e" * 64, "state": body["state"]}
                    if inv:
                        inv["state"] = new
                        return 200, {"investigation_id": inv_id, "storyboard_id": inv["storyboard_id"], "version": new["version"],
                                     "etag": new["etag"], "warnings": [], "trust": {"client_attested_authority": []}}
                    sb_states[body["storyboard_id"]] = new
                    return 200, {"storyboard_id": body["storyboard_id"], "version": new["version"], "etag": new["etag"],
                                 "warnings": [], "trust": {"client_attested_authority": []}}
                if tool == "get-state":
                    copy = inv["state"] if inv else sb_states.get(body["storyboard_id"])
                    if not copy:
                        return 404, {"error": "state_not_found"}
                    ids = {"investigation_id": inv_id, "storyboard_id": inv["storyboard_id"]} if inv else {"storyboard_id": body["storyboard_id"]}
                    return 200, dict(copy, **ids, current=True, trust={"client_attested_authority": []})
                return 404, None

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": f"http://127.0.0.1:{server.server_port}/api/orgs/o1/mcp", "CARDINAL_MCP_API_KEY": "ck"}}))
        self.path = self.base / "st.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = {"HOME": str(self.base / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "CLAUDE_CODE_SESSION_ID": self.SID}
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60, env=env)

    def meta(self, path=None) -> dict:
        return json.loads(Path(str(path or self.path) + ".server").read_text())

    def puts(self) -> list:
        return [b for t, b in self.seen if t == "put-state"]

    def create_and_init(self):
        res = self.run_cli("investigation", "create", "--question", "Why?", "--window", json.dumps(self.WINDOW))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, f"{self.INV}\n")
        res = self.run_cli("state", "init", "--investigation", self.INV, "--out", str(self.path))
        self.assertEqual(res.returncode, 0, res.stderr)
        state = json.loads(self.path.read_text())
        state["actors"] = [{"id": "owner", "role": "owner"}]
        state["hypotheses"] = [{"id": "h-gc", "statement": "GC pauses add latency.", "status": "untested"}]
        state["constraints"] = [{"id": "c1", "statement": "Queries stay exact.", "authored_by": "owner",
                                 "authority": "owner_stated", "source": [self.QUOTE]}]
        self.path.write_text(json.dumps(state))
        return state

    def test_create_init_check_publish_before_any_storyboard(self):
        self.create_and_init()
        self.assertEqual(self.seen[0], ("create-investigation", {"question": "Why?", "window": self.WINDOW}))
        state = json.loads(self.path.read_text())
        self.assertEqual(state["source"], {"storyboard_id": None, "acts": []})
        self.assertEqual((state["question"], state["window"], state["status"]), ("Why?", self.WINDOW, "open"))
        self.assertNotIn("investigation_id", state)
        meta = self.meta()
        self.assertEqual((meta["investigation_id"], meta["storyboard_id"], meta["version"]), (self.INV, None, 0))
        ok = self.run_cli("state", "check", str(self.path))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("1 owner / 0 agent quotes verified", ok.stdout)  # against this session's transcript
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("published: version 1", res.stdout)
        put = self.puts()[-1]
        self.assertEqual((put["investigation_id"], put["base_version"]), (self.INV, 0))
        self.assertNotIn("storyboard_id", put)
        self.assertEqual(put["attestation"]["quotes"][0]["session"], self.SID)
        self.assertEqual((self.meta()["investigation_id"], self.meta()["version"]), (self.INV, 1))
        state = json.loads(self.path.read_text())
        state["status"] = "blocked"
        self.path.write_text(json.dumps(state))
        self.assertEqual(self.run_cli("state", "publish", str(self.path)).returncode, 0)
        self.assertEqual(self.puts()[-1]["base_version"], 1)
        shown = self.run_cli("investigation", "show", self.INV)
        self.assertEqual(json.loads(shown.stdout)["state"]["version"], 2)

    def test_nothing_that_fails_locally_is_sent(self):
        self.create_and_init()
        state = json.loads(self.path.read_text())
        state["constraints"][0]["source"] = [{"kind": "receipt", "id": self.R1}]
        self.path.write_text(json.dumps(state))
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 1)
        self.assertIn("is not a receipt this storyboard cites", res.stdout)
        self.assertEqual(self.puts(), [])

    def test_pull_by_investigation_and_a_stale_copy_is_refused(self):
        self.create_and_init()
        other = self.base / "other.json"
        other.write_text(self.path.read_text())
        Path(str(other) + ".server").write_text(Path(str(self.path) + ".server").read_text())
        self.assertEqual(self.run_cli("state", "publish", str(self.path)).returncode, 0)
        res = self.run_cli("state", "publish", str(other))
        self.assertEqual(res.returncode, 1)
        self.assertIn(f"`cardinal-storyboard state pull --investigation {self.INV}`", res.stdout)
        pulled = self.base / "pulled.json"
        ok = self.run_cli("state", "pull", "--investigation", self.INV, "--out", str(pulled))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("version 1; current", ok.stdout)
        self.assertEqual(json.loads(pulled.read_text()), json.loads(self.path.read_text()))
        self.assertEqual(self.meta(pulled), {"origin": self.meta()["origin"], "org": "o1", "investigation_id": self.INV,
                                             "storyboard_id": None, "version": 1, "etag": "e" * 64})
        # The pulled copy is investigation-bound: it publishes on top of version 1.
        self.assertEqual(self.run_cli("state", "publish", str(pulled)).returncode, 0)
        self.assertEqual((self.puts()[-1]["investigation_id"], self.puts()[-1]["base_version"]), (self.INV, 1))

    def test_pull_without_a_published_state_says_so(self):
        self.run_cli("investigation", "create", "--question", "Why?")
        res = self.run_cli("state", "pull", "--investigation", self.INV, "--out", str(self.path))
        self.assertEqual(res.returncode, 1)
        self.assertIn("has no published InvestigationState yet", res.stderr)
        self.assertFalse(self.path.exists())

    def test_attach_then_refresh_reprojects_from_the_storyboard_and_keeps_the_authored_sections(self):
        authored = self.create_and_init()
        self.assertEqual(self.run_cli("state", "publish", str(self.path)).returncode, 0)
        res = self.run_cli("investigation", "attach", self.INV, self.SB)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"state init --investigation {self.INV} --refresh", res.stdout)
        self.assertEqual(self.seen[-1], ("attach-storyboard", {"investigation_id": self.INV, "storyboard_id": self.SB}))
        stale = self.run_cli("state", "check", str(self.path))
        self.assertEqual(stale.returncode, 1)
        self.assertIn("a storyboard is attached to the investigation now", stale.stdout)
        res = self.run_cli("state", "init", "--investigation", self.INV, "--out", str(self.path), "--refresh")
        self.assertEqual(res.returncode, 0, res.stderr)
        state = json.loads(self.path.read_text())
        self.assertEqual(state["source"], {"storyboard_id": self.SB, "acts": [1]})
        self.assertEqual([f["id"] for f in state["findings"]], ["s1"])
        for k in ("actors", "constraints"):
            self.assertEqual(state[k], authored[k])
        self.assertEqual([h["id"] for h in state["hypotheses"]], ["h-gc"])
        self.assertEqual([q["id"] for q in state["open_questions"]], ["s1/q"])
        self.assertEqual((self.meta()["investigation_id"], self.meta()["storyboard_id"], self.meta()["version"]),
                         (self.INV, self.SB, 1))
        ok = self.run_cli("state", "check", str(self.path))
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual((self.puts()[-1]["investigation_id"], self.puts()[-1]["base_version"]), (self.INV, 1))
        self.assertEqual(self.invs[self.INV]["state"]["state"]["source"]["storyboard_id"], self.SB)

    def test_refresh_never_moves_a_state_off_its_storyboard(self):
        self.run_cli("investigation", "create", "--question", "Why?")
        self.assertEqual(self.run_cli("state", "init", self.SB, "--out", str(self.path)).returncode, 0)
        res = self.run_cli("state", "init", "--investigation", self.INV, "--out", str(self.path), "--refresh")
        self.assertEqual(res.returncode, 1)
        self.assertIn(f"reflects storyboard {self.SB}", res.stderr)

    def test_a_storyboard_bound_state_is_published_exactly_as_in_0_40(self):
        self.assertEqual(self.run_cli("state", "init", self.SB, "--out", str(self.path)).returncode, 0)
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(sorted(self.puts()[-1]), ["attestation", "base_version", "state", "storyboard_id"])
        self.assertEqual(self.puts()[-1]["storyboard_id"], self.SB)
        self.assertEqual(sorted(self.meta()), ["etag", "org", "origin", "storyboard_id", "version"])
        self.assertFalse([t for t, b in self.seen if "investigation" in t or "investigation_id" in b])
        pulled = self.base / "p.json"
        self.assertEqual(self.run_cli("state", "pull", self.SB, "--out", str(pulled)).returncode, 0)
        self.assertEqual([b for t, b in self.seen if t == "get-state"][-1], {"storyboard_id": self.SB})
        self.assertNotIn("investigation_id", self.meta(pulled))

    def test_a_state_bound_to_nothing_says_how_to_bind_it(self):
        self.create_and_init()
        Path(str(self.path) + ".server").unlink()
        res = self.run_cli("state", "publish", str(self.path))
        self.assertEqual(res.returncode, 1)
        self.assertIn("belongs to no storyboard and no investigation", res.stderr)

    def test_not_the_author_is_said_plainly(self):
        self.create_and_init()
        self.mode["server"] = "not-author"
        for args, stream in ((("state", "publish", str(self.path)), "stdout"),
                             (("investigation", "attach", self.INV, self.SB), "stderr")):
            res = self.run_cli(*args)
            self.assertEqual(res.returncode, 1)
            self.assertIn("only the investigation's author can do that", getattr(res, stream))
            self.assertNotIn("403", res.stdout + res.stderr)

    def test_an_older_server_says_so_plainly(self):
        self.create_and_init()
        before = self.path.read_text()
        for server in ("pre-state", "state-only"):
            self.mode["server"] = server
            for args in (("investigation", "create", "--question", "Why?"), ("investigation", "show", self.INV),
                         ("investigation", "attach", self.INV, self.SB),
                         ("state", "init", "--investigation", self.INV, "--out", str(self.base / "n.json")),
                         ("state", "check", str(self.path)), ("state", "publish", str(self.path)),
                         ("state", "pull", "--investigation", self.INV, "--out", str(self.base / "p.json"))):
                res = self.run_cli(*args)
                out = res.stdout + res.stderr
                self.assertEqual(res.returncode, 1, (server, args, out))
                self.assertIn("does not support investigations yet", out, (server, args))
                for raw in ("403", "404", "insufficient_scope", "invalid_body", "Cannot POST"):
                    self.assertNotIn(raw, out, (server, args))
        self.assertEqual(self.path.read_text(), before)
        self.assertFalse((self.base / "n.json").exists())
        self.assertFalse((self.base / "p.json").exists())
        self.assertEqual(self.puts(), [])
