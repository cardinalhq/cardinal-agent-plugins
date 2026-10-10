import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.llm import Model, ModelProtocolError, strict_json_loads
from compilation.native_corpus import (NativeCorpus, ReviewProtocolError, blind_review,
                                       validate_review_protocol)
from compilation.storage import digest, read


class ReviewProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.packet = {'contract': {'question': 'Was the action authorized?'},
                       'sources': {'instructions': 'Require authorization'},
                       'cases': [{'id': 'behavior-audit-0', 'trace': {'input': 'Please act'}}]}
        self.valid = {'reviews': [{'id': 'behavior-audit-0', 'label': 'MATCH',
            'fidelity_pass': True, 'oracle_status': 'JUSTIFIED', 'reason': 'Observed request',
            'citations': [{'ref': '/cases/0/trace/input', 'quote': 'Please act'}]}]}

    def run_review(self, responses):
        calls = []
        def call(name, role, instruction, packet):
            calls.append((name, role, instruction, copy.deepcopy(packet)))
            result = responses[len(calls)-1]
            if isinstance(result, Exception):
                raise result
            return result
        result = blind_review(SimpleNamespace(call=call), 'blind', 'review instructions',
                              self.packet, self.root)
        return result, calls

    def test_malformed_json_and_wrong_ids_retry_identical_blind_packet(self):
        wrong = copy.deepcopy(self.valid)
        wrong['reviews'][0]['id'] = '0'
        result, calls = self.run_review([json.JSONDecodeError('bad JSON', '{', 1), wrong, self.valid])
        self.assertEqual(result, self.valid)
        self.assertEqual(len(calls), 3)
        self.assertEqual([c[0] for c in calls], ['blind', 'blind-protocol-retry-1', 'blind-protocol-retry-2'])
        for call in calls:
            self.assertEqual(call[1:], calls[0][1:])
            self.assertNotIn('expected', json.dumps(call[3]))
            self.assertNotIn('rationale', json.dumps(call[3]))
            self.assertNotIn('candidate_source', json.dumps(call[3]))
        record = read(self.root / 'blind-protocol-retry-1.json')
        self.assertEqual(record['response'], wrong)
        self.assertIn('received', record['error'])
        self.assertEqual(record['packet_sha256'], digest(self.packet))

    def test_duplicate_missing_and_extra_ids_are_protocol_failures(self):
        for ids in ([], ['behavior-audit-0'] * 2, ['unknown'], ['behavior-audit-0', 'unknown']):
            with self.subTest(ids=ids):
                response = {'reviews': [{**self.valid['reviews'][0], 'id': id_} for id_ in ids]}
                with self.assertRaises(ReviewProtocolError):
                    validate_review_protocol(response, self.packet)

    def test_wrong_types_and_missing_schema_fields_fail(self):
        malformed = [None, [], {}, {'reviews': {}}, {'reviews': [None]}]
        for key, value in [('fidelity_pass', 'true'), ('citations', [{'ref': '/trace'}]),
                           ('oracle_status', 'OK'), ('label', 'YES'), ('reason', None)]:
            bad = copy.deepcopy(self.valid)
            bad['reviews'][0][key] = value
            malformed.append(bad)
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ReviewProtocolError):
                validate_review_protocol(value, self.packet)

    def test_transport_budget_exhaustion_is_quarantined(self):
        calls = []
        def fail(name, *args):
            calls.append(name)
            raise ModelProtocolError('unavailable transport')
        with self.assertRaisesRegex(ReviewProtocolError, 'exhausted; corpus quarantined'):
            blind_review(SimpleNamespace(call=fail), 'blind', 'review', self.packet, self.root)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(list(self.root.glob('*.json'))), 3)

    def test_semantic_failure_is_not_a_protocol_retry(self):
        bad = copy.deepcopy(self.valid)
        bad['reviews'][0].update(label='UNKNOWN', fidelity_pass=False, oracle_status='AMBIGUOUS',
                                 citations=[{'ref': '/wrong/path', 'quote': 'invented'}])
        result, calls = self.run_review([bad])
        self.assertEqual(result, bad)
        self.assertEqual(len(calls), 1)

    def test_security_failure_is_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, 'prohibited'):
            self.run_review([RuntimeError('prohibited tool use')])

    def test_native_corpus_keeps_gold_and_semantic_author_budget(self):
        labels = ['MATCH', 'NON_MATCH', 'UNKNOWN']
        specs = {'cases': [{'family': str(i), 'expected': label, 'rationale': 'author secret',
            'tags': [], 'events': [{'kind': 'agent', 'name': 'root', 'start_ns': 1, 'end_ns': 2,
                                    'input': 'observed'}]} for i, label in enumerate(labels)]}
        calls = []
        def call(name, role, instruction, packet):
            calls.append((role, copy.deepcopy(packet)))
            if role == 'native-corpus-author':
                return copy.deepcopy(specs)
            self.assertEqual(set(packet), {'contract', 'sources', 'cases'})
            for case in packet['cases']:
                self.assertEqual(set(case), {'id', 'trace'})
            # Valid protocol, intentionally adverse fidelity judgment.
            return {'reviews': [{'id': c['id'], 'label': labels[i], 'fidelity_pass': False,
                'oracle_status': 'JUSTIFIED', 'reason': 'semantic fidelity disagreement',
                'citations': [{'ref': f'/cases/{i}/trace/events/0/input', 'quote': 'observed'}]}
                for i, c in enumerate(packet['cases'])]}
        corpus = NativeCorpus(self.root, 'behavior', self.packet['contract'],
            {'source_revision': 'rev', 'agent_id': 'agent', 'sources': {}, 'traces': []},
            SimpleNamespace(call=call), lambda trace: None)
        with self.assertRaisesRegex(ValueError, 'independent review failed'):
            corpus.make('audit', 1)
        self.assertEqual([role for role, _ in calls], ['native-corpus-author',
            'independent-native-corpus-review', 'native-corpus-author', 'independent-native-corpus-review'])
        for attempt in range(2):
            stored = read(self.root / f'audit-attempt-{attempt}.json')
            self.assertEqual([c['expected'] for c in stored['cases']], labels)
            self.assertFalse(any(c['review_pass'] for c in stored['cases']))
        self.assertFalse((self.root / 'audit.json').exists())

    def test_native_wire_retry_never_regenerates_cases_or_changes_gold(self):
        labels = ['MATCH', 'NON_MATCH', 'UNKNOWN']
        specs = {'cases': [{'family': str(i), 'expected': label, 'rationale': 'author secret',
            'tags': [], 'events': [{'kind': 'agent', 'name': 'root', 'start_ns': 1, 'end_ns': 2,
                                    'input': 'observed'}]} for i, label in enumerate(labels)]}
        author_calls, blind_packets = [], []
        def call(name, role, instruction, packet):
            if role == 'native-corpus-author':
                author_calls.append(name)
                return copy.deepcopy(specs)
            blind_packets.append(copy.deepcopy(packet))
            if len(blind_packets) == 1:
                raise json.JSONDecodeError('malformed response', '{', 1)
            response = {'reviews': [{'id': c['id'], 'label': labels[i], 'fidelity_pass': True,
                'oracle_status': 'JUSTIFIED', 'reason': 'evidence',
                'citations': [{'ref': f'/cases/{i}/trace/events/0/input', 'quote': 'observed'}]}
                for i, c in enumerate(packet['cases'])]}
            if len(blind_packets) == 2:
                response['reviews'][0]['id'] = 'wrong'
            return response
        corpus = NativeCorpus(self.root, 'behavior', self.packet['contract'],
            {'source_revision': 'rev', 'agent_id': 'agent', 'sources': {}, 'traces': []},
            SimpleNamespace(call=call), lambda trace: None)
        result = corpus.make('audit', 1)
        self.assertEqual(len(author_calls), 1)
        self.assertEqual(len(blind_packets), 3)
        self.assertEqual(blind_packets[0], blind_packets[1])
        self.assertEqual(blind_packets[1], blind_packets[2])
        self.assertEqual([c['expected'] for c in result], labels)
        self.assertTrue(all(c['review_pass'] for c in result))
        self.assertEqual(read(self.root / 'audit.json'), result)

    def test_native_three_wire_failures_never_produce_qualifiable_corpus(self):
        labels = ['MATCH', 'NON_MATCH', 'UNKNOWN']
        specs = {'cases': [{'family': str(i), 'expected': label, 'rationale': 'author secret',
            'tags': [], 'events': [{'kind': 'agent', 'name': 'root', 'start_ns': 1, 'end_ns': 2,
                                    'input': 'observed'}]} for i, label in enumerate(labels)]}
        author_calls, blind_packets = [], []
        def call(name, role, instruction, packet):
            if role == 'native-corpus-author':
                author_calls.append(name)
                return copy.deepcopy(specs)
            blind_packets.append(copy.deepcopy(packet))
            return {'reviews': []}
        corpus = NativeCorpus(self.root, 'behavior', self.packet['contract'],
            {'source_revision': 'rev', 'agent_id': 'agent', 'sources': {}, 'traces': []},
            SimpleNamespace(call=call), lambda trace: None)
        with self.assertRaisesRegex(ReviewProtocolError, 'exhausted; corpus quarantined'):
            corpus.make('audit', 1)
        self.assertEqual(len(author_calls), 1)
        self.assertEqual(len(blind_packets), 3)
        self.assertTrue(all(packet == blind_packets[0] for packet in blind_packets))
        self.assertFalse((self.root / 'audit.json').exists())
        records = list(self.root.glob('behavior-audit-review-*.json'))
        self.assertEqual(len(records), 3)
        self.assertTrue(all(read(path)['status'] == 'PROTOCOL_FAILURE' for path in records))
        self.assertEqual([spec['expected'] for spec in specs['cases']], labels)

    def events(self, response):
        return '\n'.join(json.dumps(event) for event in [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': response}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}])

    def test_model_native_schema_and_raw_invalid_json_receipts(self):
        def run(command, **kwargs):
            self.assertIn('--output-schema', command)
            schema = read(command[command.index('--output-schema') + 1])
            item = schema['properties']['reviews']['items']
            self.assertIn('oracle_status', item['required'])
            self.assertEqual(item['properties']['id']['enum'], ['behavior-audit-0'])
            return SimpleNamespace(stdout=self.events('{malformed'), stderr='raw stderr', returncode=0)
        with patch('compilation.llm.subprocess.run', side_effect=run):
            with self.assertRaisesRegex(ModelProtocolError, 'invalid model JSON'):
                Model(self.root).call('call', 'independent-native-corpus-review', 'review', self.packet)
        self.assertIn('{malformed', (self.root / 'call/events.jsonl').read_text())
        self.assertEqual(read(self.root / 'call/failure.json')['status'], 'INVALID_JSON')
        self.assertEqual(read(self.root / 'call/receipt.json')['response_schema'], 'native-review-schema-v1')

    def test_model_timeout_preserves_partial_events_and_attempt_receipt(self):
        error = subprocess.TimeoutExpired('codex', 1, output=b'partial events', stderr=b'partial stderr')
        with patch('compilation.llm.subprocess.run', side_effect=error):
            with self.assertRaises(ModelProtocolError):
                Model(self.root).call('call', 'independent-native-corpus-review', 'review', self.packet)
        self.assertEqual((self.root / 'call/events.jsonl').read_text(), 'partial events')
        self.assertEqual((self.root / 'call/stderr.txt').read_text(), 'partial stderr')
        receipt = read(self.root / 'call/receipt.json')
        self.assertEqual(receipt['model_invocations'], 1)
        self.assertIsNone(receipt['usage'])

    def test_model_malformed_event_stream_preserves_failure(self):
        proc = SimpleNamespace(stdout='not JSON\n' + self.events('{}'), stderr='', returncode=0)
        with patch('compilation.llm.subprocess.run', return_value=proc):
            with self.assertRaises(ModelProtocolError):
                Model(self.root).call('call', 'independent-native-corpus-review', 'review', self.packet)
        self.assertEqual(read(self.root / 'call/failure.json')['status'], 'TRANSPORT_ERROR')
        self.assertTrue(read(self.root / 'call/receipt.json')['event_errors'])

    def test_nonfinite_and_duplicate_json_retry_with_failure_receipts(self):
        responses = ('{"reviews": NaN}', '{"reviews": Infinity}', '{"reviews": -Infinity}',
                     '{"reviews": 1e999}', '{"reviews": -1e999}',
                     '{"reviews": [], "reviews": []}',
                     '{"reviews": [{"id":"a", "id":"b"}]}')
        for index, malformed in enumerate(responses):
            with self.subTest(response=malformed):
                root = self.root / str(index)
                model = Model(root / 'calls')
                outputs = [SimpleNamespace(stdout=self.events(text), stderr='', returncode=0)
                           for text in (malformed, json.dumps(self.valid))]
                with patch('compilation.llm.subprocess.run', side_effect=outputs) as transport:
                    result = blind_review(model, 'review', 'review', self.packet, root / 'protocol')
                self.assertEqual(result, self.valid)
                self.assertEqual(transport.call_count, 2)
                first, second = root / 'calls/review', root / 'calls/review-protocol-retry-1'
                self.assertEqual(read(first / 'request.json'), read(second / 'request.json'))
                self.assertEqual(read(first / 'failure.json')['status'], 'INVALID_JSON')
                self.assertTrue((first / 'receipt.json').exists())
                self.assertFalse((first / 'response.json').exists())
                self.assertEqual(read(root / 'protocol/review.json')['error_type'], 'ModelProtocolError')

    def test_strict_json_preserves_valid_numbers_and_nested_objects(self):
        text = '{"values":[null,true,false,0,-3,1.5,1e20,1e-999],"nested":{"a":"é"}}'
        self.assertEqual(strict_json_loads(text), json.loads(text))

    def test_ambiguous_or_nonfinite_event_stream_is_transport_failure(self):
        for index, line in enumerate(('{"type":"turn.completed","usage":{"tokens":1e999}}',
                                      '{"type":"turn.completed","type":"turn.failed"}')):
            with self.subTest(line=line):
                root = self.root / str(index)
                proc = SimpleNamespace(stdout=line + '\n' + self.events('{}'), stderr='', returncode=0)
                with patch('compilation.llm.subprocess.run', return_value=proc):
                    with self.assertRaises(ModelProtocolError):
                        Model(root).call('call', 'independent-native-corpus-review', 'review', self.packet)
                self.assertEqual(read(root / 'call/failure.json')['status'], 'TRANSPORT_ERROR')
                self.assertTrue(read(root / 'call/receipt.json')['event_errors'])


if __name__ == '__main__':
    unittest.main()
