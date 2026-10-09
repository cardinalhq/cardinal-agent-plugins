import ast
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'compilation'


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class CompilationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.provenance = json.loads((PACKAGE / 'provenance.json').read_text())

    def test_production_execution_methods_are_unchanged(self):
        source = (ROOT / 'server.py').read_text()
        tree = ast.parse(source)
        behavior = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Behavior')
        methods = {n.name: n for n in behavior.body if isinstance(n, ast.FunctionDef)}
        for name, expected in self.provenance['production_methods_source_sha256'].items():
            self.assertEqual(sha(ast.get_source_segment(source, methods[name])), expected, name)

    def test_semantic_discovery_review_and_repair_instructions_are_unchanged(self):
        tree = ast.parse((PACKAGE / 'corpus.py').read_text())
        instruction = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'instruction' for t in n.targets))
        self.assertEqual(sha(ast.dump(instruction, include_attributes=False)),
                         self.provenance['corpus_instruction_ast_sha256'])
        for filename, role, key in [('corpus.py', 'blind-fidelity-and-gold-reviewer', 'review'),
                                     ('workflow.py', 'program-repair', 'repair')]:
            tree = ast.parse((PACKAGE / filename).read_text())
            text = next(n.args[2].value for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == 'call'
                and len(n.args) > 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value == role)
            self.assertEqual(sha(text), self.provenance[key + '_instruction_sha256'])

    def test_no_experiment_workspace_or_bpq_import_dependency(self):
        for path in PACKAGE.glob('*.py'):
            text = path.read_text()
            for forbidden in ('/Users/', 'from bpq.', 'behavioral-compilation-correctness',
                              'behavioral-factorization-20261008'):
                self.assertNotIn(forbidden, text, path.name)


if __name__ == '__main__':
    unittest.main()
