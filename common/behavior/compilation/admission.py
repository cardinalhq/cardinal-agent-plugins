"""Trusted host admission for qualified execution, separate from teaching acceptance.

Only the host's lifecycle worker calls issue(). API callers supply opaque IDs,
never certificates, identity fingerprints, filesystem paths or Python. The key
and registry must live in the host's private storage, outside client access.
"""
from collections import Counter
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import time

from .engine import gate, check_cases
from .files import private_json, program_lock
from .runtime import load_frozen
from .storage import canonical, digest, filehash, read, write


class QualifiedRegistry:
    def __init__(self, root, jobs, observe, run_sdk, *, coverage_execute=None):
        self.root, self.jobs = Path(root).resolve(), Path(jobs).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.observe, self.run_sdk = observe, run_sdk
        # Optional trusted host integration; never a request-supplied callback.
        # The configured adapter resolves the source execution from original
        # telemetry and invokes generic completeness admission + result CAS.
        self.coverage_execute = coverage_execute
        key = self.root / 'issuer.key'
        with program_lock(self.root / 'registry'):
            if not key.exists():
                fd = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(secrets.token_bytes(32))
            if key.stat().st_mode & 0o077:
                raise ValueError('qualification issuer key must be private')
        self.key = key

    def _sign(self, value):
        return hmac.new(self.key.read_bytes(), canonical(value), hashlib.sha256).hexdigest()

    def _save(self, path, value):
        private_json(path, {'value': value, 'signature': self._sign(value)})

    def _read(self, path):
        sealed = read(path)
        if not hmac.compare_digest(sealed['signature'], self._sign(sealed['value'])):
            raise ValueError('untrusted qualification registry record')
        return sealed['value']

    def _record(self, qualified_id):
        if not re.fullmatch('[a-f0-9]{64}', qualified_id):
            raise ValueError('invalid qualified identity')
        return self._read(self.root / qualified_id / 'admission.json')

    def _validate(self, qualified_id, *, enabled=True):
        record = self._record(qualified_id)
        if record['revoked'] or (enabled and not record['enabled']):
            raise ValueError('qualification revoked or disabled')
        observed = self.observe(record['agent_id'], record['identity'])
        # observe must rehash actual source/schema/runtime; no request identity is accepted.
        if observed != record['identity']:
            raise ValueError('identity drift requires requalification')
        bundle = self.root / qualified_id / 'bundle'
        cert, source = load_frozen(bundle, {k: observed[k] for k in
            ('program', 'agent', 'telemetry_schema', 'contract', 'sdk', 'jev')})
        if digest(cert) != record['certificate_sha256']:
            raise ValueError('qualification certificate identity changed')
        if filehash(bundle / 'plan.json') != observed['compile_plan']:
            raise ValueError('CompilePlan identity changed')
        if digest(read(bundle / 'qualification.json')) != record['qualification_sha256']:
            raise ValueError('qualification evidence changed')
        for name, sha in record['evidence_manifest'].items():
            path = (self.jobs / record['job_id'] / name).resolve()
            if not path.is_relative_to(self.jobs / record['job_id']) or filehash(path) != sha:
                raise ValueError('qualification evidence manifest changed')
        return record, bundle, cert, source

    def issue(self, job_id, agent_id, question, observed):
        """Internal worker-only method, after a completed canonical lifecycle."""
        if not re.fullmatch('[a-f0-9]{32}', job_id):
            raise ValueError('invalid lifecycle job')
        job = self.jobs / job_id
        cycle = job / 'cycle'
        result = read(cycle / 'outcome.json')
        if result['status'] != 'QUALIFIED':
            raise ValueError('development accepted is not qualified')
        cert, _ = load_frozen(cycle / 'bundle', {k: observed[k] for k in
            ('program', 'agent', 'telemetry_schema', 'contract', 'sdk', 'jev')})
        audit = read(cycle / 'qualification/audit.json')
        audit_cases = read(cycle / 'qualification/audit-cases.json')
        policy = read(cycle / 'qualification/policy.json')
        qualification = read(cycle / 'qualification/qualification.json')
        used = read(cycle / 'qualification/discovery.json')
        for path in (cycle / 'qualification').glob('challenges-*.json'):
            used += read(path)
        ids, hashes = check_cases(used)
        check_cases(audit_cases, ids, hashes)
        expected_audit = {c['id']: c for c in audit_cases}
        if (len(audit) != len(expected_audit)
                or {r['id'] for r in audit} != set(expected_audit)) or any(
            r['id'] not in expected_audit or r['expected'] != expected_audit[r['id']]['expected']
            or r['trace_sha256'] != digest(expected_audit[r['id']]['trace'])
            or r['version'] != cert['version'] for r in audit):
            raise ValueError('audit cases do not bind execution receipts')
        if (not gate(audit, policy['audit_per_label'])['passed']
                or digest(audit_cases) != qualification['audit_sha256']
                or digest(policy) != qualification['policy_sha256']
                or result['candidate']['version'] != cert['version']):
            raise ValueError('independent qualification evidence failed')
        evidence = {str(p.relative_to(job)): filehash(p) for p in job.rglob('*')
                    if p.is_file() and p.name != 'admission.json'}
        identity = {**observed, 'compile_plan': filehash(cycle / 'bundle/plan.json'),
                    'policy': digest(policy), 'evidence_manifest': digest(evidence)}
        record = {'status': 'QUALIFIED', 'agent_id': agent_id, 'question': question,
                  'slot': digest([agent_id, question]), 'job_id': job_id,
                  'identity': identity, 'certificate_sha256': digest(cert),
                  'version': cert['version'], 'qualification_sha256': digest(qualification),
                  'evidence_manifest': evidence, 'enabled': True, 'revoked': False,
                  'issued_at': time.time()}
        qualified_id = digest(record)
        with program_lock(self.root / 'registry'):
            destination = self.root / qualified_id
            destination.mkdir()
            shutil.copytree(cycle / 'bundle', destination / 'bundle')
            self._save(destination / 'admission.json', record)
            self._validate(qualified_id)
            self._save(self.root / (record['slot'] + '.active.json'), {'qualified_id': qualified_id})
        return {'status': 'QUALIFIED', 'qualified_id': qualified_id, 'version': cert['version'],
                'identity': identity}

    def inspect(self, qualified_id):
        with program_lock(self.root / 'registry'):
            record = self._record(qualified_id)
            executions = []
            for path in (self.root / 'executions').glob('*/receipt.json'):
                receipt = self._read(path)
                if receipt['qualified_id'] == qualified_id:
                    executions.append({k: receipt[k] for k in
                        ('execution_id', 'status', 'eligible_executions', 'counts')})
            return {**record, 'executions': executions}

    def execution_receipt(self, qualified_id, execution_id):
        with program_lock(self.root / 'registry'):
            self._record(qualified_id)
            if not re.fullmatch('[a-f0-9]{32}', execution_id):
                raise ValueError('invalid execution id')
            receipt = self._read(self.root / 'executions' / execution_id / 'receipt.json')
            if receipt['qualified_id'] != qualified_id:
                raise ValueError('execution belongs to another qualification')
            return receipt

    def control(self, qualified_id, action, target_id=None):
        with program_lock(self.root / 'registry'):
            record = self._record(qualified_id)
            if action in ('disable', 'revoke'):
                record['enabled'] = False
                record['revoked'] = record['revoked'] or action == 'revoke'
                self._save(self.root / qualified_id / 'admission.json', record)
            elif action == 'rollback':
                prior, _, _, _ = self._validate(target_id, enabled=False)
                active = self._read(self.root / (record['slot'] + '.active.json'))
                if (prior['slot'] != record['slot'] or prior['issued_at'] >= record['issued_at']
                        or active['qualified_id'] != qualified_id):
                    raise ValueError('rollback requires a prior version of the same active diagnostic')
                prior['enabled'] = True
                self._save(self.root / target_id / 'admission.json', prior)
                self._save(self.root / (record['slot'] + '.active.json'), {'qualified_id': target_id})
            else:
                raise ValueError('unsupported qualification control')
            event = {'qualified_id': qualified_id, 'action': action, 'target_id': target_id,
                     'at': time.time()}
            self._save(self.root / 'operations' / (secrets.token_hex(16) + '.json'), event)
            return event

    def execute(self, qualified_id, population):
        """Population comes from the host's trusted telemetry provider, not caller JSON."""
        with program_lock(self.root / 'registry'):
            record, bundle, cert, source = self._validate(qualified_id)
            active = self._read(self.root / (record['slot'] + '.active.json'))
            if active['qualified_id'] != qualified_id:
                raise ValueError('qualified version is not active; explicit rollback required')
            execution_id = secrets.token_hex(16)
            output = self.root / 'executions' / execution_id
            output.mkdir(parents=True)
            write(output / 'request.json', {'qualified_id': qualified_id,
                  'version': cert['version'], 'identity': record['identity'],
                  'population': population['selection'], 'started_at': time.time()})
            rows, seen = [], set()
            for trace in population['traces']:
                self._validate(qualified_id)  # fail closed on source/runtime drift between traces
                if trace.get('agent') != record['agent_id']:
                    raise ValueError('population agent does not match qualification')
                if trace['trace_id'] in seen:
                    raise ValueError('duplicate population trace')
                seen.add(trace['trace_id'])
                sdk_run = lambda prepared: self.run_sdk(bundle, cert, source, prepared)
                row = self.coverage_execute(trace, sdk_run) if self.coverage_execute else sdk_run(trace)
                if row.get('actual', row.get('verdict')) not in ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR'):
                    raise ValueError('unsupported SDK verdict')
                if row.get('mechanical_pass') is not True:
                    row = {**row, 'original_verdict': row.get('actual', row.get('verdict')),
                           'actual': 'ERROR', 'verdict': 'ERROR', 'reason': 'evidence provenance failed'}
                row = {**row, 'trace_id': trace['trace_id'], 'trace_sha256': digest(trace)}
                write(output / (digest(trace['trace_id']) + '.json'), {'trace': trace, 'execution': row})
                rows.append(row)
            counts = Counter(r.get('actual', r.get('verdict')) for r in rows)
            receipt = {'execution_id': execution_id, 'qualified_id': qualified_id,
                       'status': 'COMPLETED', 'eligible_executions': len(rows),
                       'counts': {v: counts[v] for v in ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR')},
                       'exclusions': population.get('exclusions', []),
                       'selection': population['selection'], 'results': rows}
            self._save(output / 'receipt.json', receipt)
            return receipt
