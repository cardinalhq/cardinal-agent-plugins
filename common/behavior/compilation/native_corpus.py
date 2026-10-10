"""Native telemetry corpus adapter for the existing qualification controller.

Synthetic author and blind reviewer are separate model calls. Raw native trace
schema and tool arguments are validated by the configured canonical host adapters.
No gold or reviewer rationale enters the runtime trace.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from .diagnosis import citations
from .llm import ModelProtocolError
from .storage import digest, write


class ReviewProtocolError(ValueError):
    """The blind response cannot be interpreted as one judgment per case."""


def validate_review_protocol(response, packet):
    """Validate syntax and case identity without examining gold or evidence truth."""
    if not isinstance(response, dict) or set(response) != {'reviews'} or not isinstance(response['reviews'], list):
        raise ReviewProtocolError('response must contain only a reviews array')
    fields = {'id', 'label', 'fidelity_pass', 'oracle_status', 'reason', 'citations'}
    ids = []
    for review in response['reviews']:
        if not isinstance(review, dict) or set(review) != fields:
            raise ReviewProtocolError('review fields do not match the required schema')
        if not isinstance(review['id'], str):
            raise ReviewProtocolError('case ID must be a string')
        ids.append(review['id'])
        if (review['label'] not in ('MATCH', 'NON_MATCH', 'UNKNOWN')
                or type(review['fidelity_pass']) is not bool
                or review['oracle_status'] not in ('JUSTIFIED', 'AMBIGUOUS')
                or not isinstance(review['reason'], str)
                or not isinstance(review['citations'], list)):
            raise ReviewProtocolError('invalid review field type or enum')
        for citation in review['citations']:
            if (not isinstance(citation, dict) or set(citation) != {'ref', 'quote'}
                    or not all(isinstance(citation[k], str) for k in ('ref', 'quote'))):
                raise ReviewProtocolError('citation must contain string ref and quote')
    expected = [c['id'] for c in packet['cases']]
    if Counter(ids) != Counter(expected) or len(set(ids)) != len(ids):
        raise ReviewProtocolError('case IDs must cover the exact input IDs once each; '
                                  f'expected={expected!r}, received={ids!r}')


def blind_review(model, name, instruction, packet, root, max_protocol_retries=2):
    """Retry only wire failures; every call sees the identical, gold-free packet.

    A valid adverse judgment returns immediately to the existing semantic
    correction/quarantine path. Failed review text is retained locally, never
    included in the retry request, so an invalid response cannot prime retries.
    """
    for retry in range(max_protocol_retries + 1):
        call_name = name if retry == 0 else f'{name}-protocol-retry-{retry}'
        record = {'call_name': call_name, 'protocol_attempt': retry,
                  'packet_sha256': digest(packet), 'max_protocol_retries': max_protocol_retries}
        try:
            response = model.call(call_name, 'independent-native-corpus-review', instruction, deepcopy(packet))
            record['response'] = response
            validate_review_protocol(response, packet)
        except (ModelProtocolError, ReviewProtocolError, json.JSONDecodeError) as exc:
            write(Path(root) / f'{call_name}.json', {**record, 'status': 'PROTOCOL_FAILURE',
                'error_type': type(exc).__name__, 'error': str(exc)})
            if retry == max_protocol_retries:
                raise ReviewProtocolError('blind review protocol retries exhausted; corpus quarantined') from exc
        else:
            write(Path(root) / f'{call_name}.json', {**record, 'status': 'SCHEMA_VALID'})
            return response


def validate_review(review, packet, index):
    """Require this case's raw evidence; also permit valid source-policy citations."""
    items = review.get('citations')
    citations(items, packet)
    prefix = f'/cases/{index}/trace/'
    if not any(c['ref'].startswith(prefix) for c in items):
        raise ValueError('review needs evidence from its own raw trace')
    if any(not (c['ref'].startswith(prefix) or c['ref'].startswith('/sources/')) for c in items):
        raise ValueError('review cites another case or unsupported packet field')


def render(spec, namespace, agent, templates=None):
    trace_id = digest(namespace)[:32]
    ids = [digest([namespace, i])[:16] for i in range(len(spec['events']))]
    events = []
    for i, value in enumerate(spec['events']):
        value = deepcopy(value)
        if templates and value.get('input_json') is not None:
            def expand(item):
                if isinstance(item, str) and item in templates:
                    return deepcopy(templates[item])
                if isinstance(item, dict):
                    return {k: expand(v) for k, v in item.items()}
                if isinstance(item, list):
                    return [expand(v) for v in item]
                return item
            value['input'] = json.dumps(expand(value.pop('input_json')), ensure_ascii=False)
        if value['kind'] not in ('agent', 'other', 'llm', 'tool'):
            raise ValueError('unsupported native event kind')
        start, end = value['start_ns'], value['end_ns']
        if type(start) is not int or type(end) is not int or end < start:
            raise ValueError('invalid native event time')
        parent = value.get('parent_index')
        if parent is not None and (type(parent) is not int or not 0 <= parent < i):
            raise ValueError('parent must identify an earlier event')
        attrs = {'id': ids[i], 'trace_id': trace_id, 'name': value['name'],
                 'chq_tsns': start, 'end_tsns': end,
                 'openinference_span_kind': value['kind'].upper(),
                 'input_value': value.get('input'), 'output_value': value.get('output')}
        if value.get('status_code') in ('STATUS_CODE_OK', 'STATUS_CODE_ERROR'):
            attrs['status_code'] = value['status_code']
        if value.get('tool_call_id'):
            attrs['gen_ai_tool_call_id'] = value['tool_call_id']
        if parent is not None:
            attrs['parent_span_id'] = ids[parent]
        events.append({'id': ids[i], 'parent_id': ids[parent] if parent is not None else None,
                       'kind': 'other' if value['kind'] == 'agent' else value['kind'], 'name': value['name'], 'start': start / 1e9,
                       'end': end / 1e9, 'seq': i, 'attrs': attrs,
                       'input': value.get('input'), 'output': value.get('output'),
                       'error': value.get('error')})
    return {'trace_id': trace_id, 'agent': agent,
            'attrs': {'projection': 'native-spans-v1', 'coverage_scope': 'selected-window-objects',
                      'conversation_complete': False}, 'events': events}


class NativeCorpus:
    def __init__(self, root, behavior, contract, snapshot, model, native_validate):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.behavior, self.contract, self.snapshot, self.model = behavior, contract, snapshot, model
        self.system, self.common = '', {'source_revision': snapshot['source_revision']}
        self.native_validate = native_validate
        self.templates = {}
        for trace in snapshot['traces']:
            for event in trace['events']:
                if event.get('kind') == 'llm' and event.get('input'):
                    try:
                        value = json.loads(event['input'])
                        for key in ('system', 'tools'):
                            if key in value:
                                self.templates.setdefault('__SOURCE_' + key.upper() + '__', value[key])
                    except (ValueError, TypeError):
                        pass

    def compact(self, trace):
        return deepcopy(trace)

    def make(self, split, per_label, candidate_source=None, excluded_families=()):
        packet = {'question_contract': self.contract, 'sources': self.snapshot['sources'],
                  'representative_raw_traces': self.snapshot['traces'][:2],
                  'per_label': per_label, 'excluded_families': list(excluded_families)}
        packet['exact_input_templates'] = self.templates
        if candidate_source is not None:
            packet['candidate_source'] = candidate_source
        instruction = (
            'Create exactly ' + str(3 * per_label) + ' concise source-faithful synthetic episodes, '
            + str(per_label) + ' each MATCH, NON_MATCH, UNKNOWN. These are labelled synthetic, never production. '
            'Use actual instructions and tool input/output formats from source. All relevant source instructions '
            'must be present verbatim in the LLM input as in real telemetry. To avoid repeating large '
            'constants, supply input_json instead of input on LLM events; use the exact template tokens '
            '__SOURCE_SYSTEM__ and __SOURCE_TOOLS__ for those JSON values. The host expands them before '
            'validation, blind review and SDK execution. The system template includes actual campaign '
            'context: preserve that context or use raw input with source-faithful changes when needed. '
            'Do not invent completeness, '
            'policy, tools, role authority, or impossible tool outputs. Include clear violation, compliant, '
            'missing telemetry, wrong-entity, late and contradictory evidence, optional versus mandatory, '
            'partial failures, multiple candidate actions and incomplete window boundaries across the suite. '
            'Never insert gold labels or diagnostic hints in trace fields. Return {cases:[{family,expected,'
            'rationale,tags,events:[{kind:"agent|llm|tool",name,start_ns,end_ns,parent_index,input,output,error,'
            'status_code,tool_call_id}]}]}. input/output are source-shaped strings or null. Times are integer '
            'nanoseconds (use small synthetic epochs); parent_index is an earlier event index or null. '
            'Use distinct families. Prior inputs repeated in LLM history are context, not new actions. '
            'UNKNOWN must have meaningful observed agent evidence with a missing material premise. '
            'Do not force a definitive label when source does not justify it.')
        draft = self.model.call(self.behavior + '-' + split + '-author', 'native-corpus-author', instruction, packet)
        for attempt in range(2):
            specs = draft['cases']
            if len(specs) != per_label * 3:
                raise ValueError('wrong corpus count')
            cases = [{'id': f'{self.behavior}-{split}-{i}', 'family': spec['family'],
                      'expected': spec['expected'], 'rationale': spec['rationale'],
                      'tags': spec['tags'], 'synthetic': True,
                      'trace': render(spec, [self.behavior, split, i], self.snapshot['agent_id'], self.templates)}
                     for i, spec in enumerate(specs)]
            failures = []
            for c in cases:
                self.native_validate(c['trace'])
            review_packet = {'contract': self.contract, 'sources': self.snapshot['sources'],
                             'cases': [{'id': c['id'], 'trace': c['trace']} for c in cases]}
            review = blind_review(self.model, self.behavior + '-' + split + '-review-' + str(attempt),
                'Independently label each exact raw trace against the agent source and question contract. '
                'You cannot see author gold/rationale, programs or predictions. Verify source fidelity, '
                'actual tool schema/output possibilities, instructions, entity/temporal binding and '
                'coverage. Quarantine ambiguous cases instead of forcing gold. Return {reviews:[{id,label,'
                'fidelity_pass,oracle_status:"JUSTIFIED|AMBIGUOUS",reason,citations:[{ref,quote}]}]}. '
                'Copy the exact case IDs from the input, each exactly once; do not invent, shorten, '
                'renumber, omit or duplicate IDs. Labels MATCH/NON_MATCH/UNKNOWN; exact nonempty '
                'quotes and JSON pointers into /cases/i/trace '
                'are required for each case. Additional /sources/ citations are allowed for policy and '
                'fidelity checks; never cite another case. JUSTIFIED means the selected label is justified, '
                'including UNKNOWN when a material premise is demonstrably missing. AMBIGUOUS means '
                'the correct label itself is uncertain, not merely that the underlying behavior is '
                'unresolved. Missing support in an incomplete trace is not established lack of support.',
                review_packet, self.root)
            reviews = {r['id']: r for r in review['reviews']}
            for i, c in enumerate(cases):
                r = reviews.get(c['id'], {})
                try:
                    validate_review(r, review_packet, i)
                    good = (r.get('label') == c['expected'] and r.get('fidelity_pass') is True
                            and r.get('oracle_status') == 'JUSTIFIED')
                except (ValueError, TypeError, KeyError):
                    good = False
                c.update(review=r, review_pass=good, oracle_status=r.get('oracle_status'),
                         trace_sha256=digest(c['trace']))
                if not good:
                    failures.append({'id': c['id'], 'review': r, 'author_expected': c['expected']})
            if (len(reviews) != len(cases) or len(review['reviews']) != len(cases)
                    or Counter(c['expected'] for c in cases) != Counter(dict.fromkeys(
                        ('MATCH', 'NON_MATCH', 'UNKNOWN'), per_label))):
                failures.append({'reason': 'review cardinality or class balance'})
            write(self.root / f'{split}-attempt-{attempt}.json', {'cases': cases, 'failures': failures})
            if not failures:
                write(self.root / (split + '.json'), cases)
                return cases
            if attempt == 1:
                raise ValueError('independent review failed; unresolved cases quarantined')
            draft = self.model.call(self.behavior + '-' + split + '-correction', 'native-corpus-author',
                instruction + ' Correct all review failures; return the full replacement set.',
                {**packet, 'previous': draft, 'failures': failures})
