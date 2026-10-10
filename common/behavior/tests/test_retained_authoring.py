import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compilation.retained_authoring import FILES, prepare_retained
from compilation.storage import digest, filehash, write


class RetainedAuthoringTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.old, self.new = Path(tmp.name)/'old', Path(tmp.name)/'new'
        self.snapshot = {'sources': {'instructions': 'real source'}, 'traces': ['authentic'],
                         'selection': {'receipt': 'origin', 'retrieval_latency_ms': 1}}
        self.contract = {'description': 'original intent'}
        self.request = {'agent_id': 'megan', 'behavioral_question': 'original question',
                        'time_range': None, 'policy': {'max_repairs': 2}}
        self.identity = {'agent': 'agent-digest', 'telemetry_schema': 'schema-digest'}
        self.runtime = SimpleNamespace(identity=lambda: 'sdk-digest', jev_config={'model': 'unchanged'})
        values = {'request.json': self.request, 'discovery-input.json': self.snapshot,
                  'discovery.json': {'status': 'READY'}, 'contract.json': self.contract,
                  'generated.json': {'udf_source': 'def evaluate(): raise RuntimeError()'},
                  'identity.json': {**self.identity, 'contract': digest(self.contract),
                                    'sdk': 'sdk-digest', 'jev': digest(self.runtime.jev_config)},
                  'outcome.json': {'status': 'CANNOT_QUALIFY'}}
        for name, value in values.items(): write(self.old/name, value)
        write(self.new/'request.json', self.request)
        self.config = {'job': str(self.old), 'files': {n:filehash(self.old/n) for n in FILES}}

    def prepare(self, **kwargs):
        return prepare_retained(self.config, self.new, kwargs.get('snapshot', self.snapshot),
                                self.identity, self.runtime, 'megan', kwargs.get('question', 'original question'))

    def test_reuses_only_design_inputs_and_preserves_original(self):
        write(self.old/'heldout.json', {'expected': 'SECRET GOLD'})
        fresh = copy.deepcopy(self.snapshot); fresh['selection']['retrieval_latency_ms'] = 300
        discovery, contract, generated = self.prepare(snapshot=fresh)
        self.assertEqual(contract, self.contract)
        self.assertNotIn('SECRET GOLD', str((discovery, contract, generated)))
        self.assertTrue((self.new/'original-generated.json').exists())
        self.assertEqual({n:filehash(self.old/n) for n in FILES}, self.config['files'])

    def test_changed_question_rejected(self):
        with self.assertRaisesRegex(ValueError, 'original question'): self.prepare(question='easier question')

    def test_changed_program_rejected(self):
        (self.old/'generated.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'input drift'): self.prepare()

    def test_started_qualification_never_resets(self):
        (self.old/'cycle/qualification').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'reset qualification'): self.prepare()

    def test_new_telemetry_rejected(self):
        fresh = copy.deepcopy(self.snapshot); fresh['traces'] = ['different']
        with self.assertRaisesRegex(ValueError, 'telemetry drift'): self.prepare(snapshot=fresh)

    def test_runtime_change_rejected(self):
        self.runtime.jev_config = {'model': 'different'}
        with self.assertRaisesRegex(ValueError, 'JEV drift'): self.prepare()

    def test_policy_change_rejected(self):
        (self.new/'request.json').write_text('{"time_range":null,"policy":{"max_repairs":99}}')
        with self.assertRaisesRegex(ValueError, 'qualification policy'): self.prepare()
