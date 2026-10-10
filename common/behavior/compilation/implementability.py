"""Narrow, evidence-bound recovery for unsupported exact semantic decision counts.

This is authoring validation, not an SDK extension or an alternate adjudicator.
Only a model-authored replacement of one identified reporting sentence is allowed.
Population preconditions and behavioral truth conditions remain frozen.
"""
import ast
from copy import deepcopy

from .storage import digest, write

COUNT_SENTENCE = ('Separately report decision counts m, s, u and m/(m+s+u); '
                  'do not conflate decision and execution rates.')


def assess_contract_implementability(contract, sdk_documentation):
    report = {'status': 'NO_AUTHORIZED_REVISION', 'contract_sha256': digest(contract),
              'revision_scope': [], 'evidence': []}
    locations = [i for i, clause in enumerate(contract.get('clauses', []))
                 if clause.get('statement', '').count(COUNT_SENTENCE) == 1]
    if len(locations) != 1:
        return report
    sdk = sdk_documentation
    artifact = sdk.get('artifact', {})
    if (sdk.get('sdk_version') != '0.2.0' or artifact.get('version') != '0.2.0'
            or digest(artifact) != sdk.get('sdk_artifact_sha256')):
        raise ValueError('implementability evidence requires verified SDK 0.2.0 artifact')
    files = artifact.get('files', {})
    # These are checked against the digest-addressed installed source, not a
    # hard-coded replacement declaration of any SDK interface.
    requirements = {
        'behavior_sdk/jev.py': [
            'Typed extraction is intentionally outside v1.',
            'set(obj) != {"answer", "evidence_refs", "short_reason"}',
            'max_calls: int = 8',
        ],
        'behavior_sdk/native_profile.py': [
            'Each retained native span is one event.',
            'No application-specific inferred',
        ],
        'behavior_sdk/recorder.py': [
            'evaluate(run: TraceView, recorder: Recorder, judge: JEV) -> None',
            'def counter(',
        ],
    }
    for path, quotes in requirements.items():
        source = files.get(path, '')
        for quote in quotes:
            if quote not in source:
                raise ValueError('canonical SDK evidence changed: ' + path)
            report['evidence'].append({'path': path, 'quote': quote,
                                       'source_sha256': digest(source)})
    tree = ast.parse(files['behavior_sdk/jev.py'])
    verdict = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Verdict')
    values = {n.value.value for n in verdict.body if isinstance(n, ast.Assign)
              and isinstance(n.value, ast.Constant)}
    if values != {'YES', 'NO', 'UNCERTAIN'}:
        raise ValueError('canonical JEV verdict schema changed')
    index = locations[0]
    report.update(status='NARROW_REVISION_SUPPORTED', sdk_artifact_sha256=digest(artifact),
        revision_scope=[f'/clauses/{index}/statement'], clause_index=index,
        replace_only=COUNT_SENTENCE,
        finding=('Exact counts of materially distinct arbitrary semantic decisions are not '
                 'supplied by the native span projection or the bounded tri-state JEV ABI. '
                 'Recorder counters can store counts, but do not establish a valid semantic '
                 'enumeration. No validated decision segmentation is supplied in this path.'),
        preserved=('Preserve the original question, execution-level M/S/U/Z definitions and '
                   'denominators, all behavioral criteria, uncertainty rules, collection and '
                   'source-applicability prerequisites. Population enumeration belongs to the '
                   'host; discovery/convenience samples do not establish population rates.'),
        permitted_change=('Replace only the identified exact decision-count requirement with '
                          'an explicit disclosure that exact semantic decision counts and '
                          'decision rates are unavailable. Never substitute span, message, '
                          'tool, packet, or judge-call counts. No other contract text may change.'))
    return report


def validate_revision(original, revised, report):
    if (report.get('status') != 'NARROW_REVISION_SUPPORTED'
            or report.get('contract_sha256') != digest(original)):
        raise ValueError('contract revision has no matching implementability evidence')
    index = report['clause_index']
    old = original['clauses'][index]['statement']
    new = revised['clauses'][index]['statement']
    prefix, suffix = old.split(COUNT_SENTENCE)
    if not new.startswith(prefix) or (suffix and not new.endswith(suffix)):
        raise ValueError('contract revision changed preserved reporting semantics')
    replacement = new[len(prefix):len(new)-len(suffix) if suffix else None]
    if (not isinstance(replacement, str) or not 30 <= len(replacement) <= 1000
            or 'unavailable' not in replacement.lower()
            or 'decision' not in replacement.lower() or 'count' not in replacement.lower()):
        raise ValueError('contract revision must explicitly disclose unavailable decision counts')
    expected = deepcopy(original)
    expected['clauses'][index]['statement'] = old.replace(COUNT_SENTENCE, replacement)
    if revised != expected:
        raise ValueError('contract revision changed an unauthorized field')
    return replacement


def revise_contract_if_needed(root, *, contract, question, sdk_documentation, model, job_id):
    """At most one author and one independent review call; fail closed on rejection.

    Caller must invoke only for a retained implementability failure, before
    freezing this fresh lifecycle's contract or exposing any qualification cases.
    """
    from pathlib import Path
    root = Path(root)
    report = assess_contract_implementability(contract, sdk_documentation)
    write(root / 'implementability.json', report)
    if report['status'] != 'NARROW_REVISION_SUPPORTED':
        return contract
    write(root / 'contract-original.json', contract)
    response = model.call(job_id + '-contract-implementability-revision',
        'sdk-contract-implementability-revision',
        'The installed SDK evidence demonstrates a narrowly unsupported reporting obligation. '
        'Return JSON {replacement_sentence, rationale}. Author only one replacement sentence '
        'for the quoted exact decision-count requirement. Explicitly state exact semantic '
        'decision counts and decision rates are unavailable, and prohibit substituting '
        'span/message/tool/packet/judge-call counts. Preserve the original question and '
        'all execution-frequency semantics and prerequisites. Do not change gold labels, '
        'qualifications, SDK, JEV, or runtime behavior. No held-out evidence is supplied.',
        {'question': question, 'contract': contract, 'implementability': report})
    write(root / 'contract-revision-response.json', response)
    replacement = response.get('replacement_sentence') if isinstance(response, dict) else None
    if not isinstance(replacement, str):
        raise ValueError('contract revision lacks a replacement sentence')
    revised = deepcopy(contract)
    index = report['clause_index']
    revised['clauses'][index]['statement'] = contract['clauses'][index]['statement'].replace(
        COUNT_SENTENCE, replacement)
    validate_revision(contract, revised, report)
    review = model.call(job_id + '-contract-revision-review', 'independent-contract-revision-review',
        'Independently review this narrow contract revision against canonical SDK evidence and '
        'the unchanged original question. Return JSON {approved:bool, question_preserved:bool, '
        'execution_frequency_preserved:bool, original_semantics_preserved:bool, '
        'unavailable_counts_disclosed:bool, no_proxy_counts:bool, reason:string}. Approve only '
        'if the sole change explicitly discloses unavailable exact semantic decision counts '
        'and rates, prohibits proxy counts, and preserves every classification, uncertainty, '
        'execution denominator, source applicability and population collection prerequisite. '
        'The unchanged clauses retain semantic decision definitions; they do not require '
        'native spans to become semantic decision IDs. Do not relax the user question.',
        {'question': question, 'original_contract': contract, 'revised_contract': revised,
         'implementability': report})
    write(root / 'contract-revision-review.json', review)
    checks = ('approved', 'question_preserved', 'execution_frequency_preserved',
              'original_semantics_preserved', 'unavailable_counts_disclosed', 'no_proxy_counts')
    if (not isinstance(review, dict) or any(review.get(k) is not True for k in checks)
            or not isinstance(review.get('reason'), str) or not review['reason'].strip()):
        raise ValueError('independent contract revision review rejected semantic preservation')
    write(root / 'contract-revised.json', revised)
    return revised
