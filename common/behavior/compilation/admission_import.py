"""Operator-only import of an already completed canonical qualification job.

This is deliberately not an HTTP/MCP upload route. The operator chooses the
job directory and expected bundle digest; target contracts come from trusted
host configuration. No authoring, model call or campaign is started here.
"""
from dataclasses import asdict
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .files import program_lock
from .runtime import load_frozen
from .storage import digest, filehash, read


def _inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('canonical job must be a real directory')
    files = {}
    for path in sorted(root.rglob('*')):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError('canonical job contains a symlink or special file')
        if path.name == 'issuer.key':
            raise ValueError('issuer keys are not canonical job artifacts')
        files[str(path.relative_to(root))] = filehash(path)
    return files


def _check(host, config, job, agent_id, bundle_digest):
    if not re.fullmatch('[a-f0-9]{64}', bundle_digest):
        raise ValueError('expected bundle digest must be a SHA256')
    cycle = job / 'cycle'
    outcome = read(cycle / 'outcome.json')
    if outcome.get('status') != 'QUALIFIED':
        raise ValueError('completed job is not qualified')
    bundle = cycle / 'bundle'
    manifest = 'frozen-qualified-manifest.json'
    if filehash(bundle / manifest) != bundle_digest:
        raise ValueError('qualified bundle digest mismatch')
    if outcome.get('bundle_digest', bundle_digest) != bundle_digest:
        raise ValueError('outcome bundle digest mismatch')
    contract_path = config.get('admission_contracts', {}).get(agent_id)
    if not contract_path:
        raise ValueError('target host must configure an admission contract')
    contract = read(contract_path)
    host.lifecycle.runtime.verify()
    observed = {
        'program': filehash(bundle / 'program.py'),
        **host.catalog.identity(agent_id),
        'contract': digest(contract),
        'sdk': host.lifecycle.runtime.identity(),
        'jev': digest(host.lifecycle.runtime.jev_config),
    }
    certificate, _ = load_frozen(bundle, observed)
    if certificate['files'].get(manifest) != bundle_digest:
        raise ValueError('certificate does not bind the qualified bundle digest')
    if certificate['version'] != outcome['candidate']['version']:
        raise ValueError('completed job candidate differs from certificate')
    if digest(read(bundle / 'qualification.json')) != certificate['qualification_sha256']:
        raise ValueError('certificate qualification identity mismatch')
    if digest(read(cycle / 'qualification/policy.json')) != digest(asdict(host.lifecycle.policy)):
        raise ValueError('target host qualification policy mismatch')
    if not isinstance(contract.get('description'), str) or not contract['description'].strip():
        raise ValueError('configured contract needs its qualified description')
    return observed, contract['description']


def admit_completed_job(host, config, job_directory, agent_id, bundle_digest):
    """Copy into private target custody and use the unchanged registry gate.

    The import lock serializes repeats. A repeat rechecks source bytes, target
    environment, signed registry record and its evidence manifest, then returns
    the existing identity. It cannot silently reactivate a disabled/revoked or
    superseded admission. Recovery after issue-before-response uses the same
    signed job record, without issuing a second admission.
    """
    host.authorize(config.get('principal', 'local-operator'), 'admit')
    supplied = Path(job_directory)
    if supplied.is_symlink():
        raise ValueError('canonical job must be a real directory')
    source = supplied.resolve()
    job_id = source.name
    if not re.fullmatch('[a-f0-9]{32}', job_id):
        raise ValueError('canonical job directory needs its original job ID')
    original = _inventory(source)
    observed, question = _check(host, config, source, agent_id, bundle_digest)
    registry = host.registry
    destination = registry.jobs / job_id
    with program_lock(registry.root / 'admission-import'):
        if destination.exists():
            if _inventory(destination) != original:
                raise ValueError('target job ID already contains different evidence')
        else:
            staging = Path(tempfile.mkdtemp(prefix='.admit-', dir=registry.jobs))
            try:
                shutil.copytree(source, staging, dirs_exist_ok=True)
                if _inventory(staging) != original:
                    raise ValueError('source job changed while copying')
                for path in staging.rglob('*'):
                    path.chmod(0o700 if path.is_dir() else 0o600)
                staging.chmod(0o700)
                os.rename(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        # Rehash from target custody, never trust source paths at issuance.
        copied_observed, copied_question = _check(host, config, destination, agent_id, bundle_digest)
        if (copied_observed, copied_question) != (observed, question):
            raise ValueError('target identity changed during import')
        matches = []
        for record_path in registry.root.glob('*/admission.json'):
            qualified_id = record_path.parent.name
            record = registry.inspect(qualified_id)
            if record['job_id'] == job_id:
                matches.append((qualified_id, record))
        if len(matches) > 1:
            raise ValueError('job has multiple admissions; operator reconciliation required')
        if matches:
            qualified_id, record = matches[0]
            registry._validate(qualified_id)
            active = registry._read(registry.root / (record['slot'] + '.active.json'))
            if (record['agent_id'] != agent_id or record['question'] != question
                    or active['qualified_id'] != qualified_id):
                raise ValueError('imported job admission is not the active matching slot')
            return {'status': 'QUALIFIED', 'qualified_id': qualified_id,
                    'version': record['version'], 'identity': record['identity'],
                    'bundle_digest': bundle_digest, 'job_id': job_id, 'idempotent': True}
        issued = registry.issue(job_id, agent_id, question, observed)
        if filehash(registry.root / issued['qualified_id'] / 'bundle' / 'frozen-qualified-manifest.json') != bundle_digest:
            raise ValueError('installed qualified bundle digest mismatch')
        return {**issued, 'bundle_digest': bundle_digest, 'job_id': job_id, 'idempotent': False}
