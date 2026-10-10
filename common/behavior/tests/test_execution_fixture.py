from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.execution_fixture import load_execution_fixture
from compilation.retained_authoring import ATTEMPT_FILES, prepare_retained
from compilation.storage import digest, filehash, read, write


def replace_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


class ExecutionFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = SimpleNamespace(identity=lambda: 'sdk', jev_config={'model': 'fixed'},
            audit=Mock(return_value={'mechanical_status': 'PASS'}))
        self.trace = {'trace_id': 'engineering-only', 'events': [{'input': 'observed'}]}
        (self.root / 'program.py').write_text('def evaluate(run, recorder, judge): pass\n')
        write(self.root / 'contract.json', {'description': 'original semantics'})
        self.receipt = {'verdict': 'UNKNOWN', 'trace_sha256': digest(self.trace),
            'jev_receipts': [{'attempts': [{'status': 'ok', 'response_sha256': 'successful-provider-response'}]}]}
        write(self.root / 'trace.json', self.trace)
        write(self.root / 'execution-receipt.json', self.receipt)
        self.summary = {'sdk_version': '0.2.0', 'source_sha256': filehash(self.root / 'program.py'),
            'contract_sha256': filehash(self.root / 'contract.json'),
            'execution_receipt_sha256': filehash(self.root / 'execution-receipt.json')}
        write(self.root / 'execution-summary.json', self.summary)
        write(self.root / 'precompile-validation.json', {'sdk_identity': 'sdk', 'jev_config': {'model': 'fixed'}})
        self.config = {'directory': str(self.root),
            'files': {p.name: filehash(p) for p in self.root.iterdir()}}

    def refresh(self):
        self.config['files'] = {name: filehash(self.root / name) for name in self.config['files']}

    def change_receipt(self, change):
        receipt = deepcopy(self.receipt); change(receipt)
        replace_json(self.root / 'execution-receipt.json', receipt)
        summary = deepcopy(self.summary)
        summary['execution_receipt_sha256'] = filehash(self.root / 'execution-receipt.json')
        replace_json(self.root / 'execution-summary.json', summary)
        self.refresh()

    def test_valid_bound_fixture_loads_without_model_call_or_source_mutation(self):
        before = deepcopy(self.config['files'])
        with patch('compilation.llm.Model.call', side_effect=AssertionError('no model calls')):
            result = load_execution_fixture(self.config, self.runtime)
        self.assertEqual(result['trace'], self.trace)
        self.assertEqual(result['expected_verdict'], 'UNKNOWN')
        self.assertEqual(result['minimum_semantic_calls'], 1)
        self.assertEqual(result['fixture_source_sha256'], before['program.py'])
        self.assertEqual(result['contract_sha256'], before['contract.json'])
        self.runtime.audit.assert_called_once_with(self.receipt, self.trace)
        self.assertEqual(before, {name: filehash(self.root / name) for name in before})

    def test_source_contract_and_receipt_byte_drift_fail_before_audit(self):
        for name in ('program.py', 'contract.json', 'execution-receipt.json'):
            with self.subTest(name=name):
                path = self.root / name; original = path.read_bytes()
                path.write_bytes(original + b' ')
                with self.assertRaisesRegex(ValueError, 'file drift: ' + name):
                    load_execution_fixture(self.config, self.runtime)
                path.write_bytes(original)
        self.runtime.audit.assert_not_called()

    def test_updated_outer_hash_cannot_bypass_source_contract_or_receipt_binding(self):
        for name in ('program.py', 'contract.json', 'execution-receipt.json'):
            with self.subTest(name=name):
                path = self.root / name; original = path.read_bytes()
                path.write_bytes(original + b' '); self.refresh()
                with self.assertRaisesRegex(ValueError, 'identity binding'):
                    load_execution_fixture(self.config, self.runtime)
                path.write_bytes(original); self.refresh()

    def test_trace_binding_rejects_changed_trace_even_with_updated_file_hash(self):
        replace_json(self.root / 'trace.json', {'trace_id': 'different', 'events': []}); self.refresh()
        with self.assertRaisesRegex(ValueError, 'identity binding'):
            load_execution_fixture(self.config, self.runtime)

    def test_sdk_or_jev_drift_rejects_before_audit(self):
        self.runtime.identity = lambda: 'changed'
        with self.assertRaisesRegex(ValueError, 'identity binding'):
            load_execution_fixture(self.config, self.runtime)
        self.runtime.identity = lambda: 'sdk'
        self.runtime.jev_config = {'model': 'different'}
        with self.assertRaisesRegex(ValueError, 'identity binding'):
            load_execution_fixture(self.config, self.runtime)
        self.runtime.audit.assert_not_called()

    def test_empty_real_jev_receipts_are_rejected(self):
        self.change_receipt(lambda r: r.update(jev_receipts=[]))
        with self.assertRaisesRegex(ValueError, 'successful real SDK/JEV'):
            load_execution_fixture(self.config, self.runtime)

    def test_empty_real_jev_attempts_are_rejected(self):
        self.change_receipt(lambda r: r.update(jev_receipts=[{'attempts': []}]))
        with self.assertRaisesRegex(ValueError, 'successful real SDK/JEV'):
            load_execution_fixture(self.config, self.runtime)

    def test_failed_real_jev_attempt_is_rejected(self):
        self.change_receipt(lambda r: r.update(jev_receipts=[{'attempts': [{'status': 'failed'}]}]))
        with self.assertRaisesRegex(ValueError, 'successful real SDK/JEV'):
            load_execution_fixture(self.config, self.runtime)

    def test_error_verdict_and_failed_mechanical_audit_are_rejected(self):
        self.runtime.audit.return_value = {'mechanical_status': 'FAIL'}
        with self.assertRaisesRegex(ValueError, 'successful real SDK/JEV'):
            load_execution_fixture(self.config, self.runtime)
        self.runtime.audit.return_value = {'mechanical_status': 'PASS'}
        self.change_receipt(lambda r: r.update(verdict='ERROR'))
        with self.assertRaisesRegex(ValueError, 'successful real SDK/JEV'):
            load_execution_fixture(self.config, self.runtime)

    def test_inexact_fixture_file_set_rejected(self):
        self.config['files'].pop('program.py')
        with self.assertRaisesRegex(ValueError, 'file set mismatch'):
            load_execution_fixture(self.config, self.runtime)


class FailedAuthoringAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.old, self.new = Path(self.tmp.name) / 'old', Path(self.tmp.name) / 'new'
        self.request = {'agent_id': 'megan', 'behavioral_question': 'original question',
                        'time_range': None, 'policy': {'max_repairs': 2, 'audit_per_label': 4}}
        self.snapshot = {'sources': {'instructions': 'real source'}, 'traces': ['authentic'],
                         'selection': {'receipt': 'authentic origin', 'retrieval_latency_ms': 1}}
        self.contract = {'description': 'frozen revised contract'}
        self.generated = {'udf_source': 'failed attempt source', 'compile_plan': {}}
        values = {'request.json': self.request, 'discovery-input.json': self.snapshot,
            'discovery.json': {'status': 'READY'}, 'authoring/contract-revised.json': self.contract,
            'authoring/proposal-2.json': self.generated,
            'authoring/outcome.json': {'status': 'COMPILATION_FAILED', 'qualification_started': False},
            'outcome.json': {'status': 'COMPILATION_FAILED'}}
        for name, value in values.items(): write(self.old / name, value)
        write(self.new / 'request.json', self.request)
        self.runtime = SimpleNamespace(identity=lambda: 'sdk', jev_config={'model': 'fixed'})
        self.config = {'mode': 'failed_attempt', 'job': str(self.old), 'freeze_contract': True,
            'files': {name: filehash(self.old / name) for name in ATTEMPT_FILES},
            'sdk_identity': 'sdk', 'jev_identity': digest(self.runtime.jev_config),
            'contract_sha256': filehash(self.old / 'authoring/contract-revised.json')}

    def prepare(self, **kwargs):
        return prepare_retained(self.config, self.new, kwargs.get('snapshot', self.snapshot),
            {'source': 'identity'}, self.runtime, kwargs.get('agent', 'megan'),
            kwargs.get('question', 'original question'))

    def test_failed_attempt_mode_reuses_only_frozen_inputs_without_calls(self):
        write(self.old / 'hidden.json', {'gold': 'SECRET'})
        fresh = deepcopy(self.snapshot); fresh['selection']['retrieval_latency_ms'] = 300
        with patch('compilation.llm.Model.call', side_effect=AssertionError('no calls')):
            result = self.prepare(snapshot=fresh)
        self.assertEqual(result, ({'status': 'READY'}, self.contract, self.generated))
        self.assertNotIn('SECRET', str(result))
        self.assertEqual(read(self.new / 'original-generated.json'), self.generated)
        self.assertTrue(read(self.new / 'retained-inputs.json')['contract_frozen'])
        self.assertFalse(read(self.new / 'retained-inputs.json')['prior_scores_or_qualification_reused'])
        self.assertEqual(self.config['files'], {n: filehash(self.old / n) for n in ATTEMPT_FILES})

    def test_original_question_and_agent_must_match(self):
        for args in ({'question': 'easier question'}, {'agent': 'other'}):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'request or policy'):
                self.prepare(**args)

    def test_policy_and_scope_must_match(self):
        for key, value in (('policy', {'max_repairs': 99}), ('time_range', {'start': 'different'})):
            fresh = deepcopy(self.request); fresh[key] = value
            replace_json(self.new / 'request.json', fresh)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'request or policy'):
                self.prepare()

    def test_source_and_telemetry_drift_rejected(self):
        for key, value in (('sources', {'instructions': 'changed'}), ('traces', ['different'])):
            fresh = deepcopy(self.snapshot); fresh[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'source or telemetry drift'):
                self.prepare(snapshot=fresh)

    def test_program_contract_and_outcome_byte_drift_rejected(self):
        for name in ('authoring/proposal-2.json', 'authoring/contract-revised.json', 'outcome.json'):
            path = self.old / name; original = path.read_bytes(); path.write_bytes(original + b' ')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'input drift'):
                self.prepare()
            path.write_bytes(original)

    def test_updated_contract_file_hash_still_must_bind_fixture_contract(self):
        replace_json(self.old / 'authoring/contract-revised.json', {'description': 'changed'})
        self.config['files']['authoring/contract-revised.json'] = filehash(self.old / 'authoring/contract-revised.json')
        with self.assertRaisesRegex(ValueError, 'successful regression contract'):
            self.prepare()

    def test_sdk_and_jev_drift_rejected(self):
        self.runtime.identity = lambda: 'changed'
        with self.assertRaisesRegex(ValueError, 'SDK/JEV drift'): self.prepare()
        self.runtime.identity = lambda: 'sdk'; self.runtime.jev_config = {'model': 'changed'}
        with self.assertRaisesRegex(ValueError, 'SDK/JEV drift'): self.prepare()

    def test_prior_qualification_and_same_job_cannot_reset_budget(self):
        (self.old / 'cycle/qualification').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'qualification budget'): self.prepare()
        self.new = self.old
        with self.assertRaisesRegex(ValueError, 'qualification budget'): self.prepare()

    def test_failed_outcome_and_explicit_frozen_contract_required(self):
        self.config['freeze_contract'] = False
        with self.assertRaisesRegex(ValueError, 'frozen contract'): self.prepare()
        self.config['freeze_contract'] = True
        replace_json(self.old / 'outcome.json', {'status': 'QUALIFIED'})
        self.config['files']['outcome.json'] = filehash(self.old / 'outcome.json')
        with self.assertRaisesRegex(ValueError, 'did not fail authoring'): self.prepare()

    def test_not_ready_discovery_rejected(self):
        replace_json(self.old / 'discovery.json', {'status': 'INSUFFICIENT_SOURCE'})
        self.config['files']['discovery.json'] = filehash(self.old / 'discovery.json')
        with self.assertRaisesRegex(ValueError, 'not ready'): self.prepare()


if __name__ == '__main__':
    unittest.main()
