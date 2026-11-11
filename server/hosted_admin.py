"""Restricted operator commands, run on the server with runtime credentials."""
import argparse
import json
import math
import time
from uuid import uuid4

from accounts import PASSWORD, digest, random_token, revoke_user
from config import Settings
from hosted_ingest import Batch, enroll_relay, save_batch
from postgres import PostgresStore


def health(store, now):
    accounts = store.rows('SELECT id,disabled FROM accounts')
    summary = {
        'accounts': len(accounts),
        'suspendedAccounts': sum(bool(row['disabled']) for row in accounts),
        'databaseBytes': store.rows('SELECT pg_database_size(current_database())')[0][0],
        'connectedPrinters': 0,
        'stalePrinters': 0,
        'pendingPush': 0,
        'oldestPushAgeSeconds': None,
        'pushFailuresLastHour': 0,
        'email': {row['state']: row['count'] for row in store.rows(
            'SELECT state,COUNT(*) AS count FROM hosted_email_outbox GROUP BY state')},
        'serverTime': now,
    }
    for account in accounts:
        with store.tenant(account['id']):
            if store.printer_connected(now):
                summary['connectedPrinters'] += 1
                summary['stalePrinters'] += not any(now-reading.observedAt <= 90 for reading in store.readings())
            queue = store.rows('SELECT COUNT(*) AS count,MIN(created) AS oldest FROM outbox')[0]
            summary['pendingPush'] += queue['count']
            if queue['oldest'] is not None:
                summary['oldestPushAgeSeconds'] = max(summary['oldestPushAgeSeconds'] or 0, now-queue['oldest'])
            summary['pushFailuresLastHour'] += store.rows(
                'SELECT COUNT(*) FROM push_attempts WHERE at>=? AND (status IS NULL OR status>=400)', (now-3600,))[0][0]
    return summary


def demo(store):
    user = random_token()
    username = 'review-' + uuid4().hex[:12] + '@example.invalid'
    password = random_token()
    now = time.time()
    observations = [
        {'eventID': uuid4(), 'replay': minute != 0, 'amsID': '0',
         'humidity': round(35 + 5*math.sin(minute/14), 1),
         'temperature': 24.0, 'rawIndex': 2, 'observedAt': now - minute*60}
        for minute in range(90, -1, -1)
    ]
    with store.transaction():
        store.execute("INSERT INTO accounts(id,username,hash,role) VALUES(?,?,?,'member')",
                      (user, username, PASSWORD.hash(password)))
        token = enroll_relay(store, user)['access_token']
        save_batch(store, digest(token), Batch(printerConnected=True, readings=observations))
    return {'email': username, 'password': password, 'accountID': user}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('health')
    commands.add_parser('seed-review')
    commands.add_parser('deletion-ledger')
    suspend = commands.add_parser('suspend')
    suspend.add_argument('account_id')
    arguments = parser.parse_args()
    settings = Settings()
    store = PostgresStore(settings.database_url.get_secret_value())
    try:
        if arguments.command == 'health':
            print(json.dumps(health(store, time.time())))
        elif arguments.command == 'seed-review':
            print(json.dumps(demo(store)))
        elif arguments.command == 'deletion-ledger':
            rows = store.rows('SELECT account_id,deleted_at FROM account_deletions ORDER BY deleted_at,account_id')
            print(json.dumps([dict(row) for row in rows]))
        else:
            with store.tenant(arguments.account_id), store.transaction():
                result = store.execute('UPDATE accounts SET disabled=1,version=version+1 WHERE id=?',
                                       (arguments.account_id,))
                revoke_user(store, arguments.account_id)
                store.execute('UPDATE relay_credentials SET revoked=1 WHERE "user"=?', (arguments.account_id,))
            print(json.dumps({'suspended': result.rowcount == 1}))
    finally:
        store.close()


if __name__ == '__main__':
    main()
