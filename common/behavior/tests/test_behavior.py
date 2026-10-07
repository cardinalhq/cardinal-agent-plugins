import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Behavior, VERSION, compact, private_json

ID = 'a' * 32

class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.behavior = Behavior({'output_dir': self.temp.name, 'base_url': 'https://cardinal.example',
                                  'org': 'org', 'start': '2026-10-01T05:00:00Z', 'end': '2026-10-02T00:45:00Z'})
        self.calls = []
        self.responses = [{'execution_id': ID, 'status': 'PENDING'}]
        def request(method, path, payload=None):
            self.calls.append((method, path, payload))
            return self.responses.pop(0)
        self.behavior.request = request
        self.behavior.start(VERSION, 'cardinal-investigator')

    def result(self, trace='t1', verdict='MATCH'):
        return {'trace_id': trace, 'verdict': verdict, 'witness_refs': ['message'],
                'result_seq': 1, 'records': [{'op': 'violation', 'reason': 'Claim before submission'}],
                'jev_receipts': [{'decision': 'YES', 'decision_evidence_refs': ['message'],
                                  'reason': 'The assistant says enough evidence to close this out.', 'packet': 'RAW_SECRET_MARKER'}],
                'execution_identity': {'diagnostic_version': VERSION,
                                       'profile_sha256': self.behavior.definition['profile_sha256'],
                                       'udf_sha256': self.behavior.definition['source_sha256'],
                                       'adapter_sha256': self.behavior.definition['profile_sha256']}}

    def page(self, results, status='RUNNING', population=2, match=1, unknown=0):
        return {'execution_id': ID, 'status': status, 'results': results, 'next_cursor': len(results),
                'counts': {'population': population, 'MATCH': match, 'NON_MATCH': 0, 'UNKNOWN': unknown, 'ERROR': 0},
                'receipt': ID if status == 'COMPLETED' else None, 'shards': [{'raw': 'SHARD_SECRET_MARKER'}]}

    def test_running_incremental_projection_and_completed_receipt(self):
        first = self.result()
        self.responses.append(self.page([first]))
        got = self.behavior.poll(ID, 0)
        self.assertEqual(got['execution_status'], 'RUNNING')
        self.assertNotIn('receipt', got)
        self.assertIsNone(got['population_size'])
        self.assertEqual(got['evaluated_so_far'], 2)
        self.assertEqual(set(got['results'][0]), {'trace_id', 'verdict', 'reason', 'witness_refs', 'coverage_gaps'})
        self.assertIn('enough evidence', got['results'][0]['reason'])
        self.assertNotIn('RAW_SECRET_MARKER', json.dumps(got))
        self.assertNotIn('SHARD_SECRET_MARKER', json.dumps(got))
        second = self.result('t2', 'UNKNOWN')
        second.pop('records'); second.pop('jev_receipts'); second.pop('witness_refs')
        second['coverage_gaps'] = [{'ref': 'run', 'reason': 'not confirmed complete'}]
        done = self.page([second], 'COMPLETED', unknown=1)
        done['next_cursor'] = 1000000001
        self.responses.append(done)
        got = self.behavior.poll(ID, 0)
        self.assertIn('after_result_seq=1', self.calls[-1][1])
        self.assertEqual(got['receipt'], ID)
        self.assertEqual(got['received'], 2)
        self.assertEqual(got['population_size'], 2)
        output = self.behavior.render(ID, VERSION)
        html = Path(output['storyboard']).read_text()
        self.assertIn('t1', html); self.assertIn('t2', html)
        self.assertIn('RAW_SECRET_MARKER', html)  # Expandable evidence is confined to artifact.
        self.assertEqual(self.behavior.state_path(ID).stat().st_mode & 0o777, 0o600)

    def test_render_rejects_running_or_wrong_version(self):
        with self.assertRaisesRegex(ValueError, 'completed'):
            self.behavior.render(ID, VERSION)
        with self.assertRaisesRegex(ValueError, 'match'):
            self.behavior.render(ID, 'wrong')

    def test_completed_must_drain_all_pages_and_match_counts(self):
        self.responses.append(self.page([self.result()], 'COMPLETED', population=2, match=2))
        got = self.behavior.poll(ID, 0)
        self.assertNotIn('receipt', got)
        second = self.page([self.result('t2')], 'COMPLETED', population=2, match=1)
        second['next_cursor'] = 2
        self.responses.append(second)
        with self.assertRaisesRegex(ValueError, 'counts'):
            self.behavior.poll(ID, 0)

    def test_rejects_result_from_another_diagnostic(self):
        result = self.result()
        result['execution_identity']['diagnostic_version'] = 'other'
        self.responses.append(self.page([result]))
        with self.assertRaisesRegex(ValueError, 'DiagnosticVersion'):
            self.behavior.poll(ID, 0)

    def test_rejects_wrong_receipt_and_duplicate_trace(self):
        page = self.page([self.result()], 'COMPLETED', population=1)
        page['receipt'] = 'b' * 32
        self.responses.append(page)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.behavior.poll(ID, 0)
        self.responses.append(self.page([self.result(), self.result()]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.behavior.poll(ID, 0)

    def test_reason_does_not_use_unrelated_yes_receipt(self):
        result = self.result()
        result['jev_receipts'][0]['decision_evidence_refs'] = ['unrelated']
        self.assertEqual(compact(result)['reason'], 'Claim before submission')

if __name__ == '__main__':
    unittest.main()
