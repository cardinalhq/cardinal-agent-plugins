"""Tests for bin/cardinal-decision and hooks/decision-prompt.py
(cardinal.decision telemetry).

Each test runs the script as a subprocess with HOME pointed at a temp
dir whose .claude/settings.json routes OTLP to a local stub server, in a
temp git repo with an origin remote, and a stub `gh` on PATH.
Requires cardinal_core vendored: python3 build/vendor.py claude

Run with: python3 -m unittest tests.test_decisions -v
"""

import json
import os
import shutil
import stat
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

ADAPTER = Path(__file__).resolve().parent.parent
CLI = ADAPTER / "bin" / "cardinal-decision"
HOOK = ADAPTER / "hooks" / "decision-prompt.py"
GIT = shutil.which("git") or "/usr/bin/git"


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
    def setUp(self):
        if not (ADAPTER / "hooks" / "cardinal_core" / "decisions.py").exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        _OTLPStub.received = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OTLPStub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{self.server.server_port}",
            "OTEL_EXPORTER_OTLP_HEADERS": "x-cardinalhq-api-key=test-key",
            "OTEL_RESOURCE_ATTRIBUTES": "user.email=eng1@example.com",
        }}))
        self.repo = root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "feat/trace-index"],
                     ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                     ["remote", "add", "origin", "git@github.com:acme/widgets.git"]):
            subprocess.run([GIT, *args], cwd=self.repo, check=True, capture_output=True)
        for i in range(12):
            (self.repo / "svc").mkdir(exist_ok=True)
            (self.repo / "svc" / f"f{i}.go").write_text("package svc\n")
        subprocess.run([GIT, "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run([GIT, "commit", "-q", "-m", "init"], cwd=self.repo, check=True, capture_output=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text('#!/bin/sh\necho \'{"number": 42, "url": "https://github.com/acme/widgets/pull/42"}\'\n')
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
            ["python3", str(CLI), *args], cwd=self.repo, env=self.env(**env),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def decision_records(self) -> list:
        out = []
        for body in _OTLPStub.received:
            resource = _attrs(body["resourceLogs"][0]["resource"]["attributes"])
            for rec in body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
                attrs = _attrs(rec["attributes"])
                if attrs.get("event_name") == "cardinal.decision":
                    out.append((resource, attrs))
        return out


class CardinalDecisionCliTest(DecisionTestBase):
    def test_off_by_default(self):
        proc = self.cli("record", "--session", "s1", "--choice", "Use X", expect_rc=3)
        self.assertIn("cardinal-decision on", proc.stderr)
        self.assertEqual(self.decision_records(), [])

    def test_env_override_turns_capture_on(self):
        self.cli("record", "--session", "s1", "--choice", "Use X", CARDINAL_DECISIONS="1")
        self.assertEqual(len(self.decision_records()), 1)

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

        [(resource, attrs)] = self.decision_records()
        self.assertEqual(resource["user.email"], "eng1@example.com")
        self.assertEqual(resource["service.name"], "claude-code")
        self.assertEqual(attrs["session_id"], "s1")
        self.assertEqual(attrs["cardinal.decision.id"], "use-mutation-run-s3-lsm")
        self.assertEqual(attrs["cardinal.decision.choice"], "Use mutation-run S3 LSM")
        self.assertEqual(attrs["cardinal.decision.question"], "How do we index billions of trace IDs?")
        self.assertEqual(attrs["cardinal.decision.decided_by"], "agent")
        self.assertEqual(json.loads(attrs["cardinal.decision.alternatives"]), ["Per-ID rows in Postgres"])
        self.assertEqual(json.loads(attrs["cardinal.decision.anchors"]), [
            {"kind": "symbol", "identifier": "Index.Put", "path": "svc/f3.go"},
            {"kind": "config", "identifier": "TRACE_INDEX_ENABLED"},
        ])
        self.assertEqual(json.loads(attrs["cardinal.decision.code_clusters"]), ["d18:svc"])
        self.assertTrue(attrs["cardinal.decision.cluster_scheme"].startswith("d18-v1:"))
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
        attrs = self.decision_records()[-1][1]
        self.assertEqual(attrs["cardinal.decision.decided_by"], "user")
        self.assertEqual(json.loads(attrs["cardinal.decision.links"]), [
            {"relation": "follows_from", "to": "elsewhere"},
            {"relation": "supersedes", "to": "dir"},
        ])
        status = self.cli("status", "--session", "s1")
        self.assertIn("Decision capture: on", status.stdout)
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", status.stdout)

    def test_usage_errors(self):
        self.cli("on")
        proc = self.cli("record", "--choice", "Use X", expect_rc=2)
        self.assertIn("--session is required", proc.stderr)
        proc = self.cli("record", "--session", "s1", "--choice", "Use X", "--refines", "Bad Id", expect_rc=2)
        self.assertIn("is not a decision id", proc.stderr)
        self.assertEqual(self.decision_records(), [])

    def test_saved_locally_when_not_connected(self):
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {}}))
        self.cli("on")
        proc = self.cli("record", "--session", "s1", "--choice", "Use X")
        self.assertIn("only saved locally", proc.stdout)
        self.assertEqual(self.decision_records(), [])

    def test_help(self):
        self.assertIn("record", self.cli("--help").stdout)


class DecisionPromptHookTest(DecisionTestBase):
    def hook(self, payload: dict, **env) -> str:
        proc = subprocess.run(
            ["python3", str(HOOK)], input=json.dumps(payload), env=self.env(**env),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_silent_when_off(self):
        self.assertEqual(self.hook({"session_id": "s1", "prompt": "hi"}), "")

    def test_injects_protocol_and_ledger_when_on(self):
        self.cli("on")
        self.cli("record", "--session", "s1", "--id", "dir", "--choice", "Exact trace directory")
        out = json.loads(self.hook({"session_id": "s1", "prompt": "next"}))
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn(f'"{CLI}" record --session s1', context)
        self.assertIn("- dir: Exact trace directory", context)

    def test_ignores_task_notifications_and_bad_input(self):
        self.cli("on")
        self.assertEqual(self.hook({"session_id": "s1", "prompt": "task-notification done"}), "")
        self.assertEqual(self.hook({"prompt": "no session"}), "")
        proc = subprocess.run(["python3", str(HOOK)], input="not json", env=self.env(),
                              capture_output=True, text=True, timeout=10)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
