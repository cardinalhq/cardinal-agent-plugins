"""Explicit synthetic diagnoses for controller-only tests; not semantic evidence."""
from pathlib import Path
from compilation.diagnosis import packet
from compilation.storage import digest


def mechanical_diagnosis(candidate, item, revision):
    document = packet(candidate, item)
    trace = document['trace']
    key = 'signal' if 'signal' in trace else 'trace_id'
    cited = [{'ref': '/trace/' + key, 'quote': trace[key]}]
    expected = {'verdict': item['case']['expected'], 'basis': 'POSITIVE_EVIDENCE',
                'explanation': 'Synthetic controller fixture; not a semantic adjudication.', 'citations': cited}
    return {'packet_sha256': digest(document), 'contract': {}, 'expected': expected,
        'independent_review': {**expected, 'oracle_status': 'JUSTIFIED'},
        'observed': {'actual': item['execution']['actual'],
                     'citations': [{'ref': '/execution/actual', 'quote': item['execution']['actual']}]},
        'scope': {'candidate': 'synthetic subject', 'entity': 'synthetic entity',
                  'temporal': 'synthetic bounded episode', 'citations': cited},
        'earliest': {'classification': 'VERDICT_COMPOSITION',
                     'explanation': 'Synthetic mismatch for controller testing.', 'citations': cited,
                     'source_citations': [{'ref': '/source', 'quote': document['source'].splitlines()[0]}]},
        'jev_review': [], 'intervention': 'TARGETED_PYTHON_REPAIR',
        'intervention_reason': 'Exercise candidate safety independently of semantic model quality.'}
