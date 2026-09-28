"""Tests for local evidence capture in the Cursor adapter's postToolUse hook.

A non-Cardinal MCP result (tool name `mcp__<server>__<tool>`) is recorded in
the shared spool ~/.cardinal/evidence/<conversation_id>/ev_<12 hex>.json
(cardinal_core.evidence) and its id handed back to the agent via
`additional_context`. Cardinal's own `cardinal` server, non-MCP tools, the
opt-outs and every failure stay silent. Nothing is sent over the network.

Each test runs the hook as a subprocess with HOME pointed at a temp dir that
has no Cardinal connection state (capture does not need one).

Run: uv run --no-project --with pytest python -m pytest adapters/cursor/tests/test_cursor_evidence_capture.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from tempfile import TemporaryDirectory

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER = TESTS_DIR.parent
REPO_ROOT = ADAPTER.parent.parent
HOOK = ADAPTER / "hooks" / "cardinal-cursor-telemetry.py"
CONV = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"
LINE_RE = re.compile(r"^\[evidence:(ev_[0-9a-f]{12})\] captured locally from grafana/query_prometheus$")

if not (ADAPTER / "hooks" / "cardinal_core" / "evidence.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "cursor"],
                   check=True, capture_output=True)


def _load_hook():
    loader = SourceFileLoader("cardinal_cursor_telemetry_ev", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CursorEvidenceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = self.home / ".cardinal" / "evidence"

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra) -> dict:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(extra)
        return env

    def _payload(self, **over) -> dict:
        # Cursor's documented postToolUse shape: tool_output is a JSON
        # string; base fields (model, cursor_version, ...) on every payload.
        body = {
            "hook_event_name": "postToolUse",
            "conversation_id": CONV,
            "generation_id": "gen-1",
            "workspace_roots": [str(self.home)],
            "model": "claude-4.5-sonnet",
            "cursor_version": "1.7.29",
            "tool_name": "mcp__grafana__query_prometheus",
            "tool_input": {"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"},
            "tool_output": json.dumps({
                "content": [{"type": "text", "text": "series: 3"}],
                "structuredContent": {"series": 3},
            }),
            "tool_use_id": "call_01ABC",
        }
        body.update(over)
        return body

    def _run(self, payload: dict | None = None, raw: str | None = None, env: dict | None = None):
        stdin = raw if raw is not None else json.dumps(payload if payload is not None else self._payload())
        res = subprocess.run([sys.executable, str(HOOK), "--event", "postToolUse"], input=stdin,
                             capture_output=True, text=True, timeout=30, env=env or self._env(),
                             cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        return res

    def _captured(self, res, session: str = CONV) -> tuple:
        ctx = json.loads(res.stdout)["additional_context"]
        line = ctx.split("\n\n")[0]
        m = LINE_RE.match(line)
        self.assertIsNotNone(m, ctx)
        ev_id = m.group(1)
        path = self.root / session / f"{ev_id}.json"
        self.assertTrue(path.is_file(), path)
        return ev_id, ctx, json.loads(path.read_text()), path

    def _spool_files(self) -> list:
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.rglob("ev_*.json"))

    def _assert_no_capture(self, res):
        self.assertNotIn("[evidence:", res.stdout)
        self.assertEqual(self._spool_files(), [])

    # -- captured ----------------------------------------------------------

    def test_non_cardinal_mcp_result_is_spooled_and_its_id_returned(self):
        res = self._run()
        ev_id, ctx, entry, path = self._captured(res)
        self.assertEqual(ctx, f"[evidence:{ev_id}] captured locally from grafana/query_prometheus")
        self.assertEqual(entry["schema"], "cardinal.evidence.v1")
        self.assertEqual(entry["tier"], "captured")
        self.assertEqual(entry["evidence_id"], ev_id)
        self.assertEqual(entry["session_id"], CONV)
        self.assertEqual(entry["server"], "grafana")
        self.assertEqual(entry["tool"], "query_prometheus")
        self.assertEqual(entry["tool_name"], "mcp__grafana__query_prometheus")
        self.assertEqual(entry["agent"], "cursor/1.7.29")
        self.assertEqual(entry["tool_use_id"], "call_01ABC")
        self.assertEqual(entry["args"], {"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"})
        # tool_output's JSON string was decoded: structure kept, the text
        # mirror of structuredContent dropped only when identical.
        self.assertEqual(entry["result"], {"structured": {"series": 3}, "text": ["series: 3"]})
        self.assertFalse(entry["truncated"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_camel_case_payload_spellings(self):
        payload = {
            "hook_event_name": "postToolUse",
            "conversationId": CONV,
            "cursorVersion": "1.8.0",
            "toolName": "mcp__grafana__query_prometheus",
            "toolInput": {"expr": "up"},
            "toolOutput": "plain text result",
            "toolUseId": "call_camel",
        }
        _, _, entry, _ = self._captured(self._run(payload))
        self.assertEqual(entry["agent"], "cursor/1.8.0")
        self.assertEqual(entry["tool_use_id"], "call_camel")
        self.assertEqual(entry["args"], {"expr": "up"})
        self.assertEqual(entry["result"], {"text": ["plain text result"]})

    def test_client_is_bare_cursor_without_a_clean_version(self):
        for version in (None, "1.7.29; echo pwned", "1.7.29\nX-Injected: 1"):
            with self.subTest(version=version):
                over = {"cursor_version": version} if version is not None else {}
                payload = self._payload(**over)
                if version is None:
                    payload.pop("cursor_version")
                _, _, entry, path = self._captured(self._run(payload))
                self.assertEqual(entry["agent"], "cursor")
                path.unlink()

    def test_credentials_are_scrubbed_before_they_reach_disk(self):
        payload = self._payload(
            tool_input={"expr": "up", "api_key": "LEAKARG-7f3c9a1b2d4e"},
            tool_output=json.dumps({"content": [{"type": "text",
                                                 "text": "Authorization: Bearer LEAKRES-abcdef0123456789"}]}),
        )
        _, _, entry, path = self._captured(self._run(payload))
        on_disk = path.read_text()
        self.assertNotIn("LEAKARG", on_disk)
        self.assertNotIn("LEAKRES", on_disk)
        self.assertEqual(entry["args"]["api_key"], "[redacted]")

    def test_string_tool_input_json_is_decoded(self):
        payload = self._payload(tool_input=json.dumps({"expr": "up", "password": "LEAK-pw-1234567"}))
        _, _, entry, path = self._captured(self._run(payload))
        self.assertEqual(entry["args"], {"expr": "up", "password": "[redacted]"})
        self.assertNotIn("LEAK", path.read_text())

    def test_missing_conversation_id_still_captures_under_no_session(self):
        payload = self._payload()
        payload.pop("conversation_id")
        res = self._run(payload)
        _, ctx, entry, _ = self._captured(res, session="no-session")
        self.assertIsNone(entry["session_id"])
        self.assertEqual(json.loads(res.stdout), {"additional_context": ctx})

    def test_evidence_line_leads_a_staged_notify_message(self):
        limits_dir = self.home / ".cursor" / "cardinal" / "limits"
        limits_dir.mkdir(parents=True)
        (limits_dir / f"{CONV}.notify.json").write_text(
            json.dumps({"message": "You are at 60% of session budget.", "band": 1, "staged_at": 0}))
        res = self._run()
        ev_id, ctx, _, _ = self._captured(res)
        self.assertEqual(ctx, f"[evidence:{ev_id}] captured locally from grafana/query_prometheus"
                              "\n\nYou are at 60% of session budget.")

    # -- not captured -------------------------------------------------------

    def test_cardinal_server_is_skipped(self):
        # Cardinal's gateway mints witnessed receipts; the golden fixture's
        # tool name is exactly this shape.
        res = self._run(self._payload(tool_name="mcp__cardinal__lakerunner__list_services"))
        self._assert_no_capture(res)
        self.assertEqual(res.stdout, "")

    def test_non_mcp_tools_are_skipped(self):
        for name in ("run_terminal_cmd", "read_file", "MCP:query_prometheus", "mcp__grafana", "mcp____x"):
            with self.subTest(name=name):
                res = self._run(self._payload(tool_name=name))
                self._assert_no_capture(res)

    def test_opt_out_env(self):
        for v in ("0", "false", "off", "no"):
            with self.subTest(v=v):
                res = self._run(env=self._env(CARDINAL_EVIDENCE_CAPTURE=v))
                self._assert_no_capture(res)

    def test_opt_out_flag_file(self):
        self.root.mkdir(parents=True)
        (self.root / "disabled").touch()
        res = self._run()
        self._assert_no_capture(res)

    def test_symlinked_spool_is_refused_and_the_hook_fails_open(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (self.home / ".cardinal").mkdir()
        os.symlink(elsewhere, self.root)
        res = self._run()
        self.assertNotIn("[evidence:", res.stdout)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unwritable_spool_fails_open(self):
        (self.home / ".cardinal").mkdir()
        self.root.write_text("not a directory")
        res = self._run()
        self.assertNotIn("[evidence:", res.stdout)

    def test_garbage_stdin_fails_open(self):
        for raw in ("", "not json", "[1,2]", json.dumps({"hook_event_name": "postToolUse", "tool_name": 7})):
            with self.subTest(raw=raw):
                res = self._run(raw=raw)
                self._assert_no_capture(res)


class DecodeJsonContainerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hook = _load_hook()

    def test_decodes_json_objects_and_arrays_only(self):
        d = self.hook._decode_json_container
        self.assertEqual(d('{"content": []}'), {"content": []})
        self.assertEqual(d(' [{"type": "text", "text": "x"}] '), [{"type": "text", "text": "x"}])
        for keep in ("plain", "42", '"quoted"', "{not json", "", None, {"a": 1}, [1]):
            self.assertEqual(d(keep), keep, repr(keep))


if __name__ == "__main__":
    unittest.main()
