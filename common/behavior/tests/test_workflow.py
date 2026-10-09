"""Exercise the consolidated offline lifecycle using deterministic role doubles."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.engine import Policy, RepairFailure
from compilation.storage import read, write, digest
from compilation.runtime import IDENTITY_KEYS, execute
from compilation.workflow import compile_and_qualify
from test_qualification import cases


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.role_calls = []
        self.splits = []
        self.executions = []
        self.repair_source = 'fixed'
        def compile(name, source, plan, description, context):
            directory = self.root / name
            directory.mkdir()
            (directory / 'program.py').write_text(source)
            write(directory / 'plan.json', plan)
            return {'source': str(directory / 'program.py'), 'plan': str(directory / 'plan.json'),
                    'version': digest(source)}
        def evaluate(candidate, trace):
            source = Path(candidate['source']).read_text()
            self.executions.append((source, trace['trace_id']))
            return {'actual': trace['signal'] if source == 'fixed' else
                    'ERROR' if source == 'broken' else 'UNKNOWN', 'mechanical_pass': True,
                    'reason': 'test double', 'audit': {'errors': []}, 'jev_receipts': []}
        def make(split, per_label, candidate_source=None, excluded_families=()):
            self.splits.append(split)
            result = cases(split)
            for c in result:
                c['trace']['events'] = []
                c['review'] = {'reason': 'mechanical test review'}
            return result
        def call(name, role, instruction, packet):
            self.role_calls.append((name, role, instruction, packet))
            return {'udf_source': self.repair_source, 'compile_plan': {}}
        self.sdk = SimpleNamespace(compile=compile, evaluate=evaluate)
        self.corpus = SimpleNamespace(make=make, behavior='test', system='system', contract={}, common={})
        self.model = SimpleNamespace(call=call)

    def run_cycle(self, **kwargs):
        return compile_and_qualify(self.root / 'cycle', source='initial', plan={},
            description='contract', context='context', sdk=self.sdk, corpus=self.corpus,
            model=self.model, sdk_documentation='pinned SDK docs', jev_config={'model': 'existing'},
            identity={k: digest(k) for k in IDENTITY_KEYS if k != 'program'},
            dependencies=lambda c: {'plan.json': c['plan']},
            policy=Policy(max_repairs=1, audit_per_label=1, lifetime_executions=100), **kwargs)

    def test_full_offline_loop_freezes_and_executes_without_downstream_adjudication(self):
        result = self.run_cycle(usage_rows=lambda: [{'input_tokens': 100, 'output_tokens': 10,
            'provider_attempts': 1, 'latency_ms': 50, 'cost_usd': 2}])
        self.assertEqual(result['status'], 'QUALIFIED')
        self.assertEqual(self.splits, ['discovery', 'challenge-0', 'challenge-1', 'audit'])
        self.assertEqual(len(self.role_calls), 1)
        self.assertEqual(self.role_calls[0][1], 'program-repair')
        frozen = self.root / 'cycle/bundle'
        cert = read(frozen / 'certificate.json')
        self.assertEqual(cert['version'], digest('fixed'))
        before = len(self.role_calls)
        verdict = execute(frozen, cert['identity'], {'trace_id': 'production'},
                          lambda version, source, trace: {'verdict': 'UNKNOWN'})
        self.assertEqual(verdict, {'verdict': 'UNKNOWN'})
        self.assertEqual(len(self.role_calls), before)
        costs = read(self.root / 'cycle/cost.json')
        self.assertEqual(costs['qualification_amortized_per_execution']['cost_usd'], .02)
        self.assertIsNone(costs['steady_state_per_execution']['cost_usd'])

    def test_failed_repair_keeps_previous_source_and_never_freezes(self):
        self.repair_source = 'broken'
        result = self.run_cycle()
        self.assertEqual(result['status'], 'CANNOT_QUALIFY')
        self.assertEqual(Path(result['candidate']['source']).read_text(), 'initial')
        self.assertNotIn('audit', self.splits)
        self.assertFalse((self.root / 'cycle/bundle').exists())
        self.assertEqual(len([x for x in self.executions if x[0] == 'broken']), 9)
        self.assertIsNone(read(self.root / 'cycle/cost.json')['qualification_total']['cost_usd'])

    def test_compiler_rejection_and_infrastructure_failure_fail_closed(self):
        original = self.sdk.compile
        def compile(name, *args):
            if 'repair' in name:
                raise RepairFailure({'errors': ['unsupported SDK interface']})
            return original(name, *args)
        self.sdk.compile = compile
        result = self.run_cycle()
        self.assertEqual(result['reason'], 'repair_compile_budget_exhausted')
        self.assertEqual(Path(result['candidate']['source']).read_text(), 'initial')
        self.assertFalse((self.root / 'cycle/bundle').exists())


if __name__ == '__main__':
    unittest.main()
