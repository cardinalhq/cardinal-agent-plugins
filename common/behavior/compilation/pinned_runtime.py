"""Bind the unchanged installed SDK/host and configured isolation runner.

Paths and exact SHA-256s are supplied by the operator, never by a question or
generated program. No SDK declarations or semantic APIs are duplicated here.
"""
from pathlib import Path
import importlib
import inspect
import json
import sys

from .sdk_adapter import SDK
from .storage import digest, filehash, read


class PinnedRuntime:
    def __init__(self, root, model_calls=None, runner_module='sandbox_runner'):
        self.root = Path(root).resolve()
        self.manifest = read(self.root / 'manifest.json')
        self.model_calls = Path(model_calls) if model_calls else None
        self.executions = []
        sys.path[:0] = [str(self.root / 'host/sdk'), str(self.root / 'host'), str(self.root)]
        from behavior_runtime.backend import configuration
        self.jev_config = configuration()
        self.runner_module = runner_module
        self.verify()

    def verify(self):
        for name, sha in self.manifest['files'].items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root) or filehash(path) != sha:
                raise ValueError('pinned runtime file drift: ' + name)
        from behavior_runtime.programs import identities
        if identities() != self.manifest['identities']:
            raise ValueError('canonical SDK/profile/runtime identity drift')
        import behavior_sdk
        if not Path(behavior_sdk.__file__).resolve().is_relative_to(self.root / 'host/sdk'):
            raise ValueError('another SDK is already imported in this process')
        from behavior_runtime.backend import configuration
        if configuration() != self.jev_config:
            raise ValueError('embedded JEV configuration drift')

    def identity(self):
        self.verify()
        return digest(self.manifest)

    def documentation(self):
        self.verify()
        from behavior_runtime.programs import sdk_contract
        import behavior_sdk
        # Signatures come from the verified installed artifact, never an API mirror.
        names = ('TraceView.select', 'EventSelection.available_before', 'EvidenceSet.from_events',
                 'Judge.preflight', 'Judge.decide', 'Recorder.unknown', 'record_decision', 'evidence')
        signatures = {}
        for name in names:
            target = behavior_sdk
            for part in name.split('.'):
                target = getattr(target, part, None)
            if target is not None:
                signatures[name] = str(inspect.signature(target))
        return {**sdk_contract(), 'installed_signatures': signatures}

    def documentation_text(self):
        return self.documentation()['documentation']

    def context(self, contract, snapshot):
        return json.dumps({'contract': contract, 'sources': snapshot['sources']}, ensure_ascii=False)

    def validate_trace(self, trace):
        from behavior_sdk import TraceView
        from behavior_sdk.native_profile import NativeProfile
        run = TraceView(trace, profile=NativeProfile())
        ids = [e.id for e in run.events]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('empty/duplicate native events')
        for event in run.events:
            # Missing parents are valid partial telemetry; raw-field validation
            # still applies and never depends on parent coverage.
            for field in ('input', 'output'):
                value = getattr(event, field)
                if value is not None and not isinstance(value, str):
                    raise ValueError('native input/output must be raw strings')
        return {'status': 'PASS'}

    def audit(self, row, trace):
        from behavior_sdk import TraceView, evidence
        from behavior_sdk.native_profile import NativeProfile
        from behavior_sdk.jev import _sha, JudgeConfig
        run = TraceView(trace, profile=NativeProfile())
        events = {e.id: e for e in run.events}
        refs = set(events) | {'run'}
        errors = []
        if not set(row.get('witness_refs', [])) <= refs:
            errors.append('invalid witness reference')
        for receipt in row.get('jev_receipts', []):
            try:
                if _sha({k: v for k, v in receipt.items() if k != 'receipt_id'}) != receipt['receipt_id']:
                    raise ValueError('receipt hash mismatch')
                if receipt['config'] != self.jev_config or JudgeConfig(**receipt['config']).digest != receipt['config_digest']:
                    raise ValueError('JEV identity mismatch')
                if not set(receipt['subject_refs']) <= refs:
                    raise ValueError('invalid subject reference')
                for item in receipt['evidence']:
                    obj = run.trace if item['ref'] == 'run' else events[item['ref']]
                    if item['text'] not in evidence(obj, item['field']).text:
                        raise ValueError('evidence not verbatim')
                if not set(receipt['decision_evidence_refs']) <= {e['ref'] for e in receipt['evidence']}:
                    raise ValueError('invalid decision reference')
                packet = {k: receipt[k] for k in ('proposition', 'frame', 'subject_refs', 'evidence')}
                if _sha(packet) != receipt['packet_sha256']:
                    raise ValueError('packet hash mismatch')
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(str(exc))
        return {'mechanical_status': 'FAIL' if errors else 'PASS', 'errors': errors,
                'semantic_binding_status': 'REQUIRES_INDEPENDENT_REVIEW'}

    @staticmethod
    def metrics(row, latency_ms):
        attempts = [a for r in row.get('jev_receipts', []) for a in r.get('attempts', [])]
        unknown_attempts = row.get('verdict') == 'ERROR' and not row.get('jev_receipts')
        def total(key):
            if unknown_attempts:
                return None  # An interrupted provider call may lack a returned receipt.
            values = [(a.get('usage') or {}).get(key) for a in attempts]
            return sum(values) if all(type(v) is int and v >= 0 for v in values) else None
        return {'semantic_calls': None if unknown_attempts else len(row.get('jev_receipts', [])),
                'provider_attempts': None if unknown_attempts else len(attempts),
                'input_tokens': total('input_tokens'), 'output_tokens': total('output_tokens'),
                'latency_ms': latency_ms, 'cost_usd': None}

    def sdk(self, root, agent_id):
        from behavior_sdk import Judge, JudgeConfig
        from behavior_sdk.native_profile import NativeProfile
        from behavior_runtime.backend import TypeSafeBackend
        runtime = self
        class RecordedSDK(SDK):
            def evaluate(self, candidate, trace):
                result = super().evaluate(candidate, trace)
                runtime.executions.append(result['metrics'])
                return result
        return RecordedSDK(root, profile_factory=NativeProfile,
            judge_factory=lambda trace, cfg: Judge(trace, JudgeConfig(**cfg), TypeSafeBackend()),
            program_runner=importlib.import_module(self.runner_module).evaluate_program,
            audit_result=self.audit, metrics=self.metrics, verify=self.verify,
            jev_config=self.jev_config, population={'agent': agent_id},
            evaluation_policy={**self.manifest['identities'], 'coverage_scope': 'selected-window-objects'},
            finality_policy={'on_open': 'UNKNOWN', 'terminal_claims': False})

    def dependencies(self, candidate):
        return {'plan.json': candidate['plan'], **{
            'artifacts/' + str(p.relative_to(candidate['store'])): p
            for p in Path(candidate['store']).rglob('*.json')}}

    def run_frozen(self, bundle, certificate, source, trace):
        self.verify()
        sdk = self.sdk(Path(bundle).parent / 'runtime', trace['agent'])
        return sdk.evaluate({'source': str(Path(bundle) / 'program.py'),
                             'store': str(Path(bundle) / 'artifacts'),
                             'plan': str(Path(bundle) / 'plan.json'),
                             'version': certificate['version']}, trace)

    def usage_rows(self, job_id=None, execution_start=0):
        rows = list(self.executions[execution_start:])
        if self.model_calls:
            for p in self.model_calls.glob('*/receipt.json'):
                if job_id and not p.parent.name.startswith(job_id + '-'):
                    continue
                receipt = read(p)
                rows.append({**(receipt.get('usage') or {}), 'latency_ms': receipt.get('latency_ms'),
                             'cost_usd': receipt.get('cost_usd'),
                             'provider_attempts': receipt.get('provider_attempts')})
        return rows
