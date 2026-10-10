"""Generic completeness transport adapter over the existing native SDK profile.

This module normalizes original typed cells. It neither interprets actions nor
manufactures source closure. Missing sidecars leave coverage unverified while
the original native trace remains available to the Behavioral Program.
"""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import tempfile

from .storage import digest


class NativePopulationLoader:
    """Trusted adapter for Cardinal's existing exhaustive trace-population CLI.

    Command and custody are operator configuration. Each invocation selects one
    immutable catalog set, drains its population, and preserves original typed
    reads. No quiet-period heuristic establishes source closure.
    """
    def __init__(self, command, archive, *, timeout=180):
        self.command = tuple(command)
        self.archive = Path(archive)
        self.archive.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    def __call__(self, execution_id, source):
        rows = source['ended']
        # CLI bounds use milliseconds. Round outwards, never truncate coverage.
        start_ms = min(int(r['startTimeUnixNano']) for r in rows) // 1_000_000
        end_ms = max(int(r['endTimeUnixNano']) for r in rows) // 1_000_000 + 1
        def stamp(ms):
            return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        directory = Path(tempfile.mkdtemp(prefix='population-', dir=self.archive))
        output = directory / 'native'
        command = [*self.command, 'trace-population', '--org', source['identities']['organization'],
                   '--service-name', source['identities']['service'], '--start', stamp(start_ms),
                   '--end', stamp(end_ms), '--output', str(output)]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise OSError('native population transport timed out') from exc
        (directory / 'stdout').write_bytes(completed.stdout)
        (directory / 'stderr').write_bytes(completed.stderr)
        if completed.returncode or list(output.rglob('*.error.json')):
            raise ValueError('native population transport incomplete')
        load = lambda p: json.loads(p.read_text())
        segment_set = load(output / 'segment_set.json')
        population = load(output / 'trace_ids.json')
        trace_id = source['trace_id']
        if (segment_set['organization_id'] != source['identities']['organization'] or
                segment_set['start_ms'] != start_ms or segment_set['end_ms'] != end_ms or
                trace_id not in population or len(population) != len(set(population))):
            raise ValueError('native population membership or interval mismatch')
        native = load(output / 'traces' / (trace_id + '.json'))
        if native['traceId'] != trace_id:
            raise ValueError('native trace identity mismatch')
        from behavior_sdk.native_profile import adapt
        related = {trace_id}
        for other_id in population:
            original = load(output / 'traces' / (other_id + '.json'))
            if original['traceId'] != other_id:
                raise ValueError('native census trace identity mismatch')
            decoded = adapt(original, source['agent_id'])
            if any(event['attrs'].get('cardinal_execution_id') == execution_id
                   for event in decoded['events']):
                related.add(other_id)
        catalogs = [dict(id=str(output / 'segment_set.json'), sha256=digest(segment_set), immutable=True)]
        # Exhaustiveness is supplied by this enrolled Cardinal CLI, not trace
        # attributes. Other traces in the service interval remain in the archive.
        census = dict(complete=True, execution_id=execution_id, trace_ids=sorted(related),
            span_ids=[], catalogs_sha256=digest(catalogs), start_ns=str(start_ms * 1_000_000),
            end_ns=str(end_ms * 1_000_000), receipt=dict(segment_set_sha256=digest(segment_set),
                population_sha256=digest(population), native_read_sha256=digest(native)))
        retained = normalize_native(execution_id, source['agent_id'], native, catalogs, census)
        census['span_ids'] = [event['id'] for event in retained['trace']['events']]
        return retained


def _text_hash(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('captured content must be original text')
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def normalize_native(execution_id, agent_id, native_read, catalogs, census):
    # Reuse the unchanged installed SDK decoder for every agent.
    from behavior_sdk.native_profile import adapt
    from behavior_sdk.openinference_read_trace import _cell
    # An OTel trace may contain parents, siblings and delivery outside this
    # enrolled execution. Select by original explicit execution identity only;
    # never by a source-supplied allowlist, name or behavioral relevance.
    full_trace = adapt(native_read, agent_id)
    selected_ids = {event['id'] for event in full_trace['events']
                    if event['attrs'].get('cardinal_execution_id') == execution_id}
    selected_rows = [row for row in native_read['rows'] if _cell(row, 'id') in selected_ids]
    scoped_read = {**native_read, 'rows': selected_rows}
    trace = adapt(scoped_read, agent_id)
    observations, gaps = [], []
    for event in trace['events']:
        attrs = event['attrs']
        try:
            evidence = json.loads(attrs['cardinal_execution_evidence'])
            if evidence['schema'] != 'otel-execution-span-v1':
                raise ValueError('unsupported evidence sidecar')
            status = evidence['status']
            if attrs['status_code'] != ['STATUS_CODE_UNSET', 'STATUS_CODE_OK', 'STATUS_CODE_ERROR'][status['code']]:
                raise ValueError('retained status mismatch')
            late, dropped = attrs['cardinal_execution_late_child'], attrs['cardinal_execution_dropped_records']
            if late not in ('true', 'false') or type(dropped) is not int or dropped < 0:
                raise ValueError('explicit late/loss accounting missing')
            sequence = attrs['cardinal_execution_sequence']
            if type(sequence) is not int or sequence < 0:
                raise ValueError('source occurrence sequence missing')
            observations.append(dict(traceId=attrs['trace_id'], spanId=attrs['id'],
                parentSpanId=attrs.get('parent_span_id'), name=attrs['name'],
                kind=attrs.get('openinference_span_kind'), operationId=attrs['cardinal_execution_id'],
                toolCallId=attrs.get('gen_ai_tool_call_id'), sequence=sequence,
                startTimeUnixNano=str(attrs['chq_tsns']), endTimeUnixNano=str(attrs['end_tsns']),
                statusCode=status['code'], statusMessageSha256=_text_hash(status.get('message')),
                eventsSha256=_text_hash(_canonical(evidence['events'])),
                linksSha256=_text_hash(_canonical(evidence['links'])),
                inputSha256=_text_hash(event['input']), outputSha256=_text_hash(event['output']),
                lateChild=late == 'true', dropped=dropped))
        except (KeyError, TypeError, ValueError, IndexError):
            gaps.append('retained_occurrence_evidence_missing:' + event['id'])
    return dict(schema='otel-execution-native-v1', execution_id=execution_id,
        agent_id=agent_id, trace_id=trace['trace_id'], trace=trace,
        observations=observations, catalogs=catalogs, census=census,
        loss=dict(dropped_records=sum(row['dropped'] for row in observations),
            redacted=False if not gaps else None, truncated=False if not gaps else None, gaps=gaps),
        transport=dict(profile='existing-sdk-native-profile', native_read_sha256=digest(native_read),
            native_storage_receipt=native_read['receipt'],
            selection=dict(rule='original-cardinal_execution_id-equals-execution_id',
                execution_id=execution_id, full_span_ids=[event['id'] for event in full_trace['events']],
                selected_span_ids=[event['id'] for event in trace['events']],
                selected_rows=[dict(segment=row.get('segment'), partition=row.get('partition'), row=row.get('row'))
                               for row in selected_rows], scoped_read_sha256=digest(scoped_read))))
