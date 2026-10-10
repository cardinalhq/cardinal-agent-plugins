from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.native_corpus import NativeCorpus
from compilation.retained_corpus import RetainedCorpus, RetainedCorpusRejected
from compilation.storage import digest, filehash, read, write


class RetainedCorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'old/discovery-attempt-1.json'
        self.labels = ['MATCH', 'NON_MATCH', 'UNKNOWN']
        self.cases = [{'id': f'original-discovery-{i}', 'family': str(i), 'expected': label,
            'rationale': 'SECRET AUTHOR RATIONALE', 'tags': [], 'synthetic': True,
            'trace': {'trace_id': str(i), 'events': [{'input': f'observed {i}'}]},
            'review': {'reason': 'SECRET HISTORICAL REVIEW'}, 'review_pass': False,
            'oracle_status': 'AMBIGUOUS'} for i, label in enumerate(self.labels)]
        for case in self.cases:
            case['trace_sha256'] = digest(case['trace'])
        write(self.source, {'cases': self.cases, 'failures': ['old malformed reviewer']})
        self.config = {'path': str(self.source), 'sha256': filehash(self.source)}
        self.calls = []
        self.modifier = lambda response: response
        def call(name, role, instruction, packet):
            self.calls.append((name, role, instruction, deepcopy(packet)))
            response = {'reviews': [{'id': case['id'], 'label': self.labels[i],
                'fidelity_pass': True, 'oracle_status': 'JUSTIFIED', 'reason': 'fresh raw evidence',
                'citations': [{'ref': f'/cases/{i}/trace/events/0/input', 'quote': f'observed {i}'}]}
                for i, case in enumerate(packet['cases'])]}
            return self.modifier(response)
        self.native_calls = []
        self.corpus = RetainedCorpus(self.root / 'new', 'fresh-job', {'description': 'frozen question'},
            {'source_revision': 'rev', 'sources': {'instructions': 'policy'}, 'traces': []},
            SimpleNamespace(call=call), lambda trace: self.native_calls.append(deepcopy(trace)),
            retained_discovery=self.config)

    def test_fixed_gold_and_exact_traces_fresh_blind_review_and_original_unchanged(self):
        result = self.corpus.make('discovery', 1, candidate_source='SECRET PROGRAM')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.native_calls), 3)
        packet = self.calls[0][3]
        self.assertEqual(set(packet), {'contract', 'sources', 'cases'})
        self.assertTrue(all(set(c) == {'id', 'trace'} for c in packet['cases']))
        self.assertNotIn('SECRET', str(packet))
        self.assertEqual([c['trace'] for c in result], [c['trace'] for c in self.cases])
        self.assertEqual([c['expected'] for c in result], self.labels)
        self.assertEqual([c['id'] for c in result], [c['id'] for c in self.cases])
        self.assertTrue(all(c['review_pass'] for c in result))
        self.assertEqual(read(self.root / 'new/discovery.json'), result)
        self.assertEqual(filehash(self.source), self.config['sha256'])

    def test_disagreement_quarantines_full_set_without_gold_mutation_or_author_call(self):
        def disagreement(response):
            response['reviews'][0]['label'] = 'NON_MATCH'
            return response
        self.modifier = disagreement
        with self.assertRaisesRegex(RetainedCorpusRejected, 'FIXED_GOLD_DISAGREEMENT'):
            self.corpus.make('discovery', 1)
        outcome = read(self.root / 'new/retained-discovery-review.json')
        self.assertEqual(len(outcome['cases']), 3)
        self.assertEqual([c['expected'] for c in outcome['cases']], self.labels)
        self.assertEqual(outcome['failures'][0]['expected'], 'MATCH')
        self.assertEqual(outcome['failures'][0]['reviewed_label'], 'NON_MATCH')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.root / 'new/discovery.json').exists())

    def test_ambiguity_fidelity_and_citation_failures_are_reported_explicitly(self):
        def reject(response):
            response['reviews'][0]['oracle_status'] = 'AMBIGUOUS'
            response['reviews'][1]['fidelity_pass'] = False
            response['reviews'][2]['citations'][0]['quote'] = 'invented'
            return response
        self.modifier = reject
        with self.assertRaises(RetainedCorpusRejected):
            self.corpus.make('discovery', 1)
        outcome = read(self.root / 'new/retained-discovery-review.json')
        self.assertEqual({f['code'] for f in outcome['failures']},
            {'ORACLE_AMBIGUOUS', 'SOURCE_FIDELITY_FAILED', 'EVIDENCE_CITATION_FAILED'})
        self.assertEqual(len(outcome['cases']), 3)
        self.assertFalse((self.root / 'new/discovery.json').exists())

    def test_protocol_retries_do_not_expose_gold_or_change_cases(self):
        def malformed_first(response):
            if len(self.calls) < 3:
                response['reviews'][0]['id'] = 'incorrect'
            return response
        self.modifier = malformed_first
        result = self.corpus.make('discovery', 1)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(call[3] == self.calls[0][3] for call in self.calls))
        self.assertEqual([c['expected'] for c in result], self.labels)

    def test_protocol_exhaustion_keeps_full_quarantined_input(self):
        self.modifier = lambda response: {'reviews': []}
        with self.assertRaisesRegex(RetainedCorpusRejected, 'protocol failed'):
            self.corpus.make('discovery', 1)
        result = read(self.root / 'new/retained-discovery-review.json')
        self.assertEqual(len(result['cases']), 3)
        self.assertEqual(result['failures'][0]['code'], 'REVIEW_PROTOCOL_FAILED')
        self.assertEqual(len(self.calls), 3)
        self.assertFalse((self.root / 'new/discovery.json').exists())

    def test_hash_drift_and_count_reject_before_model(self):
        with self.assertRaisesRegex(RetainedCorpusRejected, 'count'):
            self.corpus.make('discovery', 2)
        self.source.write_text('{}')
        with self.assertRaisesRegex(RetainedCorpusRejected, 'hash mismatch'):
            self.corpus.make('discovery', 1)
        self.assertEqual(self.calls, [])

    def test_class_balance_and_duplicate_identity_reject_before_model(self):
        for mutation, message in ((lambda c: c[0].update(expected='UNKNOWN'), 'class balance'),
                                  (lambda c: c[0].update(id=c[1]['id']), 'duplicate case'),
                                  (lambda c: c[0].update(family=c[1]['family']), 'duplicate families'),
                                  (lambda c: c[0].update(trace_sha256='wrong'), 'trace hash')):
            with self.subTest(message=message):
                changed = deepcopy(self.cases); mutation(changed)
                import json
                self.source.write_text(json.dumps({'cases': changed}))
                self.corpus.retained_discovery['sha256'] = filehash(self.source)
                with self.assertRaisesRegex(RetainedCorpusRejected, message):
                    self.corpus.make('discovery', 1)
        self.assertEqual(self.calls, [])

    def test_native_validation_precedes_fresh_review(self):
        def invalid(trace):
            raise ValueError('invalid native telemetry')
        self.corpus.native_validate = invalid
        with self.assertRaisesRegex(RetainedCorpusRejected, 'native validation failed'):
            self.corpus.make('discovery', 1)
        self.assertEqual(self.calls, [])
        result = read(self.root / 'new/retained-discovery-review.json')
        self.assertEqual(len(result['failures']), 3)
        self.assertEqual(len(result['cases']), 3)

    def test_challenge_and_hidden_audit_delegate_unchanged(self):
        for split in ('challenge-1', 'audit'):
            with patch.object(NativeCorpus, 'make', return_value=['fresh']) as fresh:
                self.assertEqual(self.corpus.make(split, 4, 'candidate', ['old-family']), ['fresh'])
                fresh.assert_called_once_with(split, 4, 'candidate', ['old-family'])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
