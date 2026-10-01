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
        self.assertIn(f"{sid} · same branch fix/checkout · published, 1 act", block)
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
