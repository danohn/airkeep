"""Apply hosted migrations and grant an existing restricted runtime role."""
import argparse
import os
from pathlib import Path

import psycopg
from psycopg import sql

from postgres import TENANT_TABLES


def migrate(url, runtime_role):
    with psycopg.connect(url) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(734912604)')
        connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
        for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
            if connection.execute('SELECT 1 FROM schema_migrations WHERE name=%s', (path.name,)).fetchone():
                continue
            connection.execute(path.read_text())
            connection.execute('INSERT INTO schema_migrations(name) VALUES(%s)', (path.name,))
        for name in TENANT_TABLES:
            table = sql.Identifier('airkeep_data', name)
            connection.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(table))
            connection.execute(sql.SQL('ALTER TABLE {} FORCE ROW LEVEL SECURITY').format(table))
            connection.execute(sql.SQL('DROP POLICY IF EXISTS tenant_isolation ON {}').format(table))
            connection.execute(sql.SQL("CREATE POLICY tenant_isolation ON {} USING (tenant_id=nullif(current_setting('airkeep.tenant_id',true),'')) WITH CHECK (tenant_id=nullif(current_setting('airkeep.tenant_id',true),''))").format(table))
            connection.execute(sql.SQL("CREATE OR REPLACE VIEW {} WITH (security_invoker=true) AS SELECT * FROM {} WHERE tenant_id=nullif(current_setting('airkeep.tenant_id',true),'') WITH CASCADED CHECK OPTION").format(sql.Identifier('public', name), table))
        role = sql.Identifier(runtime_role)
        connection.execute(sql.SQL('GRANT USAGE ON SCHEMA public,airkeep_data TO {}').format(role))
        connection.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public,airkeep_data TO {}').format(role))
        connection.execute(sql.SQL('REVOKE ALL ON schema_migrations FROM {}').format(role))
        connection.execute(sql.SQL('GRANT SELECT ON schema_migrations TO {}').format(role))
        connection.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA airkeep_data TO {}').format(role))
        connection.execute(sql.SQL('REVOKE CREATE ON SCHEMA public FROM {}').format(role))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-role', default='airkeep_runtime')
    arguments = parser.parse_args()
    migrate(os.environ['AIRKEEP_MIGRATION_DATABASE_URL'], arguments.runtime_role)
