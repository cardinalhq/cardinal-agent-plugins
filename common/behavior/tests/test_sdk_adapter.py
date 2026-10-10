import dataclasses
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.diagnosis import citations
from compilation.sdk_adapter import SDK
from compilation.storage import digest
from compilation.storage import read
from compilation.storage import write
from compilation.engine import RepairFailure


class SDKPacketTests(unittest.TestCase):
    def test_parent_correction_failure_retains_exact_canonical_receipt(self):
        class CompileFailure(Exception):
            receipt_id = 'failed-round'
        class Compiler:
            def __init__(self, *args, **kwargs): pass
            def compile(self, request): raise CompileFailure()
        fields = ('diagnostic_id behavior_description optional_user_context examples profile '
                  'jev_config population evaluation_policy finality_policy').split()
        Request = dataclasses.make_dataclass('Request', fields + [
            ('parent_version', str, dataclasses.field(default=None)),
            ('correction', str, dataclasses.field(default=''))])
        compiler = types.ModuleType('behavior_runtime.compiler')
        compiler.Compiler = Compiler
        compiler.CompilerConfig = lambda **k: types.SimpleNamespace(**k)
        compiler.CompileRequest = Request
        compiler.TeachingExample = lambda **k: types.SimpleNamespace(**k)
        compiler.CompileFailure = CompileFailure
        diagnostics = types.ModuleType('behavior_runtime.diagnostics')
        diagnostics.ArtifactStore = lambda _: None
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                'behavior_runtime.compiler': compiler, 'behavior_runtime.diagnostics': diagnostics}):
            store = Path(tmp)/'parent'; (store/'compile_receipts').mkdir(parents=True)
            receipt = store/'compile_receipts/failed-round.json'
            write(receipt, {'rounds': [{'errors': ['expected UNKNOWN, got MATCH']}]})
            sdk = SDK(Path(tmp)/'child', profile_factory=lambda: None, judge_factory=lambda: None,
                program_runner=lambda: None, audit_result=lambda *a: {}, metrics=lambda *a: {},
                verify=lambda: None, jev_config={}, population={}, evaluation_policy={}, finality_policy={})
            with self.assertRaises(RepairFailure) as caught:
                sdk.compile('job-correction', 'def evaluate(run, recorder, judge):\n recorder.unknown("coverage")\n',
                    {'clauses': [], 'judge_sites': []}, 'd', 'c',
                    parent_candidate={'store': str(store), 'version': 'parent'}, correction='preserve unknown')
            self.assertEqual(caught.exception.details['errors'], ['expected UNKNOWN, got MATCH'])
            self.assertEqual(caught.exception.details['receipt_paths'], [str(receipt)])

    def test_teaching_backend_gets_feedback_and_persists_compiler_revision(self):
        @dataclasses.dataclass
        class Plan:
            clauses: list = dataclasses.field(default_factory=list)
            judge_sites: list = dataclasses.field(default_factory=list)
        calls = []
        revised = 'def evaluate(run, recorder, judge):\n recorder.unknown("coverage")\n'
        def author(packet):
            calls.append(packet)
            return {'text': __import__('json').dumps({'udf_source': revised,
                    'compile_plan': {'clauses': [], 'judge_sites': []}})}
        class Compiler:
            def __init__(self, store, backend, factory, config, **kwargs):
                self.backend, self.config = backend, config
            def compile(self, request):
                self_outer.assertEqual(self.config.max_revisions, 2)
                self_outer.assertEqual(request.examples[0].expected_verdict, 'MATCH')
                self.backend({'feedback': None})
                self.backend({'feedback': {'errors': ['expected MATCH, got UNKNOWN']}})
                return types.SimpleNamespace(udf_source=revised, compile_plan=Plan(),
                    receipt={'revisions': 1}, diagnostic_version=types.SimpleNamespace(version='revised'))
        self_outer = self
        compiler = types.ModuleType('behavior_runtime.compiler')
        compiler.Compiler = Compiler
        compiler.CompilerConfig = lambda **k: types.SimpleNamespace(**k)
        compiler.CompileRequest = lambda **k: types.SimpleNamespace(**k)
        compiler.TeachingExample = lambda **k: types.SimpleNamespace(**k)
        compiler.CompileFailure = type('CompileFailure', (Exception,), {})
        diagnostics = types.ModuleType('behavior_runtime.diagnostics')
        diagnostics.ArtifactStore = lambda _: None
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {
                'behavior_runtime.compiler': compiler, 'behavior_runtime.diagnostics': diagnostics}):
            sdk = SDK(tmp, profile_factory=lambda: None, judge_factory=lambda: None,
                program_runner=lambda: None, audit_result=lambda *a: {}, metrics=lambda *a: {},
                verify=lambda: None, jev_config={}, population={}, evaluation_policy={}, finality_policy={})
            candidate = sdk.compile('job-teaching', 'def evaluate(run, recorder, judge):\n pass\n',
                {'clauses': [], 'judge_sites': []}, 'd', 'c', author=author, max_revisions=2,
                teaching_examples=[{'trace': {'trace_id': 'positive'}, 'expected_verdict': 'MATCH'}])
            self.assertEqual(len(calls), 2)
            self.assertIn('expected MATCH', calls[1]['feedback']['errors'][0])
            self.assertEqual(Path(candidate['source']).read_text(), revised)
            self.assertEqual(read(candidate['plan']), {'clauses': [], 'judge_sites': []})

    def test_live_sdk_tuple_records_match_model_json_citations(self):
        @dataclasses.dataclass
        class Result:
            verdict: str = 'ERROR'
            records: tuple = ({'op': 'visit', 'subject': 'observed execution'},)
            jev_receipts: tuple = ()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'program.py'; source.write_text('pass')
            version = types.SimpleNamespace(version='v', source_sha256=digest(b'pass'))
            store = types.SimpleNamespace(version=lambda _: version, evaluate=lambda *a, **k: Result())
            module = types.ModuleType('behavior_runtime.diagnostics')
            module.ArtifactStore = lambda _: store
            sdk = SDK(root/'sdk', profile_factory=lambda: None, judge_factory=lambda: None,
                program_runner=lambda: None, audit_result=lambda *a: {'mechanical_status': 'PASS'},
                metrics=lambda *a: {}, verify=lambda: None, jev_config={}, population={},
                evaluation_policy={}, finality_policy={})
            with patch.dict(sys.modules, {'behavior_runtime.diagnostics': module}):
                row = sdk.evaluate({'store': tmp, 'version': 'v', 'source': str(source)}, {})
            self.assertEqual(row['actual'], 'ERROR')
            self.assertIsInstance(row['records'], list)
            citations([{'ref': '/execution/records/0/subject', 'quote': 'observed execution'}],
                      {'execution': row})
            with self.assertRaisesRegex(ValueError, 'nonexistent evidence'):
                citations([{'ref': '/execution/records/0/name', 'quote': 'observed execution'}],
                          {'execution': row})


if __name__ == '__main__':
    unittest.main()
