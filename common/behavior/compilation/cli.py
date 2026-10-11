"""Callable local host for question-first Behavioral Programs. No production mutation."""
import argparse
import json
import os
import threading
import time
from http.server import HTTPServer
from .application import build
from .qualified_api import handler
from .storage import read
from .files import private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('CARDINAL_BEHAVIOR_LIFECYCLE_CONFIG'))
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--agent-id', required=True)
    create.add_argument('--question', required=True)
    admit = commands.add_parser('admit', help='operator-only import of a completed canonical job')
    admit.add_argument('--job-dir', required=True)
    admit.add_argument('--agent-id', required=True)
    admit.add_argument('--bundle-digest', required=True)
    for name in ('execute', 'inspect', 'disable', 'revoke'):
        commands.add_parser(name).add_argument('--qualified-id', required=True)
    rollback = commands.add_parser('rollback')
    rollback.add_argument('--qualified-id', required=True)
    rollback.add_argument('--target-id', required=True)
    receipt = commands.add_parser('receipt')
    receipt.add_argument('--qualified-id', required=True)
    receipt.add_argument('--execution-id', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--port', type=int, default=8769)
    args = parser.parse_args()
    if not args.config:
        parser.error('operator lifecycle configuration is required')
    config = read(args.config)
    host = build(config)
    principal = config.get('principal', 'local-operator')
    if args.command == 'admit':
        from .admission_import import admit_completed_job
        result = admit_completed_job(host, config, args.job_dir, args.agent_id, args.bundle_digest)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == 'serve':
        token = os.environ[config['http_token_env']]
        coverage = config.get('execution_completeness', {})
        enrolled = coverage.get('qualified_ids', {})
        if enrolled:
            interval = coverage.get('poll_seconds', 30)
            if type(interval) not in (int, float) or interval < 1:
                raise ValueError('completeness poll interval must be at least one second')
            def reconcile():
                while True:
                    try:
                        rows = host.poll_completeness(enrolled)
                        private_json(host.registry.root / 'completeness-poll.json',
                            dict(at=time.time(), executions=rows))
                    except Exception as exc:
                        private_json(host.registry.root / 'completeness-poll.json',
                            dict(at=time.time(), status='UNRECONCILED', verdict='UNKNOWN',
                                reason=type(exc).__name__ + ': ' + str(exc)))
                    threading.Event().wait(interval)
            threading.Thread(target=reconcile, daemon=True).start()
        HTTPServer(('127.0.0.1', args.port), handler([(token, principal, host)])).serve_forever()
        return
    if args.command == 'create':
        payload = {'agent_id': args.agent_id, 'behavioral_question': args.question}
    else:
        payload = {'qualified_id': args.qualified_id}
        if args.command == 'rollback':
            payload['target_id'] = args.target_id
        if args.command == 'receipt':
            payload['execution_id'] = args.execution_id
    print(json.dumps(host.dispatch(principal, args.command, payload), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
