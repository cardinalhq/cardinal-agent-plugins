"""Provider dispatch and pre-gold case admission, outside the frozen engine."""
from . import anthropic_messages
from .derivation import verify_derivation


def validate_protocol(trace):
    llms, errors = [], []
    for event in trace.get('events', []):
        if event.get('kind') != 'llm':
            continue
        attrs = event.get('attrs', {})
        provider = next((attrs[k] for k in ('gen_ai.provider.name', 'gen_ai_provider_name', 'llm_provider', 'llm_system', 'gen_ai_system') if attrs.get(k)), None)
        operation = attrs.get('gen_ai.operation.name') or attrs.get('gen_ai_operation_name') or attrs.get('name')
        # Legacy OpenInference operation names are explicit adapter keys, not
        # inferred from business content or a particular agent's name.
        key = (str(provider).lower() if provider else None, operation)
        if key in {(None, 'Anthropic Messages'), ('anthropic', 'Anthropic Messages'), ('anthropic', 'messages'), ('anthropic', 'chat')}:
            llms.append(event)
        else:
            errors.append(event['id'] + ': unsupported provider/operation')
    if llms:
        errors += anthropic_messages.validate(llms, [e for e in trace['events'] if e.get('kind') == 'tool'])
    return {'pass': not errors, 'checks_run': anthropic_messages.CHECKS if llms else [], 'errors': errors}


def check_case(case, trusted_bases=None):
    protocol = validate_protocol(case['trace'])
    derivation = verify_derivation(case, trusted_bases)
    return {'id': case['id'], 'pass': protocol['pass'] and derivation['pass'],
            'protocol': protocol, 'derivation': derivation,
            'checks_run': protocol['checks_run'] + derivation['checks_run'],
            'base_sha256': derivation.get('base_sha256'),
            'derivation_sha256': derivation.get('derivation_sha256')}


def require_cases(cases, receipt_path, trusted_bases=None):
    """Persist every receipt, then fail closed before gold or prediction calls."""
    import json
    from pathlib import Path
    rows = [check_case(case, trusted_bases) for case in cases]
    p = Path(receipt_path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, indent=2) + '\n')
    if not rows or not all(row['pass'] for row in rows):
        raise ValueError('source fidelity rejected corpus before gold review')
    return rows
