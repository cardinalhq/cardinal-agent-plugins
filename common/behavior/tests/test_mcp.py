"""Large MCP tool text must remain readable after the client spills it to a file."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class MCPSerializationTests(unittest.TestCase):
    def test_large_teaching_receipt_can_be_read_in_lines(self):
        # Exercise the real MCP serializer, without executing or changing a program.
        result = {
            'receipt_file': '/tmp/behavior/teaching/receipt.json',
            'diagnostic_version': 'a' * 64,
            'results': [
                {
                    'trace_id': format(index, '032x'),
                    'verdict': 'MATCH',
                    'records': [{'op': 'witness', 'reason': 'Résumé evidence ' * 24}
                                for _ in range(250)],
                    'jev_receipts': [{'decision': 'YES', 'reason': 'Observed evidence ' * 24}
                                     for _ in range(250)],
                }
                for index in range(6)
            ],
        }
        self.assertGreater(len(json.dumps(result, ensure_ascii=False)), 1_000_000)
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'test_behavior', 'arguments': {}}}
        output = io.StringIO()
        with patch.object(sys, 'argv', ['cardinal-behavior']), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(request) + '\n')), \
                patch.object(sys, 'stdout', output), \
                patch.object(server, 'default_config', return_value={}), \
                patch.object(server, 'Behavior') as behavior:
            behavior.return_value.test.return_value = result
            server.main()
        # The outer JSON-RPC transport stays one line; the text content is pageable.
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        response = json.loads(output.getvalue())
        text = response['result']['content'][0]['text']
        self.assertEqual(json.loads(text), result)
        lines = text.splitlines()
        self.assertGreater(len(lines), 10_000)
        self.assertLess(max(map(len, lines)), 1024)
        self.assertIn(result['receipt_file'], '\n'.join(lines[:10]))
        self.assertTrue(all(len('\n'.join(lines[offset:offset + 20])) < 25_000
                            for offset in range(0, len(lines), 20)))


if __name__ == '__main__':
    unittest.main()
