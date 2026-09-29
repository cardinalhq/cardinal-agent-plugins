"""cardinal_core.evidence_gate: the tool-neutral sensitivity gate.

Table-driven: every rule id has a positive (including through shell
wrappers), and the negatives pin the false positives that matter in a coding
session.

Run with: cd core && python3 -m unittest tests.test_evidence_gate -v
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import evidence_gate as gate

HOME = "/Users/dev"
CWD = "/Users/dev/repo"

# (string in tool_input, expected rule id)
POSITIVE = [
    # paths
    (".env", "path.dotenv"),
    ("cat ../.env", "path.dotenv"),
    ("source .env.local", "path.dotenv"),
    ("/srv/app/.envrc", "path.dotenv"),
    ("config/credentials.json", "path.credentials"),
    ("~/.config/app/credentials", "path.credentials"),
    ("deploy/secrets.yaml", "path.secrets"),
    ("infra/prod.tfvars", "path.secrets"),
    ("terraform.tfstate.backup", "path.secrets"),
    ("certs/server.key", "path.private-key"),
    ("~/.ssh/id_ed25519", "path.private-key"),
    ("store.p12", "path.private-key"),
    ("~/.ssh/config", "path.ssh"),
    ("~/.aws/config", "path.aws"),
    ("/Users/dev/.gnupg/pubring.kbx", "path.gnupg"),
    ("~/.kube/config", "path.kube"),
    ("./prod-kubeconfig.yaml", "path.kube"),
    ("/var/run/secrets/kubernetes.io/serviceaccount/token", "path.kube"),
    ("~/.netrc", "path.netrc"),
    ("cat ~/.npmrc", "path.netrc"),
    ("~/.docker/config.json", "path.docker"),
    ("~/.config/gh/hosts.yml", "path.gh"),
    ("~/.config/gcloud/application_default_credentials.json", "path.credentials"),
    ("~/.config/gcloud/configurations/config_default", "path.gcloud"),
    ("~/Library/Keychains/login.keychain-db", "path.keychain"),
    ("~/.cardinal/evidence/s/ev_0123456789ab.json", "path.agent-secrets"),
    ("~/.claude/settings.json", "path.agent-secrets"),
    ("~/.claude.json", "path.agent-secrets"),
    ("~/.codex/auth.json", "path.agent-secrets"),
    ("/proc/1/environ", "path.proc-environ"),
    ("/etc/shadow", "path.proc-environ"),
    # commands
    ("printenv", "cmd.env-dump"),
    ("env", "cmd.env-dump"),
    ("env | sort", "cmd.env-dump"),
    ("sudo -E env", "cmd.env-dump"),
    ("export -p", "cmd.env-dump"),
    ("bash -lc 'printenv | sort'", "cmd.env-dump"),
    ("echo x | xargs printenv", "cmd.env-dump"),
    ("aws sts get-session-token", "cmd.aws"),
    ("aws --profile prod sts assume-role --role-arn x", "cmd.aws"),
    ("aws configure export-credentials --format env", "cmd.aws"),
    ("aws secretsmanager get-secret-value --secret-id db", "cmd.aws"),
    ("aws ssm get-parameter --name /x --with-decryption", "cmd.aws"),
    ("aws ecr get-login-password | docker login", "cmd.aws"),
    ("kubectl get secret db -o yaml", "cmd.kube-secret"),
    ("kubectl -n prod get secrets", "cmd.kube-secret"),
    ("kubectl describe secret/db", "cmd.kube-secret"),
    ("kubectl get configmap,secret", "cmd.kube-secret"),
    ("oc get secrets", "cmd.kube-secret"),
    ("kubectl config view --raw", "cmd.kube-secret"),
    ("gh auth token", "cmd.gh-token"),
    ("echo $(gh auth token)", "cmd.gh-token"),
    ("echo `gh auth token`", "cmd.gh-token"),
    ("GH_TOKEN=$(gh auth token) make release", "cmd.gh-token"),
    ("gh auth status --show-token", "cmd.gh-token"),
    ("gcloud auth print-access-token", "cmd.gcloud"),
    ("gcloud auth application-default print-access-token", "cmd.gcloud"),
    ("gcloud secrets versions access latest --secret=x", "cmd.gcloud"),
    ("az account get-access-token", "cmd.az"),
    ("az keyvault secret show --name x --vault-name v", "cmd.az"),
    ("vault kv get secret/app", "cmd.vault"),
    ("vault read database/creds/app", "cmd.vault"),
    ("op read op://vault/item/password", "cmd.vault"),
    ("op item get db", "cmd.vault"),
    ("security find-generic-password -s x -w", "cmd.keychain"),
    ("timeout 5 security find-internet-password -s x", "cmd.keychain"),
    ("git credential fill", "cmd.git-credential"),
    ("heroku auth:token", "cmd.misc-token"),
    ("sops -d secrets.enc.yaml", "cmd.misc-token"),
    ("nohup sh -c 'gpg --decrypt x.gpg'", "cmd.misc-token"),
    # urls and headers
    ("curl https://user:hunter2@example.com/x", "url.userinfo"),
    ("https://bucket.s3.amazonaws.com/o?X-Amz-Signature=abc&X-Amz-Credential=x", "url.credential-param"),
    ("https://api.example.com/v1?access_token=abc", "url.credential-param"),
    ('curl -H "Authorization: Bearer $TOKEN" https://api.example.com', "header.credential"),
    ("curl -H 'Cookie: session=abc' https://x", "header.credential"),
    ("curl -u admin:pw https://x", "arg.userpass"),
    ("PGPASSWORD=pw psql -h db", "env.password"),
    ("mysql -uroot -psecret db", "arg.password"),
]

NEGATIVE = [
    "credentials-store.ts",
    "src/lib/credentials.test.ts",
    "grep -r printenv docs/",
    "env FOO=1 make test",
    "echo $HOME",
    "cat ~/.ssh/known_hosts",
    "cat ~/.ssh/id_rsa.pub",
    "make check-maestro",
    "git status && git diff --stat",
    "kubectl get pods -n prod",
    "gh pr view 123 --json state",
    "set -euo pipefail",
    "npm test -- --run",
    "export FOO=1",
    "https://example.com/search?q=secret+sauce",
    "packages/maestro/src/storyboard/evidence-upload.ts",
    "rg -n 'Authorization' src/",
    'git commit -m "fix secret handling in credential store"',
    "echo secrets",
    # prose / file content: a bare mention is not access
    "# Setup\n\nCopy the example file to .env and fill it in.\n",
    "const headers = { Authorization: token };\nfetch(url, { headers });\n",
]


class GateTableTests(unittest.TestCase):
    def check(self, s, **kw):
        return gate.check("AnyTool", {"value": s}, cwd=CWD, home=HOME, **kw)

    def test_every_rule_fires(self):
        for s, rule in POSITIVE:
            with self.subTest(s=s):
                w = self.check(s)
                self.assertIsNotNone(w, s)
                self.assertEqual(w.rule, rule, s)

    def test_negatives_are_captured(self):
        for s in NEGATIVE:
            with self.subTest(s=s):
                self.assertIsNone(self.check(s), s)

    def test_verdicts_never_carry_the_matched_text(self):
        planted = "SeCrEt-VaLuE-9f8e7d"
        for s in (f"curl https://u:{planted}@h/x", f"PGPASSWORD={planted} psql",
                  f"https://h/x?token={planted}", f'curl -H "X-Api-Key: {planted}" https://h',
                  f"cat /srv/{planted}/.env"):
            w = self.check(s)
            self.assertIsNotNone(w, s)
            self.assertNotIn(planted, json.dumps(w.as_dict()))
            self.assertNotIn(planted, gate.describe(w))

    def test_tool_name_is_never_consulted(self):
        for name in ("Bash", "Read", "mcp__x__y", "FrobnicateWidgets_v9", "", None, 7):
            self.assertEqual(gate.check(name, {"file_path": "~/.aws/config"}, home=HOME).rule, "path.aws")
            self.assertIsNone(gate.check(name, {"file_path": "README.md"}, home=HOME))

    def test_any_input_shape_and_keys(self):
        self.assertEqual(gate.check("t", "cat .env", cwd=CWD, home=HOME).rule, "path.dotenv")
        self.assertEqual(gate.check("t", ["x", {"deep": [{"cmd": "printenv"}]}]).rule, "cmd.env-dump")
        self.assertEqual(gate.check("t", {"~/.aws/credentials": 1}, home=HOME).reason, gate.REASON_PATH)
        for v in (None, 1, 2.5, True, [], {}, "", [[[]]]):
            self.assertIsNone(gate.check("t", v))
        self.assertEqual(gate.check("t", {"headers": {"Authorization": "Bearer x"}}).rule, "header.credential")
        self.assertEqual(gate.check("t", {"headers": [{"name": "Cookie", "value": "a=b"}]}).rule,
                         "header.credential")
        self.assertIsNone(gate.check("t", {"headers": {"Accept": "text/html"}}))
        self.assertEqual(gate.check("t", {"authorization": "Basic x"}).rule, "header.credential")

    def test_relative_paths_resolve_against_cwd(self):
        self.assertEqual(gate.check("t", {"p": "../../.aws/config"}, cwd=CWD + "/sub", home=HOME).rule, "path.aws")
        self.assertIsNone(gate.check("t", {"p": "../.aws-notes/config"}, cwd=CWD, home=HOME))

    def test_a_symlink_into_a_secret_dir_is_followed(self):
        with TemporaryDirectory() as t:
            home = os.path.realpath(t)
            (Path(home) / ".ssh").mkdir()
            (Path(home) / ".ssh" / "config").write_text("Host x")
            os.symlink(Path(home) / ".ssh" / "config", Path(home) / "notes.txt")
            w = gate.check("Read", {"file_path": f"{home}/notes.txt"}, cwd=home, home=home)
            self.assertIsNotNone(w)
            self.assertEqual(w.rule, "path.ssh")

    def test_a_bare_name_counts_when_that_file_exists(self):
        with TemporaryDirectory() as t:
            cwd = os.path.realpath(t)
            self.assertIsNone(gate.check("Bash", {"command": "cat credentials"}, cwd=cwd, home=HOME))
            (Path(cwd) / "credentials").write_text("x")
            self.assertEqual(gate.check("Bash", {"command": "cat credentials"}, cwd=cwd, home=HOME).rule,
                             "path.credentials")

    def test_long_strings_are_content_not_paths(self):
        body = "x" * (gate.MAX_STRING + 1) + " ~/.aws/credentials printenv"
        self.assertIsNone(gate.check("Write", {"content": body}, home=HOME))

    def test_hostile_inputs_stay_bounded(self):
        deep = {}
        cur = deep
        for _ in range(5000):
            cur["a"] = {}
            cur = cur["a"]
        cur["b"] = "printenv"
        self.assertIsNone(gate.check("t", deep))  # beyond MAX_DEPTH: not visited, no crash
        wide = {str(i): "x" for i in range(20000)}
        wide["zz"] = "printenv"
        gate.check("t", wide)  # bounded by MAX_LEAVES; must not raise
        self.assertIsNone(gate.check("t", {"s": "\udcff\x00 $(( `` $( ((("}))
        self.assertIsNone(gate.check("t", {"s": "'" * 10000}))


class UserRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        (self.home / ".cardinal").mkdir()
        self.proj = self.home / "proj"
        (self.proj / ".cardinal").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path: Path, data) -> None:
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def rules(self):
        return gate.load_rules(self.home, str(self.proj))

    def test_user_deny_and_allow(self):
        self.write(self.home / ".cardinal" / gate.RULES_FILE, {
            "version": 1,
            "deny": {"tools": ["mcp__gmail__*"], "paths": ["**/customer-exports/**"], "commands": ["^psql .*prod"]},
            "allow": {"rules": ["path.dotenv"], "paths": ["**/fixtures/**/credentials.json"]},
        })
        r = self.rules()
        self.assertEqual(gate.check("mcp__gmail__read", {}, rules=r).rule, "user.deny-tool")
        self.assertEqual(gate.check("Read", {"p": "/data/customer-exports/a.csv"}, rules=r).rule, "user.deny-path")
        self.assertEqual(gate.check("Bash", {"c": "psql -h prod-db"}, rules=r).rule, "user.deny-command")
        self.assertIsNone(gate.check("Read", {"p": ".env.example"}, cwd=str(self.proj), rules=r))
        self.assertIsNone(gate.check("Read", {"p": "/repo/tests/fixtures/gcp/credentials.json"}, rules=r))
        self.assertEqual(gate.check("Read", {"p": "/repo/credentials.json"}, rules=r).rule, "path.credentials")
        # User deny wins over user allow.
        self.write(self.home / ".cardinal" / gate.RULES_FILE, {"deny": {"paths": ["*.env.example"]},
                                                               "allow": {"rules": ["path.dotenv"]}})
        self.assertEqual(gate.check("Read", {"p": "/r/.env.example"}, rules=self.rules()).rule, "user.deny-path")

    def test_project_rules_may_only_deny(self):
        self.write(self.proj / ".cardinal" / gate.RULES_FILE, {"deny": {"paths": ["**/internal/**"]},
                                                               "allow": {"rules": ["path.dotenv", "cmd.env-dump"]}})
        r = self.rules()
        self.assertEqual(gate.check("Read", {"p": "/x/internal/a"}, rules=r).rule, "user.deny-path")
        self.assertEqual(gate.check("Read", {"p": ".env"}, cwd=str(self.proj), rules=r).rule, "path.dotenv")
        self.assertEqual(gate.check("Bash", {"c": "printenv"}, rules=r).rule, "cmd.env-dump")

    def test_malformed_or_oversized_rules_are_ignored_with_a_warning(self):
        for body in ("{not json", "[1,2]", json.dumps({"allow": {"rules": ["path.dotenv"]}, "pad": "x" * (70 << 10)})):
            self.write(self.home / ".cardinal" / gate.RULES_FILE, body)
            r = self.rules()
            self.assertTrue(r.warnings, body[:20])
            self.assertEqual(gate.check("Read", {"p": ".env"}, cwd="/r", rules=r).rule, "path.dotenv")
        (self.home / ".cardinal" / gate.RULES_FILE).unlink()
        self.assertEqual(self.rules().warnings, ())

    def test_bad_user_regex_is_skipped(self):
        self.write(self.home / ".cardinal" / gate.RULES_FILE, {"deny": {"commands": ["(unclosed", "^rm "]}})
        r = self.rules()
        self.assertEqual(len(r.deny_commands), 1)
        self.assertTrue(r.warnings)


if __name__ == "__main__":
    unittest.main()
