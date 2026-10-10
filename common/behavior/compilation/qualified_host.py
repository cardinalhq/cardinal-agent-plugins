"""Authenticated host boundary shared by HTTP/MCP and embedded application hosts.

The embedding HTTP server authenticates its principal and chooses a tenant-scoped
instance. There is deliberately no certificate-upload or caller-identity route.
"""
import secrets
import threading
from .storage import read, write
from .files import private_json


class QualifiedHost:
    def __init__(self, lifecycle, registry, catalog, authorize):
        self.lifecycle, self.registry, self.catalog = lifecycle, registry, catalog
        self.authorize = authorize
        self.session_id = secrets.token_hex(16)
        self._work_lock = threading.Lock()

    def submit(self, principal, operation, payload):
        """Persist before dispatch; HTTP timeouts must not reset lifecycle budgets."""
        self.authorize(principal, operation)
        if operation not in ('create', 'execute'):
            raise ValueError('only lifecycle and population operations are asynchronous')
        task_id = secrets.token_hex(16)
        root = self.lifecycle.jobs / 'requests' / task_id
        root.mkdir(parents=True)
        write(root / 'request.json', {'operation': operation, 'payload': payload,
              'session_id': self.session_id})
        def work():
            try:
                result = self.dispatch(principal, operation, payload)
            except Exception as exc:
                result = {'status': 'CANNOT_QUALIFY', 'reason': type(exc).__name__ + ': ' + str(exc)}
            private_json(root / 'outcome.json', result)
        threading.Thread(target=work, daemon=True).start()
        return {'status': 'PENDING', 'job_id': task_id}

    def dispatch(self, principal, operation, payload):
        if operation in ('create', 'execute'):
            with self._work_lock:
                return self._dispatch(principal, operation, payload)
        return self._dispatch(principal, operation, payload)

    def _dispatch(self, principal, operation, payload):
        self.authorize(principal, operation)
        permitted = {
            'create': ({'agent_id', 'behavioral_question'}, {'time_range'}),
            'execute': ({'qualified_id'}, {'time_range'}),
            'inspect': ({'qualified_id'}, set()),
            'receipt': ({'qualified_id', 'execution_id'}, set()),
            'disable': ({'qualified_id'}, set()),
            'revoke': ({'qualified_id'}, set()),
            'rollback': ({'qualified_id', 'target_id'}, set()),
            'job': ({'job_id'}, set()),
        }
        if operation not in permitted:
            raise ValueError('unsupported qualified operation')
        required, optional = permitted[operation]
        if not required <= set(payload) or set(payload) - required - optional:
            raise ValueError('unexpected or missing qualified request fields')
        if operation == 'job':
            import re
            if not re.fullmatch('[a-f0-9]{32}', payload['job_id']):
                raise ValueError('invalid job id')
            root = self.lifecycle.jobs / 'requests' / payload['job_id']
            request = read(root / 'request.json')
            if (root / 'outcome.json').exists():
                return read(root / 'outcome.json')
            return {'status': 'RUNNING' if request['session_id'] == self.session_id else 'CANNOT_QUALIFY',
                    'reason': 'pending' if request['session_id'] == self.session_id else
                    'host interrupted; retained attempt is not automatically retried'}
        if operation == 'create':
            outcome = self.lifecycle.start(**payload)
            if outcome['status'] == 'QUALIFIED':
                root = self.lifecycle.jobs / outcome['job_id']
                cert = read(root / 'cycle/bundle/certificate.json')
                admission = self.registry.issue(outcome['job_id'], payload['agent_id'],
                    payload['behavioral_question'], cert['identity'])
                return {**outcome, **admission}
            return outcome
        if operation == 'execute':
            record = self.registry.inspect(payload['qualified_id'])
            population = self.catalog.collect(record['agent_id'], payload.get('time_range'), 'population')
            if population.get('synthetic') is not False:
                raise ValueError('qualified population requires authentic telemetry')
            return self.registry.execute(payload['qualified_id'], population)
        if operation == 'inspect':
            return self.registry.inspect(payload['qualified_id'])
        if operation == 'receipt':
            return self.registry.execution_receipt(**payload)
        return self.registry.control(payload['qualified_id'], operation, payload.get('target_id'))
