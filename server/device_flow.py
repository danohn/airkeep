"""OAuth device authorization for read-only Airkeep displays."""
import re
import secrets
import time
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from accounts import Principal, digest, random_token

CLIENT = 'esp32'
RELAY_CLIENT = 'airkeep-relay'
GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
LIFETIME = 600
INTERVAL = 5
INSTALLATION = re.compile(r'[a-f0-9]{32}')


class Verification(BaseModel):
    user_code: str = Field(max_length=32)


class Decision(Verification):
    approve: bool


async def form_data(request):
    from urllib.parse import parse_qs
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            return None
    if not request.headers.get('content-type', '').startswith('application/x-www-form-urlencoded'):
        return None
    try:
        form = parse_qs(body.decode(), strict_parsing=True, max_num_fields=8)
    except (UnicodeError, ValueError):
        return None
    if any(len(values) != 1 for values in form.values()):
        return None
    return {key: values[0] for key, values in form.items()}


def oauth_error(error):
    return JSONResponse({'error': error}, status_code=400)


class DeviceFlow:
    def __init__(self, oauth):
        self.oauth = oauth
        self.store = oauth.store
        if getattr(self.store, 'hosted', False):
            return
        with self.store.lock, self.store.db:
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_device_requests(
                    device_hash TEXT PRIMARY KEY, user_hash TEXT NOT NULL UNIQUE,
                    installation TEXT NOT NULL, expires REAL NOT NULL,
                    next_poll REAL NOT NULL, poll_interval INTEGER NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'pending', owner TEXT, owner_version INTEGER);
            """)

    @staticmethod
    def limit(db, buckets, now):
        db.execute('DELETE FROM oauth_attempts WHERE at<=?', (now - 300,))
        for bucket, limit in buckets:
            if db.execute('SELECT COUNT(*) FROM oauth_attempts WHERE bucket=?', (bucket,)).fetchone()[0] >= limit:
                raise HTTPException(429, 'Too many pairing attempts. Try again in five minutes.',
                                    headers={'Retry-After': '300'})
        db.executemany('INSERT INTO oauth_attempts VALUES(?,?)', ((bucket, now) for bucket, _ in buckets))

    def start(self, installation, address, client=CLIENT):
        now = time.time()
        device = random_token()
        with self.store.transaction() as db:
            self.limit(db, [('display-start:' + address, 20), ('display-start:all', 200)], now)
            db.execute('DELETE FROM oauth_device_requests WHERE expires<=?', (now,))
            while True:
                short = ''.join(secrets.choice(ALPHABET) for _ in range(8))
                if not db.execute('SELECT 1 FROM oauth_device_requests WHERE user_hash=?', (digest(short),)).fetchone():
                    break
            db.execute('''INSERT INTO oauth_device_requests
                (device_hash,user_hash,installation,expires,next_poll,poll_interval)
                VALUES(?,?,?,?,?,?)''',
                (digest(device), digest(short), installation, now + LIFETIME, now + INTERVAL, INTERVAL))
            if getattr(self.store, 'hosted', False):
                db.execute('UPDATE oauth_device_requests SET client=? WHERE device_hash=?', (client, digest(device)))
        code = short[:4] + '-' + short[4:]
        uri = self.oauth.origin + '/device'
        return {'device_code': device, 'user_code': code, 'verification_uri': uri,
                'verification_uri_complete': uri + '#' + urlencode({'user_code': code}),
                'expires_in': LIFETIME, 'interval': INTERVAL}

    def review(self, body, identity, address, approve=None):
        now = time.time()
        code = re.sub(r'[\s-]', '', body.user_code).upper()
        with self.store.transaction() as db:
            actor = self.oauth.active(identity.grant)
            if not actor:
                raise HTTPException(401, 'Sign in to Airkeep.')
            self.limit(db, [('display-check:ip:' + address, 40), ('display-check:user:' + identity.user, 20)], now)
            row = db.execute('''SELECT * FROM oauth_device_requests
                WHERE user_hash=? AND expires>? AND decision='pending' ''', (digest(code), now)).fetchone()
            if row and approve is not None:
                version = db.execute('SELECT version FROM accounts WHERE id=?', (identity.user,)).fetchone()['version']
                db.execute('''UPDATE oauth_device_requests SET decision=?,owner=?,owner_version=?
                    WHERE user_hash=?''', ('approved' if approve else 'denied', identity.user, version, digest(code)))
        # Failed lookups still count against the limit.
        if not row:
            raise HTTPException(404, 'This pairing code is invalid or has expired.')
        client = row['client'] if getattr(self.store, 'hosted', False) else CLIENT
        label = 'LAN relay' if client == RELAY_CLIENT else 'ESP32 display'
        return {'user_code': code[:4] + '-' + code[4:], 'display': label + ' · ' + row['installation'][-4:],
                'client_id': client, 'expires_at': row['expires']}

    def exchange(self, data):
        client = data.get('client_id')
        allowed = (CLIENT, RELAY_CLIENT) if getattr(self.store, 'hosted', False) else (CLIENT,)
        if client not in allowed or not data.get('device_code'):
            return oauth_error('invalid_grant')
        now = time.time()
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM oauth_device_requests WHERE device_hash=?',
                             (digest(data['device_code']),)).fetchone()
            if not row or row['decision'] == 'used':
                return oauth_error('invalid_grant')
            if getattr(self.store, 'hosted', False) and row['client'] != client:
                return oauth_error('invalid_grant')
            if row['expires'] <= now:
                return oauth_error('expired_token')
            if row['decision'] == 'denied':
                return oauth_error('access_denied')
            if now < row['next_poll']:
                interval = row['poll_interval'] + 5
                db.execute('UPDATE oauth_device_requests SET next_poll=?,poll_interval=? WHERE device_hash=?',
                           (now + interval, interval, row['device_hash']))
                return oauth_error('slow_down')
            if row['decision'] == 'pending':
                db.execute('UPDATE oauth_device_requests SET next_poll=? WHERE device_hash=?',
                           (now + row['poll_interval'], row['device_hash']))
                return oauth_error('authorization_pending')
            owner = db.execute('SELECT version FROM accounts WHERE id=? AND disabled=0', (row['owner'],)).fetchone()
            db.execute("UPDATE oauth_device_requests SET decision='used' WHERE device_hash=?", (row['device_hash'],))
            if not owner or owner['version'] != row['owner_version']:
                return oauth_error('access_denied')
            if client == RELAY_CLIENT:
                from hosted_ingest import enroll_relay
                return enroll_relay(self.store, row['owner'])
            if getattr(self.store, 'hosted', False):
                with self.store.tenant(row['owner']):
                    return self.oauth.grant(row['owner'], CLIENT, row['installation'])
            return self.oauth.grant(row['owner'], CLIENT, row['installation'])


def install_device_flow(app, oauth, principal):
    from starlette.concurrency import run_in_threadpool
    flow = DeviceFlow(oauth)

    def address(request):
        return request.client.host if request.client else 'unknown'

    def check_dashboard(request, identity):
        oauth.same_origin(request)
        if identity.client != 'dashboard':
            raise HTTPException(403, 'Pair displays in the dashboard.')

    @app.post('/auth/device/code')
    async def device_code(request: Request):
        if 'origin' in request.headers:
            oauth.same_origin(request)
        data = await form_data(request)
        allowed = (CLIENT, RELAY_CLIENT) if getattr(oauth.store, 'hosted', False) else (CLIENT,)
        if not data or data.get('client_id') not in allowed or not INSTALLATION.fullmatch(data.get('installation_id', '')):
            return oauth_error('invalid_request')
        if not oauth.origin.startswith('https://'):
            return oauth_error('invalid_request')
        return await run_in_threadpool(flow.start, data['installation_id'], address(request), data['client_id'])

    @app.post('/auth/device/verify')
    def verify(body: Verification, request: Request, identity: Principal = Depends(principal)):
        check_dashboard(request, identity)
        return flow.review(body, identity, address(request))

    @app.post('/auth/device/decision', status_code=204)
    def decide(body: Decision, request: Request, identity: Principal = Depends(principal)):
        check_dashboard(request, identity)
        flow.review(body, identity, address(request), body.approve)

    return flow
