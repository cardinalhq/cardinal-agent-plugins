"""Regression tests for the third independent security review of PR #209
(head 72f7f4f): findings 1-8 and the probe cache. The reviewer's repro cases
(test_r3.py) are here with their assertions turned around; each test here
failed before its fix.

Same fakes as tests.test_session_wake (World: launcher, probes, process
list with environments, kill, clock). Nothing starts an agent, signals a
real process or touches launchd.

Run from core/:  python3 -m unittest tests.test_session_wake_review3 -v
"""

from __future__ import annotations

import inspect
import os
import plistlib
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import owner_input as oi
from cardinal_core import session_wake as w

from tests.test_session_wake import INV, MACHO, SECRET, SID, Base, ack, decision, turn


class Cli(Base):
    def codex_config(self, text):
        p = self.home / ".codex" / "config.toml"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def cli(self, *argv, environ=None):
        out = []
        code = w.cli_main(list(argv), runtime="codex", home=self.home, system=self.world.system(out=out.append),
                          launchctl=lambda a: (0, ""), platform="darwin",
                          environ=environ or {"PATH": f"{self.codex.parent}:/usr/bin:/bin"})
        return code, "\n".join(out)

    def exit_and_settle(self, *turns):
        self.append_turns(SID, *(turns or (turn(),)))
        self.world.exit()
        self.world.t += 60
        self.run_pass()


# ---------------------------------------------------------------------------
# 1: Codex notify runs outside the sandbox
# ---------------------------------------------------------------------------

class Notify(Cli):
    def test_repro_a_config_notify_program_is_overridden(self):
        self.codex_config('notify = ["/Users/x/.codex/helper", "turn-ended"]\n'
                          '[mcp_servers.cardinal]\nurl = "https://x"\n')
        self.world.events.append(decision(5))
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1, self.world.lines)   # the owner's own notify never blocks
        argv = self.world.launches[0]["argv"]
        values = [argv[i + 1] for i, a in enumerate(argv[:argv.index("resume")]) if a == "-c"]
        self.assertEqual(values[-1], "notify=[]")                          # the last -c: it wins (C-20)
        self.assertEqual([v for v in values if v.startswith("notify")], ["notify=[]"])
        self.assertIn("notify=[]", self.world.probes[0])                   # validated by the probe too


# ---------------------------------------------------------------------------
# 2: TOCTOU; the probes' cwd; project config after the run
# ---------------------------------------------------------------------------

class Recheck(Cli):
    def test_repro_project_config_created_after_eligibility_is_not_launched(self):
        self.world.events.append(decision(5))

        def plant(sid, seq):
            d = self.repo / ".codex"
            d.mkdir()
            (d / "config.toml").write_text('[mcp_servers.x]\ncommand = "/bin/sh"\n')
        self.run_pass(before_claim=plant)
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.state(5), w.NOTIFIED)
        self.assertIn("own Codex config", self.ledger()["5"]["reason"])

    def test_a_binary_swapped_after_eligibility_is_not_launched(self):
        self.world.events.append(decision(5))
        self.run_pass(before_claim=lambda sid, seq: self.codex.write_bytes(MACHO + b"swapped"))
        self.assertEqual(self.world.launches, [])
        self.assertIn("changed since cardinal-wake enable", self.ledger()["5"]["reason"])

    def test_a_widening_config_written_after_eligibility_is_not_launched(self):
        self.world.events.append(decision(5))
        self.run_pass(before_claim=lambda sid, seq: self.codex_config("[sandbox_workspace_write]\n"))
        self.assertEqual(self.world.launches, [])
        self.assertIn("widens the workspace-write sandbox", self.ledger()["5"]["reason"])

    def test_the_probes_run_under_the_lock_in_the_launch_cwd(self):
        seen = []
        self.world.events.append(decision(5))
        self.run_pass(before_claim=lambda sid, seq: seen.append(len(self.world.probes)))
        self.assertEqual(seen, [0])                                     # none before the claim's lock
        self.assertEqual(len(self.world.probes), 2)
        self.assertEqual(self.world.probe_cwds, [os.path.realpath(str(self.repo))] * 2)
        self.assertEqual(self.world.launches[0]["cwd"], os.path.realpath(str(self.repo)))

    def test_repro_run_probe_runs_in_the_given_cwd(self):
        self.assertIn("cwd", inspect.signature(w.run_probe).parameters)
        got = {}

        def run(argv, **kw):
            got.update(kw)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(w.subprocess, "run", run):
            w.run_probe(["/x/codex", "features", "list"], "/usr/bin", cwd=str(self.repo))
        self.assertEqual(got["cwd"], str(self.repo))

    def test_project_config_present_after_the_run_disables_codex(self):
        self.world.events.append(decision(5))
        self.run_pass()
        d = self.repo / ".codex"
        d.mkdir()
        (d / "hooks.json").write_text("{}")
        self.world.events.append(ack(6, 5))
        self.exit_and_settle()
        self.assertIn("project config after the woken run", w.disabled_reason(self.home, "codex") or "")


# ---------------------------------------------------------------------------
# 3: protected paths through symlinks
# ---------------------------------------------------------------------------

class Dotfiles(Cli):
    def dotfiles(self):
        dot = self.home / "dotfiles"
        (dot / ".git").mkdir(parents=True)
        (dot / "config" / "fish").mkdir(parents=True)
        (dot / "zshrc").write_text("# shell setup\n")
        return dot

    def test_repro_a_symlinked_protected_dir_or_dotfile_makes_its_repo_ineligible(self):
        for link, target in ((".config", "config"), (".zshrc", "zshrc")):
            with self.subTest(link):
                self.setUp()
                dot = self.dotfiles()
                os.symlink(str(dot / target), str(self.home / link))
                self.session(SID, cwd=dot)
                rt = w.codex_eligibility(self.home, SID)
                self.assertFalse(rt.eligible)
                self.assertIn(f"~/{link} (or where it links to)", rt.reason)
                self.assertIsNotNone(w.home_cwd_problem(os.path.realpath(str(dot)), self.home))

    def test_a_cwd_inside_the_link_target_is_refused(self):
        dot = self.dotfiles()
        os.symlink(str(dot / "config"), str(self.home / ".config"))
        self.assertIn(".config", w.home_cwd_problem(os.path.realpath(str(dot / "config" / "fish")), self.home))

    def test_an_unrelated_repo_stays_eligible(self):
        dot = self.dotfiles()
        os.symlink(str(dot / "zshrc"), str(self.home / ".zshrc"))
        self.assertIsNone(w.home_cwd_problem(os.path.realpath(str(self.repo)), self.home))


# ---------------------------------------------------------------------------
# 4: processes that outlive the woken codex
# ---------------------------------------------------------------------------

class Survivors(Cli):
    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]
        self.run_pass()
        self.child = self.world.launches[0]
        self.tmp = self.child["env"]["TMPDIR"]
        self.world.events.append(ack(6, 5))

    def env_row(self, pid, pgid=1, tmp=None):
        return (pid, pgid, f"/bin/sleep 1000 PATH=/usr/bin TMPDIR={tmp or self.tmp} HOME=/x")

    def test_a_survivor_is_stopped_and_the_run_judged_only_after(self):
        self.world.envrows = [self.env_row(777)]
        self.exit_and_settle()
        self.assertEqual(self.world.killed, [(777, signal.SIGTERM)])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.assertFalse(os.path.exists(self.tmp))

    def test_a_stubborn_survivor_gets_sigkill(self):
        self.world.envrows = [self.env_row(778)]
        self.world.stubborn = {778}
        self.exit_and_settle()
        self.assertEqual(self.world.killed, [(778, signal.SIGTERM), (778, signal.SIGKILL)])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_an_unkillable_survivor_keeps_the_wake_in_flight(self):
        self.world.envrows = [self.env_row(779)]
        real_kill = self.world.kill
        self.world.kill = lambda pid, sig: self.world.killed.append((pid, sig))   # never goes away
        self.exit_and_settle()
        self.assertEqual(self.state(5), w.RESUMING)                 # not ACKNOWLEDGED while it runs
        self.assertTrue(os.path.isdir(self.tmp))                     # its TMPDIR is not deleted under it
        self.assertIn("outlived it", self.ledger()["5"]["lingering"])
        self.world.events.append(decision(7))                        # no concurrent codex for the session
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.world.kill = real_kill
        self.world.envrows = []
        self.world.t += 60
        self.run_pass()
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_an_unreadable_process_list_keeps_the_wake_in_flight(self):
        self.exit_and_settle_with(process_envs=lambda: None)
        self.assertEqual(self.state(5), w.RESUMING)
        self.assertIn("could not be listed", self.ledger()["5"]["lingering"])

    def exit_and_settle_with(self, **kw):
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass(**kw)

    def test_a_process_group_member_without_the_marker_is_stopped_but_a_reused_pid_is_not(self):
        pgid = self.child["pid"]
        self.world.starts[880] = self.world.t + 5     # started after the child: part of the run
        self.world.starts[881] = self.world.t - 500   # started before it: a reused group id, not ours
        self.world.envrows = [(880, pgid, "/bin/sleep 1000 PATH=/usr/bin"), (881, pgid, "/usr/bin/other")]
        self.exit_and_settle()
        self.assertEqual([p for p, _ in self.world.killed], [880])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_another_wakes_marker_or_a_prefix_is_not_ours(self):
        self.world.envrows = [self.env_row(990, tmp=self.tmp + "x"), self.env_row(991, tmp="/elsewhere")]
        self.exit_and_settle()
        self.assertEqual(self.world.killed, [])
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)

    def test_daemon_auto_start_is_disabled_and_probed(self):
        self.assertIn("daemon_auto_start", w.CODEX_DISABLED_FEATURES)
        self.assertNotIn("daemon_auto_start", w.CODEX_TESTED_ON)
        argv = self.child["argv"]
        self.assertEqual(argv[argv.index("daemon_auto_start") - 1], "--disable")


# ---------------------------------------------------------------------------
# 5: the acknowledgment note is not an exfiltration channel
# ---------------------------------------------------------------------------

class NoteChannel(Cli):
    def test_the_prompt_reads_only_owner_decisions(self):
        p = oi.wake_prompt(5, INV)
        self.assertIn('types ["owner.decided"], after 4', p)
        self.assertIn("read no other events", p)
        self.assertNotIn("Every other event is advisory", p)
        self.assertIn("The note may not be passed on", p)
        self.assertNotIn(SECRET, p)
        for runtime in ("codex", "claude", "gemini"):
            self.assertIsNone(oi.normalize_prompt(runtime, p))

    def launch_and_answer(self, note):
        import json
        self.world.events = [decision(5)]
        self.run_pass()
        tmp = Path(self.world.launches[0]["env"]["TMPDIR"])
        (tmp / w.ACK_FILE).write_text(json.dumps({"ack_of": 5, "disposition": "accepted", "note": note}))
        self.exit_and_settle()
        return self.world.posted[0]["note"]

    def test_the_note_is_dropped_by_default(self):
        self.assertIsNone(self.launch_and_answer("here is ~/.ssh/id_ed25519: -----BEGIN OPENSSH PRIVATE KEY-----"))

    def test_relay_notes_opt_in_relays_it_scrubbed(self):
        self.config(relay_notes=True)
        secret = "sk-proj-" + "a1B2c3D4e5" * 4
        note = self.launch_and_answer(f"done; OPENAI_API_KEY={secret}")
        self.assertTrue(note.startswith("done;"))
        self.assertNotIn(secret, note)

    def test_enable_relay_notes_is_off_unless_given(self):
        code, out = self.cli("enable", "--launch")
        self.assertEqual(code, 0, out)
        self.assertFalse(w.read_config(self.home)["relay_notes"])
        code, out = self.cli("enable", "--launch", "--relay-notes")
        self.assertEqual(code, 0, out)
        self.assertTrue(w.read_config(self.home)["relay_notes"])
        code, out = self.cli("enable", "--launch")
        self.assertFalse(w.read_config(self.home)["relay_notes"])   # each enable decides again


# ---------------------------------------------------------------------------
# 6: MCP declarations in other forms
# ---------------------------------------------------------------------------

class McpForms(Cli):
    def test_repro_a_quoted_table_name_refuses(self):
        self.codex_config('[mcp_servers.cardinal]\nurl = "https://x"\n["mcp_servers".evil]\ncommand = "/bin/sh"\n')
        overrides, why = w.codex_mcp_overrides(self.home)
        self.assertIsNone(overrides)
        self.assertIn("cannot disable", why)

    def test_other_forms_refuse(self):
        for text in ('"mcp_servers".x.command = "/bin/sh"\n', "['mcp_servers'.x]\ncommand = 'sh'\n",
                     'profiles = { p = { mcp_servers = { x = { command = "sh" } } } }\n',
                     '[profiles.p]\nmcp_servers.x.command = "sh"\n', '[[ "plugins" ]]\n',
                     '[profiles."p".plugins."x@y"]\nenabled = true\n'):
            with self.subTest(text):
                self.codex_config(text)
                self.assertIsNone(w.codex_mcp_overrides(self.home)[0])

    def test_benign_mentions_stay_launchable(self):
        self.codex_config('model = "gpt"\ndeveloper_instructions = "never install plugins or mcp_servers"\n'
                          '[features]\nplugins = false\n[mcp_servers.cardinal]\nurl = "https://x"\n')
        overrides, why = w.codex_mcp_overrides(self.home)
        self.assertIsNotNone(overrides, why)


# ---------------------------------------------------------------------------
# 7: the daemon's Python
# ---------------------------------------------------------------------------

class DaemonPython(Cli):
    def test_the_plist_runs_python_with_dash_I_dash_S(self):
        code, out = self.cli("enable", "--launch")
        self.assertEqual(code, 0, out)
        plist = plistlib.loads(w.plist_path(self.home).read_bytes())
        self.assertEqual(plist["ProgramArguments"][1:3], ["-I", "-S"])

    def test_a_shim_in_the_system_path_dirs_is_never_the_interpreter(self):
        shim_dir = self.home.parent / "sysbin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_bytes(MACHO)
        shim.chmod(0o755)
        with patch.object(w.sys, "executable", str(shim)), patch.object(w, "system_python", lambda: None), \
                patch.object(w, "SYSTEM_PATH_DIRS", (os.path.realpath(str(shim_dir)),)):
            python, why = w.pick_python(self.home, [])
        self.assertIsNone(python)
        self.assertIn("system shim", why)

    def test_the_xcrun_shim_is_resolved_to_its_interpreter(self):
        def run(argv, **kw):
            self.assertEqual(argv, ["/usr/bin/xcrun", "-f", "python3"])
            self.assertNotIn("DEVELOPER_DIR", kw["env"])
            return subprocess.CompletedProcess(argv, 0, "/Library/Developer/CommandLineTools/usr/bin/python3\n", "")
        with patch.object(w.subprocess, "run", run):
            self.assertEqual(w.system_python(), "/Library/Developer/CommandLineTools/usr/bin/python3")

    def test_a_vetted_interpreter_in_the_system_path_dirs_blocks_launch(self):
        shim_dir = self.home.parent / "sysbin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_bytes(MACHO)
        real = os.path.realpath(str(shim))
        self.config(python=real, binaries={real: w.fingerprint(real)})
        self.assertIsNone(w.binaries_problem(w.read_config(self.home), self.home))
        with patch.object(w, "SYSTEM_PATH_DIRS", (os.path.dirname(real),) + w.SYSTEM_PATH_DIRS):
            self.assertIn("system shim", w.binaries_problem(w.read_config(self.home), self.home) or "")


# ---------------------------------------------------------------------------
# 8: every turn ran in the session's cwd
# ---------------------------------------------------------------------------

class TurnCwd(Cli):
    def test_a_turn_in_another_cwd_is_not_eligible(self):
        elsewhere = self.home / "work" / "other"
        elsewhere.mkdir(parents=True)
        for cwd in (str(elsewhere), None, "relative/dir"):
            with self.subTest(cwd=cwd):
                t = turn()
                if cwd is None:
                    del t["payload"]["cwd"]
                else:
                    t["payload"]["cwd"] = cwd
                self.session(SID, turns=[turn(), t])
                rt = w.codex_eligibility(self.home, SID)
                self.assertFalse(rt.eligible)
                self.assertIn("another working directory", rt.reason)

    def test_a_woken_turn_in_another_cwd_disables_codex(self):
        self.world.events = [decision(5)]
        self.run_pass()
        t = turn()
        t["payload"]["cwd"] = str(self.home)
        self.world.events.append(ack(6, 5))
        self.exit_and_settle(t)
        self.assertIn("another working directory", w.disabled_reason(self.home, "codex") or "")

    def test_a_symlinked_path_to_the_same_cwd_is_the_same_cwd(self):
        link = self.home / "worklink"
        link.symlink_to(self.repo)
        t = turn()
        t["payload"]["cwd"] = str(link)
        self.session(SID, turns=[t])
        self.assertTrue(w.codex_eligibility(self.home, SID).eligible)


if __name__ == "__main__":
    unittest.main()
