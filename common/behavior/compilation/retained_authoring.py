"""Explicit operator reuse of failed design inputs, never scores or qualification state."""
from pathlib import Path

from .storage import digest, filehash, read, write


FILES = ('request.json', 'discovery-input.json', 'discovery.json', 'contract.json',
         'generated.json', 'identity.json', 'outcome.json')

ATTEMPT_FILES = ('request.json', 'discovery-input.json', 'discovery.json',
                 'authoring/contract-revised.json', 'authoring/proposal-2.json',
                 'authoring/outcome.json', 'outcome.json')


def prepare_failed_attempt(config, root, snapshot, source_identity, runtime, agent_id, question):
    """Use an explicit failed authoring attempt; never reset a started qualification."""
    previous = Path(config['job']).resolve()
    hashes = config.get('files', {})
    if set(hashes) != set(ATTEMPT_FILES) or not config.get('freeze_contract'):
        raise ValueError('failed attempt requires exact inputs and frozen contract')
    if previous == Path(root).resolve() or (previous/'cycle/qualification').exists():
        raise ValueError('failed attempt cannot reset a qualification budget')
    for name in ATTEMPT_FILES:
        if filehash(previous/name) != hashes[name]:
            raise ValueError('failed attempt input drift: ' + name)
    request, fresh = read(previous/'request.json'), read(Path(root)/'request.json')
    if (request['agent_id'] != agent_id or request['behavioral_question'] != question
            or any(request[k] != fresh[k] for k in ('time_range','policy'))):
        raise ValueError('failed attempt request or policy mismatch')
    if read(previous/'outcome.json')['status'] != 'COMPILATION_FAILED':
        raise ValueError('retained attempt did not fail authoring')
    prior = read(previous/'discovery-input.json')
    def stable(value):
        return {**value, 'selection': {k:v for k,v in value['selection'].items()
                                      if k != 'retrieval_latency_ms'}}
    if stable(prior) != stable(snapshot):
        raise ValueError('failed attempt source or telemetry drift')
    if (runtime.identity() != config['sdk_identity']
            or digest(runtime.jev_config) != config['jev_identity']):
        raise ValueError('failed attempt SDK/JEV drift')
    contract = read(previous/'authoring/contract-revised.json')
    if filehash(previous/'authoring/contract-revised.json') != config['contract_sha256']:
        raise ValueError('failed attempt differs from successful regression contract')
    discovery, generated = read(previous/'discovery.json'), read(previous/'authoring/proposal-2.json')
    if discovery.get('status') != 'READY':
        raise ValueError('retained discovery was not ready')
    write(Path(root)/'retained-inputs.json', {'mode':'failed_attempt','previous_job':str(previous),
        'files':hashes,'source_identity':source_identity,'contract_frozen':True,
        'original_question':question,'prior_scores_or_qualification_reused':False})
    write(Path(root)/'original-contract.json',contract)
    write(Path(root)/'original-generated.json',generated)
    return discovery,contract,generated


def prepare_retained(config, root, snapshot, source_identity, runtime, agent_id, question):
    """Validate frozen inputs before any model call; a new run gets fresh reviews.

    An explicit experiment is distinct from discovery recovery. Only failures
    before qualification started are eligible; no audit/repair budget is reset.
    No case, label, review, score, or previous qualification result is returned.
    """
    if config.get('mode') == 'failed_attempt':
        return prepare_failed_attempt(config, root, snapshot, source_identity, runtime, agent_id, question)
    previous = Path(config['job']).resolve()
    if previous == Path(root).resolve():
        raise ValueError('retained authoring requires a separate job')
    hashes = config.get('files', {})
    if set(hashes) != set(FILES):
        raise ValueError('retained authoring requires exact input file hashes')
    for name in FILES:
        if filehash(previous / name) != hashes[name]:
            raise ValueError('retained authoring input drift: ' + name)
    request, prior = read(previous / 'request.json'), read(previous / 'discovery-input.json')
    if request['agent_id'] != agent_id or request['behavioral_question'] != question:
        raise ValueError('retained authoring cannot change agent or original question')
    fresh_request = read(Path(root) / 'request.json')
    if any(request[k] != fresh_request[k] for k in ('time_range', 'policy')):
        raise ValueError('retained authoring cannot change scope or qualification policy')
    if (previous / 'cycle/qualification').exists():
        raise ValueError('retained authoring cannot reset qualification or audit budget')
    outcome = read(previous / 'outcome.json')
    if outcome['status'] not in ('COMPILATION_FAILED', 'CANNOT_QUALIFY'):
        raise ValueError('retained authoring requires a failed prior lifecycle')
    def stable(value):
        return {**value, 'selection': {k: v for k, v in value['selection'].items()
                                      if k != 'retrieval_latency_ms'}}
    if stable(prior) != stable(snapshot):
        raise ValueError('retained authoring source or telemetry drift')
    identity = read(previous / 'identity.json')
    contract = read(previous / 'contract.json')
    if (any(identity[k] != v for k, v in source_identity.items())
            or identity['contract'] != digest(contract)
            or identity['sdk'] != runtime.identity()
            or identity['jev'] != digest(runtime.jev_config)):
        raise ValueError('retained authoring runtime, source, contract or JEV drift')
    discovery, generated = read(previous / 'discovery.json'), read(previous / 'generated.json')
    if discovery.get('status') != 'READY':
        raise ValueError('retained discovery was not ready')
    write(Path(root) / 'retained-inputs.json', {
        'previous_job': str(previous), 'files': hashes, 'original_question': question,
        'original_contract_sha256': digest(contract), 'fresh_qualification': True,
        'previous_reviews_or_scores_reused': False,
        'reason': 'Explicit new authoring experiment from failed design inputs before qualification.'})
    write(Path(root) / 'original-contract.json', contract)
    write(Path(root) / 'original-generated.json', generated)
    return discovery, contract, generated
