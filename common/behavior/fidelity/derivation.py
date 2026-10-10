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
            texts[0]['text'] = text; final['output'] = json.dumps(output, separators=(',', ':'), ensure_ascii=False)
            final['attrs']['output_value'] = final['output']
            roots = [e for e in trace['events'] if e['parent_id'] is None]
            if len(roots) != 1:
                raise ValueError('one root required')
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


def verify_derivation(case, trusted_bases=None):
    checks = ['recorded_base_hash', 'source_attestation', 'transform_allowlist', 'byte_equal_replay', 'honest_capture']
    result = {'pass': False, 'checks_run': checks, 'errors': []}
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
        result['pass'] = True
    except (KeyError, TypeError, ValueError, OSError) as exc:
        result['errors'].append(str(exc))
    return result
