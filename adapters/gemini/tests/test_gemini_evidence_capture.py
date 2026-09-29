"""Tests for local evidence capture in the Gemini adapter's AfterTool hook.

A successful non-Cardinal MCP result (identified by mcp_context
{server_name, tool_name}, or an `mcp__<server>__<tool>` name) is recorded in
the shared spool ~/.cardinal/evidence/<session_id>/ev_<12 hex>.json
(cardinal_core.evidence) and its id returned as
hookSpecificOutput.additionalContext. Cardinal's own `cardinal` server,
non-MCP tools, failed calls, the opt-outs and every failure stay silent.
Nothing is sent over the network.

Payload shapes follow gemini-cli 0.50 (hooks/hookEventHandler.ts
fireAfterToolEvent, tools/mcp-tool.ts transformMcpContentToParts +
wrapUntrusted, core/coreToolHookTriggers.ts).

Run: uv run --no-project --with pytest python -m pytest adapters/gemini/tests/test_gemini_evidence_capture.py -q
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
HOOK = ADAPTER / "hooks" / "cardinal-gemini-telemetry.py"
SESSION = "8c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
LINE_RE = re.compile(r"^\[evidence:(ev_[0-9a-f]{12})\] captured locally from grafana/query_prometheus$")

if not (ADAPTER / "hooks" / "cardinal_core" / "evidence.py").exists():
    subprocess.run([sys.executable, str(REPO_ROOT / "build" / "vendor.py"), "gemini"],
                   check=True, capture_output=True)


def _load_hook():
    loader = SourceFileLoader("cardinal_gemini_telemetry_ev", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def untrusted(text: str) -> str:
    """gemini-cli's wrapUntrusted, verbatim."""
    escaped = text.replace("</untrusted_context>", "&lt;/untrusted_context&gt;")
    return f"<untrusted_context>\n{escaped}\n</untrusted_context>"


class GeminiEvidenceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = self.home / ".cardinal" / "evidence"
        self.bin = self.home / "bin"
        self.bin.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra) -> dict:
        # PATH holds only the system dirs + our (empty) bin: no real
        # `gemini` resolves unless a test installs a fake one.
        env = {"HOME": str(self.home), "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"}
        env.update(extra)
        return env

    def _payload(self, **over) -> dict:
        body = {
            "session_id": SESSION,
            "transcript_path": str(self.home / ".gemini" / "tmp" / "chats" / f"{SESSION}.json"),
            "cwd": str(self.home),
            "hook_event_name": "AfterTool",
            "timestamp": "2026-09-28T12:00:00.000Z",
            "tool_name": "mcp_grafana_query_prometheus",
            "tool_input": {"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"},
            "tool_response": {
                "llmContent": [{"text": untrusted('{"series": 3}')}],
                "returnDisplay": '{"series": 3}',
            },
            "mcp_context": {
                "server_name": "grafana",
                "tool_name": "query_prometheus",
                "command": "npx",
                "args": ["-y", "mcp-grafana", "--api-key", "LEAKCTX-9f8e7d6c5b4a"],
                "url": "https://grafana.example/mcp",
            },
        }
        body.update(over)
        return body

    def _run(self, payload: dict | None = None, raw: str | None = None, env: dict | None = None):
        stdin = raw if raw is not None else json.dumps(payload if payload is not None else self._payload())
        res = subprocess.run([sys.executable, str(HOOK), "--event", "AfterTool"], input=stdin,
                             capture_output=True, text=True, timeout=30, env=env or self._env(),
                             cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        return res

    def _captured(self, res, session: str = SESSION) -> tuple:
        body = json.loads(res.stdout)
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], "AfterTool")
        ctx = body["hookSpecificOutput"]["additionalContext"]
        m = LINE_RE.match(ctx)
        self.assertIsNotNone(m, ctx)
        ev_id = m.group(1)
        path = self.root / session / f"{ev_id}.json"
        self.assertTrue(path.is_file(), path)
        return ev_id, ctx, json.loads(path.read_text()), path

    def _assert_no_capture(self, res):
        self.assertEqual(res.stdout, "")
        files = sorted(p.name for p in self.root.rglob("ev_*.json")) if self.root.exists() else []
        self.assertEqual(files, [])

    def _fake_gemini(self, version: str = "0.50.0", name: str = "@google/gemini-cli") -> None:
        # Homebrew/npm layout: bin/gemini -> .../@google/gemini-cli/bundle/gemini.js
        pkg = self.home / "lib" / "node_modules" / "@google" / "gemini-cli"
        (pkg / "bundle").mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"name": name, "version": version}))
        script = pkg / "bundle" / "gemini.js"
        script.write_text("#!/usr/bin/env node\n")
        script.chmod(0o755)
        os.symlink(script, self.bin / "gemini")

    # -- captured ----------------------------------------------------------

    def test_mcp_result_is_spooled_and_its_id_returned(self):
        res = self._run()
        ev_id, ctx, entry, path = self._captured(res)
        self.assertEqual(ctx, f"[evidence:{ev_id}] captured locally from grafana/query_prometheus")
        self.assertEqual(entry["schema"], "cardinal.evidence.v1")
        self.assertEqual(entry["tier"], "captured")
        self.assertEqual(entry["session_id"], SESSION)
        self.assertEqual(entry["server"], "grafana")
        self.assertEqual(entry["tool"], "query_prometheus")
        self.assertEqual(entry["tool_name"], "mcp__grafana__query_prometheus")
        self.assertEqual(entry["agent"], "gemini")
        self.assertEqual(entry["args"], {"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"})
        # Gemini's <untrusted_context> wrapper is not part of the result; the
        # one JSON text left is the tool's structured result.
        self.assertEqual(entry["result"], {"structured": {"series": 3}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_mcp_context_connection_details_are_not_recorded(self):
        _, _, _, path = self._captured(self._run())
        on_disk = path.read_text()
        for leak in ("LEAKCTX", "grafana.example", "npx"):
            self.assertNotIn(leak, on_disk)

    def test_client_names_the_installed_gemini_cli_version(self):
        self._fake_gemini("0.50.0")
        _, _, entry, _ = self._captured(self._run())
        self.assertEqual(entry["agent"], "gemini/0.50.0")

    def test_client_ignores_a_foreign_or_unclean_package(self):
        self._fake_gemini("9.9.9", name="not-gemini")
        _, _, entry, _ = self._captured(self._run())
        self.assertEqual(entry["agent"], "gemini")

    def test_client_ignores_an_unclean_version(self):
        self._fake_gemini("0.50.0\nX-Injected: 1")
        _, _, entry, _ = self._captured(self._run())
        self.assertEqual(entry["agent"], "gemini")

    def test_mcp_double_underscore_name_without_context(self):
        payload = self._payload(tool_name="mcp__grafana__query_prometheus")
        payload.pop("mcp_context")
        _, _, entry, _ = self._captured(self._run(payload))
        self.assertEqual(entry["tool_name"], "mcp__grafana__query_prometheus")

    def test_session_id_from_env_when_payload_lacks_it(self):
        payload = self._payload()
        payload.pop("session_id")
        res = self._run(payload, env=self._env(GEMINI_SESSION_ID="env-session-1"))
        _, _, entry, _ = self._captured(res, session="env-session-1")
        self.assertEqual(entry["session_id"], "env-session-1")

    def test_credentials_are_scrubbed_before_they_reach_disk(self):
        payload = self._payload(
            tool_input={"expr": "up", "api_key": "LEAKARG-7f3c9a1b2d4e"},
            tool_response={"llmContent": [{"text": untrusted("Authorization: Bearer LEAKRES-abcdef0123456789")}],
                           "returnDisplay": "x"},
        )
        _, _, entry, path = self._captured(self._run(payload))
        on_disk = path.read_text()
        self.assertNotIn("LEAKARG", on_disk)
        self.assertNotIn("LEAKRES", on_disk)
        self.assertEqual(entry["args"]["api_key"], "[redacted]")

    def test_telemetry_still_runs_after_capture(self):
        # Progress counters advance exactly as without capture.
        self._captured(self._run())
        progress = list((self.home / ".gemini").rglob(f"*{SESSION}*"))
        self.assertTrue(progress, "AfterTool telemetry path did not run")

    # -- not captured -------------------------------------------------------

    def test_cardinal_server_is_skipped(self):
        payload = self._payload(tool_name="mcp_cardinal_lakerunner__list_services",
                                mcp_context={"server_name": "cardinal", "tool_name": "lakerunner__list_services"})
        self._assert_no_capture(self._run(payload))
        payload = self._payload(tool_name="mcp__cardinal__lakerunner__list_services")
        payload.pop("mcp_context")
        self._assert_no_capture(self._run(payload))

    def test_failed_calls_are_skipped(self):
        payload = self._payload(tool_response={
            "llmContent": "MCP tool 'query_prometheus' reported tool error ...",
            "returnDisplay": "Error: MCP tool 'query_prometheus' reported an error.",
            "error": {"type": "mcp_tool_error", "message": "boom"},
        })
        self._assert_no_capture(self._run(payload))

    def test_unparseable_result_is_skipped(self):
        payload = self._payload(tool_response={"llmContent": [{"text": "[Error: Could not parse tool response]"}],
                                               "returnDisplay": "```json\n[]\n```"})
        self._assert_no_capture(self._run(payload))

    def test_non_mcp_tools_are_skipped(self):
        for name in ("run_shell_command", "read_file", "mcp_grafana_query_prometheus"):
            with self.subTest(name=name):
                payload = self._payload(tool_name=name)
                payload.pop("mcp_context")
                self._assert_no_capture(self._run(payload))

    def test_opt_out_env(self):
        for v in ("0", "false", "off", "no"):
            with self.subTest(v=v):
                self._assert_no_capture(self._run(env=self._env(CARDINAL_EVIDENCE_CAPTURE=v)))

    def test_opt_out_flag_file(self):
        self.root.mkdir(parents=True)
        (self.root / "disabled").touch()
        self._assert_no_capture(self._run())

    def test_symlinked_spool_is_refused_and_the_hook_fails_open(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (self.home / ".cardinal").mkdir()
        os.symlink(elsewhere, self.root)
        res = self._run()
        self.assertEqual(res.stdout, "")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_garbage_stdin_fails_open(self):
        for raw in ("", "not json", "[1,2]", json.dumps({"tool_name": 7, "mcp_context": "x"})):
            with self.subTest(raw=raw):
                self._assert_no_capture(self._run(raw=raw))


class GeminiToolResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hook = _load_hook()

    def conv(self, tool_response):
        return self.hook.gemini_tool_response(tool_response)

    def test_text_parts_are_unwrapped(self):
        self.assertEqual(
            self.conv({"llmContent": [{"text": untrusted("a")}, {"text": untrusted("x </untrusted_context> y")}]}),
            {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "x </untrusted_context> y"}]})
        # Older Gemini CLI: no wrapper; a string llmContent; a single Part.
        self.assertEqual(self.conv({"llmContent": "plain"}), {"content": [{"type": "text", "text": "plain"}]})
        self.assertEqual(self.conv({"llmContent": {"text": "one"}}), {"content": [{"type": "text", "text": "one"}]})
        # Only an exact wrapper is removed.
        self.assertEqual(self.conv({"llmContent": "<untrusted_context>x"}),
                         {"content": [{"type": "text", "text": "<untrusted_context>x"}]})

    def test_media_parts_count_as_non_text_blocks_without_their_label(self):
        out = self.conv({"llmContent": [
            {"text": untrusted("chart")},
            {"text": "[Tool 'render' provided the following image data with mime-type: image/png]"},
            {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}},
        ]})
        self.assertEqual(out, {"content": [{"type": "text", "text": "chart"}, {"type": "inlineData"}]})

    def test_function_response_parts_keep_raw_mcp_content(self):
        out = self.conv({"llmContent": [{"functionResponse": {"name": "q", "response": {
            "content": [{"type": "text", "text": "rows: 2"}],
            "structuredContent": {"rows": 2},
        }}}]})
        self.assertEqual(out, {"content": [{"type": "text", "text": "rows: 2"}], "structuredContent": {"rows": 2}})

    def test_return_display_is_the_fallback(self):
        self.assertEqual(self.conv({"llmContent": [], "returnDisplay": "shown"}),
                         {"content": [{"type": "text", "text": "shown"}]})
        self.assertEqual(self.conv({"llmContent": None, "returnDisplay": ""}), {"content": []})

    def test_failed_or_unusable(self):
        self.assertIsNone(self.conv({"llmContent": "x", "error": {"type": "mcp_tool_error", "message": "m"}}))
        self.assertIsNone(self.conv({"llmContent": [{"text": "[Error: Could not parse tool response]"}]}))
        self.assertIsNone(self.conv("not a dict"))
        self.assertIsNone(self.conv(None))

    def test_normalizes_like_any_client_result(self):
        from cardinal_core import evidence
        body = evidence.normalize(self.conv({"llmContent": [
            {"text": untrusted("a")},
            {"text": "[Tool 't' provided the following audio data with mime-type: audio/wav]"},
            {"inlineData": {"mimeType": "audio/wav", "data": "UklGRg=="}},
        ]}))
        self.assertEqual(body, {"text": ["a"], "other_blocks": 1})


if __name__ == "__main__":
    unittest.main()
