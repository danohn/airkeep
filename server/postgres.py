"""PostgreSQL persistence with transaction-local tenant isolation.

The shared domain services use a small DB-API surface. Scoped, updatable
views preserve that surface and explicitly filter by tenant, while forced
row security also protects direct access to the underlying tables.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
import sys
import threading

import psycopg
from psycopg_pool import ConnectionPool

from domain import Reading
from storage import Store

TENANT_TABLES = (
    'printers', 'samples', 'latest', 'ingest_events', 'config', 'devices',
    'activities', 'alert_rules', 'alert_events', 'outbox',
    'registration_owners', 'push_attempts', 'widget_installations',
)
# Preserve the original positional INSERT statements in the shared services.
POSITIONAL_COLUMNS = {
    'config': 'id,json',
    'registration_owners': 'kind,id,grant_id,"user"',
}
tenant_id = ContextVar('airkeep_tenant_id', default='')


class Record(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def record_row(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Record(zip(names, values))


class Result:
    def __init__(self, cursor=None):
        self.rowcount = cursor.rowcount if cursor else 0
        self.values = cursor.fetchall() if cursor and cursor.description else []
        self.position = 0

    def fetchone(self):
        if self.position >= len(self.values):
            return None
        value = self.values[self.position]
        self.position += 1
        return value

    def fetchall(self):
        values = self.values[self.position:]
        self.position = len(self.values)
        return values

    def __iter__(self):
        return iter(self.fetchall())


def postgres_sql(statement):
    """Translate the limited shared SQLite dialect, leaving literals intact."""
    statement = re.sub(r'json_extract\((\w+\.)?json,\s*\'\$\.enabled\'\)=1',
                       lambda m: f"({m[1] or ''}json::jsonb->>'enabled')='true'", statement)
    statement = re.sub(r'json_extract\((\w+\.)?json,\s*\'\$\.name\'\)',
                       lambda m: f"({m[1] or ''}json::jsonb->>'name')", statement)
    statement = statement.replace('MIN(expires,?)', 'LEAST(expires,?)')
    statement = re.sub(r'\bIS\s+\?', 'IS NOT DISTINCT FROM ?', statement, flags=re.I)
    statement = re.sub(r'ON CONFLICT\(id\)', 'ON CONFLICT(tenant_id,id)', statement, flags=re.I)
    for table, columns in POSITIONAL_COLUMNS.items():
        statement = re.sub(rf'INSERT INTO {table}\s+VALUES',
                           f'INSERT INTO {table}({columns}) VALUES', statement, flags=re.I)
    parts = re.split(r"('(?:''|[^'])*'|\"[^\"]*\")", statement)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(r'\buser\b', '"user"', parts[index], flags=re.I).replace('?', '%s')
    return ''.join(parts)


class Database:
    def __init__(self, store):
        self.store = store
        self.local = threading.local()

    @property
    def in_transaction(self):
        return getattr(self.local, 'connection', None) is not None

    def __enter__(self):
        if self.in_transaction:
            self.local.depth += 1
            return self
        context = self.store.pool.connection()
        connection = context.__enter__()
        try:
            connection.execute("SELECT set_config('airkeep.tenant_id', %s, true)", (tenant_id.get(),))
        except BaseException:
            context.__exit__(*sys.exc_info())
            raise
        self.local.context = context
        self.local.connection = connection
        self.local.depth = 1
        return self

    def __exit__(self, kind, value, traceback):
        self.local.depth -= 1
        if self.local.depth:
            return False
        context = self.local.context
        self.local.connection = None
        self.local.context = None
        return context.__exit__(kind, value, traceback)

    def execute(self, statement, parameters=()):
        if not self.in_transaction:
            with self:
                return self.execute(statement, parameters)
        connection = self.local.connection
        if statement.strip().upper() == 'BEGIN IMMEDIATE':
            # Match existing atomic check-and-write operations across API replicas.
            return Result(connection.execute('SELECT pg_advisory_xact_lock(734912603)'))
        return Result(connection.execute(postgres_sql(statement), parameters))

    def executemany(self, statement, parameters):
        for values in parameters:
            self.execute(statement, values)

    def executescript(self, statement):
        raise RuntimeError('Hosted schemas are installed by the migration command.')

    def commit(self):
        raise RuntimeError('Use the transaction context to commit hosted writes.')


class PostgresStore(Store):
    hosted = True

    def __init__(self, url):
        self.lock = threading.RLock()
        self.db = Database(self)
        self.pool = ConnectionPool(url, min_size=1, max_size=10,
                                   kwargs={'row_factory': record_row}, timeout=10,
                                   open=True)
        self.pool.wait(timeout=15)
        with self.pool.connection() as connection:
            role = connection.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            owns = connection.execute("SELECT 1 FROM pg_tables WHERE schemaname='airkeep_data' AND tableowner=current_user LIMIT 1").fetchone()
            if role['rolsuper'] or role['rolbypassrls'] or owns:
                self.pool.close()
                raise RuntimeError('Hosted runtime needs a non-owner role without superuser or BYPASSRLS.')
            migrations = {file.name for file in (Path(__file__).parent / 'migrations').glob('*.sql')}
            present = connection.execute("SELECT to_regclass('public.schema_migrations') AS name").fetchone()['name']
            if not present or not migrations or not migrations.issubset(
                {row['name'] for row in connection.execute('SELECT name FROM schema_migrations')}
            ):
                self.pool.close()
                raise RuntimeError('Run hosted database migrations before starting Airkeep.')

    @contextmanager
    def tenant(self, user):
        token = tenant_id.set(user)
        try:
            if self.db.in_transaction:
                self.db.local.connection.execute("SELECT set_config('airkeep.tenant_id',%s,true)", (user,))
            yield self
        finally:
            tenant_id.reset(token)
            if self.db.in_transaction and self.db.local.connection.info.transaction_status != psycopg.pq.TransactionStatus.INERROR:
                self.db.local.connection.execute("SELECT set_config('airkeep.tenant_id',%s,true)", (tenant_id.get(),))

    def readings(self):
        rows = self.rows('SELECT json FROM latest WHERE tenant_id=? ORDER BY ams', (tenant_id.get(),))
        return [Reading.model_validate_json(row[0]) for row in rows]

    def printer_connected(self, now):
        return bool(self.rows('SELECT 1 FROM printers WHERE tenant_id=? AND connected=1 AND heartbeat>=?',
                              (tenant_id.get(), now-90)))

    def alert_readings(self, now):
        return self.readings() if self.printer_connected(now) else []

    def history(self, ams, since):
        import json
        return [json.loads(row[0]) for row in self.rows(
            'SELECT json FROM samples WHERE tenant_id=? AND ams=? AND at>=? ORDER BY at DESC LIMIT 3000',
            (tenant_id.get(), ams, since))][::-1]

    def purge_tenant(self, user):
        with self.tenant(user), self.transaction():
            for table in reversed(TENANT_TABLES):
                self.execute(f'DELETE FROM airkeep_data.{table} WHERE tenant_id=?', (user,))
            self.execute('DELETE FROM relay_credentials WHERE "user"=?', (user,))

    def close(self):
        self.pool.close()
