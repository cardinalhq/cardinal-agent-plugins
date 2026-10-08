"""The normal plugin flow uses deployed authoring and durable receipt endpoints."""
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Behavior, tools_list, SDK_IDENTITIES


class HostedBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'output_dir': self.temp.name, 'base_url': 'https://query.example'}
        self.behavior = Behavior(self.config)
        self.version = 'a' * 64
        self.execution = 'b' * 32
        self.source = 'def evaluate(trace, recorder, jev):\n    return "NON_MATCH"\n'
        self.program = {'diagnostic_version': self.version, 'diagnostic_id': 'authored-check',
            'source': self.source, 'source_sha256': hashlib.sha256(self.source.encode()).hexdigest(),
            'profile_sha256': 'c' * 64, 'adapter_sha256': 'd' * 64,
            'behavior_contract': {'clauses': [{'kind': 'trigger', 'interpretation': 'Authored check'}]},
            'compile_receipt': {'receipt_id': 'compile-1'}}
        artifact = {'format': 'behavior-sdk-authoring-v1', 'version': '0.1.1', 'files': {'behavior_sdk/__init__.py': '# canonical runtime source\nclass TraceView:\n    pass\n'}}
        digest = hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.sdk = {'artifact': artifact, 'sdk_runtime_sha256': digest, 'sdk_artifact_sha256': digest,
                    'profile_sha256': 'c' * 64, 'host_runtime_sha256': 'f' * 64}
        provenance = {'repository': 'https://github.com/cardinalhq/behavior-sdk', 'commit': '2' * 40,
                      'version': '0.1.1', 'sdk_runtime_sha256': digest,
                      'artifact_url': f'https://github.com/cardinalhq/behavior-sdk/releases/download/v0.1.1/behavior-sdk-{digest}.json'}
        self.sdk.update(sdk_source=provenance, sdk_version='0.1.1')
        self.program.update({key: self.sdk[key] for key in SDK_IDENTITIES}, sdk_source=provenance)
        self.behavior.sdk_observation = self.sdk
        self.teaching = {'sdk_source': provenance, 'diagnostic_version': self.version, 'test_receipt': '1' * 64,
                         **{key: self.sdk[key] for key in SDK_IDENTITIES},
                         'results': [{'trace_id': 'e' * 32, 'verdict': 'NON_MATCH', 'expected_verdict': 'NON_MATCH',
                                      'records': [], 'witness_refs': [], 'jev_receipts': []}]}
        self.teaching['results'][0]['execution_identity'] = {'sdk_source': provenance,
            **{key: self.sdk[key] for key in SDK_IDENTITIES}, 'diagnostic_version': self.version,
            'udf_sha256': self.program['source_sha256'], 'adapter_sha256': self.program['adapter_sha256']}
        self.acceptance = {'sdk_source': provenance, 'diagnostic_version': self.version, 'acceptance_id': 'accepted-1',
                           'test_receipt': '1' * 64, **{key: self.sdk[key] for key in SDK_IDENTITIES}}
        self.responses = []
        self.calls = []
        def request(method, path, payload=None):
            self.calls.append((method, path, payload))
            return copy.deepcopy(self.responses.pop(0))
        self.behavior.request = request

    def compile(self):
        self.responses.append(self.program)
        return self.behavior.compile('Authored check', 'service', self.source, {},
            [{'trace': {'trace_id': 'teaching-1'}, 'expected_verdict': 'NON_MATCH'}])

    def teaching_test(self, expected=True):
        receipt = {key: value for key, value in self.teaching.items() if key not in ('receipt', 'test_receipt')}
        self.teaching['receipt'] = receipt
        self.teaching['test_receipt'] = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        self.acceptance['test_receipt'] = self.teaching['test_receipt']
        self.responses.append(self.teaching)
        return self.behavior.test(self.version, ['e' * 32], 'service',
                                 '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z',
                                 {'e' * 32: 'NON_MATCH'} if expected else None)

    def test_compile_inspect_accept_execute_poll_inspect_render(self):
        self.compile()
        self.assertEqual(self.calls[-1][2]['udf_source'], self.source)
        self.assertEqual(self.calls[-1][2]['service_name'], 'service')
        with self.assertRaisesRegex(ValueError, 'Accept'):
            self.behavior.start(self.version, 'service', 'start', 'end')
        self.responses.append(self.program)
        self.assertEqual(self.behavior.inspect_program(self.version)['source'], self.source)
        self.teaching_test()
        self.responses.append(self.acceptance)
        self.behavior.accept(self.version)
        self.responses.append({'execution_id': self.execution, 'status': 'PENDING'})
        self.behavior.start(self.version, 'service', '2026-10-01T00:00:00Z', '2026-10-02T00:00:00Z')
        self.assertNotIn('org', self.calls[-1][2])
        raw = {'trace_id': 'e' * 32, 'verdict': 'NON_MATCH', 'execution_identity': {
            'sdk_source': self.sdk['sdk_source'], 'diagnostic_version': self.version, 'udf_sha256': self.program['source_sha256'],
            'profile_sha256': 'c' * 64, 'adapter_sha256': 'd' * 64,
            **{key: self.sdk[key] for key in SDK_IDENTITIES}},
            'semantic_occurrences': [{'id': 'witness-1', 'output': '<script>unsafe()</script>'}],
            'native_trace': {'receipt': {'materialization': 'verified-native-input'}},
            'jev_receipts': [{'decision': 'NO', 'reason': 'No requested behavior.',
                              'evidence': [{'ref': 'message', 'text': 'PRIVATE_INPUT'}]}]}
        projected = copy.deepcopy(raw)
        projected.pop('semantic_occurrences')
        projected.pop('native_trace')
        projected['jev_receipts'][0].pop('evidence')
        self.responses.append({'execution_id': self.execution, 'status': 'COMPLETED',
            'results': [projected], 'next_cursor': 1, 'receipt': self.execution,
            'counts': {'population': 1, 'MATCH': 0, 'NON_MATCH': 1, 'UNKNOWN': 0, 'ERROR': 0}})
        self.assertNotIn('PRIVATE_INPUT', json.dumps(self.behavior.poll(self.execution, 0)))
        self.responses.append(raw)
        inspected = self.behavior.inspect(self.execution, raw['trace_id'])
        self.assertNotIn('PRIVATE_INPUT', json.dumps(inspected))
        self.assertIn('/receipt?trace_id=', self.calls[-1][1])
        self.assertEqual(self.behavior.read(self.execution)['cursor'], 1)
        self.responses.append(raw)
        rendered = self.behavior.render(self.execution, self.version)
        self.assertIn('PRIVATE_INPUT', Path(rendered['storyboard']).read_text())
        self.assertIn('authored-check', Path(rendered['storyboard']).read_text())
        html = Path(rendered['storyboard']).read_text()
        self.assertIn('Retained source events', html)
        self.assertIn('verified-native-input', html)
        self.assertIn('&lt;script&gt;unsafe()&lt;/script&gt;', html)
        self.assertNotIn('<script>unsafe()', html)
        # A fresh process resumes using persisted program and receipt identities.
        restarted = Behavior(self.config)
        self.assertEqual(restarted.context(self.version)[0]['source_sha256'], self.program['source_sha256'])
        self.assertEqual(restarted.read(self.execution)['receipt'], self.execution)

    def test_compile_failure_returns_receipt_and_diagnostics_for_revision(self):
        failed = {'error': 'validation failed', 'compile_receipt': {'id': 'failed-1'}, 'diagnostics': ['missing evaluate']}
        self.responses.append(failed)
        result = self.behavior.compile('Check', 'service', 'invalid', {}, [{'trace': {'trace_id': 't'}}])
        self.assertEqual(result, failed)
        self.assertFalse((Path(self.temp.name) / 'programs').exists())

    def test_version_integrity_acceptance_and_source_identity(self):
        self.compile()
        self.responses.append(dict(self.program, source='changed'))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.behavior.inspect_program(self.version)
        self.teaching_test()
        self.responses.append(dict(self.acceptance, diagnostic_version='f' * 64, acceptance_id='wrong'))
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.behavior.accept(self.version)
        self.assertNotIn('acceptance_id', json.loads(self.behavior.program_path(self.version).read_text()))
        with self.assertRaisesRegex(ValueError, 'Inspect'):
            self.behavior.accept('f' * 64)

    def test_sdk_tool_and_no_frozen_default(self):
        self.responses.append(self.sdk)
        result = self.behavior.sdk()
        self.assertEqual(json.loads(Path(result['sdk_artifact_file']).read_text()), self.sdk['artifact'])
        self.assertEqual(result['artifact'], self.sdk['artifact'])
        source_path = Path(result['sdk_files']['behavior_sdk/__init__.py'])
        self.assertEqual(source_path.read_bytes(), self.sdk['artifact']['files']['behavior_sdk/__init__.py'].encode())
        self.assertEqual(len(source_path.read_text().splitlines()), 3)
        self.assertEqual(source_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.calls[-1][1], '/api/v1/behavior-programs/sdk')
        schemas = {tool['name']: tool['inputSchema'] for tool in tools_list(self.behavior)['tools']}
        self.assertEqual(schemas['execute_behavior']['required'], ['accepted_behavior', 'population', 'start', 'end'])
        self.assertIn('udf_source', schemas['compile_behavior']['required'])
        self.assertIsNone(self.behavior.version)
        self.assertFalse(self.behavior.legacy)

    def test_authoring_transport_allows_bounded_server_teaching_checks(self):
        for method, path, expected in [
            ('POST', '/api/v1/behavior-programs/compile', 310),
            ('POST', '/api/v1/behavior-programs/test', 310),
            ('POST', '/api/v1/behavior-programs/' + self.version + '/accept', 310),
            ('GET', '/api/v1/behavior-programs/' + self.version, 60),
            ('POST', '/api/v1/behavior-executions', 60),
        ]:
            with self.subTest(path=path), patch.dict('os.environ', {'CARDINAL_MCP_API_KEY': 'test-key'}), \
                    patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(b'{}')) as opened:
                Behavior.request(self.behavior, method, path)
                self.assertEqual(opened.call_args.kwargs['timeout'], expected)

    def test_sdk_tampering_and_compiler_identity_skew_fail_closed(self):
        self.responses.append(dict(self.sdk, artifact={'format': 'behavior-sdk-authoring-v1', 'version': '0.1.1', 'files': {'x.py': 'different'}}))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.behavior.sdk()
        self.responses.append(self.sdk)
        cached = Path(self.behavior.sdk()['sdk_artifact_file'])
        cached.write_text('{}')
        self.responses.append(self.sdk)
        with self.assertRaisesRegex(ValueError, 'cached SDK'):
            self.behavior.sdk()
        self.responses.append(dict(self.program, sdk_runtime_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.behavior.compile('Check', 'service', self.source, {})
        self.assertFalse(self.behavior.program_path(self.version).exists())

    def test_compilation_requires_observed_sdk_and_sends_identities(self):
        self.behavior.sdk_observation = None
        with self.assertRaisesRegex(ValueError, 'get_behavior_sdk'):
            self.behavior.compile('Check', 'service', self.source, {})
        self.responses.extend([self.sdk, self.program])
        self.behavior.sdk()
        self.behavior.compile('Check', 'service', self.source, {})
        payload = self.calls[-1][2]
        self.assertEqual(payload['teaching_examples'], [])
        self.assertEqual({key: payload[key] for key in SDK_IDENTITIES}, self.behavior.identities(self.sdk))

    def test_teaching_uses_real_trace_endpoint_and_preserves_evidence(self):
        self.compile()
        with self.assertRaisesRegex(ValueError, 'test_behavior'):
            self.behavior.accept(self.version)
        self.teaching['results'][0].update(records=[{'op': 'explanation', 'reason': 'teaching'}],
                                          jev_receipts=[{'receipt_id': 'semantic-1', 'decision': 'NO'}])
        result = self.teaching_test()
        self.assertEqual(self.calls[-1][1], '/api/v1/behavior-programs/test')
        self.assertEqual(result['results'][0]['jev_receipts'][0]['receipt_id'], 'semantic-1')
        self.assertEqual(json.loads(Path(result['receipt_file']).read_text()), self.teaching['receipt'])
        with self.assertRaisesRegex(ValueError, 'source differs'):
            self.behavior.test(self.version, ['e' * 32], 'service', 'start', 'end', udf_source='changed')
        self.responses.append(dict(self.teaching, host_runtime_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.behavior.test(self.version, ['e' * 32], 'service', 'start', 'end')

    def test_acceptance_binds_teaching_receipt_and_expected_verdicts(self):
        self.compile()
        self.teaching['results'][0].pop('expected_verdict')
        self.teaching_test(expected=False)
        with self.assertRaisesRegex(ValueError, 'expected verdict'):
            self.behavior.accept(self.version)
        self.teaching['results'][0]['expected_verdict'] = 'NON_MATCH'
        self.teaching_test()
        self.responses.append(dict(self.acceptance, test_receipt='0' * 64))
        with self.assertRaisesRegex(ValueError, 'teaching receipt identity'):
            self.behavior.accept(self.version)
        self.responses.append(self.acceptance)
        self.behavior.accept(self.version)
        self.assertEqual(self.calls[-1][2]['test_receipt'], self.teaching['test_receipt'])
        self.assertEqual(self.calls[-1][2]['sdk_runtime_sha256'], self.sdk['sdk_runtime_sha256'])
        persisted = json.loads(self.behavior.program_path(self.version).read_text())
        self.assertEqual(persisted['acceptance_receipt'], self.acceptance)
        self.assertEqual(persisted['teaching_receipt'], self.teaching)

    def test_teaching_receipt_hash_and_immutable_cache_are_verified(self):
        self.compile()
        result = self.teaching_test()
        changed = copy.deepcopy(self.teaching)
        changed['receipt']['results'][0]['records'] = [{'op': 'fabricated'}]
        self.responses.append(changed)
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.behavior.test(self.version, ['e' * 32], 'service', 'start', 'end', {'e' * 32: 'NON_MATCH'})
        path = Path(result['receipt_file'])
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'cached teaching'):
            self.teaching_test()
        self.assertEqual(path.read_text(), '{}')

    def test_acceptance_rejects_teaching_miss_or_execution_failure(self):
        self.compile()
        self.teaching['results'][0]['verdict'] = 'MATCH'
        self.teaching_test()
        with self.assertRaisesRegex(ValueError, 'do not match'):
            self.behavior.accept(self.version)
        program = json.loads(self.behavior.program_path(self.version).read_text())
        program['teaching_receipt']['results'][0].update(verdict='ERROR', expected_verdict='ERROR')
        self.behavior.program_path(self.version).write_text(json.dumps(program))
        with self.assertRaisesRegex(ValueError, 'do not match'):
            self.behavior.accept(self.version)

    def test_each_teaching_result_must_match_compiled_execution_identity(self):
        self.compile()
        self.teaching['results'][0]['execution_identity']['sdk_runtime_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            self.teaching_test()
        self.assertNotIn('test_receipt', json.loads(self.behavior.program_path(self.version).read_text()))

    def test_public_sdk_version_and_source_mismatch_fail_closed(self):
        self.responses.append(dict(self.sdk, sdk_version='0.1.2'))
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            self.behavior.sdk()
        changed = copy.deepcopy(self.program)
        changed['sdk_source']['commit'] = '3' * 40
        self.responses.append(changed)
        with self.assertRaisesRegex(ValueError, 'source/version identity mismatch'):
            self.behavior.compile('Check', 'service', self.source, {})
        self.assertFalse(self.behavior.program_path(self.version).exists())

    def test_sdk_source_materialization_rejects_tampering_and_escaping_paths(self):
        self.responses.append(self.sdk)
        result = self.behavior.sdk()
        Path(result['sdk_files']['behavior_sdk/__init__.py']).write_text('tampered')
        self.responses.append(self.sdk)
        with self.assertRaisesRegex(ValueError, 'cached SDK source integrity'):
            self.behavior.sdk()
        escaping = copy.deepcopy(self.sdk)
        escaping['artifact']['files']['../escape.py'] = 'not allowed'
        digest = hashlib.sha256(json.dumps(escaping['artifact'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        escaping.update(sdk_runtime_sha256=digest, sdk_artifact_sha256=digest)
        escaping['sdk_source'].update(sdk_runtime_sha256=digest,
            artifact_url=f'https://github.com/cardinalhq/behavior-sdk/releases/download/v0.1.1/behavior-sdk-{digest}.json')
        self.responses.append(escaping)
        with self.assertRaisesRegex(ValueError, 'invalid source path'):
            self.behavior.sdk()
        self.assertFalse((Path(self.temp.name) / 'sdk/escape.py').exists())
