import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""Mechanical controller regressions; no model calls or new qualification experiment."""
import copy
import dataclasses
import functools
import tempfile
import unittest
from pathlib import Path

from compilation.engine import Policy, Qualifier, RepairFailure, promotion_gate
from compilation.storage import digest, read


def cases(prefix):
    return [{'id': prefix + str(i), 'family': prefix + str(i), 'expected': label,
             'trace': {'trace_id': prefix + str(i), 'signal': label, 'episode': prefix},
             'review_pass': True}
            for i, label in enumerate(('MATCH', 'NON_MATCH', 'UNKNOWN'))]


def output(actual, mechanical=True):
    return {'actual': actual, 'mechanical_pass': mechanical}


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'q'
        self.initial = {'version': 'incumbent'}
        self.calls = []

    def controller(self, proposed, repair=None, max_repairs=1, challenge=None):
        def evaluate(candidate, trace):
            self.calls.append((candidate['version'], trace['trace_id']))
            return (output('UNKNOWN') if candidate['version'] == 'incumbent'
                    else proposed(candidate, trace))
        return Qualifier(self.root, Policy(max_repairs=max_repairs, audit_per_label=1),
                         evaluate, challenge or (lambda c, r: cases('challenge' + str(r))),
                         repair or (lambda c, f, r: {'version': 'proposal'}),
                         lambda: cases('audit'))

    def assert_preserved(self, result, revision=1):
        self.assertEqual(result['candidate'], self.initial)
        self.assertEqual(result['status'], 'CANNOT_QUALIFY')
        self.assertEqual(read(self.root / f'candidate-{revision}.json'), self.initial)
        self.assertFalse(read(self.root / f'promotion-{revision}.json')['passed'])
        self.assertFalse((self.root / 'audit-cases.json').exists())

    def test_error_at_first_or_last_case_runs_complete_suite_and_preserves_incumbent(self):
        for error_id in ('discovery0', 'challenge12'):
            with self.subTest(error_id=error_id):
                self.root = Path(self.temp.name) / error_id
                self.calls = []
                q = self.controller(lambda c, t: output('ERROR' if t['trace_id'] == error_id
                                                        else t['signal']))
                result = q.run(self.initial, cases('discovery'))
                self.assert_preserved(result)
                expected = cases('discovery') + cases('challenge0') + cases('challenge1')
                self.assertEqual([i for v, i in self.calls if v == 'proposal'],
                                 [c['id'] for c in expected])
                # New challenge cases must also be checked against the incumbent.
                self.assertEqual([i for v, i in self.calls if v == 'incumbent'],
                                 [c['id'] for c in expected])
                self.assertEqual(read(self.root / 'proposal-1.json')['version'], 'proposal')

    def test_net_accuracy_gain_cannot_offset_unknown_regression(self):
        def proposed(c, t):
            return output('MATCH' if t['trace_id'] == 'discovery2' else t['signal'])
        result = self.controller(proposed).run(self.initial, cases('discovery'))
        self.assert_preserved(result)
        decision = read(self.root / 'promotion-1.json')
        self.assertEqual(decision['regressions'], ['discovery2'])
        self.assertEqual(decision['execution_failures'], [])
        self.assertEqual(read(self.root / 'development-gate-1.json')['correct'], 8)

    def test_mechanical_failure_blocks_even_correct_verdicts(self):
        q = self.controller(lambda c, t: output(t['signal'], t['trace_id'] != 'challenge10'))
        self.assert_preserved(q.run(self.initial, cases('discovery')))

    def test_regression_on_new_challenge_also_blocks_promotion(self):
        q = self.controller(lambda c, t: output('MATCH' if t['trace_id'] == 'challenge12'
                                                else t['signal']))
        self.assert_preserved(q.run(self.initial, cases('discovery')))
        self.assertEqual(read(self.root / 'promotion-1.json')['regressions'], ['challenge12'])

    def test_invalid_verdict_blocks_promotion(self):
        q = self.controller(lambda c, t: output('NOT_APPLICABLE' if t['signal'] == 'MATCH'
                                                else t['signal']))
        self.assert_preserved(q.run(self.initial, cases('discovery')))

    def test_evaluator_exception_is_error_and_does_not_short_circuit(self):
        def proposed(c, t):
            if t['trace_id'] == 'discovery0':
                raise AttributeError('missing SDK interface')
            return output(t['signal'])
        result = self.controller(proposed).run(self.initial, cases('discovery'))
        self.assert_preserved(result)
        rows = read(self.root / 'development-1.json')
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[0]['actual'], 'ERROR')
        self.assertIn('AttributeError', rows[0]['reason'])
        self.assertTrue(all(v is None for v in rows[0]['metrics'].values()))
        self.assertEqual(len([c for c in self.calls if c[0] == 'proposal']), 9)

    def test_monotone_partial_repair_can_promote_but_cannot_qualify(self):
        q = self.controller(lambda c, t: output('UNKNOWN' if t['signal'] == 'NON_MATCH'
                                                else t['signal']))
        result = q.run(self.initial, cases('discovery'))
        self.assertEqual(result['candidate']['version'], 'proposal')
        self.assertEqual(result['status'], 'CANNOT_QUALIFY')
        self.assertTrue(read(self.root / 'promotion-1.json')['passed'])
        self.assertFalse((self.root / 'audit-cases.json').exists())

    def test_rejected_then_fixed_uses_incumbent_and_all_previous_challenges(self):
        parents = []
        def repair(candidate, feedback, revision):
            parents.append(copy.deepcopy(candidate))
            if revision == 2:
                self.assertTrue(all(x['execution']['actual'] == 'ERROR' for x in feedback))
            candidate['version'] = 'bad' if revision == 1 else 'fixed'
            return candidate  # In-place mutation must not modify the incumbent descriptor.
        q = self.controller(lambda c, t: output('ERROR' if c['version'] == 'bad' else t['signal']),
                            repair=repair, max_repairs=2)
        result = q.run(self.initial, cases('discovery'))
        self.assertEqual(parents, [self.initial, self.initial])
        self.assertEqual(result['status'], 'QUALIFIED')
        self.assertEqual(result['candidate']['version'], 'fixed')
        self.assertEqual(read(self.root / 'candidate-1.json'), self.initial)
        self.assertEqual(len(read(self.root / 'development-2.json')), 12)
        self.assertEqual(read(self.root / 'qualification.json')['version'], 'fixed')

    def test_rejected_then_compile_failure_preserves_candidate_and_budget(self):
        def repair(candidate, feedback, revision):
            self.assertEqual(candidate, self.initial)
            if revision == 2:
                raise RepairFailure({'errors': ['compile failed']})
            return {'version': 'bad'}
        q = self.controller(lambda c, t: output('ERROR'), repair=repair, max_repairs=2)
        result = q.run(self.initial, cases('discovery'))
        self.assert_preserved(result)
        self.assertEqual(result['repairs'], 2)
        self.assertEqual(result['reason'], 'repair_compile_budget_exhausted')
        manifests = sorted(self.root.glob('candidate-*.json'))
        self.assertTrue(all(read(p) == self.initial for p in manifests))

    def test_promoted_incumbent_survives_later_failed_repair(self):
        parents = []
        def repair(c, f, r):
            parents.append(c['version'])
            return {'version': 'partial' if r == 1 else 'bad'}
        def proposed(c, t):
            return output('ERROR' if c['version'] == 'bad' else
                          ('UNKNOWN' if t['signal'] == 'NON_MATCH' else t['signal']))
        result = self.controller(proposed, repair, max_repairs=2).run(self.initial, cases('discovery'))
        self.assertEqual(parents, ['incumbent', 'partial'])
        self.assertEqual(result['candidate']['version'], 'partial')
        self.assertEqual(read(self.root / 'candidate-2.json')['version'], 'partial')
        self.assertEqual(len(read(self.root / 'development-2.json')), 12)

    def test_challenge_exception_leaves_recovery_manifest_on_incumbent(self):
        def challenge(c, r):
            if r:
                raise RuntimeError('challenge unavailable')
            return cases('challenge0')
        q = self.controller(lambda c, t: output(t['signal']), challenge=challenge)
        with self.assertRaisesRegex(RuntimeError, 'challenge unavailable'):
            q.run(self.initial, cases('discovery'))
        self.assertEqual(read(self.root / 'proposal-1.json')['version'], 'proposal')
        self.assertEqual([read(p) for p in self.root.glob('candidate-*.json')], [self.initial])

    def test_promotion_requires_identical_complete_suite(self):
        rows = [{'id': c['id'], 'trace_sha256': digest(c['trace']), 'expected': c['expected'],
                 'actual': c['expected'], 'mechanical_pass': True, 'version': 'v'}
                for c in cases('development')]
        variants = [rows[:-1], rows + [rows[0]], list(reversed(rows)),
                    [{**rows[0], 'trace_sha256': 'other'}, *rows[1:]],
                    [{**rows[0], 'expected': 'UNKNOWN'}, *rows[1:]]]
        self.assertTrue(promotion_gate(rows, rows)['passed'])
        for altered in variants:
            self.assertFalse(promotion_gate(rows, altered)['passed'])
        self.assertFalse(promotion_gate([], [])['passed'])


if __name__ == '__main__':
    unittest.main()
