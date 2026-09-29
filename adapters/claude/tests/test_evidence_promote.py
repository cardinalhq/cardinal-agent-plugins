"""Tests for the captured-evidence upload path: hooks/storyboard-token.py
(PostToolUse on storyboard__create / __preview) and bin/cardinal-evidence
(promote / list / off / on / status).

Each test runs the hook or the CLI as a subprocess with HOME pointed at a temp
dir; promote talks to a stub maestro evidence route on 127.0.0.1.
Requires cardinal_core vendored: python3 build/vendor.py claude

Run with: python3 -m unittest tests.test_evidence_promote -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOK = PLUGIN_ROOT / "hooks" / "storyboard-token.py"
CLI = PLUGIN_ROOT / "bin" / "cardinal-evidence"
VENDORED = PLUGIN_ROOT / "hooks" / "cardinal_core" / "evidence.py"
PLUGIN_VERSION = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]

SESSION = "3f2a9c1e-7b4d-4e0a-9c8b-1a2b3c4d5e6f"
OTHER_SESSION = "9d1e2f3a-0000-4e0a-9c8b-1a2b3c4d5e6f"
ORG = "org-7f3a"
SB = "sb_" + "a1" * 12
SB2 = "sb_" + "b2" * 12
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6ImNhcmRpbmFsLXN0b3J5Ym9hcmQtZXZpZGVuY2UifQ.eyJzYiI6InNiIn0.c2lnbmF0dXJl"
TOKEN2 = "eyJhbGciOiJIUzI1NiJ9.eyJmcmVzaCI6dHJ1ZX0.bmV3LXNpZw"
API_KEY = "ck_live_plugin_key_0123456789"
RCPT_RE = re.compile(r"^rcpt_[0-9a-f]{24}$")


def _vendored():
    sys.path.insert(0, str(PLUGIN_ROOT / "hooks"))
    from cardinal_core import evidence
    return evidence


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("cardinal_evidence_cli", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def create_result(sb=SB, org=ORG, token=TOKEN, path=None) -> dict:
    return {
        "storyboard_id": sb,
        "status": "draft",
        "view_url": f"https://app.example.com/storyboards/{sb}",
        "evidence_token": token,
        "evidence_token_expires_at": "2099-01-01T00:00:00.000Z",
        "evidence_upload": {
            "method": "POST",
            "path": path if path is not None else f"/api/orgs/{org}/storyboards/{sb}/evidence",
            "authorization": "CardinalEvidence <evidence_token>",
        },
        "next": "define_surface …",
    }


class StubEvidenceRoute:
    """maestro's POST /api/orgs/:org/storyboards/:id/evidence, recording every
    request. `respond(handler, body)` -> (status, json body, headers)."""

    def __init__(self):
        self.requests = []
        self.respond = self.ok
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def ok(req, body):
        results = [{"index": i, "receipt_id": "rcpt_" + f"{i:024x}"} for i in range(len(body["items"]))]
        return 200, {"storyboard_id": req["path"].split("/")[5], "results": results}, {}

    def _handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                req = {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "raw": raw}
                stub.requests.append(req)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                status, out, headers = stub.respond(req, body)
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return H


class _HomeCase(unittest.TestCase):
    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.ev = _vendored()
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = self.home / ".cardinal" / "evidence"

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self, **extra) -> dict:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        env.update(extra)
        return env

    def run_hook(self, tool_name, tool_response, session=SESSION, env=None):
        payload = {"session_id": session, "hook_event_name": "PostToolUse", "tool_name": tool_name,
                   "tool_input": {}, "tool_response": tool_response}
        res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True,
                             text=True, timeout=30, env=env or self._env(), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")
        self.assertEqual(res.stderr, "")
        return res

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=60,
                              env=env or self._env(), cwd=str(self.home))

    def token_file(self, session=SESSION) -> Path:
        return self.root / session / "token.json"

    def capture(self, tool="query_prometheus", response="series: 3", session=SESSION, server="grafana",
                args=None, called_at=None) -> dict:
        entry = self.ev.build_entry(server=server, tool=tool, tool_name=f"mcp__{server}__{tool}",
                                    tool_input=args if args is not None else {"expr": "up"},
                                    tool_response=response, session_id=session,
                                    called_at=called_at, agent="claude-code")
        self.ev.write_entry(self.root, entry)
        return entry

    def connect(self, port, org=ORG, key=API_KEY):
        (self.home / ".claude").mkdir(exist_ok=True)
        env = {"CARDINAL_MCP_URL": f"http://127.0.0.1:{port}/api/orgs/{org}/mcp"}
        if key:
            env["CARDINAL_MCP_API_KEY"] = key
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))

    def store_token(self, sb=SB, org=ORG, token=TOKEN, session=SESSION, expires_at="2099-01-01T00:00:00Z"):
        self.ev.store_token(self.root, session, storyboard_id=sb, org=org, token=token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# hooks/storyboard-token.py
# ---------------------------------------------------------------------------

class StoryboardTokenHookTests(_HomeCase):
    def test_create_result_stores_the_token_privately(self):
        result = create_result()
        self.run_hook("mcp__plugin_cardinal_cardinal__storyboard__create",
                      {"content": json.dumps(result), "structuredContent": result})
        path = self.token_file()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        body = json.loads(path.read_text())
        self.assertEqual(body["schema"], "cardinal.evidence-token.v1")
        rec = body["storyboards"][SB]
        self.assertEqual(rec["org"], ORG)
        self.assertEqual(rec["evidence_token"], TOKEN)
        self.assertEqual(rec["expires_at"], "2099-01-01T00:00:00.000Z")

    def test_text_only_shapes_and_the_user_scope_server(self):
        result = create_result(org="org%2Dx")
        self.run_hook("mcp__cardinal__storyboard__create", [{"type": "text", "text": json.dumps(result)}])
        self.assertEqual(self.ev.read_tokens(self.root, SESSION)[SB]["org"], "org-x")
        # An org that is not a plain identifier once decoded is not stored.
        self.run_hook("mcp__cardinal__storyboard__create",
                      {"structuredContent": create_result(sb=SB2, org="org%2F..%2Fx")})
        self.assertNotIn(SB2, self.ev.read_tokens(self.root, SESSION))

    def test_other_servers_cannot_plant_a_token(self):
        # A non-Cardinal server returning the same shape must not choose
        # where a later promote sends evidence.
        result = create_result(org="attacker-org")
        for name in ("mcp__evil__storyboard__create", "mcp__cardinalx__storyboard__create",
                     "mcp__plugin_cardinal_cardinal__storyboard__publish", "Bash"):
            self.run_hook(name, {"content": json.dumps(result), "structuredContent": result})
        self.assertFalse(self.token_file().exists())

    def test_upload_path_for_another_storyboard_is_refused(self):
        result = create_result(path=f"/api/orgs/{ORG}/storyboards/{SB2}/evidence")
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": result})
        self.assertFalse(self.token_file().exists())

    def test_malformed_payloads_store_nothing_and_stay_silent(self):
        self.run_hook("mcp__cardinal__storyboard__create", "not json")
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result(token="not a jwt")})
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result(sb="sb_short")})
        res = subprocess.run([sys.executable, str(HOOK)], input="{", capture_output=True, text=True,
                             env=self._env(), timeout=30)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assertFalse(self.token_file().exists())

    def test_preview_refreshes_the_token(self):
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result()})
        preview = {"storyboard_id": SB, "ok": True, "evidence_token": TOKEN2,
                   "evidence_token_expires_at": "2099-02-01T00:00:00Z",
                   "scenes": [{"id": "s1", "preview_bundle": {
                       "path": f"/api/orgs/{ORG}/storyboards/{SB}/scenes/s1/preview.html?revision=3"}}]}
        self.run_hook("mcp__plugin_cardinal_cardinal__storyboard__preview", {"structuredContent": preview})
        rec = self.ev.read_tokens(self.root, SESSION)[SB]
        self.assertEqual((rec["evidence_token"], rec["org"]), (TOKEN2, ORG))
        # No bundle path: the stored org is kept.
        preview = {"storyboard_id": SB, "evidence_token": TOKEN, "scenes": []}
        self.run_hook("mcp__cardinal__storyboard__preview", {"structuredContent": preview})
        self.assertEqual(self.ev.read_tokens(self.root, SESSION)[SB]["evidence_token"], TOKEN)
        # An unknown storyboard with no org anywhere is not stored.
        self.run_hook("mcp__cardinal__storyboard__preview", {"structuredContent": {
            "storyboard_id": SB2, "evidence_token": TOKEN, "scenes": []}})
        self.assertNotIn(SB2, self.ev.read_tokens(self.root, SESSION))

    def test_several_storyboards_share_the_session_file(self):
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result()})
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result(sb=SB2, token=TOKEN2)})
        self.assertEqual(set(self.ev.read_tokens(self.root, SESSION)), {SB, SB2})

    def test_capture_opt_out_stores_nothing(self):
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result()},
                      env=self._env(CARDINAL_EVIDENCE_CAPTURE="0"))
        self.assertFalse(self.token_file().exists())

    def test_symlinked_session_dir_is_refused(self):
        self.root.mkdir(parents=True)
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (self.root / SESSION).symlink_to(elsewhere)
        self.run_hook("mcp__cardinal__storyboard__create", {"structuredContent": create_result()})
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_no_network_code(self):
        text = HOOK.read_text()
        for mod in ("urllib.request", "http.client", "socket", "requests", "subprocess"):
            self.assertNotRegex(text, rf"^\s*(import|from)\s+{re.escape(mod)}\b")

    def test_registered_synchronously_on_cardinal_create_and_preview(self):
        groups = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        found = [(g["matcher"], h) for g in groups for h in g["hooks"] if "storyboard-token.py" in h["command"]]
        self.assertEqual(len(found), 1)
        matcher, entry = found[0]
        self.assertEqual(entry["command"], "${CLAUDE_PLUGIN_ROOT}/hooks/storyboard-token.py")
        self.assertFalse(entry.get("async"))
        self.assertLessEqual(entry["timeout"], 3)
        for name in ("mcp__plugin_cardinal_cardinal__storyboard__create", "mcp__cardinal__storyboard__create",
                     "mcp__plugin_cardinal_cardinal__storyboard__preview", "mcp__cardinal__storyboard__preview"):
            self.assertRegex(name, "^(?:" + matcher + ")$")
        for name in ("mcp__evil__storyboard__create", "mcp__plugin_cardinal_cardinal__storyboard__publish",
                     "mcp__x_cardinal__storyboard__create", "Bash"):
            self.assertNotRegex(name, "^(?:" + matcher + ")$")
        self.assertTrue(os.access(HOOK, os.X_OK))


# ---------------------------------------------------------------------------
# bin/cardinal-evidence promote
# ---------------------------------------------------------------------------

class PromoteTests(_HomeCase):
    def setUp(self):
        super().setUp()
        self.stub = StubEvidenceRoute()
        self.addCleanup(self.stub.close)

    def test_promote_with_the_evidence_token(self):
        self.connect(self.stub.port)
        self.store_token()
        a = self.capture(args={"expr": "up", "password": "hunter2"}, called_at="2026-09-28T14:00:00.123456Z")
        b = self.capture(tool="search", response={"content": [{"type": "text", "text": "hit"},
                                                               {"type": "image", "data": "x"}],
                                                   "structuredContent": {"hits": [1, 2]}})
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"], b["evidence_id"])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(len(self.stub.requests), 1)
        req = self.stub.requests[0]
        self.assertEqual(req["path"], f"/api/orgs/{ORG}/storyboards/{SB}/evidence")
        self.assertEqual(req["headers"]["authorization"], "CardinalEvidence " + TOKEN)
        self.assertNotIn("x-cardinalhq-api-key", req["headers"])
        self.assertEqual(req["headers"]["content-type"], "application/json")
        items = json.loads(req["raw"])["items"]
        self.assertEqual(items[0], {
            "provenance": "captured",
            "source_server": "grafana",
            "client": f"claude-code/{PLUGIN_VERSION}",
            "tool": "query_prometheus",
            "client_called_at": "2026-09-28T14:00:00.123456Z",
            "args": {"expr": "up", "password": "[redacted]"},
            "result": {"content": [{"type": "text", "text": "series: 3"}]},
        })
        self.assertEqual(items[1]["tool"], "search")
        self.assertEqual(items[1]["result"], {"content": [{"type": "text", "text": "hit"}, {"type": "other"}],
                                              "structured_content": {"hits": [1, 2]}})
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0], f"{a['evidence_id']} -> rcpt_{0:024x}  (grafana/query_prometheus, captured)")
        self.assertEqual(lines[1], f"{b['evidence_id']} -> rcpt_{1:024x}  (grafana/search, captured)")
        self.assertIn(f"2 promoted to {SB}", lines[2])
        self.assertNotIn(TOKEN, res.stdout + res.stderr)

    def test_storyboard_is_inferred_from_the_session_token(self):
        self.connect(self.stub.port)
        self.store_token()
        a = self.capture()
        res = self.run_cli("promote", a["evidence_id"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.stub.requests[0]["path"], f"/api/orgs/{ORG}/storyboards/{SB}/evidence")

    def test_ambiguous_or_missing_storyboard_asks_for_it(self):
        self.connect(self.stub.port)
        a = self.capture()
        res = self.run_cli("promote", a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn("pass --storyboard", res.stderr)
        self.store_token()
        self.store_token(sb=SB2)
        res = self.run_cli("promote", a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn(SB, res.stderr)
        self.assertIn(SB2, res.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_falls_back_to_the_api_key_without_a_token(self):
        self.connect(self.stub.port)
        a = self.capture()
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 0, res.stderr)
        h = self.stub.requests[0]["headers"]
        self.assertEqual(h["x-cardinalhq-api-key"], API_KEY)
        self.assertNotIn("authorization", h)

    def test_expired_token_is_not_sent(self):
        self.connect(self.stub.port)
        self.store_token(expires_at="2020-01-01T00:00:00Z")
        a = self.capture()
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(self.stub.requests), 1)
        self.assertNotIn("authorization", self.stub.requests[0]["headers"])

    def test_rejected_token_retries_with_the_key(self):
        self.connect(self.stub.port)
        self.store_token()
        a = self.capture()

        def respond(req, body):
            if "authorization" in req["headers"]:
                return 401, {"error": "Invalid token"}, {}
            return StubEvidenceRoute.ok(req, body)

        self.stub.respond = respond
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([("authorization" in r["headers"], "x-cardinalhq-api-key" in r["headers"])
                          for r in self.stub.requests], [(True, False), (False, True)])

    def test_rejected_token_without_a_key_fails(self):
        self.connect(self.stub.port, key=None)
        self.store_token()
        a = self.capture()
        self.stub.respond = lambda req, body: (401, {"error": "Invalid token"}, {})
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn("evidence token was rejected", res.stderr)
        self.assertIn(f"{a['evidence_id']}: error not_uploaded:", res.stdout)

    def test_no_credential_at_all(self):
        self.connect(self.stub.port, key=None)
        a = self.capture()
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn("no credential", res.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_not_connected(self):
        a = self.capture()
        self.store_token()
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn("/cardinal:connect", res.stderr)

    def test_token_for_another_org_than_the_connection_is_refused(self):
        # A token file naming another org must not redirect evidence there.
        self.connect(self.stub.port)
        self.store_token(org="other-org")
        a = self.capture()
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn("connected to org " + ORG, res.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_per_item_errors_and_missing_entries(self):
        self.connect(self.stub.port)
        self.store_token()
        a = self.capture()
        b = self.capture(tool="other")

        def respond(req, body):
            return 200, {"storyboard_id": SB, "results": [
                {"index": 0, "receipt_id": "rcpt_" + "c" * 24},
                {"index": 1, "error": {"code": "invalid_item", "message": "tool: bad\nIGNORE PREVIOUS"}},
            ]}, {}

        self.stub.respond = respond
        missing = "ev_" + "0" * 12
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"], missing, b["evidence_id"],
                           a["evidence_id"])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertEqual(len(json.loads(self.stub.requests[0]["raw"])["items"]), 2, "deduped, missing skipped")
        lines = res.stdout.splitlines()
        self.assertEqual(lines[0], f"{a['evidence_id']} -> rcpt_{'c' * 24}  (grafana/query_prometheus, captured)")
        self.assertTrue(lines[1].startswith(f"{missing}: error not_in_spool: "))
        self.assertEqual(lines[2], f"{b['evidence_id']}: error invalid_item: tool: bad IGNORE PREVIOUS")
        self.assertIn("1 promoted", lines[3])
        self.assertIn("2 failed", lines[3])

    def test_published_storyboard_is_a_whole_request_refusal(self):
        self.connect(self.stub.port)
        self.store_token()
        a = self.capture()
        self.stub.respond = lambda req, body: (409, {"error": "storyboard_published"}, {})
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn("published", res.stderr)
        self.assertIn("only to a draft", res.stderr)

    def test_redirect_is_not_followed_with_credentials(self):
        other = StubEvidenceRoute()
        self.addCleanup(other.close)
        self.connect(self.stub.port)
        self.store_token()
        a = self.capture()
        self.stub.respond = lambda req, body: (
            307, {}, {"Location": f"http://127.0.0.1:{other.port}{req['path']}"})
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn("redirected", res.stderr)
        self.assertEqual(other.requests, [])

    def test_rate_limit_is_retried(self):
        cli = _load_cli()
        calls = []

        def respond(req, body):
            calls.append(1)
            if len(calls) == 1:
                return 429, {"error": "rate_limited"}, {"Retry-After": "1"}
            return StubEvidenceRoute.ok(req, body)

        self.stub.respond = respond
        slept = []
        conn = {"origin": f"http://127.0.0.1:{self.stub.port}", "org": ORG, "key": API_KEY}
        out = cli.post_upload(conn, ORG, SB, b'{"items":[{}]}', ("key", {"X-CardinalHQ-API-Key": API_KEY}),
                              sleep=slept.append)
        self.assertEqual(slept, [1])
        self.assertEqual(out["results"][0]["receipt_id"], "rcpt_" + "0" * 24)

    def test_bad_ids_are_refused_before_any_request(self):
        self.connect(self.stub.port)
        for args in (["promote", "--storyboard", SB, "../../etc/passwd"],
                     ["promote", "--storyboard", "sb_../x", "ev_" + "a" * 12]):
            res = self.run_cli(*args)
            self.assertEqual(res.returncode, 2)
        self.assertEqual(self.stub.requests, [])

    def test_org_in_the_path_is_quoted(self):
        self.connect(self.stub.port, org="org%2Fx")
        a = self.capture()
        res = self.run_cli("promote", "--storyboard", SB, a["evidence_id"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.stub.requests[0]["path"], f"/api/orgs/org%2Fx/storyboards/{SB}/evidence")


class PromoteUnitTests(_HomeCase):
    def setUp(self):
        super().setUp()
        self.cli = _load_cli()

    def test_truncated_result_uploads_its_envelope(self):
        env = {"truncated": True, "original_bytes": 900000, "prefix": "{\"rows\":["}
        item = self.cli.wire_item({"server": "s", "tool": "t", "called_at": "bad", "args": {}, "result": env},
                                  "claude-code/1.0.0")
        self.assertEqual(item["result"], {"structured_content": env})
        self.assertNotIn("client_called_at", item)

    def test_error_entry_uploads_is_error(self):
        entry = {"server": "s", "tool": "t", "args": {}, "is_error": True,
                 "result": {"text": ["upstream 403 Forbidden"]}}
        item = self.cli.wire_item(entry, "claude-code/1.0")
        self.assertEqual(item["result"], {"content": [{"type": "text", "text": "upstream 403 Forbidden"}],
                                          "is_error": True})
        entry.pop("is_error")
        self.assertNotIn("is_error", self.cli.wire_item(entry, "claude-code/1.0")["result"])

    def test_empty_result_is_still_a_result(self):
        self.assertEqual(self.cli.wire_result({}), {"text": ""})
        with self.assertRaises(ValueError):
            self.cli.wire_result(None)

    def test_batches_respect_item_and_byte_caps(self):
        rows = [(f"ev_{i:012x}", {}, b"x" * 10) for i in range(120)]
        self.assertEqual([len(b) for b in self.cli.batches(rows)], [50, 50, 20])
        big = [(f"ev_{i:012x}", {}, b"x" * (3 << 20)) for i in range(3)]
        self.assertEqual([len(b) for b in self.cli.batches(big)], [2, 1])

    def test_client_is_the_plugin_version(self):
        self.assertEqual(self.cli.plugin_client(), f"claude-code/{PLUGIN_VERSION}")


# ---------------------------------------------------------------------------
# Generic capture (v2 entries): built-in tools, withheld stubs, find/show
# ---------------------------------------------------------------------------

class GenericEvidenceTests(_HomeCase):
    def setUp(self):
        super().setUp()
        self.stub = StubEvidenceRoute()
        self.addCleanup(self.stub.close)
        from cardinal_core import evidence_capture
        self.cap = evidence_capture

    def capture_call(self, tool_name, tool_input, response=None, error=None, tuid="toolu_x", cwd=None):
        source, tool = self.cap.classify_mcp_name(tool_name, "claude-code", ("cardinal", "plugin_cardinal_cardinal"))
        call = self.cap.ToolCall(runtime="claude-code", tool_name=tool_name, source=source, tool=tool,
                                 tool_input=tool_input, response=response, error=error, session_id=SESSION,
                                 tool_use_id=tuid, cwd=cwd, client="claude-code/0.0.1-test")
        return self.cap.capture_call(call, self.home, env={}).entry

    def test_builtin_entry_uploads_as_a_builtin_source(self):
        self.connect(self.stub.port)
        self.store_token()
        e = self.capture_call("Bash", {"command": "make test"},
                              {"stdout": "42 passed", "stderr": "", "interrupted": False})
        res = self.run_cli("promote", e["evidence_id"])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        (item,) = json.loads(self.stub.requests[0]["raw"])["items"]
        self.assertEqual(item["source_server"], "builtin:claude-code")
        self.assertEqual(item["tool"], "Bash")
        self.assertEqual(item["client"], "claude-code/0.0.1-test")
        self.assertEqual(item["args"], {"command": "make test"})
        self.assertEqual(item["result"], {"structured_content": {"stdout": "42 passed", "stderr": ""}})
        self.assertIn("(builtin:claude-code/Bash, captured)", res.stdout)

    def test_odd_tool_names_and_non_object_args_fit_the_wire(self):
        e = self.capture_call("mcp__my-srv__do thing!", ["a", 1], "ok", tuid="t_odd")
        cli = _load_cli()
        item = cli.wire_item(e, "claude-code/1.0")
        self.assertEqual(item["source_server"], "my-srv")
        self.assertEqual(item["tool"], "do thing_")
        self.assertEqual(item["args"], {"input": ["a", 1]})
        e = self.capture_call("Whatever", None, None, error="Exit code 3", tuid="t_err")
        item = cli.wire_item(e, "claude-code/1.0")
        self.assertIs(item["result"]["is_error"], True)

    def test_withheld_entry_is_refused_locally_and_nothing_is_sent(self):
        self.connect(self.stub.port)
        self.store_token()
        w = self.capture_call("Read", {"file_path": str(self.home / ".aws" / "config")},
                              {"file": {"content": "aws_secret_access_key=x"}}, tuid="t_w")
        self.assertEqual(w["withheld"]["rule"], "path.aws")
        res = self.run_cli("promote", "--storyboard", SB, w["evidence_id"])
        self.assertEqual(res.returncode, 2)
        self.assertIn(f"{w['evidence_id']}: error withheld: sensitive_path (path.aws)", res.stdout)
        self.assertEqual(self.stub.requests, [])
        # Mixed with a citable entry: only the citable one is sent.
        ok = self.capture_call("Grep", {"pattern": "TODO"}, {"mode": "content", "content": "a.py:1:TODO"},
                               tuid="t_ok")
        res = self.run_cli("promote", "--storyboard", SB, w["evidence_id"], ok["evidence_id"])
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual(len(json.loads(self.stub.requests[0]["raw"])["items"]), 1)
        with self.assertRaises(ValueError):
            _load_cli().wire_item(w, "claude-code/1.0")

    def test_list_filters_find_and_show(self):
        a = self.capture_call("Bash", {"command": "make check-maestro"},
                              {"stdout": "Tests  512 passed", "stderr": ""}, tuid="t1")
        b = self.capture_call("Read", {"file_path": "/repo/a.py"}, {"file": {"content": "x = 1"}}, tuid="t2")
        w = self.capture_call("Bash", {"command": "printenv"}, {"stdout": "HOME=/x", "stderr": ""}, tuid="t3")
        res = self.run_cli("list", "--tool", "Bash")
        self.assertIn(a["evidence_id"], res.stdout)
        self.assertNotIn(b["evidence_id"], res.stdout)
        self.assertIn(f"{w['evidence_id']}", res.stdout)
        self.assertIn("WITHHELD secret_command", res.stdout)
        self.assertIn("make check-maestro", res.stdout)
        res = self.run_cli("find", "512 passed")
        self.assertEqual(res.returncode, 0)
        self.assertIn(a["evidence_id"], res.stdout)
        self.assertNotIn(b["evidence_id"], res.stdout)
        res = self.run_cli("list", "--withheld")
        self.assertIn(w["evidence_id"], res.stdout)
        self.assertNotIn(a["evidence_id"], res.stdout)
        res = self.run_cli("show", a["evidence_id"])
        self.assertEqual(res.returncode, 0)
        shown = json.loads(res.stdout)
        self.assertEqual(shown["result"]["structured"]["stdout"], "Tests  512 passed")
        res = self.run_cli("show", w["evidence_id"])
        self.assertIn("cannot be cited", res.stdout)
        self.assertNotIn("HOME=/x", res.stdout)
        big = self.capture_call("Bash", {"command": "cat big"}, {"stdout": "y" * 100000, "stderr": ""}, tuid="t4")
        res = self.run_cli("show", big["evidence_id"])
        self.assertIn("Outline of result", res.stdout)
        self.assertLess(len(res.stdout), 30 * 1024)


# ---------------------------------------------------------------------------
# list / off / on / status
# ---------------------------------------------------------------------------

class SpoolCommandTests(_HomeCase):
    def test_list_shows_the_newest_session_without_the_token(self):
        old = self.capture(session=OTHER_SESSION)
        os.utime(self.root / OTHER_SESSION, (time.time() - 3600, time.time() - 3600))
        a = self.capture(called_at="2026-09-28T14:00:00Z")
        b = self.capture(tool="search", called_at="2026-09-28T15:00:00Z")
        self.store_token()
        res = self.run_cli("list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"session {SESSION}: 2 captured", res.stdout)
        self.assertLess(res.stdout.index(a["evidence_id"]), res.stdout.index(b["evidence_id"]))
        self.assertIn("grafana/search", res.stdout)
        self.assertNotIn(old["evidence_id"], res.stdout)
        self.assertIn(f"evidence token for {SB} (org {ORG}, live", res.stdout)
        self.assertNotIn(TOKEN, res.stdout)
        res = self.run_cli("list", "--all")
        self.assertIn(old["evidence_id"], res.stdout)
        res = self.run_cli("list", "--session", OTHER_SESSION)
        self.assertIn(old["evidence_id"], res.stdout)
        self.assertNotIn(a["evidence_id"], res.stdout)

    def test_list_prefers_the_session_claude_code_names(self):
        a = self.capture(session=OTHER_SESSION)
        self.capture()
        os.utime(self.root / OTHER_SESSION, (time.time() - 3600, time.time() - 3600))
        res = self.run_cli("list", env=self._env(CLAUDE_CODE_SESSION_ID=OTHER_SESSION))
        self.assertIn(a["evidence_id"], res.stdout)
        self.assertIn(f"session {OTHER_SESSION}", res.stdout)

    def test_list_on_an_empty_spool(self):
        res = self.run_cli("list")
        self.assertEqual(res.returncode, 0)
        self.assertIn("No captured evidence", res.stdout)

    def test_off_on_toggle_the_flag_the_hook_reads(self):
        res = self.run_cli("off")
        self.assertEqual(res.returncode, 0, res.stderr)
        flag = self.root / "disabled"
        self.assertTrue(flag.is_file())
        self.assertEqual(stat.S_IMODE(flag.stat().st_mode), 0o600)
        self.assertTrue(self.ev.capture_disabled(self.root, {}))
        self.assertIn("capture: off (the flag file", self.run_cli("status").stdout)
        res = self.run_cli("on")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(flag.exists())
        self.assertFalse(self.ev.capture_disabled(self.root, {}))
        self.assertIn("capture: on\n", self.run_cli("status").stdout)

    def test_on_says_when_the_env_still_disables_capture(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {"CARDINAL_EVIDENCE_CAPTURE": "0"}}))
        res = self.run_cli("on")
        self.assertIn("still off", res.stdout)
        self.assertIn("settings.json", res.stdout)

    def test_status_never_prints_credentials(self):
        self.connect(4242)
        self.store_token()
        self.capture()
        res = self.run_cli("status")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("1 entries in 1 sessions", res.stdout)
        self.assertIn(f"evidence tokens: 1 live ({SB})", res.stdout)
        self.assertIn(f"uploads go to: http://127.0.0.1:4242 (org {ORG}), Cardinal MCP key present", res.stdout)
        self.assertNotIn(TOKEN, res.stdout)
        self.assertNotIn(API_KEY, res.stdout)

    def test_cli_is_executable_and_has_help(self):
        self.assertTrue(os.access(CLI, os.X_OK))
        res = self.run_cli("--help")
        self.assertEqual(res.returncode, 0)
        for cmd in ("promote", "list", "off", "on", "status"):
            self.assertIn(cmd, res.stdout)


# ---------------------------------------------------------------------------
# cardinal_core.evidence token helpers
# ---------------------------------------------------------------------------

class TokenStoreTests(_HomeCase):
    def test_expired_records_are_dropped_on_write_and_gc_removes_old_files(self):
        self.store_token(sb=SB2, expires_at="2020-01-01T00:00:00Z")
        self.store_token()
        self.assertEqual(set(self.ev.read_tokens(self.root, SESSION)), {SB})
        path = self.token_file()
        old = time.time() - self.ev.TOKEN_RETENTION_S - 10
        os.utime(path, (old, old))
        self.assertEqual(self.ev.gc(self.root, force=True), 1)
        self.assertFalse(path.exists())
        self.assertFalse((self.root / SESSION).exists())

    def test_invalid_records_are_refused(self):
        with self.assertRaises(ValueError):
            self.store_token(token="nope")
        with self.assertRaises(ValueError):
            self.store_token(org="bad org/../x")
        with self.assertRaises(ValueError):
            self.store_token(sb="sb_xyz")

    def test_token_file_symlink_is_not_read(self):
        (self.root / SESSION).mkdir(parents=True)
        target = self.home / "planted.json"
        target.write_text(json.dumps({"storyboards": {SB: {"org": ORG, "evidence_token": TOKEN}}}))
        self.token_file().symlink_to(target)
        self.assertEqual(self.ev.read_tokens(self.root, SESSION), {})

    def test_parse_time(self):
        self.assertEqual(self.ev.parse_time("1970-01-01T00:00:01Z"), 1.0)
        self.assertEqual(self.ev.parse_time("1970-01-01T00:00:01.5+00:00"), 1.5)
        self.assertAlmostEqual(self.ev.parse_time("2026-09-28T14:00:00.123456789Z") % 1, 0.123456, places=5)
        self.assertIsNone(self.ev.parse_time("yesterday"))


if __name__ == "__main__":
    unittest.main()
