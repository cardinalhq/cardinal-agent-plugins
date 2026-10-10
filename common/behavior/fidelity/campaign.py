"""Campaign boundary: source fidelity must pass before a gold worker starts."""
import json
from pathlib import Path
from . import require_cases


def before_gold(root, cases, frozen_recorded_bases, gold_worker, bundles=None):
    root = Path(root)
    receipts = require_cases(cases, root/'fidelity/receipts.json', frozen_recorded_bases, bundles)
    return gold_worker(cases, receipts)


def before_predictions(root, cases, frozen_recorded_bases, bundles=None):
    root = Path(root)
    if (root/'qualification').exists():
        raise ValueError('qualification already attempted; never reset')
    path = root/'fidelity/receipts.json'
    recorded = json.loads(path.read_text())
    from . import check_case
    current = [check_case(c, frozen_recorded_bases, (bundles or {}).get(c['id'])) for c in cases]
    if recorded != current or not current or not all(r['pass'] for r in current):
        raise ValueError('fidelity receipts absent, failed or changed')
    return current
