"""Tests for PR linkage on cardinal.git_state and opt-in decision capture
(scripts/cardinal-decision + sessionStart additional_context) in the
Cursor adapter.

Scripts run as subprocesses with HOME pointed at a temp dir whose
~/.cursor connection state routes OTLP to a local stub, inside a temp git
repo with an origin remote and a stub `gh` on PATH. No network.

Run: python3 -m pytest adapters/cursor/tests/test_decisions.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER = TESTS_DIR.parent
REPO_ROOT = ADAPTER.parent.parent
CLI = ADAPTER / "scripts" / "cardinal-decision"
HOOK = ADAPTER / "hooks" / "cardinal-cursor-telemetry.py"
GIT = shutil.which("git") or "/usr/bin/git"

if not (ADAPTER / "hooks" / "cardinal_core" / "decisions.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "cursor"],
                   check=True, capture_output=True)


class _OTLPStub(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def _attrs(kvs: list) -> dict:
    out = {}
    for kv in kvs:
        v = kv["value"]
        out[kv["key"]] = v.get("stringValue", v.get("intValue", v.get("boolValue")))
    return out


class CursorDecisionBase(unittest.TestCase):
    def setUp(self):
        _OTLPStub.received = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OTLPStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / "home"
        self.cursor = self.home / ".cursor"
        self.cursor.mkdir(parents=True)
        (self.cursor / "cardinal.json").write_text(json.dumps({
            "ingest_endpoint": f"http://127.0.0.1:{self.server.server_port}",
            "deployment_environment": "prod",
            "user_email": "eng1@example.com",
            "org_slug": "acme",
        }))
        (self.cursor / "cardinal-secrets.json").write_text(json.dumps({"ingest_api_key": "test-key"}))
        self.repo = root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "feat/trace-index"],
                     ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                     ["remote", "add", "origin", "git@github.com:acme/widgets.git"]):
            subprocess.run([GIT, *args], cwd=self.repo, check=True, capture_output=True)
        (self.repo / "svc").mkdir()
        for i in range(12):
            (self.repo / "svc" / f"f{i}.go").write_text("package svc\n")
        subprocess.run([GIT, "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run([GIT, "commit", "-q", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.gh_calls = root / "gh-calls"
        self.write_gh('echo \'{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}\'')

    def write_gh(self, body: str) -> None:
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\necho "$@" >> "{self.gh_calls}"\n{body}\n')
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def env(self, **extra) -> dict:
        path = f"{self.bin}:{os.path.dirname(GIT)}:/usr/bin:/bin"
        return {"HOME": str(self.home), "PATH": path, **extra}

    def cli(self, *args: str, expect_rc: int = 0, **env) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.repo, env=self.env(**env),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def hook(self, event: str, payload: dict, **env) -> str:
        proc = subprocess.run(
            [sys.executable, str(HOOK), "--event", event], input=json.dumps(payload),
            env=self.env(**env), capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def records(self, event_name: str) -> list:
        out = []
        for body in _OTLPStub.received:
            resource = _attrs(body["resourceLogs"][0]["resource"]["attributes"])
            for rec in body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
                attrs = _attrs(rec["attributes"])
                if attrs.get("event_name") == event_name:
                    out.append((resource, attrs))
        return out


class CursorDecisionCliTest(CursorDecisionBase):
    def test_off_by_default(self):
        proc = self.cli("record", "--session", "c1", "--choice", "Use X", expect_rc=3)
        self.assertIn("cardinal-decision on", proc.stderr)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_env_override_turns_capture_on(self):
        self.cli("record", "--session", "c1", "--choice", "Use X", CARDINAL_DECISIONS="1")
        self.assertEqual(len(self.records("cardinal.decision")), 1)

    def test_on_off_status_use_cursor_runtime_dir(self):
        self.cli("on")
        config = self.cursor / "cardinal" / "decisions" / "config.json"
        self.assertEqual(json.loads(config.read_text()), {"enabled": True})
        self.assertIn("Decision capture: on", self.cli("status").stdout)
        self.assertIn("Cardinal telemetry: connected", self.cli("status").stdout)
        self.cli("off")
        self.assertIn("Decision capture: off", self.cli("status").stdout)
        self.assertIn("(CARDINAL_DECISIONS=1)", self.cli("status", CARDINAL_DECISIONS="1").stdout)

    def test_records_a_tagged_decision(self):
        self.cli("on")
        proc = self.cli(
            "record", "--session", "c1",
            "--question", "How do we index billions of trace IDs?",
            "--choice", "Use mutation-run S3 LSM",
            "--why", "Global ID-only lookup without per-ID database rows.",
            "--alt", "Per-ID rows in Postgres",
            "--anchor", "svc/f3.go::Index.Put",
            "--anchor", "config:TRACE_INDEX_ENABLED",
        )
        self.assertIn("Recorded decision use-mutation-run-s3-lsm", proc.stdout)
        self.assertIn("PR #42", proc.stdout)

        [(resource, attrs)] = self.records("cardinal.decision")
        self.assertEqual(resource["service.name"], "cursor")
        self.assertEqual(resource["agent.runtime"], "cursor")
        self.assertEqual(resource["user.email"], "eng1@example.com")
        self.assertEqual(resource["cardinal.org"], "acme")
        self.assertEqual(attrs["session_id"], "c1")
        self.assertEqual(attrs["cardinal.decision.id"], "use-mutation-run-s3-lsm")
        self.assertEqual(attrs["cardinal.decision.decided_by"], "agent")
        self.assertEqual(json.loads(attrs["cardinal.decision.alternatives"]), ["Per-ID rows in Postgres"])
        self.assertEqual(json.loads(attrs["cardinal.decision.anchors"]), [
            {"kind": "symbol", "identifier": "Index.Put", "path": "svc/f3.go"},
            {"kind": "config", "identifier": "TRACE_INDEX_ENABLED"},
        ])
        self.assertEqual(json.loads(attrs["cardinal.decision.code_clusters"]), ["d18:svc"])
        self.assertEqual(attrs["cardinal.repo"], "acme/widgets")
        self.assertEqual(attrs["cardinal.branch"], "feat/trace-index")
        self.assertEqual(len(attrs["cardinal.head_sha"]), 40)
        self.assertEqual(int(attrs["cardinal.pr_number"]), 42)
        self.assertEqual(attrs["cardinal.pr_url"], "https://github.com/acme/widgets/pull/42")

    def test_links_and_ledger(self):
        self.cli("on")
        self.cli("record", "--session", "c1", "--id", "dir", "--choice", "Exact trace directory")
        proc = self.cli(
            "record", "--session", "c1", "--id", "lsm", "--choice", "Mutation-run S3 LSM",
            "--supersedes", "dir", "--follows", "elsewhere", "--by", "user",
        )
        self.assertIn("no earlier decision in this session has id elsewhere", proc.stdout)
        attrs = self.records("cardinal.decision")[-1][1]
        self.assertEqual(attrs["cardinal.decision.decided_by"], "user")
        self.assertEqual(json.loads(attrs["cardinal.decision.links"]), [
            {"relation": "follows_from", "to": "elsewhere"},
            {"relation": "supersedes", "to": "dir"},
        ])
        status = self.cli("status", "--session", "c1")
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", status.stdout)

    def test_usage_errors(self):
        self.cli("on")
        proc = self.cli("record", "--choice", "Use X", expect_rc=2)
        self.assertIn("--session is required", proc.stderr)
        proc = self.cli("record", "--session", "c1", "--choice", "Use X", "--refines", "Bad Id", expect_rc=2)
        self.assertIn("is not a decision id", proc.stderr)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_saved_locally_when_not_connected(self):
        (self.cursor / "cardinal-secrets.json").unlink()
        self.cli("on")
        proc = self.cli("record", "--session", "c1", "--choice", "Use X")
        self.assertIn("only saved locally", proc.stdout)
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertIn("- use-x: Use X", self.cli("status", "--session", "c1").stdout)

    def test_no_subcommand_prints_help(self):
        proc = self.cli(expect_rc=2)
        self.assertIn("record", proc.stdout)


class SessionStartDecisionContextTest(CursorDecisionBase):
    def payload(self, cwd: Path | None = None) -> dict:
        return {"hook_event_name": "sessionStart", "conversation_id": "c1",
                "session_id": "c1", "workspace_roots": [str(cwd or self.repo)]}

    def test_off_injects_only_convention_prompt(self):
        out = json.loads(self.hook("sessionStart", self.payload()))
        self.assertNotIn("decision capture", out["additional_context"])
        self.assertIn("Cardinal-instrumented Cursor session", out["additional_context"])

    def test_on_appends_protocol_and_ledger(self):
        self.cli("on")
        self.cli("record", "--session", "c1", "--id", "dir", "--choice", "Exact trace directory")
        out = json.loads(self.hook("sessionStart", self.payload()))
        context = out["additional_context"]
        self.assertTrue(context.startswith("You are running inside a Cardinal-instrumented Cursor session"))
        self.assertIn("Cardinal decision capture is on for this session", context)
        self.assertIn(f'python3 "{CLI}" record --session c1', context)
        self.assertIn("- dir: Exact trace directory", context)

    def test_on_outside_git_repo_still_injects_decision_context(self):
        self.cli("on")
        plain = self.home / "plain"
        plain.mkdir()
        out = json.loads(self.hook("sessionStart", self.payload(plain)))
        self.assertTrue(out["additional_context"].startswith("Cardinal decision capture is on"))
        self.assertIn("(none yet)", out["additional_context"])

    def test_off_outside_git_repo_is_silent(self):
        plain = self.home / "plain"
        plain.mkdir()
        self.assertEqual(self.hook("sessionStart", self.payload(plain)), "")

    def test_env_override_off_wins(self):
        self.cli("on")
        out = json.loads(self.hook("sessionStart", self.payload(), CARDINAL_DECISIONS="0"))
        self.assertNotIn("decision capture", out["additional_context"])


class GitStatePrLinkageTest(CursorDecisionBase):
    def prompt(self) -> dict:
        return {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "c1",
                "workspace_roots": [str(self.repo)], "prompt": "hi"}

    def test_pr_keys_on_git_state(self):
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")
        self.assertEqual(int(attrs["cardinal_pr_number"]), 42)
        self.assertEqual(attrs["cardinal_pr_url"], "https://github.com/acme/widgets/pull/42")

    def test_pr_cached_across_prompts(self):
        self.hook("beforeSubmitPrompt", self.prompt())
        self.hook("beforeSubmitPrompt", self.prompt())
        self.assertEqual(len(self.records("cardinal.git_state")), 2)
        self.assertEqual(len(self.gh_calls.read_text().splitlines()), 1)

    def test_no_pr_leaves_keys_absent(self):
        self.write_gh("echo 'no pull requests found' >&2; exit 1")
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertNotIn("cardinal_pr_url", attrs)
        self.assertEqual(attrs["cardinal_repo"], "acme/widgets")

    def test_missing_gh_never_fails_the_record(self):
        (self.bin / "gh").unlink()
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")

    def test_protected_branch_skips_gh(self):
        subprocess.run([GIT, "checkout", "-q", "-b", "main"], cwd=self.repo, check=True, capture_output=True)
        self.hook("beforeSubmitPrompt", self.prompt())
        [(_, attrs)] = self.records("cardinal.git_state")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertFalse(self.gh_calls.exists())


class ResolvePrTimeoutTest(unittest.TestCase):
    """The synchronous beforeSubmitPrompt path bounds the gh call."""

    def test_timeout_is_bounded_and_errors_swallowed(self):
        from importlib.machinery import SourceFileLoader
        import importlib.util

        loader = SourceFileLoader("cursor_telemetry_pr", str(HOOK))
        spec = importlib.util.spec_from_loader("cursor_telemetry_pr", loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)

        self.assertLessEqual(hook.PR_RESOLVE_TIMEOUT_SEC, 1.5)
        with mock.patch.object(hook.decisions, "resolve_pr", return_value=(9, "u")) as rp:
            self.assertEqual(hook.resolve_pr("/x", "a/b", "feat/y"), (9, "u"))
        self.assertEqual(rp.call_args.kwargs["timeout"], hook.PR_RESOLVE_TIMEOUT_SEC)
        with mock.patch.object(hook.decisions, "resolve_pr", side_effect=RuntimeError("boom")):
            self.assertEqual(hook.resolve_pr("/x", "a/b", "feat/y"), (None, None))


if __name__ == "__main__":
    unittest.main()
