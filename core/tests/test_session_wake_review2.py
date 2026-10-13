"""Regression tests for the second security review of PR #209 (head 3223d8d):
N1-N5, L-a, L-b and the Gemini profile. The reviewer's repro cases
(test_r2.py) are here with their assertions turned around; each test here
failed before its fix.

Same fakes as tests.test_session_wake: a temp HOME, a fixed clock, fake
process table / launcher / notifier / Cardinal reads / codex probes, an
injected launchctl. Nothing starts an agent or touches launchd; the only
real processes are /usr/bin/touch and /usr/bin/true under sandbox-exec
(macOS) to check what the shipped Gemini profile lets a process write.

Run from core/:  python3 -m unittest tests.test_session_wake_review2 -v
"""

from __future__ import annotations

import json
import os
import plistlib
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import owner_input as oi
from cardinal_core import paths
from cardinal_core import session_wake as w

from tests.test_session_wake import INV, MACHO, SECRET, SID, Base, ack, decision, turn


class Cli(Base):
    def cli(self, *argv, environ):
        out = []
        code = w.cli_main(list(argv), runtime="codex", home=self.home, system=self.world.system(out=out.append),
                          launchctl=lambda a: (0, ""), platform="darwin", environ=environ)
        return code, "\n".join(out)

    def finish(self, *turns, acked=True):
        """The launched run records `turns`, acknowledges #5 and exits; one more pass settles it."""
        self.append_turns(SID, *turns)
        if acked:
            self.world.events.append(ack(6, 5))
        self.world.exit()
        self.world.t += 60
        self.run_pass()


# ---------------------------------------------------------------------------
# N1: hooks run outside the sandbox with cwd = the workspace
# ---------------------------------------------------------------------------

class Hooks(Cli):
    def test_repro_user_hooks_json_into_the_cwd_never_runs(self):
        """The reviewer's hooks.json (a hook resolving into the workspace): the woken run
        is launched with hooks disabled, so no hook runs, Cardinal's or the user's."""
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True, exist_ok=True)
        command = f"python3 {self.repo}/adapters/codex/hooks/cardinal-codex-telemetry.py"
        hooks.write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": command}]}]}}))
        self.world.events = [decision(5)]
        self.run_pass()
        argv = self.world.launches[0]["argv"]
        i = argv.index("hooks")
        self.assertEqual(argv[i - 1], "--disable")
        self.assertLess(i, argv.index("resume"))
        self.assertIn("hooks", w.CODEX_DISABLED_FEATURES)

    def test_a_codex_that_cannot_disable_hooks_is_notify_only(self):
        rows = [f"{f:<40} stable             false" for f in w.CODEX_DISABLED_FEATURES if f != "hooks"]
        for hooks_row in ("hooks                                    stable             true", None):
            with self.subTest(hooks_row=hooks_row):
                self.setUp()
                self.world.events = [decision(5)]
                self.world.probe_answer = (0, "\n".join(rows + ([hooks_row] if hooks_row else [])) + "\n")
                self.run_pass()
                self.assertIn("'hooks'", self.notified_only())

    def test_the_prompt_names_the_investigation_and_the_read_tool_never_the_decision(self):
        self.world.events = [decision(5)]
        self.run_pass()
        prompt = self.world.launches[0]["argv"][-1]
        self.assertEqual(prompt, oi.wake_prompt(5, INV))
        self.assertIn(INV, prompt)
        self.assertIn("storyboard__read_investigation_events", prompt)
        self.assertNotIn(SECRET, prompt)
        for runtime in ("codex", "claude", "gemini"):
            self.assertIsNone(oi.normalize_prompt(runtime, prompt))
            self.assertTrue(oi.is_machine_prompt(runtime, prompt))


# ---------------------------------------------------------------------------
# N2: a workspace-chosen codex binary
# ---------------------------------------------------------------------------

def npm_install(root: Path) -> Path:
    """A fake `npm i -g @openai/codex` tree under root: bin/codex -> codex.js,
    and both darwin native binaries (never executed). Returns bin/codex."""
    pkg = root / "lib" / "node_modules" / "@openai" / "codex"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "@openai/codex", "version": "0.162.1"}))
    js = pkg / "bin" / "codex.js"
    js.write_text("#!/usr/bin/env node\nrequire('child_process').spawnSync(process.env.X)\n")
    js.chmod(0o755)
    for triple in ("aarch64-apple-darwin", "x86_64-apple-darwin"):
        native = pkg / "vendor" / triple / "codex" / "codex"
        native.parent.mkdir(parents=True)
        native.write_bytes(MACHO + triple.encode())
        native.chmod(0o755)
    link = root / "bin" / "codex"
    link.parent.mkdir(parents=True)
    link.symlink_to(js)
    return link


class CodexBinary(Cli):
    def test_repro_an_asdf_shim_is_refused(self):
        shims = self.home / ".asdf" / "shims"
        shims.mkdir(parents=True)
        shim = shims / "codex"
        shim.write_text('#!/usr/bin/env bash\nexec asdf exec "codex" "$@"\n')
        shim.chmod(0o755)
        code, out = self.cli("enable", "--launch", environ={"PATH": f"{shims}:/usr/bin:/bin"})
        self.assertEqual(code, 2, out)
        self.assertIn("version-manager shim", out)
        self.assertFalse(w.plist_path(self.home).exists())

    def test_every_shim_dir_is_refused_even_when_it_resolves_to_a_native_binary(self):
        for rel in (".volta/bin", ".nvm/versions/node/v22/bin", ".local/share/mise/shims", ".nodenv/shims"):
            with self.subTest(rel):
                self.setUp()
                d = self.home / rel
                d.mkdir(parents=True)
                (d / "codex").write_bytes(MACHO)
                (d / "codex").chmod(0o755)
                code, out = self.cli("enable", "--launch", environ={"PATH": f"{d}:/usr/bin:/bin"})
                self.assertEqual(code, 2, out)
                self.assertIn("version-manager shim", out)

    def test_a_codex_script_is_refused(self):
        self.codex.write_text("#!/bin/sh\nexec \"$(cat .codex-bin)\" \"$@\"\n")
        code, out = self.cli("enable", "--launch", environ={"PATH": f"{self.codex.parent}:/usr/bin:/bin"})
        self.assertEqual(code, 2, out)
        self.assertIn("not a native (Mach-O) executable", out)

    def test_a_native_codex_is_accepted_and_fingerprinted(self):
        code, out = self.cli("enable", "--launch", environ={"PATH": f"{self.codex.parent}:/usr/bin:/bin"})
        self.assertEqual(code, 0, out)
        c = w.read_config(self.home)
        real = os.path.realpath(str(self.codex))
        self.assertEqual(c["codex_path"], real)
        self.assertEqual(c["binaries"][real], w.fingerprint(real))
        self.assertIsNone(c["node_path"])

    def test_an_npm_install_launches_its_native_binary_directly(self):
        link = npm_install(self.home / "npm")
        code, out = self.cli("enable", "--launch", environ={"PATH": f"{link.parent}:/usr/bin:/bin"})
        self.assertEqual(code, 0, out)
        c = w.read_config(self.home)
        triple = "aarch64-apple-darwin" if platform.machine() == "arm64" else "x86_64-apple-darwin"
        native = os.path.realpath(str(self.home / "npm" / "lib" / "node_modules" / "@openai" / "codex" / "vendor"
                                      / triple / "codex" / "codex"))
        self.assertEqual(c["codex_path"], native)
        self.assertIn(native, c["binaries"])
        self.assertIsNone(c["node_path"])                       # node never runs codex.js
        self.assertNotIn(os.path.realpath(str(link)), c["binaries"])
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches[0]["argv"][0], native)

    def test_a_package_that_is_not_openai_codex_is_refused(self):
        link = npm_install(self.home / "npm")
        pkg = Path(os.path.realpath(str(link))).parent.parent
        (pkg / "package.json").write_text(json.dumps({"name": "evil-codex"}))
        code, out = self.cli("enable", "--launch", environ={"PATH": f"{link.parent}:/usr/bin:/bin"})
        self.assertEqual(code, 2, out)

    def test_a_vetted_codex_that_is_a_script_or_a_shim_blocks_launch(self):
        script = self.home / "tools" / "codex"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\n")
        shim = self.home / ".asdf" / "installs" / "codex"
        shim.parent.mkdir(parents=True)
        shim.write_bytes(MACHO)
        script.chmod(0o755)
        shim.chmod(0o755)
        for path, why in ((script, "not a native"), (shim, "version-manager shim")):
            with self.subTest(why):
                real = os.path.realpath(str(path))
                self.config(codex_path=real, binaries={**w.read_config(self.home)["binaries"],
                                                       real: w.fingerprint(real)})
                w.write_ledger(self.home, SID, {"entries": {}}, self.t0)
                self.world.events = [decision(5)]
                self.world.notes.clear()
                self.run_pass()
                self.assertEqual(self.world.launches, [])
                self.assertIn(why, self.ledger()["5"]["reason"])


# ---------------------------------------------------------------------------
# N3: the child environment is an allow-list
# ---------------------------------------------------------------------------

HOSTILE = {"NODE_OPTIONS": "--require /x/evil.js", "DYLD_INSERT_LIBRARIES": "/x/e.dylib",
           "DYLD_LIBRARY_PATH": "/x", "OPENAI_BASE_URL": "https://attacker", "CODEX_BASE_URL": "https://a",
           "NODE_PATH": "/x", "PATH": "/evil", "HTTPS_PROXY": "http://attacker:1", "SSL_CERT_FILE": "/x/ca.pem",
           "CODEX_CA_CERTIFICATE": "/x/ca.pem", "PYTHONPATH": "/x", "BASH_ENV": "/x/rc", "ZDOTDIR": "/x",
           "GIT_CONFIG_GLOBAL": "/x/gitconfig", "CODEX_HOME": "/x", "CARDINAL_OWNER_INPUT": "0",
           "TMPDIR": "/tmp/shared/", "TERM": "xterm-256color"}
SAFE = {"HOME": "/Users/u", "USER": "u", "LOGNAME": "u", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "SHELL": "/bin/zsh", "OPENAI_API_KEY": "sk-test"}


class Environment(Cli):
    def test_repro_child_env_drops_loader_and_endpoint_variables(self):
        env = w.child_env({**HOSTILE, **SAFE}, "/usr/bin", extra=w.CODEX_AUTH_ENV)
        for k in HOSTILE:
            if k not in ("PATH", "TERM"):
                self.assertNotIn(k, env)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["TERM"], "dumb")
        self.assertEqual({k: env[k] for k in SAFE}, SAFE)
        self.assertNotIn("OPENAI_API_KEY", w.child_env(SAFE, "/usr/bin"))   # only when named

    def test_the_launch_env_is_exactly_the_allow_list_with_a_private_tmpdir(self):
        self.world.events = [decision(5)]
        self.run_pass(environ={**HOSTILE, **SAFE})
        env = self.world.launches[0]["env"]
        self.assertLessEqual(set(env), {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TERM", "PATH",
                                        "TMPDIR", "OPENAI_API_KEY"})
        self.assertEqual(env["HOME"], str(self.home))
        tmp = env["TMPDIR"]
        self.assertEqual(os.path.dirname(tmp), os.path.realpath(str(w.wake_dir(self.home) / "tmp")))
        self.assertEqual(oct(os.stat(tmp).st_mode & 0o777), "0o700")

    def test_the_probe_env_is_the_same_allow_list(self):
        seen = {}

        def run(argv, **kw):
            seen.update(kw["env"])
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.dict(os.environ, {**HOSTILE, **SAFE}, clear=True), patch.object(w.subprocess, "run", run):
            w.run_probe(["/x/codex", "features", "list"], "/usr/bin:/bin")
        self.assertLessEqual(set(seen), {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SHELL", "TERM", "PATH",
                                         "OPENAI_API_KEY"})
        self.assertEqual(seen["PATH"], "/usr/bin:/bin")

    def test_the_plist_sets_only_path(self):
        code, out = self.cli("enable", "--launch", environ={**HOSTILE, **SAFE,
                                                            "PATH": f"{self.codex.parent}:/usr/bin:/bin"})
        self.assertEqual(code, 0, out)
        plist = plistlib.loads(w.plist_path(self.home).read_bytes())
        self.assertEqual(set(plist["EnvironmentVariables"]), {"PATH"})


# ---------------------------------------------------------------------------
# N4: drift (an untested default-on feature; MCP layers the waker does not parse)
# ---------------------------------------------------------------------------

class Drift(Cli):
    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]

    def features(self, *on):
        rows = [f"{f:<40} stable             false" for f in w.CODEX_DISABLED_FEATURES]
        rows += [f"{f:<40} stable             true" for f in on]
        return 0, "\n".join(rows) + "\n"

    def test_the_tested_set_launches(self):
        self.world.probe_answer = self.features(*sorted(w.CODEX_TESTED_ON))
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_a_new_default_on_feature_is_notify_only(self):
        self.world.probe_answer = self.features("code_mode_host", "agent_message_board")
        self.run_pass()
        self.assertIn("not tested with (agent_message_board)", self.notified_only())

    def test_a_feature_known_but_off_is_fine(self):
        _, text = self.features("code_mode_host")
        self.world.probe_answer = (0, text + "agent_message_board                      under development  false\n")
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_mcp_list_must_show_only_cardinal_enabled(self):
        cases = {
            "another server": (0, json.dumps([{"name": "cardinal", "enabled": True},
                                              {"name": "cloud_managed", "enabled": True}])),
            "no enabled flag": (0, json.dumps([{"name": "cardinal", "enabled": True}, {"name": "x"}])),
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.setUp()
                self.world.mcp_answer = answer
                self.run_pass()
                self.assertIn("other than Cardinal stay enabled", self.notified_only())

    def test_mcp_list_that_fails_is_notify_only(self):
        for name, answer in {"missing subcommand": (2, "error: unrecognized subcommand 'mcp'"),
                             "not json": (0, "Name  Command\ncardinal  -\n"),
                             "not a list": (0, json.dumps({"servers": []}))}.items():
            with self.subTest(name):
                self.setUp()
                self.world.mcp_answer = answer
                self.run_pass()
                self.assertIn("could not list its MCP servers", self.notified_only())

    def test_mcp_list_that_cannot_run_is_notify_only(self):
        def probe(argv, path_env):
            if "mcp" in argv:
                raise OSError("gone")
            return self.world.probe(argv, path_env)
        self.run_pass(run_probe=probe)
        self.assertIn("could not list its MCP servers", self.notified_only())

    def test_a_changed_codex_config_is_probed_again(self):
        self.run_pass()
        self.finish(turn())
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text('model = "other"\n')
        self.world.events.append(decision(7))
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len([p for p in self.world.probes if p[-2:] == ["features", "list"]]), 2)


# ---------------------------------------------------------------------------
# N5: the fingerprint
# ---------------------------------------------------------------------------

class Fingerprint(Base):
    def test_repro_an_in_place_rewrite_with_the_mtime_restored_changes_the_fingerprint(self):
        p = self.codex
        fp = w.fingerprint(str(p))
        st = os.stat(p)
        data = p.read_bytes()
        with open(p, "r+b") as f:
            f.write(b"X" * len(data))
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertNotEqual(w.fingerprint(str(p)), fp)

    def test_the_fingerprint_carries_ctime_and_sha256(self):
        import hashlib
        fp = w.fingerprint(str(self.codex))
        st = os.stat(self.codex)
        self.assertEqual(fp, [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns,
                              hashlib.sha256(self.codex.read_bytes()).hexdigest()])


# ---------------------------------------------------------------------------
# L-a: quoted keys; the sandbox pinned on the command line
# ---------------------------------------------------------------------------

class SandboxPins(Cli):
    def test_repro_quoted_keys_are_detected(self):
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        for text in ('["sandbox_workspace_write"]\n"writable_roots" = ["/Users"]\n"network_access" = true\n',
                     "['sandbox_workspace_write']\n", "'network_access' = true\n",
                     '"sandbox_workspace_write".network_access = true\n', "[profiles.x.sandbox_workspace_write]\n"):
            with self.subTest(text):
                cfg.write_text(text)
                self.assertIn("widens the workspace-write sandbox", w.codex_config_problem(self.home) or "")

    def test_the_launch_pins_network_roots_and_tmp(self):
        self.world.events = [decision(5)]
        self.run_pass()
        argv = self.world.launches[0]["argv"]
        pins = [argv[i + 1] for i, a in enumerate(argv[:argv.index("resume")]) if a == "-c"]
        for pin in ("sandbox_workspace_write.network_access=false", "sandbox_workspace_write.writable_roots=[]",
                    "sandbox_workspace_write.exclude_slash_tmp=true",
                    "sandbox_workspace_write.exclude_tmpdir_env_var=false"):
            self.assertIn(pin, pins)
        self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", self.world.probes[0])   # probed too

    def test_a_woken_turn_with_any_writable_root_disables_codex(self):
        """Unknown -c keys are accepted silently, so the pins are read back from the woken turns."""
        self.world.events = [decision(5)]
        self.run_pass()
        t = turn()
        t["payload"]["sandbox_policy"]["writable_roots"] = [str(self.repo / "build")]   # inside cwd, still not []
        self.finish(t)
        self.assertIn("writable_roots=[]", w.disabled_reason(self.home, "codex"))


# ---------------------------------------------------------------------------
# L-b: the shared /tmp; a private TMPDIR
# ---------------------------------------------------------------------------

class PrivateTmp(Cli):
    def test_a_woken_turn_that_could_write_slash_tmp_disables_codex(self):
        for name, make in (("exclude_slash_tmp false", lambda: turn(slash_tmp=True)),
                           ("slash_tmp entry", lambda: self.fs_turn("slash_tmp"))):
            with self.subTest(name):
                self.setUp()
                self.world.events = [decision(5)]
                self.run_pass()
                self.finish(make())
                self.assertIn("shared /tmp", w.disabled_reason(self.home, "codex") or "")

    def fs_turn(self, kind):
        t = turn()
        t["payload"]["file_system_sandbox_policy"] = {"kind": "restricted", "entries": [
            {"path": {"type": "special", "value": {"kind": kind}}, "access": "write"}]}
        return t

    def test_the_private_tmpdir_stays_writable_and_is_removed_when_the_wake_settles(self):
        self.world.events = [decision(5)]
        self.run_pass()
        tmp = self.world.launches[0]["env"]["TMPDIR"]
        self.assertTrue(os.path.isdir(tmp))
        self.assertEqual(self.ledger()["5"]["tmp_dir"], tmp)
        (Path(tmp) / "scratch").write_text("x")
        self.finish(self.fs_turn("tmpdir"))
        self.assertIsNone(w.disabled_reason(self.home, "codex"))
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.assertFalse(os.path.exists(tmp))

    def test_a_failed_spawn_removes_the_private_tmpdir(self):
        self.world.events = [decision(5)]
        self.world.spawn_errors = 99
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        self.assertEqual(os.listdir(str(w.wake_dir(self.home) / "tmp")), [])

    def test_remove_tmpdir_only_removes_a_wake_tmpdir(self):
        other = self.home / "keep"
        other.mkdir()
        w.remove_tmpdir(self.home, str(other))
        w.remove_tmpdir(self.home, "relative")
        self.assertTrue(other.exists())


# ---------------------------------------------------------------------------
# Gemini (launch gated off): the profile's home writes
# ---------------------------------------------------------------------------

PROFILE = Path(w.__file__).parent / w.GEMINI_SEATBELT_FILE


class GeminiProfile(unittest.TestCase):
    def test_the_profile_grants_no_broad_home_writes(self):
        text = PROFILE.read_text()
        self.assertNotIn('"/.npm"', text)
        self.assertNotIn('"/.cache")', text)
        self.assertIn('(subpath (string-append (param "HOME_DIR") "/.cache/cardinal/sandboxed"))', text)

    @unittest.skipUnless(sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"), "macOS seatbelt")
    def test_under_the_profile_only_the_relocated_state_home_is_writable(self):
        with tempfile.TemporaryDirectory() as t:
            t = os.path.realpath(t)
            home, work = os.path.join(t, "home"), os.path.join(t, "work")
            for d in (home + "/.npm", home + "/.cache/other", home + "/.cache/cardinal/sandboxed", work):
                os.makedirs(d)
            params = [f"TARGET_DIR={work}", f"TMP_DIR={work}", f"HOME_DIR={home}", f"CACHE_DIR={work}"] + \
                [f"INCLUDE_DIR_{i}=/dev/null" for i in range(5)]
            base = ["/usr/bin/sandbox-exec"] + [a for p in params for a in ("-D", p)] + ["-f", str(PROFILE)]

            def touch(path):
                return subprocess.run(base + ["/usr/bin/touch", path], capture_output=True, timeout=30).returncode
            self.assertEqual(touch(home + "/.cache/cardinal/sandboxed/ok"), 0)
            self.assertNotEqual(touch(home + "/.npm/x"), 0)
            self.assertNotEqual(touch(home + "/.cache/other/x"), 0)
            self.assertNotEqual(touch(home + "/.cache/x"), 0)

    def test_gemini_prepare_creates_the_relocated_state_home(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t) / "home"
            home.mkdir()
            rt = w.Runtime(w.GEMINI, [])
            with patch.object(w, "darwin_cache_dir", lambda: t), patch.object(tempfile, "tempdir", t):
                self.assertIsNone(w.gemini_prepare(home, rt, {"python": sys.executable}, home / "l.log", None))
            self.assertTrue(paths.sandboxed_home(home).is_dir())


if __name__ == "__main__":
    unittest.main()
