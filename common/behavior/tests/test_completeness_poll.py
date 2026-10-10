import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from compilation.qualified_host import QualifiedHost
from compilation.native_completeness import NativePopulationLoader
from compilation.completeness import verify_completeness
from test_completeness import fixture,native_fixture


class PollTests(unittest.TestCase):
    def host(self, fail=False):
        registry=Mock()
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        registry.root=Path(directory.name)
        registry.inspect.return_value={'agent_id':'agent_alpha'}
        registry.execute.return_value={'status':'EXECUTED','verdict':'NON_MATCH'}
        host=QualifiedHost(None,registry,None,lambda *_:None)
        host.completeness_store=SimpleNamespace(enumerate_pending=lambda:['bad','good'],
            load_source=lambda sid:dict(agent_id='agent_alpha',execution_id=sid,
                revision='initial-revision',generation=1,closure=None,loss={'gaps':['source_not_sealed']}),
            load_contract=Mock(return_value={'scope_id':'opaque-invocation'}),
            publish_result=Mock(return_value=True))
        def load(sid,source):
            if sid=='bad':raise OSError('transport unavailable')
            return dict(trace={'trace_id':'original'},transport={'original':'native receipt'})
        host.completeness_admission=SimpleNamespace(load_retained=load)
        return host

    def test_transport_failure_does_not_starve_qualified_execution(self):
        host=self.host()
        result=host.poll_completeness({'agent_alpha':'qualified-private-id'})
        self.assertEqual(result[0]['verdict'],'UNKNOWN')
        self.assertFalse(result[0]['sdk_executed'])
        self.assertFalse(result[0]['coverage_current'])
        from compilation.storage import digest
        durable=json.loads((host.registry.root/'unreconciled'/(digest('bad')+'.json')).read_text())
        self.assertEqual(durable['verdict'],'UNKNOWN')
        self.assertFalse(durable['sdk_executed'])
        host.completeness_store.publish_result.assert_called_once()
        sid,identity,row=host.completeness_store.publish_result.call_args.args
        self.assertEqual(sid,'bad')
        self.assertEqual(identity['source_sha256'],digest(host.completeness_store.load_source('bad')))
        self.assertEqual(identity['contract_sha256'],digest({'scope_id':'opaque-invocation'}))
        self.assertFalse(row['sdk_executed'])
        self.assertEqual(result[1]['status'],'EXECUTED')
        host.registry.execute.assert_called_once()
        qid,population=host.registry.execute.call_args.args
        self.assertEqual(qid,'qualified-private-id')
        self.assertFalse(population['synthetic'])
        self.assertEqual(population['selection']['execution_ids'],['good'])
        self.assertEqual(population['traces'],[{'trace_id':'original'}])

    def test_malformed_initial_source_persists_unknown_and_continues(self):
        host=self.host()
        def source(sid):
            if sid=='bad':raise ValueError('trusted completeness record must be an object')
            return dict(agent_id='agent_alpha',execution_id=sid)
        host.completeness_store.load_source=source
        rows=host.poll_completeness({'agent_alpha':'qid'})
        self.assertEqual(rows[0]['verdict'],'UNKNOWN')
        self.assertFalse(rows[0]['sdk_executed'])
        from compilation.storage import digest
        self.assertTrue((host.registry.root/'unreconciled'/(digest('bad')+'.json')).is_file())
        host.completeness_store.publish_result.assert_not_called()
        host.registry.execute.assert_called_once()

    def test_mismatched_private_enrollment_never_executes(self):
        host=self.host();host.registry.inspect.return_value={'agent_id':'different_agent'}
        self.assertTrue(all(row['verdict']=='UNKNOWN' for row in host.poll_completeness({'agent_alpha':'qid'})))
        host.registry.execute.assert_not_called()

    def test_existing_serve_schedules_only_operator_enrolled_ids(self):
        from compilation import cli
        with tempfile.TemporaryDirectory() as directory:
            host=Mock();host.registry.root=Path(directory)
            config={'http_token_env':'TEST_COMPLETENESS_TOKEN','execution_completeness':
                {'qualified_ids':{'agent_alpha':'private-qualified-id'},'poll_seconds':7}}
            with patch.object(cli,'read',return_value=config),patch.object(cli,'build',return_value=host),\
                 patch.object(cli,'HTTPServer') as server,patch.object(cli.threading,'Thread') as thread,\
                 patch.dict(os.environ,{'TEST_COMPLETENESS_TOKEN':'local-test'}),\
                 patch.object(sys,'argv',['host','--config','operator.json','serve']):
                cli.main()
                thread.assert_called_once()
                self.assertTrue(thread.call_args.kwargs['daemon'])
                server.return_value.serve_forever.assert_called_once()
                target=thread.call_args.kwargs['target']
                class StopPoll(BaseException):pass
                with patch.object(cli.threading,'Event') as event:
                    event.return_value.wait.side_effect=StopPoll
                    with self.assertRaises(StopPoll):target()
                    event.return_value.wait.assert_called_once_with(7)
                host.poll_completeness.assert_called_once_with({'agent_alpha':'private-qualified-id'})

    def test_shared_trace_external_boundary_for_two_agents(self):
        try:import behavior_sdk.native_profile
        except ImportError:self.skipTest('installed SDK required')
        from compilation.storage import digest
        for agent,value in (('agent_alpha','blue'),('agent_beta','17')):
            c,s,n,t=fixture(agent)
            wrapper_id='4'*16
            c['root_parent_policy']='external_boundary'
            t['events'][0]['parent_id']=wrapper_id
            s['ended'][0]['parentSpanId']=wrapper_id
            s['started'][0]['parent_span_id']=wrapper_id
            t['events'][1]['output']='opaque observed '+value
            import hashlib
            s['ended'][1]['outputSha256']=hashlib.sha256(t['events'][1]['output'].encode()).hexdigest()
            s['closure']['inventory_sha256']=digest({'started':s['started'],'ended':s['ended']})
            n,t,native=native_fixture(c,s,n,t)
            wrapper=copy.deepcopy(native['rows'][0])
            wrapper['columns']=[col for col in wrapper['columns'] if not col['name'].startswith('cardinal_execution_')]
            for col in wrapper['columns']:
                if col['name']=='id':col['valueHex']=wrapper_id.encode().hex()
                if col['name']=='parent_span_id':col['valueHex']=None
            native['rows'].insert(0,wrapper)
            for position,row in enumerate(native['rows']):
                row.update(segment='immutable-segment',partition=7,row=position)
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);payload=root/'payload.json';payload.write_text(json.dumps(native))
                script=root/'fixture_cli.py'
                script.write_text("""import json,sys
from pathlib import Path
args=sys.argv;out=Path(args[args.index('--output')+1]);(out/'traces').mkdir(parents=True)
native=json.loads(Path(args[1]).read_text());tid=native['traceId']
(out/'traces'/(tid+'.json')).write_text(json.dumps(native))
(out/'trace_ids.json').write_text(json.dumps([tid]))
(out/'segment_set.json').write_text(json.dumps({'organization_id':args[args.index('--org')+1],'start_ms':0,'end_ms':1}))
""")
                loader=NativePopulationLoader([sys.executable,str(script),str(payload)],root/'archive')
                actual=loader(s['execution_id'],s)
                self.assertEqual([e['id'] for e in actual['trace']['events']],[e['id'] for e in t['events']])
                self.assertEqual(verify_completeness(c,s,actual,actual['trace'])['status'],'VERIFIED')
                self.assertEqual(actual['transport']['native_read_sha256'],digest(native))
                self.assertEqual(actual['transport']['selection']['selected_rows'],[
                    dict(segment='immutable-segment',partition=7,row=1),
                    dict(segment='immutable-segment',partition=7,row=2)])
                # The selector cannot fill a missing scoped occurrence merely
                # because unrelated complete wrapper rows are retained.
                native['rows'].pop()
                payload.write_text(json.dumps(native))
                missing=loader(s['execution_id'],s)
                self.assertEqual(verify_completeness(c,s,missing,missing['trace'])['status'],'UNVERIFIED')

    def test_native_timeout_is_normalized_for_host_isolation(self):
        import subprocess
        c,s,n,t=fixture()
        with tempfile.TemporaryDirectory() as directory:
            loader=NativePopulationLoader(['fixture-command'],directory,timeout=1)
            with patch('compilation.native_completeness.subprocess.run',
                       side_effect=subprocess.TimeoutExpired('fixture-command',1)):
                with self.assertRaises(OSError):loader(s['execution_id'],s)

    def test_fake_cli_retains_original_cells_and_rejects_detached_execution(self):
        try:import behavior_sdk.native_profile
        except ImportError:self.skipTest('installed SDK required')
        c,s,n,t=fixture();n,t,native=native_fixture(c,s,n,t)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);payload=root/'payload.json';payload.write_text(json.dumps(native))
            script=root/'fixture_cli.py'
            script.write_text('''import json,sys
from pathlib import Path
args=sys.argv;out=Path(args[args.index('--output')+1]);(out/'traces').mkdir(parents=True)
native=json.loads(Path(args[1]).read_text());tid=native['traceId']
(out/'traces'/ (tid+'.json')).write_text(json.dumps(native))
(out/'trace_ids.json').write_text(json.dumps([tid]))
(out/'segment_set.json').write_text(json.dumps({'organization_id':args[args.index('--org')+1],'start_ms':0,'end_ms':1}))
''')
            loader=NativePopulationLoader([sys.executable,str(script),str(payload)],root/'archive')
            actual=loader(s['execution_id'],s)
            self.assertEqual(actual['trace'],t)
            self.assertEqual(verify_completeness(c,s,actual,t)['status'],'VERIFIED')
            # A retained detached trace carrying the same execution ID expands
            # the census; complete bounded single-trace coverage must reject it.
            script.write_text(script.read_text()+'''other=json.loads(json.dumps(native));other_id='9'*32;other['traceId']=other_id
for row in other['rows']:
 for col in row['columns']:
  if col['name']=='trace_id':col['valueHex']=other_id.encode().hex()
(out/'traces'/(other_id+'.json')).write_text(json.dumps(other))
(out/'trace_ids.json').write_text(json.dumps([tid,other_id]))
''')
            actual=loader(s['execution_id'],s)
            self.assertEqual(len(actual['census']['trace_ids']),2)
            self.assertEqual(verify_completeness(c,s,actual,actual['trace'])['status'],'UNVERIFIED')

if __name__=='__main__':unittest.main()
