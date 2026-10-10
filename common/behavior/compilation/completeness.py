"""Generic OTel execution coverage, separate from behavioral classification.

Source and retained-data loaders belong to the trusted host. Their adapters
authenticate custody and decode original transport fields before this module
compares observations. A source-authenticated seal is not a behavioral verdict.
"""
from copy import deepcopy
import hashlib
import re
import time

from .files import program_lock
from .storage import digest

ATTRIBUTE = 'execution_completeness'
OBSERVATION_FIELDS = (
    'traceId', 'spanId', 'parentSpanId', 'name', 'kind', 'operationId',
    'toolCallId', 'sequence', 'startTimeUnixNano', 'endTimeUnixNano',
    'statusCode', 'statusMessageSha256', 'eventsSha256', 'linksSha256',
    'inputSha256', 'outputSha256', 'lateChild', 'dropped',
)
IDENTITY_FIELDS = ('organization', 'service', 'source', 'deployment', 'recorder')
HASH_FIELDS = ('statusMessageSha256', 'eventsSha256', 'linksSha256', 'inputSha256', 'outputSha256')


def strip_witness(trace):
    """Caller metadata never crosses the trusted coverage-admission boundary."""
    clean = deepcopy(trace)
    clean.setdefault('attrs', {}).pop(ATTRIBUTE, None)
    for event in clean.get('events', []):
        event.setdefault('attrs', {}).pop(ATTRIBUTE, None)
    return clean


def _body_hash(value):
    return None if value is None else hashlib.sha256(value.encode('utf-8')).hexdigest()


def _observations(rows, required, gaps, label):
    unique = {}
    if not isinstance(rows, list):
        gaps.add(label + '_inventory_missing')
        return unique
    for row in rows:
        if not isinstance(row, dict) or any(field not in row for field in required):
            gaps.add(label + '_observation_fields_missing')
            continue
        key = row.get('spanId')
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{16}', key):
            gaps.add(label + '_span_identity_invalid')
            continue
        if any(row.get(field) is not None and not re.fullmatch('[a-f0-9]{64}', str(row[field])) for field in HASH_FIELDS):
            gaps.add(label + '_digest_invalid')
        if (type(row.get('sequence')) is not int or row['sequence'] < 0 or
                type(row.get('dropped')) is not int or row['dropped'] < 0 or
                type(row.get('lateChild')) is not bool or
                type(row.get('statusCode')) is not int or row['statusCode'] not in (0, 1, 2)):
            gaps.add(label + '_observation_types_invalid')
        try:
            if any(not isinstance(row[field], str) or not re.fullmatch('[0-9]+', row[field]) for field in ('startTimeUnixNano', 'endTimeUnixNano')):
                raise ValueError()
            if int(row['endTimeUnixNano']) < int(row['startTimeUnixNano']):
                raise ValueError()
        except (ValueError, KeyError):
            gaps.add(label + '_time_invalid')
        if row.get('dropped') != 0 or row.get('lateChild') is not False:
            gaps.add(label + '_loss_or_late_work')
        observation = {field: row.get(field) for field in required}
        if key in unique and unique[key] != observation:
            gaps.add(label + '_conflicting_duplicate')
        unique[key] = observation
    return unique


def verify_completeness(contract, source, retained, trace):
    """Verify generic identity/inventory/closure/retention only, fail closed.

The host pins contract and adapter identities. Source seal authenticity and
native census custody come from its configured trusted loaders, not booleans
provided in caller trace attributes. No content is interpreted behaviorally.
"""
    gaps = set()
    clean = strip_witness(trace)
    source = source if isinstance(source, dict) else {}
    retained = retained if isinstance(retained, dict) else {}
    contract = contract if isinstance(contract, dict) else {}
    result = dict(schema='otel-execution-completeness-v1', status='UNVERIFIED',
                  execution_id=source.get('execution_id'), revision=source.get('revision'),
                  generation=source.get('generation'), trace_id=clean.get('trace_id'),
                  scope_id=contract.get('scope_id'), source_sha256=digest(source),
                  retained_sha256=digest(retained), trace_sha256=digest(clean),
                  contract_sha256=digest(contract), gaps=[])
    try:
        if contract.get('schema') != 'otel-execution-contract-v1' or source.get('schema') != 'otel-execution-source-v1' or retained.get('schema') != 'otel-execution-native-v1':
            gaps.add('unsupported_contract_or_evidence_schema')
        execution_id, trace_id = source['execution_id'], source['trace_id']
        if not isinstance(execution_id, str) or not execution_id or not re.fullmatch('[a-f0-9]{32}', trace_id):
            gaps.add('execution_identity_invalid')
        if source['agent_id'] != clean['agent'] or source['trace_id'] != clean['trace_id'] or any(
                retained.get(key) != source[key] for key in ('execution_id', 'agent_id', 'trace_id')):
            gaps.add('execution_identity_mismatch')
        if any(not source.get('identities', {}).get(key) or source['identities'][key] != contract.get('identities', {}).get(key) for key in IDENTITY_FIELDS):
            gaps.add('immutable_identity_mismatch')
        capture = source['capture']
        required = tuple(contract['required_fields'])
        if (not set(OBSERVATION_FIELDS) <= set(required) or len(required) != len(set(required)) or
                capture['required_fields'] != contract['required_fields'] or capture['scope_id'] != contract['scope_id'] or
                not contract['scope_id'] or capture['content_captured'] is not True or
                capture['redacted'] is not False or capture['truncated'] is not False):
            gaps.add('declared_capture_scope_unverified')
        for label, evidence in (('source', source), ('retained', retained)):
            loss = evidence['loss']
            if (type(loss['dropped_records']) is not int or loss['dropped_records'] != 0 or
                    loss['redacted'] is not False or loss['truncated'] is not False or loss['gaps'] != []):
                gaps.add(label + '_loss_accounting_unverified')
        source_rows = _observations(source['ended'], required, gaps, 'source')
        native_rows = _observations(retained['observations'], required, gaps, 'retained')
        starts = source['started']
        if (not starts or any(set(row) != {'span_id', 'parent_span_id', 'sequence'} for row in starts) or
                len({row['span_id'] for row in starts}) != len(starts) or
                len({row['sequence'] for row in starts}) != len(starts) or
                set(source_rows) != {row['span_id'] for row in starts}):
            gaps.add('source_start_end_inventory_mismatch')
        root = source['root_span_id']
        if root not in source_rows:
            gaps.add('source_root_unverified')
        root_parent = source_rows.get(root, {}).get('parentSpanId')
        if root_parent is not None and (contract.get('root_parent_policy') != 'external_boundary' or root_parent in source_rows):
            gaps.add('root_parent_boundary_undeclared')
        if (contract.get('sequence_policy') == 'dense_zero_based' and
                (sorted(row['sequence'] for row in starts) != list(range(len(starts))) or source_rows.get(root, {}).get('sequence') != 0)):
            gaps.add('source_sequence_inventory_invalid')
        if contract.get('sequence_policy') not in ('dense_zero_based', 'unique_nonnegative'):
            gaps.add('source_sequence_policy_undeclared')
        for row in starts:
            ended = source_rows.get(row['span_id'], {})
            if ended.get('parentSpanId') != row['parent_span_id'] or ended.get('sequence') != row['sequence']:
                gaps.add('source_parent_or_sequence_mismatch')
            if row['span_id'] != root and row['parent_span_id'] not in source_rows:
                gaps.add('source_parent_missing')
            current, seen = row['span_id'], set()
            while current != root:
                if current in seen or current not in source_rows:
                    gaps.add('source_ancestry_not_rooted')
                    break
                seen.add(current)
                current = source_rows[current]['parentSpanId']
        for row in source_rows.values():
            if row['traceId'] != trace_id or row['operationId'] != execution_id:
                gaps.add('source_observation_identity_mismatch')
            if root in source_rows and not (int(source_rows[root]['startTimeUnixNano']) <= int(row['startTimeUnixNano']) <=
                    int(row['endTimeUnixNano']) <= int(source_rows[root]['endTimeUnixNano'])):
                gaps.add('source_occurrence_outside_root_scope')
        if source_rows != native_rows:
            gaps.add('source_native_observation_mismatch')
        closure = source['closure']
        inventory_sha = digest({'started': starts, 'ended': source['ended']})
        if (type(source['generation']) is not int or source['generation'] < 1 or
                not isinstance(source['revision'], str) or not source['revision'] or
                closure['sealed_revision'] != source['revision'] or closure['generation'] != source['generation'] or
                closure['inventory_sha256'] != inventory_sha or closure['outstanding'] != [] or closure['invalidations'] != []):
            gaps.add('source_seal_unverified')
        kind = closure['kind']
        if kind not in contract['allowed_closure_modes']:
            gaps.add('source_closure_mode_unapproved')
        elif kind == 'process_exit':
            if closure.get('process_terminated') is not True or not closure.get('custody_receipt'):
                gaps.add('process_exit_unverified')
        elif kind == 'enforced_seal':
            if (closure.get('scope_exhausted') is not True or closure.get('late_work_rejected') is not True or
                    not contract.get('guard_identity') or closure.get('guard_identity') != contract['guard_identity']):
                gaps.add('enforced_scope_seal_unverified')
        else:
            gaps.add('source_closure_mode_unknown')
        catalogs = retained['catalogs']
        if not catalogs or any(not row.get('id') or row.get('immutable') is not True or not re.fullmatch('[a-f0-9]{64}', row.get('sha256', '')) for row in catalogs):
            gaps.add('immutable_catalog_boundary_missing')
        census = retained['census']
        if (census['complete'] is not True or census['execution_id'] != execution_id or not census['receipt'] or
                census['catalogs_sha256'] != digest(catalogs) or census['trace_ids'] != [trace_id] or
                len(census['span_ids']) != len(set(census['span_ids'])) or set(census['span_ids']) != set(source_rows)):
            gaps.add('retained_execution_census_unverified')
        if not source_rows or any(not (int(census['start_ns']) <= int(row['startTimeUnixNano']) <= int(row['endTimeUnixNano']) < int(census['end_ns'])) for row in source_rows.values()):
            gaps.add('retained_census_interval_mismatch')
        if strip_witness(retained['trace']) != clean:
            gaps.add('retained_trace_binding_mismatch')
        events = clean['events']
        if len(events) != len(source_rows) or {e['id'] for e in events} != set(source_rows):
            gaps.add('trace_occurrence_inventory_mismatch')
        chronological = sorted(events, key=lambda event: (event['attrs']['chq_tsns'], event['id']))
        chronological_sequences = {event['id']: seq for seq, event in enumerate(chronological)}
        for event in events:
            row = native_rows.get(event['id'])
            if not row:
                continue
            attrs = event['attrs']
            if (event.get('parent_id') != row['parentSpanId'] or attrs.get('name') != row['name'] or
                    event.get('name') != (attrs.get('tool_name') or attrs.get('name') or attrs.get('span_name') or '') or
                    attrs.get('cardinal_execution_sequence') != row['sequence'] or
                    event['seq'] != chronological_sequences[event['id']] or
                    str(attrs['chq_tsns']) != row['startTimeUnixNano'] or str(attrs['end_tsns']) != row['endTimeUnixNano'] or
                    _body_hash(event.get('input')) != row['inputSha256'] or _body_hash(event.get('output')) != row['outputSha256']):
                gaps.add('trace_original_field_binding_mismatch')
            # The enrolled transport adapter declares its OTel-kind projection;
            # this is representation binding, not behavioral eligibility.
            if contract['kind_mapping'].get(row['kind']) != event['kind']:
                gaps.add('trace_kind_projection_mismatch')
            expected = ('STATUS_CODE_UNSET', 'STATUS_CODE_OK', 'STATUS_CODE_ERROR')[row['statusCode']]
            if attrs.get('status_code') != expected or _body_hash(attrs.get('status_message')) != row['statusMessageSha256']:
                gaps.add('trace_status_projection_mismatch')
            if event.get('error') != (attrs.get('status_message') if row['statusCode'] == 2 else None):
                gaps.add('trace_error_projection_mismatch')
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        gaps.add('required_coverage_proof_missing_or_invalid')
    result['gaps'] = sorted(gaps)
    result['status'] = 'UNVERIFIED' if gaps else 'VERIFIED'
    return result


class CompletenessAdmission:
    """Reuse the existing private registry issuer and lock/publication pattern.

Only host-configured loaders enter here. Returned unverified coverage leaves
the original trace available for an independent existential behavioral MATCH.
"""
    def __init__(self, issuer, load_contract, load_source, load_retained, publish_if_current):
        self.issuer = issuer
        self.root = issuer.root / 'completeness'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.load_contract, self.load_source, self.load_retained = load_contract, load_source, load_retained
        self.publish_if_current = publish_if_current

    def admit(self, execution_id, trace):
        clean = strip_witness(trace)
        key = digest(execution_id)
        with program_lock(self.root / 'registry'):
            try:
                source = self.load_source(execution_id)
                contract = self.load_contract(source['agent_id'])
                retained = self.load_retained(execution_id, source)
                receipt = verify_completeness(contract, source, retained, clean)
                if source.get('execution_id') != execution_id:
                    receipt['gaps'].append('requested_execution_identity_mismatch')
                current = self.load_source(execution_id)
                if current != source or self.load_contract(source['agent_id']) != contract:
                    receipt['gaps'].append('source_changed_during_admission')
                prior_path = self.root / (key + '.watermark.json')
                if prior_path.exists():
                    prior = self.issuer._read(prior_path)
                    if (type(source.get('generation')) is not int or source['generation'] < prior.get('generation', 0) or
                            (source['generation'] == prior.get('generation') and
                             (source.get('revision') != prior.get('revision') or digest(source) != prior.get('source_sha256')))):
                        receipt['gaps'].append('source_revision_not_monotonic')
                if type(source.get('generation')) is int and source['generation'] > 0 and (
                        not prior_path.exists() or source['generation'] > prior.get('generation', 0)):
                    self.issuer._save(prior_path, {'generation': source['generation'], 'revision': source.get('revision'),
                        'source_sha256': digest(source)})
            except (OSError, KeyError, TypeError, ValueError):
                receipt = dict(schema='otel-execution-completeness-v1', status='UNVERIFIED', execution_id=execution_id,
                    trace_id=clean.get('trace_id'), trace_sha256=digest(clean), gaps=['trusted_coverage_loader_unavailable'])
            receipt['gaps'] = sorted(set(receipt['gaps']))
            receipt['status'] = 'UNVERIFIED' if receipt['gaps'] else 'VERIFIED'
            receipt['issued_at'] = time.time()
            receipt['receipt_id'] = digest(receipt)
            self.issuer._save(self.root / (key + '.json'), receipt)
            published = False
            if receipt['status'] == 'VERIFIED':
                try:
                    published = self.publish_if_current(execution_id, source, receipt)
                except (OSError, ValueError):
                    pass
            if receipt['status'] == 'VERIFIED' and not published:
                receipt['status'] = 'UNVERIFIED'
                receipt['gaps'].append('source_compare_and_publish_failed')
                receipt.pop('receipt_id')
                receipt['receipt_id'] = digest(receipt)
                self.issuer._save(self.root / (key + '.json'), receipt)
            # UNVERIFIED metadata is useful disclosure but can never unlock the
            # complete existential composition in an authored program.
            clean['attrs'][ATTRIBUTE] = receipt
            return clean, receipt

    def inspect(self, execution_id):
        """Historical signed receipt only; never current execution admission."""
        with program_lock(self.root / 'registry'):
            return self.issuer._read(self.root / (digest(execution_id) + '.json'))

    def evaluate(self, execution_id, trace, run_sdk, publish_result):
        """Existing SDK execution plus a final trusted source CAS publication.

        publish_result must atomically compare the current source envelope to
        the supplied authenticated source digest and store the result. A stale
        or missing coverage proof cannot publish a negative conclusion. An
        independent positive result stays visible alongside its coverage gap.
        """
        admitted, receipt = self.admit(execution_id, trace)
        result = run_sdk(admitted)
        original = result.get('actual', result.get('verdict'))
        current = None
        contract_current = None
        try:
            current = self.load_source(execution_id)
            contract_current = self.load_contract(current['agent_id'])
        except (OSError, KeyError, TypeError, ValueError):
            pass
        coverage_current = (receipt['status'] == 'VERIFIED' and current is not None and
            digest(current) == receipt.get('source_sha256') and contract_current is not None and
            digest(contract_current) == receipt.get('contract_sha256'))
        if original == 'NON_MATCH' and not coverage_current:
            result = {**result, 'original_sdk_verdict': original, 'actual': 'UNKNOWN', 'verdict': 'UNKNOWN',
                      'reason': 'negative execution conclusion lacks current verified coverage'}
        row = {**result, 'coverage_receipt': receipt, 'coverage_current': coverage_current,
               'execution_id': execution_id, 'trace_sha256': digest(admitted)}
        # CAS callback binds this pair, rather than accepting source revision
        # alone after a contract revocation or identity change.
        publication_identity = dict(source_sha256=digest(current) if current is not None else None,
            contract_sha256=digest(contract_current) if contract_current is not None else None)
        try:
            published = publish_result(execution_id, publication_identity, row)
        except (OSError, ValueError):
            published = False
        if not published and original == 'NON_MATCH':
            row = {**row, 'original_sdk_verdict': original, 'actual': 'UNKNOWN', 'verdict': 'UNKNOWN',
                   'coverage_current': False, 'reason': 'source changed before result publication'}
        row['published'] = bool(published)
        return row

    def run_pending(self, enumerate_pending, run_sdk, publish_result):
        """One ordinary host-worker poll; callers schedule it in existing host.

        The enumerator supplies opaque IDs from trusted durable source storage.
        No client receipt, source envelope, trace attrs or path enters this API.
        """
        rows = []
        for execution_id in enumerate_pending():
            source = self.load_source(execution_id)
            retained = self.load_retained(execution_id, source)
            rows.append(self.evaluate(execution_id, retained['trace'], run_sdk, publish_result))
        return rows
