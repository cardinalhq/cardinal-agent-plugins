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
from server import Behavior, tools_list


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

    def test_compile_inspect_accept_execute_poll_inspect_render(self):
        self.compile()
        self.assertEqual(self.calls[-1][2]['udf_source'], self.source)
        self.assertEqual(self.calls[-1][2]['service_name'], 'service')
        with self.assertRaisesRegex(ValueError, 'Accept'):
            self.behavior.start(self.version, 'service', 'start', 'end')
        self.responses.append(self.program)
        self.assertEqual(self.behavior.inspect_program(self.version)['source'], self.source)
        self.responses.append({'diagnostic_version': self.version, 'acceptance_id': 'accepted-1'})
        self.behavior.accept(self.version)
        self.responses.append({'execution_id': self.execution, 'status': 'PENDING'})
        self.behavior.start(self.version, 'service', '2026-10-01T00:00:00Z', '2026-10-02T00:00:00Z')
        self.assertNotIn('org', self.calls[-1][2])
        raw = {'trace_id': 'e' * 32, 'verdict': 'NON_MATCH', 'execution_identity': {
            'diagnostic_version': self.version, 'udf_sha256': self.program['source_sha256'],
            'profile_sha256': 'c' * 64, 'adapter_sha256': 'd' * 64},
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
        self.responses.append({'diagnostic_version': 'f' * 64, 'acceptance_id': 'wrong'})
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.behavior.accept(self.version)
        self.assertNotIn('acceptance_id', json.loads(self.behavior.program_path(self.version).read_text()))
        with self.assertRaisesRegex(ValueError, 'Inspect'):
            self.behavior.accept('f' * 64)

    def test_sdk_tool_and_no_frozen_default(self):
        self.responses.append({'sdk_documentation': 'evaluate(trace, recorder, jev)'})
        self.assertIn('sdk_documentation', self.behavior.sdk())
        self.assertEqual(self.calls[-1][1], '/api/v1/behavior-programs/sdk')
        schemas = {tool['name']: tool['inputSchema'] for tool in tools_list(self.behavior)['tools']}
        self.assertEqual(schemas['execute_behavior']['required'], ['accepted_behavior', 'population', 'start', 'end'])
        self.assertIn('udf_source', schemas['compile_behavior']['required'])
        self.assertIsNone(self.behavior.version)
        self.assertFalse(self.behavior.legacy)

    def test_authoring_transport_allows_bounded_server_teaching_checks(self):
        for method, path, expected in [
            ('POST', '/api/v1/behavior-programs/compile', 310),
            ('POST', '/api/v1/behavior-programs/' + self.version + '/accept', 310),
            ('GET', '/api/v1/behavior-programs/' + self.version, 60),
            ('POST', '/api/v1/behavior-executions', 60),
        ]:
            with self.subTest(path=path), patch.dict('os.environ', {'CARDINAL_MCP_API_KEY': 'test-key'}), \
                    patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(b'{}')) as opened:
                Behavior.request(self.behavior, method, path)
                self.assertEqual(opened.call_args.kwargs['timeout'], expected)
