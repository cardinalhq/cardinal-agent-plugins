import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importlib.util
import tempfile
import unittest
from pathlib import Path
from compilation.engine import Policy, Qualifier, freeze, gate, check_cases, cost_summary
from compilation.storage import read, write, digest, Journal
from compilation.runtime import execute, load_frozen, requalification_reasons, IDENTITY_KEYS


def cases(prefix):
    return [{'id':prefix+str(i),'family':prefix+str(i),'expected':label,
             'trace':{'trace_id':prefix+str(i),'signal':label,'episode':prefix},'review_pass':True}
            for i,label in enumerate(('MATCH','NON_MATCH','UNKNOWN'))]


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source.py';self.source.write_text('original')
        self.initial={'source':str(self.source),'version':'initial'}

    def tearDown(self):self.temp.cleanup()

    def controller(self, evaluate, repair, audit=lambda:cases('audit'), max_repairs=2):
        return Qualifier(self.root/'q',Policy(max_repairs=max_repairs,audit_per_label=1),
                         evaluate,lambda c,r:cases('challenge'+str(r)),repair,audit)

    def test_repair_changes_frozen_program_not_verdict(self):
        calls=[]
        def evaluate(candidate,trace):
            self.assertNotIn('expected',trace)
            calls.append(candidate['version'])
            return {'actual':trace['signal'] if candidate['version']=='fixed' else 'NON_MATCH',
                    'mechanical_pass':True}
        def repair(candidate,feedback,revision):
            self.assertTrue(any(x['case']['expected']=='MATCH' for x in feedback))
            self.assertTrue(any(x['case']['expected']=='UNKNOWN' for x in feedback))
            self.source.write_text('fixed')
            return {**candidate,'version':'fixed'}
        result=self.controller(evaluate,repair).run(self.initial,cases('discovery'))
        self.assertEqual(result['status'],'QUALIFIED');self.assertEqual(result['repairs'],1)
        self.assertEqual(read(self.root/'q/qualification.json')['version'],'fixed')

    def test_audit_failure_terminal_no_feedback_repair(self):
        repairs=[]
        def evaluate(c,t):return {'actual':'MATCH' if t['trace_id'].startswith('audit') else t['signal'],
                                  'mechanical_pass':True}
        result=self.controller(evaluate,lambda *a:repairs.append(a)).run(self.initial,cases('dev'))
        self.assertEqual(result['status'],'CANNOT_QUALIFY');self.assertEqual(repairs,[])

    def test_budget_exhaustion(self):
        repairs=[]
        def repair(c,f,r):repairs.append(r);return c
        q=self.controller(lambda c,t:{'actual':'UNKNOWN','mechanical_pass':True},repair,max_repairs=1)
        result=q.run(self.initial,cases('dev'))
        self.assertEqual(result['reason'],'repair_budget_exhausted');self.assertEqual(repairs,[1])

    def test_compile_failure_uses_remaining_budget_and_feedback(self):
        from compilation.engine import RepairFailure
        attempts=[]
        def repair(candidate,feedback,revision):
            attempts.append(revision)
            if revision==1:raise RepairFailure({'errors':['invalid source quote']})
            self.assertIn('compilation_failure',feedback[-1])
            return {**candidate,'version':'fixed'}
        def evaluate(c,t):return {'actual':t['signal'] if c['version']=='fixed' else 'NON_MATCH','mechanical_pass':True}
        result=self.controller(evaluate,repair).run(self.initial,cases('dev'))
        self.assertEqual(attempts,[1,2]);self.assertEqual(result['status'],'QUALIFIED')

    def test_compile_failure_cannot_reset_budget(self):
        from compilation.engine import RepairFailure
        def repair(*args):raise RepairFailure({'errors':['always invalid']})
        result=self.controller(lambda c,t:{'actual':'ERROR','mechanical_pass':False},repair).run(self.initial,cases('dev'))
        self.assertEqual(result['reason'],'repair_compile_budget_exhausted');self.assertEqual(result['repairs'],2)

    def test_unknown_is_explicitly_gated(self):
        rows=[{'id':str(i),'expected':g,'actual':'MATCH' if g=='UNKNOWN' else g,'mechanical_pass':True}
              for i,g in enumerate(('MATCH','NON_MATCH','UNKNOWN'))]
        self.assertFalse(gate(rows)['passed'])
        self.assertFalse(gate(rows[:2])['passed'])

    def test_mechanical_error_cannot_pass(self):
        rows=[{'id':c['id'],'expected':c['expected'],'actual':c['expected'],'mechanical_pass':False}
              for c in cases('d')]
        self.assertFalse(gate(rows)['passed'])

    def test_review_and_overlap_fail_closed(self):
        c=cases('x');ids,hashes=check_cases(c)
        with self.assertRaises(ValueError):check_cases(c,ids,hashes)
        c[0]['review_pass']=False
        with self.assertRaises(ValueError):check_cases(c)

    def test_renaming_trace_ids_does_not_bypass_overlap(self):
        from compilation.engine import case_fingerprint
        import copy
        original=cases('x')[0];renamed=copy.deepcopy(original)
        renamed['id']='renamed';renamed['trace']['trace_id']='renamed'
        self.assertEqual(case_fingerprint(original),case_fingerprint(renamed))
        with self.assertRaises(ValueError):check_cases([original,renamed])

    def test_atomic_reservation_survives_exception(self):
        journal=Journal(self.root/'journal')
        with self.assertRaises(ZeroDivisionError):journal.call(['one'],{},lambda:1/0)
        with self.assertRaises(FileExistsError):journal.call(['one'],{},lambda:{})

    def make_bundle(self):
        qualification=self.root/'qualification.json'
        write(qualification,{'passed':True,'version':'initial'})
        identity={k:digest(k) for k in IDENTITY_KEYS if k!='program'}
        cert=freeze(self.root/'bundle',{'status':'QUALIFIED','candidate':self.initial},identity,qualification,{})
        return cert

    def test_production_executes_sdk_once(self):
        cert=self.make_bundle();calls=[]
        result=execute(self.root/'bundle',cert['identity'],{'trace_id':'t'},
                       lambda v,s,t:calls.append((v,s,t)) or {'verdict':'UNKNOWN'})
        self.assertEqual(result,{'verdict':'UNKNOWN'});self.assertEqual(len(calls),1)

    def test_all_identity_changes_require_requalification(self):
        cert=self.make_bundle()
        for key in IDENTITY_KEYS:
            observed={**cert['identity'],key:'changed'}
            self.assertEqual(requalification_reasons(cert,observed),[key])
            with self.assertRaises(ValueError):load_frozen(self.root/'bundle',observed)

    def test_frozen_source_tampering_rejected(self):
        cert=self.make_bundle();(self.root/'bundle/program.py').write_text('tampered')
        with self.assertRaises(ValueError):load_frozen(self.root/'bundle',cert['identity'])

    def test_certificate_tampering_rejected(self):
        cert=self.make_bundle();cert['version']='other'
        import json
        (self.root/'bundle/certificate.json').write_text(json.dumps(cert))
        with self.assertRaises(ValueError):load_frozen(self.root/'bundle',cert['identity'])

    def test_unqualified_not_exported(self):
        with self.assertRaises(ValueError):freeze(self.root/'bundle',{'status':'CANNOT_QUALIFY'},{},None,{})

    def test_cost_unknowns_and_amortization(self):
        r={'input_tokens':100,'output_tokens':10,'latency_ms':50,'provider_attempts':1,'cost_usd':None}
        result=cost_summary([r],[r],100)
        self.assertIsNone(result['amortized_total_usd_per_execution'])
        self.assertEqual(result['qualification_amortized_per_execution']['input_tokens'],1)
        priced={**r,'cost_usd':2}
        self.assertEqual(cost_summary([priced],[priced],100)['amortized_total_usd_per_execution'],2.02)
        with self.assertRaises(ValueError):cost_summary([r],[r],0)

    def test_runtime_has_no_offline_llm_dependency(self):
        import ast
        source=Path(__file__).resolve().parents[1]/'compilation/runtime.py'
        imports=[x.module for x in ast.walk(ast.parse(source.read_text())) if isinstance(x,ast.ImportFrom)]
        self.assertEqual(set(imports),{'storage','pathlib'})

    def test_requalification_preserves_parent_and_records_drift(self):
        from compilation.lifecycle import requalify
        cert=self.make_bundle()
        before=(self.root/'bundle/certificate.json').read_bytes()
        observed={**cert['identity'],'agent':digest('new-agent')}
        result=requalify(self.root/'new-cycle',self.root/'bundle',observed,'agent implementation changed',
                          self.initial,cases('new-dev'),Policy(max_repairs=0,audit_per_label=1),
                          lambda c,t:{'actual':t['signal'],'mechanical_pass':True},
                          lambda c,r:cases('new-challenge'),lambda *a:None,lambda:cases('new-audit'),lambda c:{})
        self.assertEqual(result['status'],'QUALIFIED')
        self.assertEqual((self.root/'bundle/certificate.json').read_bytes(),before)
        request=read(self.root/'new-cycle/request.json')
        self.assertEqual(request['changed_fields'],['agent'])
        current=read(self.root/'new-cycle/bundle/certificate.json')
        self.assertEqual(current['identity']['agent'],observed['agent'])
        load_frozen(self.root/'new-cycle/bundle',observed)

    def test_sampling_collects_only_evidence(self):
        from compilation.lifecycle import collect_sample
        key=collect_sample(self.root/'queue',{'trace_id':'t'},'certificate','sampled')
        value=read(self.root/'queue'/(key+'.json'))
        self.assertEqual(value['trace'],{'trace_id':'t'})
        self.assertNotIn('verdict',value)
        with self.assertRaises(FileExistsError):collect_sample(self.root/'queue',{'trace_id':'t'},'certificate','sampled')

    def test_identities_hash_actual_dependency_contents(self):
        from compilation.identity import identity_from_files
        dependency=self.root/'agent.py';dependency.write_text('version one')
        manifest={'agent.py':dependency}
        first=identity_from_files(self.source,manifest,manifest,manifest,manifest,{'model':'jev'})
        dependency.write_text('version two')
        second=identity_from_files(self.source,manifest,manifest,manifest,manifest,{'model':'jev'})
        self.assertNotEqual(first['agent'],second['agent'])
        self.assertEqual(first['program'],second['program'])


if __name__=='__main__':unittest.main()
