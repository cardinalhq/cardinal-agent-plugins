"""Owner-woken sessions, v4 "deliver, never launch" (cardinal_core.session_wake;
docs/specs/resume-and-wake.md §5, §7).

Every effect is faked: a temp HOME, a fixed clock, a fake process table,
process start times, notifier, Cardinal reads, `resume`'s exec, and an
injected launchctl. Nothing here starts an agent or installs a launchd
agent; the only real processes are the installed runner run with --help
and `resume --print`.

Run from core/:  python3 -m unittest tests.test_session_wake -v
"""

from __future__ import annotations

import json
import multiprocessing
import os
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import investigation_events as ie
from cardinal_core import investigation_poller as poller
from cardinal_core import owner_input as oi
from cardinal_core import session_wake as w

SID = "01a13279-3459-7433-8ae2-7f81520c32e9"
SID2 = "01a1318d-1331-7520-b01f-ef7a0c1a9574"
INV = "inv_" + "a" * 24
INV2 = "inv_" + "b" * 24
MEMBER = {"origin": "https://test.invalid", "org": "org", "key": "test-key"}
SECRET = "SECRET-DECISION-TEXT deploy prod now"
CAPS = {"owner_decision": {"enabled": True}}
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "en_US.UTF-8"}


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


NOWAKE = object()


def decision(seq, to=SID, inv=INV, created=None, wake=True, **producer):
    """An owner.decided event; wake: payload.wake (conductor#2110), the
    Composer's "Resume this session if it has stopped" (NOWAKE: absent)."""
    p = {"principal": {"kind": "user", "id": "owner"}, "session_id": None, "is_investigation_author": True}
    p.update(producer)
    payload = {"text": SECRET}
    if wake is not NOWAKE:
        payload["wake"] = wake
    return {"investigation_id": inv, "seq": seq, "class": "owner", "type": "owner.decided",
            "authority": "owner_decision", "created_at": iso(time.time() if created is None else created),
            "to_session_id": to, "payload": payload, "producer": p}


def ack(seq, ack_of, sid=SID, inv=INV, author=True):
    return {"investigation_id": inv, "seq": seq, "class": "control", "type": "acknowledged",
            "authority": "producer_claim", "created_at": "2026-10-12T12:01:00Z",
            "payload": {"ack_of": ack_of, "disposition": "accepted", "note": "on it"},
            "producer": {"principal": {"kind": "api_key", "id": "k"}, "session_id": sid,
                         "is_investigation_author": author}}


def binding(inv=INV, **extra):
    b = {"investigation_id": inv, "cursor": 0, "org": "org", "is_author": True, "capabilities": CAPS,
         "bound_at": "2026-10-12T00:00:00Z", "source": "auto"}
    b.update(extra)
    return b


def meta(sid, cwd, originator="codex_exec", source="exec"):
    return {"timestamp": "2026-10-12T23:36:08.796Z", "type": "session_meta",
            "payload": {"id": sid, "cwd": cwd, "originator": originator, "source": source,
                        "cli_version": "0.162.1", "base_instructions": {"text": "x" * 2000}}}


def turn(cwd):
    return {"timestamp": "2026-10-12T23:36:11.185Z", "type": "turn_context",
            "payload": {"cwd": cwd, "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"}}}


class World:
    """The faked outside world one waker pass sees."""

    def __init__(self, t0):
        self.t = t0
        self.events = []
        self.alive = set()
        self.starts = {}
        self.table = []
        self.notes = []           # (title, message, open_file)
        self.lines = []
        self.fetch_complete = True
        self.wake_posts = []      # session.wake reports (System.post_wake)
        self.wake_error = None    # an exception, or state -> exception
        self.execs = []           # (argv, cwd) `resume` ran
        self.exec_error = None

    def fetch(self, conn, inv, sid, after, client):
        assert conn == MEMBER
        return [e for e in self.events if e["investigation_id"] == inv and e["seq"] > after
                and e.get("to_session_id") in (None, sid)], self.fetch_complete

    def notify(self, title, msg, open_file=None):
        self.notes.append((title, msg, open_file))

    def exec(self, argv, cwd):
        if self.exec_error is not None:
            raise self.exec_error
        self.execs.append((list(argv), cwd))

    def system(self, **kw):
        args = dict(now=lambda: self.t, alive=lambda pid: pid in self.alive,
                    process_start=lambda pid: self.starts.get(pid), process_table=lambda: list(self.table),
                    notifier=self.notify, connection=lambda runtime, home: dict(MEMBER), fetch=self.fetch,
                    environ=ENV, out=self.lines.append, post_wake=self.post_wake, exec=self.exec)
        args.update(kw)
        return w.System(**args)

    def post_wake(self, conn, inv, sid, payload, key, client):
        assert conn == MEMBER
        err = self.wake_error.get(payload["state"]) if isinstance(self.wake_error, dict) else self.wake_error
        if err is not None:
            raise err
        self.wake_posts.append({"inv": inv, "sid": sid, "payload": dict(payload), "key": key, "client": client})
        return {"event": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        isolated = {k: v for k, v in os.environ.items() if not k.startswith("CARDINAL_")}
        isolated["HOME"] = str(self.home)
        env_patch = patch.dict(os.environ, isolated, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        temp = patch.object(w, "temp_dirs", lambda: ())   # the test HOME itself lives in a temp dir
        temp.start()
        self.addCleanup(temp.stop)
        self.t0 = time.time()
        self.world = World(self.t0)
        self.config()
        self.repo = self.home / "work" / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.session(SID)

    # -- fixtures --------------------------------------------------------
    def config(self, enabled=True, **extra):
        python = os.path.realpath(sys.executable)
        c = {"enabled": enabled, "python": python, "path_env": w.daemon_path(os.path.dirname(python)),
             "binaries": {python: w.fingerprint(python)}, "enabled_at": "2026-10-12T00:00:00Z"}
        c.update(extra)
        w.write_config(self.home, c)

    def rollout_path(self, sid):
        return self.home / ".codex" / "sessions" / "2026" / "10" / "12" / f"rollout-2026-10-12T16-36-07-{sid}.jsonl"

    def session(self, sid, originator="codex_exec", source="exec", cwd=None, bind=True, **bextra):
        """A bound Codex session: its rollout (session_meta + one turn) records `cwd`."""
        if bind:
            ie.write_binding(self.home, sid, binding(**bextra))
        p = self.rollout_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        cwd = str(cwd or self.repo)
        p.write_text("".join(json.dumps(r) + "\n" for r in (meta(sid, cwd, originator, source), turn(cwd))))
        self.age(p)
        return p

    def age(self, path, seconds=1000):
        os.utime(str(path), (self.t0 - seconds, self.t0 - seconds))

    def run_pass(self, **kw):
        return w.run_once(self.home, self.world.system(**kw))

    def ledger(self, sid=SID, inv=INV):
        """The session's entries for `inv`, keyed by seq (as text)."""
        return {k.split(":", 1)[1]: v for k, v in w.read_ledger(self.home, sid)["entries"].items()
                if k.startswith(inv + ":")}

    def state(self, seq, sid=SID):
        return self.ledger(sid).get(str(seq), {}).get("state")

    def notified(self, seq=5, sid=SID):
        """#seq was notified (once): its ledger reason."""
        self.assertEqual(self.state(seq, sid), w.NOTIFIED)
        return self.ledger(sid)[str(seq)]["reason"]

    def nothing(self, sid=SID):
        self.assertEqual(self.ledger(sid), {})
        self.assertEqual(self.world.notes, [])

    def install_runner(self):
        w.install_lib(self.home)
        return w.runner_path(self.home)


# ---------------------------------------------------------------------------
# §5.2 the trigger
# ---------------------------------------------------------------------------

class Predicate(Base):
    def test_a_verified_decision_addressed_to_a_stopped_session_is_notified_once(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)
        self.notified()
        self.world.t += 600
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)

    def test_every_server_stamped_field_is_required(self):
        bad = {
            "authority": dict(authority="producer_claim"),
            "class": dict(**{"class": "control"}),
            "type": dict(type="cue.added"),
        }
        producers = {
            "api_key principal": dict(principal={"kind": "api_key", "id": "k"}),
            "grantee principal": dict(principal={"kind": "grantee", "id": "g"}),
            "not the author": dict(is_investigation_author=False),
            "a session claim": dict(session_id=SID),
            "granted_by": dict(granted_by="user:x"),
        }
        cases = [(name, {**decision(5), **fields}) for name, fields in bad.items()]
        cases += [(name, decision(5, **fields)) for name, fields in producers.items()]
        for name, e in cases:
            with self.subTest(name):
                self.setUp()
                self.world.events = [e]
                self.run_pass()
                self.nothing()

    def test_a_broadcast_decision_never_notifies(self):
        self.world.events = [decision(5, to=None)]
        self.run_pass()
        self.nothing()

    def test_a_decision_addressed_to_another_session_never_notifies(self):
        self.world.events = [decision(5, to=SID2)]
        self.run_pass()
        self.nothing()

    def test_an_acknowledged_decision_never_notifies(self):
        self.world.events = [decision(5), ack(6, 5)]
        self.run_pass()
        self.nothing()

    def test_an_acknowledgment_by_another_session_does_not_count(self):
        self.world.events = [decision(5), ack(6, 5, sid=SID2)]
        self.run_pass()
        self.notified()

    def test_owner_decision_capability_off(self):
        ie.write_binding(self.home, SID, binding(capabilities={}))
        self.world.events = [decision(5)]
        self.run_pass()
        self.nothing()

    def test_a_non_author_session(self):
        ie.write_binding(self.home, SID, binding(is_author=False))
        self.world.events = [decision(5)]
        self.run_pass()
        self.nothing()

    def test_a_binding_untouched_for_more_than_the_window(self):
        self.world.events = [decision(5)]
        self.world.t = self.t0 + w.CANDIDATE_WINDOW + 10
        self.run_pass()
        self.nothing()

    def test_machine_opt_in_off(self):
        self.config(enabled=False)
        self.world.events = [decision(5)]
        self.run_pass()
        self.nothing()

    def test_an_incomplete_read_never_notifies(self):
        self.world.events = [decision(5)]
        self.world.fetch_complete = False
        self.run_pass()
        self.nothing()

    def test_a_failed_read_never_notifies(self):
        def boom(*a):
            raise OSError("network down")
        self.world.events = [decision(5)]
        self.run_pass(fetch=boom)
        self.nothing()

    def test_a_connection_for_another_org_is_not_used(self):
        self.world.events = [decision(5)]
        self.run_pass(connection=lambda runtime, home: {**MEMBER, "org": "other"})
        self.nothing()

    def test_events_are_read_from_cardinal_never_from_the_inbox(self):
        ie.write_json(ie.inbox_path(self.home, SID), {"investigation_id": INV, "after": 0, "through": 5,
                                                      "head_seq": 5, "events": [decision(5)]})
        self.run_pass()   # Cardinal answers no events
        self.nothing()

    def test_a_dry_run_writes_and_notifies_nothing(self):
        self.world.events = [decision(5)]
        report = w.run_once(self.home, self.world.system(), dry_run=True)
        self.assertEqual([r["action"] for r in report], ["would notify"])
        self.nothing()


class Caps(Base):
    def seed(self, sid, n, inv=INV2, key="notified_at"):
        led = w.read_ledger(self.home, sid)
        for i in range(n):
            e = {"seq": 100 + i, "investigation_id": inv, "runtime": "codex", key: self.t0 - 60}
            w.transition(e, w.NOTIFIED, self.t0 - 60, "seeded")
            w.add_entry(led, e)
        w.write_ledger(self.home, sid, led, self.t0)

    def test_three_notifications_per_session_per_day(self):
        self.seed(SID, w.SESSION_DAILY_CAP)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIsNone(self.state(5))            # not recorded: reconsidered once the day has passed
        self.assertEqual(len(self.world.notes), 1)
        self.assertIn("daily notification cap", self.world.notes[0][1])
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)   # the cap is notified once a day
        self.world.t = self.t0 + w.DAY
        self.run_pass()
        self.notified()
        self.assertEqual(len(self.world.notes), 2)

    def test_ten_notifications_across_sessions_per_day(self):
        for i in range(5):
            self.seed(f"0000000{i}-0000-0000-0000-000000000000", 2)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIsNone(self.state(5))

    def test_an_earlier_versions_claims_count_too(self):
        self.seed(SID, w.SESSION_DAILY_CAP, key="claimed_at")
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIsNone(self.state(5))

    def test_the_cap_stops_the_pass_at_the_first_decision_it_cannot_record(self):
        self.seed(SID, w.SESSION_DAILY_CAP - 1)
        self.world.events = [decision(5), decision(6), decision(7)]
        self.run_pass()
        self.notified(5)
        self.assertIsNone(self.state(6))
        self.assertIsNone(self.state(7))
        self.assertEqual(w.read_ledger(self.home, SID)["hwm"][INV], 5)   # never past the unrecorded #6


# ---------------------------------------------------------------------------
# The notification
# ---------------------------------------------------------------------------

class Notification(Base):
    def test_a_stopped_session_gets_its_resume_command_never_the_decision_text(self):
        self.world.events = [decision(5)]
        self.run_pass()
        title, msg, open_file = self.world.notes[0]
        self.assertEqual(title, "Cardinal wake")
        self.assertEqual(msg, f"Owner decision for Codex session {SID[:8]} ({INV} on test.invalid). "
                              f"Resume: ~/.cardinal/wake/lib/cardinal-wake resume {SID[:8]}")
        self.assertIsNone(open_file)                  # no runner installed: nothing to click
        self.assertNotIn("SECRET", msg)
        self.assertIn("stopped", self.notified())

    def test_with_the_runner_installed_a_click_resumes_through_a_private_command_file(self):
        runner = self.install_runner()
        self.world.events = [decision(5)]
        self.run_pass()
        _, msg, open_file = self.world.notes[0]
        self.assertIn("click to resume in Terminal", msg)
        p = Path(open_file)
        self.assertEqual(p, self.home / ".cardinal" / "wake" / "resume" / f"{SID}.command")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(p.parent.stat().st_mode), 0o700)
        python = w.read_config(self.home)["python"]
        self.assertEqual(p.read_text().splitlines()[-1],
                         f"exec {shlex.quote(python)} -I -S {shlex.quote(str(runner))} resume {SID}")
        self.assertNotIn("plugins", p.read_text())

    def test_a_live_session_is_told_to_switch_to_its_window(self):
        self.install_runner()
        self.world.table = [(1234, f"codex resume {SID}")]
        self.world.events = [decision(5)]
        self.run_pass()
        _, msg, open_file = self.world.notes[0]
        self.assertEqual(msg, f"Owner decision for Codex session {SID[:8]} ({INV} on test.invalid). "
                              "It is still open: switch to that window.")
        self.assertIsNone(open_file)
        self.assertIn("the session is live", self.notified())
        self.assertFalse((self.home / ".cardinal" / "wake" / "resume").exists())

    def test_old_and_unrequested_decisions_are_notified_the_same_way(self):
        self.world.events = [decision(5, created=self.t0 - 8 * 86400), decision(6, wake=NOWAKE)]
        self.run_pass()
        self.assertEqual(self.notified(5), w.DECISION_OLD)
        self.assertEqual(self.notified(6), w.WAKE_NOT_REQUESTED)
        self.assertEqual(len(self.world.notes), 2)
        for _, msg, _ in self.world.notes:
            self.assertIn("Resume:", msg)

    def test_terminal_notifier_is_used_when_installed(self):
        found = w.terminal_notifier(lambda p: p == "/usr/local/bin/terminal-notifier")
        self.assertEqual(found, "/usr/local/bin/terminal-notifier")
        self.assertEqual(w.terminal_notifier(lambda p: True), "/opt/homebrew/bin/terminal-notifier")
        self.assertIsNone(w.terminal_notifier(lambda p: False))
        f = "/Users/o'neil/.cardinal/wake/resume/x y.command"
        argv = w.notify_argv("Cardinal wake", "msg", f, "/opt/homebrew/bin/terminal-notifier")
        self.assertEqual(argv[:5], ["/opt/homebrew/bin/terminal-notifier", "-title", "Cardinal wake", "-message",
                                    "msg"])
        self.assertEqual(argv[argv.index("-execute") + 1], f"/usr/bin/open -a Terminal {shlex.quote(f)}")
        self.assertEqual(shlex.split(argv[argv.index("-execute") + 1]), ["/usr/bin/open", "-a", "Terminal", f])
        self.assertNotIn("-execute", w.notify_argv("t", "m", None, "/opt/homebrew/bin/terminal-notifier"))

    def test_osascript_otherwise_with_the_text_as_arguments(self):
        argv = w.notify_argv("Cardinal wake", 'say "hi" & do shell script "x"', "/x.command", None)
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[-2:], ["Cardinal wake", 'say "hi" & do shell script "x"'])
        self.assertNotIn("/x.command", argv)

    def test_notify_macos_picks_the_notifier(self):
        runs = []
        with patch.object(w.sys, "platform", "darwin"), \
                patch.object(w.subprocess, "run", lambda argv, **kw: runs.append(argv)), \
                patch.object(w, "terminal_notifier", lambda: "/opt/homebrew/bin/terminal-notifier"):
            w.notify_macos("t", "m", "/f.command")
        with patch.object(w.sys, "platform", "darwin"), \
                patch.object(w.subprocess, "run", lambda argv, **kw: runs.append(argv)), \
                patch.object(w, "terminal_notifier", lambda: None):
            w.notify_macos("t", "m", "/f.command")
        self.assertEqual(runs[0][0], "/opt/homebrew/bin/terminal-notifier")
        self.assertIn("-execute", runs[0])
        self.assertEqual(runs[1][0], "/usr/bin/osascript")


# ---------------------------------------------------------------------------
# Liveness (the notification's wording)
# ---------------------------------------------------------------------------

class Liveness(Base):
    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]

    def poller_status(self, anchor, written_ago=500):
        ie._ensure_dirs(self.home)
        ie.write_json(poller.status_path(self.home, SID), {"pid": 1, "anchor": anchor})
        os.utime(str(poller.status_path(self.home, SID)), (self.t0 - written_ago, self.t0 - written_ago))

    def live(self, phrase):
        self.run_pass()
        self.assertIn(phrase, self.notified())
        self.assertIn("switch to that window", self.world.notes[0][1])

    def stopped(self):
        self.run_pass()
        self.assertIn("stopped", self.notified())
        self.assertIn("Resume:", self.world.notes[0][1])

    def test_poller_anchor_alive_and_older_than_its_status_file(self):
        self.poller_status(777)
        self.world.alive.add(777)
        self.world.starts[777] = self.t0 - 1000
        self.live("agent process")

    def test_a_reused_anchor_pid_is_dead(self):
        self.poller_status(777)
        self.world.alive.add(777)
        self.world.starts[777] = self.t0 - 100   # started after the status file was written
        self.stopped()

    def test_an_anchor_whose_start_is_unknown_counts_as_live(self):
        self.poller_status(777)
        self.world.alive.add(777)
        self.live("agent process")

    def test_a_dead_anchor(self):
        self.poller_status(777)
        self.stopped()

    def test_a_process_naming_the_session(self):
        self.world.table = [(1234, f"node /opt/codex resume {SID}")]
        self.live("names the session")

    def test_an_unreadable_process_table_counts_as_live(self):
        self.world.table = None
        self.run_pass(process_table=lambda: None)
        self.assertIn("process table", self.notified())

    def test_a_rollout_written_in_the_last_two_minutes(self):
        self.age(self.rollout_path(SID), 30)
        self.live("transcript changed")

    def test_a_transcript_written_in_the_last_two_minutes(self):
        self.rollout_path(SID).unlink()
        t = self.home / ".claude" / "projects" / "p" / f"{SID}.jsonl"
        t.parent.mkdir(parents=True)
        t.write_text("{}\n")
        self.age(t, 30)
        self.live("transcript changed")

    def test_the_binding_active_file_touched_in_the_last_two_minutes(self):
        ie.touch_activity(self.home, SID)
        os.utime(str(ie.activity_path(self.home, SID)), (self.t0 - 30, self.t0 - 30))
        self.live("used a tool")

    def test_old_signals_are_not_live(self):
        ie.touch_activity(self.home, SID)
        os.utime(str(ie.activity_path(self.home, SID)), (self.t0 - 500, self.t0 - 500))
        self.world.table = [(1, "/sbin/launchd"), (os.getpid(), f"python waker {SID}")]
        self.stopped()


# ---------------------------------------------------------------------------
# §5.4 the ledger: one notification per decision
# ---------------------------------------------------------------------------

def _race_worker(home, t0, barrier, notes_file):
    """One waker process of the race (spawned: module level)."""
    world = World(t0)
    world.events = [decision(5)]

    def notify(title, msg, open_file=None):
        with open(notes_file, "a") as f:
            f.write(f"{os.getpid()}\n")

    system = world.system(notifier=notify, before_claim=lambda sid, seq: barrier.wait(60), out=lambda line: None)
    with patch.object(w, "temp_dirs", lambda: ()):
        w.run_once(Path(home), system)


class Lifecycle(Base):
    def test_two_waker_processes_racing_one_decision_notify_exactly_once(self):
        """Two OS processes run a pass against the same decision; both reach
        the record together (a barrier right before the session lock), and
        exactly one notifies."""
        self.world.events = [decision(5)]
        notes = Path(self.tmp.name) / "notes.txt"
        ctx = multiprocessing.get_context("spawn")
        for _ in range(3):   # a few rounds: the outcome must never depend on who wins
            notes.write_text("")
            with contextlib_suppress_file(w.ledger_path(self.home, SID)):
                pass
            barrier = ctx.Barrier(2)
            procs = [ctx.Process(target=_race_worker, args=(str(self.home), self.t0, barrier, str(notes)))
                     for _ in range(2)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(120)
            self.assertEqual([p.exitcode for p in procs], [0, 0])
            self.assertEqual(len(notes.read_text().splitlines()), 1)
            self.assertEqual(self.state(5), w.NOTIFIED)

    def test_every_pending_decision_is_notified_in_ascending_seq(self):
        self.world.events = [decision(7), decision(5), decision(6, wake=NOWAKE)]
        report = self.run_pass()
        self.assertEqual([r["seq"] for r in report if r["action"] == "notified"], [5, 6, 7])
        self.assertEqual(len(self.world.notes), 3)

    def test_a_seq_with_a_ledger_entry_is_never_notified_again(self):
        self.world.events = [decision(5)]
        self.run_pass()
        led = w.read_ledger(self.home, SID)
        for e in led["entries"].values():
            e["updated_at"] = self.world.t - w.LEDGER_KEEP - 10
        w.write_ledger(self.home, SID, led, self.world.t)   # prunes the terminal entry
        self.assertEqual(self.ledger(), {})
        self.world.t += 600
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)          # the high-water mark holds

    def test_a_decision_for_another_investigation_is_a_different_decision(self):
        self.world.events = [decision(5)]
        self.run_pass()
        ie.write_binding(self.home, SID, binding(inv=INV2))
        self.world.events = [decision(5, inv=INV2)]
        self.world.t += 600
        self.run_pass()
        self.assertEqual(self.ledger(inv=INV2)["5"]["state"], w.NOTIFIED)
        self.assertEqual(len(self.world.notes), 2)


class LegacyLedger(Base):
    """Ledgers an earlier version (unattended launch) wrote still load: their
    launch states are terminal, never settled or reported again."""

    def write_legacy(self):
        led = {"session_id": SID, "hwm": {INV: 4}, "probe_retries": {INV + ":9": 1}, "entries": {}}
        for i, st in enumerate(w.LEGACY_STATES):
            seq = i + 1
            led["entries"][f"{INV}:{seq}"] = {
                "seq": seq, "investigation_id": INV, "runtime": "codex", "state": st, "claimed_at": self.t0 - 2 * 86400,
                "updated_at": self.t0 - 2 * 86400, "reports": [], "tmp_dir": "/nonexistent/tmp",
                "child": {"pid": 99999, "start": self.t0, "log": "/nonexistent.log"},
                "history": [{"state": "CLAIMED", "at": "2026-10-11T00:00:00Z"}, {"state": st, "at": "2026-10-11T"}]}
        w.ledger_dir(self.home).mkdir(parents=True)
        w.ledger_path(self.home, SID).write_text(json.dumps(led))

    def test_an_old_ledger_loads_and_a_new_decision_is_notified(self):
        self.write_legacy()
        led = w.read_ledger(self.home, SID)
        self.assertEqual(len(led["entries"]), len(w.LEGACY_STATES))
        self.assertNotIn("probe_retries", led)
        self.world.events = [decision(2), decision(7)]
        self.run_pass()
        self.assertEqual(self.state(2), "RESUMING")          # never reconsidered
        self.notified(7)
        self.assertEqual(len(self.world.notes), 1)
        self.assertEqual(self.world.execs, [])

    def test_legacy_states_are_terminal_and_pruned(self):
        self.write_legacy()
        led = w.read_ledger(self.home, SID)
        w.write_ledger(self.home, SID, led, self.t0 + w.LEDGER_KEEP)
        led = w.read_ledger(self.home, SID)
        self.assertEqual(led["entries"], {})
        self.assertEqual(led["hwm"], {INV: 4})

    def test_status_lists_them(self):
        self.write_legacy()
        out = []
        self.assertEqual(w.status(self.home, self.world.system(), out.append), 0)
        self.assertIn(f"{SID[:8]} #2 RESUMING", "\n".join(out))

    def test_an_old_config_with_launch_on_is_read_and_ignored(self):
        self.config(launch=True, codex_path="/opt/homebrew/bin/codex", roots=["/x"], relay_notes=True)
        c = w.read_config(self.home)
        self.assertTrue(c["enabled"])
        self.assertNotIn("launch", c)
        self.world.events = [decision(5)]
        self.run_pass()
        self.notified()
        self.assertEqual(self.world.execs, [])


# ---------------------------------------------------------------------------
# Other runtimes
# ---------------------------------------------------------------------------

class OtherRuntimes(Base):
    def setUp(self):
        super().setUp()
        self.rollout_path(SID).unlink()
        self.calls = []
        self.world.events = [decision(5)]

    def connection(self, runtime, home):
        self.calls.append(runtime)
        return dict(MEMBER)

    def gemini_chat(self, sid=SID, suffix=".jsonl", head=None):
        p = self.home / ".gemini" / "tmp" / "my-project" / "chats" / f"session-2026-10-12T10-00-{sid[:8]}{suffix}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((head or json.dumps({"sessionId": sid, "projectHash": "h", "kind": "main"})) + "\n")
        self.age(p)
        return p

    def cursor_transcript(self, sid=SID):
        p = self.home / ".cursor" / "projects" / "Users-x-repo" / "agent-transcripts" / f"{sid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}\n")
        self.age(p)
        return p

    def assert_notified(self, runtime, label):
        self.run_pass(connection=self.connection)
        entry = self.ledger()["5"]
        self.assertEqual((entry["state"], entry["runtime"]), (w.NOTIFIED, runtime))
        self.assertEqual(len(self.world.notes), 1)
        self.assertTrue(self.world.notes[0][1].startswith(f"Owner decision for {label} session {SID[:8]} "),
                        self.world.notes[0])
        self.assertNotIn("SECRET", self.world.notes[0][1])
        return entry

    def test_gemini(self):
        self.gemini_chat()
        self.assert_notified("gemini", "Gemini CLI")
        self.assertEqual(self.calls[0], "gemini")   # its own plugin's key first

    def test_a_legacy_gemini_json_chat(self):
        self.gemini_chat(suffix=".json", head=json.dumps({"sessionId": SID, "messages": []}, indent=2))
        self.assert_notified("gemini", "Gemini CLI")

    def test_a_gemini_chat_for_another_session_with_the_same_prefix_is_not_it(self):
        self.gemini_chat(head=json.dumps({"sessionId": SID[:8] + "-0000-0000-0000-000000000000"}))
        self.assert_notified("unknown", "Unknown-runtime")

    def test_cursor(self):
        self.cursor_transcript()
        self.assert_notified("cursor", "Cursor")
        self.assertEqual(self.calls[0], "cursor")

    def test_a_cursor_chat_store(self):
        d = self.home / ".cursor" / "chats" / "abc123" / SID
        d.mkdir(parents=True)
        (d / "store.db").write_bytes(b"x")
        self.age(d / "store.db")
        self.assert_notified("cursor", "Cursor")

    def test_an_unknown_runtime_is_notified(self):
        self.assert_notified("unknown", "Unknown-runtime")
        self.assertEqual(self.calls, ["codex"])    # tries every plugin's key, first match wins

    def test_records_of_two_runtimes_are_unknown(self):
        self.session(SID)
        self.gemini_chat()
        self.assert_notified("unknown", "Unknown-runtime")
        self.assertIn("Codex and Gemini CLI", w.runtime_kind(self.home, SID).reason)

    def test_a_live_gemini_session_is_notified_as_live(self):
        p = self.gemini_chat()
        self.age(p, 30)
        self.assertIn("live", self.assert_notified("gemini", "Gemini CLI")["reason"])

    def test_the_plugin_key_of_the_bindings_org_is_used(self):
        self.gemini_chat()

        def by_runtime(runtime, home):
            self.calls.append(runtime)
            return dict(MEMBER) if runtime == "claude" else {**MEMBER, "org": "other"}
        self.run_pass(connection=by_runtime)
        self.assertEqual(self.calls[:2], ["gemini", "codex"])
        self.assertEqual(self.state(5), w.NOTIFIED)


# ---------------------------------------------------------------------------
# resume: attended, in the owner's terminal
# ---------------------------------------------------------------------------

class Resume(Base):
    def setUp(self):
        super().setUp()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        for name in ("claude", "codex", "gemini", "cursor-agent", "agent"):
            self.tool(name)
        self.environ = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        ie.write_binding(self.home, SID, binding(cwd=str(self.home)))   # a binding's cwd is never used

    def tool(self, name):
        p = self.bin / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
        return p

    def resume(self, arg=SID, *, print_only=False):
        out = []
        code = w.resume(self.home, arg, self.world.system(out=out.append, environ=self.environ),
                        print_only=print_only)
        return code, "\n".join(out)

    def notify5(self):
        self.world.events = [decision(5)]
        self.run_pass()

    def plan(self, sid=SID):
        return w.resume_plan(self.home, sid, self.environ)

    # -- the session ------------------------------------------------------
    def test_a_unique_prefix_of_eight_hex_characters(self):
        self.assertEqual(w.resolve_session(self.home, SID[:8]), SID)
        self.assertEqual(w.resolve_session(self.home, SID[:8].upper()), SID)
        self.assertEqual(w.resolve_session(self.home, SID[:13]), SID)

    def test_an_ambiguous_prefix(self):
        other = SID[:8] + "-0000-0000-0000-000000000000"
        ie.write_binding(self.home, other, binding())
        with self.assertRaises(w.ResumeError) as e:
            w.resolve_session(self.home, SID[:8])
        self.assertIn("ambiguous", str(e.exception))
        self.assertEqual(w.resolve_session(self.home, SID[:10]), SID)

    def test_a_prefix_that_is_too_short_or_not_hex(self):
        for bad in (SID[:7], "", "01a1-32", "zzzzzzzz", "../../etc", "-01a13279"):
            with self.subTest(bad=bad), self.assertRaises(w.ResumeError):
                w.resolve_session(self.home, bad)

    def test_prefixes_cover_the_relocated_state_home_too(self):
        from cardinal_core import paths
        gsid = "8a6e4964-0d1f-4c55-9f0e-2b7c1d3a4e5f"
        ie.write_binding(paths.sandboxed_home(self.home), gsid, binding())
        self.assertEqual(w.resolve_session(self.home, gsid[:8]), gsid)

    def test_an_unbound_prefix(self):
        with self.assertRaises(w.ResumeError):
            w.resolve_session(self.home, "ffffffff")

    def test_only_a_uuid_reaches_an_argv(self):
        for bad in ("not-a-uuid", SID + "x", "-" + SID[1:], SID.replace("-", "")):
            with self.subTest(bad=bad), self.assertRaises(w.ResumeError):
                w.resume_plan(self.home, bad, self.environ)
        ie.write_binding(self.home, "0123456789abcdef", binding())   # a valid binding id, not a UUID
        with self.assertRaises(w.ResumeError):
            w.resolve_session(self.home, "01234567")

    # -- each runtime -----------------------------------------------------
    def test_codex_cli(self):
        self.notify5()
        plan = self.plan()
        self.assertEqual(plan.argv, [str(self.bin / "codex"), "resume", SID, oi.wake_prompt(5, INV)])
        self.assertEqual(plan.cwd, str(self.repo))   # the rollout's session_meta.cwd, never the binding
        code, out = self.resume()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.world.execs, [(plan.argv, str(self.repo))])

    def test_codex_cwd_is_the_meta_whose_id_is_the_session(self):
        p = self.rollout_path(SID)
        other = self.home / "elsewhere"
        other.mkdir()
        p.write_text(json.dumps(meta(SID2, str(other))) + "\n" + p.read_text())   # a forked parent's meta first
        self.assertEqual(self.plan().cwd, str(self.repo))

    def test_codex_desktop_opens_the_thread(self):
        self.session(SID, originator="Codex Desktop", source="vscode")
        plan = self.plan()
        self.assertEqual(plan.argv, ["/usr/bin/open", f"codex://threads/{SID}"])
        self.assertIsNone(plan.cwd)
        code, out = self.resume()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.world.execs, [(["/usr/bin/open", f"codex://threads/{SID}"], None)])
        self.assertIn("desktop", out)

    def claude_transcript(self, *cwds):
        self.rollout_path(SID).unlink()
        d = self.home / ".claude" / "projects" / w.claude_project_dir_name(str(self.repo))
        d.mkdir(parents=True)
        lines = [{"type": "user", "sessionId": SID, "cwd": c, "message": {"content": "x"}} for c in cwds]
        (d / f"{SID}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))

    def test_claude_code(self):
        self.claude_transcript(str(self.repo))
        self.notify5()
        plan = self.plan()
        self.assertEqual(plan.argv, [str(self.bin / "claude"), "--resume", SID, oi.wake_prompt(5, INV)])
        self.assertEqual(plan.cwd, str(self.repo))

    def test_claude_code_prefers_the_cwd_that_names_its_project_directory(self):
        sub = self.repo / "sub"
        sub.mkdir()
        self.claude_transcript(str(sub), str(self.repo), str(sub))
        self.assertEqual(self.plan().cwd, str(self.repo))

    def test_gemini_cli(self):
        self.rollout_path(SID).unlink()
        chat = self.home / ".gemini" / "tmp" / "h" / "chats" / f"session-2026-10-12T10-00-{SID[:8]}.jsonl"
        chat.parent.mkdir(parents=True)
        chat.write_text(json.dumps({"sessionId": SID}) + "\n")
        (chat.parent.parent / ".project_root").write_text(str(self.repo) + "\n")
        plan = self.plan()
        self.assertEqual(plan.argv, [str(self.bin / "gemini"), "--resume", SID])   # nothing notified: no prompt
        self.assertEqual(plan.cwd, str(self.repo))
        self.assertIn("without a first prompt", plan.note)

    def cursor_transcript(self, rec):
        self.rollout_path(SID).unlink()
        p = self.home / ".cursor" / "projects" / "Users-x-repo" / "agent-transcripts" / f"{SID}.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(rec) + "\n")

    def test_cursor_agent_experimental(self):
        self.cursor_transcript({"workspace_roots": [str(self.repo)]})
        self.notify5()
        plan = self.plan()
        self.assertEqual(plan.argv, [str(self.bin / "cursor-agent"), "--resume", SID, oi.wake_prompt(5, INV)])
        code, out = self.resume(print_only=True)
        self.assertIn("Cursor (experimental)", out)

    def test_cursor_without_cursor_agent_says_how_to_reopen_it(self):
        self.cursor_transcript({"cwd": str(self.repo)})
        (self.bin / "cursor-agent").unlink()      # `agent` stays on PATH: never used
        code, out = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("cursor-agent was not found", out)
        self.assertIn("Reopen it in Cursor", out)
        self.assertEqual(self.world.execs, [])

    def test_cursor_records_without_a_directory(self):
        self.cursor_transcript({"role": "user"})
        code, out = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("Reopen it in Cursor", out)

    # -- refusals ---------------------------------------------------------
    def test_a_missing_working_directory_is_refused(self):
        gone = self.home / "gone"
        self.session(SID, cwd=gone)
        code, out = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("no longer exists", out)
        self.assertEqual(self.world.execs, [])

    def test_an_unknown_runtime_is_refused(self):
        self.rollout_path(SID).unlink()
        code, out = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("cannot tell which runtime", out)

    def test_a_missing_binary(self):
        (self.bin / "codex").unlink()
        code, out = self.resume()
        self.assertEqual(code, 1)
        self.assertIn("codex was not found", out)

    def test_an_exec_that_fails(self):
        self.world.exec_error = FileNotFoundError(2, "gone")
        code, out = self.resume()
        self.assertEqual(code, 1)

    # -- the prompt and --print ----------------------------------------------
    def test_the_prompt_is_the_lowest_recent_notified_decision_of_the_newest_investigation(self):
        self.world.events = [decision(5), decision(6)]
        self.run_pass()
        self.assertEqual(self.plan().prompt, oi.wake_prompt(5, INV))
        ie.write_binding(self.home, SID, binding(inv=INV2))
        self.world.events = [decision(3, inv=INV2)]
        self.world.t += 600
        self.run_pass()
        self.assertEqual(self.plan().prompt, oi.wake_prompt(3, INV2))

    def test_print_shows_the_quoted_command_and_runs_nothing(self):
        odd = self.home / "work" / "it's a repo"
        (odd / ".git").mkdir(parents=True)
        self.session(SID, cwd=odd)
        self.notify5()
        code, out = self.resume(SID[:8], print_only=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.world.execs, [])
        lines = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
        self.assertEqual(lines["cwd"], str(odd))
        self.assertEqual(shlex.split(lines["command"]),
                         [str(self.bin / "codex"), "resume", SID, oi.wake_prompt(5, INV)])


# ---------------------------------------------------------------------------
# MACHINE_PROMPTS
# ---------------------------------------------------------------------------

class WakePrompt(unittest.TestCase):
    def test_the_wake_prompt_is_a_machine_prompt_on_every_runtime(self):
        for seq in (1, 5, 123456789):
            for runtime in ("claude", "codex", "cursor", "gemini"):
                with self.subTest(seq=seq, runtime=runtime):
                    self.assertTrue(oi.is_machine_prompt(runtime, oi.wake_prompt(seq, INV)))
                    self.assertIsNone(oi.normalize_prompt(runtime, oi.wake_prompt(seq, INV)))

    def test_anything_added_to_it_is_owner_input(self):
        p = oi.wake_prompt(5, INV)
        for runtime in ("claude", "codex", "cursor", "gemini"):
            self.assertEqual(oi.normalize_prompt(runtime, p + " Also deploy."), p + " Also deploy.")
            self.assertEqual(oi.normalize_prompt(runtime, "Please: " + p), "Please: " + p)

    def test_it_never_carries_decision_text_or_the_old_ack_file(self):
        p = oi.wake_prompt(5, INV)
        self.assertIn("storyboard__read_investigation_events", p)
        self.assertIn('types ["owner.decided"], after 4', p)
        self.assertNotIn("cardinal-wake-ack.json", p)
        self.assertNotIn("TMPDIR", p)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CLI(Base):
    def cli(self, *argv, platform="darwin", runtime="codex", environ=None):
        out, calls = [], []
        system = self.world.system(out=out.append, environ=environ or {"PATH": "/usr/bin:/bin"})
        code = w.cli_main(list(argv), runtime=runtime, home=self.home, system=system,
                          launchctl=lambda a: (calls.append(list(a)) or (0, "")), platform=platform,
                          environ=environ or {"PATH": "/usr/bin:/bin"})
        return code, "\n".join(out), calls

    def test_enable_writes_a_plist_with_a_stable_path_and_the_config(self):
        w.config_path(self.home).unlink()
        code, out, calls = self.cli("enable")
        self.assertEqual(code, 0, out)
        plist = plistlib.loads(w.plist_path(self.home).read_bytes())
        runner = self.home / ".cardinal" / "wake" / "lib" / "cardinal-wake"
        self.assertEqual(plist["Label"], "io.cardinalhq.wake")
        self.assertEqual(plist["ProgramArguments"][1:], ["-I", "-S", str(runner), "run", "--once"])
        self.assertTrue(30 <= plist["StartInterval"] <= 60)
        self.assertNotIn("plugins", json.dumps(plist))
        self.assertTrue((runner.parent / "cardinal_core" / "session_wake.py").is_file())
        self.assertFalse((runner.parent / "cardinal_core" / "allowlist_proxy.py").exists())
        self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o700)
        c = json.loads(w.config_path(self.home).read_text())
        self.assertEqual(set(c), {"enabled", "python", "path_env", "binaries", "enabled_at", "versions"})
        self.assertTrue(c["enabled"])
        self.assertEqual(stat.S_IMODE(w.config_path(self.home).stat().st_mode), 0o600)
        self.assertEqual([a[0] for a in calls], ["bootout", "bootstrap"])
        self.assertIn("Nothing is ever launched unattended", out)
        self.assertIn("cardinal-wake resume <session>", out)

    def test_enable_launch_is_refused_in_one_line(self):
        w.config_path(self.home).unlink()
        code, out, calls = self.cli("enable", "--launch")
        self.assertEqual(code, 2)
        self.assertEqual(out, w.LAUNCH_REMOVED)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("unattended launch was removed", out)
        self.assertEqual(calls, [])
        self.assertFalse(w.plist_path(self.home).exists())
        self.assertFalse(w.config_path(self.home).exists())

    def test_reenable_and_relay_notes_are_gone(self):
        with self.assertRaises(SystemExit):
            self.cli("reenable", "codex")
        with self.assertRaises(SystemExit):
            self.cli("enable", "--relay-notes")

    def test_the_installed_runner_imports_its_own_copy_and_resumes(self):
        self.cli("enable")
        runner = self.home / ".cardinal" / "wake" / "lib" / "cardinal-wake"
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        r = subprocess.run([sys.executable, str(runner), "--help"], capture_output=True, text=True, timeout=60,
                           env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("resume", r.stdout)
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/sh\n")
        (bin_dir / "codex").chmod(0o755)
        r = subprocess.run([sys.executable, "-I", "-S", str(runner), "resume", SID[:8], "--print"],
                           capture_output=True, text=True, timeout=60, env={**env, "PATH": f"{bin_dir}:/usr/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"command: {bin_dir / 'codex'} resume {SID}", r.stdout)
        self.assertIn(f"cwd: {self.repo}", r.stdout)

    def test_enable_again_updates_in_place(self):
        self.cli("enable")
        first = json.loads(w.config_path(self.home).read_text())["enabled_at"]
        code, out, _ = self.cli("enable")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(w.config_path(self.home).read_text())["enabled_at"], first)

    def test_enable_on_claude_mentions_remote_control(self):
        code, out, _ = self.cli("enable", runtime="claude")
        self.assertEqual(code, 0, out)
        self.assertIn("claude --remote-control", out)

    def test_enable_is_macos_only(self):
        code, out, calls = self.cli("enable", platform="linux")
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertFalse(w.plist_path(self.home).exists())

    def test_disable_removes_the_agent(self):
        self.cli("enable")
        code, out, calls = self.cli("disable")
        self.assertEqual(code, 0)
        self.assertFalse(w.plist_path(self.home).exists())
        self.assertEqual(calls[0][0], "bootout")
        self.assertFalse(json.loads(w.config_path(self.home).read_text())["enabled"])
        self.world.events = [decision(5)]
        self.run_pass()
        self.nothing()

    def test_status_shows_each_candidates_runtime_and_resume_command_never_decision_text(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/sh\n")
        (bin_dir / "codex").chmod(0o755)
        self.session(SID2, cwd=self.home / "gone")
        self.world.events = [decision(5), decision(6, to=SID2)]
        self.run_pass()
        code, out, _ = self.cli("status", environ={"PATH": f"{bin_dir}:/usr/bin"})
        self.assertEqual(code, 0)
        self.assertIn("wake: on", out)
        self.assertNotIn("unattended launch", out)
        self.assertIn(f"{SID[:8]} #5 NOTIFIED", out)
        self.assertIn(f"  {SID[:8]} Codex: ~/.cardinal/wake/lib/cardinal-wake resume {SID[:8]} -> "
                      f"{bin_dir / 'codex'} resume {SID} <wake prompt> (in {self.repo})", out)
        self.assertIn(f"  {SID2[:8]} Codex: cannot resume here:", out)
        for path in (self.home / ".cardinal" / "wake").rglob("*"):
            if path.is_file() and path.suffix == ".json":
                self.assertNotIn(SECRET, path.read_text())
        for blob in [out] + [m for _, m, _ in self.world.notes] + self.world.lines:
            self.assertNotIn("SECRET", blob)

    def test_resume_through_the_cli(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/sh\n")
        (bin_dir / "codex").chmod(0o755)
        code, out, _ = self.cli("resume", SID[:8], environ={"PATH": f"{bin_dir}:/usr/bin"})
        self.assertEqual(code, 0, out)
        self.assertEqual(self.world.execs, [([str(bin_dir / "codex"), "resume", SID], str(self.repo))])

    def test_run_dry_run(self):
        self.world.events = [decision(5)]
        code, out, _ = self.cli("run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would notify", out)
        self.nothing()


class contextlib_suppress_file:
    """Remove `path` (if present) on entry: a fresh ledger per race round."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
