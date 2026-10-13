"""Owner-woken sessions (cardinal_core.session_wake; docs/specs/resume-and-wake.md §5, §7).

Every effect is faked: a temp HOME, a fixed clock, a fake process table,
process start times, launcher, notifier and Cardinal reads, and an injected
launchctl. Nothing here starts an agent or installs a launchd agent.

Run from core/:  python3 -m unittest tests.test_session_wake -v
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import plistlib
import stat
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
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "CARDINAL_STORYBOARD_SESSION_START": "0",
       "CARDINAL_INVESTIGATION_POLLER": "0", "CARDINAL_INVESTIGATION_ID": INV2, "CARDINAL_OWNER_INPUT": "0",
       "CODEX_HOME": "/tmp/elsewhere", "CODEX_SESSION_ID": "parent", "LANG": "en_US.UTF-8"}


def decision(seq, to=SID, inv=INV, **producer):
    p = {"principal": {"kind": "user", "id": "owner"}, "session_id": None, "is_investigation_author": True}
    p.update(producer)
    return {"investigation_id": inv, "seq": seq, "class": "owner", "type": "owner.decided",
            "authority": "owner_decision", "created_at": "2026-10-12T12:00:00Z", "to_session_id": to,
            "payload": {"text": SECRET}, "producer": p}


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


def turn(mode="workspace-write", network=False, ts="2026-10-12T23:36:11.185Z"):
    policy = {"type": mode}
    if network is not None:
        policy["network_access"] = network
    return {"timestamp": ts, "type": "turn_context", "payload": {"cwd": "/ignored", "approval_policy": "never",
                                                                   "sandbox_policy": policy}}


def meta(sid, cwd, originator="codex_exec", source="exec"):
    return {"timestamp": "2026-10-12T23:36:08.796Z", "type": "session_meta",
            "payload": {"id": sid, "cwd": cwd, "originator": originator, "source": source,
                        "cli_version": "0.162.1", "base_instructions": {"text": "x" * 2000}}}


class World:
    """The faked outside world one waker pass sees."""

    def __init__(self, t0):
        self.t = t0
        self.events = []
        self.launches = []
        self.attempts = 0
        self.spawn_errors = 0
        self.alive = set()
        self.starts = {}
        self.table = []
        self.notes = []
        self.lines = []
        self.next_pid = 40000
        self.fetch_complete = True

    def fetch(self, conn, inv, sid, after, client):
        assert conn == MEMBER
        return [e for e in self.events if e["investigation_id"] == inv and e["seq"] > after
                and e.get("to_session_id") in (None, sid)], self.fetch_complete

    def launch(self, argv, cwd, env, log):
        self.attempts += 1
        if self.spawn_errors:
            self.spawn_errors -= 1
            raise FileNotFoundError(2, "no such file")
        pid = self.next_pid
        self.next_pid += 1
        self.launches.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "log": log, "pid": pid})
        self.alive.add(pid)
        self.starts[pid] = self.t
        return pid

    def system(self, **kw):
        args = dict(now=lambda: self.t, alive=lambda pid: pid in self.alive,
                    process_start=lambda pid: self.starts.get(pid), process_table=lambda: list(self.table),
                    launcher=self.launch, notifier=lambda title, msg: self.notes.append((title, msg)),
                    connection=lambda runtime, home: dict(MEMBER), fetch=self.fetch, environ=ENV,
                    out=self.lines.append)
        args.update(kw)
        return w.System(**args)

    def exit(self, pid=None):
        """The (last) launched child exits."""
        self.alive.discard(pid if pid is not None else self.launches[-1]["pid"])


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
        self.t0 = time.time()
        self.world = World(self.t0)
        self.codex = self.home / "bin" / "codex"
        self.codex.parent.mkdir()
        self.codex.write_text("#!/bin/sh\nexit 1\n")   # never executed: the launcher is faked
        self.codex.chmod(0o755)
        self.config()
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.session(SID)

    # -- fixtures --------------------------------------------------------
    def config(self, enabled=True, launch=True, codex=True):
        w.write_config(self.home, {"enabled": enabled, "launch": launch,
                                   "codex_path": str(self.codex) if codex else None,
                                   "enabled_at": "2026-10-12T00:00:00Z"})

    def rollout_path(self, sid):
        return self.home / ".codex" / "sessions" / "2026" / "10" / "12" / f"rollout-2026-10-12T16-36-07-{sid}.jsonl"

    def session(self, sid, turns=None, originator="codex_exec", source="exec", cwd=None, bind=True, **bextra):
        if bind:
            ie.write_binding(self.home, sid, binding(**bextra))
        p = self.rollout_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        recs = [meta(sid, str(cwd or self.repo), originator, source)] + (turns if turns is not None else [turn()])
        p.write_text("".join(json.dumps(r) + "\n" for r in recs))
        self.age(p)
        return p

    def append_turns(self, sid, *turns):
        with open(self.rollout_path(sid), "a") as f:
            for t in turns:
                f.write(json.dumps(t) + "\n")
        self.age(self.rollout_path(sid))

    def age(self, path, seconds=1000):
        os.utime(str(path), (self.t0 - seconds, self.t0 - seconds))

    def run_pass(self, **kw):
        return w.run_once(self.home, self.world.system(**kw))

    def ledger(self, sid=SID):
        return w.read_ledger(self.home, sid)["entries"]

    def state(self, seq, sid=SID):
        return self.ledger(sid).get(str(seq), {}).get("state")

    def notified_only(self, seq=5, sid=SID):
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.state(seq, sid), w.NOTIFIED)
        return self.ledger(sid)[str(seq)]["reason"]

    def nothing(self, sid=SID):
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.ledger(sid), {})
        self.assertEqual(self.world.notes, [])


# ---------------------------------------------------------------------------
# §5.2 the trigger
# ---------------------------------------------------------------------------

class Predicate(Base):
    def test_a_verified_decision_addressed_to_a_stopped_eligible_session_launches_once(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertEqual(self.state(5), w.RESUMING)
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

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

    def test_a_broadcast_decision_never_wakes(self):
        self.world.events = [decision(5, to=None)]
        self.run_pass()
        self.nothing()

    def test_a_decision_addressed_to_another_session_never_wakes(self):
        self.world.events = [decision(5, to=SID2)]
        self.run_pass()
        self.nothing()

    def test_an_acknowledged_decision_never_wakes(self):
        self.world.events = [decision(5), ack(6, 5)]
        self.run_pass()
        self.nothing()

    def test_an_acknowledgment_by_another_session_does_not_count(self):
        self.world.events = [decision(5), ack(6, 5, sid=SID2)]
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

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

    def test_a_seq_with_a_ledger_entry_is_never_woken_again(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 10
        self.run_pass()                       # settled: FAILED (no ack)
        self.assertEqual(self.state(5), w.FAILED)
        self.world.t += 3 * w.LIVE_WINDOW
        self.run_pass()
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_machine_opt_in_off(self):
        self.config(enabled=False)
        self.world.events = [decision(5)]
        self.run_pass()
        self.nothing()

    def test_an_incomplete_read_never_wakes(self):
        self.world.events = [decision(5)]
        self.world.fetch_complete = False
        self.run_pass()
        self.nothing()

    def test_a_failed_read_never_wakes(self):
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


class Caps(Base):
    def seed(self, sid, n, inv=INV):
        led = w.read_ledger(self.home, sid)
        for i in range(n):
            e = {"seq": 100 + i, "investigation_id": inv, "runtime": "codex", "claimed_at": self.t0 - 60}
            w.transition(e, w.FAILED, self.t0 - 60, "seeded")
            led["entries"][str(100 + i)] = e
        w.write_ledger(self.home, sid, led, self.t0)

    def test_three_claims_per_session_per_day(self):
        self.seed(SID, w.SESSION_DAILY_CAP)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches, [])
        self.assertIsNone(self.state(5))           # not NOTIFIED: reconsidered once the day has passed
        self.assertEqual(len(self.world.notes), 1)
        self.run_pass()
        self.assertEqual(len(self.world.notes), 1)  # the cap is notified once a day
        self.world.t = self.t0 + w.DAY
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_ten_claims_across_sessions_per_day(self):
        for i in range(5):
            other = f"0000000{i}-0000-0000-0000-000000000000"
            self.seed(other, 2)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches, [])
        self.assertIsNone(self.state(5))


# ---------------------------------------------------------------------------
# §5.3 eligibility
# ---------------------------------------------------------------------------

class Eligibility(Base):
    def test_one_danger_full_access_turn_makes_it_notify_only(self):
        self.session(SID, turns=[turn(), turn("danger-full-access", network=None), turn()])
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("danger-full-access", self.notified_only())
        self.assertEqual(len(self.world.notes), 1)
        self.assertNotIn(SECRET, self.world.notes[0][1])

    def test_network_access_makes_it_notify_only(self):
        self.session(SID, turns=[turn("workspace-write", network=True)])
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("network", self.notified_only())

    def test_the_desktop_app_is_notify_only(self):
        self.session(SID, originator="Codex Desktop", source="vscode")
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("desktop", self.notified_only())

    def test_an_unrecognized_originator_is_notify_only(self):
        self.session(SID, originator="codex_something_new")
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("originator", self.notified_only())

    def test_a_subagent_thread_is_notify_only(self):
        self.session(SID, source={"subagent": {"thread_spawn": {"parent_thread_id": SID2}}})
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("subagent", self.notified_only())

    def test_no_rollout_is_notify_only(self):
        self.rollout_path(SID).unlink()
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("no Codex, Claude Code, Cursor or Gemini CLI records", self.notified_only())
        self.assertEqual(self.ledger()["5"]["runtime"], "unknown")
        self.assertIn("Unknown-runtime session", self.world.notes[0][1])

    def test_a_claude_session_is_notify_only(self):
        self.rollout_path(SID).unlink()
        t = self.home / ".claude" / "projects" / "-Users-x-repo" / f"{SID}.jsonl"
        t.parent.mkdir(parents=True)
        t.write_text("{}\n")
        self.age(t)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("Claude Code sessions are notify-only", self.notified_only())
        self.assertEqual(self.ledger()["5"]["runtime"], "claude")

    def test_no_turn_recorded_is_notify_only(self):
        self.session(SID, turns=[])
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("no turn", self.notified_only())

    def test_cwd_comes_from_the_rollout_never_the_binding(self):
        ie.write_binding(self.home, SID, binding(cwd="/", workspace="/etc"))
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches[0]["cwd"], str(self.repo))

    def test_a_cwd_outside_a_git_work_tree_is_notify_only(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        self.session(SID, cwd=plain)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("git work tree", self.notified_only())

    def test_a_non_uuid_session_id_is_never_launched(self):
        sid = "--dangerously-bypass-approvals-and-sandbox"
        self.session(sid)
        self.world.events = [decision(5, to=sid)]
        self.run_pass()
        self.assertIn("UUID", self.notified_only(sid=sid))

    def test_a_symlinked_rollout_is_notify_only(self):
        real = Path(self.tmp.name) / "real.jsonl"
        os.replace(str(self.rollout_path(SID)), str(real))
        os.symlink(str(real), str(self.rollout_path(SID)))
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("regular file", self.notified_only())

    def test_a_malformed_turn_record_is_notify_only(self):
        with open(self.rollout_path(SID), "a") as f:
            f.write('{"timestamp":"x","type":"turn_context","payload":{"sandbox_policy":\n')
        self.age(self.rollout_path(SID))
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("malformed", self.notified_only())

    def test_the_last_turns_mode_is_pinned(self):
        self.session(SID, turns=[turn("workspace-write"), turn("read-only")])
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches[0]["argv"][3], "read-only")

    def test_only_session_records_are_parsed_and_long_lines_are_skipped(self):
        big = json.dumps({"type": "response_item", "payload": {"text": "x" * 5000 + " turn_context"}}) + "\n"
        with patch.object(w, "MAX_LINE", 4096):
            with open(self.rollout_path(SID), "a") as f:
                f.write(big)
            self.age(self.rollout_path(SID))
            rt = w.resolve(self.home, SID)
        self.assertTrue(rt.eligible, rt.reason)


# ---------------------------------------------------------------------------
# §5.4 liveness
# ---------------------------------------------------------------------------

class Liveness(Base):
    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]

    def poller_status(self, anchor, written_ago=500):
        ie._ensure_dirs(self.home)
        ie.write_json(poller.status_path(self.home, SID), {"pid": 1, "anchor": anchor})
        os.utime(str(poller.status_path(self.home, SID)), (self.t0 - written_ago, self.t0 - written_ago))

    def test_poller_anchor_alive_and_older_than_its_status_file(self):
        self.poller_status(777)
        self.world.alive.add(777)
        self.world.starts[777] = self.t0 - 1000
        self.run_pass()
        self.assertIn("agent process", self.notified_only())

    def test_a_reused_anchor_pid_is_dead(self):
        self.poller_status(777)
        self.world.alive.add(777)
        self.world.starts[777] = self.t0 - 100   # started after the status file was written
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_an_anchor_whose_start_is_unknown_counts_as_live(self):
        self.poller_status(777)
        self.world.alive.add(777)
        self.run_pass()
        self.assertIn("agent process", self.notified_only())

    def test_a_dead_anchor(self):
        self.poller_status(777)
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_a_process_naming_the_session(self):
        self.world.table = [(1234, f"node /opt/codex resume {SID}")]
        self.run_pass()
        self.assertIn("names the session", self.notified_only())

    def test_an_unreadable_process_table_counts_as_live(self):
        self.run_pass(process_table=lambda: None)
        self.assertIn("process table", self.notified_only())

    def test_a_rollout_written_in_the_last_two_minutes(self):
        self.age(self.rollout_path(SID), 30)
        self.run_pass()
        self.assertIn("transcript changed", self.notified_only())

    def test_a_transcript_written_in_the_last_two_minutes(self):
        self.rollout_path(SID).unlink()
        t = self.home / ".claude" / "projects" / "p" / f"{SID}.jsonl"
        t.parent.mkdir(parents=True)
        t.write_text("{}\n")
        self.age(t, 30)
        self.run_pass()
        self.assertIn("live", self.notified_only())

    def test_the_binding_active_file_touched_in_the_last_two_minutes(self):
        ie.touch_activity(self.home, SID)
        os.utime(str(ie.activity_path(self.home, SID)), (self.t0 - 30, self.t0 - 30))
        self.run_pass()
        self.assertIn("used a tool", self.notified_only())

    def test_old_signals_are_not_live(self):
        ie.touch_activity(self.home, SID)
        os.utime(str(ie.activity_path(self.home, SID)), (self.t0 - 500, self.t0 - 500))
        self.world.table = [(1, "/sbin/launchd"), (os.getpid(), f"python waker {SID}")]
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_a_ledger_child_alive_holds_every_other_decision(self):
        self.run_pass()
        self.world.events.append(decision(7))
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertNotIn("7", self.ledger())   # not NOTIFIED: delivered inside the run, or considered after it


# ---------------------------------------------------------------------------
# §5.4 lifecycle: one execution per decision
# ---------------------------------------------------------------------------

def _race_worker(home, t0, barrier, launches_file):
    """One waker process of the C-5 race (spawned: module level)."""
    world = World(t0)
    world.events = [decision(5)]

    def launch(argv, cwd, env, log):
        with open(launches_file, "a") as f:
            f.write(f"{os.getpid()}\n")
        return 4242

    system = world.system(launcher=launch, alive=lambda pid: pid == 4242, process_start=lambda pid: t0,
                          before_claim=lambda sid, seq: barrier.wait(60), out=lambda line: None)
    w.run_once(Path(home), system)


class Lifecycle(Base):
    def test_two_waker_processes_racing_one_decision_launch_once(self):
        """Proof C-5: two OS processes run a pass against the same decision;
        both reach the claim together (a barrier right before the session
        lock), and exactly one launches."""
        self.world.events = [decision(5)]
        launches = Path(self.tmp.name) / "launches.txt"
        ctx = multiprocessing.get_context("spawn")
        for _ in range(3):   # a few rounds: the outcome must never depend on who wins
            launches.write_text("")
            with contextlib_suppress_file(w.ledger_path(self.home, SID)):
                pass
            barrier = ctx.Barrier(2)
            procs = [ctx.Process(target=_race_worker, args=(str(self.home), self.t0, barrier, str(launches)))
                     for _ in range(2)]
            for p in procs:
                p.start()
            for p in procs:
                p.join(120)
            self.assertEqual([p.exitcode for p in procs], [0, 0])
            self.assertEqual(len(launches.read_text().splitlines()), 1)
            self.assertEqual(self.state(5), w.RESUMING)

    def test_two_decisions_for_one_session_give_one_run(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.world.events.append(decision(7))
        self.world.t += 60
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()                                  # #5 settles: FAILED (unacknowledged)
        self.assertEqual(self.state(5), w.FAILED)
        self.assertEqual(len(self.world.launches), 1)    # the run's writes are still fresh: not yet
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()                                  # #7, still unacknowledged after the run
        self.assertEqual(len(self.world.launches), 2)
        self.assertIn("#7", self.world.launches[1]["argv"][-1])

    def test_a_decision_acknowledged_inside_the_run_is_not_considered_again(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.world.events += [decision(7), ack(8, 5), ack(9, 7)]
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.assertEqual(len(self.world.launches), 1)

    def test_a_spawn_error_is_retried_at_most_twice_then_failed(self):
        self.world.spawn_errors = 10
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.attempts, 1 + w.SPAWN_RETRIES)
        self.assertEqual(self.state(5), w.FAILED)
        self.assertEqual(self.ledger()["5"]["spawn_errors"], 3)
        self.world.t += 3600
        self.run_pass()
        self.assertEqual(self.world.attempts, 3)

    def test_a_spawn_error_then_a_start(self):
        self.world.spawn_errors = 1
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.attempts, 2)
        self.assertEqual(self.state(5), w.RESUMING)

    def test_a_started_child_that_exits_without_an_ack_fails_and_is_never_retried(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        for _ in range(3):
            self.world.t += 3600
            self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertTrue(any("failed" in m for _, m in self.world.notes))

    def test_an_acknowledged_decision_is_acknowledged(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.append_turns(SID, turn())
        self.world.events.append(ack(6, 5))
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.assertEqual(self.state(5), w.ACKNOWLEDGED)
        self.assertFalse(w.disabled_reason(self.home, "codex"))
        states = [h["state"] for h in self.ledger()["5"]["history"]]
        self.assertEqual(states, [w.DECISION_SEEN, w.CLAIMED, w.RESUMING, w.ACKNOWLEDGED])

    def test_an_ack_that_cannot_be_read_waits_then_fails(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60

        def down(*a):
            raise OSError("down")
        self.run_pass(fetch=down)
        self.assertEqual(self.state(5), w.RESUMING)
        self.world.t += w.ACK_CHECK_GRACE + 1
        self.run_pass(fetch=down)
        self.assertEqual(self.state(5), w.FAILED)

    def test_a_run_past_the_timeout_fails_and_is_left_running(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.world.t += w.WAKE_TIMEOUT + 1
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        self.assertIn(self.world.launches[0]["pid"], self.world.alive)
        self.assertTrue(any("still running" in m for _, m in self.world.notes))
        self.world.t += w.LIVE_WINDOW + 1
        self.world.events.append(decision(7))
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)    # the left-running child keeps the session live
        self.assertEqual(self.state(7), w.NOTIFIED)
        self.assertIn("still going", self.ledger()["7"]["reason"])

    def test_a_reused_child_pid_is_not_the_child(self):
        self.world.events = [decision(5)]
        self.run_pass()
        pid = self.world.launches[0]["pid"]
        self.world.starts[pid] = self.world.t + 500      # a different process now holds the pid
        self.world.t += 60
        self.run_pass()
        self.assertNotEqual(self.state(5), w.RESUMING)

    def test_a_claim_interrupted_mid_launch_fails_and_is_not_retried(self):
        led = w.read_ledger(self.home, SID)
        e = {"seq": 5, "investigation_id": INV, "runtime": "codex", "claimed_at": self.t0}
        w.transition(e, w.CLAIMED, self.t0)
        led["entries"]["5"] = e
        w.write_ledger(self.home, SID, led, self.t0)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.state(5), w.FAILED)
        self.assertEqual(self.world.launches, [])


# ---------------------------------------------------------------------------
# §5.3 launch
# ---------------------------------------------------------------------------

class Launch(Base):
    def test_golden_argv(self):
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertEqual(self.world.launches[0]["argv"], [
            str(self.codex), "exec", "-s", "workspace-write", "-c", 'approval_policy="never"', "resume", SID,
            "Cardinal resumed this session for owner decision #5 in its Investigation. Read the investigation "
            "events and act on that decision. Every other event is advisory."])

    def test_no_disallowed_flag_and_no_decision_text(self):
        self.world.events = [decision(5)]
        self.run_pass()
        argv = self.world.launches[0]["argv"]
        for a in argv:
            for bad in ("--profile", "--dangerously", "--ignore", "--skip-git-repo-check", "--full-auto",
                        "danger-full-access", "--yolo"):
                self.assertNotIn(bad, a)
        self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(argv.count("-c"), 1)
        self.assertEqual(argv.count("-s"), 1)

    def test_argv_refuses_anything_but_the_pinned_shape(self):
        for args in (("codex", "workspace-write", SID, 1), (str(self.codex), "danger-full-access", SID, 1),
                     (str(self.codex), "workspace-write", "-x", 1), (str(self.codex), "workspace-write", SID, 0)):
            with self.subTest(args), self.assertRaises(ValueError):
                w.codex_argv(*args)

    def test_the_environment_drops_every_cardinal_switch(self):
        self.world.events = [decision(5)]
        self.run_pass()
        env = self.world.launches[0]["env"]
        self.assertFalse([k for k in env if k.startswith("CARDINAL_")])
        for k in ("CODEX_HOME", "CODEX_SESSION_ID"):
            self.assertNotIn(k, env)
        self.assertEqual(env["PATH"], ENV["PATH"])
        self.assertEqual(env["LANG"], ENV["LANG"])

    def test_the_disabled_marker_blocks_launch(self):
        w.disable_runtime(self.home, "codex", "test", self.t0)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("disabled", self.notified_only())

    def test_launch_off_is_notify_only(self):
        self.config(launch=False)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("launch is off", self.notified_only())

    def test_codex_not_found_at_enable_is_notify_only(self):
        self.config(codex=False)
        self.world.events = [decision(5)]
        self.run_pass()
        self.assertIn("codex was not found", self.notified_only())

    def test_the_default_launcher_detaches_and_logs_privately(self):
        log = Path(self.tmp.name) / "logs" / "x.log"
        pid = w.launch_detached(["/bin/sh", "-c", "echo hi"], self.tmp.name, {"PATH": "/bin:/usr/bin"}, log)
        child = next(p for p in w._CHILDREN if p.pid == pid)
        child.wait(30)
        w.reap()
        self.assertNotIn(child, w._CHILDREN)
        self.assertEqual(log.read_text().strip(), "hi")
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        with self.assertRaises(OSError):
            w.launch_detached([str(Path(self.tmp.name) / "missing")], self.tmp.name, {}, log)

    def test_dry_run_launches_writes_and_notifies_nothing(self):
        self.world.events = [decision(5)]
        report = w.run_once(self.home, self.world.system(), dry_run=True)
        self.nothing()
        self.assertEqual([r["action"] for r in report], ["would launch"])


class PostRunVerification(Base):
    def launch_and_exit(self, *turns, acked=True):
        self.world.events = [decision(5)]
        self.run_pass()
        self.append_turns(SID, *turns)
        if acked:
            self.world.events.append(ack(6, 5))
        self.world.exit()
        self.world.t += 60
        self.run_pass()

    def assert_disabled_and_no_more_launches(self):
        self.assertTrue(w.disabled_reason(self.home, "codex"))
        self.assertTrue(any("disabled" in m for _, m in self.world.notes))
        self.session(SID2)
        self.world.events.append(decision(9, to=SID2))
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertIn("disabled", self.ledger(SID2)["9"]["reason"])

    def test_a_woken_turn_outside_the_pinned_sandbox_disables_codex(self):
        self.launch_and_exit(turn("danger-full-access", network=None))
        self.assertIn("danger-full-access", w.disabled_reason(self.home, "codex"))
        self.assert_disabled_and_no_more_launches()

    def test_a_woken_turn_with_network_disables_codex(self):
        self.launch_and_exit(turn("workspace-write", network=True))
        self.assert_disabled_and_no_more_launches()

    def test_no_woken_turn_disables_codex(self):
        self.launch_and_exit()
        self.assertIn("no turn", w.disabled_reason(self.home, "codex"))
        self.assert_disabled_and_no_more_launches()

    def test_a_new_rollout_file_is_verified_too(self):
        self.world.events = [decision(5)]
        self.run_pass()
        other = self.rollout_path(SID).with_name(f"rollout-2026-10-13T01-00-00-{SID}.jsonl")
        other.write_text(json.dumps(turn("danger-full-access", network=None)) + "\n")
        self.age(other)
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        self.assertTrue(w.disabled_reason(self.home, "codex"))

    def test_the_pinned_sandbox_keeps_codex_enabled(self):
        self.launch_and_exit(turn(), turn())
        self.assertIsNone(w.disabled_reason(self.home, "codex"))
        self.assertTrue(self.ledger()["5"]["verified"])

    def test_reenable_clears_the_marker(self):
        self.launch_and_exit(turn("danger-full-access", network=None))
        out = []
        code = w.cli_main(["reenable", "codex"], home=self.home, system=self.world.system(out=out.append))
        self.assertEqual(code, 0)
        self.assertIsNone(w.disabled_reason(self.home, "codex"))


class ProjectCodexConfig(Base):
    """Proof C-8: a project .codex/config.toml's MCP servers run outside the
    sandbox on resume, so a repo carrying Codex project config is
    notify-only (checked before every launch)."""

    def setUp(self):
        super().setUp()
        self.world.events = [decision(5)]

    def assert_refused(self):
        self.run_pass()
        self.assertIn("its own Codex config", self.notified_only())

    def test_config_toml_in_the_cwd(self):
        (self.repo / ".codex").mkdir()
        (self.repo / ".codex" / "config.toml").write_text('[mcp_servers.x]\ncommand = "/bin/sh"\n')
        self.assert_refused()

    def test_any_toml_or_hooks_json(self):
        for name in ("profile.toml", "hooks.json"):
            with self.subTest(name):
                self.setUp()
                (self.repo / ".codex").mkdir()
                (self.repo / ".codex" / name).write_text("{}")
                self.assert_refused()

    def test_config_in_a_parent_within_the_repo(self):
        sub = self.repo / "pkg" / "svc"
        sub.mkdir(parents=True)
        (self.repo / "pkg" / ".codex").mkdir()
        (self.repo / "pkg" / ".codex" / "config.toml").write_text("")
        self.session(SID, cwd=sub)
        self.assert_refused()

    def test_config_above_the_repository_root_is_not_walked(self):
        outer = self.repo.parent / ".codex"
        outer.mkdir()
        (outer / "config.toml").write_text("")
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_a_symlinked_codex_dir(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        os.symlink(str(target), str(self.repo / ".codex"))
        self.assert_refused()

    def test_a_codex_file_that_is_not_a_directory(self):
        (self.repo / ".codex").write_text("")
        self.assert_refused()

    @unittest.skipIf(os.geteuid() == 0, "root reads any directory")
    def test_an_unreadable_codex_dir(self):
        d = self.repo / ".codex"
        d.mkdir()
        os.chmod(str(d), 0)
        self.addCleanup(os.chmod, str(d), 0o700)
        self.assert_refused()

    def test_a_codex_dir_without_config_stays_eligible(self):
        (self.repo / ".codex").mkdir()
        (self.repo / ".codex" / "AGENTS.md").write_text("# notes")
        (self.repo / ".codex" / "prompts").mkdir()
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)

    def test_config_added_after_one_launch_blocks_the_next(self):
        self.run_pass()
        self.append_turns(SID, turn())
        self.world.exit()
        self.world.t += 60
        self.run_pass()
        (self.repo / ".codex").mkdir()
        (self.repo / ".codex" / "config.toml").write_text("")
        self.world.events.append(decision(7))
        self.world.t += w.LIVE_WINDOW + 1
        self.run_pass()
        self.assertEqual(len(self.world.launches), 1)
        self.assertIn("its own Codex config", self.ledger()["7"]["reason"])


class OtherRuntimes(Base):
    """Cursor, Gemini CLI and unrecognized runtimes: the notify path end to
    end, never a launch, even with launch on."""

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

    def assert_notified(self, runtime, label, phrase):
        self.run_pass(connection=self.connection)
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.world.attempts, 0)
        entry = self.ledger()["5"]
        self.assertEqual((entry["state"], entry["runtime"]), (w.NOTIFIED, runtime))
        self.assertIn(phrase, entry["reason"])
        self.assertEqual(len(self.world.notes), 1)
        self.assertTrue(self.world.notes[0][1].startswith(f"{label} session {SID[:8]}:"), self.world.notes[0])
        self.assertNotIn("SECRET", self.world.notes[0][1])
        return entry

    def test_gemini_is_notify_only_with_launch_on(self):
        self.gemini_chat()
        self.assert_notified("gemini", "Gemini CLI", "sandbox wraps the harness")
        self.assertEqual(self.calls[0], "gemini")   # its own plugin's key first

    def test_a_legacy_gemini_json_chat(self):
        self.gemini_chat(suffix=".json", head=json.dumps({"sessionId": SID, "messages": []}, indent=2))
        self.assert_notified("gemini", "Gemini CLI", "notify-only")

    def test_a_gemini_chat_for_another_session_with_the_same_prefix_is_not_it(self):
        self.gemini_chat(head=json.dumps({"sessionId": SID[:8] + "-0000-0000-0000-000000000000"}))
        self.assert_notified("unknown", "Unknown-runtime", "no Codex, Claude Code, Cursor or Gemini CLI records")

    def test_cursor_is_notify_only_with_launch_on(self):
        self.cursor_transcript()
        self.assert_notified("cursor", "Cursor", "Cursor sessions are notify-only")
        self.assertEqual(self.calls[0], "cursor")

    def test_a_cursor_chat_store(self):
        d = self.home / ".cursor" / "chats" / "abc123" / SID
        d.mkdir(parents=True)
        (d / "store.db").write_bytes(b"x")
        self.age(d / "store.db")
        self.assert_notified("cursor", "Cursor", "notify-only")

    def test_an_unknown_runtime_is_notified(self):
        self.assert_notified("unknown", "Unknown-runtime", "notify-only")
        self.assertEqual(self.calls, ["codex"])    # tries every plugin's key, first match wins

    def test_records_of_two_runtimes_are_unknown(self):
        self.session(SID)            # a Codex rollout (eligible on its own)
        self.gemini_chat()
        self.assert_notified("unknown", "Unknown-runtime", "Codex and Gemini CLI")

    def test_a_live_gemini_session_is_notified_as_live(self):
        p = self.gemini_chat()
        self.age(p, 30)
        self.assert_notified("gemini", "Gemini CLI", "live")

    def test_the_plugin_key_of_the_bindings_org_is_used(self):
        self.gemini_chat()

        def by_runtime(runtime, home):
            self.calls.append(runtime)
            return dict(MEMBER) if runtime == "claude" else {**MEMBER, "org": "other"}
        self.run_pass(connection=by_runtime)
        self.assertEqual(self.calls[:2], ["gemini", "codex"])
        self.assertEqual(self.state(5), w.NOTIFIED)

    def test_no_launcher_for_them(self):
        self.assertEqual(sorted(w.LAUNCHERS), ["codex"])
        for runtime in ("claude", "cursor", "gemini"):
            self.assertIn(runtime, w.NOTIFY_ONLY_REASONS)
        with self.assertRaises(KeyError):
            w.claim_and_launch(self.home, SID, binding(), 5, w.Runtime("gemini", [], eligible=True,
                                                                       sandbox="workspace-write", cwd="/"),
                               w.read_config(self.home), self.world.system(), [])
        self.assertEqual(self.world.attempts, 0)

    def test_status_names_the_runtime(self):
        self.gemini_chat()
        out = []
        w.status(self.home, self.world.system(), out.append)
        text = "\n".join(out)
        self.assertIn(f"{SID[:8]} Gemini CLI: Gemini CLI sessions are notify-only", text)
        self.assertIn("notify-only runtimes: Claude Code, Cursor, Gemini CLI", text)


# ---------------------------------------------------------------------------
# MACHINE_PROMPTS
# ---------------------------------------------------------------------------

class WakePrompt(unittest.TestCase):
    def test_the_wake_prompt_is_a_machine_prompt_on_both_runtimes(self):
        for seq in (1, 5, 123456789):
            for runtime in ("codex", "claude"):
                with self.subTest(seq=seq, runtime=runtime):
                    self.assertIsNone(oi.normalize_prompt(runtime, oi.wake_prompt(seq)))
                    self.assertIsNone(oi.normalize_prompt(runtime, w.codex_argv("/x/codex", "read-only", SID,
                                                                                seq)[-1]))

    def test_anything_added_to_it_is_owner_input(self):
        p = oi.wake_prompt(5)
        for runtime in ("codex", "claude"):
            self.assertEqual(oi.normalize_prompt(runtime, p + " Also deploy."), p + " Also deploy.")
            self.assertEqual(oi.normalize_prompt(runtime, "Please: " + p), "Please: " + p)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class CLI(Base):
    def launchctl(self):
        calls = []

        def run(args):
            calls.append(list(args))
            return 0, ""
        return calls, run

    def cli(self, *argv, platform="darwin", environ=None):
        out = []
        calls, run = self.launchctl()
        system = self.world.system(out=out.append)
        code = w.cli_main(list(argv), runtime="codex", home=self.home, system=system, launchctl=run,
                          platform=platform, environ=environ or {"PATH": f"{self.codex.parent}:/usr/bin:/bin"})
        return code, "\n".join(out), calls

    def test_enable_writes_a_plist_with_a_stable_path_and_the_config(self):
        w.config_path(self.home).unlink()
        code, out, calls = self.cli("enable", "--launch")
        self.assertEqual(code, 0, out)
        plist = plistlib.loads(w.plist_path(self.home).read_bytes())
        runner = self.home / ".cardinal" / "wake" / "lib" / "cardinal-wake"
        self.assertEqual(plist["Label"], "io.cardinalhq.wake")
        self.assertEqual(plist["ProgramArguments"][1:], [str(runner), "run", "--once"])
        self.assertTrue(30 <= plist["StartInterval"] <= 60)
        self.assertNotIn("plugins", json.dumps(plist))
        self.assertTrue((runner.parent / "cardinal_core" / "session_wake.py").is_file())
        self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o700)
        c = json.loads(w.config_path(self.home).read_text())
        self.assertEqual((c["enabled"], c["launch"], c["codex_path"]), (True, True, str(self.codex)))
        self.assertTrue(c["enabled_at"])
        self.assertEqual(stat.S_IMODE(w.config_path(self.home).stat().st_mode), 0o600)
        self.assertEqual([a[0] for a in calls], ["bootout", "bootstrap"])
        self.assertEqual(calls[1][2], str(w.plist_path(self.home)))
        self.assertIn("Can launch: Codex CLI sessions", out)
        self.assertIn("Notify-only", out)
        self.assertIn("Claude Code sessions", out)
        self.assertIn("Codex desktop", out)
        self.assertIn("danger-full-access", out)

    def test_the_installed_runner_imports_its_own_copy(self):
        self.cli("enable")
        runner = self.home / ".cardinal" / "wake" / "lib" / "cardinal-wake"
        import subprocess
        import sys
        r = subprocess.run([sys.executable, str(runner), "--help"], capture_output=True, text=True, timeout=60,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("enable", r.stdout)

    def test_enable_again_updates_in_place(self):
        self.cli("enable", "--launch")
        code, out, _ = self.cli("enable")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(w.config_path(self.home).read_text())["launch"])
        self.assertIn("Unattended launch is off", out)

    def test_enable_without_codex_on_path_says_so(self):
        code, out, _ = self.cli("enable", "--launch", environ={"PATH": "/nonexistent"})
        self.assertEqual(code, 0)
        self.assertIn("codex was not found", out)
        self.assertIsNone(json.loads(w.config_path(self.home).read_text())["codex_path"])

    def test_enable_on_notify_only_runtimes_says_so(self):
        for runtime, label in (("gemini", "Gemini CLI"), ("cursor", "Cursor"), ("claude", "Claude Code")):
            with self.subTest(runtime):
                out = []
                calls, run = self.launchctl()
                code = w.cli_main(["enable", "--launch"], runtime=runtime, home=self.home,
                                  system=self.world.system(out=out.append), launchctl=run, platform="darwin",
                                  environ={"PATH": f"{self.codex.parent}:/usr/bin:/bin"})
                text = "\n".join(out)
                self.assertEqual(code, 0, text)
                self.assertIn(f"{label} sessions are notify-only", text)
                self.assertIn("with or without --launch", text)
                self.assertIn("Can launch: Codex CLI sessions", text)
        code, text, _ = self.cli("enable", "--launch")   # runtime codex: no notify-only line for itself
        self.assertNotIn("Codex sessions are notify-only", text)

    def test_enable_is_macos_only(self):
        code, out, calls = self.cli("enable", platform="linux")
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertFalse(w.plist_path(self.home).exists())

    def test_disable_removes_the_agent(self):
        self.cli("enable", "--launch")
        code, out, calls = self.cli("disable")
        self.assertEqual(code, 0)
        self.assertFalse(w.plist_path(self.home).exists())
        self.assertEqual(calls[0][0], "bootout")
        self.assertFalse(json.loads(w.config_path(self.home).read_text())["enabled"])
        self.world.events = [decision(5)]
        self.run_pass()
        self.nothing()

    def test_status_shows_state_and_never_decision_text(self):
        self.session(SID2, turns=[turn("danger-full-access", network=None)])
        self.world.events = [decision(5), decision(6, to=SID2)]
        self.run_pass()
        w.disable_runtime(self.home, "codex", "a test reason", self.t0)
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("wake: on; unattended launch: on", out)
        self.assertIn("codex launch disabled: a test reason", out)
        self.assertIn(f"{SID[:8]} #5 RESUMING", out)
        self.assertIn(f"{SID2[:8]} #6 NOTIFIED", out)
        self.assertIn("candidate sessions:", out)
        self.assertIn("danger-full-access", out)
        for path in (self.home / ".cardinal" / "wake").rglob("*"):
            if path.is_file() and path.suffix == ".json":
                self.assertNotIn(SECRET, path.read_text())
        for blob in [out] + [m for _, m in self.world.notes] + self.world.lines:
            self.assertNotIn("SECRET", blob)

    def test_run_dry_run(self):
        self.world.events = [decision(5)]
        code, out, _ = self.cli("run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would launch", out)
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
