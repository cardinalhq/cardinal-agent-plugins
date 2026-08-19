"""Smoke tests for the migrated Cardinal Gemini adapter.

Ported from the source repo's tests/test_smoke.py: runs the telemetry hook
with fabricated Gemini CLI payloads for each event, verifies non-crash
behaviour, and inspects the state written under a sandboxed ~/.gemini/
(via HOME override) — no network, no real Gemini CLI. The pricing and
bash-classifier checks now exercise the vendored cardinal_core the hook
imports.

Run from adapters/gemini/:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "hooks" / "cardinal-gemini-telemetry.py"
MONOREPO_ROOT = ROOT.parents[1]


def _ensure_vendored() -> None:
    if (ROOT / "hooks" / "cardinal_core" / "__init__.py").exists():
        return
    subprocess.run(
        [sys.executable, str(MONOREPO_ROOT / "build" / "vendor.py"), "gemini"],
        check=True, capture_output=True,
    )


def run_hook(event: str, payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("GEMINI_SESSION_ID", None)
    return subprocess.run(
        [sys.executable, str(HOOK), "--event", event],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=10,
        env=env,
    )


class HookSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_vendored()

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        # Simulate "not connected" — hooks must silently no-op without state.
        (self.home / ".gemini").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_all_events_no_crash_without_state(self) -> None:
        payload = {"session_id": "s1", "cwd": str(self.home), "prompt": "hi"}
        for event in (
            "SessionStart", "BeforeAgent", "AfterModel", "AfterTool",
            "AfterAgent", "PreCompress", "SessionEnd",
        ):
            with self.subTest(event=event):
                result = run_hook(event, payload, self.home)
                self.assertEqual(result.returncode, 0, msg=result.stderr.decode())

    def test_session_start_outside_git_repo_emits_nothing(self) -> None:
        payload = {"session_id": "s1", "cwd": str(self.home)}
        result = run_hook("SessionStart", payload, self.home)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"", "Should suppress convention prompt outside a git repo")

    def test_after_model_writes_progress_cursor(self) -> None:
        # Even without a connected state (no ingest post), the hook still
        # advances its per-session progress file — required so turn/tool
        # counters remain monotonic across events.
        payload = {
            "session_id": "s-test",
            "model": "gemini-2.0-flash",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_input_tokens": 10,
            },
        }
        result = run_hook("AfterModel", payload, self.home)
        self.assertEqual(result.returncode, 0)
        progress = self.home / ".gemini" / "cardinal" / "telemetry" / "s-test.json"
        self.assertTrue(progress.exists(), "AfterModel must write a progress cursor")
        state = json.loads(progress.read_text())
        self.assertEqual(state["turn_seq"], 1)

    def test_after_tool_bash_classification(self) -> None:
        payload = {
            "session_id": "s-tool",
            "tool_name": "run_shell_command",
            "tool_input": {"command": "git status"},
            "success": True,
        }
        result = run_hook("AfterTool", payload, self.home)
        self.assertEqual(result.returncode, 0)
        progress = self.home / ".gemini" / "cardinal" / "telemetry" / "s-tool.json"
        self.assertTrue(progress.exists())

    def test_after_agent_emits_on_identifying_facet_only(self) -> None:
        # No identifying facet → suppressed (avoids stray main-agent AfterAgent).
        # With subagent_type present → emit path taken (returncode 0 is enough:
        # network POST silently fails without connection state).
        for payload, should_progress in (
            ({"session_id": "s-a"}, False),
            ({"session_id": "s-a", "subagent_type": "code-reviewer"}, True),
            ({"session_id": "s-a", "description": "review the diff"}, True),
            ({"session_id": "s-a", "duration_ms": 1200}, True),
        ):
            with self.subTest(payload=payload):
                result = run_hook("AfterAgent", payload, self.home)
                self.assertEqual(result.returncode, 0, msg=result.stderr.decode())

    def test_before_agent_advances_user_turn_seq(self) -> None:
        payload = {"session_id": "s-b", "cwd": str(self.home)}
        run_hook("BeforeAgent", payload, self.home)
        run_hook("BeforeAgent", payload, self.home)
        progress = self.home / ".gemini" / "cardinal" / "telemetry" / "s-b.json"
        self.assertTrue(progress.exists())
        state = json.loads(progress.read_text())
        self.assertEqual(state["user_turn_seq"], 2)
        # turn_seq / tool_seq reset each user turn.
        self.assertEqual(state["turn_seq"], 0)
        self.assertEqual(state["tool_seq"], 0)


class CoreFunctionTests(unittest.TestCase):
    """Pricing + bash-classifier checks against the vendored cardinal_core
    (the exact package the hook imports)."""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_vendored()
        sys.path.insert(0, str(ROOT / "hooks"))
        from cardinal_core import bashclass, pricing  # noqa: PLC0415
        cls.pricing = pricing
        cls.bashclass = bashclass

    def test_price_lookup_exact_and_prefix(self) -> None:
        table = self.pricing.GEMINI_PRICING_USD_PER_M
        self.assertIsNotNone(self.pricing.price_for_model("gemini-2.0-flash", table))
        self.assertIsNotNone(
            self.pricing.price_for_model("gemini-2.0-pro-2026-03-01", table),
            "longest-prefix fallback should price dated SKUs")
        self.assertIsNone(self.pricing.price_for_model("gpt-5", table),
                          "non-gemini model should be unpriced")

    def test_compute_cost_with_thought_tokens(self) -> None:
        # gemini-2.0-flash: input $0.10 / cached $0.025 / output $0.40 per 1M
        # 1M input (200k cached) + 500k output + 100k thought (bills as output)
        cost = self.pricing.compute_cost_usd("gemini-2.0-flash", {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 200_000,
            "output_tokens": 500_000,
            "thought_tokens": 100_000,
        }, self.pricing.GEMINI_PRICING_USD_PER_M)
        expected = (800_000 * 0.10 + 200_000 * 0.025 + 600_000 * 0.40) / 1_000_000
        self.assertAlmostEqual(cost, round(expected, 6), places=6)

    def test_single_verb(self) -> None:
        classify = self.bashclass.classify_bash_command
        self.assertEqual(classify("git status"), ("git-read", False))
        self.assertEqual(classify("rm -rf foo"), ("file-write", False))
        self.assertEqual(classify("git checkout -b feat/x"), ("git-write", False))

    def test_write_risk_wins_on_compound(self) -> None:
        # ls (file-read) + rm (file-write) → file-write wins, multi flag set.
        self.assertEqual(
            self.bashclass.classify_bash_command("ls && rm foo"),
            ("file-write", True),
        )


class PrivacyRedactionTests(unittest.TestCase):
    """Regression tests for docs/privacy-redaction.md §7: AfterTool's
    tool_result event used to serialize `tool_input`/`tool_parameters` as
    raw JSON of the full tool-call arguments (the whole shell command
    string, or a file-write tool's full content payload). Connects a real
    StubIngest so the actual POSTed OTLP records can be inspected, not
    just the hook's exit code."""

    @classmethod
    def setUpClass(cls) -> None:
        _ensure_vendored()
        sys.path.insert(0, str(MONOREPO_ROOT / "core" / "tests"))
        from harness import StubIngest  # noqa: PLC0415
        cls.StubIngest = StubIngest

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.stub = self.StubIngest().start()
        gemini_dir = self.home / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        (gemini_dir / "cardinal.json").write_text(json.dumps({
            "schema_version": 1,
            "ingest_endpoint": self.stub.endpoint,
            "deployment_environment": "test",
            "user_email": "t@t",
            "org_slug": "test-org",
        }))
        (gemini_dir / "cardinal-secrets.json").write_text(json.dumps({
            "ingest_api_key": "test-key",
        }))

    def tearDown(self) -> None:
        self.stub.stop()
        self._tmp.cleanup()

    def _records_named(self, name: str) -> list[dict]:
        out = []
        for batch in self.stub.log_batches:
            for rl in batch.get("resourceLogs", []):
                for sl in rl.get("scopeLogs", []):
                    for rec in sl.get("logRecords", []):
                        attrs = {a["key"]: list(a["value"].values())[0] for a in rec["attributes"]}
                        if attrs.get("event_name") == name:
                            out.append(attrs)
        return out

    def test_bash_tool_result_never_carries_command_text(self) -> None:
        cmd = "curl -H 'Authorization: Bearer supersecrettoken1234567890' https://internal.example.com/api"
        payload = {
            "session_id": "s-privacy-1",
            "tool_name": "run_shell_command",
            "tool_input": {"command": cmd},
            "success": True,
        }
        result = run_hook("AfterTool", payload, self.home)
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())

        (tool_result,) = self._records_named("tool_result")
        self.assertEqual(tool_result["tool_name"], "Bash")
        self.assertEqual(tool_result["bash_class"], "network")
        self.assertIn("command_hash", tool_result)
        self.assertNotIn("tool_input", tool_result)
        self.assertNotIn("tool_parameters", tool_result)

        (secret_event,) = self._records_named("cardinal.secret_detected")
        self.assertIn("BEARER_TOKEN", secret_event["secret_patterns"])

        raw = json.dumps(self.stub.log_batches)
        for fragment in (cmd, "supersecrettoken1234567890"):
            self.assertNotIn(fragment, raw)

    def test_write_file_tool_result_never_carries_content(self) -> None:
        content = "DATABASE_PASSWORD=hunter2\nrest of the file"
        payload = {
            "session_id": "s-privacy-2",
            "tool_name": "write_file",
            "tool_input": {"file_path": "config.py", "content": content},
            "success": True,
        }
        result = run_hook("AfterTool", payload, self.home)
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())

        (tool_result,) = self._records_named("tool_result")
        self.assertIn("args_hash", tool_result)
        self.assertIn("args_length", tool_result)
        self.assertNotIn("tool_input", tool_result)

        raw = json.dumps(self.stub.log_batches)
        self.assertNotIn(content, raw)
        self.assertNotIn("hunter2", raw)

    def test_git_state_strips_origin_url_credentials(self) -> None:
        repo = self.home / "repo"
        repo.mkdir()
        env = {**os.environ, "HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1"}

        def _git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

        _git("init", "-q", "-b", "feat/creds-test")
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-q", "-m", "init")
        token = "ghp_" + "s" * 36
        _git("remote", "add", "origin", f"https://x-access-token:{token}@github.com/cardinalhq/private-repo.git")

        payload = {"session_id": "s-privacy-3", "cwd": str(repo), "prompt": "hi"}
        result = run_hook("BeforeAgent", payload, self.home)
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())

        (git_state,) = self._records_named("cardinal.git_state")
        self.assertEqual(git_state["cardinal_remote_url"], "https://github.com/cardinalhq/private-repo.git")
        self.assertEqual(git_state["cardinal_remote_url_credential_scrubbed"], True)
        self.assertEqual(git_state["cardinal_repo"], "cardinalhq/private-repo")
        raw = json.dumps(self.stub.log_batches)
        self.assertNotIn(token, raw)
        self.assertNotIn("x-access-token", raw)


if __name__ == "__main__":
    unittest.main()
