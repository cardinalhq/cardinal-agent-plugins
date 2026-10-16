import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_bundle as bundle

INV = 'inv_' + 'a' * 24
RCPT = 'rcpt_' + 'b' * 24


class DetailsTests(unittest.TestCase):
    def test_details_round_trip_and_capability_fail_closed(self):
        event = {'type': 'experiment.completed', 'id': 'exp_remote', 'outcome': 'One lane failed',
                 'lanes': [{'lane': 'consumer', 'h': '64', 'state': 'failed'}],
                 'run': {'host': 'aws-worker', 'source_commit': 'c' * 40, 'image_digest': 'sha256:' + 'd' * 64}}
        events = ie.checkpoint_events([event])
        self.assertEqual(events[0]['payload']['lanes'], event['lanes'])
        with self.assertRaisesRegex(ie.CheckpointInputError, 'upgrade Maestro'):
            ie.checkpoint_capabilities(events, {})
        ie.checkpoint_capabilities(events, {'capabilities': {'checkpoint_details': True, 'checkpoint_artifacts': True}})

    def test_invalid_details_rejected(self):
        for name, value in [('lanes', [{'lane': 'x', 'state': 'maybe'}]),
                            ('lanes', [{'lane': 'x', 'state': 'failed'}] * 2),
                            ('lanes', [{'lane': '../x', 'state': 'failed', 'extra': True}]),
                            ('run', {'source_commit': 'short'}), ('run', {'image_digest': 'latest'}),
                            ('run', {'host': 'x\nforged'})]:
            with self.subTest(name=name, value=value), self.assertRaises(ie.CheckpointInputError):
                ie.validate_structured(name, value)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.source = self.root / 'worker'; self.source.mkdir()
        self.data = b'child=1 peak_rss=12345\n'; (self.source / 'measurements.log').write_bytes(self.data)
        self.hash = hashlib.sha256(self.data).hexdigest()
        self.event = {'seq': 7, 'investigation_id': INV, 'class': 'semantic', 'type': 'experiment.completed',
                      'payload': {'semantic_id': 'exp_remote', 'outcome': 'Measured on AWS', 'evidence': [RCPT],
                                  'run': {'host': 'aws-worker'},
                                  'artifacts': [{'path': 'measurements.log', 'sha256': self.hash, 'role': 'report'}]}}

    def export(self, root=None):
        out = self.root / 'bundle'
        with patch.object(bundle, 'selected', return_value=[self.event]), patch.object(bundle.sync, '_post', return_value={'receipt': {'provenance': 'captured', 'result': 'peak=12345'}}):
            bundle.export({'token': 'test'}, INV, '7', out, root=root, client='test')
        return out

    def test_portable_artifact_and_receipt_then_tamper_detected(self):
        out = self.export(self.source)
        self.assertEqual(bundle.verify(out), 0)
        (out / 'artifacts' / self.hash).write_bytes(b'changed')
        self.assertEqual(bundle.verify(out), 1)

    def test_missing_artifacts_explicit_and_existing_destination_preserved(self):
        out = self.export()
        manifest = json.loads((out / 'manifest.json').read_text())
        self.assertFalse(manifest['artifacts'][0]['included'])
        self.assertEqual(bundle.verify(out), 0)  # integrity only, never a verified remote claim
        with self.assertRaises(ValueError): self.export()

    def test_wrong_source_and_escaping_symlink_leave_no_bundle(self):
        (self.source / 'measurements.log').write_bytes(b'wrong run')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'): self.export(self.source)
        self.assertFalse((self.root / 'bundle').exists())
        (self.source / 'measurements.log').unlink()
        (self.root / 'outside.log').write_bytes(self.data)
        (self.source / 'measurements.log').symlink_to(self.root / 'outside.log')
        with self.assertRaisesRegex(ValueError, 'escapes'): self.export(self.source)

    def test_bundle_path_traversal_rejected(self):
        out = self.export()
        file = out / 'manifest.json'; manifest = json.loads(file.read_text())
        manifest['files'][0]['path'] = '../outside.log'; file.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): bundle.verify(out)
