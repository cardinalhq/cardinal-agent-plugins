import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from compilation.autonomous import AgentCatalog,AutonomousLifecycle,JobModel
from compilation.engine import Policy
from compilation.storage import digest,read,write
from compilation.application import RetainedTelemetry
from compilation.native_corpus import render,validate_review
from compilation.model_packet import factor

class AutonomousTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        source=self.root/'agent.txt';source.write_text('Only perform an action when the user explicitly authorizes it.')
        self.agents={'agent':{**{k:{'source':str(source)} for k in ['instructions','implementation','tools','telemetry_schema']},'revision':'source-commit','collection':'existing telemetry API'}}
        sample={'traces':[{'trace_id':'raw','events':[]}],'synthetic':False,'selection':{'receipt':'authentic-source'}}
        self.catalog=AgentCatalog(self.agents,lambda *a:sample)
        self.calls=[]
        def call(name,role,instruction,payload):
            self.calls.append((role,payload))
            citation={'ref':'/sources/instructions/source/text','quote':'explicitly authorizes'}
            if role=='agent-discovery':return {'status':'READY','findings':[],'citations':[citation]}
            if role=='source-linked-contract':return {'description':'action evidence','evidence_requirements':['authorization'],'clauses':[{'kind':k,'statement':k,'citations':[citation]} for k in ['MATCH','NON_MATCH','UNKNOWN','candidate_selection','entity_binding','temporal_scope','evidence_requirements']]}
            return {'udf_source':'model generated source','compile_plan':{'schema_version':2}}
        self.model=SimpleNamespace(call=call)
        self.runtime=SimpleNamespace(verify=lambda:None,documentation=lambda:{'canonical':'sdk'},documentation_text=lambda:'exact SDK',context=lambda c,s:json.dumps(c),sdk=lambda *a:SimpleNamespace(compile=lambda *a:{'version':'v'},evaluate=lambda candidate,trace:{'actual':'UNKNOWN','trace_sha256':digest(trace),'mechanical_pass':True,'audit':{'mechanical_status':'PASS'}}),identity=lambda:digest('sdk'),jev_config={'model':'pinned'},dependencies=lambda c:{},usage_rows=lambda *a:[],executions=[])
        self.lifecycle=AutonomousLifecycle(self.root/'jobs',self.catalog,self.model,self.runtime,lambda *a:object())

    def test_two_argument_entry_generates_all_inputs_and_invokes_existing_workflow(self):
        captured={}
        def workflow(root,**kwargs):captured.update(kwargs);return {'status':'CANNOT_QUALIFY','candidate':{'version':'v'},'reason':'audit failed'}
        with patch('compilation.autonomous.compile_and_qualify',workflow):
            result=self.lifecycle.start('agent','Does it act without evidence?')
        self.assertEqual(result['status'],'CANNOT_QUALIFY');self.assertEqual(captured['source'],'model generated source')
        self.assertEqual(captured['policy'],Policy());self.assertEqual([r for r,p in self.calls],['agent-discovery','source-linked-contract','sdk-program-generation'])
        self.assertNotIn('expected',str(self.calls[0][1]))
        self.assertTrue((self.root/'jobs'/result['job_id']/'generated.json').exists())

    def test_diagnosis_receipts_share_job_economics_scope(self):
        names=[];model=JobModel(SimpleNamespace(call=lambda name,*args:names.append(name)),'job')
        model.call('diagnosis-hash','role','instruction',{})
        model.call('job-contract','role','instruction',{})
        self.assertEqual(names,['job-diagnosis-hash','job-contract'])

    def test_interrupted_sdk_usage_is_unknown_not_free(self):
        from compilation.pinned_runtime import PinnedRuntime
        metrics=PinnedRuntime.metrics({'verdict':'ERROR','jev_receipts':[]},15)
        self.assertIsNone(metrics['input_tokens']);self.assertIsNone(metrics['provider_attempts'])

    def test_required_sdk_version_rejects_wrong_runtime_before_model_setup(self):
        from compilation.application import build
        config={'output_dir':str(self.root/'host'),'runtime':'installed','sdk_version':'0.2.0'}
        runtime=SimpleNamespace(documentation=lambda:{'sdk_version':'0.1.1'})
        with patch('compilation.application.PinnedRuntime',return_value=runtime), patch('compilation.application.Model') as model:
            with self.assertRaisesRegex(ValueError,'required behavior-sdk 0.2.0; pinned runtime uses 0.1.1'):
                build(config)
            model.assert_not_called()

    def test_required_sdk_version_accepts_matching_runtime(self):
        from compilation.application import build
        config={'output_dir':str(self.root/'host'),'runtime':'installed','sdk_version':'0.2.0','agents':{}}
        runtime=SimpleNamespace(documentation=lambda:{'sdk_version':'0.2.0'},run_frozen=lambda *a:None)
        with patch('compilation.application.PinnedRuntime',return_value=runtime), patch('compilation.application.Model') as model:
            host=build(config)
            self.assertIs(host.lifecycle.runtime,runtime)
            model.assert_called_once()

    def test_blind_review_allows_source_citations_but_requires_own_trace(self):
        packet={'cases':[{'trace':{'text':'observed'}},{'trace':{'text':'another'}}],
                'sources':{'instructions':'policy'}}
        valid={'citations':[{'ref':'/cases/0/trace/text','quote':'observed'},
                            {'ref':'/sources/instructions','quote':'policy'}]}
        validate_review(valid,packet,0)
        with self.assertRaises(ValueError):validate_review(valid,packet,1)
        with self.assertRaises(ValueError):validate_review({'citations':valid['citations'][1:]},packet,0)
        valid['citations'][1]['quote']='invented'
        with self.assertRaises(ValueError):validate_review(valid,packet,0)

    def test_exact_recovery_reuses_model_authorship_but_not_review(self):
        from compilation.llm import Model
        original=self.root/'models'/'old';original.mkdir(parents=True)
        request={'role':'sdk-program-generation','instruction':'generate','payload':{'source':'real'}}
        response={'udf_source':'model-authored','compile_plan':{}}
        write(original/'request.json',request);write(original/'response.json',response)
        write(original/'receipt.json',{'model':'test','exit_code':0,'tool_items':[],
              'request_sha256':digest(request),'usage':{'input_tokens':5}})
        (original/'events.jsonl').write_text(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(response)}})+'\n')
        model=Model(self.root/'models','test',reuse_from=self.root/'models')
        with patch('compilation.llm.subprocess.run',side_effect=AssertionError('must reuse')):
            self.assertEqual(model.call('recovered','sdk-program-generation','generate',{'source':'real'}),response)
        self.assertEqual(read(self.root/'models/recovered/receipt.json')['model_invocations'],0)
        with patch('compilation.llm.subprocess.run',side_effect=RuntimeError('fresh review required')):
            with self.assertRaisesRegex(RuntimeError,'fresh review'):
                model.call('review','independent-native-corpus-review','generate',{'source':'real'})

    def test_recovery_never_resets_started_development_or_audit(self):
        prior='a'*32;old=self.root/'jobs'/prior;old.mkdir(parents=True)
        write(old/'request.json',{'agent_id':'agent','behavioral_question':'question','time_range':None,'policy':{'max_repairs':2,'audit_per_label':4,'lifetime_executions':10000}})
        (old/'cycle/qualification').mkdir(parents=True)
        self.lifecycle.discovery_reuse_job=prior
        result=self.lifecycle.start('agent','question')
        self.assertEqual(result['status'],'INSUFFICIENT_SOURCE')
        self.assertIn('cannot reset',result['reason']);self.assertEqual(self.calls,[])

    def test_missing_source_stops_before_any_model(self):
        self.agents['agent']['instructions']={}
        result=self.lifecycle.start('agent','question');self.assertEqual(result['status'],'INSUFFICIENT_SOURCE');self.assertEqual(self.calls,[])

    def test_fabricated_source_citation_stops_before_compiler(self):
        self.model.call=lambda *a:{'status':'READY','citations':[{'ref':'/sources/instructions/source/text','quote':'invented policy'}]}
        with patch('compilation.autonomous.compile_and_qualify') as compiler:
            result=self.lifecycle.start('agent','question');compiler.assert_not_called()
        self.assertEqual(result['status'],'INSUFFICIENT_SOURCE')

    def test_compile_failure_kept_distinct_from_qualification(self):
        with patch('compilation.autonomous.compile_and_qualify',return_value={'status':'CANNOT_QUALIFY','candidate':None,'reason':'CompileFailure'}):
            result=self.lifecycle.start('agent','question')
        self.assertEqual(result['status'],'COMPILATION_FAILED')

    def test_drift_during_qualification_prevents_admission(self):
        def workflow(*a,**kw):Path(self.agents['agent']['instructions']['source']).write_text('changed source');return {'status':'QUALIFIED','candidate':{'version':'v'}}
        with patch('compilation.autonomous.compile_and_qualify',workflow):result=self.lifecycle.start('agent','question')
        self.assertEqual(result['status'],'CANNOT_QUALIFY')

    def test_native_renderer_never_adds_gold_or_complete_coverage(self):
        spec={'events':[{'kind':'agent','name':'root','start_ns':1,'end_ns':8,'parent_index':None,'input':'request','output':None},{'kind':'tool','name':'lookup','start_ns':2,'end_ns':4,'parent_index':0,'input':'{}','output':'partial','status_code':'STATUS_CODE_ERROR'}]}
        trace=render(spec,'case','agent');self.assertFalse(trace['attrs']['conversation_complete']);self.assertNotIn('expected',trace);self.assertEqual(trace['events'][1]['parent_id'],trace['events'][0]['id']);self.assertEqual(trace['events'][1]['attrs']['status_code'],'STATUS_CODE_ERROR')
        spec['events'][1]['end_ns']=1
        with self.assertRaises(ValueError):render(spec,'case','agent')

    def test_source_templates_expand_before_sdk_without_labels(self):
        source={'__SOURCE_SYSTEM__':'Actual instructions','__SOURCE_TOOLS__':[{'name':'lookup'}]}
        spec={'events':[{'kind':'llm','name':'model','start_ns':1,'end_ns':2,
                         'input_json':{'system':'__SOURCE_SYSTEM__','tools':'__SOURCE_TOOLS__','messages':[]}}]}
        trace=render(spec,'case','agent',source)
        self.assertEqual(json.loads(trace['events'][0]['input'])['system'],'Actual instructions')
        self.assertEqual(trace['events'][0]['input'],trace['events'][0]['attrs']['input_value'])

    def test_factored_model_packet_roundtrips_every_byte_and_pointer(self):
        system='évidence\\"\n'*200
        values=[json.dumps({'system':system,'tools':[{'description':system}],'messages':[str(i)]},ensure_ascii=False) for i in range(3)]
        original={'traces':[{'input':s,'attrs':{'input_value':s}} for s in values]}
        packed=factor(original)
        def expand(v):
            if isinstance(v,dict) and '$verbatim_segments' in v:
                return ''.join(s if isinstance(s,str) else packed['verbatim_definitions'][s['ref']] for s in v['$verbatim_segments'])
            if isinstance(v,dict):return {k:expand(x) for k,x in v.items()}
            if isinstance(v,list):return [expand(x) for x in v]
            return v
        self.assertEqual(expand(packed['packet']),original)
        self.assertLess(len(json.dumps(packed)),len(json.dumps(original)))

if __name__=='__main__':unittest.main()
