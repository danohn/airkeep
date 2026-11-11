"""Hosted background worker, with database election across overlapping deploys."""
import asyncio
import logging
import os
from pathlib import Path
import time

import psycopg

from accounts import OAuth
from app import tick
from config import Settings
from hosted_accounts import HostedAccounts
from postgres import PostgresStore
from push import APNs

log = logging.getLogger('airkeep.worker')


async def run():
    settings = Settings()
    if settings.deployment != 'hosted':
        raise RuntimeError('Run the separate worker with AIRKEEP_DEPLOYMENT=hosted.')
    url = settings.database_url.get_secret_value()
    store = PostgresStore(url)
    oauth = OAuth(store, settings)
    accounts = HostedAccounts(oauth, settings)
    push = APNs(settings)
    health = Path(os.environ.get('AIRKEEP_WORKER_HEALTH_PATH', '/tmp/airkeep-worker-health'))
    try:
        # A session lock admits one active scheduler, including during rolling
        # deployment. All state changes still use the shared transaction locks.
        with psycopg.connect(url, autocommit=True) as election:
            while True:
                if not election.execute('SELECT pg_try_advisory_lock(734912605)').fetchone()[0]:
                    health.touch()
                    await asyncio.sleep(5)
                    continue
                try:
                    while True:
                        election.execute('SELECT 1')
                        now = time.time()
                        try:
                            oauth.maintain(now)
                            failed = False
                            for row in store.rows('SELECT id FROM accounts WHERE disabled=0 ORDER BY id'):
                                try:
                                    with store.tenant(row['id']):
                                        await tick(store, settings, push, now, True)
                                        store.execute('DELETE FROM samples WHERE at<?', (now-7*86400,))
                                        store.execute('DELETE FROM ingest_events WHERE at<?', (now-7*86400,))
                                except Exception:
                                    failed = True
                                    log.exception('Tenant background iteration failed')
                            await accounts.tick()
                            if not failed:
                                health.touch()
                        except Exception:
                            log.exception('Hosted worker iteration failed')
                        await asyncio.sleep(5)
                finally:
                    election.execute('SELECT pg_advisory_unlock(734912605)')
    finally:
        await push.close()
        await accounts.close()
        store.close()
        health.unlink(missing_ok=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    asyncio.run(run())
