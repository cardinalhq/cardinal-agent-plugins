"""Verify an operator-owned real execution fixture, without exposing it as gold."""
from pathlib import Path
from .storage import read, filehash, digest


def load_execution_fixture(config, runtime):
    root = Path(config['directory'])
    required = {'program.py','contract.json','trace.json','execution-receipt.json',
                'execution-summary.json','precompile-validation.json'}
    if set(config['files']) != required:
        raise ValueError('execution fixture file set mismatch')
    for name, expected in config['files'].items():
        if filehash(root/name) != expected:
            raise ValueError('execution fixture file drift: ' + name)
    trace, receipt = read(root/'trace.json'), read(root/'execution-receipt.json')
    summary, preflight = read(root/'execution-summary.json'), read(root/'precompile-validation.json')
    if (preflight['sdk_identity'] != runtime.identity() or preflight['jev_config'] != runtime.jev_config
            or summary['sdk_version'] != '0.2.0'
            or summary['source_sha256'] != filehash(root/'program.py')
            or summary['contract_sha256'] != filehash(root/'contract.json')
            or summary['execution_receipt_sha256'] != filehash(root/'execution-receipt.json')
            or receipt['trace_sha256'] != digest(trace)):
        raise ValueError('execution fixture identity binding failed')
    if (receipt['verdict'] not in ('MATCH','NON_MATCH','UNKNOWN')
            or runtime.audit(receipt,trace)['mechanical_status'] != 'PASS'
            or not receipt.get('jev_receipts')
            or any(not r.get('attempts') for r in receipt['jev_receipts'])
            or any(a.get('status') != 'ok' for r in receipt['jev_receipts'] for a in r['attempts'])):
        raise ValueError('fixture is not a successful real SDK/JEV execution')
    return {'trace':trace,'trace_sha256':digest(trace),'expected_verdict':receipt['verdict'],
            'minimum_semantic_calls':1,'fixture_receipt_sha256':filehash(root/'execution-receipt.json'),
            'fixture_source_sha256':filehash(root/'program.py'),
            'contract_sha256':filehash(root/'contract.json')}
