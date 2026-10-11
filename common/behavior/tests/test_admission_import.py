import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_qualified_admission import AdmissionTests
from compilation.admission import QualifiedRegistry
from compilation.admission_import import admit_completed_job
from compilation.engine import Policy
from compilation.storage import read, write, digest, filehash


class ImportTests(unittest.TestCase):
    def setUp(self):
        fixture = AdmissionTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        job_id, identity = fixture.make_job()
        self.source = fixture.jobs / job_id
        self.bundle = self.source / 'cycle/bundle'
        self.contract = fixture.root / 'operator-contract.json'
        write(self.contract, {'description': 'generic qualified question'})
        manifest = self.bundle / 'frozen-qualified-manifest.json'
        write(manifest, {'generic_test_bundle': True})
        self.digest = filehash(manifest)
        cert = read(self.bundle / 'certificate.json')
        cert['identity']['contract'] = digest(read(self.contract))
        cert['files']['frozen-qualified-manifest.json'] = self.digest
        self.reseal(cert)
        self.catalog_identity = {k: identity[k] for k in ('agent', 'telemetry_schema')}
        self.jobs = fixture.root / 'target/jobs'
        self.jobs.mkdir(parents=True)
        self.registry = QualifiedRegistry(fixture.root / 'target/qualified', self.jobs,
            lambda agent, expected: {**expected, **self.catalog_identity}, fixture.run_sdk)
        self.host = SimpleNamespace(registry=self.registry,
            catalog=SimpleNamespace(identity=lambda agent: self.catalog_identity),
            authorize=lambda *args: None,
            lifecycle=SimpleNamespace(policy=Policy(audit_per_label=1),
                runtime=SimpleNamespace(verify=lambda: None, identity=lambda: digest('sdk'), jev_config='jev'),
                start=lambda *args: self.fail('import started a campaign')))
        self.config = {'admission_contracts': {'agent': str(self.contract)}}

    def reseal(self, cert):
        cert.pop('certificate_sha256', None)
        cert['certificate_sha256'] = digest(cert)
        (self.bundle / 'certificate.json').write_text(json.dumps(cert))

    def run_import(self, **kwargs):
        return admit_completed_job(self.host, self.config, self.source, 'agent', kwargs.get('digest', self.digest))

    def assert_no_admission(self):
        self.assertEqual(list(self.registry.root.glob('*/admission.json')), [])
        self.assertEqual(self.fixture.calls, [])

    def test_completed_job_import_and_second_import_are_idempotent(self):
        with patch.object(self.registry, 'issue', wraps=self.registry.issue) as issue:
            first = self.run_import()
            second = self.run_import()
        self.assertFalse(first['idempotent'])
        self.assertTrue(second['idempotent'])
        self.assertEqual(first['qualified_id'], second['qualified_id'])
        self.assertEqual(issue.call_count, 1)
        self.assertEqual(filehash(self.registry.root / first['qualified_id'] / 'bundle/frozen-qualified-manifest.json'), self.digest)
        self.assertEqual(len(list(self.jobs.iterdir())), 1)
        self.assertEqual(self.fixture.calls, [])
        self.assertEqual((self.jobs / self.source.name).stat().st_mode & 0o777, 0o700)

    def test_wrong_bundle_digest(self):
        with self.assertRaisesRegex(ValueError, 'bundle digest mismatch'):
            self.run_import(digest='0' * 64)
        self.assert_no_admission()

    def test_failed_certificate_even_with_recomputed_hash(self):
        cert = read(self.bundle / 'certificate.json')
        cert['status'] = 'CANNOT_QUALIFY'
        self.reseal(cert)
        with self.assertRaisesRegex(ValueError, 'not qualified'):
            self.run_import()
        self.assert_no_admission()

    def test_failed_independent_audit_cannot_issue(self):
        p = self.source / 'cycle/qualification/audit.json'
        rows = read(p)
        rows[0]['actual'] = 'ERROR'
        p.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError, 'independent qualification evidence failed'):
            self.run_import()
        self.assert_no_admission()

    def test_catalog_mismatch(self):
        self.catalog_identity['agent'] = digest('different agent')
        with self.assertRaisesRegex(ValueError, 'requalification required: agent'):
            self.run_import()
        self.assert_no_admission()

    def test_target_contract_mismatch(self):
        self.contract.write_text(json.dumps({'description': 'different contract'}))
        with self.assertRaisesRegex(ValueError, 'requalification required: contract'):
            self.run_import()
        self.assert_no_admission()

    def test_target_runtime_mismatch(self):
        self.host.lifecycle.runtime.identity = lambda: digest('different sdk')
        with self.assertRaisesRegex(ValueError, 'requalification required: sdk'):
            self.run_import()
        self.assert_no_admission()

    def test_target_jev_mismatch(self):
        self.host.lifecycle.runtime.jev_config = 'different jev'
        with self.assertRaisesRegex(ValueError, 'requalification required: jev'):
            self.run_import()
        self.assert_no_admission()

    def test_target_policy_mismatch(self):
        self.host.lifecycle.policy = Policy(audit_per_label=4)
        with self.assertRaisesRegex(ValueError, 'policy mismatch'):
            self.run_import()
        self.assert_no_admission()

    def test_requires_operator_configured_contract(self):
        self.config = {}
        with self.assertRaisesRegex(ValueError, 'configure an admission contract'):
            self.run_import()
        self.assert_no_admission()

    def test_symlink_job_artifact_rejected(self):
        (self.source / 'outside-link').symlink_to(self.contract)
        with self.assertRaisesRegex(ValueError, 'symlink or special file'):
            self.run_import()
        self.assert_no_admission()

    def test_changed_evidence_on_repeat_rejected(self):
        self.run_import()
        (self.source / 'new-evidence.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'different evidence'):
            self.run_import()
        self.assertEqual(len(list(self.registry.root.glob('*/admission.json'))), 1)
        self.assertEqual(self.fixture.calls, [])

    def test_repeat_cannot_reactivate_disabled_admission(self):
        first = self.run_import()
        self.registry.control(first['qualified_id'], 'disable')
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.run_import()
        self.assertEqual(len(list(self.registry.root.glob('*/admission.json'))), 1)


if __name__ == '__main__':
    unittest.main()
