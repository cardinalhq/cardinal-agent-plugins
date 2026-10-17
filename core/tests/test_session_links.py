"""Storyboard session links (conductor docs/specs/storyboard-session-links.md):
`repo_host` in the storyboard context, gated on the server's
associations_api inside collect(), and the supervisor recorded on a
supervise grant (`investigation grant --session`).

Run from core/:  python3 -m unittest tests.test_session_links -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cardinal_core import investigation_agent as agent
from cardinal_core import investigation_bootstrap as boot
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_grants as grants
from cardinal_core import investigation_state_sync as sync
from cardinal_core import session_repos
from cardinal_core import storyboard_agent
from cardinal_core import storyboard_context as sc
from cardinal_core import storyboard_discovery

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_investigation_lifecycle import LifecycleMaestro  # noqa: E402

CLIENT = "claude-code/0.58.0"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


class HostParsingTests(unittest.TestCase):
    def test_forms(self):
        cases = {
            "git@github.com:CardinalHQ/Conductor.git": ("github.com", "cardinalhq/conductor"),
            "https://GitHub.com/CardinalHQ/Conductor": ("github.com", "cardinalhq/conductor"),
            "https://gitlab.example.com/group/sub/repo.git": ("gitlab.example.com", "group/sub/repo"),
            "https://ci-bot@git.example.com/o/r.git": ("git.example.com", "o/r"),
            "ssh://git@git.corp.example:2222/team/repo.git": ("git.corp.example", "team/repo"),
            "ssh://GIT.Corp.Example/team/repo": ("git.corp.example", "team/repo"),
            "git@gitea:o/r": ("gitea", "o/r"),  # single-label hosts are hostnames too
        }
        for url, (host, repo) in cases.items():
            with self.subTest(url=url):
                self.assertEqual(sc.host_of_url(url), host)
                self.assertEqual(sc.repo_of_url(url), repo)

    def test_invalid_hosts_are_none(self):
        for url in (
            None, "", "/srv/git/local.git", "file:///srv/x.git", "git@bad_host:o/r",
            "ssh://[::1]/o/r", "git@" + "a" * 64 + ".com:o/r", "git@" + ".".join(["abc"] * 64) + ":o/r",
            "https://host.example:8443/o/r",  # the port would be read as the owner: no repo to link
            "git@-github.com:o/r", "git@github-.com:o/r", "git@a..b:o/r", "git@github.com\n:o/r",
        ):
            with self.subTest(url=url):
                self.assertIsNone(sc.host_of_url(url))

    def test_an_https_remote_with_a_port_has_no_repo(self):
        # initiative.canonical_repo reads the port as the owner ("8443/o/r").
        self.assertIsNone(sc.repo_of_url("https://git.example.com:8443/o/r.git"))
        self.assertEqual(sc.repo_of_url("git@git.example.com:o/r.git"), "o/r")

    def test_gate(self):
        for caps, sends in ((None, False), (0, False), (1, False), (True, False), ("2", False), (2, True), (3, True)):
            with self.subTest(caps=caps):
                self.assertIs(sc.sends_repo_host(caps), sends)

    def test_repo_host_is_a_context_field(self):
        self.assertIn("repo_host", sc.CONTEXT_FIELDS)
        self.assertEqual(sc.REPO_HOST_API, 2)


class CollectRepoHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        _git(self.dir, "init", "-q")
        _git(self.dir, "checkout", "-q", "-b", "feat/x")
        _git(self.dir, "remote", "add", "origin", "ssh://git@GitLab.Example.com:2222/Team/Repo.git")
        (self.dir / "f").write_text("x\n")
        _git(self.dir, "add", "-A")
        _git(self.dir, "commit", "-q", "-m", "init")

    def test_only_at_associations_api_2(self):
        for caps in (None, 0, 1):
            with self.subTest(caps=caps):
                ctx = sc.collect(str(self.dir), client=CLIENT, associations_api=caps)
                self.assertEqual(ctx["repo"], "team/repo")
                self.assertNotIn("repo_host", ctx)
        self.assertNotIn("repo_host", sc.collect(str(self.dir), client=CLIENT))  # the default is unknown
        ctx = sc.collect(str(self.dir), client=CLIENT, associations_api=2)
        self.assertEqual((ctx["repo"], ctx["repo_host"]), ("team/repo", "gitlab.example.com"))

    def test_only_with_repo(self):
        _git(self.dir, "remote", "set-url", "origin", "/srv/git/local.git")
        ctx = sc.collect(str(self.dir), client=CLIENT, associations_api=2)
        self.assertNotIn("repo", ctx)
        self.assertNotIn("repo_host", ctx)

    def test_https_port_remote_sends_neither(self):
        _git(self.dir, "remote", "set-url", "origin", "https://git.example.com:8443/o/r.git")
        ctx = sc.collect(str(self.dir), client=CLIENT, associations_api=2)
        self.assertNotIn("repo", ctx)
        self.assertNotIn("repo_host", ctx)

    def test_outside_git_never(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIn("repo_host", sc.collect(tmp, client=CLIENT, associations_api=2))

    def test_wiring_passes_the_cached_capability(self):
        wiring = storyboard_agent.Wiring.__new__(storyboard_agent.Wiring)
        with patch.object(storyboard_agent.Wiring, "client", "codex-plugin/0.36.0"), \
                patch.object(storyboard_agent.Wiring, "actor_email", return_value=None), \
                patch.object(storyboard_agent.Wiring, "pr_resolver", return_value=None), \
                patch.object(storyboard_agent.Wiring, "edited_paths", return_value=None):
            with patch.object(storyboard_agent.Wiring, "caps", return_value=1):
                self.assertNotIn("repo_host", wiring.collect(str(self.dir), None, gh=False))
            with patch.object(storyboard_agent.Wiring, "caps", return_value=2):
                self.assertEqual(wiring.collect(str(self.dir), None, gh=False)["repo_host"], "gitlab.example.com")


INV = "inv_" + "a" * 24          # the worker's (granted) investigation
OWN = "inv_" + "b" * 24          # the supervisor session's own investigation
SID = "supervisor-session"
MEMBER = {"origin": "https://test.invalid", "org": "org", "key": "test-key"}
TOKEN = "aa.bb.cc"
GRANT_ID = "grt_" + "d" * 24


class GrantSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        isolated = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        isolated["HOME"] = str(self.home)
        env_patch = patch.dict(os.environ, isolated, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        ie.write_binding(self.home, SID, {"investigation_id": OWN, "cursor": 0, "org": "org", "is_author": True})
        self.wiring = SimpleNamespace(
            paths=SimpleNamespace(home=self.home / ".codex"),
            environ={},
            client="codex/test",
            cli=str(self.home / "scripts/cli"),
            connection=Mock(return_value=MEMBER),
        )
        self.bodies: list = []
        self.answers: list = []

    def _post(self, conn, tool, body, **_):
        self.assertEqual(tool, "grant-investigation-access")
        self.bodies.append(dict(body))
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return {"investigation_id": body["investigation_id"], "grant_id": GRANT_ID, "token": TOKEN,
                "scopes": body["scopes"], **(answer or {})}

    def run_cli(self, *argv, environ=None):
        if environ is not None:
            self.wiring.environ = environ
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(sync, "_post", side_effect=self._post):
            code = agent.cli_main(list(argv), self.wiring, ("CODEX_SESSION_ID",))
        return code, out.getvalue(), err.getvalue()

    def grant(self, *extra, scope="read,supervise,evidence:read", environ=None):
        return self.run_cli("grant", "--investigation", INV, "--scope", scope, "--label", "codex sup",
                            "--out", str(self.home / f"grant-{len(self.bodies)}.json"), *extra, environ=environ)

    def test_supervise_grant_sends_the_supervisor(self):
        code, _, err = self.grant("--session", SID)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.bodies), 1)
        body = self.bodies[0]
        self.assertEqual(body["supervisor_investigation_id"], OWN)
        self.assertEqual(body["supervisor_session_id"], SID)
        self.assertEqual(body["investigation_id"], INV)
        self.assertIn("Supervised by", err)

    def test_only_an_explicit_session_is_linked(self):
        # The defaulted agent session may be unrelated (an owner minting a
        # supervise grant for someone else): never a "Supervised by" link.
        code, _, err = self.grant(environ={"CODEX_SESSION_ID": SID})
        self.assertEqual(code, 0, err)
        self.assertNotIn("supervisor_investigation_id", self.bodies[0])
        self.assertNotIn("not linking", err)
        code, _, err = self.grant("--session", SID, environ={"CODEX_SESSION_ID": "other"})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bodies[1]["supervisor_session_id"], SID)

    def test_a_session_id_with_a_trailing_newline_is_refused(self):
        code, _, err = self.grant("--session", SID + "\n")
        self.assertEqual(code, 0, err)
        self.assertNotIn("supervisor_session_id", self.bodies[0])
        self.assertIn("not a session id", err)
        self.assertFalse(storyboard_agent.valid_session(SID + "\n"))

    def test_never_for_a_grant_without_supervise(self):
        code, _, err = self.grant("--session", SID, scope="read,advise,evidence:read")
        self.assertEqual(code, 0, err)
        self.assertNotIn("supervisor_investigation_id", self.bodies[0])
        self.assertNotIn("supervisor_session_id", self.bodies[0])

    def test_never_when_the_session_names_the_granted_investigation(self):
        # Without --investigation, --session picks the investigation to grant.
        code, _, err = self.run_cli("grant", "--session", SID, "--scope", "supervise")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.bodies[0]["investigation_id"], OWN)
        self.assertNotIn("supervisor_investigation_id", self.bodies[0])
        # An owner minting a supervise grant on their own investigation, from its session.
        ie.write_binding(self.home, "worker", {"investigation_id": INV, "cursor": 0, "org": "org", "is_author": True})
        code, _, err = self.grant("--session", "worker")
        self.assertEqual(code, 0, err)
        self.assertNotIn("supervisor_investigation_id", self.bodies[1])
        self.assertNotIn("not linking", err)

    def test_unusable_bindings_mint_without_the_link_and_say_so(self):
        ie.write_binding(self.home, "joined", {"investigation_id": OWN, "cursor": 0, "org": "org", "is_author": False})
        ie.write_binding(self.home, "elsewhere", {"investigation_id": OWN, "cursor": 0, "org": "o2", "is_author": True})
        for sid in ("unbound", "joined", "elsewhere"):
            with self.subTest(sid=sid):
                code, _, err = self.grant("--session", sid)
                self.assertEqual(code, 0, err)
                self.assertNotIn("supervisor_investigation_id", self.bodies[-1])
                self.assertIn("not linking a supervisor storyboard", err)

    def test_an_older_cardinal_is_retried_once_without_the_fields(self):
        self.answers = [sync.ServerError(400, {"error": "invalid_body", "issues": [{"path": "", "message": (
            "Unrecognized key(s) in object: 'supervisor_investigation_id', 'supervisor_session_id'")}]}, "x")]
        out_file = self.home / "grant-0.json"
        code, out, err = self.grant("--session", SID)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.bodies), 2)
        self.assertIn("supervisor_investigation_id", self.bodies[0])
        self.assertNotIn("supervisor_investigation_id", self.bodies[1])
        self.assertNotIn("supervisor_session_id", self.bodies[1])
        self.assertIn("this Cardinal does not link supervisors yet", err)
        self.assertNotIn("Supervised by", err)
        self.assertTrue(out_file.exists())
        self.assertNotIn(TOKEN, out)

    def test_zod_v4_message_is_an_older_cardinal_too(self):
        self.answers = [sync.ServerError(400, {"error": "invalid_body", "issues": [{"path": "", "message": (
            'Unrecognized keys: "supervisor_investigation_id", "supervisor_session_id"')}]}, "x")]
        code, _, err = self.grant("--session", SID)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.bodies), 2)

    def test_another_invalid_body_is_a_real_refusal(self):
        self.answers = [sync.ServerError(400, {"error": "invalid_body", "issues": [
            {"path": "label", "message": "String must contain at most 200 character(s)"}]}, "x")]
        code, _, err = self.grant("--session", SID)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.bodies), 1, "not retried")
        self.assertNotIn("does not link supervisors", err)
        self.assertIn("at most 200", err)

    def test_the_retry_is_once(self):
        bad = sync.ServerError(400, {"error": "invalid_body", "issues": [{"path": "", "message": (
            "Unrecognized key(s) in object: 'supervisor_investigation_id'")}]}, "x")
        self.answers = [bad, bad]
        code, _, err = self.grant("--session", SID)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.bodies), 2)
        self.assertFalse((self.home / "grant-0.json").exists())

    def test_invalid_body_without_a_supervisor_is_not_retried(self):
        self.answers = [sync.ServerError(400, {"error": "invalid_body"}, "x")]
        code, _, _ = self.grant("--session", SID, scope="read,advise")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.bodies), 1)

    def test_supervisor_invalid_fails_clearly(self):
        self.answers = [sync.ServerError(422, {"error": "supervisor_invalid", "message": "no"}, "x")]
        code, _, err = self.grant("--session", SID)
        self.assertEqual(code, 1)
        self.assertEqual(len(self.bodies), 1, "never silently minted without the link")
        self.assertIn("no access granted", err)
        self.assertIn("as the supervisor", err)
        self.assertFalse((self.home / "grant-0.json").exists())

    def test_client_refuses_bad_supervisor_bodies(self):
        conn = dict(MEMBER)
        with patch.object(sync, "_post", side_effect=AssertionError("no request")):
            for scopes, sup in (
                (["read", "advise"], {"investigation_id": OWN, "session_id": SID}),
                (["read", "advise", "supervise"], {"investigation_id": INV, "session_id": SID}),
                (["read", "advise", "supervise"], {"investigation_id": OWN, "session_id": None}),
            ):
                with self.subTest(scopes=scopes, sup=sup), self.assertRaises(ValueError):
                    grants.grant(conn, INV, scopes, client="t", supervisor=sup)


REPO_CTX = {"repo": "acme/widgets", "repo_host": "github.com", "branch": "fix/one", "client": "claude-code/0.1"}


class OldMaestro(LifecycleMaestro):
    """ensure-session-investigation of a maestro before associations_api 2:
    400 on the unknown context key repo_host (context.ts
    unknownContextKeyMessage), unless repo_host_ok."""

    def __init__(self):
        super().__init__()
        self.repo_host_ok = False

    def answer(self, tool, body):
        if tool == "ensure-session-investigation" and not self.repo_host_ok \
                and "repo_host" in (body.get("context") or {}):
            return 400, {"error": "invalid_body", "issues": [{"path": "context", "message": (
                'context: unknown key "repo_host"; accepted keys: repo, repo_path, branch, pr_number, '
                'pr_url, head_sha, workdir_hash, client, actor_email, paths')}]}
        return super().answer(tool, body)


class SavedContextReplayTests(unittest.TestCase):
    """The bootstrap snapshot and staged repo labels keep repo_host; it is
    sent only while the origin is cached at associations_api >= 2, and an
    older maestro's 400 on it is retried once without it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.fake = OldMaestro()
        self.addCleanup(self.fake.close)
        env_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def caps(self, version, runtime="codex"):
        path = self.home / f".{runtime}" / "cardinal" / "server-caps.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({self.fake.conn["origin"]: {"associations_api": version, "at": time.time()}}))

    def ensures(self):
        return [r[1] for r in self.fake.requests if r[0] == "ensure-session-investigation"]

    def start_and_stage(self):
        res = boot.ensure(self.home, SID, self.fake.conn, "claude-code/0.1", context=dict(REPO_CTX))
        self.assertEqual(res["status"], "ok")
        self.assertTrue(session_repos.stage(self.home, SID, {**REPO_CTX, "repo": "acme/other"}, runtime="claude",
                                            env={}))
        self.assertEqual(session_repos.pending(self.home, SID, "o1")[0]["repo_host"], "github.com")
        self.assertEqual(boot.session_context(self.home, SID, self.fake.conn)["repo_host"], "github.com")
        self.fake.requests.clear()

    def test_unknown_caps_strips_repo_host_at_send_time(self):
        self.start_and_stage()
        self.assertEqual(boot.refresh_capabilities(self.home, SID, self.fake.conn, "claude-code/0.1"), "ok")
        sent = self.ensures()
        self.assertEqual([b["context"]["repo"] for b in sent], ["acme/widgets", "acme/other"])
        self.assertTrue(all("repo_host" not in b["context"] for b in sent))
        self.assertEqual(boot.session_context(self.home, SID, self.fake.conn)["repo_host"], "github.com",
                         "the saved snapshot is unchanged")

    def test_caps_2_sends_it(self):
        self.fake.repo_host_ok = True
        self.caps(2, runtime="claude")
        self.start_and_stage()
        boot.refresh_capabilities(self.home, SID, self.fake.conn, "claude-code/0.1")
        self.assertTrue(all(b["context"]["repo_host"] == "github.com" for b in self.ensures()))

    def test_the_freshest_runtime_cache_wins(self):
        self.caps(2, runtime="claude")
        time.sleep(0.01)
        self.caps(1, runtime="codex")  # the server was rolled back since
        self.assertEqual(storyboard_discovery.read_caps_any(self.home, self.fake.conn["origin"]), 1)
        self.assertNotIn("repo_host", boot.sendable_context(self.home, self.fake.conn, dict(REPO_CTX)))

    def test_a_stale_cache_is_retried_once_without_repo_host(self):
        self.caps(2)  # cached before maestro was rolled back
        res = boot.ensure(self.home, SID, self.fake.conn, "claude-code/0.1", context=dict(REPO_CTX))
        self.assertEqual(res["status"], "ok")
        sent = self.ensures()
        self.assertEqual(len(sent), 2)
        self.assertIn("repo_host", sent[0]["context"])
        self.assertEqual(sent[1]["context"], {k: v for k, v in REPO_CTX.items() if k != "repo_host"})

    def test_other_context_refusals_are_not_retried(self):
        self.fake.repo_host_ok = True
        self.fake.ensure_answer = (400, {"error": "invalid_body", "issues": [
            {"path": "context", "message": 'context: unknown key "nope"; accepted keys: repo'}]})
        with self.assertRaises(sync.ServerError):
            sync.ensure_session_investigation(self.fake.conn, SID, context=dict(REPO_CTX), client="c")
        self.assertEqual(len(self.ensures()), 1)


if __name__ == "__main__":
    unittest.main()
