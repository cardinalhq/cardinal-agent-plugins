#!/usr/bin/env python3
# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""Cardinal behavior MCP: accepted contract, deployed execution, compact findings."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from storyboard import render_storyboard

ROOT = Path(__file__).resolve().parent
VERSION = '87dde6be3a806f1c9a0346f82d6e861a6ab9bba7aad9d9a70ab884e6427d35f0'
POPULATION = 'cardinal-investigator'
VERDICTS = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR')
DESCRIPTION = ('Flag an investigator run when the assistant states it has enough evidence '
               'to close the investigation before its first submit_report invocation. '
               'A statement after submission is not a match. A match does not establish '
               'that its evidence was sufficient or insufficient, or that submission succeeded.')


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False)
    temporary.replace(path)


def bounded_text(value, limit=1400):
    return str(value)[:limit] if value else None


def compact(raw: dict) -> dict:
    """Allowlist only the proven agent-facing fields; never relay backend objects."""
    gaps = raw.get('coverage_gaps') or (raw.get('validation_observations') or {}).get('coverage_gaps', [])
    reason = raw.get('reason')
    if not reason:
        reason = next((r.get('reason') for r in raw.get('records', [])
                       if r.get('op') == 'violation' and r.get('reason')), None)
    # Preserve the model's compact explanation of actual evidence, without its packet.
    judged = next((r.get('reason') for r in raw.get('jev_receipts', [])
                   if r.get('reason') and (raw['verdict'] != 'MATCH' or r.get('decision') == 'YES' and set(r.get('decision_evidence_refs', [])) & set(raw.get('witness_refs', [])))), None)
    if judged:
        reason = f'{reason} {judged}' if reason else judged
    if not reason and gaps:
        reason = '; '.join(str(g.get('reason', '')) for g in gaps)
    if not reason:
        reason = raw.get('error')
    if not reason and raw['verdict'] == 'NON_MATCH':
        reason = 'No violation recorded; the diagnostic supplied no further reason.'
    return {'trace_id': raw['trace_id'], 'verdict': raw['verdict'],
            'reason': bounded_text(reason),
            'witness_refs': [str(ref)[:256] for ref in raw.get('witness_refs', [])[:32]],
            'coverage_gaps': [{k: bounded_text(g[k], 500) for k in ('ref', 'reason') if k in g}
                              for g in gaps[:16]]}


class Behavior:
    def __init__(self, config: dict):
        self.config = config
        self.output = Path(config['output_dir']).expanduser().resolve()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifacts = Path(config.get('artifacts', ROOT / 'artifacts'))
        self.definition = json.loads((artifacts / 'versions' / f'{VERSION}.json').read_text())
        payload = {k: v for k, v in self.definition.items() if k != 'version'}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if self.definition['version'] != VERSION or digest != VERSION:
            raise ValueError('accepted DiagnosticVersion integrity check failed')
        self.preview = json.loads((artifacts / 'previews' / f'{self.definition["preview_version"]}.json').read_text())
        preview_payload = {k: v for k, v in self.preview.items() if k != 'version'}
        preview_digest = hashlib.sha256(json.dumps(preview_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if preview_digest != self.definition['preview_version']:
            raise ValueError('accepted contract preview integrity check failed')

    def request(self, method: str, path: str, payload=None) -> dict:
        headers = {'Content-Type': 'application/json'}
        if self.config.get('headers_file'):
            headers_path = Path(self.config['headers_file']).expanduser()
            if headers_path.stat().st_mode & 0o077:
                raise ValueError('headers_file must be private (mode 0600)')
            configured = json.loads(headers_path.read_text())
            allowed = {'x-chq-internal-key', 'x-chq-internal-org-id', 'x-cardinalhq-api-key'}
            if set(k.lower() for k in configured) - allowed:
                raise ValueError('unsupported authentication header')
            headers.update(configured)
        elif self.config.get('internal_key_env'):
            headers['x-chq-internal-key'] = os.environ[self.config['internal_key_env']]
            headers['x-chq-internal-org-id'] = self.config['org']
        else:
            headers['x-cardinalhq-api-key'] = os.environ[self.config.get('api_key_env', 'CARDINAL_MCP_API_KEY')]
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.config['base_url'].rstrip('/') + path,
                                         data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Cardinal API returned HTTP {exc.code}; check the configured service and credentials') from None
        except urllib.error.URLError:
            raise RuntimeError('Cardinal API is unreachable; check the configured service') from None

    def state_path(self, execution_id):
        if not re.fullmatch('[a-f0-9]{32}', execution_id):
            raise ValueError('invalid execution reference')
        return self.output / execution_id / 'receipt.json'

    def read(self, execution_id):
        state = json.loads(self.state_path(execution_id).read_text())
        if state['execution_id'] != execution_id or state['diagnostic_version'] != VERSION:
            raise ValueError('receipt identity mismatch')
        return state

    def select(self):
        return {'diagnostic_version': VERSION, 'accepted_behavior': VERSION,
                'description': DESCRIPTION, 'population': POPULATION,
                'contract': [c['interpretation'] for c in self.preview['clauses']],
                'window': {'start': self.config['start'], 'end': self.config['end']},
                'selection': 'Existing accepted DiagnosticVersion; no new compilation.'}

    def start(self, accepted_behavior, population):
        if accepted_behavior != VERSION or population != POPULATION:
            raise ValueError('unsupported accepted behavior or population')
        payload = {'diagnostic_version': VERSION, 'org': self.config['org'],
                   'service_name': POPULATION, 'start': self.config['start'], 'end': self.config['end']}
        response = self.request('POST', '/api/v1/behavior-executions', payload)
        execution_id = response['execution_id']
        state = {'execution_id': execution_id, 'diagnostic_version': VERSION,
                 'population_specification': payload, 'status': response['status'],
                 'cursor': 0, 'results': [], 'counts': {}, 'receipt': None,
                 'submission': {'method': 'POST', 'url': self.config['base_url'].rstrip('/') + '/api/v1/behavior-executions',
                                'submitted_at': time.time(), 'response': response}}
        private_json(self.state_path(execution_id), state)
        return {'execution_id': execution_id, 'execution_status': state['status'],
                'diagnostic_version': VERSION, 'population': POPULATION,
                'submission': {'method': 'POST', 'endpoint': state['submission']['url'], 'accepted': True}}

    def poll(self, execution_id, wait_seconds=20):
        state = self.read(execution_id)
        deadline = time.monotonic() + max(0, min(30, float(wait_seconds)))
        fresh = []
        while True:
            page = self.request('GET', f'/api/v1/behavior-executions/{execution_id}/results?after_result_seq={state["cursor"]}')
            if page['execution_id'] != execution_id:
                raise ValueError('API returned a different execution')
            if page['next_cursor'] < state['cursor']:
                raise ValueError('API cursor moved backwards')
            seen = {r['trace_id'] for r in state['results']}
            for item in page['results']:
                if item['trace_id'] in seen:
                    raise ValueError('API returned a duplicate trace result')
                if item['verdict'] not in VERDICTS:
                    raise ValueError('API returned an invalid verdict')
                identity = item.get('execution_identity', {})
                expected = {'diagnostic_version': VERSION, 'profile_sha256': self.definition['profile_sha256'],
                            'udf_sha256': self.definition['source_sha256'],
                            'adapter_sha256': self.definition['profile_sha256']}
                if any(identity.get(k) != v for k, v in expected.items()):
                    raise ValueError('result does not belong to the accepted DiagnosticVersion')
                seen.add(item['trace_id'])
                fresh.append(item)
                state['results'].append(item)
            state.update(cursor=page['next_cursor'], status=page['status'], counts=page['counts'])
            # Raw evidence and backend metadata stay in this private host-side file.
            state['last_response'] = page
            state['receipt'] = None
            complete = page['status'] == 'COMPLETED' and len(state['results']) == page['counts']['population']
            if complete:
                if page.get('receipt') != execution_id:
                    raise ValueError('completed receipt identity mismatch')
                actual = Counter(r['verdict'] for r in state['results'])
                if any(actual[v] != page['counts'][v] for v in VERDICTS):
                    raise ValueError('completed receipt counts do not match results')
                state['receipt'] = execution_id
            private_json(self.state_path(execution_id), state)
            if fresh or complete or page['status'] == 'FAILED' or time.monotonic() >= deadline:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        response = {'execution_id': execution_id, 'execution_status': state['status'],
                    'results': [compact(r) for r in fresh], 'received': len(state['results']),
                    'population_size': state['counts']['population'] if state['status'] == 'COMPLETED' else None,
                    'evaluated_so_far': state['counts']['population']}
        if state['receipt']:
            response.update(receipt=execution_id, verdicts={v: state['counts'][v] for v in VERDICTS})
        elif state['status'] == 'FAILED':
            response['error'] = 'Deployed execution failed; retained evidence is available for operator review.'
        else:
            response['next_action'] = 'Call next_behavior_result again until a receipt appears.'
        return response

    def render(self, receipt, accepted_behavior):
        if accepted_behavior != VERSION:
            raise ValueError('accepted behavior does not match this execution')
        state = self.read(receipt)
        output = self.state_path(receipt).parent / f'storyboard-{receipt}.html'
        return render_storyboard(state, self.definition, self.preview, output)


def tools_list():
    def tool(name, description, properties, required):
        return {'name': name, 'description': description,
                'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
    version = {'type': 'string', 'enum': [VERSION]}
    execution = {'type': 'string', 'description': 'Reference returned by execute_behavior'}
    return {'tools': [
        tool('select_behavior', 'Inspect the existing accepted behavior for investigator agents claiming enough evidence before submitting their report. Read its contract and select it only if it matches the user request. ' + DESCRIPTION, {}, []),
        tool('execute_behavior', 'Submit the selected accepted behavior to the deployed Cardinal API. First inspect select_behavior. The production investigation runs independently of polling.',
             {'accepted_behavior': version, 'population': {'type': 'string', 'enum': [POPULATION]}}, ['accepted_behavior', 'population']),
        tool('next_behavior_result', 'Observe newly committed compact findings while Cardinal investigates. Repeat until receipt appears. Empty results mean the execution is still running; do not fabricate findings. The total population is unknown until COMPLETED: evaluated_so_far is progress, never a total denominator.',
             {'execution_id': execution, 'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 30, 'default': 20}}, ['execution_id']),
        tool('render_storyboard', 'Render the existing evidence Storyboard from a completed execution receipt. Evidence remains expandable in the HTML; raw receipts never enter this chat.',
             {'receipt': execution, 'accepted_behavior': version}, ['receipt', 'accepted_behavior'])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('CARDINAL_BEHAVIOR_CONFIG'))
    args = parser.parse_args()
    if not args.config:
        raise ValueError('configure CARDINAL_BEHAVIOR_CONFIG or --config')
    behavior = Behavior(json.loads(Path(args.config).expanduser().read_text()))
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        try:
            if method == 'initialize':
                value = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                         'serverInfo': {'name': 'cardinal-behavior', 'version': '0.1.0'}}
            elif method == 'tools/list':
                value = tools_list()
            elif method == 'ping':
                value = {}
            elif method == 'tools/call':
                params = request['params']
                functions = {'select_behavior': behavior.select, 'execute_behavior': behavior.start,
                             'next_behavior_result': behavior.poll, 'render_storyboard': behavior.render}
                result = functions[params['name']](**params.get('arguments', {}))
                value = {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
            else:
                raise ValueError('unsupported MCP method')
        except Exception as exc:
            # Do not echo exception payloads from network, credentials, or raw result parsing.
            safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            value = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps({'error': safe})}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': value}), flush=True)


if __name__ == '__main__':
    main()
