"""Tests for scripts/cardinal-decision and the BeforeAgent decision prompt +
PR linkage in hooks/cardinal-gemini-telemetry.py.

Each test runs the script as a subprocess with HOME pointed at a temp dir
whose ~/.gemini/cardinal.json + cardinal-secrets.json route OTLP to a local
stub server, inside a temp git repo with an origin remote, and a stub `gh`
on PATH (no network).

Run from the monorepo root:
    python3 -m pytest adapters/gemini/tests -q
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

ADAPTER = Path(__file__).resolve().parent.parent
MONOREPO_ROOT = ADAPTER.parents[1]
CLI = ADAPTER / "scripts" / "cardinal-decision"
HOOK = ADAPTER / "hooks" / "cardinal-gemini-telemetry.py"
GIT = shutil.which("git") or "/usr/bin/git"

GH_PR = '{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}'


def _ensure_vendored() -> None:
    if (ADAPTER / "hooks" / "cardinal_core" / "decisions.py").exists():
        return
    subprocess.run(
        [sys.executable, str(MONOREPO_ROOT / "build" / "vendor.py"), "gemini"],
        check=True, capture_output=True,
    )


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


class DecisionTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_vendored()

    def setUp(self):
        _OTLPStub.received = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OTLPStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / "home"
        self.gemini = self.home / ".gemini"
        self.gemini.mkdir(parents=True)
        self.write_connected()
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
        self.set_gh(f"#!/bin/sh\necho '{GH_PR}'\n")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def write_connected(self):
        (self.gemini / "cardinal.json").write_text(json.dumps({
            "user_email": "eng1@example.com",
            "org_slug": "acme",
            "deployment_environment": "prod",
            "ingest_endpoint": f"http://127.0.0.1:{self.server.server_port}",
        }))
        (self.gemini / "cardinal-secrets.json").write_text(json.dumps({
            "ingest_api_key": "test-key",
            "ingest_api_header": "x-cardinalhq-api-key",
        }))

    def set_gh(self, script: str):
        gh = self.bin / "gh"
        gh.write_text(script)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)

    def env(self, **extra) -> dict:
        path = f"{self.bin}:{os.path.dirname(GIT)}:/usr/bin:/bin"
        return {"HOME": str(self.home), "PATH": path,
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", **extra}

    def cli(self, *args: str, expect_rc: int = 0, **env) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.repo, env=self.env(**env),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def hook(self, event: str, payload: dict, **env) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(HOOK), "--event", event], input=json.dumps(payload),
            cwd=self.repo, env=self.env(**env), capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def records(self, event_name: str) -> list:
        out = []
        for body in _OTLPStub.received:
            resource = _attrs(body["resourceLogs"][0]["resource"]["attributes"])
            for rec in body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
                attrs = _attrs(rec["attributes"])
                if attrs.get("event_name") == event_name:
                    out.append((resource, attrs))
        return out


class CardinalDecisionCliTest(DecisionTestBase):
    def test_off_by_default(self):
        proc = self.cli("record", "--session", "s1", "--choice", "Use X", expect_rc=3)
        self.assertIn("cardinal-decision on", proc.stderr)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_env_override_turns_capture_on(self):
        self.cli("record", "--session", "s1", "--choice", "Use X", CARDINAL_DECISIONS="1")
        self.assertEqual(len(self.records("cardinal.decision")), 1)
        self.cli("on")
        self.cli("record", "--session", "s1", "--choice", "Use Y", expect_rc=3, CARDINAL_DECISIONS="0")

    def test_on_off_state_lives_under_gemini_runtime_dir(self):
        self.cli("on")
        config = self.gemini / "cardinal" / "decisions" / "config.json"
        self.assertEqual(json.loads(config.read_text()), {"enabled": True})
        self.cli("off")
        self.assertEqual(json.loads(config.read_text()), {"enabled": False})

    def test_records_a_tagged_decision(self):
        self.cli("on")
        proc = self.cli(
            "record", "--session", "s1",
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
        self.assertEqual(resource["user.email"], "eng1@example.com")
        self.assertEqual(resource["service.name"], "gemini-cli")
        self.assertEqual(resource["agent.runtime"], "gemini")
        self.assertEqual(attrs["session_id"], "s1")
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
        self.cli("record", "--session", "s1", "--id", "dir", "--choice", "Exact trace directory")
        proc = self.cli(
            "record", "--session", "s1", "--id", "lsm", "--choice", "Mutation-run S3 LSM",
            "--supersedes", "dir", "--follows", "elsewhere", "--by", "user",
        )
        self.assertIn("no earlier decision in this session has id elsewhere", proc.stdout)
        attrs = self.records("cardinal.decision")[-1][1]
        self.assertEqual(attrs["cardinal.decision.decided_by"], "user")
        status = self.cli("status", "--session", "s1")
        self.assertIn("Decision capture: on", status.stdout)
        self.assertIn("Cardinal telemetry: connected", status.stdout)
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", status.stdout)

    def test_session_falls_back_to_gemini_session_env(self):
        self.cli("on")
        self.cli("record", "--choice", "Use X", GEMINI_SESSION_ID="s-env")
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["session_id"], "s-env")

    def test_usage_errors(self):
        self.cli("on")
        proc = self.cli("record", "--choice", "Use X", expect_rc=2)
        self.assertIn("--session is required", proc.stderr)
        proc = self.cli("record", "--session", "s1", "--choice", "Use X", "--refines", "Bad Id", expect_rc=2)
        self.assertIn("is not a decision id", proc.stderr)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_saved_locally_when_not_connected(self):
        (self.gemini / "cardinal-secrets.json").unlink()
        self.cli("on")
        proc = self.cli("record", "--session", "s1", "--choice", "Use X")
        self.assertIn("only saved locally", proc.stdout)
        self.assertIn("not connected", self.cli("status").stdout)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_help(self):
        self.assertIn("record", self.cli("--help").stdout)


class BeforeAgentDecisionPromptTest(DecisionTestBase):
    def payload(self, **extra) -> dict:
        return {"session_id": "s1", "cwd": str(self.repo), "prompt": "next", **extra}

    def test_silent_when_off(self):
        self.assertEqual(self.hook("BeforeAgent", self.payload()).stdout, "")

    def test_injects_protocol_and_ledger_when_on(self):
        self.cli("on")
        self.cli("record", "--session", "s1", "--id", "dir", "--choice", "Exact trace directory")
        out = json.loads(self.hook("BeforeAgent", self.payload()).stdout)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "BeforeAgent")
        context = hso["additionalContext"]
        self.assertIn(f'python3 "{CLI}" record --session s1', context)
        self.assertIn("run_shell_command", context)
        self.assertIn("- dir: Exact trace directory", context)
        # Gemini escapes < and > in additionalContext; keep the protocol free of them.
        self.assertNotIn("<", context)
        self.assertNotIn(">", context)

    def test_env_override_injects(self):
        out = json.loads(self.hook("BeforeAgent", self.payload(), CARDINAL_DECISIONS="1").stdout)
        self.assertIn("(none yet)", out["hookSpecificOutput"]["additionalContext"])

    def test_merges_with_limits_warn_and_block_wins(self):
        import time
        limits = self.gemini / "cardinal" / "limits"
        limits.mkdir(parents=True)
        (limits / "s-warn.verdict.json").write_text(json.dumps({
            "decision": "warn", "band": 2, "agent_context": "Budget standing: 80%.",
            "user_message": "You are at 80%.", "fetched_at": time.time(),
        }))
        (limits / "s-block.verdict.json").write_text(json.dumps({
            "decision": "block", "band": 3, "block_reason": "Limit exhausted.",
            "fetched_at": time.time(),
        }))
        self.cli("on")
        out = json.loads(self.hook("BeforeAgent", self.payload(session_id="s-warn")).stdout)
        self.assertEqual(out["systemMessage"], "You are at 80%.")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("Budget standing: 80%.\n\nCardinal decision capture is on"))
        out = json.loads(self.hook("BeforeAgent", self.payload(session_id="s-block")).stdout)
        self.assertEqual(out, {"decision": "block", "reason": "Limit exhausted."})

    def test_missing_session_or_bad_input_is_silent(self):
        self.cli("on")
        self.assertEqual(self.hook("BeforeAgent", {"prompt": "no session"}).stdout, "")
        proc = subprocess.run([sys.executable, str(HOOK), "--event", "BeforeAgent"], input="not json",
                              env=self.env(), capture_output=True, text=True, timeout=10)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


class BeforeAgentPrLinkageTest(DecisionTestBase):
    def git_state(self) -> dict:
        [(_, attrs)] = self.records("cardinal.git_state")
        return attrs

    def test_git_state_carries_pr(self):
        self.hook("BeforeAgent", {"session_id": "s1", "cwd": str(self.repo), "prompt": "hi"})
        attrs = self.git_state()
        self.assertEqual(int(attrs["cardinal_pr_number"]), 42)
        self.assertEqual(attrs["cardinal_pr_url"], "https://github.com/acme/widgets/pull/42")
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")

    def test_pr_keys_absent_when_gh_fails(self):
        self.set_gh("#!/bin/sh\necho 'no pull requests found' >&2\nexit 1\n")
        self.hook("BeforeAgent", {"session_id": "s1", "cwd": str(self.repo), "prompt": "hi"})
        attrs = self.git_state()
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertNotIn("cardinal_pr_url", attrs)
        self.assertEqual(attrs["cardinal_repo"], "acme/widgets")

    def test_pr_keys_absent_when_gh_missing(self):
        (self.bin / "gh").unlink()
        self.hook("BeforeAgent", {"session_id": "s1", "cwd": str(self.repo), "prompt": "hi"})
        self.assertNotIn("cardinal_pr_number", self.git_state())

    def test_slow_gh_is_bounded(self):
        self.set_gh(f"#!/bin/sh\nsleep 5\necho '{GH_PR}'\n")
        import time
        start = time.monotonic()
        self.hook("BeforeAgent", {"session_id": "s1", "cwd": str(self.repo), "prompt": "hi"})
        self.assertLess(time.monotonic() - start, 4.5)
        attrs = self.git_state()
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertIn("cardinal_head_sha", attrs)


class HookRegistrationTest(unittest.TestCase):
    """Gemini CLI's extension loader reads the event map from a top-level
    `hooks` key and treats `timeout` as milliseconds."""

    def test_extension_hooks_json_shape(self):
        data = json.loads((ADAPTER / "extension" / "hooks" / "hooks.json").read_text())
        self.assertIsInstance(data.get("hooks"), dict)
        self.assertIn("BeforeAgent", data["hooks"])
        for event, groups in data["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    with self.subTest(event=event):
                        self.assertIn(f"--event {event}", handler["command"])
                        self.assertGreaterEqual(handler["timeout"], 5000)


if __name__ == "__main__":
    unittest.main()
