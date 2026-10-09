"""Optional mechanical replays of sealed SDK failures; no model/backend calls."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.engine import Policy, Qualifier
from compilation.storage import read, digest, filehash


def output(actual):
    return {'actual': actual, 'mechanical_pass': True}


@unittest.skipUnless(os.environ.get('CARDINAL_BEHAVIOR_RETAINED_FIXTURES'),
                     'set CARDINAL_BEHAVIOR_RETAINED_FIXTURES to the sealed qualification workspace')
class RetainedSDKFailureTests(unittest.TestCase):
    """Replay retained programs on retained development traces, with JEV forbidden.

    The incumbent uses a deterministic UNKNOWN stand-in to isolate promotion logic.
    These tests make no claim about its semantic correctness or qualification.
    The proposed source and all traces are the original sealed artifacts.
    """
    def reproduce(self, behavior, failure, expected_count):
        root = Path(os.environ['CARDINAL_BEHAVIOR_RETAINED_FIXTURES']).resolve()
        environment = read(root.parent / 'behavioral-compilation-correctness/environment.json')
        host = Path(environment['host_path'])
        prior = root.parent / 'behavioral-compilation-correctness'
        sys.path[:0] = [str(host/'sdk'), str(host), str(host.parent), str(prior/'harness')]
        from behavior_runtime.diagnostics import ArtifactStore
        from behavior_sdk import Judge, JudgeConfig
        from behavior_sdk.native_profile import NativeProfile
        from sandbox_runner import evaluate_program
        from common import audit_result, metrics
        from compilation.sdk_adapter import SDK
        def verify():
            for name, sha in environment['host_files'].items():
                self.assertEqual(filehash(host/name), sha)
            for info in environment['sandbox_files'].values():
                self.assertEqual(filehash(info['path']), info['sha256'])
        retained = root / 'runs/pilot-002'
        prior = retained / 'qualification' / behavior
        initial = read(retained / 'initial' / (behavior + '.json'))
        broken = read(retained / 'research-freezes' / (behavior + '.json'))['candidate']
        source = Path(broken['source']).read_text()
        store = ArtifactStore(broken['store'])
        self.assertEqual(digest(source.encode()), store.version(broken['version']).source_sha256)
        discovery = read(prior / 'discovery.json')
        challenge0 = read(prior / 'challenges-0.json')
        later = [case for path in sorted(prior.glob('challenges-*.json'))
                 if path.name != 'challenges-0.json' for case in read(path)]
        suite = discovery + challenge0 + later
        self.assertEqual(len(suite), expected_count)
        backend_calls = []
        executions = []

        def forbidden_backend(request):
            backend_calls.append(request)
            raise AssertionError('Mechanical test must never invoke a model backend')

        def evaluate(candidate, trace):
            if candidate == initial:
                return output('UNKNOWN')  # Explicit mechanical stand-in, not a semantic reevaluation.
            self.assertEqual(candidate, broken)
            raw = sdk.evaluate(candidate, trace)
            executions.append(raw)
            return raw

        with tempfile.TemporaryDirectory() as temp:
            sdk = SDK(Path(temp)/'sdk', profile_factory=NativeProfile,
                      judge_factory=lambda trace, config: Judge(trace, JudgeConfig(**config), forbidden_backend),
                      program_runner=evaluate_program, audit_result=audit_result, metrics=metrics,
                      verify=verify, jev_config=environment['jev_config'], population={'agent':'megan'},
                      evaluation_policy={'scope':'selected-window-objects'},
                      finality_policy={'on_open':'UNKNOWN','terminal_claims':True})
            qroot = Path(temp) / 'q'
            q = Qualifier(qroot, Policy(max_repairs=1, audit_per_label=1), evaluate,
                          lambda c, r: challenge0 if r == 0 else later,
                          lambda c, f, r: broken,
                          lambda: self.fail('Rejected SDK failure must never reach audit'))
            result = q.run(initial, discovery)
            self.assertEqual(len(executions), expected_count)
            self.assertEqual(backend_calls, [])
            for execution in executions:
                self.assertEqual(execution['verdict'], 'ERROR')
                self.assertIn(failure, execution['reason'])
                self.assertFalse(execution['jev_receipts'])
            self.assertEqual(result['candidate'], initial)
            self.assertEqual(result['status'], 'CANNOT_QUALIFY')
            self.assertEqual(read(qroot / 'candidate-1.json'), initial)
            self.assertEqual(read(qroot / 'proposal-1.json'), broken)
            promotion = read(qroot / 'promotion-1.json')
            self.assertFalse(promotion['passed'])
            self.assertEqual(promotion['execution_failures'], [c['id'] for c in suite])
            self.assertEqual(len(read(qroot / 'development-1.json')), expected_count)

    def test_retained_id_failure(self):
        self.reproduce('B03', "unsupported evidence field 'id'", 21)

    def test_retained_parent_id_failure(self):
        self.reproduce('B07', "unsupported evidence field 'parent_id'", 21)

    def test_retained_traceview_attrs_failure(self):
        self.reproduce('B13', "'TraceView' object has no attribute 'attrs'", 27)
