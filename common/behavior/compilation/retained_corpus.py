"""Fresh blind review of immutable retained development cases, with fixed gold.

Only discovery is retained. Challenge and hidden audit generation continue through
NativeCorpus unchanged; historical reviews, predictions and scores are not reused.
"""
from collections import Counter
from copy import deepcopy
from pathlib import Path

from .llm import strict_json_loads
from .native_corpus import NativeCorpus, ReviewProtocolError, blind_review, validate_review
from .storage import digest, write


class RetainedCorpusRejected(ValueError):
    """The entire retained development corpus remains quarantined."""


class RetainedCorpus(NativeCorpus):
    def __init__(self, *args, retained_discovery, **kwargs):
        super().__init__(*args, **kwargs)
        if (not isinstance(retained_discovery, dict)
                or set(retained_discovery) != {'path', 'sha256'}):
            raise ValueError('retained_discovery requires exactly path and sha256')
        self.retained_discovery = deepcopy(retained_discovery)

    def _load(self, per_label):
        path = Path(self.retained_discovery['path'])
        raw = path.read_bytes()
        if digest(raw) != self.retained_discovery['sha256']:
            raise RetainedCorpusRejected('retained discovery file hash mismatch')
        source = strict_json_loads(raw)
        cases = source.get('cases') if isinstance(source, dict) else None
        if (type(per_label) is not int or per_label < 1 or not isinstance(cases, list)
                or len(cases) != 3 * per_label):
            raise RetainedCorpusRejected('retained discovery count must equal three times per_label')
        fields = ('id', 'family', 'expected', 'rationale', 'tags', 'synthetic', 'trace')
        fixed = []
        for case in cases:
            if (not isinstance(case, dict) or any(key not in case for key in fields)
                    or not isinstance(case['id'], str) or not case['id']
                    or not isinstance(case['family'], str) or not case['family']
                    or case['expected'] not in ('MATCH', 'NON_MATCH', 'UNKNOWN')
                    or case['synthetic'] is not True or not isinstance(case['trace'], dict)
                    or not isinstance(case['rationale'], str) or not isinstance(case['tags'], list)):
                raise RetainedCorpusRejected('retained discovery case schema is invalid')
            trace_hash = digest(case['trace'])
            if case.get('trace_sha256', trace_hash) != trace_hash:
                raise RetainedCorpusRejected('retained trace hash mismatch: ' + case['id'])
            fixed.append({**{key: deepcopy(case[key]) for key in fields}, 'trace_sha256': trace_hash})
        if len({c['id'] for c in fixed}) != len(fixed):
            raise RetainedCorpusRejected('retained discovery has duplicate case IDs')
        if len({c['family'] for c in fixed}) != len(fixed):
            raise RetainedCorpusRejected('retained discovery has duplicate families')
        if Counter(c['expected'] for c in fixed) != Counter(dict.fromkeys(
                ('MATCH', 'NON_MATCH', 'UNKNOWN'), per_label)):
            raise RetainedCorpusRejected('retained discovery class balance differs from requested per_label')
        return fixed

    def make(self, split, per_label, candidate_source=None, excluded_families=()):
        if split != 'discovery':
            return super().make(split, per_label, candidate_source, excluded_families)
        cases = self._load(per_label)
        provenance = {'source': self.retained_discovery, 'fixed_gold_sha256': digest([
            {'id': c['id'], 'expected': c['expected'], 'trace_sha256': c['trace_sha256']}
            for c in cases]), 'previous_reviews_or_predictions_reused': False}
        write(self.root / 'retained-discovery-input.json', {**provenance, 'cases': cases})
        failures = []
        for case in cases:
            try:
                self.native_validate(deepcopy(case['trace']))
                if case['family'] in excluded_families:
                    raise ValueError('retained family is excluded')
            except Exception as exc:
                failures.append({'id': case['id'], 'code': 'NATIVE_VALIDATION_FAILED',
                                 'reason': f'{type(exc).__name__}: {exc}'})
        if failures:
            write(self.root / 'retained-discovery-review.json', {**provenance, 'cases': cases,
                  'status': 'QUARANTINED', 'failures': failures})
            raise RetainedCorpusRejected('retained discovery native validation failed; full corpus quarantined')
        packet = {'contract': self.contract, 'sources': self.snapshot['sources'],
                  'cases': [{'id': c['id'], 'trace': deepcopy(c['trace'])} for c in cases]}
        instruction = (
            'Independently label each exact raw trace against the supplied source and question contract. '
            'You cannot see author gold/rationale, programs or predictions. Verify source fidelity, '
            'actual tool schema/output possibilities, instructions, entity/temporal binding and coverage. '
            'Return {reviews:[{id,label,fidelity_pass,oracle_status:"JUSTIFIED|AMBIGUOUS",reason,'
            'citations:[{ref,quote}]}]}. Copy every exact input case ID once, with no additions or omissions. '
            'Labels are MATCH/NON_MATCH/UNKNOWN. Each case requires nonempty literal quotes with JSON '
            'pointers into its own /cases/i/trace; additional /sources citations are permitted. Never '
            'cite another case. JUSTIFIED means the selected label is supported, including UNKNOWN '
            'when a material premise is missing. AMBIGUOUS means the correct label itself is uncertain. '
            'Missing support in an incomplete trace is not established lack of support. Do not force '
            'a definitive judgment or infer complete coverage. Quarantine ambiguous cases.')
        try:
            response = blind_review(self.model, self.behavior + '-retained-discovery-review',
                                    instruction, packet, self.root)
        except ReviewProtocolError as exc:
            write(self.root / 'retained-discovery-review.json', {**provenance, 'cases': cases,
                'status': 'QUARANTINED', 'failures': [{'code': 'REVIEW_PROTOCOL_FAILED', 'reason': str(exc)}]})
            raise RetainedCorpusRejected('retained discovery review protocol failed; full corpus quarantined') from exc
        reviews = {r['id']: r for r in response['reviews']}
        for index, case in enumerate(cases):
            review = reviews[case['id']]
            reasons = []
            if review['label'] != case['expected']:
                reasons.append({'code': 'FIXED_GOLD_DISAGREEMENT', 'expected': case['expected'],
                                'reviewed_label': review['label'], 'reason': review['reason']})
            if review['fidelity_pass'] is not True:
                reasons.append({'code': 'SOURCE_FIDELITY_FAILED', 'reason': review['reason']})
            if review['oracle_status'] != 'JUSTIFIED':
                reasons.append({'code': 'ORACLE_AMBIGUOUS', 'reason': review['reason']})
            try:
                validate_review(review, packet, index)
            except (ValueError, KeyError, TypeError) as exc:
                reasons.append({'code': 'EVIDENCE_CITATION_FAILED', 'reason': str(exc)})
            case.update(review=review, review_pass=not reasons, oracle_status=review['oracle_status'])
            failures.extend({'id': case['id'], **reason} for reason in reasons)
        write(self.root / 'retained-discovery-review.json', {**provenance, 'cases': cases,
              'status': 'QUARANTINED' if failures else 'JUSTIFIED', 'failures': failures})
        if failures:
            details = '; '.join(f"{f['id']}: {f['code']} ({f['reason']})" for f in failures)
            raise RetainedCorpusRejected('retained discovery full corpus quarantined: ' + details)
        write(self.root / 'discovery.json', cases)
        return cases
