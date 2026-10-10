"""Small replay allowlist. Source coverage is never behavioral compliance."""
import copy
import hashlib
import json
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


ALLOWLIST = {
    'final_text': {'edits': ['root output', 'final LLM text blocks'], 'forces': {}},
    'drop_span': {'edits': ['one retained span'], 'forces': {'gaps': True}},
    'redact_output': {'edits': ['one output field'], 'forces': {'redacted': True}},
    'truncate_output': {'edits': ['one output suffix'], 'forces': {'truncated': True}},
    'undeclare_scope': {'edits': ['coverage scope declaration'], 'forces': {'scope_declared': False}},
}


def replay(base, transforms):
    trace = copy.deepcopy(base)
    capture = {'content_captured': True, 'redacted': False, 'truncated': False,
               'scope_declared': True, 'loss': {'gaps': [], 'dropped_records': 0}}
    for transform in transforms:
        op = transform.get('op')
        if op not in ALLOWLIST:
            raise ValueError('transform not allowlisted: ' + str(op))
        fields = {'final_text': {'op','text'}, 'drop_span': {'op','span_id'},
                  'redact_output': {'op','span_id'}, 'truncate_output': {'op','span_id','length'},
                  'undeclare_scope': {'op'}}[op]
        if set(transform) != fields:
            raise ValueError('unexpected transform parameters')
        if op == 'final_text':
            text = transform['text']
            if not isinstance(text, str):
                raise ValueError('final_text requires text')
            final = max((e for e in trace['events'] if e['kind'] == 'llm'), key=lambda e: int(e['attrs']['chq_tsns']))
            output = json.loads(final['output'])
            if output.get('stop_reason') != 'end_turn' or any(b['type'] not in ('text','thinking','redacted_thinking') for b in output['content']):
                raise ValueError('final_text requires an end_turn text response')
            texts = [b for b in output['content'] if b['type'] == 'text']
            if len(texts) != 1:
                raise ValueError('final_text requires exactly one text block')
            roots = [e for e in trace['events'] if e['parent_id'] is None]
            if len(roots) != 1:
                raise ValueError('one root required')
            old_text = texts[0]['text']
            for other in trace['events']:
                if other['id'] in (final['id'], roots[0]['id']):
                    continue
                if old_text and any(old_text in (other.get(field) or '') for field in ('input','output')):
                    raise ValueError('final_text repeated in another span')
            texts[0]['text'] = text; final['output'] = json.dumps(output, separators=(',', ':'), ensure_ascii=False)
            final['attrs']['output_value'] = final['output']
            roots[0]['output'] = text; roots[0]['attrs']['output_value'] = text
        elif op == 'undeclare_scope':
            capture['scope_declared'] = False
        else:
            matches = [e for e in trace['events'] if e['id'] == transform['span_id']]
            if len(matches) != 1:
                raise ValueError('transform span missing or ambiguous')
            event = matches[0]
            if op == 'drop_span':
                trace['events'].remove(event); capture['loss']['gaps'].append('dropped_span:' + event['id']); capture['loss']['dropped_records'] += 1
            else:
                # Captured successful provider requests are never edited. Loss
                # may obscure outputs only; provider-field deletion is forbidden.
                if event['kind'] != 'other':
                    raise ValueError('only root/other output redaction/truncation allowlisted')
                if op == 'redact_output':
                    event['output'] = None; capture['redacted'] = True
                else:
                    length = transform['length']
                    if type(length) is not int or not 0 <= length < len(event['output']):
                        raise ValueError('invalid truncation length')
                    event['output'] = event['output'][:length]; capture['truncated'] = True
                event['attrs']['output_value'] = event['output']
    return trace, capture


def body_hash(value):
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()


def check_observations(rows, trace):
    expected = {e['id']: e for e in trace['events']}
    if len(rows) != len(expected) or {r['spanId'] for r in rows} != set(expected):
        raise ValueError('base bundle occurrence inventory differs from recorded base')
    for row in rows:
        event = expected[row['spanId']]
        if row['inputSha256'] != body_hash(event['input']) or row['outputSha256'] != body_hash(event['output']):
            raise ValueError('base bundle content hashes differ from recorded base')


def replay_bundle(base_bundle, base_trace, trace, capture):
    """Derive the exact admission payload from an enrolled original bundle.

    Source inventory retains genuinely observed missing/redacted occurrences;
    retained inventory loses them. A declared hypothetical final-text edit is
    reflected in both inventories. No new real source receipt is issued.
    """
    bundle = copy.deepcopy(base_bundle)
    source, retained = bundle['source'], bundle['retained']
    source['capture'].update({k:capture[k] for k in ('content_captured','redacted','truncated')})
    if not capture['scope_declared']:
        source['capture']['scope_id'] = None
    for target in (source, retained):
        target['loss'].update(copy.deepcopy(capture['loss']))
        target['loss'].update(redacted=capture['redacted'], truncated=capture['truncated'])
    current = {e['id']:e for e in trace['events']}
    retained['observations'] = [r for r in retained['observations'] if r['spanId'] in current]
    for rows in (source['ended'], retained['observations']):
        for row in rows:
            event = current.get(row['spanId'])
            # Redaction/truncation does not rewrite independent source bytes.
            if event is not None and (rows is retained['observations'] or not (capture['redacted'] or capture['truncated'])):
                row['inputSha256'] = body_hash(event['input'])
                row['outputSha256'] = body_hash(event['output'])
    source['closure']['inventory_sha256'] = digest({'started':source['started'],'ended':source['ended']})
    bundle['trace'] = copy.deepcopy(trace)
    retained['trace'] = copy.deepcopy(trace)
    # Original typed storage is provenance of the base, not a fabricated read of
    # the derived case. It is not submitted as a second mutable admission input.
    bundle.pop('native_read', None)
    bundle.pop('coverage_preflight', None)
    return bundle


def verify_derivation(case, trusted_bases=None, bundle=None):
    checks = ['recorded_base_hash', 'source_attestation', 'transform_allowlist', 'byte_equal_replay', 'honest_capture', 'admission_bundle_replay']
    result = {'pass': False, 'checks_run': checks, 'errors': []}
    if bundle is None:
        bundle = case.get('provenance_bundle')
    try:
        derivation = case['derivation']; result['derivation_sha256'] = digest(derivation)
        base_path = Path(derivation['base_path']); raw = base_path.read_bytes()
        result['base_sha256'] = hashlib.sha256(raw).hexdigest()
        if result['base_sha256'] != derivation['base_sha256']:
            raise ValueError('recorded base hash mismatch')
        trusted = (trusted_bases or {}).get(derivation['base_sha256'])
        if not trusted or trusted.get('receipt_sha256') != derivation['receipt_sha256']:
            raise ValueError('base not enrolled in frozen recorded-base catalog')
        if trusted.get('source_identity') != derivation['source_identity'] or trusted.get('recorder_identity') != derivation['recorder_identity']:
            raise ValueError('base source/recorder identity mismatch')
        receipt_path = Path(derivation['receipt_path']); receipt_raw = receipt_path.read_bytes()
        if hashlib.sha256(receipt_raw).hexdigest() != derivation['receipt_sha256']:
            raise ValueError('base receipt hash mismatch')
        receipt = json.loads(receipt_raw)
        if receipt.get('synthetic') or receipt.get('source_revision') != derivation['source_identity']:
            raise ValueError('recorded source identity not attested')
        if receipt.get('status') != 'VERIFIED' or receipt.get('artifact_hashes', {}).get(base_path.name) != derivation['base_sha256']:
            raise ValueError('source did not attest recorded base')
        if not receipt.get('source_to_native_all_observations_equal') or not receipt.get('source_process_terminated') or not receipt.get('provider_shutdown_completed'):
            raise ValueError('source observation coverage not independently established')
        # Validate all attested companion artifacts, not a trace's self-claim.
        for name, expected in receipt['artifact_hashes'].items():
            artifact = (receipt_path.parent/name).resolve()
            if not artifact.is_relative_to(receipt_path.parent.resolve()) or hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
                raise ValueError('base companion attestation drift: ' + name)
        trace, capture = replay(json.loads(raw), derivation['transforms'])
        if canonical(trace) != canonical(case['trace']):
            raise ValueError('case differs from replayed base')
        if capture != case['capture']:
            raise ValueError('capture claims stronger than or inconsistent with derivation')
        if not bundle or bundle.get('synthetic'):
            raise ValueError('missing or synthetic admission bundle')
        bp = Path(trusted['bundle_path']); bundle_raw = bp.read_bytes()
        if hashlib.sha256(bundle_raw).hexdigest() != trusted['bundle_sha256']:
            raise ValueError('enrolled base bundle drift')
        base_bundle = json.loads(bundle_raw); base_trace = json.loads(raw)
        if base_bundle.get('synthetic') or canonical(base_bundle['trace']) != canonical(base_trace) or canonical(base_bundle['retained']['trace']) != canonical(base_trace):
            raise ValueError('base bundle is synthetic or differs from recorded trace')
        identities = base_bundle['source']['identities']
        if identities['source'] != derivation['source_identity'] or identities['recorder'] != derivation['recorder_identity']:
            raise ValueError('base bundle source/recorder mismatch')
        bc = base_bundle['source']['capture']
        if bc.get('content_captured') is not True or bc.get('redacted') is not False or bc.get('truncated') is not False or not bc.get('scope_id'):
            raise ValueError('base bundle capture not verified')
        for evidence in (base_bundle['source'],base_bundle['retained']):
            if evidence['loss'] != {'gaps':[],'dropped_records':0,'redacted':False,'truncated':False}:
                raise ValueError('base bundle loss not verified')
        check_observations(base_bundle['source']['ended'], base_trace)
        check_observations(base_bundle['retained']['observations'], base_trace)
        expected_bundle = replay_bundle(base_bundle, base_trace, trace, capture)
        if canonical(bundle) != canonical(expected_bundle):
            raise ValueError('admission bundle differs from replayed derivation')
        result['pass'] = True
    except (KeyError, TypeError, ValueError, OSError) as exc:
        result['errors'].append(str(exc))
    return result
