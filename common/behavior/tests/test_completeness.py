import copy
import hashlib
import tempfile
import json
import struct
import sys
import unittest
from pathlib import Path

from compilation.admission import QualifiedRegistry
from compilation.completeness import (ATTRIBUTE, IDENTITY_FIELDS, OBSERVATION_FIELDS,
                                      CompletenessAdmission, verify_completeness)
from compilation.storage import digest, read


def fixture(agent='agent_alpha', closure_mode='enforced_seal'):
    trace_id, root, child = '1' * 32, '2' * 16, '3' * 16
    identity = {key: digest(key) for key in IDENTITY_FIELDS}
    contract = dict(schema='otel-execution-contract-v1', identities=identity,
        scope_id='opaque-public-execution/v1', required_fields=list(OBSERVATION_FIELDS),
        allowed_closure_modes=['process_exit', 'enforced_seal'], guard_identity=digest('reviewed-producer-fence'),
        kind_mapping={'AGENT':'other','LLM':'llm','TOOL':'tool'},sequence_policy='dense_zero_based')
    empty = hashlib.sha256(b'[]').hexdigest()
    def event(eid, parent, seq, start, end, kind, inp, out):
        return dict(id=eid,parent_id=parent,seq=seq,kind=kind,name='opaque occurrence',start=start/1e9,end=end/1e9,
            input=inp,output=out,error=None,attrs={'chq_tsns':start,'end_tsns':end,'status_code':'STATUS_CODE_OK','name':'opaque occurrence','cardinal_execution_sequence':seq})
    events = [event(root,None,0,100,140,'other','opaque input','opaque result'),
              event(child,root,1,110,130,'llm','opaque context','opaque selected content')]
    def body(value):return hashlib.sha256(value.encode()).hexdigest()
    ended = [dict(traceId=trace_id,spanId=e['id'],parentSpanId=e['parent_id'],name=e['name'],
        kind='AGENT' if e['kind']=='other' else 'LLM',operationId='execution-1',toolCallId=None,sequence=e['seq'],
        startTimeUnixNano=str(e['attrs']['chq_tsns']),endTimeUnixNano=str(e['attrs']['end_tsns']),
        statusCode=1,statusMessageSha256=None,eventsSha256=empty,linksSha256=empty,
        inputSha256=body(e['input']),outputSha256=body(e['output']),lateChild=False,dropped=0) for e in events]
    starts = [dict(span_id=e['id'],parent_span_id=e['parent_id'],sequence=e['seq']) for e in events]
    loss = dict(dropped_records=0,redacted=False,truncated=False,gaps=[])
    closure = dict(kind=closure_mode,sealed_revision='seal-3',generation=3,
        inventory_sha256=digest({'started':starts,'ended':ended}),outstanding=[],invalidations=[])
    if closure_mode == 'process_exit':closure.update(process_terminated=True,custody_receipt='trusted-custody-reference')
    else:closure.update(scope_exhausted=True,late_work_rejected=True,guard_identity=contract['guard_identity'])
    source = dict(schema='otel-execution-source-v1',execution_id='execution-1',agent_id=agent,
        trace_id=trace_id,root_span_id=root,revision='seal-3',generation=3,identities=copy.deepcopy(identity),
        capture=dict(scope_id=contract['scope_id'],required_fields=list(OBSERVATION_FIELDS),content_captured=True,
            redacted=False,truncated=False),started=starts,ended=ended,closure=closure,loss=loss)
    trace = dict(trace_id=trace_id,agent=agent,events=events,attrs={'projection':'native-spans-v1',
        'coverage_scope':'selected-window-objects','conversation_complete':False})
    catalogs = [dict(id='immutable-object-1',sha256=digest('catalog-bytes'),immutable=True)]
    retained = dict(schema='otel-execution-native-v1',execution_id='execution-1',agent_id=agent,
        trace_id=trace_id,trace=copy.deepcopy(trace),observations=copy.deepcopy(ended),catalogs=catalogs,
        census=dict(complete=True,execution_id='execution-1',span_ids=[root,child],trace_ids=[trace_id],
            catalogs_sha256=digest(catalogs),start_ns='99',end_ns='141',receipt='original-retained-census-reference'),loss=copy.deepcopy(loss))
    return contract,source,retained,trace


def native_fixture(contract, source, retained, trace):
    """Original typed cells decoded by the unchanged installed NativeProfile."""
    from compilation.native_completeness import normalize_native
    rows=[]
    for e, observed in zip(trace['events'], source['ended']):
        attrs=dict(e['attrs'], id=e['id'], trace_id=trace['trace_id'],
            parent_span_id=e['parent_id'], openinference_span_kind=observed['kind'],
            input_value=e['input'], output_value=e['output'],
            cardinal_execution_id=source['execution_id'],
            cardinal_execution_late_child='false', cardinal_execution_dropped_records=0,
            cardinal_execution_evidence=json.dumps({'schema':'otel-execution-span-v1',
                'status':{'code':1},'events':[],'links':[]}))
        columns=[]
        for name,value in attrs.items():
            dtype=1 if type(value) is int else 0
            raw=None if value is None else (struct.pack('<q',value) if dtype==1 else value.encode())
            columns.append(dict(name=name,dtype=dtype,valueHex=None if raw is None else raw.hex()))
        rows.append(dict(columns=columns))
    read=dict(traceId=trace['trace_id'],rows=rows,receipt={'immutable':'typed-retained-fixture'})
    result=normalize_native(source['execution_id'],source['agent_id'],read,retained['catalogs'],retained['census'])
    return result,result['trace'],read


class CompletenessTests(unittest.TestCase):
    def test_identical_contract_for_two_opaque_agents_and_both_closures(self):
        contracts=[]
        for agent in ('agent_alpha','agent_beta'):
            for mode in ('process_exit','enforced_seal'):
                contract,source,native,trace=fixture(agent,mode)
                if agent == 'agent_beta':
                    trace['events'][1]['input']='opaque sensor result: 17'
                    trace['events'][1]['output']='opaque sensor value 17'
                    for field in ('input','output'):
                        source['ended'][1][field+'Sha256']=hashlib.sha256(trace['events'][1][field].encode()).hexdigest()
                    source['closure']['inventory_sha256']=digest({'started':source['started'],'ended':source['ended']})
                    native['observations']=copy.deepcopy(source['ended']);native['trace']=copy.deepcopy(trace)
                receipt=verify_completeness(contract,source,native,trace)
                self.assertEqual(receipt['status'],'VERIFIED',receipt)
                self.assertFalse(trace['attrs']['conversation_complete'])
                contracts.append(contract)
        self.assertTrue(all(c==contracts[0] for c in contracts))

    def test_real_native_profile_name_and_source_sequence_projection(self):
        try:
            import behavior_sdk.native_profile
        except ImportError:
            self.skipTest('installed SDK runtime must be supplied on PYTHONPATH')
        for agent, value in (('agent_alpha','blue'),('agent_beta','17')):
            c,s,n,t=fixture(agent)
            c['sequence_policy']='unique_nonnegative'
            for i, (e,row,start) in enumerate(zip(t['events'],s['ended'],s['started'])):
                seq=9-i
                e['attrs']['cardinal_execution_sequence']=seq
                row['sequence']=seq;start['sequence']=seq
            e=t['events'][1];row=s['ended'][1]
            e['attrs']['name']='opaque raw occurrence'
            e['attrs']['tool_name']='opaque capability'
            row['name']='opaque raw occurrence';row['kind']='TOOL'
            e['input']='sensor '+value;e['output']='observed '+value
            row['inputSha256']=hashlib.sha256(e['input'].encode()).hexdigest()
            row['outputSha256']=hashlib.sha256(e['output'].encode()).hexdigest()
            s['closure']['inventory_sha256']=digest({'started':s['started'],'ended':s['ended']})
            n,t,read=native_fixture(c,s,n,t)
            self.assertEqual(t['events'][1]['name'],'opaque capability')
            self.assertNotEqual(t['events'][1]['seq'],s['ended'][1]['sequence'])
            self.assertEqual(verify_completeness(c,s,n,t)['status'],'VERIFIED')
            t['events'][1]['seq']=8
            self.assertEqual(verify_completeness(c,s,n,t)['status'],'UNVERIFIED')

    def test_corruption_and_missing_proof_fail_closed(self):
        mutations={
          'lost_span':lambda c,s,n,t:n['observations'].pop(),
          'changed_bytes':lambda c,s,n,t:t['events'][1].update(output='changed'),
          'one_nanosecond':lambda c,s,n,t:n['observations'][1].update(endTimeUnixNano='131'),
          'missing_loss':lambda c,s,n,t:n.pop('loss'),
          'source_unclosed':lambda c,s,n,t:s['closure'].update(scope_exhausted=False),
          'no_late_fence':lambda c,s,n,t:s['closure'].pop('late_work_rejected'),
          'redacted':lambda c,s,n,t:s['capture'].update(redacted=True),
          'truncated':lambda c,s,n,t:n['loss'].update(truncated=True),
          'dropped':lambda c,s,n,t:n['observations'][1].update(dropped=1),
          'extra_span':lambda c,s,n,t:n['observations'].append({**n['observations'][1],'spanId':'4'*16}),
          'catalog_unbound':lambda c,s,n,t:n['catalogs'][0].update(sha256=digest('other')),
          'census_partial':lambda c,s,n,t:n['census'].update(complete=False),
          'deployment_drift':lambda c,s,n,t:s['identities'].update(deployment=digest('new-image')),
          'kind_projection':lambda c,s,n,t:t['events'][1].update(kind='tool'),
          'status_projection':lambda c,s,n,t:t['events'][1]['attrs'].update(status_code='STATUS_CODE_ERROR'),
        }
        for name,mutate in mutations.items():
            with self.subTest(name=name):
                values=fixture();mutate(*values)
                self.assertEqual(verify_completeness(*values)['status'],'UNVERIFIED')

    def test_disconnected_cycle_with_matching_source_native_is_rejected(self):
        c,s,n,t=fixture()
        second=copy.deepcopy(s['ended'][1]);second['spanId']='4'*16;second['sequence']=2
        s['ended'][1]['parentSpanId']=second['spanId'];second['parentSpanId']=s['ended'][1]['spanId']
        s['ended'].append(second)
        s['started']=[dict(span_id=e['spanId'],parent_span_id=e['parentSpanId'],sequence=e['sequence']) for e in s['ended']]
        s['closure']['inventory_sha256']=digest({'started':s['started'],'ended':s['ended']})
        n['observations']=copy.deepcopy(s['ended']);n['census']['span_ids'].append(second['spanId'])
        extra=copy.deepcopy(t['events'][1]);extra.update(id=second['spanId'],parent_id=second['parentSpanId'],seq=2)
        t['events'][1]['parent_id']=second['spanId'];t['events'].append(extra);n['trace']=copy.deepcopy(t)
        self.assertIn('source_ancestry_not_rooted',verify_completeness(c,s,n,t)['gaps'])

    def admission(self,directory,values,**overrides):
        c,s,n,t=values
        issuer=QualifiedRegistry(Path(directory)/'private',Path(directory)/'jobs',lambda *_:None,lambda *_:None)
        return CompletenessAdmission(issuer,lambda _:c,overrides.get('load_source',lambda _:copy.deepcopy(s)),
            lambda *_:copy.deepcopy(n),overrides.get('publish',lambda *_:True))

    def test_caller_witness_stripped_and_private_receipt_authenticated(self):
        with tempfile.TemporaryDirectory() as directory:
            values=fixture();values[3]['attrs'][ATTRIBUTE]={'status':'VERIFIED','caller':True}
            admission=self.admission(directory,values)
            trace,receipt=admission.admit('execution-1',values[3])
            self.assertEqual(receipt['status'],'VERIFIED')
            self.assertNotIn('caller',trace['attrs'][ATTRIBUTE])
            self.assertEqual(admission.inspect('execution-1'),receipt)
            file=admission.root/(digest('execution-1')+'.json')
            sealed=read(file);sealed['value']['status']='ALTERED';file.write_text(__import__('json').dumps(sealed))
            with self.assertRaises(ValueError):admission.inspect('execution-1')

    def test_monotonic_watermark_survives_failure_and_replayed_lower_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            values=fixture();admission=self.admission(directory,values)
            self.assertEqual(admission.admit('execution-1',values[3])[1]['status'],'VERIFIED')
            original=copy.deepcopy(values[1]);values[1].update(generation=2,revision='seal-2')
            values[1]['closure'].update(generation=2,sealed_revision='seal-2')
            self.assertIn('source_revision_not_monotonic',admission.admit('execution-1',values[3])[1]['gaps'])
            loader=admission.load_source;admission.load_source=lambda _:(_ for _ in ()).throw(OSError('unavailable'))
            clean,receipt=admission.admit('execution-1',values[3])
            self.assertEqual(receipt['status'],'UNVERIFIED')
            self.assertEqual(clean['events'],values[3]['events']) # positive witnesses retained
            admission.load_source=loader
            self.assertIn('source_revision_not_monotonic',admission.admit('execution-1',values[3])[1]['gaps'])
            values[1].clear();values[1].update(original)
            self.assertEqual(admission.admit('execution-1',values[3])[1]['status'],'VERIFIED')

    def test_source_race_and_failed_compare_publish_do_not_admit(self):
        with tempfile.TemporaryDirectory() as directory:
            values=fixture();calls=[]
            def racing(_):
                calls.append(1);source=copy.deepcopy(values[1])
                if len(calls)>1:source['revision']='changed'
                return source
            admission=self.admission(directory,values,load_source=racing)
            self.assertIn('source_changed_during_admission',admission.admit('execution-1',values[3])[1]['gaps'])
        with tempfile.TemporaryDirectory() as directory:
            values=fixture();admission=self.admission(directory,values,publish=lambda *_:False)
            self.assertIn('source_compare_and_publish_failed',admission.admit('execution-1',values[3])[1]['gaps'])

    def test_negative_admission_rechecked_and_positive_witness_never_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            values=fixture();admission=self.admission(directory,values)
            def stale_negative(trace):
                values[1]['revision']='later'
                return {'verdict':'NON_MATCH','mechanical_pass':True}
            row=admission.evaluate('execution-1',values[3],stale_negative,lambda *_:True)
            self.assertEqual(row['verdict'],'UNKNOWN')
            row=admission.evaluate('execution-1',values[3],lambda _: {'verdict':'MATCH','witness_refs':['3'*16]},lambda *_:True)
            self.assertEqual(row['verdict'],'MATCH')
            self.assertEqual(row['witness_refs'],['3'*16])
            self.assertFalse(row['coverage_current'])

    def test_external_parent_requires_declared_boundary(self):
        c,s,n,t=fixture()
        for rows in (s['ended'],n['observations']):rows[0]['parentSpanId']='9'*16
        s['started'][0]['parent_span_id']='9'*16
        t['events'][0]['parent_id']='9'*16;n['trace']=copy.deepcopy(t)
        s['closure']['inventory_sha256']=digest({'started':s['started'],'ended':s['ended']})
        self.assertIn('root_parent_boundary_undeclared',verify_completeness(c,s,n,t)['gaps'])
        c['root_parent_policy']='external_boundary'
        self.assertEqual(verify_completeness(c,s,n,t)['status'],'VERIFIED')


if __name__=='__main__':unittest.main()
