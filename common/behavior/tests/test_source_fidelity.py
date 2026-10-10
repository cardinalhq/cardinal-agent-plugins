"""Protocol/capture regression, with prior corpora used read-only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fidelity import check_case, validate_protocol
from fidelity.anthropic_messages import request_errors
from fidelity.derivation import replay, replay_bundle, verify_derivation, digest, body_hash
from fidelity.campaign import before_gold, before_predictions


WS = Path('/Users/ruchirj/workspace')


def basic():
    use = {'type':'tool_use', 'id':'toolu_a', 'name':'query', 'input':{}}
    messages = [{'role':'user','content':'query'}, {'role':'assistant','content':[use]},
                {'role':'user','content':[{'type':'tool_result','tool_use_id':'toolu_a','content':'value'}]}]
    def event(ident, kind, start, end, inp, out, **attrs):
        return {'id':ident,'kind':kind,'parent_id':None if kind=='other' else 'root',
                'input':inp,'output':out,'error':None,'attrs':{'name':'Anthropic Messages' if kind=='llm' else kind,
                'chq_tsns':start,'end_tsns':end,'status_code':'STATUS_CODE_OK',**attrs}}
    return {'events':[event('root','other',0,9,'query','returned value'),
                      event('a','llm',1,2,json.dumps({'messages':messages[:1]}),json.dumps({'content':[use],'stop_reason':'tool_use'})),
                      event('t','tool',3,4,'{}','value',gen_ai_tool_call_id='toolu_a'),
                      event('b','llm',5,6,json.dumps({'messages':messages}),json.dumps({'content':[{'type':'text','text':'returned value'}],'stop_reason':'end_turn'}))]}


class ProtocolTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(validate_protocol(basic())['pass'])

    def mutate_request(self, fn, expected):
        t = basic(); r = json.loads(t['events'][-1]['input']); fn(r['messages']); t['events'][-1]['input']=json.dumps(r)
        self.assertIn(expected, '\n'.join(validate_protocol(t)['errors']))

    def test_missing_id(self):
        self.mutate_request(lambda m:m[-1]['content'][0].pop('tool_use_id'), 'tool_result missing tool_use_id')

    def test_wrong_predecessor(self):
        self.mutate_request(lambda m:m.insert(2,{'role':'assistant','content':'unrelated'}), 'immediately preceding assistant')

    def test_result_order(self):
        self.mutate_request(lambda m:m[-1]['content'].insert(0,{'type':'text','text':'prefix'}), 'must come first')

    def test_unanswered(self):
        self.mutate_request(lambda m:m[-1].update(content='no result'), 'not answered in next user')

    def test_duplicate_ids(self):
        self.mutate_request(lambda m:m[1]['content'].append(copy.deepcopy(m[1]['content'][0])), 'duplicate tool_use id')

    def test_wrong_call_id(self):
        t=basic();t['events'][2]['attrs']['gen_ai_tool_call_id']='wrong'
        self.assertIn('no earlier emitted tool_use',str(validate_protocol(t)))

    def test_wrong_tool_content(self):
        t=basic();t['events'][2]['output']='changed'
        self.assertIn('differs from tool_result.content',str(validate_protocol(t)))

    def test_tool_start_order(self):
        t=basic();t['events'][2]['attrs']['chq_tsns']=1
        self.assertIn('starts before',str(validate_protocol(t)))

    def test_tool_end_order(self):
        t=basic();t['events'][2]['attrs']['end_tsns']=7
        self.assertIn('ends after',str(validate_protocol(t)))

    def test_successful_invalid_json(self):
        t=basic();t['events'][1]['input']='invalid'
        self.assertFalse(validate_protocol(t)['pass'])

    def test_no_following_user_turn(self):
        t=basic();t['events'].pop()
        self.assertIn('emitted tool_use not answered',str(validate_protocol(t)))

    def test_provider_dispatch(self):
        t=basic();t['events'][1]['attrs'].update(llm_provider='other')
        self.assertIn('unsupported provider',str(validate_protocol(t)))

    def test_original_r12(self):
        root=WS/'behavior-megan-automatic-qualification2-20261010/sealed'
        cases=json.loads((root/'cases.json').read_text());bundles=json.loads((root/'provenance-bundles.json').read_text())
        case=next(c for c in cases if c['id']=='r12')
        self.assertTrue(bundles['r12']['synthetic'])
        result=check_case(case)
        self.assertFalse(result['pass'])
        self.assertIn('tool_result missing tool_use_id', '\n'.join(result['protocol']['errors']))


class DerivationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        base=root/'base.json';base.write_text(json.dumps(basic()));sha=hashlib.sha256(base.read_bytes()).hexdigest()
        receipt=root/'receipt.json';receipt.write_text(json.dumps({'status':'VERIFIED','source_revision':'source',
            'source_to_native_all_observations_equal':True,'source_process_terminated':True,'provider_shutdown_completed':True,
            'artifact_hashes':{'base.json':sha}}))
        rh=hashlib.sha256(receipt.read_bytes()).hexdigest()
        rows=[{'spanId':e['id'],'inputSha256':body_hash(e['input']),'outputSha256':body_hash(e['output'])} for e in basic()['events']]
        loss={'gaps':[],'dropped_records':0,'redacted':False,'truncated':False}
        self.base_bundle={'trace':basic(),'source':{'identities':{'source':'source','recorder':'recorder'},
            'capture':{'content_captured':True,'redacted':False,'truncated':False,'scope_id':'scope'},
            'loss':copy.deepcopy(loss),'started':[],'ended':copy.deepcopy(rows),'closure':{}},
            'retained':{'trace':basic(),'observations':copy.deepcopy(rows),'loss':copy.deepcopy(loss)}}
        bp=root/'bundle.json';bp.write_text(json.dumps(self.base_bundle))
        self.catalog={sha:{'receipt_sha256':rh,'source_identity':'source','recorder_identity':'recorder',
            'bundle_path':str(bp),'bundle_sha256':hashlib.sha256(bp.read_bytes()).hexdigest()}}
        self.case={'id':'unit','derivation':{'base_path':str(base),'base_sha256':sha,'receipt_path':str(receipt),
            'receipt_sha256':rh,'source_identity':'source','recorder_identity':'recorder','transforms':[]}}
        self.apply([])

    def apply(self, transforms):
        self.case['derivation']['transforms']=transforms
        self.case['trace'],self.case['capture']=replay(basic(),transforms)
        self.case['provenance_bundle']=replay_bundle(self.base_bundle,basic(),self.case['trace'],self.case['capture'])

    def test_replay_and_attestation(self):
        self.assertTrue(verify_derivation(self.case,self.catalog)['pass'])

    def test_not_self_enrolled(self):
        self.assertIn('not enrolled',str(verify_derivation(self.case)))

    def test_final_text(self):
        self.apply([{'op':'final_text','text':'a different final claim'}])
        self.assertTrue(check_case(self.case,self.catalog)['pass'])

    def test_drop_span_forces_loss(self):
        self.apply([{'op':'drop_span','span_id':'t'}]);self.assertTrue(check_case(self.case,self.catalog)['pass'])
        self.case['capture']['loss']['gaps']=[]
        self.assertFalse(verify_derivation(self.case,self.catalog)['pass'])

    def test_redaction_forces_flag(self):
        self.apply([{'op':'redact_output','span_id':'root'}]);self.assertTrue(check_case(self.case,self.catalog)['pass'])
        self.case['capture']['redacted']=False;self.assertFalse(verify_derivation(self.case,self.catalog)['pass'])

    def test_truncation_forces_flag(self):
        self.apply([{'op':'truncate_output','span_id':'root','length':1}]);self.assertTrue(check_case(self.case,self.catalog)['pass'])
        self.case['capture']['truncated']=False;self.assertFalse(verify_derivation(self.case,self.catalog)['pass'])

    def test_scope(self):
        self.apply([{'op':'undeclare_scope'}]);self.assertFalse(self.case['capture']['scope_declared'])
        self.assertTrue(check_case(self.case,self.catalog)['pass'])

    def test_unlisted_transform(self):
        with self.assertRaisesRegex(ValueError,'not allowlisted'):
            replay(basic(),[{'op':'remove_protocol_field'}])

    def test_final_text_delivery_duplicate(self):
        base=basic();extra=copy.deepcopy(base['events'][0]);extra['id']='delivery';extra['parent_id']='root';base['events'].append(extra)
        with self.assertRaisesRegex(ValueError,'repeated in another span'):
            replay(base,[{'op':'final_text','text':'changed'}])

    def test_honest_case_dishonest_bundle(self):
        self.apply([{'op':'redact_output','span_id':'root'}])
        self.case['provenance_bundle']['source']['capture']['redacted']=False
        self.case['provenance_bundle']['source']['loss']['redacted']=False
        self.assertIn('bundle differs from replayed',str(check_case(self.case,self.catalog)))

    def test_retained_side_channel(self):
        self.case['provenance_bundle']['retained']['trace']['events'][0]['output']='hidden change'
        self.assertFalse(check_case(self.case,self.catalog)['pass'])

    def test_synthetic_bundle(self):
        self.case['provenance_bundle']['synthetic']=True
        self.assertIn('synthetic admission bundle',str(check_case(self.case,self.catalog)))

    def test_closure_side_channel(self):
        self.case['provenance_bundle']['source']['closure']['scope_exhausted']=True
        self.assertFalse(check_case(self.case,self.catalog)['pass'])

    def test_observation_hash_side_channel(self):
        self.case['provenance_bundle']['source']['ended'][0]['outputSha256']='invented'
        self.assertFalse(check_case(self.case,self.catalog)['pass'])

    def test_unreplayed_edit(self):
        self.case['trace']['events'][0]['output']='extra'
        self.assertIn('differs from replayed',str(verify_derivation(self.case,self.catalog)))

    def test_base_identity(self):
        self.case['derivation']['source_identity']='wrong'
        self.assertIn('source/recorder identity mismatch',str(verify_derivation(self.case,self.catalog)))

    def test_changed_base(self):
        Path(self.case['derivation']['base_path']).write_text('{}')
        self.assertIn('base hash mismatch',str(verify_derivation(self.case,self.catalog)))

    def test_gold_not_called_if_rejected(self):
        calls=[];self.case['capture']['redacted']=True
        with self.assertRaisesRegex(ValueError,'before gold'):
            before_gold(Path(self.tmp.name),[self.case],self.catalog,lambda *_:calls.append(True))
        self.assertEqual(calls,[])

    def test_predictions_recheck_receipts(self):
        root=Path(self.tmp.name);before_gold(root,[self.case],self.catalog,lambda *_:None)
        before_predictions(root,[self.case],self.catalog)
        self.case['capture']['redacted']=True
        with self.assertRaises(ValueError):before_predictions(root,[self.case],self.catalog)

    def test_predictions_refuse_repeat(self):
        root=Path(self.tmp.name);(root/'qualification').mkdir()
        with self.assertRaisesRegex(ValueError,'already attempted'):before_predictions(root,[self.case],self.catalog)


if __name__ == '__main__':
    unittest.main()
