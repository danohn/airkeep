"""Local accounts and first-party OAuth authorization for the relay."""
from contextlib import nullcontext
from dataclasses import dataclass
import base64
import hashlib
from pathlib import Path
import re
import secrets
import time
import threading
from typing import Annotated, Literal
from urllib.parse import urlencode

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
import jwt
from pydantic import BaseModel, Field

DAY = 86400
BROWSER_SESSION_SECONDS = 8 * 3600
NATIVE_CALLBACK = 'io.airkeep.oauth:/callback'
PASSWORD = PasswordHasher()
USERNAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$')
PASSWORD_WORK = threading.Semaphore(2)
VERIFIER = re.compile(r'[A-Za-z0-9._~-]{43,128}')
# An empty hash marks an account that signs in through OIDC only.
LOCAL_ADMIN = "role='admin' AND disabled=0 AND temporary_until IS NULL AND hash<>''"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def random_token():
    return secrets.token_urlsafe(32)


def schema(store):
    if getattr(store, 'hosted', False):
        return
    with store.transaction():
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts(
                id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','member')),
                disabled INTEGER NOT NULL DEFAULT 0, temporary_until REAL, version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS oauth_requests(
                id TEXT PRIMARY KEY, client TEXT NOT NULL, redirect TEXT NOT NULL,
                state TEXT NOT NULL, challenge TEXT NOT NULL, installation TEXT,
                expires REAL NOT NULL, user TEXT, version INTEGER, code TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS oauth_grants(
                id TEXT PRIMARY KEY, user TEXT NOT NULL, client TEXT NOT NULL,
                installation TEXT, parent TEXT, expires REAL NOT NULL, idle_until REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS oauth_grants_user ON oauth_grants(user);
            CREATE INDEX IF NOT EXISTS oauth_grants_parent ON oauth_grants(parent);
            CREATE TABLE IF NOT EXISTS oauth_refresh(
                hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, spent INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS oauth_refresh_grant ON oauth_refresh(grant_id);
            CREATE TABLE IF NOT EXISTS oauth_attempts(bucket TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS oauth_attempts_bucket ON oauth_attempts(bucket, at);
            CREATE TABLE IF NOT EXISTS registration_owners(
                kind TEXT NOT NULL, id TEXT NOT NULL, grant_id TEXT NOT NULL, user TEXT NOT NULL,
                PRIMARY KEY(kind,id));
            CREATE INDEX IF NOT EXISTS registration_grant ON registration_owners(grant_id);
        """)
        store.db.executescript((Path(__file__).parent / 'migrations/006_account_security.sql').read_text())


def bootstrap(store, username, password_hash):
    schema(store)
    if not USERNAME.fullmatch(username) or not password_hash.startswith('$argon2id$'):
        raise ValueError('Use a valid username and an Argon2id password hash.')
    with store.transaction():
        db = store.db
        if db.execute('SELECT 1 FROM accounts LIMIT 1').fetchone():
            raise ValueError('Accounts already exist. Use the recovery command instead.')
        user = random_token()
        db.execute("INSERT INTO accounts(id,username,hash,role) VALUES(?,?,?,'admin')",
                   (user, username, password_hash))
        for table in ('alert_rules', 'alert_events'):
            db.execute(f'UPDATE {table} SET owner_id=? WHERE owner_id IS NULL', (user,))
        for table in ('web_sessions', 'outbox', 'devices', 'activities', 'widget_installations'):
            db.execute(f'DELETE FROM {table}')
        db.execute("INSERT INTO config VALUES('accounts_enabled','true')")


def tenant_context(store, user):
    return store.tenant(user) if getattr(store, 'hosted', False) else nullcontext()


def lock_account(store, user):
    suffix = ' FOR UPDATE' if getattr(store, 'hosted', False) else ''
    return store.db.execute('SELECT * FROM accounts WHERE id=?' + suffix, (user,)).fetchone()


def cleanup_registrations(store, grant_id):
    db = store.db
    db.execute("DELETE FROM outbox WHERE device IN (SELECT id FROM registration_owners WHERE kind='device' AND grant_id=?)", (grant_id,))
    for kind, table in (('device', 'devices'), ('activity', 'activities'), ('widget', 'widget_installations')):
        db.execute(f'DELETE FROM {table} WHERE id IN (SELECT id FROM registration_owners WHERE kind=? AND grant_id=?)', (kind, grant_id))
    db.execute('DELETE FROM registration_owners WHERE grant_id=?', (grant_id,))


def revoke_grant(store, grant_id):
    db = store.db
    for row in db.execute('SELECT id FROM oauth_grants WHERE id=? OR parent=?', (grant_id, grant_id)).fetchall():
        db.execute('UPDATE oauth_grants SET revoked=1 WHERE id=?', (row['id'],))
        owner = db.execute('SELECT user FROM oauth_grants WHERE id=?', (row['id'],)).fetchone()
        if owner:
            with tenant_context(store, owner['user']):
                cleanup_registrations(store, row['id'])


def revoke_user(store, user):
    if getattr(store, 'hosted', False):
        store.db.execute('UPDATE relay_credentials SET revoked=1 WHERE user=?', (user,))
    for row in store.db.execute('SELECT id FROM oauth_grants WHERE user=? AND revoked=0 AND parent IS NULL', (user,)).fetchall():
        revoke_grant(store, row['id'])


def recover(store, username, password_hash, *, clear_sign_in_methods=False):
    schema(store)
    with store.transaction():
        row = store.db.execute("SELECT id FROM accounts WHERE username=? AND role='admin'", (username,)).fetchone()
        if not row:
            raise ValueError('No administrator has that username.')
        store.db.execute('UPDATE accounts SET hash=?,disabled=0,temporary_until=NULL,version=version+1 WHERE id=?',
                         (password_hash, row['id']))
        if clear_sign_in_methods:
            for table in ('account_passkeys', 'account_totp', 'account_recovery_codes', 'account_auth_challenges'):
                store.db.execute(f'DELETE FROM {table} WHERE account_id=?', (row['id'],))
        revoke_user(store, row['id'])


@dataclass(frozen=True)
class Principal:
    user: str
    role: str
    client: str
    grant: str
    installation: str | None


class Authorization(BaseModel):
    client_id: Literal['dashboard', 'apple-ios', 'apple-mac']
    redirect_uri: str = Field(max_length=500)
    state: str = Field(pattern=r'^[A-Za-z0-9_-]{32,128}$')
    code_challenge: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$')
    code_challenge_method: Literal['S256']
    response_type: Literal['code']
    installation_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,128}$')


VerificationCode = Annotated[str, Field(min_length=6, max_length=40)]


class Credentials(BaseModel):
    transaction: str = Field(max_length=100)
    username: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    new_password: str | None = Field(default=None, min_length=12, max_length=1024)
    verification_code: VerificationCode | None = None


class NewAccount(BaseModel):
    username: str = Field(pattern=USERNAME.pattern)
    role: Literal['admin', 'member'] = 'member'


class EditAccount(BaseModel):
    role: Literal['admin', 'member'] | None = None
    disabled: bool | None = None


class PasswordChange(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)
    verification_code: VerificationCode | None = None


class WatchPair(BaseModel):
    installation_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


class OAuth:
    def __init__(self, store, settings):
        from account_security import AccountSecurity

        self.store = store
        self.origin = settings.web_origin.rstrip('/')
        self.key = settings.jwt_signing_key
        self.secure_cookie = settings.web_secure_cookies and self.origin.startswith('https://')
        self.cookie_name = '__Host-airkeep_session' if self.secure_cookie else 'airkeep_account_session'
        schema(store)
        self.dummy_hash = PASSWORD.hash(random_token())
        self.factors = AccountSecurity(self)

    def active(self, grant_id):
        now = time.time()
        rows = self.store.rows("""SELECT g.*, a.role FROM oauth_grants g JOIN accounts a ON a.id=g.user
            WHERE g.id=? AND g.revoked=0 AND a.disabled=0 AND g.expires>? AND g.idle_until>?
              AND (g.parent IS NULL OR EXISTS (SELECT 1 FROM oauth_grants p
                WHERE p.id=g.parent AND p.revoked=0 AND p.expires>? AND p.idle_until>?))""",
            (grant_id, now, now, now, now))
        return rows[0] if rows else None

    def authenticate(self, token, *, browser=False):
        try:
            claims = jwt.decode(token, self.key, algorithms=['HS256'], issuer=self.origin,
                                audience='airkeep-web' if browser else 'airkeep-api',
                                options={'require': ['sub','sid','exp','iat','iss','aud','jti','client_id']})
            if jwt.get_unverified_header(token).get('typ') != 'at+jwt' or not isinstance(claims['sid'], str):
                raise jwt.InvalidTokenError()
            row = self.active(claims['sid'])
            if (row and (row['client'] == 'dashboard') == browser
                    and row['user'] == claims['sub'] and row['client'] == claims['client_id']):
                return Principal(row['user'],row['role'],row['client'],row['id'],row['installation'])
        except (jwt.InvalidTokenError, TypeError, ValueError):
            pass
        raise HTTPException(401, 'Sign in to Airkeep.', headers={'WWW-Authenticate':'Bearer'})

    def browser_session(self, request):
        token = request.cookies.get(self.cookie_name, '')
        if not token:
            raise HTTPException(401, 'Sign in to Airkeep.')
        identity = self.authenticate(token, browser=True)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            self.same_origin(request)
        expected = request.headers.get('x-airkeep-account')
        if expected is not None and not secrets.compare_digest(expected.encode(), identity.user.encode()):
            raise HTTPException(409, 'Your signed-in account changed. Reload Airkeep.',
                                headers={'X-Airkeep-Account-Changed': '1'})
        return identity

    def set_browser_cookie(self, response, token):
        response.set_cookie(self.cookie_name, token, max_age=BROWSER_SESSION_SECONDS,
                            httponly=True, secure=self.secure_cookie, samesite='strict', path='/')

    def clear_browser_cookie(self, response):
        response.delete_cookie(self.cookie_name, httponly=True, secure=self.secure_cookie,
                               samesite='strict', path='/')

    def same_origin(self, request):
        if request.headers.get('origin') != self.origin:
            raise HTTPException(403, 'Open Airkeep at its configured address.')

    def rate_limit(self, request, username, *, scope='login'):
        now = time.time()
        address = request.client.host if request.client else ''
        if getattr(self.store, 'hosted', False):
            from hosted_accounts import login_limit
            return login_limit(self.store, self.key, username, address, scope=scope)
        prefix = '' if scope == 'login' else scope + ':'
        keys = [prefix + 'account:' + username.lower(), prefix + 'ip:' + address]
        with self.store.transaction():
            db = self.store.db
            db.execute('DELETE FROM oauth_attempts WHERE at<?', (now-300,))
            if any(db.execute('SELECT COUNT(*) FROM oauth_attempts WHERE bucket=?', (key,)).fetchone()[0] >= limit
                   for key, limit in zip(keys, (5, 30))):
                raise HTTPException(429, 'Please wait before trying again.', headers={'Retry-After':'300'})
            db.executemany('INSERT INTO oauth_attempts VALUES(?,?)', ((key,now) for key in keys))

    def verify(self, password, password_hash):
        try:
            with PASSWORD_WORK:
                verified = PASSWORD.verify(password_hash or self.dummy_hash, password)
            return bool(password_hash) and verified
        except (InvalidHashError, VerificationError):
            return False

    def begin(self, body):
        callback = self.origin + '/auth/callback' if body.client_id == 'dashboard' else NATIVE_CALLBACK
        if body.redirect_uri != callback or bool(body.installation_id) != (body.client_id != 'dashboard'):
            raise HTTPException(400, 'Invalid application callback.')
        identifier = random_token()
        self.store.execute("""INSERT INTO oauth_requests(id,client,redirect,state,challenge,installation,expires)
            VALUES(?,?,?,?,?,?,?)""", (digest(identifier),body.client_id,callback,body.state,
                                     body.code_challenge,body.installation_id,time.time()+300))
        return identifier

    def login(self, body, request):
        if getattr(self.store, 'hosted', False):
            from hosted_accounts import canonical_email
            body = body.model_copy(update={'username': canonical_email(body.username, strict=False)})
        elif len(body.username) > 64:
            raise HTTPException(422, 'Check your username and password.')
        self.rate_limit(request, body.username)
        if not self.store.rows('SELECT 1 FROM oauth_requests WHERE id=? AND code IS NULL AND expires>?',
                               (digest(body.transaction),time.time())):
            raise HTTPException(400, 'Start sign-in again.')
        row = self.store.rows('SELECT * FROM accounts WHERE username=?', (body.username,))
        user = row[0] if row else None
        verified = self.verify(body.password, user['hash'] if user else None)
        if not verified or not user or user['disabled'] or (
            user['temporary_until'] is not None and user['temporary_until'] <= time.time()
        ):
            raise HTTPException(401, 'Check your username and password.')
        if user['temporary_until'] is not None and body.new_password == body.password:
            raise HTTPException(422, 'Choose a password different from the temporary password.')
        with PASSWORD_WORK:
            replacement = PASSWORD.hash(body.new_password) if user['temporary_until'] is not None and body.new_password else None
        with self.store.transaction():
            db = self.store.db
            tx = db.execute('SELECT * FROM oauth_requests WHERE id=? AND code IS NULL AND expires>?',
                            (digest(body.transaction),time.time())).fetchone()
            current = lock_account(self.store, user['id'])
            if not tx or not current or current['disabled'] or current['version'] != user['version']:
                raise HTTPException(400, 'Start sign-in again.')
            if current['temporary_until'] is not None:
                if current['temporary_until'] <= time.time():
                    raise HTTPException(401, 'Check your username and password.')
                if replacement is None:
                    return {'passwordChangeRequired': True}
            if body.verification_code is None and self.factors.totp_enabled(db, user['id']):
                return {'totpRequired': True}
            self.factors.verify_totp(db, user['id'], body.verification_code)
            version = user['version']
            if replacement is not None:
                version += 1
                db.execute('UPDATE accounts SET hash=?,temporary_until=NULL,version=? WHERE id=?',
                           (replacement,version,user['id']))
                revoke_user(self.store,user['id'])
            if not getattr(self.store, 'hosted', False):
                db.execute('DELETE FROM oauth_attempts WHERE bucket=?', ('account:'+user['username'].lower(),))
            return {'redirect': self.complete(db, tx, user['id'], version)}

    def complete(self, db, transaction, user_id, version):
        code = random_token()
        db.execute('UPDATE oauth_requests SET user=?,version=?,code=?,expires=? WHERE id=?',
                   (user_id,version,digest(code),time.time()+60,transaction['id']))
        return transaction['redirect']+'?'+urlencode({'code':code,'state':transaction['state']})

    def grant(self, user, client, installation, parent=None):
        account = lock_account(self.store, user)
        if not account or account['disabled']:
            raise HTTPException(401, 'Sign in to Airkeep.')
        now = time.time()
        identifier = random_token()
        expires = now + (BROWSER_SESSION_SECONDS if client == 'dashboard' else 90*DAY)
        if parent:
            expires = min(expires, parent['expires'])
        if installation:
            for old in self.store.db.execute('SELECT id FROM oauth_grants WHERE client=? AND installation=? AND user=? AND revoked=0',
                                             (client,installation,user)).fetchall():
                revoke_grant(self.store,old['id'])
        self.store.db.execute('INSERT INTO oauth_grants VALUES(?,?,?,?,?,?,?,0)',
                              (identifier,user,client,installation,parent['id'] if parent else None,
                               expires,min(expires,now+30*DAY)))
        return self.tokens(self.active(identifier))

    def tokens(self, row):
        now = int(time.time())
        browser = row['client'] == 'dashboard'
        lifetime = min(int(row['expires'])-now, int(row['idle_until'])-now)
        if not browser:
            lifetime = min(600, lifetime)
        token = jwt.encode({'iss':self.origin,'aud':'airkeep-web' if browser else 'airkeep-api','sub':row['user'],'sid':row['id'],
                            'client_id':row['client'],'jti':random_token(),'iat':now,'exp':now+lifetime},
                           self.key,algorithm='HS256',headers={'typ':'at+jwt'})
        if browser:
            return {'access_token': token}
        refresh = random_token()
        self.store.db.execute('INSERT INTO oauth_refresh(hash,grant_id) VALUES(?,?)',(digest(refresh),row['id']))
        return {'access_token':token,'refresh_token':refresh,'token_type':'Bearer','expires_in':lifetime,
                'grant_id':row['id']}

    def exchange(self, data):
        kind, client = data.get('grant_type'), data.get('client_id')
        with self.store.transaction():
            db = self.store.db
            if kind == 'authorization_code':
                row = db.execute("""SELECT r.* FROM oauth_requests r JOIN accounts a ON a.id=r.user
                    WHERE r.code=? AND r.expires>? AND a.disabled=0 AND a.version=r.version""",
                    (digest(data.get('code','')),time.time())).fetchone()
                if row:
                    lock_account(self.store, row['user'])
                    row = db.execute("""SELECT r.* FROM oauth_requests r JOIN accounts a ON a.id=r.user
                        WHERE r.code=? AND r.expires>? AND a.disabled=0 AND a.version=r.version""",
                        (digest(data.get('code','')),time.time())).fetchone()
                verifier = data.get('code_verifier','')
                if not row or client != row['client'] or data.get('redirect_uri') != row['redirect'] or not VERIFIER.fullmatch(verifier):
                    return None
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
                if not secrets.compare_digest(challenge,row['challenge']):
                    return None
                db.execute('DELETE FROM oauth_requests WHERE id=?',(row['id'],))
                return self.grant(row['user'],client,row['installation'])
            if kind == 'refresh_token':
                item = db.execute('SELECT * FROM oauth_refresh WHERE hash=?',(digest(data.get('refresh_token','')),)).fetchone()
                grant = self.active(item['grant_id']) if item else None
                if grant:
                    lock_account(self.store, grant['user'])
                    item = db.execute('SELECT * FROM oauth_refresh WHERE hash=?', (digest(data.get('refresh_token','')),)).fetchone()
                row = self.active(item['grant_id']) if item else None
                if not row or row['client'] != client or client == 'dashboard':
                    return None
                if item['spent']:
                    revoke_grant(self.store,row['id'])
                    return None
                db.execute('UPDATE oauth_refresh SET spent=1 WHERE hash=?',(item['hash'],))
                db.execute('UPDATE oauth_grants SET idle_until=MIN(expires,?) WHERE id=?',(time.time()+30*DAY,row['id']))
                return self.tokens(self.active(row['id']))
        return None

    def maintain(self, now):
        with self.store.transaction():
            db = self.store.db
            for row in db.execute('SELECT id FROM oauth_grants WHERE revoked=0 AND (expires<=? OR idle_until<=?)', (now,now)).fetchall():
                revoke_grant(self.store,row['id'])
            db.execute('DELETE FROM oauth_requests WHERE expires<=?',(now,))
            db.execute('DELETE FROM account_auth_challenges WHERE expires<=?', (now,))
            db.execute('DELETE FROM oauth_refresh WHERE grant_id IN (SELECT id FROM oauth_grants WHERE expires<=? OR revoked=1)',(now,))
            db.execute('DELETE FROM oauth_grants WHERE expires<=? OR revoked=1',(now,))


def install_accounts(app, oauth, principal, admin, connection):
    from account_security import install_security
    from device_flow import GRANT, form_data, install_device_flow, oauth_error

    store = oauth.store
    devices = install_device_flow(app, oauth, principal)
    install_security(app, oauth, principal)

    @app.get('/auth/authorize')
    def authorize_redirect(body: Annotated[Authorization, Query()]):
        identifier = oauth.begin(body)
        return RedirectResponse('/connect?'+urlencode({'transaction':identifier}),status_code=303)

    @app.post('/auth/authorize')
    def authorize(body: Authorization, request: Request):
        oauth.same_origin(request)
        return {'transaction':oauth.begin(body)}

    @app.post('/auth/credentials')
    def credentials(body: Credentials, request: Request):
        oauth.same_origin(request)
        return oauth.login(body,request)

    @app.post('/auth/token')
    async def token(request: Request):
        from starlette.concurrency import run_in_threadpool
        data = await form_data(request)
        if data is None:
            return oauth_error('invalid_request')
        if data.get('client_id') == 'dashboard' or 'origin' in request.headers:
            oauth.same_origin(request)
        if data.get('grant_type') == GRANT:
            return await run_in_threadpool(devices.exchange, data)
        result = await run_in_threadpool(oauth.exchange, data)
        if result and data.get('client_id') == 'dashboard':
            response = JSONResponse({'authenticated': True})
            oauth.set_browser_cookie(response, result['access_token'])
            return response
        return result if result else oauth_error('invalid_grant')

    @app.get('/auth/me')
    def me(identity: Principal = Depends(principal)):
        row = store.rows('SELECT id,username,role,hash FROM accounts WHERE id=?',(identity.user,))[0]
        return account_details(row)

    @app.post('/auth/revoke', status_code=204)
    def revoke(response: Response, identity: Principal = Depends(connection)):
        with store.transaction():
            revoke_grant(store,identity.grant)
        if identity.client == 'dashboard':
            oauth.clear_browser_cookie(response)

    @app.get('/auth/grants')
    def grants(identity: Principal = Depends(principal)):
        if identity.client != 'dashboard':
            raise HTTPException(403,'Manage connections in the dashboard.')
        now = time.time()
        rows = store.rows("""SELECT id,client,expires,CASE WHEN client='esp32' THEN substr(installation,length(installation)-3) END AS display_suffix FROM oauth_grants
            WHERE user=? AND revoked=0 AND expires>? AND idle_until>?
            ORDER BY client,id""",(identity.user,now,now))
        return [{**dict(row),'current':row['id']==identity.grant} for row in rows]

    @app.delete('/auth/grants/{grant_id}', status_code=204)
    def revoke_connection(grant_id: str, identity: Principal = Depends(principal)):
        if identity.client != 'dashboard':
            raise HTTPException(403,'Manage connections in the dashboard.')
        with store.transaction() as db:
            if not oauth.active(identity.grant):
                raise HTTPException(401,'Sign in to Airkeep.')
            if not db.execute('SELECT 1 FROM oauth_grants WHERE id=? AND user=?',(grant_id,identity.user)).fetchone():
                raise HTTPException(404,'Connection not found.')
            revoke_grant(store,grant_id)

    @app.post('/auth/watch')
    def watch(body: WatchPair, identity: Principal = Depends(principal)):
        if identity.client != 'apple-ios':
            raise HTTPException(403,'Pair Apple Watch from your iPhone.')
        with store.transaction():
            lock_account(store, identity.user)
            parent = oauth.active(identity.grant)
            if parent is None:
                raise HTTPException(401,'Sign in to Airkeep.')
            for old in store.db.execute('SELECT id FROM oauth_grants WHERE parent=? AND revoked=0',(identity.grant,)).fetchall():
                revoke_grant(store,old['id'])
            return oauth.grant(identity.user,'watch',body.installation_id,parent)

    @app.delete('/auth/watch', status_code=204)
    def unpair(identity: Principal = Depends(principal)):
        if identity.client != 'apple-ios':
            raise HTTPException(403,'Manage Apple Watch from your iPhone.')
        with store.transaction():
            for row in store.db.execute('SELECT id FROM oauth_grants WHERE parent=?',(identity.grant,)).fetchall():
                revoke_grant(store,row['id'])

    @app.post('/auth/password', status_code=204)
    def password(body: PasswordChange, request: Request, response: Response, identity: Principal = Depends(principal)):
        if identity.client != 'dashboard':
            raise HTTPException(403,'Change your password in the dashboard.')
        oauth.rate_limit(request,identity.user)
        users = store.rows('SELECT hash,version FROM accounts WHERE id=?',(identity.user,))
        user = users[0] if users else None
        if not oauth.verify(body.password,user['hash'] if user else ''):
            raise HTTPException(401,'Check your current password.')
        with PASSWORD_WORK:
            replacement = PASSWORD.hash(body.new_password)
        with store.transaction():
            current = lock_account(store, identity.user)
            if not current or current['version'] != user['version'] or not oauth.active(identity.grant):
                raise HTTPException(409, 'Your account changed. Sign in again.')
            oauth.factors.verify_totp(store.db, identity.user, body.verification_code)
            changed = store.db.execute('UPDATE accounts SET hash=?,version=version+1 WHERE id=? AND version=? AND disabled=0',
                                       (replacement,identity.user,user['version']))
            if not changed.rowcount:
                raise HTTPException(409,'Your account changed. Sign in again.')
            revoke_user(store,identity.user)
            if getattr(store, 'hosted', False):
                from hosted_accounts import cancel_links
                cancel_links(store.db, identity.user)
        oauth.clear_browser_cookie(response)

    def public_accounts():
        return [account_details(row) for row in store.rows(
            'SELECT id,username,role,disabled,temporary_until,hash FROM accounts ORDER BY username')]

    @app.get('/v2/admin/accounts', dependencies=[Depends(admin)])
    def accounts():
        return public_accounts()

    @app.post('/v2/admin/accounts', status_code=201)
    def create(body: NewAccount, identity: Principal = Depends(admin)):
        temporary = random_token()
        password_hash = PASSWORD.hash(temporary)
        identifier = random_token()
        with store.transaction():
            actor = oauth.active(identity.grant)
            if not actor or actor['role'] != 'admin':
                raise HTTPException(403,'Administrator access required.')
            if store.rows('SELECT 1 FROM accounts WHERE username=?',(body.username,)):
                raise HTTPException(409,'That username is already in use.')
            store.db.execute('INSERT INTO accounts(id,username,hash,role,temporary_until) VALUES(?,?,?,?,?)',
                             (identifier,body.username,password_hash,body.role,time.time()+DAY))
        return {'id':identifier,'temporaryPassword':temporary}

    @app.patch('/v2/admin/accounts/{user}', status_code=204)
    def edit(user: str, body: EditAccount, identity: Principal = Depends(admin)):
        with store.transaction():
            db = store.db
            actor = oauth.active(identity.grant)
            if not actor or actor['role'] != 'admin':
                raise HTTPException(403,'Administrator access required.')
            account = db.execute('SELECT * FROM accounts WHERE id=?',(user,)).fetchone()
            if not account:
                raise HTTPException(404,'Account not found.')
            role = body.role if body.role is not None else account['role']
            disabled = body.disabled if body.disabled is not None else account['disabled']
            if role != 'admin' or disabled:
                protect_local_admin(db, user)
            if role != account['role'] or disabled != account['disabled']:
                db.execute('UPDATE accounts SET role=?,disabled=?,version=version+1 WHERE id=?',(role,int(disabled),user))
                revoke_user(store,user)
                if disabled:
                    db.execute('DELETE FROM outbox WHERE rule_id IN (SELECT id FROM alert_rules WHERE owner_id=?)',(user,))
                    db.execute("UPDATE alert_rules SET state=json_set(state,'$.since',NULL) WHERE owner_id=?",(user,))

    @app.post('/v2/admin/accounts/{user}/reset')
    def reset(user: str, identity: Principal = Depends(admin)):
        temporary = random_token()
        password_hash = PASSWORD.hash(temporary)
        with store.transaction():
            actor = oauth.active(identity.grant)
            if not actor or actor['role'] != 'admin':
                raise HTTPException(403,'Administrator access required.')
            protect_local_admin(store.db, user)
            row = store.db.execute("UPDATE accounts SET hash=?,temporary_until=?,version=version+1 WHERE id=? AND hash<>''",
                                   (password_hash,time.time()+DAY,user))
            if not row.rowcount:
                raise HTTPException(404,'Account not found.')
            revoke_user(store,user)
        return {'temporaryPassword':temporary}


def protect_local_admin(db, user):
    if (db.execute(f'SELECT 1 FROM accounts WHERE {LOCAL_ADMIN} AND id=?', (user,)).fetchone()
            and not db.execute(f'SELECT 1 FROM accounts WHERE {LOCAL_ADMIN} AND id<>?', (user,)).fetchone()):
        raise HTTPException(409, 'Keep at least one active administrator with a local password.')


def account_details(row):
    account = dict(row)
    account['local_password'] = bool(account.pop('hash'))
    return account
