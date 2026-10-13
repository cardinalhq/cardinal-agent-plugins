"""Security properties of cardinal-wake kept from the reviews of PR #209 into
v4 ("deliver, never launch"): B1 (the daemon's interpreter and PATH are
never agent-writable), N5 (fingerprints), B3 / M2 (replay), M3 (candidates
and notifications), the relocated hook state of seatbelt-sandboxed
sessions, HTTPS_PROXY in the plugin's openers, and log pruning.

Same fakes as tests.test_session_wake: a temp HOME, a fixed clock, fake
process table / notifier / Cardinal reads, an injected launchctl. Nothing
starts an agent or touches launchd.

Run from core/:  python3 -m unittest tests.test_session_wake_security -v
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cardinal_core import evidence
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_state_sync as sync
from cardinal_core import paths
from cardinal_core import session_wake as w
from cardinal_core.evidence_promote import no_redirect_opener

from tests.test_session_wake import INV, INV2, MEMBER, SID, Base, binding, decision

# A native-executable header (N2: the interpreter must be one, never a script).
MACHO = b"\xcf\xfa\xed\xfe" + b"\0" * 60
GSID = "8a6e4964-0d1f-4c55-9f0e-2b7c1d3a4e5f"
BOXED = {"SANDBOX": "sandbox-exec"}
HOSTILE = {"NODE_OPTIONS": "--require /tmp/x.js", "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib",
           "PYTHONPATH": "/tmp/py", "OPENAI_BASE_URL": "https://evil.example"}


class Cli(Base):
    def cli(self, *argv, environ=None):
        out, calls = [], []
        code = w.cli_main(list(argv), runtime="codex", home=self.home, system=self.world.system(out=out.append),
                          launchctl=lambda a: (calls.append(a) or (0, "")), platform="darwin",
                          environ=environ or {"PATH": "/usr/bin:/bin"})
        return code, "\n".join(out)


# ---------------------------------------------------------------------------
# B1: the daemon's interpreter and PATH
# ---------------------------------------------------------------------------

class Enable(Cli):
    def test_enable_refuses_an_interpreter_inside_home(self):
        venv = self.repo / ".venv" / "bin"
        venv.mkdir(parents=True)
        fake = venv / "python3"
        fake.write_bytes(MACHO)
        fake.chmod(0o755)
        with patch.object(w.sys, "executable", str(fake)), patch.object(w, "system_python", lambda: None):
            code, out = self.cli("enable")
        self.assertEqual(code, 2, out)
        self.assertIn("no Python the daemon may run", out)
        self.assertFalse(w.plist_path(self.home).exists())

    def test_enable_refuses_an_interpreter_in_a_temp_dir(self):
        here = os.path.dirname(os.path.realpath(w.sys.executable))
        with patch.object(w, "temp_dirs", lambda: (here,)), patch.object(w, "system_python", lambda: None):
            code, out = self.cli("enable")
        self.assertEqual(code, 2, out)
        self.assertIn("temporary directory", out)

    def test_enable_refuses_a_version_manager_shim(self):
        shim = self.home / ".pyenv" / "shims" / "python3"
        shim.parent.mkdir(parents=True)
        shim.write_bytes(MACHO)
        shim.chmod(0o755)
        with patch.object(w.sys, "executable", str(shim)), patch.object(w, "system_python", lambda: None):
            python, why = w.pick_python(self.home)
        self.assertIsNone(python)
        self.assertTrue("home directory" in why or "shim" in why, why)

    def test_the_plist_runs_python_isolated_with_only_a_sanitized_path(self):
        workspace_bin = self.repo / "bin"
        workspace_bin.mkdir()
        code, out = self.cli("enable", environ={**HOSTILE, "PATH": f"{workspace_bin}:/opt/evil/bin:/usr/bin:/bin"})
        self.assertEqual(code, 0, out)
        plist = plistlib.loads(w.plist_path(self.home).read_bytes())
        self.assertEqual(plist["ProgramArguments"][1:3], ["-I", "-S"])
        self.assertEqual(set(plist["EnvironmentVariables"]), {"PATH"})
        path = plist["EnvironmentVariables"]["PATH"].split(":")
        self.assertNotIn(str(workspace_bin), path)
        self.assertNotIn("/opt/evil/bin", path)
        self.assertEqual(path[:4], ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        self.assertEqual(path[4:], [os.path.dirname(w.read_config(self.home)["python"])])
        c = w.read_config(self.home)
        self.assertEqual(c["path_env"], plist["EnvironmentVariables"]["PATH"])
        self.assertEqual(list(c["binaries"]), [c["python"]])

    def test_an_interpreter_changed_after_enable(self):
        fake = self.home.parent / "sysroot" / "python3"
        fake.parent.mkdir()
        fake.write_bytes(MACHO)
        real = os.path.realpath(str(fake))
        self.config(python=real, binaries={real: w.fingerprint(real)})
        self.assertIsNone(w.binaries_problem(w.read_config(self.home), self.home))
        fake.write_bytes(MACHO + b"swapped")
        self.assertIn("changed since cardinal-wake enable", w.binaries_problem(w.read_config(self.home), self.home))

    def test_an_unvetted_config(self):
        self.config(binaries={}, path_env=None)
        self.assertIn("not vetted", w.binaries_problem(w.read_config(self.home), self.home))
        out = []
        w.status(self.home, self.world.system(), out.append)
        self.assertIn("interpreter: the daemon's interpreter was not vetted", "\n".join(out))


class DaemonPython(Cli):
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

    def test_a_script_is_never_the_interpreter(self):
        script = self.home.parent / "opt" / "python3"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\nexec /usr/bin/python3 \"$@\"\n")
        script.chmod(0o755)
        with patch.object(w.sys, "executable", str(script)), patch.object(w, "system_python", lambda: None):
            python, why = w.pick_python(self.home)
        self.assertIsNone(python)
        self.assertIn("not a native (Mach-O) executable", why)

    def test_the_xcrun_shim_is_resolved_to_its_interpreter(self):
        def run(argv, **kw):
            self.assertEqual(argv, ["/usr/bin/xcrun", "-f", "python3"])
            self.assertNotIn("DEVELOPER_DIR", kw["env"])
            return subprocess.CompletedProcess(argv, 0, "/Library/Developer/CommandLineTools/usr/bin/python3\n", "")
        with patch.object(w.subprocess, "run", run):
            self.assertEqual(w.system_python(), "/Library/Developer/CommandLineTools/usr/bin/python3")

    def test_a_vetted_interpreter_in_the_system_path_dirs_is_a_problem(self):
        shim_dir = self.home.parent / "sysbin"
        shim_dir.mkdir()
        shim = shim_dir / "python3"
        shim.write_bytes(MACHO)
        real = os.path.realpath(str(shim))
        self.config(python=real, binaries={real: w.fingerprint(real)})
        self.assertIsNone(w.binaries_problem(w.read_config(self.home), self.home))
        with patch.object(w, "SYSTEM_PATH_DIRS", (os.path.dirname(real),) + w.SYSTEM_PATH_DIRS):
            self.assertIn("system shim", w.binaries_problem(w.read_config(self.home), self.home) or "")


class Fingerprint(Base):
    def setUp(self):
        super().setUp()
        self.f = self.home.parent / "bin-python"
        self.f.write_bytes(MACHO)

    def test_an_in_place_rewrite_with_the_mtime_restored_changes_the_fingerprint(self):
        fp = w.fingerprint(str(self.f))
        st = os.stat(self.f)
        data = self.f.read_bytes()
        with open(self.f, "r+b") as f:
            f.write(b"X" * len(data))
        os.utime(self.f, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertNotEqual(w.fingerprint(str(self.f)), fp)

    def test_the_fingerprint_carries_ctime_and_sha256(self):
        fp = w.fingerprint(str(self.f))
        st = os.stat(self.f)
        self.assertEqual(fp, [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns,
                              hashlib.sha256(self.f.read_bytes()).hexdigest()])


# ---------------------------------------------------------------------------
# B3 / M2: replay
# ---------------------------------------------------------------------------

class Replay(Base):
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

    def test_a_decision_older_than_seven_days_is_notified_once(self):
        self.world.events = [decision(5, created=self.t0 - 8 * 86400)]
        self.run_pass()
        self.assertIn("older than 7 days", self.notified())
        self.world.t += 600
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)

    def test_an_undated_decision(self):
        e = decision(5)
        e.pop("created_at")
        self.world.events = [e]
        self.run_pass()
        self.assertIn("older than 7 days (or undated)", self.notified())

    def test_the_ledger_is_keyed_by_investigation(self):
        self.world.events = [decision(5)]
        self.run_pass()
        ie.write_binding(self.home, SID, binding(inv=INV2))
        self.world.events = [decision(5, inv=INV2)]
        self.world.t += 400
        self.run_pass()
        self.assertEqual(self.ledger(inv=INV2)["5"]["state"], w.NOTIFIED)
        self.assertEqual(self.ledger(inv=INV)["5"]["state"], w.NOTIFIED)
        self.assertEqual(len(self.world.notes), 2)


# ---------------------------------------------------------------------------
# M3: candidates and what a notification may carry
# ---------------------------------------------------------------------------

class Candidates(Base):
    def test_a_stale_real_binding_is_not_shadowed_by_a_relocated_one(self):
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
        self.world.events = [decision(5)]
        self.run_pass()
        msg = self.world.notes[0][1]
        self.assertNotIn("evil", msg)
        self.assertIn(f"{INV} on test.invalid", msg)

    def test_resume_never_takes_its_directory_from_a_binding(self):
        elsewhere = self.home / "attacker"
        elsewhere.mkdir()
        ie.write_binding(self.home, SID, binding(cwd=str(elsewhere), workspace_roots=[str(elsewhere)]))
        bin_dir = self.home.parent / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/sh\n")
        (bin_dir / "codex").chmod(0o755)
        plan = w.resume_plan(self.home, SID, {"PATH": str(bin_dir)})
        self.assertEqual(plan.cwd, str(self.repo))
        self.rollout_path(SID).unlink()
        with self.assertRaises(w.ResumeError):   # no runtime record, no resume: never the binding's word
            w.resume_plan(self.home, SID, {"PATH": str(bin_dir)})


# ---------------------------------------------------------------------------
# Hook state of seatbelt-sandboxed sessions (relocated, agent-writable)
# ---------------------------------------------------------------------------

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
            self.assertEqual(w.resume_dir(home), home / ".cardinal" / "wake" / "resume")
        with patch.dict(os.environ, {"SANDBOX": ""}):
            self.assertEqual(ie.sessions_dir(home), home / ".cardinal" / "investigations" / "sessions")
            self.assertEqual(evidence.default_root(home), home / ".cardinal" / "evidence")

    def test_checkpoint_holds_land_in_the_sandboxed_spool(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            boxed_root = paths.sandboxed_home(home) / ".cardinal" / "evidence"
            (boxed_root / GSID).mkdir(parents=True)
            with patch.dict(os.environ, BOXED):
                evidence.hold_after_checkpoint(home, GSID, env={})
            self.assertEqual(evidence.read_hold(boxed_root, GSID)["reason"], "checkpoint")
            self.assertFalse((home / ".cardinal" / "evidence").exists())

    def test_the_users_evidence_opt_out_reaches_the_sandboxed_spool(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            with patch.dict(os.environ, BOXED):
                root = evidence.default_root(home)
                self.assertFalse(evidence.capture_disabled(root, {}))
                (home / ".cardinal" / "evidence").mkdir(parents=True)
                (home / ".cardinal" / "evidence" / evidence.DISABLED_FLAG).write_text("")
                self.assertTrue(evidence.capture_disabled(root, {}))


class WakerReadsRelocatedBindings(Base):
    def setUp(self):
        super().setUp()
        self.boxed_home = paths.sandboxed_home(self.home)
        ie.write_binding(self.boxed_home, GSID, binding())
        chat = self.home / ".gemini" / "tmp" / "h" / "chats" / f"session-2026-10-12T10-00-{GSID[:8]}.jsonl"
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({"sessionId": GSID}) + "\n")
        self.age(chat)

    def test_candidate_from_the_sandboxed_home(self):
        found = {sid: state for sid, _, _, state in w.candidate_sessions(self.home, self.world.t)}
        self.assertEqual(found[GSID], self.boxed_home)
        self.assertEqual(found[SID], self.home)

    def test_a_relocated_binding_is_only_a_hint(self):
        # it names a decision that is broadcast and unverified: nothing happens
        self.world.events = [decision(5, to=None), {**decision(6, to=GSID), "authority": "producer_claim"}]
        self.run_pass()
        self.assertEqual(self.world.notes, [])
        self.assertEqual(self.ledger(GSID), {})

    def test_a_verified_decision_is_notified_and_the_waker_never_writes_under_the_sandboxed_home(self):
        before = sorted(p for p in self.boxed_home.rglob("*") if p.is_file())
        self.world.events = [decision(5, to=GSID)]
        self.run_pass()
        after = sorted(p for p in self.boxed_home.rglob("*") if p.is_file())
        self.assertEqual(before, after)
        self.assertEqual(self.notified(sid=GSID), "the session is stopped: notified with its resume command")
        self.assertTrue(self.ledger(GSID)["5"]["sandboxed_state"])
        self.assertTrue(self.world.notes[0][1].startswith(f"Owner decision for Gemini CLI session {GSID[:8]}"))


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


# ---------------------------------------------------------------------------
# Files the waker keeps
# ---------------------------------------------------------------------------

class Prune(Base):
    def test_only_the_newest_logs_are_kept(self):
        d = w.logs_dir(self.home)
        d.mkdir(parents=True)
        for i in range(w.WAKE_LOGS_KEEP + 7):
            p = d / f"{SID}-{i}-1.log"
            p.write_text("x")
            os.utime(str(p), (self.t0 + i, self.t0 + i))
        (d / "launchd.log").write_text("keep")
        w.prune_files(self.home, self.t0)
        left = sorted(n for n in os.listdir(str(d)) if n != "launchd.log")
        self.assertEqual(len(left), w.WAKE_LOGS_KEEP)
        self.assertNotIn(f"{SID}-0-1.log", left)
        self.assertTrue((d / "launchd.log").exists())

    def test_old_resume_files_are_deleted(self):
        w.install_lib(self.home)
        p = Path(w.write_resume_file(self.home, SID, w.read_config(self.home)))
        w.prune_files(self.home, self.t0)
        self.assertTrue(p.exists())
        w.prune_files(self.home, self.t0 + w.RESUME_FILE_KEEP + 10)
        self.assertFalse(p.exists())

    def test_a_resume_file_is_written_only_for_a_uuid(self):
        w.install_lib(self.home)
        self.assertIsNone(w.write_resume_file(self.home, "not-a-uuid", w.read_config(self.home)))
        self.assertIsNone(w.write_resume_file(self.home, "0123456789abcdef", w.read_config(self.home)))


if __name__ == "__main__":
    unittest.main()
