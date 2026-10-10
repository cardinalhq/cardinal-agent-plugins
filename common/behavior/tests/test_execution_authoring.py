import ast
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.autonomous import AutonomousLifecycle
from compilation.engine import RepairFailure
from compilation.execution_authoring import execution_regression_errors
from compilation.execution_authoring import load_development_teaching, evaluate_development
from compilation.sdk_adapter import SDK
from compilation.storage import digest, read, write, filehash
from compilation.wording_repair import apply_wording_patch, literal_judge_arguments, wording_patch_targets


def draft():
    source = ('CAMPAIGN = ' + repr('c' * 997) + '\nOTHER = ' + repr('o' * 1005)
        + '\nCAMPAIGN_FRAME = ' + repr('f' * 605) + '\nOTHER_FRAME = ' + repr('g' * 612)
        + '\ndef campaign(judge, items):\n return judge.decide(proposition=CAMPAIGN, evidence=items, frame=CAMPAIGN_FRAME)'
        + '\ndef other(judge, items):\n return judge.decide(proposition=OTHER, evidence=items, frame=OTHER_FRAME)'
        + '\ndef evaluate(run, recorder, judge):\n recorder.unknown("missing material context")\n')
    return {'udf_source': source, 'compile_plan': {'clauses': [{'text': 'frozen policy'}], 'judge_sites': [
        {'name': 'campaign', 'proposition': 'c' * 997}, {'name': 'other', 'proposition': 'o' * 1005}]}}


def wording():
    return {'replacements': [{'constant': 'OTHER', 'value': 'o' * 998},
                            {'constant': 'CAMPAIGN_FRAME', 'value': 'f' * 590},
                            {'constant': 'OTHER_FRAME', 'value': 'g' * 590}]}


def sdk(root):
    return SDK(root, profile_factory=Mock(), judge_factory=Mock(), program_runner=Mock(),
        audit_result=Mock(), metrics=Mock(), verify=Mock(), jev_config={}, population={},
        evaluation_policy={}, finality_policy={})


class ExecutionAuthoringTests(unittest.TestCase):
    def test_full_development_regression_reenters_canonical_parent_with_shared_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); calls = root/'calls'; calls.mkdir()
            for index in (0, 1):
                (calls/f'job-compiler-teaching-{index}').mkdir()
            # Slot 1 was rejected by transport before a proposal existed.
            write(calls/'job-compiler-teaching-0/response.json', {})
            source = root/'program.py'; source.write_text('def evaluate(run, recorder, judge):\n pass\n')
            write(root/'plan.json', {'clauses': [], 'judge_sites': []})
            parent = {'source': str(source), 'plan': str(root/'plan.json'), 'version': 'parent', 'store': tmp}
            cases = [{'id': label, 'expected': label,
                'execution_expected': 'MATCH' if label == 'MATCH' else 'UNKNOWN',
                'trace': {'id': label, 'trace_id': label}, 'review': {}} for label in ('MATCH', 'NON_MATCH', 'UNKNOWN')]
            write(root/'baseline.json', {'development': [{**c, 'actual': 'UNKNOWN'} for c in cases]})
            teaching = {'cases': cases, 'teaching_ids': ['MATCH', 'NON_MATCH'], 'annotations': [],
                        'baseline': {'path': str(root/'baseline.json')}}
            initial_rows = [{**c, 'actual': 'MATCH' if c['id'] != 'NON_MATCH' else 'UNKNOWN',
                             'mechanical_pass': True, 'receipt': str(root/(c['id']+'.json'))} for c in cases]
            for r in initial_rows:
                write(r['receipt'], {'jev_receipts': []})
            write(root/'results.json', initial_rows)
            adapter = SimpleNamespace(compile=Mock(return_value={**parent, 'version': 'child'}),
                evaluate=lambda c, t: {'actual': 'MATCH' if t['id']=='MATCH' else 'UNKNOWN',
                    'mechanical_pass': True, 'metrics': {'semantic_calls': 1}, 'trace_sha256': digest(t)})
            model = SimpleNamespace(root=calls)
            lifecycle = AutonomousLifecycle(root/'jobs', None, model,
                SimpleNamespace(context=lambda *a: 'context'), None)
            result = lifecycle._develop(root, 'job', adapter, model, parent, teaching,
                {'description': 'contract'}, {}, initial_result={'results': str(root/'results.json')})
            kwargs = adapter.compile.call_args.kwargs
            self.assertEqual(kwargs['parent_candidate'], parent)
            self.assertEqual(kwargs['max_revisions'], 1)
            self.assertIn('UNKNOWN', [e['trace']['id'] for e in kwargs['teaching_examples']])
            self.assertEqual(result['status'], 'DEVELOPMENT_REPAIRED')
            self.assertFalse(result['hidden_qualification_invoked'])

    def test_supported_action_annotations_cannot_certify_execution_non_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); contract = {'description': 'unchanged'}
            cases = [{'id': label, 'expected': label,
                'execution_expected': 'MATCH' if label == 'MATCH' else 'UNKNOWN',
                'trace': {'trace_id': label, 'attrs': {'conversation_complete': False}},
                'review': {'label': label, 'oracle_status': 'JUSTIFIED'}}
                for label in ('MATCH', 'NON_MATCH', 'UNKNOWN')]
            for c in cases:
                c['trace_sha256'] = digest(c['trace'])
            write(root/'baseline.json', {'development': [{**c, 'actual': 'UNKNOWN'} for c in cases]})
            packet = {'scope': 'exposed-development-only', 'contract_sha256': digest(contract),
                'cases': cases, 'teaching_ids': [c['id'] for c in cases],
                'baseline': {'path': str(root/'baseline.json'), 'sha256': filehash(root/'baseline.json')}}
            write(root/'input.json', packet)
            config = {'path': str(root/'input.json'), 'sha256': filehash(root/'input.json')}
            runtime = SimpleNamespace(validate_trace=lambda t: None)
            teaching = load_development_teaching(config, contract, runtime)
            self.assertEqual([e['expected_verdict'] for e in teaching['examples']], ['MATCH', 'UNKNOWN', 'UNKNOWN'])
            self.assertEqual(teaching['annotations'][1]['observed_decision_label'], 'NON_MATCH')
            packet['cases'][1]['execution_expected'] = 'NON_MATCH'
            write(root/'bad.json', packet)
            with self.assertRaisesRegex(ValueError, 'cannot assert execution completeness'):
                load_development_teaching({'path': str(root/'bad.json'), 'sha256': filehash(root/'bad.json')}, contract, runtime)

    def test_development_success_requires_recovered_match_provenance_and_no_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root/'baseline.json', {'development': [
                {'id': 'positive', 'expected': 'MATCH', 'actual': 'UNKNOWN'},
                {'id': 'unknown', 'expected': 'UNKNOWN', 'actual': 'UNKNOWN'}]})
            teaching = {'baseline': {'path': str(root/'baseline.json')}, 'cases': [
                {'id': 'positive', 'expected': 'MATCH', 'execution_expected': 'MATCH', 'trace': {'id': 'positive'}},
                {'id': 'unknown', 'expected': 'UNKNOWN', 'execution_expected': 'UNKNOWN', 'trace': {'id': 'unknown'}}]}
            def evaluate(candidate, trace):
                return {'actual': 'MATCH', 'mechanical_pass': True, 'trace_sha256': digest(trace),
                        'metrics': {'semantic_calls': 1}}
            sdk = SimpleNamespace(evaluate=evaluate)
            result = evaluate_development(root, sdk, {}, teaching)
            self.assertEqual(result['status'], 'DEVELOPMENT_INCOMPLETE')
            self.assertEqual(result['restoration_gate']['regressions'], ['unknown'])
            self.assertFalse(result['hidden_qualification_invoked'])
            self.assertFalse(result['qualified'])

    def test_frames_and_propositions_reject_together_before_host_or_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = sdk(tmp); generated = draft()
            with self.assertRaises(RepairFailure) as failure:
                runtime.compile('initial', generated['udf_source'], generated['compile_plan'], 'd', 'c')
            details = failure.exception.details
            arguments = details['judge_argument_measurements']
            self.assertEqual([(r['field'], r['characters'], r['valid']) for r in arguments],
                [('proposition', 997, True), ('frame', 605, False), ('proposition', 1005, False), ('frame', 612, False)])
            self.assertEqual(sum(e['code'] == 'JUDGE_FRAME_BOUNDS' for e in details['errors']), 2)
            runtime.program_runner.assert_not_called(); runtime.judge_factory.assert_not_called()
            self.assertFalse((Path(tmp)/'initial/descriptor.json').exists())

    def test_model_constant_patch_preserves_valid_wording_and_every_other_ast_node(self):
        original = draft(); updated = apply_wording_patch(original, wording())
        self.assertIn('c' * 997, updated['udf_source'])
        before, after = ast.parse(original['udf_source']), ast.parse(updated['udf_source'])
        for tree in (before, after):
            for node in tree.body:
                if isinstance(node, ast.Assign) and node.targets[0].id in ('OTHER', 'CAMPAIGN_FRAME', 'OTHER_FRAME'):
                    node.value.value = '__allowed_wording__'
        self.assertEqual(ast.dump(before), ast.dump(after))
        self.assertEqual(updated['compile_plan']['clauses'], original['compile_plan']['clauses'])
        self.assertEqual(updated['compile_plan']['judge_sites'][0], original['compile_plan']['judge_sites'][0])
        self.assertEqual(updated['compile_plan']['judge_sites'][1]['proposition'], 'o' * 998)
        self.assertTrue(all(row['valid'] for row in literal_judge_arguments(updated['udf_source'])[0]))

    def test_no_source_injection_valid_constant_changes_or_partial_patch(self):
        for response in (
            {'replacements': wording()['replacements'], 'udf_source': 'pass'},
            {'replacements': wording()['replacements'] + [{'constant': 'CAMPAIGN', 'value': 'different'}]},
            {'replacements': wording()['replacements'][:-1]},
            {'replacements': [{'constant': 'OTHER', 'value': 'x' * 1001}]},
        ):
            with self.subTest(response=response), self.assertRaises(ValueError):
                apply_wording_patch(draft(), response)

    def test_literal_frame_and_dynamic_frame_are_not_confused(self):
        source = 'def evaluate(run, recorder, judge):\n judge.decide(proposition="x", evidence=[], frame=' + repr('f'*601) + ')'
        rows, _ = literal_judge_arguments(source)
        self.assertEqual([r['characters'] for r in rows], [1, 601])
        with self.assertRaisesRegex(ValueError, 'module constants'):
            wording_patch_targets(source)
        rows, _ = literal_judge_arguments(source.replace(repr('f'*601), 'run.trace.attrs["frame"]'))
        self.assertEqual([r['field'] for r in rows], ['proposition'])
        shadowed = 'FRAME = ' + repr('f'*601) + '\ndef helper(judge, FRAME):\n return judge.decide(proposition="x", evidence=[], frame=FRAME)'
        self.assertEqual([r['field'] for r in literal_judge_arguments(shadowed)[0]], ['proposition'])

    def fixture(self):
        trace = {'trace_id': 'authentic-development', 'events': [{'id': 'event'}]}
        fixture = {'trace': trace, 'trace_sha256': digest(trace), 'expected_verdict': 'UNKNOWN',
                   'minimum_semantic_calls': 1, 'fixture_receipt_sha256': 'verified-original-receipt'}
        row = {'actual': 'UNKNOWN', 'verdict': 'UNKNOWN', 'trace_sha256': digest(trace),
               'mechanical_pass': True, 'audit': {'mechanical_status': 'PASS'}, 'metrics': {'semantic_calls': 2}}
        return fixture, row

    def test_regression_requires_true_execution_provenance_trace_and_jev(self):
        fixture, row = self.fixture()
        self.assertEqual(execution_regression_errors(row, fixture), [])
        variants = [dict(row, actual='ERROR'), dict(row, trace_sha256='wrong'),
                    dict(row, audit={'mechanical_status': 'FAIL'}), dict(row, metrics={'semantic_calls': 0}),
                    dict(row, actual='MATCH', verdict='MATCH'), dict(row, mechanical_pass=False)]
        for changed in variants:
            with self.subTest(changed=changed):
                self.assertTrue(execution_regression_errors(changed, fixture))

    def test_bounded_wording_repair_then_verified_execution_gate_before_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); generated = draft(); fixture, row = self.fixture()
            real_adapter = sdk(root/'compile')
            calls = []
            def compile_candidate(name, source, plan, description, context):
                calls.append(source)
                if len(calls) == 1:
                    return real_adapter.compile(name, source, plan, description, context)
                self.assertTrue(all(r['valid'] for r in literal_judge_arguments(source)[0]))
                return {'version': 'freshly-compiled'}
            model = Mock(); model.call.return_value = wording()
            adapter = SimpleNamespace(compile=compile_candidate, evaluate=Mock(return_value=row))
            runtime = SimpleNamespace(documentation=lambda: {'canonical': 'sdk'}, context=lambda c, s: 'frozen')
            corpus = Mock()
            lifecycle = AutonomousLifecycle(root/'jobs', None, model, runtime, corpus, execution_regression=fixture)
            lifecycle.retained_authoring = {'freeze_contract': True}
            updated, candidate, contract = lifecycle._author(root, 'job', adapter, model, generated,
                {'description': 'original contract'}, {}, 'original question', execution_regression=fixture)
            self.assertEqual(model.call.call_count, 1)
            self.assertEqual(model.call.call_args.args[1], 'sdk-wording-repair')
            self.assertEqual(updated, apply_wording_patch(generated, wording()))
            adapter.evaluate.assert_called_once_with(candidate, fixture['trace'])
            self.assertEqual(read(root/'authoring/execution-1.json')['status'], 'PASS')
            self.assertTrue(read(root/'authoring/outcome.json')['execution_validated'])
            corpus.assert_not_called()

    def test_error_execution_cannot_enter_corpus_or_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); generated = apply_wording_patch(draft(), wording()); fixture, row = self.fixture()
            row.update(actual='ERROR', verdict='ERROR')
            model = Mock(); model.call.return_value = generated
            adapter = SimpleNamespace(compile=Mock(return_value={'version': 'compiled'}), evaluate=Mock(return_value=row))
            runtime = SimpleNamespace(documentation=lambda: {}, context=lambda c, s: 'context')
            corpus = Mock(); lifecycle = AutonomousLifecycle(root/'jobs', None, model, runtime, corpus)
            with self.assertRaises(RepairFailure):
                lifecycle._author(root, 'job', adapter, model, generated, {'description': 'contract'}, {},
                                  'question', execution_regression=fixture)
            self.assertEqual(model.call.call_count, 2)
            self.assertEqual(adapter.evaluate.call_count, 3)
            self.assertEqual(read(root/'authoring/execution-2.json')['status'], 'FAIL')
            self.assertFalse((root/'generated.json').exists())
            corpus.assert_not_called()

if __name__ == '__main__':
    unittest.main()
