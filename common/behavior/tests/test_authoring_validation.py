import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.authoring_validation import execution_path_errors
from compilation.autonomous import AutonomousLifecycle
from compilation.engine import RepairFailure
from compilation.sdk_adapter import SDK, judge_site_validation
from compilation.storage import read


class AuthoringValidationTests(unittest.TestCase):
    def test_placeholder_dead_paths_rejected_without_executing_code(self):
        for source in (
            'def evaluate(run, recorder, judge):\n raise RuntimeError("not implemented")\n recorder.unknown("unreachable")',
            'def evaluate(run, recorder, judge):\n if True:\n  raise ValueError("blocked")',
            'def evaluate(run, recorder, judge):\n if run.events:\n  raise ValueError()\n else:\n  raise RuntimeError()',
            'def blocked():\n raise RuntimeError()\ndef evaluate(run, recorder, judge):\n blocked()',
            'def evaluate(run, recorder, judge):\n assert False, "TODO"',
            'def evaluate(run, recorder, judge):\n while True:\n  pass',
            'def evaluate(run, recorder, judge):\n pass',
            'def evaluate(run, recorder, judge):\n return NotImplemented',
            'raise RuntimeError("module stub")\ndef evaluate(run, recorder, judge):\n recorder.unknown("data")',
        ):
            with self.subTest(source=source):
                self.assertTrue(execution_path_errors(source))

    def test_guarded_runtime_failures_and_early_returns_remain_legal(self):
        for source in (
            'def evaluate(run, recorder, judge):\n if not run.events:\n  raise ValueError("invalid input")\n recorder.unknown("missing support")',
            'def evaluate(run, recorder, judge):\n if run.events:\n  recorder.unknown("missing support")\n  return\n raise ValueError("invalid input")',
            'def evaluate(run, recorder, judge):\n if False:\n  raise RuntimeError("dead")\n recorder.unknown("missing support")',
        ):
            with self.subTest(source=source):
                self.assertEqual(execution_path_errors(source), [])

    def test_sdk_rejects_placeholder_before_host_import_or_compile(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = SDK(tmp, profile_factory=Mock(), judge_factory=Mock(), program_runner=Mock(),
                audit_result=Mock(), metrics=Mock(), verify=Mock(), jev_config={}, population={},
                evaluation_policy={}, finality_policy={})
            # This succeeds without an installed behavior_runtime: rejection precedes import.
            with self.assertRaises(RepairFailure) as failure:
                sdk.compile('stub', 'def evaluate(run, recorder, judge):\n raise RuntimeError("blocked")', {}, 'd', 'c')
            self.assertEqual(failure.exception.details['errors'][0]['code'], 'NON_EXECUTABLE_ENTRYPOINT')
            self.assertEqual(read(Path(tmp)/'stub/mechanical-validation.json')['status'], 'FAIL')
            self.assertFalse((Path(tmp)/'stub/descriptor.json').exists())
            sdk.program_runner.assert_not_called()
            sdk.judge_factory.assert_not_called()

    def author_fixture(self, root, proposals):
        contract = {'description': 'preserve user semantics', 'clauses': []}
        documentation = {'example_source': 'canonical installed example',
                         'installed_signatures': {'Judge.decide': 'canonical signature'}}
        runtime = SimpleNamespace(documentation=lambda: documentation,
                                  context=lambda c, s: json.dumps(c))
        model = Mock()
        model.call.side_effect = proposals
        lifecycle = AutonomousLifecycle(root/'jobs', None, model, runtime, Mock())
        return lifecycle, model, contract

    def test_mechanical_feedback_repairs_before_corpus_and_preserves_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal = {'udf_source': 'implemented', 'compile_plan': {}}
            lifecycle, model, contract = self.author_fixture(root, [proposal])
            sdk = SimpleNamespace(compile=Mock(side_effect=[
                RepairFailure({'errors': [{'code': 'NON_EXECUTABLE_ENTRYPOINT'}],
                    'judge_site_measurements': [{'name': 'valid', 'characters': 994, 'maximum': 1000, 'valid': True}]}), {'version': 'valid'}]))
            result, candidate, final_contract = lifecycle._author(root, 'job', sdk, model,
                {'udf_source': 'stub', 'compile_plan': {}}, contract, {}, 'original question')
            self.assertEqual(result, proposal)
            self.assertEqual(candidate, {'version': 'valid'})
            self.assertEqual(final_contract, contract)
            self.assertEqual(model.call.call_count, 1)
            packet = model.call.call_args.args[3]
            self.assertEqual(packet['question'], 'original question')
            self.assertEqual(packet['contract'], contract)
            self.assertEqual(packet['mechanical_failures'][0]['errors'][0]['code'], 'NON_EXECUTABLE_ENTRYPOINT')
            self.assertIn('installed_signatures', packet['sdk'])
            self.assertEqual(packet['mechanical_failures'][0]['judge_site_measurements'][0]['characters'], 994)
            self.assertIn('frames byte-for-byte', model.call.call_args.args[2])
            self.assertFalse(any(k in packet for k in ('cases', 'expected', 'gold', 'heldout')))
            lifecycle.corpus_factory.assert_not_called()

    def test_exhausted_prequalification_budget_is_three_attempts_two_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            draft = {'udf_source': 'stub', 'compile_plan': {}}
            lifecycle, model, contract = self.author_fixture(root, [draft, draft])
            sdk = SimpleNamespace(compile=Mock(side_effect=RepairFailure({'errors': ['still blocked']})))
            with self.assertRaises(RepairFailure):
                lifecycle._author(root, 'job', sdk, model, draft, contract, {}, 'question')
            self.assertEqual(sdk.compile.call_count, 3)
            self.assertEqual(model.call.call_count, 2)
            self.assertFalse(read(root/'authoring/outcome.json')['qualification_started'])
            lifecycle.corpus_factory.assert_not_called()
            self.assertFalse((root/'cycle').exists())

    def test_only_retained_nonexecutable_failure_can_request_contract_revision(self):
        for retained, code, expected_calls in (
                (True, 'NON_EXECUTABLE_ENTRYPOINT', 1),
                (False, 'NON_EXECUTABLE_ENTRYPOINT', 0),
                (True, 'PYTHON_SYNTAX', 0)):
            with self.subTest(retained=retained, code=code), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                draft = {'udf_source': 'source', 'compile_plan': {}}
                lifecycle, model, contract = self.author_fixture(root, [draft])
                lifecycle.retained_authoring = {'job': 'verified'} if retained else None
                sdk = SimpleNamespace(compile=Mock(side_effect=[
                    RepairFailure({'errors': [{'code': code}]}), {'version': 'valid'}]))
                with patch('compilation.implementability.revise_contract_if_needed', return_value=contract) as revision:
                    lifecycle._author(root, 'job', sdk, model, draft, contract, {}, 'question')
                self.assertEqual(revision.call_count, expected_calls)
                if expected_calls:
                    self.assertEqual(revision.call_args.kwargs['question'], 'question')
                    self.assertEqual(revision.call_args.kwargs['contract'], contract)

    def test_compound_unconditional_failures_cannot_hide_placeholders(self):
        for body in (
            'try:\n  raise RuntimeError("blocked")\n finally:\n  pass',
            'try:\n  return\n finally:\n  raise RuntimeError("blocked")',
            'try:\n  pass\n except RuntimeError:\n  recorder.unknown("unreachable")\n else:\n  raise RuntimeError("blocked")',
            'try:\n  raise RuntimeError("blocked")\n except ValueError:\n  recorder.unknown("handled")',
            'try:\n  raise RuntimeError("blocked")\n except Exception:\n  raise',
            'try:\n  raise RuntimeError("blocked")\n except Exception:\n  raise ValueError("still blocked")',
            'for item in [1]:\n  raise RuntimeError("blocked")',
            'for item in (1, 2):\n  continue\n else:\n  raise RuntimeError("blocked")',
            'while True:\n  continue',
            'try:\n  while True:\n   pass\n finally:\n  return',
            'if run.events:\n  raise RuntimeError("blocked")\n else:\n  while True:\n   pass',
        ):
            source = 'def evaluate(run, recorder, judge):\n ' + body + '\n'
            with self.subTest(source=source):
                self.assertTrue(execution_path_errors(source))

    def test_compound_normal_paths_remain_available(self):
        for body in (
            'try:\n  raise RuntimeError("handled")\n except Exception:\n  recorder.unknown("missing")',
            'try:\n  raise RuntimeError("handled")\n except (ValueError, RuntimeError):\n  recorder.unknown("missing")',
            'try:\n  if not run.events:\n   raise RuntimeError("invalid")\n  recorder.unknown("missing")\n finally:\n  pass',
            'for item in []:\n  raise RuntimeError("unreachable")\n recorder.unknown("missing")',
            'for item in [1]:\n  break\n  raise RuntimeError("unreachable")\n recorder.unknown("missing")',
            'for item in run.events:\n  raise RuntimeError("guarded by nonempty evidence")',
            'while True:\n  break\n recorder.unknown("missing")',
            'while False:\n  raise RuntimeError("unreachable")\n recorder.unknown("missing")',
        ):
            source = 'def evaluate(run, recorder, judge):\n ' + body + '\n'
            with self.subTest(source=source):
                self.assertEqual(execution_path_errors(source), [])

    def test_direct_aborting_helper_alias_and_async_helper_are_distinct(self):
        source = ('def blocked():\n raise RuntimeError("blocked")\n'
                  'def evaluate(run, recorder, judge):\n alias = blocked\n alias()\n')
        self.assertTrue(execution_path_errors(source))
        async_source = source.replace('def blocked()', 'async def blocked()')
        self.assertEqual(execution_path_errors(async_source), [])
        conditional_alias = source.replace(' alias()\n',
            ' if run.events:\n  alias = judge.decide\n alias()\n')
        self.assertEqual(execution_path_errors(conditional_alias), [])

    def test_unknown_exceptions_and_finally_rebindings_preserve_normal_paths(self):
        source = ('def evaluate(run, recorder, judge):\n'
                  ' try:\n  int(run.trace.attrs["x"])\n'
                  ' except ValueError:\n  recorder.unknown("invalid")\n  return\n'
                  ' else:\n  raise RuntimeError("unsupported")\n')
        self.assertEqual(execution_path_errors(source), [])
        rebound = ('def blocked():\n raise RuntimeError("blocked")\n'
                   'def evaluate(run, recorder, judge):\n alias = blocked\n'
                   ' try:\n  alias = recorder.unknown\n finally:\n  alias("missing")\n')
        self.assertEqual(execution_path_errors(rebound), [])

    def test_loop_else_rebindings_and_handled_arithmetic_remain_normal(self):
        rebound = ('def blocked():\n raise RuntimeError("blocked")\n'
                   'def evaluate(run, recorder, judge):\n alias = blocked\n'
                   ' for item in [1]:\n  alias = recorder.unknown\n else:\n  alias("missing")\n')
        self.assertEqual(execution_path_errors(rebound), [])
        handled = ('def evaluate(run, recorder, judge):\n'
                   ' try:\n  1/0\n except ZeroDivisionError:\n  recorder.unknown("missing")\n'
                   ' else:\n  raise RuntimeError("blocked")\n')
        self.assertEqual(execution_path_errors(handled), [])

    def test_all_site_lengths_include_valid_sites_and_all_errors(self):
        plan = {'judge_sites': [{'name': name, 'proposition': 'x' * size}
            for name, size in [('campaign', 1022), ('other', 994), ('too_long', 1005), ('empty', 0), ('boundary', 1000)]]}
        measured, errors = judge_site_validation(plan)
        self.assertEqual([(m['name'], m['characters']) for m in measured],
            [('campaign', 1022), ('other', 994), ('too_long', 1005), ('empty', 0), ('boundary', 1000)])
        self.assertEqual([e['name'] for e in errors], ['campaign', 'too_long', 'empty'])
        self.assertTrue(all(m['minimum'] == 1 and m['maximum'] == 1000 for m in measured))
        with tempfile.TemporaryDirectory() as tmp:
            sdk = SDK(tmp, profile_factory=Mock(), judge_factory=Mock(), program_runner=Mock(),
                audit_result=Mock(), metrics=Mock(), verify=Mock(), jev_config={}, population={},
                evaluation_policy={}, finality_policy={})
            with self.assertRaises(RepairFailure) as failure:
                sdk.compile('sites', 'def evaluate(run, recorder, judge):\n recorder.unknown("missing")', plan, 'd', 'c')
            self.assertEqual(failure.exception.details['judge_site_measurements'], measured)
            self.assertEqual(failure.exception.details['errors'], errors)
            sdk.program_runner.assert_not_called()

    def test_non_compile_failure_never_causes_author_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lifecycle, model, contract = self.author_fixture(root, [])
            sdk = SimpleNamespace(compile=Mock(side_effect=ValueError('runtime drift')))
            with self.assertRaisesRegex(ValueError, 'runtime drift'):
                lifecycle._author(root, 'job', sdk, model,
                    {'udf_source': 'source', 'compile_plan': {}}, contract, {}, 'question')
            model.call.assert_not_called()

if __name__ == '__main__':
    unittest.main()
