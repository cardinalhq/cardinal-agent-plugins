from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.implementability import (COUNT_SENTENCE, assess_contract_implementability,
    revise_contract_if_needed, validate_revision)
from compilation.storage import digest, read


class ImplementabilityTests(unittest.TestCase):
    def setUp(self):
        self.contract = {'description': 'Original execution frequency question',
            'clauses': [{'kind': 'candidate_selection', 'statement':
                        'Report M/N and U/N. ' + COUNT_SENTENCE + ' Zero denominators are undefined.',
                        'citations': [{'ref': '/question', 'quote': 'Original question'}]},
                       {'kind': 'UNKNOWN', 'statement': 'Missing evidence is not proven absence.'}],
            'evidence_requirements': {'population': ['Independent enumeration and source verification']}}
        files = {
            'behavior_sdk/jev.py': '''# Typed extraction is intentionally outside v1.
# set(obj) != {"answer", "evidence_refs", "short_reason"}
# max_calls: int = 8
class Verdict:
    YES = "YES"
    NO = "NO"
    UNCERTAIN = "UNCERTAIN"
''',
            'behavior_sdk/native_profile.py': '# Each retained native span is one event.\n# No application-specific inferred',
            'behavior_sdk/recorder.py': '# evaluate(run: TraceView, recorder: Recorder, judge: JEV) -> None\n# def counter(',
        }
        artifact = {'version': '0.2.0', 'files': files}
        self.sdk = {'sdk_version': '0.2.0', 'artifact': artifact, 'sdk_artifact_sha256': digest(artifact)}
        self.sentence = ('Exact semantic decision counts and decision rates are unavailable; '
                         'never substitute span, message, tool, packet, or judge-call counts.')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_unverified_or_changed_api_cannot_authorize_revision(self):
        for modify in ('hash', 'schema'):
            sdk = deepcopy(self.sdk)
            sdk['artifact']['files']['behavior_sdk/jev.py'] += '\n# drift'
            if modify == 'schema':
                sdk['artifact']['files']['behavior_sdk/jev.py'] += '\n    EXTRACT = "EXTRACT"\n'
                sdk['sdk_artifact_sha256'] = digest(sdk['artifact'])
            with self.assertRaises(ValueError):
                assess_contract_implementability(self.contract, sdk)

    def test_unrecognized_contract_never_calls_model(self):
        contract = {'clauses': [{'statement': 'A different obligation.'}]}
        model = SimpleNamespace(call=lambda *a: self.fail('unexpected model call'))
        result = revise_contract_if_needed(self.root, contract=contract, question='Original question',
            sdk_documentation={}, model=model, job_id='job')
        self.assertEqual(result, contract)

    def test_changed_truth_conditions_or_denominators_rejected(self):
        report = assess_contract_implementability(self.contract, self.sdk)
        revised = deepcopy(self.contract)
        revised['clauses'][0]['statement'] = revised['clauses'][0]['statement'].replace(COUNT_SENTENCE, self.sentence)
        self.assertEqual(validate_revision(self.contract, revised, report), self.sentence)
        changed = deepcopy(revised); changed['clauses'][1]['statement'] = 'Missing means violation.'
        with self.assertRaisesRegex(ValueError, 'unauthorized'):
            validate_revision(self.contract, changed, report)
        changed = deepcopy(revised); changed['clauses'][0]['statement'] = changed['clauses'][0]['statement'].replace('M/N', 'M/S')
        with self.assertRaisesRegex(ValueError, 'preserved reporting'):
            validate_revision(self.contract, changed, report)

    def test_revision_requires_two_independent_calls_and_preserves_original(self):
        calls = []
        def call(name, role, instruction, payload):
            calls.append((role, payload))
            if role == 'sdk-contract-implementability-revision':
                return {'replacement_sentence': self.sentence, 'rationale': 'ABI lacks semantic extraction.'}
            return {**{k: True for k in ('approved', 'question_preserved', 'execution_frequency_preserved',
                    'original_semantics_preserved', 'unavailable_counts_disclosed', 'no_proxy_counts')},
                    'reason': 'Only unavailable count disclosure changes.'}
        original = deepcopy(self.contract)
        revised = revise_contract_if_needed(self.root, contract=self.contract, question='Original question',
            sdk_documentation=self.sdk, model=SimpleNamespace(call=call), job_id='job')
        self.assertEqual(self.contract, original)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], 'independent-contract-revision-review')
        self.assertEqual(calls[1][1]['question'], 'Original question')
        self.assertEqual(read(self.root / 'contract-original.json'), original)
        self.assertEqual(read(self.root / 'contract-revised.json'), revised)

    def test_rejected_review_never_writes_accepted_contract(self):
        calls = []
        def call(name, role, *args):
            calls.append(role)
            return {'replacement_sentence': self.sentence} if len(calls) == 1 else {'approved': False, 'reason': 'Unsupported.'}
        with self.assertRaisesRegex(ValueError, 'review rejected'):
            revise_contract_if_needed(self.root, contract=self.contract, question='Original question',
                sdk_documentation=self.sdk, model=SimpleNamespace(call=call), job_id='job')
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.root / 'contract-revised.json').exists())
        self.assertTrue((self.root / 'contract-revision-review.json').exists())


if __name__ == '__main__':
    unittest.main()
