"""Verified-email accounts and a durable Twilio Email outbox for the hosted service."""
import hashlib
import hmac
import html
import re
import time
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, Response
import httpx
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.concurrency import run_in_threadpool

from accounts import DAY, PASSWORD, PASSWORD_WORK, Principal, digest, lock_account, random_token, revoke_user

EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}")
GENERIC = {'message': 'If this email address can receive an Airkeep link, check your inbox shortly.'}
TWILIO_EMAIL = 'https://comms.twilio.com/v1/Emails'


class EmailSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='AIRKEEP_', env_file='.env', extra='ignore', hide_input_in_errors=True)
    twilio_api_key: SecretStr = SecretStr('')
    twilio_api_secret: SecretStr = SecretStr('')
    email_from: str = ''
    email_from_name: str = 'Airkeep'
    email_link_key: SecretStr = SecretStr('')


class EmailAddress(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class CompleteLink(BaseModel):
    token: str = Field(min_length=32, max_length=128)
    password: str = Field(min_length=12, max_length=1024)


class DeleteAccount(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    confirmation: str = Field(max_length=50)


def canonical_email(value, *, strict=True):
    address = value.strip().lower()
    try:
        local, domain = address.rsplit('@', 1)
        address = local + '@' + domain.encode('idna').decode('ascii')
    except (ValueError, UnicodeError):
        pass
    valid = (len(address) <= 254 and EMAIL.fullmatch(address)
             and '..' not in address and len(address.split('@')[0]) <= 64)
    if strict and not valid:
        raise HTTPException(422, 'Enter a valid email address.')
    return address


def limited(db, key, scope, value, capacity, window, now):
    # Fixed-window HMAC keys retain neither email addresses nor IP addresses.
    subject = f'{scope}:{int(now // window)}:{value}'
    bucket = hmac.new(key.encode(), subject.encode(), hashlib.sha256).hexdigest()
    row = db.execute('''INSERT INTO hosted_account_limits(bucket,count,expires) VALUES(?,1,?)
        ON CONFLICT(bucket) DO UPDATE SET count=hosted_account_limits.count+1
        WHERE hosted_account_limits.count<? RETURNING count''',
        (bucket, (int(now // window)+1)*window, capacity)).fetchone()
    return row is not None


def login_limit(store, key, username, address):
    now = time.time()
    with store.transaction() as db:
        ip_ok = limited(db, key, 'login-ip', address, 30, 300, now)
        user_ok = limited(db, key, 'login-account', username.lower(), 5, 300, now)
    if not ip_ok or not user_ok:
        raise HTTPException(429, 'Please wait before trying again.', headers={'Retry-After': '300'})


def cancel_links(db, user):
    db.execute('''UPDATE hosted_email_outbox SET sealed_secret=NULL,state='canceled',lease=NULL,lease_until=0
        WHERE link_hash IN (SELECT hash FROM hosted_account_links WHERE account_id=?)''', (user,))
    db.execute('DELETE FROM hosted_account_links WHERE account_id=?', (user,))


class HostedAccounts:
    def __init__(self, oauth, settings, email_settings=None):
        self.oauth, self.store = oauth, oauth.store
        self.signup_open = getattr(settings, 'public_signup', False)
        self.origin = settings.web_origin.rstrip('/')
        self.settings = email_settings or EmailSettings()
        if not self.origin.startswith('https://'):
            raise ValueError('Hosted account links require an HTTPS web origin.')
        self.configured = bool(self.settings.twilio_api_key.get_secret_value() and
                               self.settings.twilio_api_secret.get_secret_value() and self.settings.email_from)
        if self.signup_open and not self.configured:
            raise ValueError('Public signup requires Twilio Email credentials and a verified sender.')
        self.cipher = Fernet(self.settings.email_link_key.get_secret_value().encode())
        self.client = None

    def request_link(self, body, request, kind):
        self.oauth.same_origin(request)
        email = canonical_email(body.email)
        if not self.configured or (kind == 'signup' and not self.signup_open):
            return GENERIC
        now = time.time()
        address = request.client.host if request.client else ''
        with self.store.transaction() as db:
            if not self.allow_email(db, email, address, now):
                return GENERIC
            row = db.execute('SELECT * FROM accounts WHERE username=?', (email,)).fetchone()
            if (kind == 'signup' and row) or (kind == 'reset' and (not row or row['disabled'])):
                return GENERIC
            if row:
                row = lock_account(self.store, row['id'])
                if not row or row['disabled']:
                    return GENERIC
            self.queue_link(db, kind, email, row, now)
        return GENERIC

    def allow_email(self, db, email, address, now, *, actor=None):
        key = self.oauth.key
        if actor and not limited(db, key, 'email-admin', actor, 20, 900, now):
            return False
        ip_ok = limited(db, key, 'email-ip', address, 30, 900, now)
        email_ok = limited(db, key, 'email-account', email, 4, 900, now)
        global_ok = limited(db, key, 'email-global', '', 600, 900, now)
        return ip_ok and email_ok and global_ok

    def queue_link(self, db, kind, email, account, now):
        token = random_token()
        link_hash = digest(token)
        expires = now + (3600 if kind == 'signup' else 1200)
        db.execute('''INSERT INTO hosted_account_links
            (hash,kind,email,account_id,account_version,created,expires) VALUES(?,?,?,?,?,?,?)''',
            (link_hash, kind, email, account['id'] if account else None,
             account['version'] if account else None, now, expires))
        db.execute('''INSERT INTO hosted_email_outbox
            (id,link_hash,sealed_secret,next_attempt,created,updated) VALUES(?,?,?,?,?,?)''',
            (random_token(), link_hash, self.cipher.encrypt(token.encode()).decode(), now, now, now))

    def complete(self, body, request, kind):
        self.oauth.same_origin(request)
        address = request.client.host if request.client else ''
        with self.store.transaction() as db:
            allowed = limited(db, self.oauth.key, 'complete-ip', address, 20, 300, time.time())
        if not allowed:
            raise HTTPException(429, 'Please wait before trying again.', headers={'Retry-After': '300'})
        link = self.store.rows('SELECT * FROM hosted_account_links WHERE hash=? AND kind=? AND expires>?',
                               (digest(body.token), kind, time.time()))
        if not link:
            raise HTTPException(400, 'This link has expired or has already been used. Request a fresh link.')
        with PASSWORD_WORK:
            password_hash = PASSWORD.hash(body.password)
        with self.store.transaction() as db:
            if kind == 'reset':
                account = lock_account(self.store, link[0]['account_id'])
            current = db.execute('SELECT * FROM hosted_account_links WHERE hash=? AND kind=? AND expires>?',
                                 (digest(body.token), kind, time.time())).fetchone()
            if not current:
                raise HTTPException(400, 'This link has expired or has already been used. Request a fresh link.')
            if kind == 'signup':
                account_id = random_token()
                created = db.execute("""INSERT INTO accounts(id,username,hash,role) VALUES(?,?,?,'member')
                    ON CONFLICT(username) DO NOTHING RETURNING id""",
                    (account_id, current['email'], password_hash)).fetchone()
                if not created:
                    raise HTTPException(400, 'This link has expired or has already been used. Request a fresh link.')
                db.execute('''UPDATE hosted_email_outbox SET sealed_secret=NULL,state='canceled',lease=NULL,lease_until=0
                    WHERE link_hash IN (SELECT hash FROM hosted_account_links WHERE email=?)''', (current['email'],))
                db.execute('DELETE FROM hosted_account_links WHERE email=?', (current['email'],))
            else:
                if not account or account['disabled'] or account['version'] != current['account_version']:
                    raise HTTPException(400, 'This link has expired or has already been used. Request a fresh link.')
                db.execute('UPDATE accounts SET hash=?,version=version+1,temporary_until=NULL WHERE id=?',
                           (password_hash, account['id']))
                revoke_user(self.store, account['id'])
                cancel_links(db, account['id'])
        return {'message': 'Your password is set. Sign in to Airkeep.'}

    def delete(self, body, request, response, identity):
        self.oauth.same_origin(request)
        if identity.client != 'dashboard':
            raise HTTPException(403, 'Manage your account in the dashboard.')
        if body.confirmation != 'DELETE':
            raise HTTPException(422, 'Type DELETE to confirm account deletion.')
        self.oauth.rate_limit(request, identity.user)
        rows = self.store.rows('SELECT * FROM accounts WHERE id=?', (identity.user,))
        account = rows[0] if rows else None
        if not self.oauth.verify(body.password, account['hash'] if account else ''):
            raise HTTPException(401, 'Check your current password.')
        with self.store.transaction() as db:
            current = lock_account(self.store, identity.user)
            if not current or current['version'] != account['version'] or not self.oauth.active(identity.grant):
                raise HTTPException(409, 'Your account changed. Sign in again.')
            db.execute('INSERT INTO account_deletions(account_id,deleted_at) VALUES(?,?) ON CONFLICT DO NOTHING',
                       (identity.user, time.time()))
            revoke_user(self.store, identity.user)
            cancel_links(db, identity.user)
            with self.store.tenant(identity.user):
                self.store.purge_tenant(identity.user)
            db.execute('DELETE FROM oauth_device_requests WHERE owner=?', (identity.user,))
            db.execute('DELETE FROM oauth_requests WHERE user=?', (identity.user,))
            db.execute('DELETE FROM oauth_refresh WHERE grant_id IN (SELECT id FROM oauth_grants WHERE user=?)', (identity.user,))
            db.execute('DELETE FROM oauth_grants WHERE user=?', (identity.user,))
            db.execute('DELETE FROM registration_owners WHERE user=?', (identity.user,))
            db.execute('DELETE FROM accounts WHERE id=?', (identity.user,))
        self.oauth.clear_browser_cookie(response)

    def claim(self, enabled=True):
        now = time.time()
        with self.store.transaction() as db:
            db.execute('''UPDATE hosted_email_outbox SET sealed_secret=NULL,state='expired',lease=NULL,lease_until=0
                WHERE state='pending' AND (link_hash IS NULL OR link_hash IN
                (SELECT hash FROM hosted_account_links WHERE expires<=?))''', (now,))
            db.execute('DELETE FROM hosted_account_links WHERE expires<=?', (now,))
            db.execute('DELETE FROM hosted_account_limits WHERE expires<=?', (now,))
            db.execute('DELETE FROM hosted_email_outbox WHERE updated<?', (now - 7*DAY,))
            if not enabled:
                return None
            suffix = ' FOR UPDATE OF o SKIP LOCKED' if getattr(self.store, 'hosted', False) else ''
            row = db.execute('''SELECT o.*,l.email,l.kind,l.expires FROM hosted_email_outbox o
                LEFT JOIN hosted_account_links l ON l.hash=o.link_hash
                WHERE o.state IN ('pending','accepted') AND o.next_attempt<=? AND o.lease_until<=?
                ORDER BY o.next_attempt,o.id LIMIT 1''' + suffix, (now, now)).fetchone()
            if not row:
                return None
            lease = random_token()
            db.execute('UPDATE hosted_email_outbox SET lease=?,lease_until=? WHERE id=?', (lease, now+60, row['id']))
            return {**dict(row), 'lease': lease}

    def finish_attempt(self, row, *, state, status=None, operation=None, retry=False):
        now = time.time()
        attempt = row['attempts'] + 1
        delay = min(1800, 15 * 2**min(attempt, 7))
        if retry and (attempt >= 8 or (row['state'] == 'pending' and now + delay >= row['expires'])):
            state, retry = 'failed', False
        sealed = row['sealed_secret'] if state == 'pending' and retry else None
        with self.store.transaction() as db:
            db.execute('''UPDATE hosted_email_outbox SET state=?,last_status=?,operation_id=?,sealed_secret=?,
                attempts=?,next_attempt=?,lease=NULL,lease_until=0,updated=? WHERE id=? AND lease=?''',
                (state, status, operation or row['operation_id'], sealed, attempt,
                 now + (delay if retry else 60), now, row['id'], row['lease']))

    async def tick(self):
        # HTTP submission and polling run outside database transactions.
        row = await run_in_threadpool(self.claim, self.configured)
        if not row:
            return
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        auth = (self.settings.twilio_api_key.get_secret_value(), self.settings.twilio_api_secret.get_secret_value())
        try:
            if row['state'] == 'pending':
                if not row['sealed_secret'] or row['expires'] <= time.time():
                    await run_in_threadpool(self.finish_attempt, row, state='expired')
                    return
                token = self.cipher.decrypt(row['sealed_secret'].encode(), ttl=3600).decode()
                path = '/signup/complete' if row['kind'] == 'signup' else '/reset-password'
                link = self.origin + path + '#' + urlencode({'token': token})
                subject = 'Verify your Airkeep email' if row['kind'] == 'signup' else 'Reset your Airkeep password'
                text = f'{subject}\n\nOpen this link to set your password. The link expires shortly and works once.\n\n{link}\n\nIf you did not request this, you can ignore this email.'
                payload = {'from': {'address': self.settings.email_from, 'name': self.settings.email_from_name},
                           'to': [{'address': row['email']}], 'content': {'subject': subject, 'text': text,
                           'html': f'<p>{html.escape(subject)}</p><p><a href="{html.escape(link, quote=True)}">Set your password</a></p><p>This link expires shortly and works once. If you did not request this, you can ignore this email.</p>'}}
                response = await client.post(TWILIO_EMAIL, auth=auth, json=payload)
                if response.status_code == 202:
                    operation = response.json().get('operationId')
                    if not isinstance(operation, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', operation):
                        await run_in_threadpool(self.finish_attempt, row, state='failed', status=202)
                    else:
                        await run_in_threadpool(self.finish_attempt, row, state='accepted', status=202, operation=operation)
                else:
                    retry = response.status_code == 429 or response.status_code >= 500
                    await run_in_threadpool(self.finish_attempt, row, state='pending' if retry else 'failed',
                                            status=response.status_code, retry=retry)
            else:
                response = await client.get(TWILIO_EMAIL + '/Operations/' + quote(row['operation_id'], safe=''), auth=auth)
                if response.is_success:
                    operation = response.json()
                    stats = operation.get('stats', {})
                    state = 'accepted'
                    if stats.get('delivered', 0) or stats.get('sent', 0):
                        state = 'sent'
                    elif operation.get('status') in ('COMPLETED', 'CANCELED'):
                        state = 'failed'
                    await run_in_threadpool(self.finish_attempt, row, state=state, status=response.status_code,
                                            retry=state == 'accepted')
                else:
                    await run_in_threadpool(self.finish_attempt, row, state='accepted', status=response.status_code, retry=True)
        except InvalidToken:
            await run_in_threadpool(self.finish_attempt, row, state='failed')
        except (httpx.HTTPError, ValueError, TypeError):
            await run_in_threadpool(self.finish_attempt, row, state=row['state'], retry=True)
        finally:
            if owned_client:
                await client.aclose()

    async def close(self):
        if self.client is not None:
            await self.client.aclose()


def install_hosted_accounts(app, oauth, settings, principal):
    service = HostedAccounts(oauth, settings)

    @app.post('/auth/signup')
    @app.post('/auth/signup/resend')
    def signup(body: EmailAddress, request: Request):
        return service.request_link(body, request, 'signup')

    @app.post('/auth/password/forgot')
    def forgot(body: EmailAddress, request: Request):
        return service.request_link(body, request, 'reset')

    @app.post('/auth/signup/complete')
    def complete_signup(body: CompleteLink, request: Request):
        return service.complete(body, request, 'signup')

    @app.post('/auth/password/reset')
    def reset(body: CompleteLink, request: Request):
        return service.complete(body, request, 'reset')

    @app.delete('/auth/account', status_code=204)
    def delete(body: DeleteAccount, request: Request, response: Response, identity: Principal = Depends(principal)):
        service.delete(body, request, response, identity)

    return service
