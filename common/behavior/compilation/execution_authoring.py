"""Mechanical checks for a fresh execution against an authenticated development fixture."""
from .storage import digest
from .storage import read, write, filehash
from collections import Counter
from pathlib import Path
import json


def execution_regression_errors(result, fixture):
    errors = []
    trace_digest = digest(fixture['trace'])
    if fixture.get('trace_sha256') != trace_digest or result.get('trace_sha256') != trace_digest:
        errors.append({'code': 'EXECUTION_TRACE_BINDING', 'message': 'Execution must bind the exact authenticated fixture trace.'})
    actual = result.get('actual')
    if actual not in ('MATCH', 'NON_MATCH', 'UNKNOWN') or result.get('verdict', actual) != actual:
        errors.append({'code': 'EXECUTION_VERDICT', 'message': 'SDK execution must finish with MATCH, NON_MATCH, or UNKNOWN.'})
    if fixture.get('expected_verdict') is not None and actual != fixture['expected_verdict']:
        errors.append({'code': 'EXECUTION_REGRESSION', 'message': 'SDK execution differs from the verified development fixture verdict.'})
    if result.get('mechanical_pass') is not True or result.get('audit', {}).get('mechanical_status') != 'PASS':
        errors.append({'code': 'EXECUTION_PROVENANCE', 'message': 'SDK execution must pass the unmodified provenance audit.'})
    minimum = fixture.get('minimum_semantic_calls', 0)
    calls = result.get('metrics', {}).get('semantic_calls')
    if type(minimum) is not int or minimum < 0 or (minimum and (type(calls) is not int or calls < minimum)):
        errors.append({'code': 'EXECUTION_SEMANTIC_CALLS', 'message': 'Execution must retain the fixture minimum embedded JEV calls.'})
    return errors


def load_development_teaching(config, contract, runtime):
    """Operator-pinned exposed development data; never hidden qualification data.

    Decision labels and execution labels are different scopes. Keep both explicit;
    an observed supported action cannot certify a complete execution.
    """
    if set(config) != {'path', 'sha256'} or filehash(config['path']) != config['sha256']:
        raise ValueError('development teaching identity mismatch')
    packet = read(config['path'])
    if packet['contract_sha256'] != digest(contract) or packet['scope'] != 'exposed-development-only':
        raise ValueError('development teaching contract/scope mismatch')
    cases = packet['cases']
    baseline_ref = packet['baseline']
    if filehash(baseline_ref['path']) != baseline_ref['sha256']:
        raise ValueError('development baseline drift')
    baseline = {c['id']: c for c in read(baseline_ref['path'])['development']}
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('duplicate development case')
    for c in cases:
        runtime.validate_trace(c['trace'])
        if digest(c['trace']) != c['trace_sha256']:
            raise ValueError('development trace drift')
        if (c['id'] not in baseline or baseline[c['id']]['trace_sha256'] != c['trace_sha256']
                or baseline[c['id']]['expected'] != c['expected']):
            raise ValueError('development/baseline identity mismatch')
        if c['expected'] not in ('MATCH', 'NON_MATCH', 'UNKNOWN'):
            raise ValueError('invalid observed decision label')
        review = c['review']
        if review.get('label', review.get('verdict')) != c['expected'] or review['oracle_status'] != 'JUSTIFIED':
            raise ValueError('development label not independently justified')
        if c['expected'] == 'NON_MATCH':
            if c['execution_expected'] != 'UNKNOWN' or c['trace'].get('attrs', {}).get('conversation_complete') is not False:
                raise ValueError('supported observed action cannot assert execution completeness')
        elif c['execution_expected'] != c['expected']:
            raise ValueError('positive/uncertain execution teaching label mismatch')
    selected = [next(c for c in cases if c['id'] == i) for i in packet['teaching_ids']]
    if len(selected) > 8 or len({c['trace']['trace_id'] for c in selected}) != len(selected):
        raise ValueError('compiler teaching limit/identity mismatch')
    if {c['expected'] for c in selected} != {'MATCH', 'NON_MATCH', 'UNKNOWN'}:
        raise ValueError('explicit positive, supported-action and unknown teachings required')
    return {**packet, 'examples': [{'trace': c['trace'], 'expected_verdict': c['execution_expected']}
                                 for c in selected],
            'annotations': [{'trace_id': c['trace']['trace_id'], 'observed_decision_label': c['expected'],
                'expected_execution_verdict': c['execution_expected'], 'review': c['review']} for c in selected]}


TEACHING_INSTRUCTION = '''Use the canonical compiler request and feedback to author ordinary
Python implementing the unchanged source-linked contract. Return only
{udf_source,compile_plan}. This is autonomous authoring, not a supplied-source echo.
Generate narrow behavioral questions from individual applicable contract rules;
do not combine unrelated campaign, research, identity and authorization obligations
into two huge propositions. Preserve all material conditions and exceptions, with
real top-level judge.decide anchors and exact literal contract/SDK quotes in the plan.
Use mechanical candidate selection by event kind/name, occurrence, field, time and
explicit call IDs, never keywords or expected labels to detect a violation.
SDK Evidence(ref,field,text) supports any verbatim substring of the original field.
For large serialized requests, mechanically locate JSON structural boundaries and
retain exact original substrings (e.g. complete original messages arrays), roles,
explicit tool IDs, prior results and entity/time metadata. Parse only to locate
structure; never supply json.dumps/reconstructed/unescaped evidence, mutate traces,
blindly truncate, or discard material dynamic context. Original system/tool policy
may be supplied through exact applicable source-linked rules in narrow propositions;
do not mistake omitted context for proven absence. Include needed request/authority
and same-entity current results. Use judge.preflight; unresolved context remains
UNCERTAIN. Record selected field offsets and omitted-context disclosures. Preflight
overflow requires a bounded sufficient witness or UNKNOWN, not silent truncation.
An explicit prohibition contradicted by an action or a current connected result
contradicted by a recommendation can establish MATCH without proving every other
action observed. Favor positive-witness questions; a NO only disproves that bounded
violation, not complete execution support. Preserve any_of(..., complete=False)
and record_decision so a supported existential YES survives other uncertainty.
All selected-window executions without a proven violation remain UNKNOWN; do not
weaken NON_MATCH completeness. Observed NON_MATCH annotations teach supported actions,
but their expected execution verdict is UNKNOWN for this incomplete profile.
Stay within the pinned 8 semantic-call budget per execution. Stop once an independent
MATCH is established, consume earlier uncertain receipts in terminal composition,
and never turn budget exhaustion into ERROR or fabricate completeness. Operational
errors must propagate. Use all teaching feedback to revise within the compiler's
existing budget, preserving previously correct cases. No external adjudicator,
trace/entity/text special cases, hidden cases, SDK/JEV changes or invented APIs.'''


def teaching_author(model, job_id, contract, teaching, *, round_offset=0):
    """Adapt existing model transport to canonical Compiler's feedback backend."""
    calls = round_offset
    previous = None
    def author(request):
        nonlocal calls, previous
        name = f'{job_id}-compiler-teaching-{calls}'
        calls += 1  # Reserve a distinct slot even if transport rejects the input.
        response = model.call(name, 'sdk-program-generation',
            TEACHING_INSTRUCTION, {'compiler_request': request, 'contract': contract,
                'teaching_annotations': teaching['annotations'], 'previous_proposal': previous})
        previous = response
        transport = getattr(model, 'model', model)
        receipt_root = getattr(transport, 'root', None)
        usage = {}
        if receipt_root is not None:
            receipt = read(Path(receipt_root) / name / 'receipt.json')
            usage = {k: v for k, v in (receipt.get('usage') or {}).items()
                     if k in ('input_tokens', 'output_tokens') and type(v) is int}
        return {'model': 'teaching-author', 'model_version': '1',
                'text': json.dumps(response), 'usage': usage}
    return author


def evaluate_development(root, sdk, candidate, teaching):
    """Evaluate only already exposed cases, with unchanged labels and no admission."""
    directory = Path(root) / 'development'
    directory.mkdir()
    rows = []
    for c in teaching['cases']:
        result = sdk.evaluate(candidate, c['trace'])
        write(directory / (c['id'] + '.json'), result)
        rows.append({'id': c['id'], 'expected': c['expected'],
            'execution_expected': c['execution_expected'], 'actual': result['actual'],
            'mechanical_pass': result['mechanical_pass'], 'metrics': result['metrics'],
            'trace_sha256': result['trace_sha256'], 'receipt': str(directory / (c['id'] + '.json'))})
    write(directory / 'results.json', rows)
    matrix = {label: dict(Counter(r['actual'] for r in rows if r['expected'] == label))
              for label in ('MATCH', 'NON_MATCH', 'UNKNOWN')}
    write(directory / 'confusion-matrix.json', matrix)
    baseline = {c['id']: c for c in read(teaching['baseline']['path'])['development']}
    regressions = [r['id'] for r in rows if baseline[r['id']]['actual'] == r['expected']
                   and (r['actual'] != r['expected'] or not r['mechanical_pass'])]
    recovered = [r['id'] for r in rows if r['expected'] == 'MATCH'
                 and baseline[r['id']]['actual'] != 'MATCH' and r['actual'] == 'MATCH'
                 and r['mechanical_pass'] and r['metrics']['semantic_calls'] > 0]
    passed = bool(recovered) and not regressions and all(r['mechanical_pass'] for r in rows)
    gate = {'passed': passed, 'recovered_matches': recovered, 'regressions': regressions,
            'all_provenance_pass': all(r['mechanical_pass'] for r in rows),
            'qualification_admission': False}
    write(directory / 'restoration-gate.json', gate)
    return {'status': 'DEVELOPMENT_REPAIRED' if passed else 'DEVELOPMENT_INCOMPLETE', 'candidate': candidate,
            'results': str(directory / 'results.json'), 'confusion_matrix': matrix,
            'restoration_gate': gate,
            'hidden_qualification_invoked': False, 'population_executed': False,
            'qualified': False}
