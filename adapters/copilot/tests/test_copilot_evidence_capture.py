"""Local evidence capture in the Copilot adapter's postToolUse and
postToolUseFailure hooks.

Every tool call (an MCP tool Copilot names `<server>-<tool>`, rewritten to
`mcp__<server>__<tool>`, or Copilot's own bash / view / edit / ...) goes
through the generic pipeline (cardinal_core.evidence_capture) into the shared
spool ~/.cardinal/evidence/<sessionId>/ev_<12 hex>.json and its id is handed
back as `additionalContext`. Cardinal's own `cardinal` gateway, the opt-outs
and every failure stay silent. Nothing is sent over the network; no
connection state is needed.

Run: cd adapters/copilot && python3 -m unittest discover tests -p test_copilot_evidence_capture.py -v
"""

from __future__ import annotations

import json
import os
import re
import stat
import unittest

from fixtures import EVIDENCE_CLI, SID, HookCase

LINE_RE = re.compile(r"^\[evidence:(ev_[0-9a-f]{12})\]")


class CopilotEvidenceCaptureTests(HookCase):
    def setUp(self):
        super().setUp()
        self.root = self.home / ".cardinal" / "evidence"
        (self.copilot / "mcp-config.json").write_text(json.dumps({"mcpServers": {"grafana": {"command": "g"}}}))

    def mcp_payload(self, **over) -> dict:
        body = self.payload(
            toolName="grafana-query_prometheus",
            toolCallId="call_01ABC",
            toolArgs=json.dumps({"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"}),
            toolResult={"resultType": "success", "textResultForLlm": json.dumps({"series": 3})},
        )
        body.update(over)
        return body

    def post(self, payload: dict | None = None, event: str = "postToolUse", raw: str | None = None, **env):
        res = self.run_hook(event, payload if payload is not None else self.mcp_payload(), raw=raw, **env)
        self.assertEqual(res.stderr, "")
        return res

    def captured(self, res, session: str = SID) -> tuple:
        ctx = self.context(res)
        line = ctx.split("\n\n")[0]
        m = LINE_RE.match(line)
        self.assertIsNotNone(m, ctx)
        ev_id = m.group(1)
        path = self.root / session / f"{ev_id}.json"
        self.assertTrue(path.is_file(), path)
        return ev_id, ctx, json.loads(path.read_text()), path

    def spool_files(self) -> list:
        return sorted(p.name for p in self.root.rglob("ev_*.json")) if self.root.exists() else []

    def assert_no_capture(self, res):
        self.assertNotIn("[evidence:", res.stdout)
        self.assertEqual(self.spool_files(), [])

    # -- captured ----------------------------------------------------------

    def test_mcp_result_is_spooled_and_its_id_returned(self):
        ev_id, ctx, entry, path = self.captured(self.post())
        self.assertIn(f"{EVIDENCE_CLI} promote ev_", ctx)
        _, ctx2, _, _ = self.captured(self.post(self.mcp_payload(toolCallId="call_2")))
        self.assertRegex(ctx2, r"^\[evidence:ev_[0-9a-f]{12}\]$")
        self.assertEqual(entry["schema"], "cardinal.evidence.v2")
        self.assertEqual(entry["source"], {"kind": "mcp", "server": "grafana", "runtime": "copilot"})
        self.assertEqual(entry["tier"], "captured")
        self.assertEqual(entry["evidence_id"], ev_id)
        self.assertEqual(entry["session_id"], SID)
        self.assertEqual(entry["server"], "grafana")
        self.assertEqual(entry["tool"], "query_prometheus")
        self.assertEqual(entry["tool_name"], "grafana-query_prometheus")
        self.assertEqual(entry["client"], "copilot")
        self.assertEqual(entry["tool_use_id"], "call_01ABC")
        # toolArgs arrived as a JSON string; the result text's JSON was decoded.
        self.assertEqual(entry["args"], {"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"})
        self.assertEqual(entry["result"], {"structured": {"series": 3}})
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_unconfigured_server_is_a_builtin_tool(self):
        (self.copilot / "mcp-config.json").unlink()
        _, _, entry, _ = self.captured(self.post())
        self.assertEqual(entry["source"], {"kind": "builtin", "runtime": "copilot"})
        self.assertEqual(entry["tool"], "grafana-query_prometheus")

    def test_cardinal_behavior_is_captured_as_its_own_server(self):
        _, _, entry, _ = self.captured(self.post(self.mcp_payload(toolName="cardinal-behavior-get_behavior_sdk")))
        self.assertEqual(entry["source"], {"kind": "mcp", "server": "cardinal-behavior", "runtime": "copilot"})
        self.assertEqual(entry["tool"], "get_behavior_sdk")

    def test_snake_case_payload_spellings(self):
        payload = {"session_id": SID, "cwd": str(self.repo), "tool_name": "grafana-query_prometheus",
                   "tool_input": {"expr": "up"}, "tool_use_id": "call_snake",
                   "tool_result": {"result_type": "success", "text_result_for_llm": "plain text result"}}
        _, _, entry, _ = self.captured(self.post(payload))
        self.assertEqual(entry["tool_use_id"], "call_snake")
        self.assertEqual(entry["args"], {"expr": "up"})
        self.assertEqual(entry["result"], {"text": ["plain text result"]})

    def test_credentials_are_scrubbed_before_they_reach_disk(self):
        payload = self.mcp_payload(
            toolArgs={"expr": "up", "api_key": "LEAKARG-7f3c9a1b2d4e"},
            toolResult={"resultType": "success", "textResultForLlm": "Authorization: Bearer LEAKRES-abcdef0123456789"},
        )
        _, _, entry, path = self.captured(self.post(payload))
        on_disk = path.read_text()
        self.assertNotIn("LEAKARG", on_disk)
        self.assertNotIn("LEAKRES", on_disk)
        self.assertEqual(entry["args"]["api_key"], "[redacted]")

    def test_missing_session_id_still_captures_under_no_session(self):
        payload = self.mcp_payload()
        payload.pop("sessionId")
        res = self.post(payload)
        _, ctx, entry, _ = self.captured(res, session="no-session")
        self.assertIsNone(entry["session_id"])
        self.assertEqual(json.loads(res.stdout), {"additionalContext": ctx})

    def test_copilot_own_tools_are_captured(self):
        shell = self.tool(args={"command": "npm test"}, text="12 passing\n<exited with exit code 0>",
                          toolCallId="call_sh")
        _, _, entry, _ = self.captured(self.post(shell))
        self.assertEqual(entry["source"], {"kind": "builtin", "runtime": "copilot"})
        self.assertEqual(entry["server"], "builtin:copilot")
        self.assertEqual(entry["tool"], "bash")
        self.assertEqual(entry["args"], {"command": "npm test"})
        self.assertEqual(entry["result"], {"text": ["12 passing\n<exited with exit code 0>"]})
        for i, name in enumerate(("view", "edit", "create", "grep", "glob", "web_fetch", "SomeFutureTool")):
            with self.subTest(name=name):
                payload = self.tool(name=name, args={"path": str(self.repo / "README.md")}, toolCallId=f"c{i}")
                _, _, entry, _ = self.captured(self.post(payload))
                self.assertEqual(entry["source"]["kind"], "builtin")
                self.assertEqual(entry["tool"], name)

    def test_result_without_text_keeps_the_whole_tool_result(self):
        payload = self.tool(name="view", args={"path": "README.md"}, text=None)
        _, _, entry, _ = self.captured(self.post(payload))
        self.assertEqual(entry["status"], "ok")
        self.assertIn("success", json.dumps(entry["result"]))

    def test_sensitive_call_is_withheld(self):
        payload = self.tool(name="view", args={"path": str(self.home / ".env")}, text="API_TOKEN=SECRET-zz9")
        ctx = self.context(self.post(payload))
        self.assertRegex(ctx, r"^\[evidence:ev_[0-9a-f]{12} withheld: sensitive path \(\.env\*\)\]")
        self.assertNotIn("SECRET-zz9", "".join(p.read_text() for p in self.root.rglob("ev_*.json")))

    def test_failed_tool_call_is_captured_as_an_error(self):
        for error in ("Command failed: exit code 1", {"message": "Command failed: exit code 1", "name": "Error"}):
            with self.subTest(error=error):
                payload = self.payload(toolName="bash", toolArgs={"command": "npm test"}, error=error)
                _, ctx, entry, path = self.captured(self.post(payload, event="postToolUseFailure"))
                self.assertEqual(entry["status"], "error")
                self.assertIn("Command failed", json.dumps(entry["result"]))
                path.unlink()
        # The failure text from a toolResult when there is no `error`.
        payload = self.tool(args={"command": "make"}, text="make: *** [all] Error 2", result_type="failure")
        _, _, entry, _ = self.captured(self.post(payload, event="postToolUseFailure"))
        self.assertEqual(entry["status"], "error")
        # No error text at all: nothing to capture.
        payload = self.payload(toolName="bash", toolArgs={"command": "npm test"})
        self.assertEqual(self.post(payload, event="postToolUseFailure").stdout, "")

    # -- not captured -------------------------------------------------------

    def test_cardinal_gateway_is_skipped(self):
        for name in ("cardinal-lakerunner__list_services", "cardinal-storyboard__find"):
            with self.subTest(name=name):
                res = self.post(self.mcp_payload(toolName=name))
                self.assert_no_capture(res)
                self.assertEqual(res.stdout, "")

    def test_opt_out_env(self):
        for v in ("0", "false", "off", "no"):
            with self.subTest(v=v):
                self.assert_no_capture(self.post(CARDINAL_EVIDENCE_CAPTURE=v))

    def test_opt_out_flag_file(self):
        self.root.mkdir(parents=True)
        (self.root / "disabled").touch()
        self.assert_no_capture(self.post())

    def test_symlinked_spool_is_refused_and_the_hook_fails_open(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (self.home / ".cardinal").mkdir()
        os.symlink(elsewhere, self.root)
        res = self.post()
        self.assertNotIn("[evidence:", res.stdout)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unwritable_spool_fails_open(self):
        (self.home / ".cardinal").mkdir()
        self.root.write_text("not a directory")
        self.assertNotIn("[evidence:", self.post().stdout)

    def test_garbage_stdin_fails_open(self):
        for raw in ("", "not json", "[1,2]", json.dumps({"sessionId": SID, "toolName": 7})):
            with self.subTest(raw=raw):
                self.assert_no_capture(self.post(raw=raw))


if __name__ == "__main__":
    unittest.main()
