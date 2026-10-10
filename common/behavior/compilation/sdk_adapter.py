"""Configurable adapter to the existing SDK compiler and embedded-JEV runtime.

The application supplies its pinned SDK import environment and existing profile,
JEV factory, sandbox runner and evidence auditor. No SDK or prompts are vendored.
"""
import dataclasses
import functools
import json
import time
from pathlib import Path
from .engine import RepairFailure
from .authoring_validation import execution_path_errors
from .wording_repair import literal_judge_arguments
from .storage import read, write, digest


def judge_site_validation(plan):
    """Report every declared proposition's documented JEV length bound together."""
    measurements, errors = [], []
    sites = plan.get('judge_sites', []) if isinstance(plan, dict) else []
    if not isinstance(sites, list):
        return measurements, [{'code': 'INVALID_JUDGE_SITES', 'message': 'judge_sites must be a list.'}]
    for index, site in enumerate(sites):
        proposition = site.get('proposition') if isinstance(site, dict) else None
        size = len(proposition) if isinstance(proposition, str) else None
        measured = {'index': index, 'name': site.get('name') if isinstance(site, dict) else None,
                    'characters': size, 'minimum': 1, 'maximum': 1000,
                    'valid': size is not None and 1 <= size <= 1000}
        measurements.append(measured)
        if not measured['valid']:
            errors.append({'code': 'JUDGE_PROPOSITION_BOUNDS', **measured,
                'message': f"Judge site {measured['name']!r} proposition has {size} characters; required 1-1000."})
    return measurements, errors


class SDK:
    def __init__(self, root, *, profile_factory, judge_factory, program_runner,
                 audit_result, metrics, verify, jev_config, population,
                 evaluation_policy, finality_policy):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_factory, self.judge_factory = profile_factory, judge_factory
        self.program_runner, self.audit_result, self.metrics = program_runner, audit_result, metrics
        self.verify, self.jev_config, self.population = verify, jev_config, population
        self.evaluation_policy, self.finality_policy = evaluation_policy, finality_policy
        verify()

    def compile(self, name, source, plan, description, context, *,
                teaching_examples=(), author=None, max_revisions=0,
                parent_candidate=None, correction=None):
        self.verify()
        out = self.root / name
        if not out.resolve().is_relative_to(self.root.resolve()) or out == self.root:
            raise ValueError('invalid compilation name')
        out.mkdir()
        (out / 'program.py').write_text(source)
        write(out / 'plan.json', plan)
        errors = execution_path_errors(source)
        measurements, site_errors = judge_site_validation(plan)
        errors.extend(site_errors)
        argument_measurements, _ = literal_judge_arguments(source)
        for measured in argument_measurements:
            if not measured['valid']:
                errors.append({'code': 'JUDGE_' + measured['field'].upper() + '_BOUNDS',
                    **{k: v for k, v in measured.items() if k != 'value'},
                    'message': f"judge.decide {measured['field']} at line {measured['line']} has "
                               f"{measured['characters']} characters; maximum {measured['maximum']}."})
        if author is not None:
            # This is only the retained seed; the canonical compiler will ask
            # the author for a fresh proposal and validate each actual round.
            errors = []
        validation = {'status': 'SEED_ONLY' if author else ('FAIL' if errors else 'PASS'), 'errors': errors,
                      'stage': 'retained-seed' if author else 'authoring-mechanical-validation',
                      'source_sha256': digest(source.encode()),
                      'judge_site_measurements': measurements,
                      'judge_argument_measurements': argument_measurements}
        write(out / 'mechanical-validation.json', validation)
        if errors:
            raise RepairFailure({**validation, 'draft': {'udf_source': source, 'compile_plan': plan},
                                 'receipt_paths': [str(out / 'mechanical-validation.json')]})
        from behavior_runtime.diagnostics import ArtifactStore
        from behavior_runtime.compiler import Compiler, CompilerConfig, CompileRequest, CompileFailure, TeachingExample
        store_path = Path(parent_candidate['store']) if parent_candidate else out / 'artifacts'
        store = ArtifactStore(store_path)
        config = CompilerConfig(model='teaching-author' if author else 'supplied-source',
                                model_version='1', max_revisions=max_revisions if author else 0,
                                max_output_tokens=32768)
        supplied = lambda _: {
            'model': 'supplied-source', 'model_version': '1',
            'text': __import__('json').dumps({'udf_source': source, 'compile_plan': plan}),
            'usage': {'input_tokens': 0, 'output_tokens': 0}}
        checked_round = 0
        def checked_author(packet):
            nonlocal checked_round
            response = author(packet)
            proposal = json.loads(response['text'])
            failures = execution_path_errors(proposal['udf_source'])
            _, site_failures = judge_site_validation(proposal['compile_plan'])
            failures.extend(site_failures)
            args, _ = literal_judge_arguments(proposal['udf_source'])
            failures.extend({'code': 'JUDGE_ARGUMENT_BOUNDS', 'field': a['field'],
                             'characters': a['characters']} for a in args if not a['valid'])
            write(out / f'author-validation-{checked_round}.json', {
                'status': 'FAIL' if failures else 'PASS', 'errors': failures,
                'source_sha256': digest(proposal['udf_source'].encode())})
            checked_round += 1
            if failures:
                raise ValueError(json.dumps(failures))
            return response
        compiler = Compiler(store, checked_author if author else supplied, self.judge_factory, config,
            program_runner=self.program_runner)
        request = CompileRequest(diagnostic_id=name.split('-')[0], behavior_description=description,
            optional_user_context=context, examples=tuple(TeachingExample(**e) for e in teaching_examples),
            profile=self.profile_factory(),
            jev_config=self.jev_config, population=self.population,
            evaluation_policy=self.evaluation_policy, finality_policy=self.finality_policy)
        if parent_candidate:
            request = dataclasses.replace(request, parent_version=parent_candidate['version'],
                                          correction=correction)
        try:
            result = compiler.compile(request)
        except CompileFailure as exc:
            # A correction shares its parent's store. Bind the failure to its
            # actual canonical receipt, rather than looking in an empty child.
            receipts = [store_path / 'compile_receipts' / (exc.receipt_id + '.json')]
            errors = [error for p in receipts for round in read(p)['rounds'] for error in round['errors']]
            raise RepairFailure({'errors': errors, 'judge_site_measurements': measurements,
                                 'judge_argument_measurements': argument_measurements, 'draft': {'udf_source': source, 'compile_plan': plan},
                                 'receipt_paths': [str(p) for p in receipts]}) from exc
        write(out / 'compile-receipt.json', result.receipt)
        # The compiler may have revised the initial draft. Persist its actual
        # frozen source/plan, never bind the old draft to the new version.
        (out / 'program.py').write_text(result.udf_source)
        (out / 'plan.json').write_text(json.dumps(dataclasses.asdict(result.compile_plan), indent=2) + '\n')
        descriptor = {'version': result.diagnostic_version.version, 'source': str(out / 'program.py'),
                      'plan': str(out / 'plan.json'), 'store': str(store_path)}
        write(out / 'descriptor.json', descriptor)
        return descriptor

    def evaluate(self, candidate, trace):
        from behavior_runtime.diagnostics import ArtifactStore
        self.verify()
        store = ArtifactStore(candidate['store'])
        version = store.version(candidate['version'])
        source = Path(candidate['source']).read_text()
        if digest(source.encode()) != version.source_sha256:
            raise ValueError('source/version binding failed')
        started = time.monotonic()
        try:
            row = dataclasses.asdict(store.evaluate(version.version, trace, profile=self.profile_factory(),
                judge_factory=self.judge_factory, finalized=True,
                _program_runner=functools.partial(self.program_runner, source)))
            # SDK dataclasses retain tuples. Model packets serialize those as
            # JSON arrays, so validate citations against that same representation.
            row = json.loads(json.dumps(row, ensure_ascii=False, allow_nan=False))
        except Exception as exc:
            row = {'verdict': 'ERROR', 'reason': f'{type(exc).__name__}: {exc}', 'jev_receipts': [],
                   'records': [], 'witness_refs': []}
        audit = self.audit_result(row, trace)
        usage = self.metrics(row, (time.monotonic() - started) * 1000)
        return {**row, 'actual': row['verdict'], 'mechanical_pass': audit['mechanical_status'] == 'PASS',
                'audit': audit, 'metrics': {**usage, 'cost_usd': None}, 'trace_sha256': digest(trace)}
