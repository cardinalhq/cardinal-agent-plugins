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


class SDKPacketTests(unittest.TestCase):
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
