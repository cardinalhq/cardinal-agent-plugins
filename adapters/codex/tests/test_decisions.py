"""Decision capture + PR linkage for the Codex adapter.

- scripts/cardinal-decision (on|off|status|record → one cardinal.decision
  OTLP log), modeled on adapters/claude/tests/test_decisions.py.
- The telemetry hook's UserPromptSubmit handler: injects the decision
  instructions + ledger when capture is on (merged with the spend gate's
  single JSON object), and adds cardinal_pr_number / cardinal_pr_url to
  cardinal.git_state.

Every subprocess runs with HOME in a temp dir holding a prefabricated
~/.codex connection pointing at a local OTLP stub, and a stub `gh` on PATH
(tests/fixtures.py) — no network.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent   # adapters/codex
REPO_ROOT = ROOT.parent.parent
HOOK = ROOT / "hooks" / "cardinal-codex-telemetry.py"
CLI = ROOT / "scripts" / "cardinal-decision"


def setUpModule() -> None:  # noqa: N802
    subprocess.run(
        [sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "codex"],
        check=True, capture_output=True,
    )


def _attrs(kvs: list) -> dict:
    out = {}
    for kv in kvs:
        v = kv["value"]
        out[kv["key"]] = next(iter(v.values()))
    return out


class DecisionTestBase(unittest.TestCase):
    def setUp(self):
        self.received: list[dict] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/v1/logs":
                    received.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = TemporaryDirectory()
        root = Path(os.path.realpath(self.tmp.name))
        self.home = root / "home"
        self.home.mkdir()
        fixtures.write_connection(self.home, f"http://127.0.0.1:{self.server.server_port}")
        self.repo = root / "repo"
        self.repo.mkdir()
        git_env = {**os.environ, **fixtures.GIT_ENV}
        for args in (["init", "-q", "-b", "feat/trace-index"],
                     ["remote", "add", "origin", "git@github.com:acme/widgets.git"]):
            subprocess.run(["git", *args], cwd=self.repo, env=git_env, check=True, capture_output=True)
        (self.repo / "svc").mkdir()
        for i in range(12):
            (self.repo / "svc" / f"f{i}.go").write_text("package svc\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, env=git_env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, env=git_env,
                       check=True, capture_output=True)
        self.bin = fixtures.install_gh_stub(self.home)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def env(self, **extra: str) -> dict:
        env = os.environ.copy()
        for k in ("CODEX_SESSION_ID", "OPENAI_CODEX_SESSION_ID", "CARDINAL_DECISIONS",
                  "CARDINAL_FIXTURE_GH"):
            env.pop(k, None)
        env["HOME"] = str(self.home)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env.update(extra)
        return env

    def cli(self, *args: str, expect_rc: int = 0, **env: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.repo, env=self.env(**env),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def prompt(self, session_id: str = "s1", cwd: Path | None = None,
               **env: str) -> subprocess.CompletedProcess:
        env_extra = {k: v for k, v in env.items()}
        proc = fixtures.run_hook(HOOK, "UserPromptSubmit", self.home, {
            "session_id": session_id, "cwd": str(cwd or self.repo), "prompt": "next",
        }, env_extra=env_extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def records(self, event: str) -> list[tuple[dict, dict]]:
        out = []
        for body in self.received:
            resource = _attrs(body["resourceLogs"][0]["resource"]["attributes"])
            for rec in body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]:
                attrs = _attrs(rec["attributes"])
                if attrs.get("event_name") == event:
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
        self.cli("record", "--session", "s1", "--choice", "Use Y", CARDINAL_DECISIONS="0", expect_rc=3)

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
        self.assertIn(f"PR #{fixtures.FIXTURE_PR_NUMBER}", proc.stdout)

        [(resource, attrs)] = self.records("cardinal.decision")
        self.assertEqual(resource["user.email"], "golden@example.com")
        self.assertEqual(resource["service.name"], "codex")
        self.assertEqual(resource["agent.runtime"], "codex")
        self.assertEqual(resource["cardinal.org"], "golden-org")
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
        self.assertEqual(int(attrs["cardinal.pr_number"]), fixtures.FIXTURE_PR_NUMBER)
        self.assertEqual(attrs["cardinal.pr_url"], fixtures.FIXTURE_PR_URL)
        # State lives under the Codex runtime dir.
        self.assertTrue(
            (self.home / ".codex" / "cardinal" / "decisions" / "sessions" / "s1.json").is_file()
        )

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
        self.assertEqual(json.loads(attrs["cardinal.decision.links"]), [
            {"relation": "follows_from", "to": "elsewhere"},
            {"relation": "supersedes", "to": "dir"},
        ])
        status = self.cli("status", "--session", "s1")
        self.assertIn("Decision capture: on", status.stdout)
        self.assertIn("Cardinal telemetry: connected", status.stdout)
        self.assertIn("- dir: Exact trace directory [superseded by lsm]", status.stdout)

    def test_usage_errors(self):
        self.cli("on")
        proc = self.cli("record", "--choice", "Use X", expect_rc=2)
        self.assertIn("--session is required", proc.stderr)
        proc = self.cli("record", "--session", "s1", "--choice", "Use X", "--refines", "Bad Id", expect_rc=2)
        self.assertIn("is not a decision id", proc.stderr)
        self.assertEqual(self.records("cardinal.decision"), [])

    def test_session_from_codex_env(self):
        self.cli("on")
        self.cli("record", "--choice", "Use X", CODEX_SESSION_ID="env-sess")
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["session_id"], "env-sess")

    def test_saved_locally_when_not_connected(self):
        (self.home / ".codex" / "cardinal-secrets.json").unlink()
        self.cli("on")
        proc = self.cli("record", "--session", "s1", "--choice", "Use X")
        self.assertIn("only saved locally", proc.stdout)
        self.assertEqual(self.records("cardinal.decision"), [])
        self.assertIn("not connected", self.cli("status").stdout)

    def test_off_and_help(self):
        self.cli("on")
        self.cli("off")
        self.assertIn("Decision capture: off", self.cli("status").stdout)
        self.assertIn("record", self.cli("--help").stdout)
        self.cli(expect_rc=2)


class DecisionPromptInjectionTest(DecisionTestBase):
    def test_silent_when_off(self):
        self.assertEqual(self.prompt().stdout, "")

    def test_injects_protocol_and_ledger_when_on(self):
        self.cli("on")
        self.cli("record", "--session", "s1", "--id", "dir", "--choice", "Exact trace directory")
        out = json.loads(self.prompt().stdout)
        specific = out["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "UserPromptSubmit")
        context = specific["additionalContext"]
        # The command the agent is told to run must be this install's CLI.
        self.assertIn(f"python3 {shlex.quote(str(CLI.resolve()))} record --session s1", context)
        self.assertIn("- dir: Exact trace directory", context)
        self.assertNotIn("decision", out)

    def test_instructed_command_actually_records(self):
        self.cli("on")
        context = json.loads(self.prompt(session_id="sess-run")
                             .stdout)["hookSpecificOutput"]["additionalContext"]
        line = next(l for l in context.splitlines() if " record --session " in l)
        prefix = line.split(" --choice ", 1)[0]
        argv = shlex.split(prefix)
        argv[0] = sys.executable
        proc = subprocess.run([*argv, "--choice", "Use the ledger"], cwd=self.repo,
                              env=self.env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        [(_, attrs)] = self.records("cardinal.decision")
        self.assertEqual(attrs["session_id"], "sess-run")

    def test_env_override_enables_injection(self):
        out = json.loads(self.prompt(CARDINAL_DECISIONS="1").stdout)
        self.assertIn("(none yet)", out["hookSpecificOutput"]["additionalContext"])

    def _write_verdict(self, session_id: str, verdict: dict) -> None:
        limits = self.home / ".codex" / "cardinal" / "limits"
        limits.mkdir(parents=True, exist_ok=True)
        (limits / f"{session_id}.verdict.json").write_text(
            json.dumps({"fetched_at": time.time(), **verdict}))

    def test_merges_with_spend_gate_warning(self):
        self.cli("on")
        self._write_verdict("s1", {
            "decision": "warn", "band": 2,
            "agent_context": "Economize: budget at 90%.",
            "user_message": "Cardinal: session budget at 90%.",
        })
        out = json.loads(self.prompt().stdout)
        self.assertEqual(out["systemMessage"], "Cardinal: session budget at 90%.")
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("Economize: budget at 90%.\n\n"), context)
        self.assertIn("Cardinal decision capture is on", context)

    def test_block_verdict_is_not_diluted(self):
        self.cli("on")
        self._write_verdict("s1", {
            "decision": "block", "band": 3, "block_reason": "Session budget of $100 reached.",
        })
        out = json.loads(self.prompt().stdout)
        self.assertEqual(out, {"decision": "block", "reason": "Session budget of $100 reached."})


class GitStatePrLinkageTest(DecisionTestBase):
    def git_state(self) -> dict:
        return self.records("cardinal.git_state")[-1][1]

    def test_pr_keys_present_when_resolved(self):
        self.prompt()
        attrs = self.git_state()
        self.assertEqual(int(attrs["cardinal_pr_number"]), fixtures.FIXTURE_PR_NUMBER)
        self.assertEqual(attrs["cardinal_pr_url"], fixtures.FIXTURE_PR_URL)
        self.assertEqual(attrs["cardinal_repo"], "acme/widgets")
        cache = self.home / ".codex" / "cardinal" / "decisions" / "cache" / "prs.json"
        self.assertIn("acme/widgets#feat/trace-index", json.loads(cache.read_text()))

    def test_pr_keys_absent_when_no_pr(self):
        self.prompt(CARDINAL_FIXTURE_GH="none")
        attrs = self.git_state()
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertNotIn("cardinal_pr_url", attrs)
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")

    def test_pr_keys_absent_when_gh_missing(self):
        env = {"PATH": "/usr/bin:/bin"}  # git only, no gh at all
        proc = fixtures.run_hook(HOOK, "UserPromptSubmit", self.home, {
            "session_id": "s1", "cwd": str(self.repo), "prompt": "hi",
        }, env_extra=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("cardinal_pr_number", self.git_state())

    def test_slow_gh_is_bounded(self):
        started = time.monotonic()
        self.prompt(CARDINAL_FIXTURE_GH="slow")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.5, "gh cache miss must not stall the prompt")
        attrs = self.git_state()
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")

    def test_protected_branch_skips_gh(self):
        subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=self.repo,
                       check=True, capture_output=True)
        self.prompt()
        self.assertNotIn("cardinal_pr_number", self.git_state())
        self.assertFalse(
            (self.home / ".codex" / "cardinal" / "decisions" / "cache" / "prs.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
