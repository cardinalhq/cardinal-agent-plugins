"""Tests for hooks/evidence-capture.py (PostToolUse / PostToolUseFailure on
every tool): the local evidence spool writer, through the generic pipeline
cardinal_core.evidence_capture.

Each test runs the hook as a subprocess with HOME pointed at a temp dir.
Requires cardinal_core vendored: python3 build/vendor.py claude

Run with: python3 -m unittest tests.test_evidence_capture -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOK = PLUGIN_ROOT / "hooks" / "evidence-capture.py"
VENDORED = PLUGIN_ROOT / "hooks" / "cardinal_core" / "evidence.py"
CORE = PLUGIN_ROOT / "hooks" / "cardinal_core"
PLUGIN_VERSION = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
SESSION = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"
EV_ID_RE = re.compile(r"^\[evidence:(ev_[0-9a-f]{12})\]")
WITHHELD_RE = re.compile(r"^\[evidence:(ev_[0-9a-f]{12}) withheld: ([^\]]+)\]")


class EvidenceCaptureHookTests(unittest.TestCase):
    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = self.home / ".cardinal" / "evidence"

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra) -> dict:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(extra)
        return env

    def _payload(self, tool_response=None, **over) -> dict:
        body = {
            "session_id": SESSION,
            "transcript_path": str(self.home / ".claude" / "projects" / "-w" / f"{SESSION}.jsonl"),
            "cwd": str(self.home),
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__grafana__query_prometheus",
            "tool_input": {"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"},
            "tool_response": "series: 3" if tool_response is None else tool_response,
            "tool_use_id": "toolu_01ABC",
        }
        body.update(over)
        return body

    def _run(self, payload=None, raw: str | None = None, env=None, hook: Path = HOOK):
        stdin = raw if raw is not None else json.dumps(payload if payload is not None else self._payload())
        res = subprocess.run([sys.executable, str(hook)], input=stdin, capture_output=True, text=True, timeout=30,
                             env=env or self._env(), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        return res

    def _captured(self, res, event="PostToolUse") -> tuple:
        body = json.loads(res.stdout)
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], event)
        ctx = body["hookSpecificOutput"]["additionalContext"]
        m = EV_ID_RE.match(ctx)
        self.assertIsNotNone(m, ctx)
        ev_id = m.group(1)
        path = self.root / SESSION / f"{ev_id}.json"
        self.assertTrue(path.is_file(), path)
        return ev_id, ctx, json.loads(path.read_text()), path

    def _assert_nothing(self, res):
        self.assertEqual(res.stdout, "")
        self.assertFalse(self.root.exists() and any(self.root.rglob("ev_*.json")))

    # -- never silent: pathological payloads -----------------------------------

    def _withheld(self, res, event="PostToolUse"):
        body = json.loads(res.stdout)
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], event)
        m = WITHHELD_RE.match(body["hookSpecificOutput"]["additionalContext"])
        self.assertIsNotNone(m, body)
        entry = json.loads((self.root / SESSION / f"{m.group(1)}.json").read_text())
        self.assertIsNone(entry["args"])
        self.assertIsNone(entry["result"])
        return entry

    def test_payload_nested_deeper_than_the_parser_is_a_withheld_stub(self):
        d = 1_200_000
        raw = ('{"session_id":"%s","tool_use_id":"toolu_deep","hook_event_name":"PostToolUseFailure",'
               '"tool_name":"mcp__weird__nest","tool_input":{},"error":' % SESSION) + "[" * d + "]" * d + "}"
        entry = self._withheld(self._run(raw=raw), event="PostToolUseFailure")
        self.assertEqual(entry["withheld"]["reason"], "unreadable")
        self.assertEqual(entry["withheld"]["rule"], "depth")
        self.assertEqual(entry["tool"], "nest")
        self.assertEqual(entry["server"], "weird")

    def test_raw_control_characters_in_strings_are_still_captured(self):
        # A raw tab and a raw U+0001 inside JSON strings: not strict JSON.
        raw = ('{"session_id":"%s","tool_use_id":"toolu_ctl","hook_event_name":"PostToolUse",'
               '"tool_name":"AnyTool","tool_input":{"a":"x\ty"},"tool_response":"line1\x01line2"}' % SESSION)
        with self.assertRaises(ValueError):
            json.loads(raw)
        _, _, entry, _ = self._captured(self._run(raw=raw))
        self.assertEqual(entry["tool"], "AnyTool")
        self.assertIn("line2", json.dumps(entry["result"]))

    def test_input_too_large_to_check_in_time_is_a_withheld_stub(self):
        # A very wide input (200k members): the pipeline cannot finish in the
        # hook's budget, so the call is kept as a stub, not dropped.
        wide = {f"k{i}": f"v{i}" for i in range(200_000)}
        t = time.monotonic()
        res = self._run(self._payload(tool_name="FutureBulkTool", tool_input=wide, tool_response=wide))
        self.assertLess(time.monotonic() - t, 2.5)
        entry = self._withheld(res)
        self.assertEqual(entry["withheld"]["reason"], "unreadable")
        self.assertIn(entry["withheld"]["rule"], ("budget", "bounds"))
        self.assertEqual(entry["tool"], "FutureBulkTool")

    # -- payload shapes ------------------------------------------------------

    def test_string_result_is_captured_with_the_exact_context_line(self):
        ev_id, ctx, entry, _ = self._captured(self._run())
        # The first capture of a session explains how to cite; later ones are
        # just the id.
        self.assertTrue(ctx.startswith(f"[evidence:{ev_id}] Cardinal kept this result on this machine."), ctx)
        self.assertIn("cardinal-evidence promote ev_", ctx)
        self.assertIn("cardinal-evidence find", ctx)
        ev2, ctx2, _, _ = self._captured(self._run(self._payload(tool_use_id="toolu_02")))
        self.assertEqual(ctx2, f"[evidence:{ev2}]")
        self.assertNotEqual(ev2, ev_id)
        self.assertEqual(entry["evidence_id"], ev_id)
        self.assertEqual(entry["schema"], "cardinal.evidence.v2")
        self.assertEqual(entry["tier"], "captured")
        self.assertEqual(entry["source"], {"kind": "mcp", "server": "grafana", "runtime": "claude-code"})
        self.assertEqual(entry["server"], "grafana")
        self.assertEqual(entry["tool"], "query_prometheus")
        self.assertEqual(entry["tool_name"], "mcp__grafana__query_prometheus")
        self.assertEqual(entry["session_id"], SESSION)
        self.assertEqual(entry["tool_use_id"], "toolu_01ABC")
        self.assertEqual(entry["client"], f"claude-code/{PLUGIN_VERSION}")
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["normalizer"], "mcp-content")
        self.assertEqual(entry["args"], {"expr": "rate(http_requests_total[5m])", "datasourceUid": "prom"})
        self.assertEqual(entry["result"], {"text": ["series: 3"]})
        self.assertIs(entry["truncated"], False)
        self.assertRegex(entry["called_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")

    def test_content_and_structured_content(self):
        sc = {"series": [{"metric": {"job": "api"}, "values": [[1, "0.5"]]}]}
        res = self._run(self._payload(tool_response={"content": json.dumps(sc), "structuredContent": sc}))
        _, _, entry, _ = self._captured(res)
        self.assertEqual(entry["result"], {"structured": sc})
        blocks = [{"type": "text", "text": "hello"}, {"type": "image", "data": "iVBOR", "mimeType": "image/png"}]
        _, _, entry, _ = self._captured(self._run(self._payload(tool_response=blocks)))
        self.assertEqual(entry["result"], {"text": ["hello"], "other_blocks": 1})

    def test_spill_notice_is_followed_only_under_claude_projects(self):
        spill = self.home / ".claude" / "projects" / "-w" / SESSION / "tool-results" / "mcp-grafana-1.txt"
        spill.parent.mkdir(parents=True)
        spill.write_text('{"series":[1,2,3],"password":"LEAK-SPILL"}')
        notice = (f"Error: result (91,000 characters) exceeds maximum allowed tokens. Output has been saved to "
                  f"{spill}.\nUse offset and limit parameters to read specific portions of the file.")
        _, _, entry, _ = self._captured(self._run(self._payload(tool_response=notice)))
        self.assertIs(entry["spilled"], True)
        self.assertEqual(entry["result"], {"structured": {"password": "[redacted]", "series": [1, 2, 3]}})
        # A notice naming a file elsewhere is stored as the notice, unread.
        outside = self.home / "private" / "keys.txt"
        outside.parent.mkdir()
        outside.write_text("TOP-SECRET-FILE")
        notice = f"Output has been saved to {outside}."
        _, _, entry, path = self._captured(self._run(self._payload(tool_response=notice)))
        self.assertNotIn("spilled", entry)
        self.assertNotIn("TOP-SECRET-FILE", path.read_text())

    def test_large_spilled_pod_list_keeps_env_pair_and_header_secrets_off_disk(self):
        # A kube MCP's `get pods -o json` easily passes the 2 MiB structural
        # limit; the stored prefix must still get the {name, value} pair and
        # headers rules, not just the plain-text ones.
        pod = {"metadata": {"name": "api-0", "annotations": {"note": "x" * 200}},
               "spec": {"containers": [{"name": "app",
                                        "env": [{"name": "DB_PASSWORD", "value": "hunter2-plaintext"},
                                                {"name": "LOG_LEVEL", "value": "info"}],
                                        "probe": {"headers": {"X-Tenant": "tenant-secret-zzz"}}}]}}
        spill = self.home / ".claude" / "projects" / "-w" / SESSION / "tool-results" / "mcp-kube-1.txt"
        spill.parent.mkdir(parents=True)
        spill.write_text(json.dumps({"kind": "PodList", "items": [pod] * 8000}))
        size = spill.stat().st_size
        self.assertGreater(size, 2 << 20)
        notice = f"Error: result exceeds maximum allowed tokens. Output has been saved to {spill}.\n"
        _, _, entry, path = self._captured(self._run(self._payload(tool_response=notice,
                                                                   tool_name="mcp__kube__list_pods")))
        stored = path.read_text()
        self.assertNotIn("hunter2-plaintext", stored)
        self.assertNotIn("tenant-secret-zzz", stored)
        self.assertIn("LOG_LEVEL", stored)
        self.assertIs(entry["truncated"], True)
        self.assertEqual(entry["spilled_bytes"], size)
        self.assertLessEqual(len(json.dumps(entry["result"]).encode("utf-8")), 300 * 1024)

    def test_structured_content_serialized_as_text_is_kept_structured(self):
        # How Claude Code hands a hook a tool's structuredContent
        # (server-everything get-structured-content, as captured live).
        text = '{"temperature":36,"conditions":"Light rain / drizzle","humidity":82}'
        _, _, entry, _ = self._captured(self._run(self._payload(tool_response=text)))
        self.assertEqual(entry["result"], {"structured": json.loads(text)})

    def test_image_and_blob_placeholders_keep_no_local_path(self):
        saved = self.home / ".claude" / "projects" / "-w" / SESSION / "tool-results"
        blocks = [{"type": "text", "text": "Here's the image you requested:"},
                  {"type": "text", "text": f"[Image source: {saved}/mcp-x-blob-1.png]"}]
        _, _, entry, path = self._captured(self._run(self._payload(tool_response=blocks)))
        self.assertEqual(entry["result"], {"text": ["Here's the image you requested:", "[Image source: [local file]]"]})
        self.assertNotIn(str(self.home), path.read_text())

    def test_hostile_text_is_still_captured(self):
        # Lone surrogates arrive as JSON escapes; NULs and control characters
        # as themselves. None of them may cost the capture.
        raw = json.dumps(self._payload(tool_response=[{"type": "text", "text": "PLACEHOLDER"}]))
        raw = raw.replace("PLACEHOLDER", "lone:\\udcff\\udc80 nul:\\u0000 esc:\\u001b[31m rtl:\\u202e ok")
        _, _, entry, path = self._captured(self._run(raw=raw))
        self.assertEqual(entry["result"], {"text": ["lone:\ufffd\ufffd nul:\ufffd esc:\x1b[31m rtl:\u202e ok"]})
        path.read_bytes().decode("utf-8")

    def test_pem_private_key_never_reaches_disk(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowLEAKIBAAKCAQEA7\n-----END RSA PRIVATE KEY-----"
        _, _, entry, path = self._captured(self._run(self._payload(tool_response={"structuredContent": {"pem": pem}})))
        self.assertNotIn("LEAK", path.read_text())
        self.assertEqual(entry["result"], {"structured": {"pem": "[redacted]"}})

    # -- error results (PostToolUseFailure) --------------------------------------

    def _failure(self, **over) -> dict:
        body = self._payload(hook_event_name="PostToolUseFailure",
                             error="upstream 403 Forbidden: rejected api_key=sk-ant-api03-LEAKabcdefghijklmnop"
                                   "qrstuvwxyz0123 password=hunter2 for tenant acme",
                             is_interrupt=False)
        body.pop("tool_response")
        body.update(over)
        return body

    def test_error_result_is_captured_and_marked(self):
        _, ctx, entry, path = self._captured(self._run(self._failure()), "PostToolUseFailure")
        self.assertIs(entry["is_error"], True)
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["result"], {"text": ["upstream 403 Forbidden: rejected api_key=[redacted] "
                                                    "password=[redacted] for tenant acme"]})
        self.assertNotIn("LEAK", path.read_text())
        # A JSON error body stays the error's text, redacted as error text.
        res = self._run(self._failure(error='{"error":"auth failed: token=LEAK-T","code":401}'))
        m = EV_ID_RE.match(json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"])
        entry = json.loads((self.root / SESSION / f"{m.group(1)}.json").read_text())
        self.assertEqual(list(entry["result"]), ["text"])
        self.assertNotIn("LEAK", json.dumps(entry))

    def test_interrupts_empty_errors_cardinal_tools_and_other_events_are_skipped(self):
        self._assert_nothing(self._run(self._failure(is_interrupt=True)))
        self._assert_nothing(self._run(self._failure(error="")))
        self._assert_nothing(self._run(self._failure(error=None)))
        self._assert_nothing(self._run(self._failure(tool_name="mcp__plugin_cardinal_cardinal__lakerunner__x")))
        self._assert_nothing(self._run(self._payload(hook_event_name="PreToolUse")))

    # -- skip list: Cardinal's own gateway only --------------------------------

    def test_only_cardinal_gateway_tools_are_skipped(self):
        for name in ("mcp__cardinal__lakerunner__execute_logs_query",
                     "mcp__plugin_cardinal_cardinal__lakerunner__execute_logs_query",
                     "mcp__plugin_cardinal_cardinal__storyboard__preview", ""):
            self._assert_nothing(self._run(self._payload(tool_name=name)))
        # A server merely named like Cardinal is not Cardinal's gateway.
        _, _, entry, _ = self._captured(self._run(self._payload(tool_name="mcp__cardinal-dev__query")))
        self.assertEqual(entry["server"], "cardinal-dev")
        # Names that are not mcp__<server>__<tool> are the runtime's own tools.
        for i, name in enumerate(("Bash", "Read", "mcp__", "mcp__grafana")):
            _, _, entry, _ = self._captured(self._run(self._payload(tool_name=name, tool_use_id=f"toolu_n{i}")))
            self.assertEqual(entry["source"]["kind"], "builtin")
            self.assertEqual(entry["server"], "builtin:claude-code")
            self.assertEqual(entry["tool"], name)

    # -- every tool: built-in, unknown ---------------------------------------------

    def test_bash_result_is_captured_with_its_shape(self):
        payload = self._payload(tool_name="Bash", tool_input={"command": "make check-maestro", "description": "x"},
                                tool_response={"stdout": "ok 42 passed\nDB_PASSWORD=hunter2\n", "stderr": "",
                                               "interrupted": False, "isImage": False,
                                               "noOutputExpected": False})
        ev_id, _, entry, path = self._captured(self._run(payload))
        self.assertEqual(entry["server"], "builtin:claude-code")
        self.assertEqual(entry["tool"], "Bash")
        self.assertEqual(entry["normalizer"], "shell")
        self.assertEqual(entry["summary"], "make check-maestro")
        self.assertEqual(entry["result"], {"structured": {"stdout": "ok 42 passed\nDB_PASSWORD=[redacted]\n",
                                                          "stderr": ""}})
        self.assertNotIn("hunter2", path.read_text())
        self.assertNotIn("exit_code", entry)

    def test_failed_bash_records_its_exit_code(self):
        body = self._payload(tool_name="Bash", tool_input={"command": "make test"}, hook_event_name="PostToolUseFailure",
                             error="Exit code 2\nFAIL src/a.test.ts", is_interrupt=False)
        body.pop("tool_response")
        _, _, entry, _ = self._captured(self._run(body), "PostToolUseFailure")
        self.assertEqual(entry["status"], "error")
        self.assertEqual(entry["exit_code"], 2)
        self.assertEqual(entry["result"], {"structured": {"exit_code": 2, "output": "FAIL src/a.test.ts"}})

    def test_read_edit_write_and_an_unknown_tool_are_captured(self):
        cwd = str(self.home / "repo")
        write = {"type": "create", "filePath": f"{cwd}/notes.md", "content": "# hi\n",
                 "structuredPatch": [{"lines": ["+# hi"]}], "originalFile": None, "userModified": False}
        _, _, entry, _ = self._captured(self._run(self._payload(
            tool_name="Write", cwd=cwd, tool_use_id="t_w",
            tool_input={"file_path": f"{cwd}/notes.md", "content": "# hi\n"}, tool_response=write)))
        # Write's `content` key is not MCP content: the patch is the evidence.
        self.assertEqual(entry["normalizer"], "file-edit")
        self.assertEqual(entry["result"]["structured"]["file_path"], "./notes.md")
        self.assertEqual(entry["result"]["structured"]["patch"], [{"lines": ["+# hi"]}])
        read = {"type": "text", "file": {"filePath": f"{cwd}/a.py", "content": "print(1)\n", "numLines": 1}}
        _, _, entry, _ = self._captured(self._run(self._payload(
            tool_name="Read", cwd=cwd, tool_use_id="t_r", tool_input={"file_path": f"{cwd}/a.py"},
            tool_response=read)))
        self.assertEqual(entry["normalizer"], "generic")
        self.assertEqual(entry["result"]["structured"]["file"]["content"], "print(1)\n")
        self.assertEqual(entry["args"], {"file_path": "./a.py"})
        _, _, entry, _ = self._captured(self._run(self._payload(
            tool_name="FrobnicateWidgets_v9", tool_use_id="t_u", tool_input={"x": [1, {"y": "z"}]},
            tool_response={"weird": {"n": 1}})))
        self.assertEqual(entry["source"], {"kind": "builtin", "runtime": "claude-code"})
        self.assertEqual(entry["normalizer"], "generic")
        self.assertEqual(entry["result"], {"structured": {"weird": {"n": 1}}})

    def test_sensitive_call_is_a_withheld_stub(self):
        secret = "SECRET-VALUE-zz9"
        payload = self._payload(tool_name="Read", tool_use_id="t_env", cwd=str(self.home),
                                tool_input={"file_path": str(self.home / ".env")},
                                tool_response={"type": "text", "file": {"content": f"API_TOKEN={secret}"}})
        res = self._run(payload)
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        m = WITHHELD_RE.match(ctx)
        self.assertIsNotNone(m, ctx)
        self.assertEqual(m.group(2), "sensitive path (.env*)")
        path = self.root / SESSION / f"{m.group(1)}.json"
        entry = json.loads(path.read_text())
        self.assertEqual(entry["withheld"], {"reason": "sensitive_path", "rule": "path.dotenv", "hint": ".env*"})
        self.assertIsNone(entry["args"])
        self.assertIsNone(entry["result"])
        self.assertNotIn(secret, path.read_text())
        self.assertNotIn(".env\"", path.read_text().replace('"hint":".env*"', ""))
        res = self._run(self._payload(tool_name="Bash", tool_use_id="t_tok",
                                      tool_input={"command": "echo $(gh auth token)"},
                                      tool_response={"stdout": "gho_" + "x" * 36, "stderr": ""}))
        self.assertRegex(json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"],
                         r"withheld: secret command \(gh auth token\)")

    def test_context_lines_can_be_turned_off_but_capture_continues(self):
        res = self._run(env=self._env(CARDINAL_EVIDENCE_CONTEXT="0"))
        self.assertEqual(res.stdout, "")
        self.assertEqual(len(list(self.root.rglob("ev_*.json"))), 1)

    # -- opt-out ---------------------------------------------------------------

    def test_env_opt_out(self):
        for v in ("0", "false", "off"):
            self._assert_nothing(self._run(env=self._env(CARDINAL_EVIDENCE_CAPTURE=v)))
        self._captured(self._run(env=self._env(CARDINAL_EVIDENCE_CAPTURE="1")))

    def test_flag_file_opt_out(self):
        self.root.mkdir(parents=True)
        (self.root / "disabled").touch()
        self._assert_nothing(self._run())

    # -- permissions, scrub, GC ----------------------------------------------

    def test_spool_is_private(self):
        _, _, _, path = self._captured(self._run())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root.parent).st_mode), 0o700)

    def test_credentials_never_reach_disk(self):
        payload = self._payload(
            tool_input={"query": "select 1", "api_key": "LEAK-ARG", "headers": {"X-Tenant": "LEAKx1"}},
            tool_response={"content": "conn postgres://admin:LEAK-PW@db:5432/app ok",
                           "structuredContent": {"rows": [{"user": "bob", "password_hash": "LEAK-HASH"}],
                                                 "token": "ghp_" + "L" * 36}})
        _, _, entry, path = self._captured(self._run(payload))
        self.assertNotIn("LEAK", path.read_text())
        self.assertEqual(entry["args"]["api_key"], "[redacted]")
        self.assertEqual(entry["args"]["headers"], {"X-Tenant": "[redacted]"})
        # A request that carries a credential header is withheld outright:
        # what it returned is private too.
        res = self._run(self._payload(tool_use_id="t_auth", tool_input={"headers": {"Authorization": "Bearer LEAKx1"}}))
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertRegex(ctx, r"withheld: credential in a request")
        self.assertEqual(entry["result"]["text"], ["conn postgres://[redacted]@db:5432/app ok"])

    def test_large_result_is_capped_with_a_truncation_marker(self):
        big = {"rows": [{"i": i, "msg": "x" * 100} for i in range(5000)]}
        _, _, entry, path = self._captured(self._run(self._payload(tool_response={"structuredContent": big})))
        self.assertIs(entry["truncated"], True)
        self.assertIs(entry["result"]["truncated"], True)
        self.assertGreater(entry["result"]["original_bytes"], 256 * 1024)
        self.assertLessEqual(len(json.dumps(entry["result"], separators=(",", ":")).encode()), 256 * 1024)
        self.assertLess(path.stat().st_size, 300 * 1024)

    def test_expired_entries_are_collected(self):
        old = self.root / "old-session" / "ev_0123456789ab.json"
        old.parent.mkdir(parents=True)
        old.write_text("{}")
        t = time.time() - 15 * 24 * 3600
        os.utime(old, (t, t))
        self._captured(self._run())
        self.assertFalse(old.exists())
        self.assertFalse(old.parent.exists())

    # -- fail open -------------------------------------------------------------

    def test_fails_open(self):
        for raw in ("", "not json", "[]", "42", '{"tool_name": 7}'):
            self.assertEqual(self._run(raw=raw).stdout, "")
        # A read-only home: nothing written, nothing printed, exit 0.
        ro = self.home / "ro"
        ro.mkdir()
        os.chmod(ro, 0o500)
        try:
            self.assertEqual(self._run(env=self._env(HOME=str(ro))).stdout, "")
        finally:
            os.chmod(ro, 0o700)
        # The spool root cannot be created (a file sits where ~/.cardinal is).
        (self.home / ".cardinal").write_text("in the way")
        self.assertEqual(self._run().stdout, "")
        (self.home / ".cardinal").unlink()
        # A tool_response json cannot express as-is is still handled.
        self._captured(self._run(self._payload(tool_response={"content": [{"type": "text"}, None, 3]})))

    def test_fails_open_without_a_vendored_core(self):
        probe = subprocess.run([sys.executable, "-c", "import cardinal_core"], cwd=str(self.home),
                               env=self._env(), capture_output=True)
        if probe.returncode == 0:
            self.skipTest("cardinal_core is installed site-wide; cannot simulate a missing core")
        fake = self.home / "plugin" / "hooks"
        fake.mkdir(parents=True)
        shutil.copy(HOOK, fake / HOOK.name)
        self._assert_nothing(self._run(hook=fake / HOOK.name))

    def test_no_network_code(self):
        core = [CORE / "evidence_capture.py", CORE / "evidence_gate.py"] + sorted(
            (CORE / "evidence_normalizers").glob("*.py"))
        for src in [HOOK, VENDORED] + core:
            text = src.read_text()
            for mod in ("urllib", "http.client", "socket", "requests", "subprocess"):
                self.assertNotRegex(text, rf"^\s*(import|from)\s+{re.escape(mod)}\b", f"{src.name} imports {mod}")

    # -- registration ----------------------------------------------------------

    def test_registered_synchronously_on_every_tool_with_a_short_timeout(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        for event in ("PostToolUse", "PostToolUseFailure"):
            with self.subTest(event=event):
                self._check_registration(hooks[event])

    def _check_registration(self, groups):
        found = [(g["matcher"], h) for g in groups for h in g["hooks"] if "evidence-capture.py" in h["command"]]
        self.assertEqual(len(found), 1)
        matcher, entry = found[0]
        self.assertEqual(matcher, ".*")
        self.assertEqual(entry["command"], "${CLAUDE_PLUGIN_ROOT}/hooks/evidence-capture.py")
        self.assertFalse(entry.get("async"), "additionalContext is dropped from async hooks")
        self.assertIsInstance(entry.get("timeout"), int)
        self.assertLessEqual(entry["timeout"], 2)
        for name in ("mcp__grafana__query_prometheus", "Bash", "Agent", "Edit", "WebFetch", "SomeFutureTool"):
            self.assertRegex(name, "^(?:" + matcher + ")$")
        self.assertTrue(os.access(HOOK, os.X_OK))


if __name__ == "__main__":
    unittest.main()
