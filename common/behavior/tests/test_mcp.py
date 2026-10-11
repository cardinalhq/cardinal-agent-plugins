"""Large MCP tool results must stay bounded on stdio and remain fully retained on disk."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def call(name, result, output):
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
               'params': {'name': name, 'arguments': {}}}
    stdout = io.StringIO()
    with patch.object(sys, 'argv', ['cardinal-behavior']), \
            patch.object(sys, 'stdin', io.StringIO(json.dumps(request) + '\n')), \
            patch.object(sys, 'stdout', stdout), \
            patch.object(server, 'default_config', return_value={}), \
            patch.object(server, 'Behavior') as behavior:
        behavior.return_value.output = output
        getattr(behavior.return_value, {'test_behavior': 'test', 'accept_behavior': 'accept',
                                        'inspect_behavior': 'inspect_program'}[name]).return_value = result
        server.main()
    # The JSON-RPC transport stays one bounded line.
    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1
    assert len(lines[0].encode('utf-8')) < 2 * server.MCP_TEXT_LIMIT
    return json.loads(json.loads(lines[0])['result']['content'][0]['text'])


def teaching_result(traces=8):
    results = [{'trace_id': format(index, '032x'), 'expected_verdict': 'MATCH', 'verdict': 'MATCH',
                'witness_refs': ['span-a'],
                'records': [{'op': 'violation', 'subject': 'claim', 'events': ['span-a'],
                             'reason': 'Résumé evidence ' * 24} for _ in range(250)],
                'jev_receipts': [{'decision': 'YES', 'reason': 'Observed evidence ' * 24,
                                  'evidence': [{'ref': 'span-a', 'text': 'x' * 4000}],
                                  'usage': {'input_tokens': 900, 'output_tokens': 40}}
                                 for _ in range(250)],
                'native_trace': {'spans': ['y' * 1000] * 1000},
                'semantic_occurrences': ['z' * 1000] * 700}
               for index in range(traces)]
    return {'receipt_file': '/tmp/behavior/teaching/receipt.json', 'diagnostic_version': 'a' * 64,
            'test_receipt': 'b' * 64, 'mechanical_errors': [], 'results': results,
            'receipt': {'results': results, 'native_traces': ['n' * 1_000_000] * 9}}


class MCPSerializationTests(unittest.TestCase):
    def test_teaching_result_returns_bounded_verdicts_and_receipt_path(self):
        result = teaching_result()
        self.assertGreater(len(json.dumps(result)), 20_000_000)
        with tempfile.TemporaryDirectory() as output:
            view = call('test_behavior', result, Path(output))
            self.assertEqual(list(Path(output).rglob('*')), [])
        self.assertEqual(view['receipt_file'], result['receipt_file'])
        self.assertEqual(view['test_receipt'], result['test_receipt'])
        self.assertNotIn('receipt', view)
        self.assertEqual([(r['trace_id'], r['expected_verdict'], r['verdict']) for r in view['results']],
                         [(r['trace_id'], 'MATCH', 'MATCH') for r in result['results']])
        first = view['results'][0]
        self.assertEqual((len(first['records']), first['record_count']), (32, 250))
        self.assertEqual((len(first['jev_decisions']), first['jev_receipt_count']), (8, 250))
        self.assertEqual(first['jev_decisions'][0]['usage'], {'input_tokens': 900, 'output_tokens': 40})
        self.assertNotIn('native_trace', first)
        self.assertNotIn('semantic_occurrences', first)

    def test_acceptance_returns_teaching_verdicts_only(self):
        teaching = teaching_result()['results']
        with tempfile.TemporaryDirectory() as output:
            view = call('accept_behavior', {'acceptance_id': 'c' * 64, 'teaching_results': teaching}, Path(output))
        self.assertEqual(view['acceptance_id'], 'c' * 64)
        self.assertEqual(view['teaching_results'][0],
                         {'trace_id': teaching[0]['trace_id'], 'expected_verdict': 'MATCH', 'verdict': 'MATCH'})

    def test_any_oversized_result_is_retained_on_disk_with_its_small_fields(self):
        result = {'diagnostic_version': 'd' * 64, 'source': 'q' * (2 * server.MCP_TEXT_LIMIT)}
        with tempfile.TemporaryDirectory() as output:
            view = call('inspect_behavior', result, Path(output))
            retained = Path(view['result_file'])
            self.assertTrue(retained.is_relative_to(Path(output)))
            self.assertEqual(json.loads(retained.read_text()), result)
            self.assertEqual(oct(retained.stat().st_mode & 0o777), '0o600')
        self.assertEqual(view['diagnostic_version'], 'd' * 64)
        self.assertEqual(view['omitted_fields'], ['source'])
        self.assertGreater(view['result_bytes'], server.MCP_TEXT_LIMIT)


if __name__ == '__main__':
    unittest.main()
