"""Regression tests for the security review of PR #209 (head 9675269):
B1-B3, H1, M1-M3, G1-G3, L1-L4. Each test failed before its fix. The
reviewer's repro script (test_repro.py) cases are here as tests.

Same fakes as tests.test_session_wake: a temp HOME, a fixed clock, fake
process table / launcher / notifier / Cardinal reads, an injected
launchctl. Nothing starts an agent or touches launchd; the only real
process is /usr/bin/true under sandbox-exec (macOS) to check that the
shipped seatbelt profile parses with the waker's parameters.

Run from core/:  python3 -m unittest tests.test_session_wake_security -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import allowlist_proxy as proxy
from cardinal_core import investigation_events as ie
from cardinal_core import owner_input as oi
from cardinal_core import paths
from cardinal_core import session_wake as w

from tests.test_session_wake import (DISABLES, INV, INV2, MACHO, MEMBER, NOTIFY_OFF, PINS, PROMPT5, SID, Base, ack,
                                     binding,
                                     decision, meta, turn)


def mcp_call(server, tool):
    return {"timestamp": "2026-10-12T23:40:00Z", "type": "response_item",
            "payload": {"type": "function_call", "namespace": f"mcp__{server}", "name": tool, "arguments": "{}",
                        "call_id": "c1"}}


class Enable(Base):
    """B1: the daemon's interpreter and binaries never come from an
    agent-writable place; -I; a sanitized PATH; re-verified every run."""

    def cli(self, *argv, environ=None):
        out, calls = [], []
        code = w.cli_main(list(argv), runtime="codex", home=self.home, system=self.world.system(out=out.append),
                          launchctl=lambda a: (calls.append(a) or (0, "")), platform="darwin",
                          environ=environ or {"PATH": f"{self.codex.parent}:/usr/bin:/bin"})
        return code, "\n".join(out)

    def test_enable_refuses_an_interpreter_inside_home(self):
        venv = self.repo / ".venv" / "bin"
        venv.mkdir(parents=True)
        fake = venv / "python3"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        with patch.object(w.sys, "executable", str(fake)), patch.object(w, "system_python", lambda: None):
            code, out = self.cli("enable", "--launch")
        self.assertEqual(code, 2, out)
        self.assertIn("no Python the daemon may run", out)
        self.assertFalse(w.plist_path(self.home).exists())

    def test_plist_runs_python_isolated_with_a_sanitized_path(self):
        workspace_bin = self.repo / "bin"
        workspace_bin.mkdir()
        code, out = self.cli("enable", "--launch",
                             environ={"PATH": f"{workspace_bin}:{self.codex.parent}:/opt/evil/bin:/usr/bin:/bin"})
        self.assertEqual(code, 0, out)
        plist = plistlib.loads(w.plist_path(self.home).read_bytes())
        self.assertEqual(plist["ProgramArguments"][1:3], ["-I", "-S"])
        path = plist["EnvironmentVariables"]["PATH"].split(":")
        self.assertNotIn(str(workspace_bin), path)
        self.assertNotIn("/opt/evil/bin", path)
        self.assertEqual(path[:4], ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        self.assertIn(os.path.dirname(os.path.realpath(str(self.codex))), path)
        c = w.read_config(self.home)
        self.assertEqual(c["path_env"], plist["EnvironmentVariables"]["PATH"])
        self.assertIn(os.path.realpath(str(self.codex)), c["binaries"])

    def test_enable_refuses_codex_inside_a_workspace(self):
        bad = self.repo / "tools" / "codex"
        bad.parent.mkdir()
        bad.write_text("#!/bin/sh\n")
        bad.chmod(0o755)
        code, out = self.cli("enable", "--launch", environ={"PATH": f"{bad.parent}:/usr/bin:/bin"})
        self.assertEqual(code, 2, out)
        self.assertIn("inside a git work tree", out)

    def test_enable_refuses_a_node_codex_that_is_not_an_npm_install(self):
        """N2: a codex script is never run through node: an npm install is resolved to its
        native binary; anything else (here, with node inside a workspace) is refused."""
        self.codex.write_text("#!/usr/bin/env node\nconsole.log(1)\n")
        node = self.repo / "node_modules" / ".bin" / "node"
        node.parent.mkdir(parents=True)
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        code, out = self.cli("enable", "--launch", environ={"PATH": f"{node.parent}:{self.codex.parent}:/usr/bin"})
        self.assertEqual(code, 2, out)
        self.assertIn("not a native (Mach-O) executable", out)

    def test_enable_refuses_binaries_in_temp_dirs(self):
        with patch.object(w, "temp_dirs", lambda: (os.path.realpath(str(self.home.parent)),)):
            code, out = self.cli("enable", "--launch")
        self.assertEqual(code, 2, out)
        self.assertIn("temporary directory", out)

    def test_a_binary_changed_after_enable_blocks_launch(self):
        self.world.events = [decision(5)]
        self.codex.write_text("#!/bin/sh\necho swapped\n")
        self.run_pass()
        self.assertIn("changed since cardinal-wake enable", self.notified_only())

    def test_unvetted_config_blocks_launch(self):
        self.config(binaries={}, path_env=None)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("not vetted", self.notified_only())

    def test_a_binary_inside_the_session_cwd_blocks_launch(self):
        inside = self.repo / "codex"
        inside.write_text("#!/bin/sh\n")
        inside.chmod(0o755)
        real = os.path.realpath(str(inside))
        self.config(codex_path=real, binaries={**w.read_config(self.home)["binaries"], real: w.fingerprint(real)})
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches, [])
        self.assertIn("inside", self.ledger()["5"]["reason"])


class CodexBounds(Base):
    """B2 and M1 (and the reviewer's repro cases)."""

    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]

    def test_repro_home_cwd_is_not_eligible(self):
        (self.home / ".git").mkdir()
        self.session(SID, cwd=self.home)
        rt = w.resolve(self.home, SID)
        self.assertFalse(rt.eligible)
        self.run_pass()
        self.assertIn("strictly inside your home directory", self.notified_only())

    def test_cwd_outside_home_is_not_eligible(self):
        outside = self.home.parent / "outside"
        (outside / ".git").mkdir(parents=True)
        self.session(SID, cwd=outside)
        self.run_pass()
        self.assertIn("strictly inside your home directory", self.notified_only())

    def test_cwd_inside_or_above_a_protected_home_dir(self):
        for rel in (".codex/x", ".cardinal", "Library/Thing", ".ssh"):
            with self.subTest(rel):
                self.setUp()
                d = self.home / rel
                (d / ".git").mkdir(parents=True)
                self.session(SID, cwd=d)
                self.run_pass()
                self.assertIn("inside or above", self.notified_only())

    def test_repro_writable_roots_outside_cwd(self):
        t = turn()
        t["payload"]["sandbox_policy"]["writable_roots"] = [str(self.repo), str(self.home / ".cardinal"),
                                                            str(self.home)]
        self.session(SID, turns=[t])
        self.run_pass()
        self.assertIn("could write outside", self.notified_only())

    def test_writable_roots_inside_cwd_are_fine(self):
        t = turn()
        t["payload"]["sandbox_policy"]["writable_roots"] = [str(self.repo / "build")]
        self.session(SID, turns=[t])
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_file_system_policy_entries(self):
        def fs(*entries):
            t = turn()
            t["payload"]["file_system_sandbox_policy"] = {"kind": "restricted", "entries": list(entries)}
            return t
        root_read = {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"}
        tmp_write = {"path": {"type": "special", "value": {"kind": "slash_tmp"}}, "access": "write"}

        def unrestricted():
            t = turn()
            t["payload"]["file_system_sandbox_policy"] = {"kind": "unrestricted"}
            return t
        cases = (
            ("ok", lambda: fs(root_read, tmp_write,
                              {"path": {"type": "path", "path": str(self.repo)}, "access": "write"},
                              {"path": {"type": "path", "path": str(self.home / ".ssh")}, "access": "read"}), 1),
            ("bad path", lambda: fs(root_read, {"path": {"type": "path", "path": str(self.home / ".zshrc")},
                                                "access": "write"}), 0),
            ("bad special", lambda: fs({"path": {"type": "special", "value": {"kind": "root"}}, "access": "write"}), 0),
            ("unrestricted", unrestricted, 0),
        )
        for name, make, launches in cases:
            with self.subTest(name):
                self.setUp()
                t = make()
                self.session(SID, turns=[t])
                self.run_pass()
                self.assertEqual(len(self.world.launches), launches, self.ledger())

    def test_permission_profile_network(self):
        t = turn()
        t["payload"]["permission_profile"] = {"type": "managed", "network": "enabled"}
        self.session(SID, turns=[t])
        self.run_pass()
        self.assertIn("network", self.notified_only())

    def test_codex_config_that_widens_workspace_write(self):
        for text in ("[sandbox_workspace_write]\nnetwork_access = true\n",
                     "[sandbox_workspace_write]\nwritable_roots = [\"/Users\"]\n",
                     "sandbox_workspace_write.writable_roots = [\"/\"]\n",
                     "[permissions.default]\nnetwork = \"enabled\"\n",
                     "default_permissions = \"full\"\n"):
            with self.subTest(text):
                self.setUp()
                cfg = self.home / ".codex" / "config.toml"
                cfg.parent.mkdir(parents=True, exist_ok=True)
                cfg.write_text("model = \"x\"\n" + text)
                self.run_pass()
                self.assertIn("widens the workspace-write sandbox", self.notified_only())

    def test_a_benign_codex_config_stays_eligible(self):
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("model = \"gpt\"\n# sandbox_workspace_write.network_access = true (a comment)\n"
                       "[projects.\"/x\"]\ntrust_level = \"trusted\"\n")
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_repro_symlinked_cwd_is_judged_where_it_really_is(self):
        real = self.home / "elsewhere" / "proj"
        real.mkdir(parents=True)
        (real.parent / ".git").mkdir()
        (real.parent / ".codex").mkdir()
        (real.parent / ".codex" / "config.toml").write_text("[mcp_servers.x]\ncommand='/bin/sh'\n")
        link = self.repo / "sub"
        link.symlink_to(real)
        self.session(SID, cwd=link)
        self.assertTrue(w.codex_project_config(str(link)))
        self.run_pass()
        self.assertIn("its own Codex config", self.notified_only())

    def test_the_launch_runs_in_the_real_path(self):
        link = self.home / "worklink"
        link.symlink_to(self.repo)
        self.session(SID, cwd=link)
        self.run_pass()
        self.assertEqual(self.world.launches[0]["cwd"], os.path.realpath(str(self.repo)))

    def test_a_woken_turn_writing_outside_cwd_disables_codex(self):
        self.run_pass()
        t = turn()
        t["payload"]["sandbox_policy"]["writable_roots"] = [str(self.home)]
        self.append_turns(SID, t)
        self.world.events.append(ack(6, 5))
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.assertIn("extra writable roots", w.disabled_reason(self.home, "codex"))


class Replay(Base):
    """B3 and M2."""

    def test_repro_a_pruned_entry_never_lets_an_old_decision_run_again(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        led = w.read_ledger(self.home, SID)
        for e in led["entries"].values():
            e["updated_at"] = self.world.t - w.LEDGER_KEEP - 10
        w.write_ledger(self.home, SID, led, self.world.t)   # prunes the terminal entry
        self.assertEqual(self.ledger(), {})
        self.assertEqual(w.read_ledger(self.home, SID)["hwm"][INV], 5)
        self.world.t += w.LIVE_WINDOW + 10
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)        # #5 never runs twice
        self.assertEqual(self.ledger(), {})

    def test_the_high_water_mark_survives_the_entry_cap(self):
        led = w.read_ledger(self.home, SID)
        for i in range(1, w.LEDGER_MAX_ENTRIES + 20):
            e = {"seq": i, "investigation_id": INV, "runtime": "codex"}
            w.transition(e, w.NOTIFIED, self.t0 - 10, "seeded")
            w.add_entry(led, e)
        w.write_ledger(self.home, SID, led, self.t0)
        led = w.read_ledger(self.home, SID)
        self.assertLessEqual(len(led["entries"]), w.LEDGER_MAX_ENTRIES)
        self.assertTrue(w.decision_seen(led, INV, 1))

    def test_a_decision_older_than_seven_days_is_notified_once_never_launched(self):
        self.world.events = [decision(5, created=self.t0 - 8 * 86400)]
        self.run_pass()
        self.assertIn("older than 7 days", self.notified_only())
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)

    def test_an_undated_decision_is_never_launched(self):
        e = decision(5)
        e.pop("created_at")
        self.world.events = [e]
        self.run_pass()
        self.assertIn("older than 7 days (or undated)", self.notified_only())

    def test_repro_ledger_is_keyed_by_investigation(self):
        self.world.events = [decision(5)]
        self.world.table = [(1, f"codex resume {SID}")]   # live: #5 of INV is NOTIFIED
        self.run_pass()
        self.world.table = []
        ie.write_binding(self.home, SID, binding(inv=INV2))
        self.world.events = [decision(5, inv=INV2)]
        self.world.t += 400
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)         # #5 of INV2 is a different decision
        self.assertEqual(self.ledger(inv=INV2)["5"]["state"], w.RESUMING)
        self.assertEqual(self.ledger(inv=INV)["5"]["state"], w.NOTIFIED)


class McpRestriction(Base):
    """H1: a woken Codex run may use only the Cardinal server's read/post of
    investigation events; every other server and plugin is disabled."""

    CONFIG = (
        "model = \"gpt\"\n"
        "[mcp_servers.node_repl]\ncommand = \"/x/node_repl\"\n"
        "[mcp_servers.node_repl.env]\nA = \"1\"\n"
        "# BEGIN cardinal-codex-plugin managed MCP server\n"
        "[mcp_servers.cardinal]\nurl = \"https://app.cardinalhq.io/api/orgs/o/mcp\"\n"
        "[mcp_servers.computer-use]\ncommand = \"./x\"\nenabled = false\n"
        "[mcp_servers.cardinal-behavior]\ncommand = \"python3\"\n"
        "[mcp_servers.\"slack bot\"]\ncommand = \"/x/slack\"\n"
        "[plugins.\"browser@openai-bundled\"]\nenabled = true\n"
        "[plugins.\"cardinal-codex-plugin@personal\"]\nenabled = true\n"
    )

    def write_config(self, text):
        cfg = self.home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(text)

    def test_golden_argv_with_the_mcp_restriction(self):
        self.write_config(self.CONFIG)
        self.world.events = [decision(5)]
        self.run_pass()
        expected = [
            os.path.realpath(str(self.codex)), "exec", "-s", "workspace-write", "-c", 'approval_policy="never"',
            "-c", 'web_search="disabled"', *PINS,
            "-c", 'mcp_servers.cardinal.tools.storyboard__read_investigation_events.approval_mode="approve"',
            "-c", 'mcp_servers.cardinal.enabled_tools=["storyboard__read_investigation_events"]',
            "-c", "mcp_servers.cardinal-behavior.enabled=false",
            "-c", "mcp_servers.computer-use.enabled=false",
            "-c", "mcp_servers.node_repl.enabled=false",
            "-c", 'mcp_servers."slack bot".enabled=false',
            "-c", 'plugins."browser@openai-bundled".enabled=false',
            *NOTIFY_OFF, *DISABLES, "resume", SID, PROMPT5]
        self.assertEqual(self.world.launches[0]["argv"], expected)
        # The same lockdown was validated first, with no session: `codex <flags> features list`, then
        # `codex <flags> mcp list --json` (N4).
        flags = expected[4:expected.index("resume")]
        self.assertEqual(self.world.probes, [[expected[0]] + flags + ["features", "list"],
                                             [expected[0]] + flags + ["mcp", "list", "--json"]])

    def test_servers_declared_another_way_refuse(self):
        for text in ("mcp_servers.x.command = \"/bin/sh\"\n", "mcp_servers = { x = { command = \"sh\" } }\n",
                     "[[mcp_servers]]\nname = \"x\"\n", "[plugins]\n\"x@y\" = { enabled = true }\n"):
            with self.subTest(text):
                self.setUp()
                self.write_config(text)
                self.world.events = [decision(5)]
                self.run_pass()
                self.assertIn("cannot disable", self.notified_only())

    def test_a_server_id_that_cannot_be_named_refuses(self):
        self.write_config('[mcp_servers."we\\"ird"]\ncommand = "x"\n')
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches, [])

    def test_post_run_any_other_mcp_call_disables_codex(self):
        for server, tool, disabled in (("cardinal", "storyboard__read_investigation_events", False),
                                       ("cardinal", "storyboard__post_investigation_event", True),
                                       ("cardinal", "slack__send_message", True),
                                       ("node_repl", "js", True)):
            with self.subTest(server=server, tool=tool):
                self.setUp()
                self.world.events = [decision(5)]
                self.run_pass()
                self.append_turns(SID, turn(), mcp_call(server, tool))
                self.world.events.append(ack(6, 5))
                self.world.exit()
                self.world.t += 60
                self.run_pass()
                self.assertEqual(bool(w.disabled_reason(self.home, "codex")), disabled)
                if disabled:
                    self.assertIn("outside its allow-list", w.disabled_reason(self.home, "codex"))


class Lockdown(Base):
    """H1 (live, codex-cli 0.162.1): the feature lockdown is validated with
    `codex <flags> features list` before a launch; anything short of a full
    lockdown is a notification, never a partial launch."""

    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]

    def features(self, **over):
        rows = {f: ("stable", False) for f in w.CODEX_DISABLED_FEATURES}
        rows.update(over)
        return "\n".join(f"{k:<40} {v[0]:<18} {'true' if v[1] else 'false'}" for k, v in rows.items() if v) + "\n"

    def feature_probes(self):
        return [p for p in self.world.probes if p[-2:] == ["features", "list"]]

    def test_a_full_lockdown_launches_and_is_cached(self):
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(len(self.feature_probes()), 1)
        self.assertNotIn("resume", self.world.probes[0])
        self.assertEqual(len(self.world.probes), 2)    # + the mcp list, every launch
        self.world.events.append(decision(7))
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.launches), 2)
        self.assertEqual(len(self.feature_probes()), 2)   # review 3: never cached, run before every launch
        self.assertEqual(len(self.world.probes), 4)

    def test_a_rejected_flag_or_key_is_notify_only(self):
        self.world.probe_answer = (1, "")
        self.run_pass()
        self.assertIn("rejected the lockdown flags", self.notified_only())

    def test_an_unknown_feature_is_notify_only(self):
        self.world.probe_answer = (0, self.features(in_app_browser=None))
        self.run_pass()
        self.assertIn("does not know the feature 'in_app_browser'", self.notified_only())

    def test_a_removed_or_still_enabled_feature_is_notify_only(self):
        for over in ({"apps": ("removed", False)}, {"plugins": ("stable", True)}):
            with self.subTest(over):
                self.setUp()
                self.world.probe_answer = (0, self.features(**over))
                self.run_pass()
                self.assertIn("cannot disable the feature", self.notified_only())

    def test_a_probe_that_cannot_run_is_retried_then_notify_only(self):
        # Review 6: a transient probe failure must not consume the decision. It is left unclaimed
        # (reconsidered) for PROBE_RETRY_PASSES - 1 passes, then NOTIFIED.
        def boom(argv, path_env, cwd=None):
            raise OSError("no codex")
        for _ in range(w.PROBE_RETRY_PASSES - 1):
            self.run_pass(run_probe=boom)
            self.assertEqual(self.ledger(), {})                     # no entry: the seq is not consumed
            self.world.t += w.LIVE_WINDOW + 1
        self.run_pass(run_probe=boom)
        self.assertIn("could not be run to validate the lockdown", self.notified_only())

    def test_a_probe_that_recovers_launches(self):
        boom = [True]

        def probe(argv, path_env, cwd=None):
            if boom[0]:
                raise OSError("no codex")
            return self.world.probe(argv, path_env, cwd)
        self.run_pass(run_probe=probe)
        self.assertEqual((self.world.launches, self.ledger()), ([], {}))
        boom[0] = False
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass(run_probe=probe)
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.state(5), w.RESUMING)

    def test_a_new_codex_binary_is_probed_again(self):
        self.run_pass()
        self.codex.write_bytes(MACHO + b"v2")
        real = os.path.realpath(str(self.codex))
        self.config(binaries={**w.read_config(self.home)["binaries"], real: w.fingerprint(real)})
        self.world.events.append(decision(7))
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.feature_probes()), 2)

    def test_parse_features(self):
        out = w.parse_features("apps                                     stable             false\n"
                               "agent_message_board                      under development  true\n"
                               "garbage line\n")
        self.assertEqual(out, {"apps": ("stable", False), "agent_message_board": ("under development", True)})

    def test_post_run_any_other_tool_call_disables_codex(self):
        def call(kind, ns, name):
            payload = {"type": kind, "name": name, "call_id": "c"}
            if ns is not None:
                payload["namespace"] = ns
            return {"timestamp": "2026-10-12T23:40:00Z", "type": "response_item", "payload": payload}
        allowed = [call("custom_tool_call", None, "exec"), call("function_call", "functions", "wait"),
                   call("function_call", "collaboration", "spawn_agent"), call("function_call", "clock", "sleep"),
                   call("function_call", "mcp__cardinal", "storyboard__read_investigation_events")]
        bad = [call("web_search_call", None, "None"), call("image_generation_call", None, "None"),
               call("function_call", "mcp__codex_apps", "sites_deploy_site_version"),
               call("function_call", "web", "run"), call("function_call", None, "request_plugin_install"),
               call("local_shell_call", None, "None")]
        for extra, disabled in [(allowed, False)] + [([b], True) for b in bad]:
            with self.subTest(extra=[c["payload"]["type"] + ":" + c["payload"]["name"] for c in extra]):
                self.setUp()
                self.run_pass()
                self.append_turns(SID, turn(), *extra)
                self.world.events.append(ack(6, 5))
                self.world.exit()
                self.world.t += 60
                self.run_pass()
                self.assertEqual(bool(w.disabled_reason(self.home, "codex")), disabled)


class Candidates(Base):
    """M3."""

    def test_repro_a_stale_real_binding_is_not_shadowed_by_a_relocated_one(self):
        p = ie.binding_path(self.home, SID)
        os.utime(str(p), (self.t0 - 8 * 86400, self.t0 - 8 * 86400))
        boxed = paths.sandboxed_home(self.home)
        ie.write_binding(boxed, SID, binding(inv="inv_" + "c" * 24, investigation_url="https://evil.example/login"))
        self.assertEqual([s for s, *_ in w.candidate_sessions(self.home, self.t0)], [])

    def test_relocated_bindings_are_ignored_for_codex_and_claude_sessions(self):
        boxed = paths.sandboxed_home(self.home)
        ie.binding_path(self.home, SID).unlink()
        ie.write_binding(boxed, SID, binding())
        self.assertEqual(w.candidate_sessions(self.home, self.t0), [])

    def test_notifications_never_carry_a_binding_link(self):
        ie.write_binding(self.home, SID, binding(investigation_url="https://evil.example/login"))
        self.session(SID, turns=[turn("danger-full-access", network=None)], bind=False)
        self.world.events = [decision(5)]
        self.run_pass()
        msg = self.world.notes[0][1]
        self.assertNotIn("evil", msg)
        self.assertIn(f"{INV} on test.invalid", msg)


class Acknowledgment(Base):
    """L3."""

    def finish(self, *events):
        self.world.events = [decision(5)]
        self.run_pass()
        self.append_turns(SID, turn())
        self.world.events += list(events)
        self.world.exit()
        self.world.t += 60
        self.run_pass()

    def test_the_disposition_is_reported(self):
        a = ack(6, 5)
        a["payload"]["disposition"] = "declined"
        self.finish(a)
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.assertEqual(self.ledger()["5"]["disposition"], "declined")
        self.assertTrue(any("(declined)" in m for _, m in self.world.notes))

    def test_an_ack_carrying_granted_by_does_not_count(self):
        a = ack(6, 5)
        a["producer"]["granted_by"] = "user:someone"
        self.finish(a)
        self.assertEqual(self.state(5), w.FAILED)

    def test_a_grantee_ack_does_not_count(self):
        a = ack(6, 5)
        a["producer"]["principal"] = {"kind": "grantee", "id": "g"}
        self.finish(a)
        self.assertEqual(self.state(5), w.FAILED)


class Logs(Base):
    """L4."""

    def test_only_the_newest_logs_are_kept(self):
        d = w.logs_dir(self.home)
        d.mkdir(parents=True)
        for i in range(w.WAKE_LOGS_KEEP + 7):
            p = d / f"{SID}-{i}-1.log"
            p.write_text("x")
            os.utime(str(p), (self.t0 + i, self.t0 + i))
        (d / "launchd.log").write_text("keep")
        w.prune_logs(self.home)
        left = sorted(n for n in os.listdir(str(d)) if n != "launchd.log")
        self.assertEqual(len(left), w.WAKE_LOGS_KEEP)
        self.assertNotIn(f"{SID}-0-1.log", left)
        self.assertTrue((d / "launchd.log").exists())

    def test_a_huge_finished_log_keeps_its_tail(self):
        p = self.home / "big.log"
        with patch.object(w, "MAX_WAKE_LOG", 1000), patch.object(w, "WAKE_LOG_TAIL", 100):
            p.write_bytes(b"a" * 5000 + b"END")
            w.trim_log(str(p))
        data = p.read_bytes()
        self.assertTrue(data.endswith(b"END"))
        self.assertLess(len(data), 200)
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")


class GeminiMachinePrompt(unittest.TestCase):
    """G1."""

    def test_the_wake_prompt_is_a_machine_prompt_on_gemini(self):
        self.assertIsNone(oi.normalize_prompt("gemini", oi.wake_prompt(9, INV)))
        self.assertTrue(oi.is_machine_prompt("gemini", oi.wake_prompt(9, INV)))


class ExclusivePort(unittest.TestCase):
    """G3: the proxy never shares its port."""

    def test_another_holder_of_the_loopback_port_makes_the_proxy_fail_closed(self):
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        with self.assertRaises(OSError):
            proxy.make_server(["a.example.com"], port=port, log=lambda *a: None)
        ran = []
        with contextlib.redirect_stderr(io.StringIO()):
            rc = proxy.main(["--port", str(port), "a.example.com", "--", "/usr/bin/true"],
                            popen=lambda cmd: ran.append(cmd))
        self.assertEqual(rc, 3)
        self.assertEqual(ran, [])     # the command never starts without the port

    def test_the_bind_does_not_reuse_addresses(self):
        server = proxy.make_server(["a.example.com"], port=0, log=lambda *a: None)
        self.addCleanup(server.server_close)
        self.assertFalse(server.allow_reuse_address)
        reuse = server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
        self.assertEqual(reuse, 0)
        proxy.verify_exclusive(server.server_address[1])   # ours: nobody else can bind it

    def test_command_mode_runs_the_command_and_returns_its_status(self):
        calls = []

        class Child:
            def wait(self):
                return 7

            def send_signal(self, s):
                pass
        rc = proxy.main(["--port", "0", "a.example.com", "--", "/bin/echo", "x"],
                        popen=lambda cmd: (calls.append(cmd) or Child()))
        self.assertEqual(rc, 7)
        self.assertEqual(calls, [["/bin/echo", "x"]])


@unittest.skipUnless(sys.platform == "darwin" and os.access("/usr/bin/sandbox-exec", os.X_OK), "macOS seatbelt")
class ShippedSeatbelt(unittest.TestCase):
    """G2: the waker's own copy of the restrictive-proxied profile parses
    with the parameters the Gemini argv passes, and a seatbelt cannot be
    nested (why Gemini's own -s is not passed inside it). Runs /usr/bin/true
    and a nested sandbox-exec only; never an agent."""

    def run_sandboxed(self, *cmd):
        import tempfile
        profile = Path(w.__file__).parent / w.GEMINI_SEATBELT_FILE
        with tempfile.TemporaryDirectory() as t:
            t = os.path.realpath(t)
            params = [f"TARGET_DIR={t}", f"TMP_DIR={t}", f"HOME_DIR={os.path.realpath(str(Path.home()))}",
                      f"CACHE_DIR={t}"] + [f"INCLUDE_DIR_{i}=/dev/null" for i in range(5)]
            argv = ["/usr/bin/sandbox-exec"] + [a for p in params for a in ("-D", p)] + ["-f", str(profile), *cmd]
            return subprocess.run(argv, capture_output=True, text=True, timeout=30)

    def test_the_profile_parses_with_the_wake_parameters(self):
        r = self.run_sandboxed("/usr/bin/true")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_seatbelt_cannot_be_nested(self):
        r = self.run_sandboxed("/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "/usr/bin/true")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
