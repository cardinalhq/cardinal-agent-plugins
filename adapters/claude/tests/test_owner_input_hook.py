"""Owner input in Claude Code: the UserPromptSubmit hook (owner-input.py),
the outbox flush at Stop (investigation-events.sh -> .py), the SessionStart
transparency line (storyboard-session.py), and the Investigation grants CLI
(`cardinal-storyboard investigation grant | grants | revoke`, token-aware
`events | post | show`) against a fake maestro.

Privacy invariants: nothing is captured when the capability is absent or
off, when the session is not the author, when the kill switch is set, or
for a task notification; an older server (404) gets zero bytes queued; the
debug log has a status code, never the prompt; no CLI subcommand writes
owner input; a grant token is used instead of the key, never with it.

Requires cardinal_core vendored: python3 build/vendor.py claude
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS = PLUGIN_ROOT / "hooks"
HOOK = HOOKS / "owner-input.py"
FAST = HOOKS / "investigation-events.sh"
SESSION_HOOK = HOOKS / "storyboard-session.py"
CLI = PLUGIN_ROOT / "bin" / "cardinal-storyboard"
VENDORED = HOOKS / "cardinal_core" / "owner_input.py"

INV = "inv_" + "c" * 24
OTHER_INV = "inv_" + "d" * 24
SB = "sb_" + "5" * 24
SID = "0f6e2a9c-1b2d-4e5f-8a7b-9c0d1e2f3a4b"
GRANT = "grt_" + "1" * 24
SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB"
ON = {"owner_input": {"enabled": True}}
LINE = (f"This session's prompts are recorded to Investigation {INV} as owner input (credentials scrubbed, up to "
        "32 KiB); only you and grantees you authorize can read them.")


def jwt(header: dict, claims: dict) -> str:
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc(header)}.{enc(claims)}.c2lnbmF0dXJl"


TOKEN = jwt({"alg": "HS256", "typ": "CardinalInvestigation"},
            {"org": "o1", "inv": INV, "gid": GRANT, "scopes": ["read", "advise"], "exp": 2_000_000_000})


class FakeMaestro:
    def __init__(self):
        self.events: list = []
        self.requests: list = []   # (tool, body, headers, path)
        self.status = None         # answered to every record-owner-input
        self.old_server = False
        self.principals = None     # the read answer's `principals` map (C2)
        self.read_status = None    # answered to every read-investigation-events
        self.grant_status = None   # answered to every grant-investigation-access
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                tool = self.path.rsplit("/", 1)[-1]
                fake.requests.append((tool, body, {k.lower(): v for k, v in self.headers.items()}, self.path))
                status, out = fake.answer(tool, body)
                data = json.dumps(out).encode() if out is not None else b"<html>Cannot POST</html>"
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def tools(self) -> list:
        return [r[0] for r in self.requests]

    def records(self) -> list:
        return [r[1] for r in self.requests if r[0] == "record-owner-input"]

    def answer(self, tool, body):
        if tool == "record-owner-input" and not self.old_server:
            if self.status:
                return self.status
            e = {"investigation_id": body["investigation_id"], "seq": len(self.events) + 1,
                 "type": "owner_input.recorded", "class": "owner_input", "payload": body["payload"],
                 "authority": "owner_input_client_attested",
                 "producer": {"principal": {"kind": "user", "id": "u1"}, "session_id": body["session_id"],
                              "is_investigation_author": True}}
            self.events.append(e)
            return 201, {"event": e, "duplicate": False, "trust": "client_attested: …"}
        if tool == "read-investigation-events":
            if self.read_status:
                return self.read_status
            evs = [e for e in self.events if e["seq"] > body.get("after", 0)]
            if body.get("class"):
                evs = [e for e in evs if e.get("class") == body["class"]]
            out = {"investigation_id": body["investigation_id"], "events": evs[:body.get("limit", 100)],
                   "head_seq": len(self.events)}
            if self.principals is not None:
                out["principals"] = self.principals
            return 200, out
        if tool == "append-investigation-event":
            e = {"investigation_id": body["investigation_id"], "seq": len(self.events) + 1, "type": body["type"],
                 "class": "control", "payload": body["payload"], "to_session_id": body.get("to_session_id")}
            self.events.append(e)
            return 201, {"event": e}
        if tool == "get-investigation":
            return 200, {"investigation_id": body["investigation_id"], "storyboard_id": SB}
        if tool == "grant-investigation-access":
            if self.grant_status:
                return self.grant_status
            return 201, {"grant_id": GRANT, "token": TOKEN, "expires_at": "2026-10-07T16:00:00.000Z",
                         "scopes": body["scopes"], "label": body.get("label"), "principal": "grantee:" + GRANT,
                         "investigation_id": body["investigation_id"], "org": "o1"}
        if tool == "list-investigation-grants":
            return 200, {"investigation_id": body["investigation_id"],
                         "grants": [{"grant_id": GRANT, "principal": "grantee:" + GRANT,
                                     "investigation_id": body["investigation_id"],
                                     "scopes": ["read", "owner_input:read"], "label": "sup\nervisor",
                                     "granted_by": "user:u1", "created_at": "t",
                                     "expires_at": "2026-10-07T16:00:00.000Z", "revoked_at": None,
                                     "revoked_by": None, "active": True}]}
        if tool == "revoke-investigation-grant":
            return 200, {"grant": {"grant_id": body["grant_id"], "active": False}, "revoked": True}
        return 404, None


class Base(unittest.TestCase):
    def setUp(self):
        if not VENDORED.exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        self.base = Path(os.path.realpath(self.tmp.name))
        self.home = self.base / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.fake = FakeMaestro()
        self.addCleanup(self.fake.close)
        self.connect()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CARDINAL_", "OTEL_", "CLAUDE_CODE_SESSION", "CLAUDE_SESSION"))}
        self.env["HOME"] = str(self.home)
        self.env["CARDINAL_INVESTIGATION_POLLER"] = "0"

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self, port=None, **extra):
        env = {"CARDINAL_MCP_URL": f"http://127.0.0.1:{port or self.fake.port}/api/orgs/o1/mcp",
               "CARDINAL_MCP_API_KEY": "ck"}
        env.update(extra)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": env}))

    def sessions(self) -> Path:
        return self.home / ".cardinal" / "investigations" / "sessions"

    def bind(self, **fields):
        b = {"investigation_id": INV, "cursor": 0, "bound_at": "t", "source": "auto", "is_author": True,
             "storyboard_id": SB, "org": "o1", "bootstrap": {"status": "ok"}, "capabilities": ON,
             "owner_input_disclosed": True}
        b.update(fields)
        self.sessions().mkdir(parents=True, exist_ok=True)
        (self.sessions() / f"{SID}.json").write_text(json.dumps(b))

    def binding(self) -> dict:
        return json.loads((self.sessions() / f"{SID}.json").read_text())

    def outbox(self) -> Path:
        return self.sessions() / f"{SID}.owner-input-outbox.json"

    def queue(self, *prompts):
        """Prompts left in the outbox by a 503."""
        self.fake.status = (503, {"error": "unavailable"})
        for p in prompts:
            self.silent(self.run_hook(p))
        self.fake.status = None
        box = json.loads(self.outbox().read_text())
        box["retry_after"] = 0
        self.outbox().write_text(json.dumps(box))

    def run_hook(self, prompt, env=None):
        payload = {"session_id": SID, "transcript_path": "/x.jsonl", "cwd": str(self.base),
                   "hook_event_name": "UserPromptSubmit", "prompt": prompt}
        return subprocess.run([str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=30,
                              env=env or self.env, cwd=str(self.base))

    def run_stop(self, env=None):
        payload = {"session_id": SID, "hook_event_name": "Stop", "stop_hook_active": False}
        return subprocess.run(["sh", str(FAST)], input=json.dumps(payload), capture_output=True, text=True,
                              timeout=30, env=env or self.env, cwd=str(self.base))

    def cli(self, *args, **env):
        return subprocess.run([str(CLI), "investigation", *args], capture_output=True, text=True, timeout=60,
                              env=dict(self.env, **env), cwd=str(self.base))

    def silent(self, res):
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))


class Hook(Base):
    def test_records_the_owner_prompt_and_prints_nothing(self):
        self.bind()
        self.silent(self.run_hook(f"Only touch the cache layer. My token is {SECRET}"))
        self.silent(self.run_hook("yes, go ahead"))
        got = self.fake.records()
        self.assertEqual([b["payload"]["turn"] for b in got], [1, 2])
        self.assertEqual((got[0]["investigation_id"], got[0]["session_id"]), (INV, SID))
        self.assertNotIn(SECRET, got[0]["payload"]["text"])
        self.assertTrue(got[0]["payload"]["text"].startswith("Only touch the cache layer."))
        headers = self.fake.requests[0][2]
        self.assertEqual(headers["x-cardinalhq-api-key"], "ck")
        self.assertEqual(self.binding()["owner_input_turn"], 2)

    def assert_nothing(self):
        self.assertEqual(self.fake.requests, [])
        self.assertNotIn("owner_input_turn", self.binding())
        self.assertFalse(self.outbox().exists())

    def test_capability_absent_or_off(self):
        for caps in ({}, {"owner_input": {"enabled": False}}, {"projection": {"enabled": True}}):
            self.bind(capabilities=caps)
            self.silent(self.run_hook("hello"))
            self.assert_nothing()
        self.bind()
        b = self.binding()
        del b["capabilities"]   # a binding from before 0.44.0
        (self.sessions() / f"{SID}.json").write_text(json.dumps(b))
        self.silent(self.run_hook("hello"))
        self.assert_nothing()

    def test_not_the_author(self):
        self.bind(is_author=False, source="env")
        self.silent(self.run_hook("hello"))
        self.assert_nothing()

    def test_the_kill_switch_in_the_environment_or_in_settings(self):
        self.bind()
        for v in ("0", "false", "OFF", "no"):
            self.silent(self.run_hook("hello", env=dict(self.env, CARDINAL_OWNER_INPUT=v)))
        for v in ("0", False, 0, "no"):   # settings.json values of any JSON type
            self.connect(CARDINAL_OWNER_INPUT=v)
            self.silent(self.run_hook("hello"))
        self.assert_nothing()

    def test_the_kill_switch_forgets_the_disclosure(self):
        self.bind()   # disclosed earlier in the session
        self.silent(self.run_hook("hello", env=dict(self.env, CARDINAL_OWNER_INPUT="0")))
        self.assertNotIn("owner_input_disclosed", self.binding())
        # The switch is removed: the user is told again before capture resumes.
        res = self.run_hook("not captured")
        self.assertIn(LINE, json.loads(res.stdout)["systemMessage"])
        self.assertEqual(self.fake.records(), [])
        self.silent(self.run_hook("captured"))
        self.assertEqual([b["payload"]["text"] for b in self.fake.records()], ["captured"])

    def test_the_kill_switch_in_project_settings(self):
        self.bind()
        (self.base / ".claude").mkdir()
        for name in ("settings.json", "settings.local.json"):
            f = self.base / ".claude" / name
            f.write_text(json.dumps({"env": {"CARDINAL_OWNER_INPUT": False}}))
            self.silent(self.run_hook("hello"))
            self.assert_nothing()
            f.unlink()
        self.assertIn("systemMessage", json.loads(self.run_hook("told again first").stdout))
        self.silent(self.run_hook("now it records"))
        self.assertEqual([b["payload"]["text"] for b in self.fake.records()], ["now it records"])

    def test_discloses_to_the_user_before_the_first_capture(self):
        self.bind(owner_input_disclosed=False)
        res = self.run_hook("first prompt")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        out = json.loads(res.stdout)
        self.assertEqual(list(out), ["systemMessage"], "user-visible only: no context, no decision")
        self.assertIn(LINE, out["systemMessage"])
        self.assertIn("CARDINAL_OWNER_INPUT=0", out["systemMessage"])
        self.assertEqual(self.fake.requests, [], "the disclosing prompt is not captured")
        self.assertIs(self.binding()["owner_input_disclosed"], True)
        self.silent(self.run_hook("second prompt"))
        self.assertEqual([b["payload"]["text"] for b in self.fake.records()], ["second prompt"])

    def test_no_disclosure_when_nothing_would_be_captured(self):
        for fields in ({"capabilities": {}}, {"is_author": False, "source": "env"}):
            self.bind(owner_input_disclosed=False, **fields)
            self.silent(self.run_hook("hello"))
            self.assertNotIn("owner_input_disclosed", {k for k, v in self.binding().items() if v is True})
        self.bind(owner_input_disclosed=False)
        self.silent(self.run_hook("hello", env=dict(self.env, CARDINAL_OWNER_INPUT="0")))
        self.assertFalse(self.binding().get("owner_input_disclosed"))
        self.silent(self.run_hook("<task-notification>x</task-notification>"))
        self.assertFalse(self.binding().get("owner_input_disclosed"))

    def test_task_notifications_are_not_owner_turns(self):
        self.bind()
        for prompt in ("task-notification done", "<task-notification>\n<task-id>b1</task-id>\n</task-notification>",
                       "  <task-notification>x</task-notification>"):
            self.silent(self.run_hook(prompt))
        self.assert_nothing()

    def test_unconnected_or_unbound_does_nothing(self):
        self.silent(self.run_hook("hello"))
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(self.sessions().exists())
        self.bind()
        (self.home / ".claude" / "settings.json").unlink()
        self.silent(self.run_hook("hello"))
        self.assert_nothing()

    def test_an_older_server_gets_zero_bytes_queued(self):
        self.bind()   # even with a (stale) capability
        self.fake.old_server = True
        self.silent(self.run_hook("hello"))
        self.silent(self.run_hook("hello again"))
        self.assertFalse(self.outbox().exists())
        self.assertEqual(self.fake.tools(), ["record-owner-input"])   # then off for the session
        self.assertEqual(self.binding()["capabilities"]["owner_input"], {"enabled": False})

    def test_a_refusal_is_dropped_never_queued(self):
        for status in ((404, {"error": "owner_input_disabled"}), (400, {"error": "invalid_body"}),
                       (401, {"error": "unauthorized"}), (403, {"error": "not_investigation_author"})):
            self.bind()
            self.fake.status = status
            self.silent(self.run_hook("hello"))
            self.assertFalse(self.outbox().exists(), status)

    def test_an_unreachable_server_queues_within_the_budget_0600(self):
        import time
        self.bind()
        self.connect(9)  # nothing listens there
        t = time.monotonic()
        self.silent(self.run_hook("hello"))
        self.assertLess(time.monotonic() - t, 3.0)
        box = json.loads(self.outbox().read_text())
        self.assertEqual([e["payload"]["turn"] for e in box["entries"]], [1])
        self.assertEqual(oct(self.outbox().stat().st_mode & 0o777), "0o600")

    def test_the_debug_log_has_a_code_never_the_prompt(self):
        self.bind()
        self.silent(self.run_hook(f"private words {SECRET}", env=dict(self.env, CARDINAL_HOOK_DEBUG="1")))
        log = (self.home / ".claude" / "cardinal" / "hook-debug.log").read_text()
        line = json.loads(log.splitlines()[-1])
        self.assertEqual((line["hook"], line["status"]), ("owner-input", "posted"))
        self.assertNotIn("private", log)
        self.assertNotIn(SECRET, log)


class Stop(Base):
    def test_stop_flushes_the_outbox_while_enabled(self):
        self.bind()
        self.queue("first", "second")
        res = self.run_stop()
        self.assertEqual(res.returncode, 0)
        self.assertEqual([b["payload"]["text"] for b in self.fake.records()[-2:]], ["first", "second"])
        self.assertFalse(self.outbox().exists())

    def test_stop_deletes_the_outbox_when_the_capability_is_off(self):
        self.bind()
        self.queue("first")
        self.bind(capabilities={})
        n = len(self.fake.requests)
        self.run_stop()
        self.assertFalse(self.outbox().exists())
        self.assertNotIn("record-owner-input", self.fake.tools()[n:])

    def test_stop_deletes_the_outbox_under_the_kill_switch(self):
        self.bind()
        self.queue("first")
        n = len(self.fake.requests)
        self.run_stop(env=dict(self.env, CARDINAL_OWNER_INPUT="0"))
        self.assertFalse(self.outbox().exists())
        self.assertNotIn("record-owner-input", self.fake.tools()[n:])


class SessionStart(Base):
    def output(self, env=None) -> dict:
        res = subprocess.run([str(SESSION_HOOK)], input=json.dumps({"session_id": SID, "source": "resume",
                                                                     "cwd": str(self.base)}),
                             capture_output=True, text=True, timeout=30, env=env or self.env, cwd=str(self.base))
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def context(self, env=None) -> str:
        return self.output(env)["hookSpecificOutput"]["additionalContext"]

    def test_the_line_only_when_prompts_are_recorded(self):
        self.bind()
        ctx = self.context()
        self.assertIn(LINE, ctx)
        self.assertNotIn("verbatim", ctx)
        self.bind(capabilities={})
        self.assertNotIn("owner input", self.context())
        self.bind(is_author=False, source="env")
        self.assertNotIn("owner input", self.context())
        self.bind()
        self.assertNotIn("owner input", self.context(env=dict(self.env, CARDINAL_OWNER_INPUT="off")))

    def test_the_user_is_shown_the_disclosure_and_it_is_recorded(self):
        self.bind(owner_input_disclosed=False)
        out = self.output()
        self.assertEqual(set(out), {"hookSpecificOutput", "systemMessage"})
        self.assertIn(LINE, out["systemMessage"])
        self.assertNotIn("systemMessage", out["hookSpecificOutput"])
        self.assertIs(self.binding()["owner_input_disclosed"], True)
        self.silent(self.run_hook("captured, no second disclosure"))
        self.assertEqual(len(self.fake.records()), 1)

    def run_watched(self, fail_write: bool) -> str:
        """SessionStart with a stdout that notes, at every write, whether the
        binding already says the user was told (and can fail the write)."""
        log = self.base / "writes.log"
        wrapper = (
            "import json, runpy, sys\n"
            f"BINDING, LOG, FAIL = {str(self.sessions() / (SID + '.json'))!r}, {str(log)!r}, {fail_write!r}\n"
            "class Out:\n"
            "    def write(self, s):\n"
            "        b = json.load(open(BINDING))\n"
            "        open(LOG, 'a').write(repr(b.get('owner_input_disclosed')) + ' ' + str('systemMessage' in s) + '\\n')\n"
            "        if FAIL:\n"
            "            raise BrokenPipeError('closed')\n"
            "        return len(s)\n"
            "    def flush(self):\n"
            "        pass\n"
            "sys.stdout = Out()\n"
            f"sys.argv = [{str(SESSION_HOOK)!r}]\n"
            f"runpy.run_path({str(SESSION_HOOK)!r}, run_name='__main__')\n")
        res = subprocess.run([sys.executable, "-c", wrapper],
                             input=json.dumps({"session_id": SID, "source": "startup", "cwd": str(self.base)}),
                             capture_output=True, text=True, timeout=30, env=self.env, cwd=str(self.base))
        self.assertEqual(res.returncode, 0, res.stderr)
        return log.read_text()

    def test_the_disclosure_is_recorded_only_after_it_was_written(self):
        self.bind(owner_input_disclosed=False)
        self.assertEqual(self.run_watched(fail_write=False), "False True\n")   # not yet recorded while writing
        self.assertIs(self.binding()["owner_input_disclosed"], True)

    def test_a_failed_write_records_no_disclosure(self):
        self.bind(owner_input_disclosed=False)
        self.assertEqual(self.run_watched(fail_write=True), "False True\n")
        self.assertFalse(self.binding()["owner_input_disclosed"])
        res = self.run_hook("not captured: the prompt hook discloses first")
        self.assertIn("systemMessage", json.loads(res.stdout))
        self.assertEqual(self.fake.records(), [])

    def test_no_disclosure_when_off(self):
        for fields in ({"capabilities": {}}, {"is_author": False, "source": "env"}):
            self.bind(owner_input_disclosed=False, **fields)
            self.assertNotIn("systemMessage", self.output())
            self.assertFalse(self.binding().get("owner_input_disclosed"))
        self.bind(owner_input_disclosed=False)
        (self.base / ".claude").mkdir()
        (self.base / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"CARDINAL_OWNER_INPUT": 0}}))
        out = self.output()
        self.assertNotIn("systemMessage", out)
        self.assertNotIn("owner input", out["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.binding().get("owner_input_disclosed"))


class GrantCli(Base):
    def test_grant_prints_one_export_line(self):
        self.bind()
        res = self.cli("grant", "--owner-input", "--ttl", "2h", "--label", "ChatGPT supervisor",
                       CLAUDE_CODE_SESSION_ID=SID)
        self.assertEqual(res.returncode, 0, res.stderr)
        words = shlex.split(res.stdout.strip())
        self.assertEqual(words[0], "export")
        self.assertEqual(dict(w.split("=", 1) for w in words[1:]),
                         {"CARDINAL_INVESTIGATION_TOKEN": TOKEN, "CARDINAL_INVESTIGATION_ORIGIN": self.fake.origin})
        body = self.fake.requests[-1][1]
        self.assertEqual(body, {"investigation_id": INV, "scopes": ["read", "owner_input:read", "advise"],
                                "ttl_seconds": 7200, "label": "ChatGPT supervisor"})
        self.assertIn(GRANT, res.stderr)
        self.assertIn("owner's recorded prompts", res.stderr)

    def test_grant_refuses_bad_input_before_any_request(self):
        self.bind()
        for args in (("--scope", "observe"), ("--scope", "advise", "--owner-input"), ("--ttl", "2d"),
                     ("--ttl", "1m"), ("--label", "")):
            self.assertEqual(self.cli("grant", *args, CLAUDE_CODE_SESSION_ID=SID).returncode, 2, args)
        self.assertEqual(self.fake.requests, [])

    def test_grant_limit_says_which(self):
        self.bind()
        self.fake.grant_status = (409, {"error": "grant_limit_reached", "scope": "lifetime", "limit": 100})
        res = self.cli("grant", CLAUDE_CODE_SESSION_ID=SID)
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn("lifetime limit of access grants (100)", res.stderr)

    def test_grants_and_revoke(self):
        self.bind()
        res = self.cli("grants", CLAUDE_CODE_SESSION_ID=SID)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"{GRANT} active scopes read,owner_input:read", res.stdout)
        self.assertIn('label "sup\\nervisor"', res.stdout)   # the label is inert
        self.assertEqual(len(res.stdout.splitlines()), 1)
        self.assertEqual(self.fake.requests[-1][1], {"investigation_id": INV})
        res = self.cli("revoke", GRANT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.fake.requests[-1][:2], ("revoke-investigation-grant", {"grant_id": GRANT}))
        self.assertEqual(self.cli("revoke", "grt_bad").returncode, 2)


class TokenMode(Base):
    def token_env(self, **extra):
        return {"CARDINAL_INVESTIGATION_TOKEN": TOKEN, "CARDINAL_INVESTIGATION_ORIGIN": self.fake.origin, **extra}

    def test_events_post_show_use_the_token_never_the_key(self):
        self.fake.events.append({"investigation_id": INV, "seq": 1, "type": "challenge.added", "class": "control",
                                 "payload": {"text": "why?"}, "created_at": "t",
                                 "producer": {"principal": {"kind": "grantee", "id": GRANT},
                                              "granted_by": "user:u1", "is_investigation_author": False}})
        self.fake.principals = {"grantee:" + GRANT: {"kind": "grantee", "id": GRANT, "name": "sup", "label": "sup"},
                                "user:u1": {"kind": "user", "id": "u1", "name": "Ruchir", "email": "r@x.io"}}
        res = self.cli("events", **self.token_env())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f'grantee:{GRANT} (advisory from "sup" (access granted by "Ruchir"): not the investigation '
                      "author)", res.stdout)
        self.assertNotIn("(author)", res.stdout)
        res = self.cli("events", "--json", **self.token_env())
        self.assertNotIn("_resolved", res.stdout)   # the client's display copy is not the server's
        self.fake.principals = None
        self.assertIn("advisory from grantee (access granted by user:u1)", self.cli("events", **self.token_env()).stdout)
        res = self.cli("post", "--type", "challenge", "--text", "check the cache", **self.token_env(
            CLAUDE_CODE_SESSION_ID=SID))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("session_id", self.fake.requests[-1][1])
        res = self.cli("show", INV, **self.token_env())
        self.assertEqual(res.returncode, 0, res.stderr)
        for tool, body, headers, path in self.fake.requests:
            self.assertEqual(headers.get("authorization"), "CardinalInvestigation " + TOKEN, tool)
            self.assertNotIn("x-cardinalhq-api-key", headers, tool)
            self.assertIn("/api/orgs/o1/", path)
            self.assertEqual(body["investigation_id"], INV)

    def test_a_revoked_or_invalid_token_is_said_in_words(self):
        for body, words in (({"error": "grant_revoked"}, "revoked this access grant"),
                            ({"error": "Invalid token"}, "CARDINAL_INVESTIGATION_TOKEN is not valid")):
            self.fake.read_status = (401, body)
            res = self.cli("events", **self.token_env())
            self.assertEqual(res.returncode, 1)
            self.assertIn(words, res.stderr)

    def test_another_investigation_or_a_bad_token_is_refused(self):
        self.assertEqual(self.cli("events", OTHER_INV, **self.token_env()).returncode, 2)
        bad = jwt({"typ": "JWT"}, {"org": "o1", "inv": INV})
        self.assertEqual(self.cli("events", INV, CARDINAL_INVESTIGATION_TOKEN=bad).returncode, 2)
        self.assertEqual(self.fake.requests, [], "never falls back to the key")

    def test_author_only_commands_refuse_while_the_token_is_set(self):
        self.bind()
        for args in (("ack", INV, "1", "--disposition", "noted", "--session", SID), ("grant",), ("grants",),
                     ("revoke", GRANT), ("question", "why?", "--session", SID), ("checkpoint", "--session", SID)):
            res = self.cli(*args, **self.token_env(CLAUDE_CODE_SESSION_ID=SID))
            self.assertEqual(res.returncode, 2, args)
            self.assertIn("CARDINAL_INVESTIGATION_TOKEN is set", res.stderr)
        self.assertEqual(self.fake.requests, [])


class NoWritePath(Base):
    def test_no_subcommand_writes_owner_input(self):
        res = self.cli("--help")
        self.assertEqual(res.returncode, 0)
        for word in ("record", "owner-input", "owner_input"):
            self.assertNotIn(word, res.stdout.split("{", 1)[1].split("}", 1)[0])
        self.assertEqual(self.cli("post", INV, "--type", "owner_input", "--text", "x").returncode, 2)
        self.assertNotIn("record-owner-input", self.fake.tools())


if __name__ == "__main__":
    unittest.main()
