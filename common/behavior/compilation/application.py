"""Operator-configured application binding; question callers supply only agent and question."""
from pathlib import Path
import time

from .admission import QualifiedRegistry
from .autonomous import AgentCatalog, AutonomousLifecycle, InsufficientSource
from .engine import Policy
from .llm import Model
from .native_corpus import NativeCorpus
from .pinned_runtime import PinnedRuntime
from .qualified_host import QualifiedHost
from .storage import read, digest, filehash


class RetainedTelemetry:
    """Authenticated telemetry exports, with immutable origin receipts and split membership.

    A host may replace this callable with its existing catalog/query API collector.
    Export metadata is trusted operator configuration, never supplied by a question.
    """
    def __init__(self, agents):
        self.agents = agents

    def __call__(self, agent_id, time_range, purpose):
        started = time.monotonic()
        config = self.agents[agent_id]['telemetry']
        manifest = read(config['manifest'])
        if filehash(config['manifest']) != config['sha256'] or manifest['synthetic'] is not False:
            raise InsufficientSource('telemetry origin identity failed')
        if time_range is not None and time_range != manifest['time_range']:
            raise InsufficientSource('requested time range requires a fresh authenticated telemetry export')
        if purpose not in ('discovery', 'population'):
            raise ValueError('unsupported telemetry purpose')
        traces = []
        for item in manifest['splits'][purpose]:
            path = Path(config['manifest']).parent / item['file']
            if filehash(path) != item['sha256']:
                raise ValueError('retained telemetry changed')
            trace = read(path)
            if trace['agent'] != agent_id or trace['trace_id'] != item['trace_id']:
                raise ValueError('telemetry membership mismatch')
            traces.append(trace)
        discovery = {x['trace_id'] for x in manifest['splits']['discovery']}
        population = {x['trace_id'] for x in manifest['splits']['population']}
        if discovery & population:
            raise ValueError('discovery/population overlap')
        return {'traces': traces, 'synthetic': False, 'exclusions': manifest.get('exclusions', []),
                'selection': {'purpose': purpose, 'time_range': manifest['time_range'],
                    'manifest_sha256': config['sha256'], 'source_receipts': manifest['source_receipts'],
                    'trace_ids': [t['trace_id'] for t in traces],
                    'retrieval_latency_ms': (time.monotonic() - started) * 1000}}


def build(config):
    output = Path(config['output_dir'])
    runtime = PinnedRuntime(config['runtime'], output / 'model-calls',
                            config.get('runner_module', 'sandbox_runner'))
    model = Model(output / 'model-calls', config.get('model', 'gpt-6-astra'),
                  config.get('model_timeout_seconds', 600), config.get('model_reuse_from'))
    telemetry = RetainedTelemetry(config['agents'])
    catalog = AgentCatalog(config['agents'], telemetry)
    policy = Policy(**config.get('policy', {}))
    lifecycle = AutonomousLifecycle(output / 'jobs', catalog, model, runtime,
        lambda root, behavior, contract, snapshot, model: NativeCorpus(
            root, behavior, contract, snapshot, model, runtime.validate_trace), policy=policy,
        discovery_reuse_job=config.get('discovery_reuse_job'))
    def observe(agent_id, expected):
        # Artifact identities are checked against private frozen bytes by registry;
        # environmental identities are freshly computed from actual host inputs.
        return {**expected, **catalog.identity(agent_id), 'sdk': runtime.identity(),
                'jev': digest(runtime.jev_config), 'policy': digest({
                    'max_repairs': policy.max_repairs, 'audit_per_label': policy.audit_per_label,
                    'lifetime_executions': policy.lifetime_executions})}
    registry = QualifiedRegistry(output / 'qualified', output / 'jobs', observe, runtime.run_frozen)
    principal = config.get('principal', 'local-operator')
    def authorize(actual, operation):
        if actual != principal:
            raise PermissionError('qualified host principal mismatch')
    return QualifiedHost(lifecycle, registry, catalog, authorize)
