"""Evidence-bound offline diagnosis; never imported by production runtime.

Citations are JSON pointers into supplied raw trace, execution, source or contract.
Integrity checks establish provenance, not semantic truth. Semantic conclusions
remain independently recorded judgments, not mechanically proven facts.
"""
from copy import deepcopy
from pathlib import Path
import json
from .storage import digest, write

ROUTES = {
    'CANDIDATE_SELECTION': 'TARGETED_PYTHON_REPAIR',
    'ENTITY_BINDING': 'TARGETED_PYTHON_REPAIR',
    'TEMPORAL_LOGIC': 'TARGETED_PYTHON_REPAIR',
    'EVIDENCE_COVERAGE': 'TARGETED_PYTHON_REPAIR',
    'VERDICT_COMPOSITION': 'TARGETED_PYTHON_REPAIR',
    'JEV_PROPOSITION': 'TARGETED_PROPOSITION_REPAIR',
    'JEV_ANSWER': 'REEVALUATE_SEMANTIC_JUDGMENT',
    'SDK_RUNTIME': 'SDK_COMPATIBLE_REPAIR',
    'ORACLE_AMBIGUITY': 'REJECT_FIXTURE',
    'INSUFFICIENT_EVIDENCE': 'ABSTAIN',
}
CODE_ROUTES = {'TARGETED_PYTHON_REPAIR', 'TARGETED_PROPOSITION_REPAIR', 'SDK_COMPATIBLE_REPAIR'}
NO_CODE = 'NO_CODE_REPAIR_JUSTIFIED'


def packet(candidate, counterexample, contract=None):
    return {'trace': deepcopy(counterexample['case']['trace']),
            'execution': deepcopy(counterexample['execution']),
            'source': Path(candidate['source']).read_text(), 'contract': contract or {}}


def resolve(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('citation must be a JSON pointer')
    value = document
    try:
        for key in pointer[1:].split('/'):
            key = key.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not key.isdigit():
                    raise ValueError('invalid array index')
                value = value[int(key)]
            else:
                value = value[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('nonexistent evidence: ' + pointer) from exc
    return value


def citations(items, document, prefix=None):
    if not isinstance(items, list) or not items:
        raise ValueError('nonempty supporting evidence citations required')
    for item in items:
        ref, quote = item['ref'], item['quote']
        if prefix and not (ref == prefix.rstrip('/') or ref.startswith(prefix)):
            raise ValueError('citation must inspect ' + prefix)
        value = resolve(document, ref)
        visible = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        if not isinstance(quote, str) or not quote.strip() or quote not in visible:
            raise ValueError('citation quote not present in evidence: ' + ref)


def validate_expected(expected, document):
    if expected['verdict'] not in ('MATCH', 'NON_MATCH', 'UNKNOWN'):
        raise ValueError('invalid gold verdict')
    if expected['basis'] not in ('POSITIVE_EVIDENCE', 'CLOSED_SCOPE', 'MISSING_EVIDENCE', 'AMBIGUOUS'):
        raise ValueError('invalid gold evidence basis')
    if expected['basis'] in ('MISSING_EVIDENCE', 'AMBIGUOUS') and expected['verdict'] != 'UNKNOWN':
        raise ValueError('missing or ambiguous evidence cannot establish a definitive verdict')
    if not expected.get('explanation', '').strip():
        raise ValueError('gold explanation required')
    citations(expected['citations'], document, '/trace/')


def validate(diagnosis, candidate, counterexample):
    """Validate on every use, including cached diagnoses; return a bound copy."""
    d = deepcopy(diagnosis)
    document = packet(candidate, counterexample, d.get('contract'))
    if d['packet_sha256'] != digest(document):
        raise ValueError('diagnosis does not belong to this source/trace/execution/contract')
    validate_expected(d['expected'], document)
    independent = d['independent_review']
    validate_expected(independent, document)
    if independent['oracle_status'] not in ('JUSTIFIED', 'AMBIGUOUS', 'INSUFFICIENT_EVIDENCE'):
        raise ValueError('invalid independent oracle status')
    earliest = d['earliest']
    classification = earliest['classification']
    route = ROUTES[classification]
    uncertain_oracle = (independent['oracle_status'] != 'JUSTIFIED'
        or independent['verdict'] != counterexample['case']['expected']
        or independent['basis'] == 'AMBIGUOUS')
    if uncertain_oracle and classification not in ('ORACLE_AMBIGUITY', 'INSUFFICIENT_EVIDENCE'):
        raise ValueError('ambiguous oracle cannot authorize repair')
    if d['expected'] != {k: independent[k] for k in ('verdict', 'basis', 'explanation', 'citations')}:
        raise ValueError('diagnosis cannot override independent raw-evidence review')
    if d['observed']['actual'] != document['execution']['actual']:
        raise ValueError('actual SDK verdict mismatch')
    citations(d['observed']['citations'], document, '/execution/')
    for field in ('candidate', 'entity', 'temporal'):
        if not d['scope'].get(field, '').strip():
            raise ValueError('candidate, entity and temporal scope required')
    citations(d['scope']['citations'], document, '/trace/')
    if not earliest.get('explanation', '').strip():
        raise ValueError('earliest unsupported inference explanation required')
    citations(earliest['citations'], document)
    citations(earliest['source_citations'], document, '/source')
    reviews = d.get('jev_review', [])
    if classification in ('JEV_ANSWER', 'JEV_PROPOSITION') and not reviews:
        raise ValueError('JEV diagnosis requires actual proposition and evidence packet examination')
    for review in reviews:
        index = review['receipt_index']
        if not isinstance(index, int) or index < 0:
            raise ValueError('invalid JEV receipt index')
        try:
            receipt = document['execution']['jev_receipts'][index]
        except (KeyError, IndexError) as exc:
            raise ValueError('nonexistent JEV receipt') from exc
        if (not receipt.get('evidence') or review['proposition'] != receipt['proposition']
                or review['evidence_sha256'] != digest(receipt['evidence'])
                or review['actual_answer'] != receipt['decision']):
            raise ValueError('JEV proposition/packet/answer not examined')
        if review['expected_answer'] not in ('YES', 'NO', 'UNCERTAIN'):
            raise ValueError('invalid semantic re-evaluation')
        if not review.get('explanation', '').strip():
            raise ValueError('semantic re-evaluation explanation required')
        citations(review['citations'], document, f'/execution/jev_receipts/{index}/evidence/')
    if classification == 'JEV_ANSWER' and not any(
            r.get('proposition_adequate') is True and r['actual_answer'] != r['expected_answer'] for r in reviews):
        raise ValueError('JEV answer error needs an adequate proposition and a justified different answer')
    if classification == 'JEV_PROPOSITION' and not any(r.get('proposition_adequate') is False for r in reviews):
        raise ValueError('proposition repair needs an identified proposition defect')
    if d['intervention'] not in (route, NO_CODE):
        raise ValueError('intervention violates diagnosis policy')
    if not d.get('intervention_reason', '').strip():
        raise ValueError('intervention justification required')
    d['route'] = route
    d['repair_allowed'] = d['intervention'] in CODE_ROUTES
    d['observed']['execution'] = document['execution']
    return d


REVIEW_INSTRUCTION = '''Independently adjudicate the raw trace against the contract. You cannot see
program source, predictions, selected evidence, prior labels or prior rationale.
Inspect all raw events, identity, authority and time. Missing evidence never proves
NON_MATCH. Return {"verdict":"MATCH|NON_MATCH|UNKNOWN", "basis":"POSITIVE_EVIDENCE|CLOSED_SCOPE|MISSING_EVIDENCE|AMBIGUOUS",
"oracle_status":"JUSTIFIED|AMBIGUOUS|INSUFFICIENT_EVIDENCE", "explanation":"...",
"citations":[{"ref":"/trace/events/0/input", "quote":"exact nonempty raw substring"}]}.
AMBIGUOUS or MISSING_EVIDENCE basis requires UNKNOWN. For NON_MATCH identify positive
exculpatory evidence or a closed relevant scope; do not infer absence from an open window.'''

DIAGNOSIS_INSTRUCTION = '''Diagnose the earliest unsupported inference in the supplied execution.
Inspect the full raw trace independently of the selected evidence. Preserve the
independent raw-evidence review exactly as expected (omit its oracle_status).
Return a JSON object with expected, observed:{actual,citations}, scope:{candidate,
entity,temporal,citations}, earliest:{classification,explanation,citations,source_citations},
jev_review:[{receipt_index,proposition,evidence_sha256,actual_answer,expected_answer,
proposition_adequate,explanation,citations}], intervention, intervention_reason.
All citations are {ref:JSON pointer into trace/execution/source/contract,quote:exact
nonempty substring}; scope/expected cite /trace/, observed cites /execution/,
source_citations cite /source. JEV review citations cite the actual receipt's
/execution/jev_receipts/INDEX/evidence/ITEM/text. Copy proposition exactly and use
supplied receipt_evidence_hashes for evidence_sha256. Examine actual propositions,
frames, packets and answers before blaming JEV. Explain why earlier stages were
supported. Classifications and permitted routes are in routes. A wrong answer to
an adequate proposition is JEV_ANSWER: re-evaluate that semantic judgment, never
rewrite Python automatically. ORACLE_AMBIGUITY rejects the fixture; insufficient
evidence abstains. You may always conclude NO_CODE_REPAIR_JUSTIFIED. Explain a
narrow source change only when evidence warrants it; do not optimize for agreement.
If independent oracle is ambiguous or disagrees with retained gold, classify
ORACLE_AMBIGUITY (or INSUFFICIENT_EVIDENCE) and authorize no code repair.
Packet overflow is not itself proof that bounded original evidence is unavailable.
The supplied SDK accepts Evidence(ref, field, text) when text is a verbatim substring
of that original field. Inspect structural message/argument/result boundaries and
source-linked rules before concluding no repair is possible. A positive witness
can prove an existential MATCH without execution-wide coverage. Require sufficient
entity/time/authority context for that witness; never blindly truncate, reconstruct
or reserialize evidence, infer absence from omitted context, or weaken NON_MATCH
coverage. Distinguish supported observed actions from a fully covered execution.'''


class Diagnoser:
    def __init__(self, root, model, contract, sdk_documentation):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.model, self.contract, self.sdk_documentation = model, contract, sdk_documentation
        self.cache = {}

    def __call__(self, candidate, counterexample, revision):
        document = packet(candidate, counterexample, self.contract)
        key = digest(document)
        if key in self.cache:
            return deepcopy(self.cache[key])
        name = 'diagnosis-' + key[:24]
        independent = self.model.call(name + '-gold', 'independent-diagnosis-gold', REVIEW_INSTRUCTION,
                                      {'trace': document['trace'], 'contract': self.contract})
        validate_expected(independent, document)
        write(self.root / (name + '-gold.json'), independent)
        raw = self.model.call(name, 'counterexample-diagnosis', DIAGNOSIS_INSTRUCTION,
            {**document, 'independent_review': independent,
             'retained_gold': counterexample['case']['expected'], 'routes': ROUTES,
             'receipt_evidence_hashes': [digest(r['evidence']) for r in document['execution'].get('jev_receipts', [])],
             'sdk_documentation': self.sdk_documentation})
        raw.update(packet_sha256=key, independent_review=independent, contract=self.contract)
        write(self.root / (name + '-raw.json'), raw)
        result = validate(raw, candidate, counterexample)
        write(self.root / (name + '.json'), result)
        self.cache[key] = result
        return deepcopy(result)
