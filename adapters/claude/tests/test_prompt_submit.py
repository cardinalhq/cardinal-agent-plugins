"""Process-level prompt tests: no network wait, merged output, durable queue."""
import json
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_owner_input_hook as owner
from tests import test_storyboard_discovery_hook as discovery

HOOKS = owner.HOOKS
HOOK = HOOKS / 'prompt-submit.py'


class Dispatcher(owner.Base):
    def setUp(self):
        super().setUp()
        self.connect(CARDINAL_STORYBOARD_DISCOVERY='0')
        self.env['CARDINAL_HOOK_DEBUG'] = '0'
        self.env.update(NO_PROXY='*', no_proxy='*')  # the fake server must never use host proxy settings

    def prompt(self, text='hello', wrapper=None, **extra):
        body = {'session_id':owner.SID, 'cwd':str(self.base), 'hook_event_name':'UserPromptSubmit',
                'prompt':text, **extra}
        cmd = [sys.executable,str(HOOK)] if wrapper is None else [sys.executable,'-c',wrapper]
        return subprocess.run(cmd,input=json.dumps(body),capture_output=True,text=True,
                              env=self.env,cwd=self.base,timeout=15)

    def decisions(self):
        path = self.home / '.claude/cardinal/decisions/config.json'
        path.parent.mkdir(parents=True)
        path.write_text('{"enabled":true}')

    def wait_until(self, fn):
        end = time.monotonic()+15
        while time.monotonic()<end:
            try:
                if fn():
                    return
            except (OSError, ValueError):
                pass  # the worker may atomically remove/replace the outbox
            time.sleep(.05)
        self.fail('detached operation did not finish')

    def test_merges_context_and_disclosure(self):
        self.bind(owner_input_disclosed=False)
        self.decisions()
        out=json.loads(self.prompt().stdout)
        self.assertIn('prompts are recorded',out['systemMessage'])
        self.assertIn('Cardinal decision capture is on',out['hookSpecificOutput']['additionalContext'])
        self.assertEqual(out['hookSpecificOutput']['hookEventName'],'UserPromptSubmit')
        self.assertTrue(self.binding()['owner_input_disclosed'])
        self.assertEqual(self.fake.records(),[])

    def test_disclosure_is_marked_only_after_flush(self):
        for fail in (True, False):
            self.bind(owner_input_disclosed=False)
            log=self.base/'writes.log'
            log.unlink(missing_ok=True)
            self.assertEqual(self.checked_stdout(HOOK,{'session_id':owner.SID,'prompt':'hello'},fail),
                             'False True\n')
            self.assertEqual(self.binding()['owner_input_disclosed'],not fail)
        self.assertEqual(self.fake.records(),[])

    def test_slow_post_is_outside_prompt_and_outbox_is_scrubbed(self):
        self.bind()
        entered,release=threading.Event(),threading.Event()
        answer=self.fake.answer
        def held(tool,body):
            if tool=='record-owner-input':
                entered.set()
                release.wait(12)
            return answer(tool,body)
        self.fake.answer=held
        try:
            self.silent(self.prompt('Use cache. Token '+owner.SECRET))
            self.assertTrue(entered.wait(10))
            self.assertFalse(release.is_set())
            self.assertNotIn(owner.SECRET,self.outbox().read_text())
        finally:
            release.set()
        # A sender that timed out while the response was held keeps the
        # prompt for retry; a completed sender may already have drained it.
        self.wait_until(lambda:not self.outbox().exists() or
                        json.loads(self.outbox().read_text()).get('retry_after', 0) > time.time())
        self.assertEqual(self.fake.records()[0]['payload']['turn'],1)

    def test_healthy_sender_drains_the_durable_queue(self):
        self.bind()
        self.silent(self.prompt('Keep the scope small'))
        self.wait_until(lambda:bool(self.fake.records()) and not self.outbox().exists())
        self.assertEqual(self.fake.records()[0]['payload']['text'], 'Keep the scope small')

    def test_failed_spawn_keeps_outbox_for_stop(self):
        self.bind()
        wrapper=(f'import sys,runpy;sys.path.insert(0,{str(HOOKS)!r});'
                 'from cardinal_core import owner_input;owner_input.spawn_flush=lambda *a:False;'
                 f'runpy.run_path({str(HOOK)!r},run_name="__main__")')
        self.silent(self.prompt('Keep this prompt',wrapper))
        self.assertEqual(self.fake.records(),[])
        self.assertEqual(json.loads(self.outbox().read_text())['entries'][0]['payload']['text'],'Keep this prompt')
        self.silent(self.run_stop())
        self.assertEqual(self.fake.records()[0]['payload']['text'],'Keep this prompt')

    def test_machine_prompts_never_load_feature_modules(self):
        self.bind()
        self.decisions()
        log=self.base/'imports.log'
        wrapper=f'''import sys,runpy,builtins
original=builtins.__import__
def guarded(name,*a,**kw):
    if name in ('_otel_settings','cardinal_core.decisions','cardinal_core.owner_input',
                'cardinal_core.owner_relay','cardinal_core.storyboard_authoring') or \\
       (name=='cardinal_core' and any(x in ('decisions','owner_input','owner_relay','storyboard_authoring')
                                     for x in (kw.get('fromlist',a[2] if len(a)>2 else ()) or ()))):
        open({str(log)!r},'a').write(name+'\\n')
        raise ImportError('heavy import')
    return original(name,*a,**kw)
builtins.__import__=guarded
runpy.run_path({str(HOOK)!r},run_name='__main__')
'''
        for text in ('task-notification done','<task-notification>done</task-notification>',
                     'Monitor event: [Monitor expired after 30m with no events delivered.]','⏺ Monitor event: expired'):
            self.silent(self.prompt(text,wrapper))
        self.assertFalse(log.exists())
        self.assertFalse(self.outbox().exists())
        self.assertEqual(self.fake.requests,[])

    def test_slow_local_check_cannot_hold_process_open(self):
        wrapper=(f'import sys,runpy,time;sys.path.insert(0,{str(HOOKS)!r});import _connection;'
                 '_connection.is_connected=lambda:time.sleep(25);'
                 f'runpy.run_path({str(HOOK)!r},run_name="__main__")')
        self.silent(self.prompt(wrapper=wrapper))

    def test_contended_session_lock_cannot_hold_process_open(self):
        self.bind()
        sys.path.insert(0,str(HOOKS))
        from cardinal_core import investigation_events as ie
        with ie.locked(self.home,owner.SID,wait=1) as got:
            self.assertTrue(got)
            self.silent(self.prompt())
        self.assertEqual(self.fake.records(),[])

    def test_stalled_owner_capture_does_not_suppress_decision_context(self):
        self.bind()
        self.decisions()
        wrapper = (f'import sys,runpy,time;sys.path.insert(0,{str(HOOKS)!r});'
                   'from cardinal_core import owner_input;owner_input.capture=lambda *a,**kw:time.sleep(25);'
                   f'runpy.run_path({str(HOOK)!r},run_name="__main__")')
        out = json.loads(self.prompt(wrapper=wrapper).stdout)
        self.assertIn('Cardinal decision capture is on', out['hookSpecificOutput']['additionalContext'])
        self.assertFalse(self.outbox().exists())

    def test_stalled_feature_import_cannot_hold_process_open(self):
        wrapper = f'''import sys,runpy,time,builtins
original=builtins.__import__
def slow(name,*a,**kw):
    if name=='_connection': time.sleep(25)
    return original(name,*a,**kw)
builtins.__import__=slow
runpy.run_path({str(HOOK)!r},run_name='__main__')
'''
        self.silent(self.prompt(wrapper=wrapper))


class IntegratedRelay(owner.OwnerRelay):
    """Run the relay's process/transcript contracts through the dispatcher."""
    def relay_hook(self, prompt, env=None, **extra):
        isolated = dict(env or self.env, CARDINAL_OWNER_INPUT="0", CARDINAL_STORYBOARD_DISCOVERY="0",
                        NO_PROXY="*", no_proxy="*")
        with patch.object(owner, "RELAY_HOOK", HOOK):
            return super().relay_hook(prompt, env=isolated, **extra)


class DeferredDiscovery(discovery._HookCase):
    def state_file(self):
        return self.home / '.claude/cardinal/storyboard-discovery' / (discovery.SESSION + '.json')

    def prompt(self):
        body={'session_id':discovery.SESSION,'cwd':str(self.repo),'hook_event_name':'UserPromptSubmit',
              'prompt':'inspect this branch'}
        # This fixture checks cache/branch delivery, not cold-start latency
        # (Dispatcher above exercises the real process boundary). Preload the
        # feature modules so slow filesystem paging cannot swallow the look.
        wrapper = (f'import sys,runpy;sys.path.insert(0,{str(HOOKS)!r});'
                   'from cardinal_core import storyboard_discovery,storyboard_authoring;'
                   f'runpy.run_path({str(HOOK)!r},run_name="__main__")')
        return subprocess.run([sys.executable,"-c",wrapper],input=json.dumps(body),capture_output=True,
                              text=True,timeout=15,env={"HOME":str(self.home),"PATH":self.path,"NO_PROXY":"*","no_proxy":"*"})

    def refreshed(self, branch='fix/checkout'):
        end = time.monotonic() + 45
        retries = 0
        while time.monotonic() < end:
            state = json.loads(self.state_file().read_text()) if self.state_file().exists() else {}
            running = self.state_file().with_suffix('.refresh.lock').exists()
            if not running and state.get('pending') and state.get('branch') == branch:
                return state
            if not running and retries < 3:
                # A slow host may exhaust the best-effort Git budget. Make
                # its failed attempt retryable without waiting minutes for
                # the production backoff; persistent failures still fail.
                if state.get('failed'):
                    state['at'] = 0
                    self.state_file().write_text(json.dumps(state))
                self.silent(self.prompt())
                retries += 1
            time.sleep(.05)
        self.fail('background discovery did not stage its context after retries')

    def test_stages_then_delivers_once_and_replays_to_subagents(self):
        self.connect()
        self.silent(self.prompt())
        self.refreshed()
        self.silent(self.run_hook('SubagentStart'))
        block=self.context_of(self.prompt(),'UserPromptSubmit')
        self.assertIn('Hit rate fell',block)
        self.assertFalse(json.loads(self.state_file().read_text()).get('pending'))
        count=len(self.fake.requests)
        self.silent(self.prompt())
        self.assertEqual(len(self.fake.requests),count)
        self.assertEqual(self.context_of(self.run_hook('SubagentStart'),'SubagentStart'),block)

    def test_changed_branch_does_not_deliver_old_pending_context(self):
        self.connect()
        self.silent(self.prompt())
        self.refreshed()
        discovery._git(self.repo,'checkout','-q','-b','fix/other')
        self.silent(self.prompt())
        self.assertEqual(self.refreshed('fix/other')['branch'],'fix/other')


class Registration(unittest.TestCase):
    def test_single_optional_launch_has_startup_headroom(self):
        entries=json.loads((HOOKS/'hooks.json').read_text())['hooks']['UserPromptSubmit'][0]['hooks']
        self.assertEqual([e['command'].rsplit('/',1)[-1] for e in entries],
                         ['git-state.py','limits-gate.py','prompt-submit.py'])
        self.assertEqual(entries[-1]['timeout'],15)
        self.assertFalse(entries[-1].get('async'))
