import copy
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.admission import QualifiedRegistry
from compilation.engine import Policy, freeze, gate
from compilation.storage import write, read, digest, filehash
from compilation.runtime import IDENTITY_KEYS
from compilation.qualified_api import handler, PREFIX
from compilation.qualified_host import QualifiedHost
from test_qualification import cases


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.jobs = self.root/'jobs'; self.jobs.mkdir()
        self.drift = {}; self.calls = []
        self.registry = QualifiedRegistry(self.root/'registry', self.jobs,
            lambda agent, identity: {**identity, **self.drift}, self.run_sdk)

    def run_sdk(self, bundle, certificate, source, trace):
        self.calls.append(trace)
        return {'actual': 'UNKNOWN', 'mechanical_pass': True, 'reason': 'test fixture', 'witness_refs': []}

    def make_job(self, source='candidate', status='QUALIFIED', question='question'):
        job_id = secrets.token_hex(16); job = self.jobs/job_id; cycle=job/'cycle'; q=cycle/'qualification'; q.mkdir(parents=True)
        program=job/'program.py'; program.write_text(source); plan=job/'plan.json';write(plan,{'source':source})
        candidate={'source':str(program),'plan':str(plan),'version':digest(source)}
        policy={'max_repairs':2,'audit_per_label':1,'lifetime_executions':10000};write(q/'policy.json',policy)
        discovery=cases('dev');audit=cases('audit');write(q/'discovery.json',discovery);write(q/'audit-cases.json',audit)
        rows=[{'id':c['id'],'expected':c['expected'],'actual':c['expected'],'mechanical_pass':True,'trace_sha256':digest(c['trace']),'version':candidate['version']} for c in audit]
        write(q/'audit.json',rows)
        qualification={**gate(rows),'version':candidate['version'],'audit_sha256':digest(audit),'policy_sha256':digest(policy)};write(q/'qualification.json',qualification)
        result={'status':status,'candidate':candidate};write(cycle/'outcome.json',result)
        identity={k:digest(k) for k in IDENTITY_KEYS if k!='program'}
        if status=='QUALIFIED':freeze(cycle/'bundle',result,identity,q/'qualification.json',{'plan.json':plan})
        identity['program']=filehash(program)
        return job_id,identity

    def issue(self,source='candidate',question='question'):
        job,identity=self.make_job(source,question=question)
        return self.registry.issue(job,'agent',question,identity)['qualified_id']

    def population(self):
        return {'traces':[{'trace_id':'real-trace','agent':'agent'}],'selection':{'service':'agent','receipt':'trusted-test'},'exclusions':[]}

    def test_execute_qualified_once_and_keep_unknown(self):
        q=self.issue();receipt=self.registry.execute(q,self.population())
        self.assertEqual(receipt['counts'],{'MATCH':0,'NON_MATCH':0,'UNKNOWN':1,'ERROR':0});self.assertEqual(len(self.calls),1)
        self.assertEqual(self.registry.execution_receipt(q,receipt['execution_id']),receipt)
        self.assertEqual(self.registry.inspect(q)['executions'][0]['execution_id'],receipt['execution_id'])

    def test_other_agent_cannot_enter_population(self):
        q=self.issue();population=self.population();population['traces'][0]['agent']='another-agent'
        with self.assertRaisesRegex(ValueError,'agent does not match'):self.registry.execute(q,population)
        self.assertEqual(self.calls,[])

    def test_development_acceptance_cannot_issue(self):
        job,identity=self.make_job(status='DEVELOPMENT_ACCEPTED')
        with self.assertRaisesRegex(ValueError,'not qualified'):self.registry.issue(job,'agent','question',identity)
        self.assertEqual(self.calls,[])

    def test_tampered_certificate_even_rehashed_is_rejected(self):
        q=self.issue();p=self.registry.root/q/'bundle/certificate.json';cert=read(p);cert['version']='forged';cert.pop('certificate_sha256');cert['certificate_sha256']=digest(cert);p.write_text(json.dumps(cert))
        with self.assertRaises(ValueError):self.registry.execute(q,self.population())
        self.assertEqual(self.calls,[])

    def test_issuer_registry_signature_prevents_caller_certificate(self):
        q=self.issue();p=self.registry.root/q/'admission.json';r=read(p);r['value']['enabled']=False;p.write_text(json.dumps(r))
        with self.assertRaisesRegex(ValueError,'untrusted'):self.registry.inspect(q)

    def test_each_identity_drift_fails_before_sdk(self):
        q=self.issue();keys=self.registry.inspect(q)['identity']
        for key in keys:
            self.drift={key:'changed'}
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'drift'):self.registry.execute(q,self.population())
        self.assertEqual(self.calls,[])

    def test_plan_and_evidence_tampering(self):
        q=self.issue();job=self.registry.inspect(q)['job_id'];(self.jobs/job/'cycle/qualification/audit.json').write_text('[]')
        with self.assertRaisesRegex(ValueError,'evidence manifest'):self.registry.execute(q,self.population())

    def test_disable_revoke_and_valid_rollback(self):
        old=self.issue('old');new=self.issue('new')
        with self.assertRaisesRegex(ValueError,'not active'):self.registry.execute(old,self.population())
        self.registry.control(new,'disable')
        with self.assertRaisesRegex(ValueError,'disabled'):self.registry.execute(new,self.population())
        self.registry.control(new,'rollback',old);self.registry.execute(old,self.population())
        self.registry.control(old,'revoke')
        with self.assertRaisesRegex(ValueError,'revoked'):self.registry.execute(old,self.population())

    def test_rollback_rejects_drift_and_unrelated_question(self):
        old=self.issue('old');new=self.issue('new');self.drift={'agent':'changed'}
        with self.assertRaisesRegex(ValueError,'drift'):self.registry.control(new,'rollback',old)
        self.drift={};other=self.issue('other',question='another')
        with self.assertRaises(ValueError):self.registry.control(other,'rollback',old)

    def test_provenance_error_is_error_not_unknown(self):
        q=self.issue();self.registry.run_sdk=lambda *a:{'actual':'MATCH','mechanical_pass':False}
        receipt=self.registry.execute(q,self.population());self.assertEqual(receipt['counts']['ERROR'],1)

    def test_http_auth_and_no_certificate_upload_route(self):
        class Lifecycle: pass
        host=QualifiedHost(Lifecycle(),self.registry,None,lambda principal,operation:None)
        q=self.issue();server=HTTPServer(('127.0.0.1',0),handler([('test-token','tenant',host)]));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}'+PREFIX+'inspect'
        def request(body,token):return urlopen(Request(url,data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+token}),timeout=5)
        with self.assertRaises(HTTPError) as error:request({'qualified_id':q},'wrong')
        self.assertEqual(error.exception.code,401)
        with self.assertRaises(HTTPError) as error:request({'qualified_id':q,'certificate':{}},'test-token')
        self.assertEqual(error.exception.code,409)
        with request({'qualified_id':q},'test-token') as response:self.assertEqual(json.load(response)['status'],'QUALIFIED')
        with urlopen(Request(url,data=json.dumps({'qualified_id':q}).encode(),headers={'x-cardinalhq-api-key':'test-token'}),timeout=5) as response:
            self.assertEqual(json.load(response)['status'],'QUALIFIED')

    def test_async_question_persists_outcome_and_restart_fails_closed(self):
        from types import SimpleNamespace
        event=threading.Event()
        def start(**payload):
            event.wait(2)
            return {'status':'CANNOT_QUALIFY','reason':'retained failed audit'}
        lifecycle=SimpleNamespace(jobs=self.jobs,start=start)
        host=QualifiedHost(lifecycle,self.registry,None,lambda *a:None)
        task=host.submit('tenant','create',{'agent_id':'agent','behavioral_question':'question'})
        self.assertEqual(host.dispatch('tenant','job',{'job_id':task['job_id']})['status'],'RUNNING')
        restarted=QualifiedHost(lifecycle,self.registry,None,lambda *a:None)
        self.assertEqual(restarted.dispatch('tenant','job',{'job_id':task['job_id']})['status'],'CANNOT_QUALIFY')
        event.set()
        import time
        for _ in range(100):
            outcome=host.dispatch('tenant','job',{'job_id':task['job_id']})
            if outcome['status']!='RUNNING':break
            time.sleep(.01)
        self.assertEqual(outcome['reason'],'retained failed audit')

if __name__=='__main__':unittest.main()
