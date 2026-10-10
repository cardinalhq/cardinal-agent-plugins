import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.diagnosis import Diagnoser, NO_CODE, ROUTES, packet, validate
from compilation.engine import Qualifier, Policy, check_cases
from compilation.storage import digest, read
from diagnosis_helpers import mechanical_diagnosis


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / 'program.py'; source.write_text('def evaluate(run): pass')
        self.candidate = {'version': 'original', 'source': str(source)}
        self.case = {'id': 'dev', 'family': 'optional', 'expected': 'NON_MATCH', 'review_pass': True,
                     'trace': {'trace_id': 'dev', 'signal': 'optional ranking; required count satisfied'}}
        self.execution = {'actual': 'MATCH', 'mechanical_pass': True, 'audit': {'errors': []},
            'jev_receipts': [{'proposition': 'Are all binding requirements satisfied?', 'decision': 'NO',
                             'evidence': [{'text': 'optional ranking; required count satisfied'}]}]}
        self.item = {'case': self.case, 'execution': self.execution}
        self.d = mechanical_diagnosis(self.candidate, self.item, 1)

    def jev(self):
        d = copy.deepcopy(self.d)
        d['earliest']['classification'] = 'JEV_ANSWER'
        d['intervention'] = ROUTES['JEV_ANSWER']
        r = self.execution['jev_receipts'][0]
        d['jev_review'] = [{'receipt_index': 0, 'proposition': r['proposition'],
            'evidence_sha256': digest(r['evidence']), 'actual_answer': 'NO', 'expected_answer': 'YES',
            'proposition_adequate': True, 'explanation': 'Explicit optionality excludes ranking.',
            'citations': [{'ref': '/execution/jev_receipts/0/evidence/0/text', 'quote': 'optional ranking'}]}]
        return d

    def test_nonexistent_evidence_or_fabricated_quote_rejected(self):
        for citation in ({'ref': '/trace/absent', 'quote': 'x'},
                         {'ref': '/trace/signal', 'quote': 'invented'}):
            d = copy.deepcopy(self.d); d['scope']['citations'] = [citation]
            with self.assertRaises(ValueError): validate(d, self.candidate, self.item)

    def test_jev_requires_actual_proposition_packet_and_answer(self):
        good = self.jev()
        self.assertFalse(validate(good, self.candidate, self.item)['repair_allowed'])
        for key, value in [('proposition', 'different'), ('evidence_sha256', 'bad'),
                           ('actual_answer', 'YES'), ('proposition_adequate', False), ('citations', [])]:
            d = copy.deepcopy(good); d['jev_review'][0][key] = value
            with self.assertRaises(ValueError): validate(d, self.candidate, self.item)
        good['jev_review'] = []
        with self.assertRaises(ValueError): validate(good, self.candidate, self.item)

    def test_missing_evidence_never_proves_non_match(self):
        d = copy.deepcopy(self.d)
        d['expected']['basis'] = 'MISSING_EVIDENCE'
        with self.assertRaisesRegex(ValueError, 'definitive'): validate(d, self.candidate, self.item)

    def test_ambiguous_or_disagreeing_oracle_cannot_authorize_code(self):
        d = copy.deepcopy(self.d); d['independent_review']['oracle_status'] = 'AMBIGUOUS'
        with self.assertRaisesRegex(ValueError, 'ambiguous oracle'): validate(d, self.candidate, self.item)
        d = copy.deepcopy(self.d); d['independent_review']['verdict'] = 'MATCH'
        with self.assertRaisesRegex(ValueError, 'ambiguous oracle'): validate(d, self.candidate, self.item)
        with self.assertRaisesRegex(ValueError, 'ambiguous oracle'):
            check_cases([{**self.case, 'oracle_status': 'AMBIGUOUS'}])

    def test_stale_source_trace_or_execution_rejected(self):
        for field in ('trace', 'actual'):
            item = copy.deepcopy(self.item)
            if field == 'trace': item['case']['trace']['signal'] = 'changed'
            else: item['execution']['actual'] = 'UNKNOWN'
            with self.assertRaisesRegex(ValueError, 'does not belong'): validate(self.d, self.candidate, item)
        Path(self.candidate['source']).write_text('changed')
        with self.assertRaises(ValueError): validate(self.d, self.candidate, self.item)

    def controller(self, diagnosis, repairs):
        return Qualifier(self.root/'q', Policy(max_repairs=3), lambda c,t: self.execution,
            lambda c,r: [], lambda *a: repairs.append(a), lambda: self.fail('audit must not run'),
            diagnose=diagnosis)

    def bound(self, d, item):
        d = copy.deepcopy(d); d['packet_sha256'] = digest(packet(self.candidate, item))
        return d

    def test_jev_answer_and_no_code_preserve_incumbent_without_attempt(self):
        repairs = []
        q = self.controller(lambda c,i,r: self.bound(self.jev(), i), repairs)
        result = q.run(self.candidate, [self.case])
        self.assertEqual(result['repairs'], 0); self.assertEqual(repairs, [])
        self.assertEqual(result['candidate'], self.candidate)
        self.assertEqual(result['reason'], 'no_code_repair_justified')

    def test_explicit_no_code_and_insufficient_evidence_preserve_incumbent(self):
        for classification in ('VERDICT_COMPOSITION', 'INSUFFICIENT_EVIDENCE'):
            with self.subTest(classification=classification):
                self.root = self.root / classification; self.root.mkdir()
                d = copy.deepcopy(self.d); d['intervention'] = NO_CODE
                d['earliest']['classification'] = classification
                repairs = []; q = self.controller(lambda c,i,r: self.bound(d, i), repairs)
                result = q.run(self.candidate, [self.case])
                self.assertEqual(result['candidate'], self.candidate); self.assertEqual(repairs, [])

    def test_oracle_ambiguity_quarantines_fixture_before_any_repair(self):
        d = copy.deepcopy(self.d); d['earliest']['classification'] = 'ORACLE_AMBIGUITY'
        d['intervention'] = 'REJECT_FIXTURE'; d['independent_review']['oracle_status'] = 'AMBIGUOUS'
        repairs = []; q = self.controller(lambda c,i,r: self.bound(d, i), repairs)
        result = q.run(self.candidate, [self.case])
        self.assertEqual(result['reason'], 'oracle_ambiguity'); self.assertEqual(repairs, [])
        self.assertEqual(read(self.root/'q/quarantined-fixtures.json'), ['dev'])

    def test_sdk_runtime_supplies_compatible_feedback(self):
        self.execution.update(actual='ERROR', mechanical_pass=False, reason="TraceView.attrs invalid")
        d = mechanical_diagnosis(self.candidate, self.item, 1)
        d['earliest']['classification'] = 'SDK_RUNTIME'; d['intervention'] = 'SDK_COMPATIBLE_REPAIR'
        repairs = []
        def repair(candidate, feedback, revision):
            repairs.append(revision)
            self.assertEqual(feedback[0]['diagnosis']['intervention'], 'SDK_COMPATIBLE_REPAIR')
            self.assertIn('TraceView.attrs', feedback[0]['diagnosis']['observed']['execution']['reason'])
            from compilation.engine import RepairFailure
            raise RepairFailure({'errors': ['invalid SDK access']})
        q = self.controller(lambda c,i,r: self.bound(d, i), repairs); q.repair = repair
        result = q.run(self.candidate, [self.case])
        self.assertEqual(repairs, [1,2,3]); self.assertEqual(result['repairs'], 3)
        self.assertEqual(result['candidate'], self.candidate)

    def test_missing_diagnoser_fails_closed(self):
        q = self.controller(None, [])
        self.assertEqual(q.run(self.candidate, [self.case])['reason'], 'diagnosis_required')

    def test_jev_cannot_route_to_python(self):
        d = self.jev(); d['intervention'] = 'TARGETED_PYTHON_REPAIR'
        with self.assertRaisesRegex(ValueError, 'policy'): validate(d, self.candidate, self.item)

    def test_raw_review_is_source_prediction_and_gold_blind(self):
        calls = []
        def call(name, role, instruction, payload):
            calls.append((role,payload))
            if role == 'independent-diagnosis-gold':
                self.assertEqual(set(payload), {'trace','contract'})
                self.assertEqual(payload['trace'], self.case['trace'])
                return self.d['independent_review']
            return copy.deepcopy(self.d)
        model = type('Model', (), {'call': staticmethod(call)})()
        diagnose = Diagnoser(self.root/'diagnoses', model, {}, 'SDK interfaces')
        diagnose(self.candidate, self.item, 1); diagnose(self.candidate, self.item, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]['execution']['jev_receipts'], self.execution['jev_receipts'])


if __name__ == '__main__': unittest.main()
