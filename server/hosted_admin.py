"""Hosted account operations and restricted host commands."""
import argparse
import json
import logging
import math
import time
from uuid import uuid4

from fastapi import HTTPException

from accounts import PASSWORD, digest, lock_account, random_token, revoke_user
from config import Settings
from hosted_accounts import cancel_links, canonical_email
from hosted_ingest import Batch, enroll_relay, save_batch
from postgres import PostgresStore


def audit(action, outcome, *, actor=None, target=None):
    # Uvicorn configures this logger in the API. The CLI configures root logging.
    logging.getLogger('uvicorn.error').info(json.dumps({
        'event': 'hosted_admin', 'action': action, 'outcome': outcome, 'at': time.time(),
        'source': 'dashboard' if actor else 'host', 'actorAccountID': actor, 'targetAccountID': target,
    }))


def member_account(store, user):
    account = lock_account(store, user)
    if not account:
        raise HTTPException(404, 'Account not found.')
    if account['role'] != 'member':
        raise HTTPException(409, 'This action is available for member accounts only.')
    return account


def set_suspended(store, user, suspended):
    account = member_account(store, user)
    if bool(account['disabled']) == suspended:
        return False
    store.execute('UPDATE accounts SET disabled=?,version=version+1 WHERE id=?', (int(suspended), user))
    if suspended:
        revoke_user(store, user)
        cancel_links(store.db, user)
        store.execute('DELETE FROM oauth_requests WHERE user=?', (user,))
        store.execute('DELETE FROM oauth_device_requests WHERE owner=?', (user,))
    with store.tenant(user):
        if suspended:
            # Clear registrations and queued work even if they no longer have an owner grant.
            for table in ('outbox', 'registration_owners', 'devices', 'activities', 'widget_installations'):
                store.execute(f'DELETE FROM {table}')
        store.execute('UPDATE printers SET heartbeat=NULL,connected=0')
        store.execute("UPDATE alert_rules SET state=(state::jsonb || '{\"since\":null,\"last_observed\":null}'::jsonb)::text")
    return True


def set_hosted_role(store, email, role):
    email = canonical_email(email)
    with store.transaction() as db:
        account = db.execute('SELECT id,role FROM accounts WHERE lower(username)=? AND disabled=0 FOR UPDATE',
                             (email,)).fetchone()
        if not account:
            raise HTTPException(404, 'No active account has that email address.')
        changed = account['role'] != role
        if changed:
            db.execute('UPDATE accounts SET role=? WHERE id=?', (role, account['id']))
    audit('grant-admin' if role == 'admin' else 'revoke-admin', 'changed' if changed else 'unchanged',
          target=account['id'])
    return {'accountID': account['id'], 'role': role}


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
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('health')
    commands.add_parser('seed-review')
    commands.add_parser('deletion-ledger')
    suspend = commands.add_parser('suspend')
    suspend.add_argument('account_id')
    for command in ('grant-admin', 'revoke-admin'):
        role = commands.add_parser(command)
        role.add_argument('email')
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
        elif arguments.command in ('grant-admin', 'revoke-admin'):
            print(json.dumps(set_hosted_role(store, arguments.email, 'admin' if arguments.command == 'grant-admin' else 'member')))
        else:
            with store.transaction():
                changed = set_suspended(store, arguments.account_id, True)
            audit('suspend', 'changed' if changed else 'unchanged', target=arguments.account_id)
            print(json.dumps({'suspended': True}))
    except HTTPException as error:
        parser.error(error.detail)
    finally:
        store.close()


if __name__ == '__main__':
    main()
