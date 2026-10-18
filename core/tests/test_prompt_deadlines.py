"""Deadline and delivery contracts without a timing-sensitive Git/HTTP fixture."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cardinal_core import storyboard_discovery as sd
from cardinal_core import owner_relay as relay
from cardinal_core.machine_prompts import is_claude_machine_prompt
from tests.test_storyboard_discovery import match


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.conn = {'origin': 'https://test.invalid', 'org': 'o1', 'key': 'test-key'}
        self.cid = sd.connection_id(self.conn)
        self.block = sd.render_block([match()], has_get=True)

    def discover(self, **kw):
        return sd.discover('/unused', conn=self.conn, session_id='s1', state_dir=self.state,
                           event='UserPromptSubmit', **kw)

    def stage(self):
        sd.record_run(self.state, 's1', 'fix/cache', 'a'*40, block=self.block, pending=True, conn_id=self.cid)

    def test_git_cannot_exceed_the_remaining_deadline(self):
        with patch.object(sd.time, 'monotonic', return_value=100), patch.object(sd, 'git', return_value='') as git:
            sd.head_state('/unused', deadline=100.2)
            self.assertAlmostEqual(git.call_args.kwargs['timeout'], .2)
            git.reset_mock()
            self.assertEqual(sd.head_state('/unused', deadline=99), (None, None))
            git.assert_not_called()

    def test_refresh_is_scheduled_without_network_work(self):
        jobs = []
        with patch.object(sd, 'head_state', return_value=('fix/cache','a'*40)), \
                patch.object(sd, '_discover') as network:
            self.assertIsNone(self.discover(schedule=lambda: jobs.append(True)))
            self.assertEqual(jobs, [True])
            network.assert_not_called()

    def test_expired_deadline_records_failure_without_starting_a_worker(self):
        sd.record_run(self.state, 's1', 'fix/cache', 'a'*40, block=self.block, conn_id=self.cid)
        with patch.object(sd, 'head_state', return_value=('fix/cache', 'a'*40)), \
                patch.object(sd.threading, 'Thread') as worker:
            self.assertIsNone(sd.discover('/unused', conn=self.conn, session_id='s1', state_dir=self.state,
                                         event='SessionStart', deadline=time.monotonic()-1))
            worker.assert_not_called()
        state = sd._read_state(self.state, 's1', self.cid)
        self.assertTrue(state['failed'])
        self.assertEqual(state['block'], self.block)

    def test_pending_is_consumed_only_after_successful_delivery(self):
        self.stage()
        callbacks = []
        with patch.object(sd, 'head_state', return_value=('fix/cache','a'*40)), \
                patch.object(sd, '_discover') as network:
            self.assertIsNone(self.discover(stage_only=True))
            self.assertEqual(self.discover(after_write=callbacks.append), self.block)
            self.assertTrue(sd._read_state(self.state,'s1',self.cid)['pending'])
            self.assertEqual(self.discover(after_write=lambda callback: None), self.block)
            callbacks[0]()
            self.assertNotIn('pending', sd._read_state(self.state,'s1',self.cid))
            self.assertIsNone(self.discover())
            network.assert_not_called()

    def test_delivery_callback_never_overwrites_a_newer_refresh(self):
        self.stage()
        callbacks = []
        with patch.object(sd, 'head_state', return_value=('fix/cache','a'*40)):
            self.discover(after_write=callbacks.append)
        sd.record_run(self.state,'s1','fix/new','b'*40,block=self.block,pending=True,conn_id=self.cid)
        callbacks[0]()
        self.assertEqual(sd._read_state(self.state,'s1',self.cid)['branch'],'fix/new')
        self.assertTrue(sd._read_state(self.state,'s1',self.cid)['pending'])


class RelayDelivery(unittest.TestCase):
    def test_unflushed_response_keeps_outcomes_and_commit_does_not_consume_new_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            now = time.time()
            relay.add_target(home,'s1','inv_'+'a'*24,'worker')
            def outcome(pid, text):
                return {'pid':pid,'at':now,'message':text,'context':text,'ex':'decide: '+text,
                        'owner':False,'model':False}
            with relay._updating(home,'s1',now) as store:
                store['outcomes'] = [outcome('p1','Keep cache')]
            callbacks=[]
            owner,model=relay.take_outcomes(home,'s1',now,after_write=callbacks.append)
            self.assertEqual(owner,['Keep cache'])
            self.assertIn('Earlier prompt',model[0])
            self.assertEqual(relay.take_outcomes(home,'s1',now,after_write=lambda fn:None),(owner,model))
            with relay._updating(home,'s1',now) as store:
                store['outcomes'].append(outcome('p2','New decision'))
            callbacks[0]()
            self.assertEqual(relay.take_outcomes(home,'s1',now)[0],['New decision'])


class MachinePrompts(unittest.TestCase):
    def test_recognizes_only_known_envelopes(self):
        for text in ('task-notification done','<task-notification>done',
                     'Monitor event: expired','⏺ Monitor event: expired'):
            self.assertTrue(is_claude_machine_prompt(text))
        for text in ('decide: monitor events only','Explain this Monitor event: error','task-notifications help','hello'):
            self.assertFalse(is_claude_machine_prompt(text))
