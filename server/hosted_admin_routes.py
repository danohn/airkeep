"""Browser-only administration for the hosted service."""
from contextlib import contextmanager
import json
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import ConfigDict

from accounts import Principal, lock_account
from hosted_accounts import EmailAddress, canonical_email
from hosted_admin import audit, member_account, set_suspended
from hosted_ingest import relay_status
from web_routes import push_setup
from widgets import display_fingerprint

ACCOUNT_COLUMNS = 'id,username,role,disabled'
MAX_ATTEMPT_ID = 2**63 - 1
APNS_REASONS = frozenset({
    'BadCertificate', 'BadCertificateEnvironment', 'BadCollapseId', 'BadDeviceToken', 'BadExpirationDate', 'BadMessageId', 'BadPath',
    'BadPriority', 'BadTopic', 'DeviceTokenNotForTopic', 'DuplicateHeaders', 'ExpiredProviderToken',
    'Forbidden', 'IdleTimeout', 'InternalServerError', 'InvalidProviderToken', 'MethodNotAllowed',
    'MissingDeviceToken', 'MissingProviderToken', 'MissingTopic', 'PayloadEmpty', 'PayloadTooLarge',
    'ServiceUnavailable', 'Shutdown', 'TooManyProviderTokenUpdates', 'TooManyRequests',
    'TopicDisallowed', 'Unregistered', 'connection_failed', 'credentials_missing',
})
AccountID = Annotated[str, Path(min_length=1, max_length=128)]


class Invitation(EmailAddress):
    model_config = ConfigDict(extra='forbid')


def account_details(account):
    return {'id': account['id'], 'email': account['username'], 'role': account['role'],
            'disabled': bool(account['disabled'])}


def apns_reason(reason):
    return reason if reason is None or reason in APNS_REASONS else 'provider_error'


def apns_request_id(value):
    try:
        return str(UUID(value)) if value else None
    except ValueError:
        return None


def install_hosted_admin(app, connection, accounts, settings):
    oauth, store = accounts.oauth, accounts.store
    started = time.monotonic()

    def admin(identity: Principal = Depends(connection)):
        # Browser sessions and API bearer grants have different audiences. The
        # request connection resolves the current role from PostgreSQL.
        if identity.client != 'dashboard' or identity.role != 'admin':
            raise HTTPException(403, 'Administrator access required.')
        return identity

    router = APIRouter(prefix='/v2/admin/hosted', dependencies=[Depends(admin)])

    def lock_admin(identity):
        actor = lock_account(store, identity.user)
        grant = oauth.active(identity.grant)
        if (not actor or actor['disabled'] or actor['role'] != 'admin'
                or not grant or grant['user'] != identity.user or grant['client'] != 'dashboard'):
            raise HTTPException(403, 'Administrator access required.')

    @contextmanager
    def selected(user, identity, view):
        rows = store.rows(f'SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id=?', (user,))
        if not rows:
            raise HTTPException(404, 'Account not found.')
        with store.tenant(user):
            yield rows[0]
        audit(view, 'read', actor=identity.user, target=user)

    @router.get('/health')
    def health(identity: Principal = Depends(admin)):
        size = store.rows('SELECT pg_database_size(current_database())')[0][0]
        email = store.rows('SELECT state,COUNT(*) AS count FROM hosted_email_outbox GROUP BY state')
        checks = push_setup(settings)
        audit('health', 'read', actor=identity.user)
        return {'version': app.version, 'uptimeSeconds': time.monotonic()-started, 'serverTime': time.time(),
                'database': {'status': 'ok', 'bytes': size}, 'pushSetup': checks,
                'email': {'configured': accounts.configured, 'counts': {row['state']: row['count'] for row in email}}}

    @router.get('/accounts')
    def directory(cursor: str = Query(default='', max_length=128), limit: int = Query(default=50, ge=1, le=100),
                  identity: Principal = Depends(admin)):
        rows = store.rows(f'SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id>? ORDER BY id LIMIT ?', (cursor, limit+1))
        audit('accounts', 'read', actor=identity.user)
        return {'accounts': [account_details(row) for row in rows[:limit]],
                'nextCursor': rows[limit-1]['id'] if len(rows) > limit else None}

    @router.get('/accounts/{user}/overview')
    def overview(user: AccountID, identity: Principal = Depends(admin)):
        now = time.time()
        with selected(user, identity, 'overview') as account:
            oldest = store.rows("SELECT MIN((json::jsonb->>'observedAt')::double precision) FROM latest")[0][0]
            return {'account': account_details(account), 'printerConnected': store.printer_connected(now),
                    'relayStatus': relay_status(store, now)['status'],
                    'readingAgeSeconds': max(0, now-oldest) if oldest is not None else None,
                    'counts': {table: store.rows(f'SELECT COUNT(*) FROM {table}')[0][0]
                               for table in ('samples', 'devices', 'activities', 'alert_rules', 'outbox')}}

    @router.get('/accounts/{user}/devices')
    def devices(user: AccountID, identity: Principal = Depends(admin)):
        grant_platforms = {'apple-ios': 'ios', 'apple-mac': 'mac', 'watch': 'watch'}
        topic_platforms = {settings.ios_topic: 'ios', settings.mac_topic: 'mac', settings.watch_topic: 'watch'}
        if settings.ios_topic == settings.mac_topic:
            topic_platforms[settings.ios_topic] = 'unknown'
        with selected(user, identity, 'devices'):
            rows = store.rows('''SELECT d.id,d.topic,d.environment,d.receive_alerts,d.created_at,d.registered_at,g.client
                                 FROM devices d
                                 LEFT JOIN registration_owners o
                                   ON o.kind='device' AND o.tenant_id=d.tenant_id AND o.id=d.id
                                 LEFT JOIN oauth_grants g ON g.id=o.grant_id AND g."user"=d.tenant_id
                                 ORDER BY d.registered_at DESC NULLS LAST,d.id''')
        return [{'id': row['id'], 'platform': grant_platforms.get(row['client'], topic_platforms.get(row['topic'], 'unknown')),
                 'topic': row['topic'],
                 'environment': row['environment'], 'receiveAlerts': bool(row['receive_alerts']),
                 'createdAt': row['created_at'], 'lastRegisteredAt': row['registered_at']} for row in rows]

    @router.get('/accounts/{user}/activities')
    def activities(user: AccountID, identity: Principal = Depends(admin)):
        with selected(user, identity, 'activities'):
            rows = store.rows('''SELECT id,environment,created_at,registered_at,expires,last_sent
                                 FROM activities ORDER BY expires DESC,id''')
        return [{'id': row['id'], 'environment': row['environment'], 'topic': settings.ios_topic+'.push-type.liveactivity',
                 'createdAt': row['created_at'], 'lastRegisteredAt': row['registered_at'],
                 'expiresAt': row['expires'], 'lastAcceptedAt': row['last_sent'] or None} for row in rows]

    @router.get('/accounts/{user}/widgets')
    def widgets(user: AccountID, identity: Principal = Depends(admin)):
        now = time.time()
        with selected(user, identity, 'widgets'):
            rows = store.rows('''SELECT id,generation,ams_ids,environment,bundle_id,created,updated,last_sent,
                                 last_status,last_reason,last_fingerprint,last_attempt,next_attempt
                                 FROM widget_installations WHERE deleted=0 ORDER BY updated DESC NULLS LAST,id''')
            readings = {row.amsID: row for row in store.readings()} if rows else {}
        result = []
        for row in rows:
            active = bool(json.loads(row['ams_ids']))
            blocked = row['last_status'] is not None and 400 <= row['last_status'] < 500 and row['last_status'] != 429
            pending = (active and not blocked
                       and display_fingerprint(row, readings, now, settings.stale_seconds) != row['last_fingerprint'])
            result.append({'id': row['id'], 'generation': row['generation'], 'active': active,
                           'environment': row['environment'], 'topic': row['bundle_id']+'.push-type.widgets',
                           'createdAt': row['created'], 'updatedAt': row['updated'],
                           'lastAcceptedAt': row['last_sent'] or None, 'lastStatus': row['last_status'],
                           'lastReason': apns_reason(row['last_reason']), 'pending': pending,
                           'nextAttemptAt': max(now, row['next_attempt'], row['last_attempt']+300) if pending else None})
        return result

    @router.get('/accounts/{user}/push-attempts')
    def attempts(user: AccountID, before: int = Query(default=MAX_ATTEMPT_ID, ge=1, le=MAX_ATTEMPT_ID),
                 limit: int = Query(default=50, ge=1, le=100), identity: Principal = Depends(admin)):
        with selected(user, identity, 'push-attempts'):
            rows = store.rows('''SELECT id,at,kind,destination,environment,topic,status,reason,apns_id
                                 FROM push_attempts WHERE at>=? AND id<? ORDER BY id DESC LIMIT ?''',
                              (time.time()-7*86400, before, limit+1))
        return {'attempts': [{**row, 'reason': apns_reason(row['reason']), 'apns_id': apns_request_id(row['apns_id'])}
                             for row in rows[:limit]], 'nextCursor': rows[limit-1]['id'] if len(rows) > limit else None}

    @router.get('/accounts/{user}/queue')
    def queue(user: AccountID, identity: Principal = Depends(admin)):
        with selected(user, identity, 'queue'):
            rows = store.rows('SELECT id,device,attempts,next,created FROM outbox ORDER BY next,id LIMIT 100')
        return [{'id': row['id'], 'deviceID': row['device'], 'attempts': row['attempts'],
                 'nextAttemptAt': row['next'], 'createdAt': row['created']} for row in rows]

    @router.post('/accounts/{user}/suspend', status_code=204)
    def suspend(user: AccountID, identity: Principal = Depends(admin)):
        with store.transaction():
            lock_admin(identity)
            changed = set_suspended(store, user, True)
        audit('suspend', 'changed' if changed else 'unchanged', actor=identity.user, target=user)

    @router.post('/accounts/{user}/reactivate', status_code=204)
    def reactivate(user: AccountID, identity: Principal = Depends(admin)):
        with store.transaction():
            lock_admin(identity)
            changed = set_suspended(store, user, False)
        audit('reactivate', 'changed' if changed else 'unchanged', actor=identity.user, target=user)

    def send_link(request, identity, kind, *, user=None, email=None):
        action = 'reset' if kind == 'reset' else 'invite'
        with store.transaction() as db:
            lock_admin(identity)
            if not accounts.configured:
                raise HTTPException(503, 'Email delivery is unavailable.')
            account = None
            if user is not None:
                account = member_account(store, user)
                if account['disabled']:
                    raise HTTPException(409, 'Reactivate this account before sending a reset link.')
                email = account['username']
            elif db.execute('SELECT 1 FROM accounts WHERE lower(username)=?', (email,)).fetchone():
                raise HTTPException(409, 'An account already uses that email address.')
            address = request.client.host if request.client else ''
            now = time.time()
            allowed = accounts.allow_email(db, canonical_email(email), address, now, actor=identity.user)
            if allowed:
                accounts.queue_link(db, kind, email, account, now)
        audit(action, 'queued' if allowed else 'rate_limited', actor=identity.user, target=user)
        if not allowed:
            raise HTTPException(429, 'Please wait before sending another account email.', headers={'Retry-After': '900'})
        return {'message': 'Password reset email queued.' if user is not None else 'Invitation email queued.'}

    @router.post('/accounts/{user}/reset', status_code=202)
    def reset(user: AccountID, request: Request, identity: Principal = Depends(admin)):
        return send_link(request, identity, 'reset', user=user)

    @router.post('/invitations', status_code=202)
    def invite(body: Invitation, request: Request, identity: Principal = Depends(admin)):
        return send_link(request, identity, 'signup', email=canonical_email(body.email))

    app.include_router(router)
