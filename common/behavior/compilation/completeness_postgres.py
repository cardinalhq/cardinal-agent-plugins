"""Configured private JSON KV transport for the existing completeness host.

The database role must restrict source/contract writes to enrolled producers and
operators. JSON flags from question callers never enter this adapter. Publication
locks source AND contract rows until its durable receipt/result commit.
"""
from contextlib import contextmanager
import json
import re
from urllib.parse import quote, unquote

from .storage import digest


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', value):
        raise ValueError('invalid configured SQL identifier')
    return '"' + value + '"'


class PostgresCompletenessStore:
    def __init__(self, connect, *, table, key_column='key', value_column='value', schema='public'):
        self.connect = connect
        self.table = _identifier(schema) + '.' + _identifier(table)
        self.key, self.value = _identifier(key_column), _identifier(value_column)

    @contextmanager
    def _connection(self):
        try:
            with self.connect() as connection:
                yield connection
        except Exception as exc:
            if type(exc).__module__.startswith('psycopg'):
                raise OSError('trusted completeness database unavailable') from exc
            raise

    @staticmethod
    def source_key(execution_id):
        return 'cardinal.execution-source.v1/' + quote(execution_id, safe='') + '/source'

    @staticmethod
    def contract_key(agent_id):
        return 'cardinal.execution-contract.v1/' + quote(agent_id, safe='') + '/contract'

    def _read(self, cursor, key, lock=False):
        cursor.execute(f'SELECT {self.value} FROM {self.table} WHERE {self.key} = %s' + (' FOR UPDATE' if lock else ''), (key,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError('trusted completeness record unavailable')
        value = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if not isinstance(value, dict):
            raise ValueError('trusted completeness record must be an object')
        return value

    def load_source(self, execution_id):
        with self._connection() as connection, connection.cursor() as cursor:
            value = self._read(cursor, self.source_key(execution_id))
            if value.get('execution_id') != execution_id:
                raise ValueError('source execution identity mismatch')
            return value

    def load_contract(self, agent_id):
        with self._connection() as connection, connection.cursor() as cursor:
            contract = self._read(cursor, self.contract_key(agent_id))
            self.validate_enrollment(contract)
            return contract

    @staticmethod
    def validate_enrollment(contract):
        identities=contract.get('identities',{})
        for key in ('organization','service','source','deployment','recorder'):
            value=identities.get(key)
            if not isinstance(value,str) or value.strip().lower() in ('','unverified','unknown','latest','main','master','head'):
                raise ValueError('immutable enrolled identity missing')
        # Immutable source/deployment/recorder custody is content addressed.
        for key in ('source','deployment','recorder'):
            if not re.search(r'[a-f0-9]{40,64}',identities[key]):
                raise ValueError('enrollment identity must include immutable digest')
        if 'enforced_seal' in contract.get('allowed_closure_modes',[]):
            if contract.get('guard_identity') != identities['recorder']:
                raise ValueError('enrolled guard must bind immutable recorder identity')

    def _publish(self, execution_id, expected_source, expected_contract, row, suffix):
        with self._connection() as connection, connection.cursor() as cursor:
            source = self._read(cursor, self.source_key(execution_id), lock=True)
            if source.get('execution_id') != execution_id or not isinstance(source.get('agent_id'), str):
                return False
            contract = self._read(cursor, self.contract_key(source['agent_id']), lock=True)
            self.validate_enrollment(contract)
            if digest(source) != expected_source or digest(contract) != expected_contract:
                return False
            key = 'cardinal.execution-admission.v1/' + quote(execution_id, safe='') + '/' + suffix
            cursor.execute(f'INSERT INTO {self.table} ({self.key}, {self.value}) VALUES (%s, %s) '
                f'ON CONFLICT ({self.key}) DO UPDATE SET {self.value} = EXCLUDED.{self.value}',
                (key, json.dumps(row, sort_keys=True, separators=(',', ':'), ensure_ascii=False)))
            return True

    def publish_if_current(self, execution_id, source, receipt):
        if (receipt.get('revision') != source.get('revision') or
                receipt.get('generation') != source.get('generation') or
                receipt.get('source_sha256') != digest(source)):
            return False
        return self._publish(execution_id, digest(source), receipt.get('contract_sha256'), receipt, 'receipt')

    def publish_result(self, execution_id, identities, result):
        return self._publish(execution_id, identities.get('source_sha256'),
            identities.get('contract_sha256'), result, 'result')

    def enumerate_pending(self, limit=100):
        """Bounded source discovery; unchanged successful revisions are skipped.

        Operators schedule polls and enforce source retention in existing host.
        Source IDs must be retained until either ingested or explicitly expired.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('poll batch must be between 1 and 1000')
        prefix='cardinal.execution-source.v1/'
        with self._connection() as connection, connection.cursor() as cursor:
            cursor_key='cardinal.execution-admission.v1/poll-cursor'
            cursor.execute(f'INSERT INTO {self.table} ({self.key}, {self.value}) VALUES (%s, %s) '
                f'ON CONFLICT ({self.key}) DO NOTHING',(cursor_key,json.dumps({'after':''})))
            position=self._read(cursor,cursor_key,lock=True)['after']
            def batch(after):
                cursor.execute(f'SELECT {self.key}, {self.value} FROM {self.table} '
                    f'WHERE {self.key} > %s AND left({self.key}, %s) = %s AND right({self.key}, 7) = %s ORDER BY {self.key} LIMIT %s',
                    (after,len(prefix),prefix,'/source',limit))
                return cursor.fetchall()
            records=batch(position)
            if not records and position:
                records=batch('')
            after=records[-1][0] if records else ''
            cursor.execute(f'UPDATE {self.table} SET {self.value}=%s WHERE {self.key}=%s',
                (json.dumps({'after':after}),cursor_key))
            selected=[]
            for key, raw in records:
                if not key.endswith('/source'):
                    continue
                execution_id=unquote(key[len(prefix):-len('/source')])
                if not execution_id or self.source_key(execution_id)!=key:
                    continue
                try:
                    source=json.loads(raw) if isinstance(raw,str) else raw
                    if not isinstance(source,dict) or source.get('execution_id')!=execution_id:
                        raise ValueError('malformed source identity')
                except (TypeError,ValueError):
                    # Preserve canonical visible execution identity for the host
                    # to durably disclose UNKNOWN; commit the cursor advance.
                    selected.append(execution_id)
                    continue
                result_key='cardinal.execution-admission.v1/'+quote(execution_id,safe='')+'/result'
                try:
                    prior=self._read(cursor,result_key)
                    contract=self._read(cursor,self.contract_key(source['agent_id']))
                    receipt=prior.get('coverage_receipt',{})
                    if (prior.get('coverage_current') is True and prior.get('published') is not False and
                        receipt.get('source_sha256')==digest(source) and receipt.get('contract_sha256')==digest(contract)):
                        continue
                except (KeyError,ValueError):
                    pass
                selected.append(execution_id)
                if len(selected)>=limit:
                    break
            return selected

    def execution_id(self, trace):
        ids = {event.get('attrs', {}).get('cardinal_execution_id') for event in trace.get('events', [])}
        if len(ids) != 1 or not isinstance(next(iter(ids)), str) or not next(iter(ids)):
            raise ValueError('native execution identity missing or ambiguous')
        return next(iter(ids))

    def bind(self, issuer, load_retained):
        from .completeness import CompletenessAdmission
        admission = CompletenessAdmission(issuer, self.load_contract, self.load_source,
            load_retained, self.publish_if_current)
        def execute(trace, run_sdk):
            try:
                execution_id=self.execution_id(trace)
            except ValueError:
                execution_id='unresolved-native-identity:'+digest(trace)
            return admission.evaluate(execution_id, trace, run_sdk, self.publish_result)
        issuer.coverage_execute = execute
        return admission
