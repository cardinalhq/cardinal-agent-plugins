"""Zero-transform recorded bases, including independently witnessed native loss.

Source emissions must pass the SAME provider validator. Retained inputs must
be the unchanged SDK projection of original storage cells. An incomplete read
can pass fidelity without obtaining verified coverage or a behavioral label.
This entry point is enrolled before construction; legacy replay is unchanged.
"""
import hashlib
import json
from pathlib import Path
from . import validate_protocol
from .derivation import canonical, digest, body_hash


def check_case(case, trusted_bases, bundle):
    result = dict(id=case['id'], **{'pass': False}, checks_run=[
        'frozen_catalog_enrollment', 'companion_artifact_hashes', 'source_protocol',
        'zero_transform_native_projection', 'source_native_loss_reconciliation',
        'unchanged_generic_completeness', 'exact_admission_bundle'], errors=[])
    try:
        from behavior_sdk.native_profile import adapt
        from compilation.native_completeness import normalize_native
        from compilation.completeness import verify_completeness
        d = case['derivation']
        result.update(base_sha256=d['base_sha256'], derivation_sha256=digest(d))
        if d['transforms'] != []:
            raise ValueError('recorded-native entry point permits no transforms')
        enrolled = trusted_bases[d['base_sha256']]
        for key in ('base_path','receipt_path','receipt_sha256','source_identity','recorder_identity'):
            if d[key] != enrolled[key]:
                raise ValueError('derivation differs from enrolled identity: ' + key)
        def checked(path, expected):
            raw = Path(path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError('enrolled artifact drift: ' + str(path))
            return json.loads(raw)
        base = checked(d['base_path'], d['base_sha256'])
        receipt = checked(d['receipt_path'], d['receipt_sha256'])
        directory = Path(d['receipt_path']).parent.resolve()
        for name, expected in receipt['artifact_hashes'].items():
            p = (directory/name).resolve()
            if not p.is_relative_to(directory) or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
                raise ValueError('companion attestation drift: ' + name)
        if (receipt['synthetic'] is not False or receipt['provider'] != 'scripted' or
                receipt['population_eligible'] is not False or receipt['source_revision'] != d['source_identity'] or
                receipt['recorder_identity'] != d['recorder_identity'] or
                receipt['source_process_terminated'] is not True or receipt['provider_shutdown_completed'] is not True):
            raise ValueError('recorded execution custody missing')
        for name, expected in ((Path(d['base_path']).name,d['base_sha256']),
                               (Path(enrolled['bundle_path']).name,enrolled['bundle_sha256'])):
            if receipt['artifact_hashes'].get(name) != expected:
                raise ValueError('receipt does not bind base/bundle')
        original = checked(enrolled['bundle_path'], enrolled['bundle_sha256'])
        if original.get('synthetic') is not False or canonical(bundle) != canonical(original):
            raise ValueError('admission bundle differs from enrolled original')
        if canonical(case['trace']) != canonical(base) or canonical(original['trace']) != canonical(base):
            raise ValueError('case differs from original recorded base')
        source, retained = original['source'], original['retained']
        if source['identities']['source'] != d['source_identity'] or source['identities']['recorder'] != d['recorder_identity']:
            raise ValueError('source identity differs')
        native = json.loads((directory/'native-2.json').read_text())
        previous = json.loads((directory/'native-1.json').read_text())
        if adapt(native, source['agent_id']) != adapt(previous, source['agent_id']) or not receipt['stable_native_snapshots_equal']:
            raise ValueError('native snapshots unstable')
        recomputed = normalize_native(source['execution_id'], source['agent_id'], native, retained['catalogs'], retained['census'])
        if canonical(recomputed['trace']) != canonical(base):
            raise ValueError('base differs from original native SDK projection')
        sr = {r['spanId']:r for r in source['ended']}
        nr = {r['spanId']:r for r in recomputed['observations']}
        missing = sorted(set(sr)-set(nr)); extra = sorted(set(nr)-set(sr))
        changed = sorted(i for i in set(sr)&set(nr) if sr[i] != nr[i])
        recomputed['loss']['gaps'] += ['missing_source_occurrence:'+i for i in missing]+['unexpected_retained_occurrence:'+i for i in extra]+['source_native_content_mismatch:'+i for i in changed]
        if canonical(retained) != canonical(recomputed):
            raise ValueError('retained evidence/loss differs from actual native read')
        if receipt['loss_accounting'] != dict(missing_expected_spans=missing,unexpected_spans=extra,changed_observations=changed):
            raise ValueError('receipt loss inventory differs')
        if receipt['source_to_native_all_observations_equal'] != (sr == nr):
            raise ValueError('false source/native equality claim')
        emissions = json.loads((directory/'source-emissions.json').read_text())
        er = {e['id']:e for e in emissions['events']}
        if set(er) != set(sr):
            raise ValueError('source projection inventory differs from source attestation')
        for ident,e in er.items():
            if body_hash(e['input']) != sr[ident]['inputSha256'] or body_hash(e['output']) != sr[ident]['outputSha256']:
                raise ValueError('source projection content differs from independent observation')
        protocol = validate_protocol(emissions)
        result['protocol'] = protocol
        if not protocol['pass']:
            raise ValueError('source provider protocol invalid: ' + '; '.join(protocol['errors']))
        coverage = verify_completeness(original['contract'], source, retained, base)
        if coverage != original['coverage_preflight'] or coverage != receipt['coverage'] or receipt['status'] != coverage['status']:
            raise ValueError('coverage receipt differs from unchanged verifier')
        capture = dict(content_captured=source['capture']['content_captured'],redacted=source['capture']['redacted'],truncated=source['capture']['truncated'],scope_declared=bool(source['capture']['scope_id']),loss=dict(gaps=retained['loss']['gaps'],dropped_records=retained['loss']['dropped_records']))
        if case['capture'] != capture:
            raise ValueError('capture claims differ from independently reconciled loss')
        result.update(**{'pass':True}, coverage_status=coverage['status'], missing_source_occurrences=missing)
    except (KeyError,TypeError,ValueError,OSError) as exc:
        result['errors'].append(str(exc))
    return result


def before_gold(root, cases, trusted_bases, gold_worker, bundles):
    path=Path(root)/'fidelity/receipts.json'; path.parent.mkdir(parents=True,exist_ok=True)
    rows=[check_case(c,trusted_bases,bundles[c['id']]) for c in cases]
    path.write_text(json.dumps(rows,indent=2)+'\n')
    if not rows or not all(r['pass'] for r in rows):
        raise ValueError('recorded source fidelity rejected corpus before gold')
    return gold_worker(cases,rows)


def before_predictions(root,cases,trusted_bases,bundles):
    root=Path(root)
    if (root/'qualification').exists():
        raise ValueError('qualification already attempted; never reset')
    rows=[check_case(c,trusted_bases,bundles[c['id']]) for c in cases]
    if json.loads((root/'fidelity/receipts.json').read_text()) != rows or not rows or not all(r['pass'] for r in rows):
        raise ValueError('recorded fidelity receipts absent, failed or changed')
    return rows
