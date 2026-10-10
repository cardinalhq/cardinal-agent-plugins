"""Real PostgreSQL integration tests (opt in with COMPLETENESS_TEST_DSN)."""
import copy
import json
import os
import threading
import unittest

from compilation.completeness import verify_completeness
from compilation.completeness_postgres import PostgresCompletenessStore
from compilation.storage import digest
from test_completeness import fixture


@unittest.skipUnless(os.environ.get('COMPLETENESS_TEST_DSN'), 'requires isolated PostgreSQL database')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.connect=lambda:psycopg.connect(os.environ['COMPLETENESS_TEST_DSN'])
        self.store=PostgresCompletenessStore(self.connect,table='coverage_test')
        self.c,self.s,self.n,self.t=fixture()
        self.c['guard_identity']=self.c['identities']['recorder']
        self.s['closure']['guard_identity']=self.c['guard_identity']
        with self.connect() as con,con.cursor() as cur:
            cur.execute('CREATE TABLE IF NOT EXISTS coverage_test (key text PRIMARY KEY, value text NOT NULL)')
            cur.execute('TRUNCATE coverage_test')
            for k,v in ((self.store.source_key(self.s['execution_id']),self.s),
                        (self.store.contract_key(self.s['agent_id']),self.c)):
                cur.execute('INSERT INTO coverage_test VALUES (%s,%s)',(k,json.dumps(v)))
        self.receipt=verify_completeness(self.c,self.s,self.n,self.t)

    def test_publish_binds_both_contract_and_source(self):
        self.assertTrue(self.store.publish_if_current(self.s['execution_id'],self.s,self.receipt))
        self.assertEqual(self.store.load_source(self.s['execution_id']),self.s)
        with self.connect() as con,con.cursor() as cur:
            altered=copy.deepcopy(self.c);altered['scope_id']='revoked-scope'
            cur.execute('UPDATE coverage_test SET value=%s WHERE key=%s',(json.dumps(altered),self.store.contract_key(self.s['agent_id'])))
        self.assertFalse(self.store.publish_if_current(self.s['execution_id'],self.s,self.receipt))

    def test_waiting_publication_rejects_concurrent_source_revision(self):
        blocker=self.connect();cur=blocker.cursor()
        cur.execute('SELECT value FROM coverage_test WHERE key=%s FOR UPDATE',(self.store.source_key(self.s['execution_id']),))
        started=threading.Event();answers=[]
        def publish():
            started.set();answers.append(self.store.publish_if_current(self.s['execution_id'],self.s,self.receipt))
        thread=threading.Thread(target=publish);thread.start();started.wait(2)
        changed=copy.deepcopy(self.s);changed['generation']+=1;changed['revision']='invalidated'
        cur.execute('UPDATE coverage_test SET value=%s WHERE key=%s',(json.dumps(changed),self.store.source_key(self.s['execution_id'])))
        blocker.commit();blocker.close();thread.join(5)
        self.assertEqual(answers,[False])

    def test_durable_round_robin_does_not_starve_new_sources(self):
        with self.connect() as con,con.cursor() as cur:
            for number in range(4):
                row=copy.deepcopy(self.s);row['execution_id']='new-'+str(number)
                cur.execute('INSERT INTO coverage_test VALUES (%s,%s)',
                    (self.store.source_key(row['execution_id']),json.dumps(row)))
        seen=[]
        for unused in range(3):
            # A fresh process/store must resume the database cursor.
            fresh=PostgresCompletenessStore(self.connect,table='coverage_test')
            seen.extend(fresh.enumerate_pending(limit=2))
        self.assertEqual(set(seen),{'execution-1','new-0','new-1','new-2','new-3'})
        self.assertTrue(self.store.enumerate_pending(limit=2))

    def test_malformed_source_and_dense_span_keys_do_not_starve_valid_source(self):
        with self.connect() as con,con.cursor() as cur:
            for number in range(8):
                cur.execute('INSERT INTO coverage_test VALUES (%s,%s)',
                    ('cardinal.execution-source.v1/aaa/start/'+str(number),'{}'))
            cur.execute('INSERT INTO coverage_test VALUES (%s,%s)',
                (self.store.source_key('aaa-malformed'),'[1,2]'))
        self.assertEqual(self.store.enumerate_pending(limit=1),['aaa-malformed'])
        with self.assertRaises(ValueError):self.store.load_source('aaa-malformed')
        self.assertEqual(self.store.enumerate_pending(limit=1),['execution-1'])
        with self.connect() as con,con.cursor() as cur:
            cur.execute('UPDATE coverage_test SET value=%s WHERE key=%s',
                ('{not json',self.store.source_key('aaa-malformed')))
        self.assertEqual(self.store.enumerate_pending(limit=1),['aaa-malformed'])
        self.assertEqual(self.store.enumerate_pending(limit=1),['execution-1'])

    def test_enrollment_and_identifier_safety(self):
        with self.assertRaises(ValueError):PostgresCompletenessStore(self.connect,table='unsafe;drop')
        self.assertEqual(self.store.enumerate_pending(),[self.s['execution_id']])
        for field in ('source','deployment','recorder'):
            bad=copy.deepcopy(self.c);bad['identities'][field]='unverified'
            with self.assertRaises(ValueError):self.store.validate_enrollment(bad)
        self.assertIn('%2F',self.store.source_key('execution/segment_%'))

if __name__=='__main__':unittest.main()
