"""Tests for scripts/cardinal-decision and the BeforeAgent decision prompt,
PR linkage and background network work in hooks/cardinal-gemini-telemetry.py.

Each test runs the script as a subprocess with HOME pointed at a temp dir
whose ~/.gemini/cardinal.json + cardinal-secrets.json route OTLP to a local
stub server, inside a temp git repo with an origin remote, and a stub `gh`
on PATH (no network). Background jobs run inline unless a test opts out.

Run from the monorepo root:
    uv run --no-project --with pytest python -m pytest adapters/gemini/tests -q
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
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
    delay_sec: float = 0.0

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if type(self).delay_sec:
            time.sleep(type(self).delay_sec)
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


def final_model_chunk(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "llm_request": {"model": "gemini-2.0-flash", "messages": []},
        "llm_response": {
            "candidates": [{"content": {"role": "model", "parts": ["ok"]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        },
    }


class DecisionTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_vendored()

    def setUp(self):
        _OTLPStub.received = []
        _OTLPStub.delay_sec = 0.0
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
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
                "CARDINAL_GEMINI_INLINE_BACKGROUND": "1", **extra}

    def cli(self, *args: str, expect_rc: int = 0, **env) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(CLI), *args], cwd=self.repo, env=self.env(**env),
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, expect_rc, proc.stdout + proc.stderr)
        return proc

    def hook(self, event: str, payload: dict, **env) -> subprocess.CompletedProcess:
        # capture_output waits for the stdio pipes to close, exactly as Gemini
        # CLI's hookRunner does (it resolves on the child's `close` event).
        proc = subprocess.run(
            [sys.executable, str(HOOK), "--event", event], input=json.dumps(payload),
            cwd=self.repo, env=self.env(**env), capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def records(self, event_name: str) -> list:
        out = []
        for body in list(_OTLPStub.received):
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

    def test_unwritable_runtime_dir_fails_cleanly(self):
        # Simulates Gemini CLI's sandbox blocking writes under ~/.gemini.
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.cli("on")
        decisions_dir = self.gemini / "cardinal" / "decisions"
        decisions_dir.chmod(0o500)
        try:
            proc = self.cli("record", "--session", "s1", "--choice", "Use X", expect_rc=4)
            self.assertIn("sandbox", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)
            proc = self.cli("off", expect_rc=4)
            self.assertIn("sandbox", proc.stderr)
        finally:
            decisions_dir.chmod(0o700)
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
        self.hook("BeforeAgent", {"session_id": "s1", "cwd": str(self.repo), "prompt": "/code-review"})
        attrs = self.git_state()
        self.assertEqual(int(attrs["cardinal_pr_number"]), 42)
        self.assertEqual(attrs["cardinal_pr_url"], "https://github.com/acme/widgets/pull/42")
        self.assertEqual(attrs["cardinal_branch"], "feat/trace-index")
        self.assertEqual(attrs["cardinal_command"], "code-review")

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


class BackgroundNetworkTest(DecisionTestBase):
    """Hooks must return without waiting on the network: a slow ingest and a
    slow `gh` are handled by the detached background child."""

    def test_hooks_return_before_slow_network_work_finishes(self):
        _OTLPStub.delay_sec = 3.0
        self.set_gh(f"#!/bin/sh\nsleep 3\necho '{GH_PR}'\n")
        detached = {"CARDINAL_GEMINI_INLINE_BACKGROUND": "0"}
        tool = {"session_id": "s1", "cwd": str(self.repo), "tool_name": "run_shell_command",
                "tool_input": {"command": "ls"}, "tool_response": {"llmContent": "ok"}}
        for event, payload in (
            ("BeforeAgent", {"session_id": "s1", "cwd": str(self.repo), "prompt": "hi"}),
            ("AfterModel", final_model_chunk("s1")),
            ("AfterTool", tool),
            ("PreCompress", {"session_id": "s1", "cwd": str(self.repo), "trigger": "auto"}),
        ):
            with self.subTest(event=event):
                start = time.monotonic()
                self.hook(event, payload, **detached)
                self.assertLess(time.monotonic() - start, 2.0,
                                f"{event} waited on network work")

        deadline = time.monotonic() + 20
        wanted = ("cardinal.git_state", "api_request", "cardinal.turn_tool", "cardinal.plan_usage")
        while time.monotonic() < deadline and not all(self.records(e) for e in wanted):
            time.sleep(0.2)
        for event_name in wanted:
            self.assertTrue(self.records(event_name), f"{event_name} never arrived")
        self.assertEqual(int(self.records("cardinal.git_state")[0][1]["cardinal_pr_number"]), 42)
        spool = self.gemini / "cardinal" / "spool"
        self.assertEqual(list(spool.glob("*.json")), [], "background jobs left spool files behind")

    def test_non_final_model_chunk_exits_without_state(self):
        chunk = final_model_chunk("s-chunk")
        del chunk["llm_response"]["candidates"][0]["finishReason"]
        self.hook("AfterModel", chunk)
        self.assertFalse((self.gemini / "cardinal" / "telemetry" / "s-chunk.json").exists())

    def test_main_agent_after_agent_is_not_a_subagent(self):
        self.hook("AfterAgent", {"session_id": "s1", "cwd": str(self.repo), "prompt": "fix it",
                                 "prompt_response": "done", "stop_hook_active": False})
        self.assertEqual(self.records("cardinal.subagent_usage"), [])


if __name__ == "__main__":
    unittest.main()
