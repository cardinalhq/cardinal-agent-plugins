import copy
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Behavior, VERSION, compact, private_json, tools_list

ID = 'a' * 32

class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.behavior = Behavior({'diagnostic_version': VERSION, 'output_dir': self.temp.name, 'base_url': 'https://cardinal.example',
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

    def configured_behavior(self):
        artifacts = Path(self.temp.name) / 'custom-artifacts'
        def freeze(kind, payload):
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            value = dict(payload, version=digest)
            private_json(artifacts / kind / f'{digest}.json', value)
            return value
        preview = freeze('previews', {'clauses': [{'kind': 'trigger', 'interpretation': 'Detect the configured Megan behavior.'}]})
        definition = freeze('versions', {
            'diagnostic_id': 'megan-custom-behavior', 'preview_version': preview['version'],
            'source_sha256': '1' * 64, 'profile_sha256': '2' * 64,
            'population': {'agent': 'megan'}, 'evaluation_policy': {}, 'finality_policy': {}})
        config = dict(self.behavior.config, diagnostic_version=definition['version'],
                      population='megan', description='The newly compiled Megan behavior.',
                      artifacts=str(artifacts), adapter_sha256='3' * 64,
                      compile_receipt_ref='compiler-receipt-42')
        return Behavior(config), config

    def test_configured_version_selection_schema_submit_projection_and_render(self):
        behavior, config = self.configured_behavior()
        calls = []
        responses = [{'execution_id': ID, 'status': 'PENDING'}]
        def request(method, path, payload=None):
            calls.append((method, path, payload))
            return responses.pop(0)
        behavior.request = request
        selected = behavior.select()
        self.assertEqual(selected['diagnostic_version'], config['diagnostic_version'])
        self.assertEqual(selected['population'], 'megan')
        self.assertEqual(selected['compile_receipt_ref'], 'compiler-receipt-42')
        self.assertIn('no compilation performed', selected['selection'])
        schemas = {tool['name']: tool for tool in tools_list(behavior)['tools']}
        execute = schemas['execute_behavior']['inputSchema']['properties']
        self.assertEqual(execute['accepted_behavior']['enum'], [behavior.version])
        self.assertEqual(execute['population']['enum'], ['megan'])
        self.assertIn(config['description'], schemas['select_behavior']['description'])
        started = behavior.start(behavior.version, 'megan')
        self.assertEqual(started['diagnostic_version'], behavior.version)
        self.assertEqual(calls[0][2]['diagnostic_version'], behavior.version)
        self.assertEqual(calls[0][2]['service_name'], 'megan')
        raw = self.result()
        raw['execution_identity'] = {'diagnostic_version': behavior.version,
                                     'udf_sha256': '1' * 64, 'profile_sha256': '2' * 64,
                                     'adapter_sha256': '3' * 64}
        responses.append(self.page([raw], 'COMPLETED', population=1))
        result = behavior.poll(ID, 0)
        self.assertNotIn('RAW_SECRET_MARKER', json.dumps(result))
        self.assertEqual(set(result['results'][0]), {'trace_id', 'verdict', 'reason', 'witness_refs', 'coverage_gaps'})
        rendered = behavior.render(result['receipt'], behavior.version)
        self.assertIn('megan-custom-behavior', Path(rendered['storyboard']).read_text())

    def test_configured_identity_rejects_changed_adapter_profile_or_source(self):
        behavior, _ = self.configured_behavior()
        for field in ('diagnostic_version', 'profile_sha256', 'udf_sha256', 'adapter_sha256'):
            with self.subTest(field=field):
                responses = [{'execution_id': ID, 'status': 'PENDING'}]
                behavior.request = lambda *args, **kwargs: responses.pop(0)
                behavior.start(behavior.version, 'megan')
                raw = self.result()
                raw['execution_identity'] = {'diagnostic_version': behavior.version,
                                             'udf_sha256': '1' * 64, 'profile_sha256': '2' * 64,
                                             'adapter_sha256': '3' * 64}
                raw['execution_identity'][field] = 'f' * 64
                responses.append(self.page([raw]))
                with self.assertRaisesRegex(ValueError, 'DiagnosticVersion'):
                    behavior.poll(ID, 0)

    def test_configured_artifacts_are_content_verified(self):
        behavior, config = self.configured_behavior()
        path = Path(config['artifacts']) / 'versions' / f'{behavior.version}.json'
        original = path.read_text()
        changed = json.loads(original)
        changed['profile_sha256'] = 'f' * 64
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'DiagnosticVersion integrity'):
            Behavior(config)
        path.write_text(original)
        preview_path = Path(config['artifacts']) / 'previews' / f'{behavior.definition["preview_version"]}.json'
        preview = json.loads(preview_path.read_text())
        preview['clauses'][0]['interpretation'] = 'A different contract'
        preview_path.write_text(json.dumps(preview))
        with self.assertRaisesRegex(ValueError, 'preview integrity'):
            Behavior(config)

    def test_custom_version_requires_explicit_product_context(self):
        for field in ('population', 'description', 'artifacts'):
            with self.subTest(field=field):
                _, config = self.configured_behavior()
                config.pop(field)
                with self.assertRaisesRegex(ValueError, 'requires population'):
                    Behavior(config)

    def test_reason_does_not_use_unrelated_yes_receipt(self):
        result = self.result()
        result['jev_receipts'][0]['decision_evidence_refs'] = ['unrelated']
        self.assertEqual(compact(result)['reason'], 'Claim before submission')

    def test_error_reason_prioritizes_failure_over_earlier_success(self):
        result = self.result(verdict='ERROR')
        result['error'] = 'JudgeExecutionError: backend timed out'
        result['jev_receipts'].append({'decision': 'ERROR', 'reason': 'judge response unavailable: TimeoutError'})
        self.assertEqual(compact(result)['reason'], result['error'])
        result.pop('error')
        self.assertEqual(compact(result)['reason'], 'judge response unavailable: TimeoutError')
        result['jev_receipts'].pop()
        self.assertEqual(compact(result)['reason'], 'Trace evaluation failed.')

    def test_receipt_inspection_keeps_evidence_private_and_does_not_advance_cursor(self):
        raw = self.result(verdict='ERROR')
        raw['jev_receipts'] = [dict(raw['jev_receipts'][0], decision='ERROR',
            proposition='Does the response ask for clarification?',
            evidence=[{'ref': 'message', 'text': 'PRIVATE_MODEL_INPUT'}],
            config={'model': 'pinned-model', 'model_version': 'v1', 'secret': 'PRIVATE_CONFIG'},
            attempts=[{'status': 'invalid_after_retry', 'error': 'BackendTimeout'}]) for _ in range(10)]
        page = self.page([raw], 'COMPLETED', population=1, match=0)
        page['counts']['ERROR'] = 1
        self.responses.append(page)
        self.behavior.poll(ID, 0)
        before = self.behavior.state_path(ID).read_bytes()
        summary = self.behavior.inspect(ID, 't1')
        self.assertEqual(summary['receipt'], ID)
        self.assertEqual(summary['next_jev'], 8)
        self.assertEqual(len(summary['jev_receipts']), 8)
        self.assertEqual(summary['jev_receipts'][0]['decision'], 'ERROR')
        self.assertEqual(summary['jev_receipts'][0]['attempts'][0]['error'], 'BackendTimeout')
        self.assertNotIn('PRIVATE_', json.dumps(summary))
        self.assertNotIn('RAW_SECRET_MARKER', json.dumps(summary))
        self.assertNotIn('SHARD_SECRET_MARKER', json.dumps(summary))
        self.assertEqual(self.behavior.inspect(ID, 't1', 8)['next_jev'], None)
        self.assertEqual(self.behavior.state_path(ID).read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'not present'): self.behavior.inspect(ID, 'foreign')
        with self.assertRaisesRegex(ValueError, 'nonnegative'): self.behavior.inspect(ID, 't1', -1)

if __name__ == '__main__':
    unittest.main()
