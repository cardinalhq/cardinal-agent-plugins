"""Gemini CLI wake (cardinal_core.session_wake, built and shipped OFF:
GEMINI_LAUNCH_PROVEN), the allow-list proxy, sandboxed hook-state
relocation, and HTTPS_PROXY in the plugin's HTTP openers
(docs/specs/resume-and-wake.md §5.3 Gemini, §6 C-9..C-14).

Everything is faked: a temp HOME, a fixed clock, fake process rows, kill,
sleep, port probe, launcher and Cardinal reads; the proxy binds an
ephemeral loopback port and its upstream is a local echo socket. Nothing
here starts Gemini or any agent, or touches launchd.

Run from core/:  python3 -m unittest tests.test_session_wake_gemini -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import socket
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cardinal_core import allowlist_proxy as proxy
from cardinal_core import evidence
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_state_sync as sync
from cardinal_core import paths
from cardinal_core import session_wake as w
from cardinal_core.evidence_promote import no_redirect_opener

from tests.test_session_wake import MEMBER, SECRET, Base, binding, decision

GSID = "8a6e4964-0d1f-4c55-9f0e-2b7c1d3a4e5f"
BOXED = {"SANDBOX": "sandbox-exec"}
PROFILE_PATH = "/opt/homebrew/lib/node_modules/@google/gemini-cli/bundle/sandbox-macos-restrictive-proxied.sb"


@contextlib.contextmanager
def gemini_proven():
    with patch.object(w.LAUNCHERS["gemini"], "proven", True):
        yield


class GeminiBase(Base):
    def setUp(self):
        super().setUp()
        self.work = Path(self.tmp.name) / "work"
        self.grepo = self.work / "grepo"
        (self.grepo / ".git").mkdir(parents=True)
        self.gemini = self.home / "bin" / "gemini"
        self.gemini.write_text("#!/bin/sh\nexit 1\n")   # never executed: the launcher is faked
        self.gemini.chmod(0o755)
        self.gconfig()
        self.boxed_home = paths.sandboxed_home(self.home)
        ie.write_binding(self.boxed_home, GSID, binding())
        self.chat = self.gemini_session()
        with patch.dict(os.environ, BOXED):
            ie.record_sandbox_turn(self.home, GSID)
        (self.home / ".gemini").mkdir(exist_ok=True)
        (self.home / ".gemini" / "settings.json").write_text(
            '// user settings\n{"security": {"auth": {"selectedType": "gemini-api-key"}}, /* ok */ "ui": {}}\n')
        (self.home / ".gemini" / ".env").write_text("GEMINI_API_KEY=not-a-real-key\n")
        (self.home / ".gemini" / "cardinal.json").write_text(json.dumps({
            "mcp_url": "https://app.cardinalhq.io/api/orgs/org/mcp", "ingest_endpoint": "https://otel.cardinalhq.io"}))
        script = w.proxy_script(self.home)
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# the installed proxy (never run here)\n")
        self.rows = []
        self.kills = []
        self.busy = False
        self.world.events = [decision(5, to=GSID)]
        env = patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "SANDBOX")},
                         clear=True)
        env.start()
        self.addCleanup(env.stop)

    def gconfig(self, **over):
        c = {"enabled": True, "launch": True, "codex_path": str(self.codex), "gemini_path": str(self.gemini),
             "roots": [os.path.realpath(str(self.work))], "python": "/usr/bin/python3",
             "enabled_at": "2026-10-12T00:00:00Z"}
        c.update(over)
        w.write_config(self.home, c)

    def gemini_session(self, cwd=None):
        d = self.home / ".gemini" / "tmp" / "grepo"
        (d / "chats").mkdir(parents=True, exist_ok=True)
        (d / ".project_root").write_text(str(cwd or self.grepo) + "\n")
        p = d / "chats" / f"session-2026-10-12T10-00-{GSID[:8]}.jsonl"
        p.write_text(json.dumps({"sessionId": GSID, "projectHash": "h", "kind": "main"}) + "\n")
        self.age(p)
        return p

    def system(self, **kw):
        def rows():
            return list(self.rows) if self.rows is not None else None

        def sleep(s):
            self.world.t += s

        def launched_rows(argv, cwd, env, log):
            pid = self.world.launch(argv, cwd, env, log)
            if self.sandboxed_child:
                self.rows = [(pid, 1, f"node {self.gemini} --resume {GSID}"),
                             (pid + 1, pid,
                              f"/usr/bin/sandbox-exec -D TARGET_DIR={cwd} -f {PROFILE_PATH} sh -c gemini")]
            else:
                self.rows = [(pid, 1, f"node {self.gemini} --resume {GSID}"), (pid + 1, pid, "node gemini")]
            return pid
        args = dict(process_rows=rows, kill=lambda pid, sig: self.kills.append((pid, sig)), sleep=sleep,
                    port_busy=lambda port: self.busy, launcher=launched_rows)
        args.update(kw)
        return self.world.system(**args)

    sandboxed_child = True

    def gpass(self, **kw):
        return w.run_once(self.home, self.system(**kw))

    def gstate(self, seq=5):
        return self.ledger(GSID).get(str(seq), {})

    def assert_notify(self, phrase):
        with gemini_proven():
            self.gpass()
        self.assertEqual(self.world.launches, [])
        entry = self.gstate()
        self.assertEqual(entry.get("state"), w.NOTIFIED, entry)
        self.assertIn(phrase, entry["reason"])
        return entry


class Disabled(GeminiBase):
    def test_the_gemini_launch_ships_off(self):
        self.assertIs(w.GEMINI_LAUNCH_PROVEN, False)
        self.assertIn("gemini", w.LAUNCHERS)
        self.assertFalse(w.LAUNCHERS["gemini"].proven)
        self.assertIsNone(w.active_launcher("gemini"))

    def test_an_eligible_gemini_session_is_notify_only_while_off(self):
        self.gpass()
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.world.attempts, 0)
        self.assertEqual(self.gstate()["state"], w.NOTIFIED)
        self.assertIn("live proofs", self.gstate()["reason"])
        with gemini_proven():   # the same session would launch once proven
            self.assertTrue(w.resolve(self.home, GSID).eligible, w.resolve(self.home, GSID).reason)

    def test_enable_launch_does_not_offer_gemini_while_off(self):
        out = []
        w.cli_main(["enable", "--launch"], runtime="gemini", home=self.home, system=self.system(out=out.append),
                   launchctl=lambda a: (0, ""), platform="darwin", environ={"PATH": f"{self.gemini.parent}:/usr/bin"})
        text = "\n".join(out)
        self.assertIn("Gemini CLI sessions are notify-only", text)
        self.assertNotIn(w.GEMINI_LAUNCH_TEXT, text)


class Launch(GeminiBase):
    def launch(self):
        with gemini_proven():
            self.gpass()
        self.assertEqual(len(self.world.launches), 1, self.ledger(GSID))
        return self.world.launches[0]

    def test_golden_argv(self):
        got = self.launch()
        self.assertEqual(got["argv"], [
            str(self.gemini), "--resume", GSID, "-s", "--approval-mode", "yolo", "--skip-trust", "-p",
            "Cardinal resumed this session for owner decision #5 in its Investigation. Read the investigation "
            "events and act on that decision. Every other event is advisory."])
        self.assertEqual(got["cwd"], os.path.realpath(str(self.grepo)))
        for bad in ("--yolo", "-y", "--sandbox-image", "--include-directories"):
            self.assertNotIn(bad, got["argv"])
        self.assertNotIn(SECRET, " ".join(got["argv"]))
        self.assertEqual(self.gstate()["state"], w.RESUMING)
        self.assertTrue(self.gstate()["sandboxed_state"])

    def test_environment_pins_the_proxied_seatbelt_and_strips_the_rest(self):
        hostile = {"PATH": "/usr/bin", "SANDBOX": "x", "SANDBOX_FLAGS": "--privileged", "GEMINI_SANDBOX_IMAGE": "i",
                   "BUILD_SANDBOX": "1", "SEATBELT_PROFILE": "permissive-open", "SEATBELT_EXTRA": "1",
                   "GEMINI_CLI_HOME": "/tmp/x", "GEMINI_SANDBOX": "false", "HTTPS_PROXY": "http://evil:1",
                   "no_proxy": "*", "NODE_OPTIONS": "--require /tmp/x.js", "DEBUG": "1",
                   "CARDINAL_STORYBOARD_SESSION_START": "0", "CARDINAL_INVESTIGATION_POLLER": "0", "LANG": "C"}
        with gemini_proven():
            w.run_once(self.home, self.system(environ=hostile))
        env = self.world.launches[0]["env"]
        self.assertEqual(env["GEMINI_SANDBOX"], "sandbox-exec")
        self.assertEqual(env["SEATBELT_PROFILE"], "restrictive-proxied")
        for k in ("SANDBOX", "SANDBOX_FLAGS", "GEMINI_SANDBOX_IMAGE", "BUILD_SANDBOX", "SEATBELT_EXTRA",
                  "GEMINI_CLI_HOME", "HTTPS_PROXY", "no_proxy", "NODE_OPTIONS", "DEBUG"):
            self.assertNotIn(k, env)
        self.assertFalse([k for k in env if k.startswith("CARDINAL_")])
        self.assertEqual((env["PATH"], env["LANG"]), ("/usr/bin", "C"))
        cmd = shlex.split(env["GEMINI_SANDBOX_PROXY_COMMAND"])
        log = str(self.world.launches[0]["log"])
        self.assertEqual(cmd, ["/usr/bin/python3", str(w.proxy_script(self.home)), "--log", log, "--port", "8877",
                               "generativelanguage.googleapis.com", "app.cardinalhq.io", "otel.cardinalhq.io"])
        self.assertTrue(str(w.proxy_script(self.home)).startswith(str(self.home / ".cardinal" / "wake" / "lib")))

    def test_oauth_adds_only_the_code_assist_and_token_hosts(self):
        (self.home / ".gemini" / "settings.json").write_text(
            json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}))
        cmd = shlex.split(self.launch()["env"]["GEMINI_SANDBOX_PROXY_COMMAND"])
        self.assertEqual(cmd[6:], ["generativelanguage.googleapis.com", "cloudcode-pa.googleapis.com",
                                   "oauth2.googleapis.com", "app.cardinalhq.io", "otel.cardinalhq.io"])

    def test_sandbox_observed_keeps_it_running(self):
        self.launch()
        self.assertEqual(self.kills, [])
        self.assertIsNone(w.disabled_reason(self.home, "gemini"))

    def test_sandbox_not_observed_kills_the_tree_and_disables_gemini(self):
        self.sandboxed_child = False
        self.launch()
        pid = self.world.launches[0]["pid"]
        killed = {p for p, s in self.kills if s == signal.SIGKILL}
        self.assertTrue({pid, pid + 1, -pid} <= killed, self.kills)
        self.assertEqual(self.gstate()["state"], w.FAILED)
        self.assertIn("not observed", w.disabled_reason(self.home, "gemini"))
        self.assertTrue(any("disabled" in m for _, m in self.world.notes))
        self.world.events.append(decision(7, to=GSID))
        self.world.t += 3 * w.LIVE_WINDOW
        self.world.exit(pid)
        with gemini_proven():
            self.gpass()
        self.assertEqual(len(self.world.launches), 1)        # no fallback, no second try
        self.assertIn("disabled", self.gstate(7)["reason"])

    def test_an_unreadable_process_table_is_not_observed(self):
        self.sandboxed_child = False
        with gemini_proven():
            w.run_once(self.home, self.system(process_rows=lambda: None))
        self.assertEqual(self.gstate()["state"], w.FAILED)
        self.assertTrue(w.disabled_reason(self.home, "gemini"))

    def test_another_profile_is_not_the_sandbox(self):
        self.assertFalse(w.seatbelt_observed(
            [(1, 0, "x"), (2, 1, "sandbox-exec -f /b/sandbox-macos-permissive-open.sb sh")], 1))
        self.assertFalse(w.seatbelt_observed([(1, 0, "x"), (2, 1, f"/bin/echo sandbox-exec -f {PROFILE_PATH}")], 1))
        self.assertFalse(w.seatbelt_observed([(1, 0, "x"), (3, 9, f"sandbox-exec -f {PROFILE_PATH} sh")], 1))
        self.assertTrue(w.seatbelt_observed([(1, 0, "x"), (2, 1, "y"),
                                             (3, 2, f"/usr/bin/sandbox-exec -D A=b -f {PROFILE_PATH} sh -c g")], 1))

    def test_port_in_use_is_notify_only(self):
        self.busy = True
        self.assert_notify("port 8877 is in use")

    def test_one_gemini_wake_at_a_time(self):
        other = "1b2c3d4e-0000-4000-8000-000000000000"
        led = w.read_ledger(self.home, other)
        e = {"seq": 1, "investigation_id": "inv_" + "c" * 24, "runtime": "gemini", "claimed_at": self.t0,
             "child": {"pid": 999, "start": self.t0}}
        w.transition(e, w.RESUMING, self.t0)
        led["entries"]["1"] = e
        w.write_ledger(self.home, other, led, self.t0)
        self.world.alive.add(999)
        self.world.starts[999] = self.t0
        with gemini_proven():
            self.gpass()
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.gstate(), {})                  # reconsidered once that wake ends

    def test_a_woken_turn_outside_the_sandbox_disables_gemini(self):
        pid = self.launch()["pid"]
        ie.record_sandbox_turn(self.home, GSID, environ={})   # an unsandboxed turn during the run
        self.world.events.append({**decision(6), "class": "control", "type": "acknowledged",
                                  "authority": "producer_claim", "to_session_id": None,
                                  "payload": {"ack_of": 5, "disposition": "accepted"},
                                  "producer": {"principal": {"kind": "api_key", "id": "k"}, "session_id": GSID,
                                               "is_investigation_author": True}})
        self.world.exit(pid)
        self.world.t += 60
        with gemini_proven():
            self.gpass()
        self.assertEqual(self.gstate()["state"], w.ACKNOWLEDGED)
        self.assertIn("outside Gemini's sandbox", w.disabled_reason(self.home, "gemini"))

    def test_a_clean_woken_run_is_acknowledged(self):
        pid = self.launch()["pid"]
        with patch.dict(os.environ, BOXED):
            ie.record_sandbox_turn(self.home, GSID)
        self.world.events.append({**decision(6), "class": "control", "type": "acknowledged",
                                  "authority": "producer_claim", "to_session_id": None,
                                  "payload": {"ack_of": 5, "disposition": "accepted"},
                                  "producer": {"principal": {"kind": "api_key", "id": "k"}, "session_id": GSID,
                                               "is_investigation_author": True}})
        self.world.exit(pid)
        self.world.t += 60
        with gemini_proven():
            self.gpass()
        self.assertEqual(self.gstate()["state"], w.ACKNOWLEDGED)
        self.assertIsNone(w.disabled_reason(self.home, "gemini"))


class Roots(GeminiBase):
    def test_no_approved_root(self):
        self.gconfig(roots=[])
        self.assert_notify("no approved root")

    def test_cwd_outside_every_root(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        (elsewhere / ".git").mkdir(parents=True)
        self.gemini_session(cwd=elsewhere)
        self.assert_notify("not inside an approved root")

    def test_cwd_is_home(self):
        (self.home / ".git").mkdir()
        self.gconfig(roots=[os.path.realpath(str(self.tmp.name))])
        self.gemini_session(cwd=self.home)
        self.assert_notify("home directory or above it")

    def test_cwd_must_be_a_git_work_tree(self):
        plain = self.work / "plain"
        plain.mkdir()
        self.gemini_session(cwd=plain)
        self.assert_notify("not a git work tree")

    def test_a_symlinked_cwd_is_judged_by_its_real_path(self):
        outside = Path(self.tmp.name) / "outside"
        (outside / ".git").mkdir(parents=True)
        link = self.work / "link"
        os.symlink(str(outside), str(link))
        self.gemini_session(cwd=link)
        self.assert_notify("not inside an approved root")

    def test_missing_project_record(self):
        (self.chat.parent.parent / ".project_root").unlink()
        self.assert_notify("project record")

    def test_enable_root_is_realpathed_and_listed(self):
        link = Path(self.tmp.name) / "worklink"
        os.symlink(str(self.work), str(link))
        out = []
        code = w.cli_main(["enable", "--root", str(link), "--root", str(self.work)], runtime="gemini",
                          home=self.home, system=self.system(out=out.append), launchctl=lambda a: (0, ""),
                          platform="darwin", environ={"PATH": "/usr/bin"})
        self.assertEqual(code, 0, out)
        self.assertEqual(w.read_config(self.home)["roots"], [os.path.realpath(str(self.work))])
        code = w.cli_main(["enable"], runtime="gemini", home=self.home, system=self.system(out=out.append),
                          launchctl=lambda a: (0, ""), platform="darwin", environ={"PATH": "/usr/bin"})
        self.assertEqual(w.read_config(self.home)["roots"], [os.path.realpath(str(self.work))])   # kept
        status = []
        w.status(self.home, self.system(), status.append)
        self.assertIn("approved roots: " + os.path.realpath(str(self.work)), "\n".join(status))

    def test_enable_refuses_home_and_its_ancestors(self):
        for bad in (str(self.home), str(self.home.parent), "/"):
            with self.subTest(bad):
                out = []
                code = w.cli_main(["enable", "--root", bad], runtime="gemini", home=self.home,
                                  system=self.system(out=out.append), launchctl=lambda a: (0, ""), platform="darwin",
                                  environ={"PATH": "/usr/bin"})
                self.assertEqual(code, 2)
                self.assertIn("home directory or above it", "\n".join(out))


class History(GeminiBase):
    def test_an_unsandboxed_turn_anywhere(self):
        ie.record_sandbox_turn(self.home, GSID, environ={})
        self.assert_notify("outside Gemini's sandbox")

    def test_a_binding_under_the_real_home(self):
        ie.write_binding(self.home, GSID, binding())
        # the relocated binding is then shadowed; the session is still refused
        with gemini_proven():
            self.gpass()
        self.assertEqual(self.world.launches, [])
        self.assertIn("outside Gemini's sandbox", self.gstate()["reason"])

    def test_no_sandboxed_turn_recorded(self):
        w_path = ie.sandbox_turns_path(self.boxed_home, GSID)
        w_path.unlink()
        self.assert_notify("no sandboxed turn")

    def test_turns_are_counted_in_the_home_their_process_could_write(self):
        sid = "2b2c3d4e-0000-4000-8000-000000000000"
        ie.record_sandbox_turn(self.home, sid, environ={})
        with patch.dict(os.environ, BOXED):
            ie.record_sandbox_turn(self.home, sid)
            ie.record_sandbox_turn(self.home, sid)
        self.assertEqual(ie.sandbox_turns(self.home, sid), {"sandboxed": 0, "unsandboxed": 1})
        self.assertEqual(ie.sandbox_turns(self.boxed_home, sid), {"sandboxed": 2, "unsandboxed": 0})


class Auth(GeminiBase):
    def test_vertex_is_not_supported(self):
        (self.home / ".gemini" / "settings.json").write_text(
            json.dumps({"security": {"auth": {"selectedType": "vertex-ai"}}}))
        self.assert_notify("not supported for an unattended wake")

    def test_an_api_key_only_in_the_keychain(self):
        (self.home / ".gemini" / ".env").write_text("OTHER=1\n")
        self.assert_notify("put GEMINI_API_KEY in ~/.gemini/.env")


class ProjectGuard(GeminiBase):
    def write(self, rel, text="", base=None):
        p = (base or self.grepo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_custom_sandbox_files(self):
        for name in ("sandbox-macos-mine.sb", "sandbox.Dockerfile", "sandbox.bashrc"):
            with self.subTest(name):
                self.setUp()
                self.write(f".gemini/{name}")
                self.assert_notify("customizes Gemini's sandbox")

    def test_env_files_setting_sandbox_variables(self):
        cases = [(".env", "SANDBOX=sandbox-exec"), (".gemini/.env", "export SEATBELT_PROFILE=permissive-open"),
                 (".env", "GEMINI_SANDBOX_PROXY_COMMAND=nc"), (".env", "GEMINI_CLI_HOME=/tmp/g"),
                 (".env", "SANDBOX_FLAGS=x"), (".env", "GEMINI_SANDBOX_IMAGE=x"), (".env", "GEMINI_SANDBOX=false")]
        for rel, text in cases:
            with self.subTest(rel=rel, text=text):
                self.setUp()
                self.write(rel, f"# c\nFOO=1\n{text}\n")
                self.assert_notify("sets " + text.replace("export ", "").split("=")[0])

    def test_an_env_file_above_the_repository_counts_too(self):
        self.write(".env", "SEATBELT_PROFILE=permissive-open\n", base=self.work)
        self.assert_notify("sets SEATBELT_PROFILE")

    def test_the_users_env_files(self):
        (self.home / ".gemini" / ".env").write_text("GEMINI_API_KEY=k\nGEMINI_CLI_HOME=/tmp/x\n")
        self.assert_notify("sets GEMINI_CLI_HOME")

    def test_a_benign_env_file(self):
        self.write(".env", "DATABASE_URL=postgres://x\nGEMINI_API_KEY=k\n")
        with gemini_proven():
            self.gpass()
        self.assertEqual(len(self.world.launches), 1, self.gstate())

    def test_project_settings_keys(self):
        cases = [{"tools": {"sandbox": False}}, {"tools": {"sandbox": "docker"}}, {"security": {"x": 1}},
                 {"context": {"includeDirectories": ["~/.cardinal"]}}, {"tools": {"sandboxAllowedPaths": []}},
                 {"mcpServers": {"x": {"command": "sh"}}}, {"hooks": {}}, {"tools": {"discoveryCommand": "x"}}]
        for settings in cases:
            with self.subTest(settings):
                self.setUp()
                self.write(".gemini/settings.json", json.dumps(settings))
                self.assert_notify("settings.json sets")

    def test_benign_project_settings_with_comments(self):
        self.write(".gemini/settings.json", '{\n // a "comment"\n "ui": {"theme": "x // not a comment"} /* b */\n}\n')
        self.write(".gemini/GEMINI.md", "# notes")
        with gemini_proven():
            self.gpass()
        self.assertEqual(len(self.world.launches), 1, self.gstate())

    def test_unparseable_settings(self):
        self.write(".gemini/settings.json", "{not json")
        self.assert_notify("cannot be parsed")

    def test_a_symlinked_gemini_dir(self):
        target = Path(self.tmp.name) / "g"
        target.mkdir()
        os.symlink(str(target), str(self.grepo / ".gemini"))
        self.assert_notify("not a plain directory")

    def test_a_symlinked_env_file(self):
        target = Path(self.tmp.name) / "e"
        target.write_text("FOO=1\n")
        os.symlink(str(target), str(self.grepo / ".env"))
        self.assert_notify("cannot be checked")

    @unittest.skipIf(os.geteuid() == 0, "root reads any directory")
    def test_an_unreadable_gemini_dir(self):
        d = self.grepo / ".gemini"
        d.mkdir()
        os.chmod(str(d), 0)
        self.addCleanup(os.chmod, str(d), 0o700)
        self.assert_notify("cannot be read")

    def test_user_settings_that_widen_the_sandbox(self):
        (self.home / ".gemini" / "settings.json").write_text(json.dumps({
            "security": {"auth": {"selectedType": "gemini-api-key"}}, "context": {"includeDirectories": ["/tmp"]}}))
        self.assert_notify("sets context.includeDirectories")

    def test_a_parent_gemini_dir_within_the_repo(self):
        sub = self.grepo / "pkg"
        sub.mkdir()
        self.write(".gemini/sandbox.bashrc")
        self.gemini_session(cwd=sub)
        self.assert_notify("customizes Gemini's sandbox")


class StateRelocation(unittest.TestCase):
    def test_state_home(self):
        home = Path("/Users/x")
        self.assertEqual(paths.state_home(home, {}), home)
        self.assertEqual(paths.state_home(home, BOXED), home / ".cache" / "cardinal" / "sandboxed")
        self.assertEqual(paths.state_home(home, {"SANDBOX": "docker"}), home)
        boxed = paths.sandboxed_home(home)
        self.assertEqual(paths.state_home(boxed, BOXED), boxed)          # idempotent
        self.assertEqual(paths.state_home(boxed, {}), boxed)
        self.assertEqual(paths.unsandboxed_home(boxed), home)

    def test_hook_state_and_evidence_move_inside_the_sandbox_and_wake_state_never_does(self):
        home = Path("/Users/x")
        with patch.dict(os.environ, BOXED):
            self.assertEqual(ie.sessions_dir(home),
                             home / ".cache/cardinal/sandboxed/.cardinal/investigations/sessions")
            self.assertEqual(evidence.default_root(home), home / ".cache/cardinal/sandboxed/.cardinal/evidence")
            self.assertEqual(w.wake_dir(home), home / ".cardinal" / "wake")
            self.assertEqual(w.ledger_path(home, GSID).parent, home / ".cardinal" / "wake" / "ledger")
            self.assertEqual(w.disabled_path(home, "gemini"), home / ".cardinal" / "wake" / "disabled" / "gemini")
        with patch.dict(os.environ, {"SANDBOX": ""}):
            self.assertEqual(ie.sessions_dir(home), home / ".cardinal" / "investigations" / "sessions")
            self.assertEqual(evidence.default_root(home), home / ".cardinal" / "evidence")

    def test_checkpoint_holds_land_in_the_sandboxed_spool(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            boxed_root = paths.sandboxed_home(home) / ".cardinal" / "evidence"
            (boxed_root / GSID).mkdir(parents=True)
            with patch.dict(os.environ, BOXED):
                evidence.hold_after_checkpoint(home, GSID, env={})
            self.assertEqual(evidence.read_hold(boxed_root, GSID)["reason"], "checkpoint")
            self.assertFalse((home / ".cardinal" / "evidence").exists())

    def test_the_waker_sets_no_hold_in_a_sandboxed_spool(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            boxed = paths.sandboxed_home(home)
            (boxed / ".cardinal" / "evidence" / GSID).mkdir(parents=True)
            w.wake_hold(home, GSID, boxed)
            self.assertIsNone(evidence.read_hold(boxed / ".cardinal" / "evidence", GSID))

    def test_the_users_evidence_opt_out_reaches_the_sandboxed_spool(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            with patch.dict(os.environ, BOXED):
                root = evidence.default_root(home)
                self.assertFalse(evidence.capture_disabled(root, {}))
                (home / ".cardinal" / "evidence").mkdir(parents=True)
                (home / ".cardinal" / "evidence" / evidence.DISABLED_FLAG).write_text("")
                self.assertTrue(evidence.capture_disabled(root, {}))


class WakerReadsRelocatedBindings(GeminiBase):
    def test_candidate_from_the_sandboxed_home(self):
        found = {sid: state for sid, _, _, state in w.candidate_sessions(self.home, self.world.t)}
        self.assertEqual(found[GSID], self.boxed_home)
        self.assertEqual(found["01a13279-3459-7433-8ae2-7f81520c32e9"], self.home)

    def test_a_relocated_binding_is_only_a_hint(self):
        # it names a decision that is broadcast and unverified: nothing happens
        self.world.events = [decision(5, to=None), {**decision(6, to=GSID), "authority": "producer_claim"}]
        with gemini_proven():
            self.gpass()
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.ledger(GSID), {})

    def test_the_waker_never_writes_under_the_sandboxed_home(self):
        before = sorted(p for p in self.boxed_home.rglob("*") if p.is_file())
        with gemini_proven():
            self.gpass()
        after = sorted(p for p in self.boxed_home.rglob("*") if p.is_file())
        self.assertEqual(before, after)
        self.assertTrue(w.ledger_path(self.home, GSID).is_file())


# ---------------------------------------------------------------------------
# The allow-list proxy
# ---------------------------------------------------------------------------

class Proxy(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.echo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.echo.bind(("127.0.0.1", 0))
        self.echo.listen(4)
        threading.Thread(target=self._echo, daemon=True).start()
        self.upstreams = []

        def upstream(host, port):
            self.upstreams.append((host, port))
            return socket.create_connection(self.echo.getsockname(), timeout=5)
        self.server = proxy.make_server(["allowed.example.com", "Other.Example.org"], port=0,
                                        log=lambda v, t, why="": self.lines.append((v, t, why)), upstream=upstream)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.echo.close)
        self.port = self.server.server_address[1]

    def _echo(self):
        while True:
            try:
                c, _ = self.echo.accept()
            except OSError:
                return
            data = c.recv(1024)
            c.sendall(b"echo:" + data)
            c.close()

    def ask(self, head: bytes, family=socket.AF_INET, host="127.0.0.1"):
        s = socket.socket(family, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.settimeout(5)
        s.connect((host, self.port))
        s.sendall(head)
        return s

    def response(self, s):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        return data

    def test_an_allowed_host_on_443_is_tunnelled(self):
        s = self.ask(b"CONNECT allowed.example.com:443 HTTP/1.1\r\nHost: allowed.example.com:443\r\n\r\n")
        self.assertTrue(self.response(s).startswith(b"HTTP/1.1 200"))
        s.sendall(b"hello")
        self.assertEqual(s.recv(1024), b"echo:hello")
        s.close()
        self.assertEqual(self.upstreams, [("allowed.example.com", 443)])
        self.assertIn(("ALLOW", "allowed.example.com:443", ""), self.lines)

    def test_host_matching_is_exact_and_case_insensitive(self):
        s = self.ask(b"CONNECT other.example.ORG:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(self.response(s).startswith(b"HTTP/1.1 200"))
        s.close()
        for target in (b"evil.allowed.example.com:443", b"allowed.example.com.evil.net:443", b"example.com:443",
                       b"127.0.0.1:443", b"[::1]:443"):
            with self.subTest(target):
                s = self.ask(b"CONNECT " + target + b" HTTP/1.1\r\n\r\n")
                self.assertTrue(self.response(s).startswith(b"HTTP/1.1 403"))
                s.close()
        self.assertEqual(self.upstreams, [("other.example.org", 443)])

    def test_only_port_443(self):
        s = self.ask(b"CONNECT allowed.example.com:80 HTTP/1.1\r\n\r\n")
        self.assertTrue(self.response(s).startswith(b"HTTP/1.1 403"))
        self.assertIn(("DENY", "allowed.example.com:80", "port not allowed"), self.lines)

    def test_only_connect(self):
        s = self.ask(b"GET http://allowed.example.com/ HTTP/1.1\r\nHost: allowed.example.com\r\n\r\n")
        self.assertTrue(self.response(s).startswith(b"HTTP/1.1 405"))
        self.assertEqual(self.upstreams, [])

    def test_malformed(self):
        s = self.ask(b"CONNECT nonsense\r\n\r\n")
        self.assertTrue(self.response(s).startswith(b"HTTP/1.1 400"))

    def test_ipv6_loopback_too(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        try:
            s = self.ask(b"CONNECT allowed.example.com:443 HTTP/1.1\r\n\r\n", socket.AF_INET6, "::1")
        except OSError:
            self.skipTest("no IPv6 loopback")
        self.assertTrue(self.response(s).startswith(b"HTTP/1.1 200"))

    def test_a_non_loopback_peer_is_refused(self):
        handler = proxy.make_handler(["allowed.example.com"], lambda v, t, why="": self.lines.append((v, t, why)),
                                     lambda h, p: None)
        a, b = socket.socketpair()
        try:
            handler(a, ("192.168.1.9", 5555, 0, 0), None)
        finally:
            a.close()
            b.close()
        self.assertIn(("DENY", "192.168.1.9", "not a loopback peer"), self.lines)

    def test_the_log_file_is_private_and_carries_only_verdicts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            log = Path(t) / "wake.log"
            logger = proxy._Logger(str(log))
            logger("DENY", "x.example:443", "host not allowed")
            self.assertEqual(oct(log.stat().st_mode & 0o777), "0o600")
            self.assertRegex(log.read_text(), r"proxy DENY x\.example:443 \(host not allowed\)\n$")

    def test_main_refuses_anything_but_host_names(self):
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(proxy.main(["--port", "0", "allowed.example.com", "1.2.3.4"]), 2)
            self.assertEqual(proxy.main(["--port", "0", "*.example.com"]), 2)
        self.assertIn("not host names", err.getvalue())


# ---------------------------------------------------------------------------
# HTTPS_PROXY in the plugin's own openers (hooks inside a proxied sandbox)
# ---------------------------------------------------------------------------

class HonorsHttpsProxy(unittest.TestCase):
    def setUp(self):
        self.seen = []
        seen = self.seen

        class Recorder(BaseHTTPRequestHandler):
            def do_CONNECT(self):   # noqa: N802
                seen.append(self.path)
                self.send_response(403)
                self.end_headers()

            def log_message(self, *a):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        url = f"http://127.0.0.1:{self.server.server_address[1]}"
        env = patch.dict(os.environ, {"HTTPS_PROXY": url, "https_proxy": url, "NO_PROXY": "", "no_proxy": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_the_no_redirect_opener_tunnels_through_the_proxy(self):
        with self.assertRaises((urllib.error.URLError, OSError)):
            no_redirect_opener().open("https://cardinal.invalid/x", timeout=5)
        self.assertEqual(self.seen, ["cardinal.invalid:443"])

    def test_a_maestro_call_goes_through_the_proxy(self):
        with self.assertRaises(Exception):
            sync._post({**MEMBER, "origin": "https://cardinal.invalid"}, "read-investigation-events", {},
                       client="gemini", timeout=5)
        self.assertEqual(self.seen, ["cardinal.invalid:443"])

    def test_the_grant_opener_too(self):
        with self.assertRaises(Exception):
            sync._post({**MEMBER, "origin": "https://cardinal.invalid"}, "grant-investigation-access", {},
                       client="gemini", timeout=5)
        self.assertEqual(self.seen, ["cardinal.invalid:443"])


if __name__ == "__main__":
    unittest.main()
