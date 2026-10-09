"""Hosted API mechanics only: no remote requests, models or new semantic study."""
import copy
import hashlib
import json
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Behavior, SDK_IDENTITIES, tools_list
from compilation.storage import digest
import test_hosted as fixtures


class CompilationHistoryTests(unittest.TestCase):
    setUp = fixtures.HostedBehaviorTests.setUp
    compile = fixtures.HostedBehaviorTests.compile
    teaching_test = fixtures.HostedBehaviorTests.teaching_test

    def candidate(self, version='b' * 64):
        program = copy.deepcopy(self.program)
        program['diagnostic_version'] = version
        program['source'] += '# mechanical candidate\n'
        program['source_sha256'] = hashlib.sha256(program['source'].encode()).hexdigest()
        self.responses.append(program)
        return self.behavior.compile('Authored check', 'service', program['source'], {})

    def receipt(self, version, specs):
        program = json.loads(self.behavior.program_path(version).read_text())
        identity = {**self.behavior.identities(program), 'sdk_source': program['sdk_source'],
                    'diagnostic_version': version, 'udf_sha256': program['source_sha256'],
                    'adapter_sha256': program['adapter_sha256']}
        rows = [{'trace_id': trace, 'expected_verdict': expected, 'verdict': actual,
                 'execution_identity': identity, 'reason': reason}
                for trace, expected, actual, reason in specs]
        receipt = {**self.behavior.identities(program), 'sdk_source': program['sdk_source'],
                   'diagnostic_version': version, 'results': rows}
        return {**receipt, 'receipt': receipt, 'test_receipt': digest(receipt)}

    def teach(self, version, specs):
        self.responses.append(self.receipt(version, specs))
        return self.behavior.test(version, [x[0] for x in specs], 'service',
            '2026-10-01T00:00:00Z', '2026-10-01T01:00:00Z', {x[0]: x[1] for x in specs})

    def accept(self, version):
        program = json.loads(self.behavior.program_path(version).read_text())
        response = {**self.acceptance, 'diagnostic_version': version,
                    'test_receipt': program['test_receipt']}
        self.responses.append(response)
        return self.behavior.accept(version)

    def test_repair_inherits_complete_suite_and_preserves_incumbent_across_restart(self):
        self.compile()
        old = [('e' * 32, 'NON_MATCH', 'NON_MATCH', ''), ('f' * 32, 'UNKNOWN', 'UNKNOWN', '')]
        self.teach(self.version, old)
        self.accept(self.version)
        candidate = self.candidate()['diagnostic_version']
        # Compiling and testing only one case cannot replace the accepted version.
        self.teach(candidate, old[:1])
        with self.assertRaisesRegex(ValueError, 'complete accumulated'):
            self.behavior.accept(candidate)
        restarted = Behavior(self.config)
        state = restarted.compilation_status(candidate)
        self.assertEqual(state['incumbent'], self.version)
        self.assertEqual((state['tested_cases'], state['required_cases']), (1, 2))
        self.teach(candidate, old[1:])
        self.accept(candidate)
        self.assertEqual(restarted.compilation_status(candidate)['incumbent'], candidate)
        self.assertEqual(restarted.context(self.version)[0]['version'], self.version)

    def test_latest_passing_batch_cannot_hide_error_or_unknown_regression(self):
        for actual, reason in [('ERROR', "unsupported evidence field 'id'"),
                               ('ERROR', "unsupported evidence field 'parent_id'"),
                               ('ERROR', "'TraceView' object has no attribute 'attrs'"),
                               ('MATCH', 'UNKNOWN regression')]:
            with self.subTest(reason=reason):
                self.compile()
                self.teach(self.version, [('e' * 32, 'UNKNOWN', actual, reason)])
                self.teach(self.version, [('f' * 32, 'MATCH', 'MATCH', '')])
                calls = len(self.calls)
                with self.assertRaisesRegex(ValueError, 'Accumulated regression'):
                    self.behavior.accept(self.version)
                self.assertEqual(len(self.calls), calls)
                self.assertIsNone(self.behavior.compilation_status(self.version)['incumbent'])

    def test_replay_executes_more_than_eight_cases_even_after_first_batch_error(self):
        self.compile()
        specs = [(f'{i:032x}', 'NON_MATCH', 'NON_MATCH', '') for i in range(10)]
        self.teach(self.version, specs[:8])
        self.teach(self.version, specs[8:])
        self.accept(self.version)
        candidate = self.candidate()['diagnostic_version']
        failed = [(t, e, 'ERROR', "unsupported evidence field 'id'") for t, e, a, r in specs[:8]]
        self.responses.extend([self.receipt(candidate, failed), self.receipt(candidate, specs[8:])])
        result = self.behavior.run_regressions(candidate)
        self.assertEqual(result['attempted_cases'], 10)
        self.assertEqual(len(result['test_receipts']), 2)
        self.assertEqual(result['status'], 'CANNOT_PROMOTE')
        self.assertEqual(result['incumbent'], self.version)
        with self.assertRaisesRegex(ValueError, 'Accumulated regression'):
            self.behavior.accept(candidate)

    def test_failed_replay_cannot_reuse_old_passing_rows(self):
        self.compile()
        specs = [(f'{i:032x}', 'NON_MATCH', 'NON_MATCH', '') for i in range(10)]
        self.teach(self.version, specs[:8])
        self.teach(self.version, specs[8:])
        self.accept(self.version)
        request = self.behavior.request
        calls = []
        def fail_first(method, path, payload=None):
            calls.append(payload)
            if len(calls) == 1:
                raise RuntimeError('deployed test unavailable')
            return self.receipt(self.version, specs[8:])
        self.behavior.request = fail_first
        result = self.behavior.run_regressions(self.version)
        self.behavior.request = request
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['status'], 'CANNOT_PROMOTE')
        self.assertEqual(result['incumbent'], self.version)
        with self.assertRaisesRegex(ValueError, 'complete accumulated'):
            self.behavior.accept(self.version)

    def test_compile_failure_and_receipt_tampering_do_not_replace_incumbent(self):
        self.compile()
        result = self.teaching_test()
        self.accept(self.version)
        self.responses.append({'error': 'compilation failed'})
        self.behavior.compile('Authored check', 'service', 'broken', {})
        self.assertEqual(self.behavior.compilation_status(self.version)['incumbent'], self.version)
        Path(result['receipt_file']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'regression receipt'):
            self.behavior.accept(self.version)
        self.assertEqual(self.behavior.compilation_status(self.version)['incumbent'], self.version)

    def test_changing_gold_requires_explicit_new_contract(self):
        self.compile()
        self.teach(self.version, [('e' * 32, 'UNKNOWN', 'UNKNOWN', '')])
        with self.assertRaisesRegex(ValueError, 'gold changed'):
            self.teach(self.version, [('e' * 32, 'MATCH', 'MATCH', '')])
        self.assertEqual(self.behavior.compilation_status(self.version)['cases'][0]['expected'], 'UNKNOWN')

    def test_mcp_advertises_regression_tools(self):
        names = {tool['name'] for tool in tools_list(self.behavior)['tools']}
        self.assertTrue({'get_behavior_compilation', 'run_behavior_regressions'} <= names)


if __name__ == '__main__':
    unittest.main()
