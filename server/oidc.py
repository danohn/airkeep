"""Provider sign-in and invitations for Airkeep accounts."""

import base64
import hashlib
import json
import time
from typing import Annotated, Literal
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
import httpx
import jwt
from pydantic import BaseModel, Field

from accounts import DAY, USERNAME, Principal, digest, random_token

TOKEN = r'^[A-Za-z0-9_-]{43}$'
ALGORITHMS = {'RS256', 'RS384', 'RS512', 'PS256', 'PS384', 'PS512',
              'ES256', 'ES384', 'ES512', 'EdDSA'}


class Start(BaseModel):
    transaction: str = Field(pattern=TOKEN)
    invitation: str | None = Field(default=None, pattern=TOKEN)


class Policy(BaseModel):
    automatic_creation: bool = False
    group_claim: str = Field(default='groups', min_length=1, max_length=100)
    admin_groups: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=list, max_length=50)


class Invitation(BaseModel):
    account_id: str | None = Field(default=None, pattern=TOKEN)
    username: str | None = Field(default=None, pattern=USERNAME.pattern)
    role: Literal['member', 'admin'] = 'member'


class ProviderError(Exception):
    pass


def https_url(value):
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.username is not None
            or url.fragment or url.port == 0):
        raise ProviderError()
    return url


class OIDC:
    def __init__(self, oauth, settings):
        self.oauth = oauth
        self.store = oauth.store
        self.issuer = settings.oidc_issuer
        self.client_id = settings.oidc_client_id
        self.secret = settings.oidc_client_secret.get_secret_value()
        self.callback_url = oauth.origin + '/auth/oidc/callback'
        self.additional_scopes = settings.oidc_scopes.split()
        with self.store.lock, self.store.db:
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS oidc_policies(
                    issuer TEXT PRIMARY KEY, policy TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS oidc_identities(
                    issuer TEXT NOT NULL, subject TEXT NOT NULL, account_id TEXT NOT NULL,
                    PRIMARY KEY(issuer,subject), UNIQUE(issuer,account_id));
                CREATE TABLE IF NOT EXISTS oidc_invitations(
                    id TEXT PRIMARY KEY, issuer TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                    account_id TEXT, username TEXT COLLATE NOCASE, role TEXT NOT NULL,
                    expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS oidc_requests(
                    state_hash TEXT PRIMARY KEY, issuer TEXT NOT NULL, transaction_id TEXT NOT NULL UNIQUE,
                    nonce TEXT NOT NULL, verifier TEXT NOT NULL, invitation_hash TEXT, expires REAL NOT NULL);
            """)

    def metadata(self, client):
        discovery = self.issuer.rstrip('/') + '/.well-known/openid-configuration'
        document = self.get_document(client.get(discovery))
        if document.get('issuer') != self.issuer:
            raise ProviderError()
        for key in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            https_url(document[key])
        return document

    @staticmethod
    def get_document(response):
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, dict):
            raise ProviderError()
        return document

    def authorization_url(self, metadata, state, nonce, verifier):
        url = https_url(metadata['authorization_endpoint'])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        scopes = ['openid']
        if 'profile' in metadata.get('scopes_supported', ['profile']):
            scopes.append('profile')
        scopes.extend(self.additional_scopes)
        fields = dict(parse_qsl(url.query, keep_blank_values=True))
        fields.update(client_id=self.client_id, response_type='code', scope=' '.join(dict.fromkeys(scopes)),
                      redirect_uri=self.callback_url, state=state, nonce=nonce,
                      code_challenge=challenge, code_challenge_method='S256')
        return urlunsplit(url._replace(query=urlencode(fields)))

    def identity(self, client, code, pending):
        metadata = self.metadata(client)
        fields = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': self.callback_url,
                  'code_verifier': pending['verifier']}
        methods = metadata.get('token_endpoint_auth_methods_supported', ['client_secret_basic'])
        auth = None
        if 'client_secret_basic' in methods:
            auth = (quote_plus(self.client_id), quote_plus(self.secret))
        elif 'client_secret_post' in methods:
            fields.update(client_id=self.client_id, client_secret=self.secret)
        else:
            raise ProviderError()
        tokens = self.get_document(client.post(metadata['token_endpoint'], data=fields, auth=auth))
        token = tokens['id_token']
        header = jwt.get_unverified_header(token)
        algorithm = header.get('alg')
        if algorithm not in ALGORITHMS or algorithm not in metadata.get('id_token_signing_alg_values_supported', [algorithm]):
            raise ProviderError()
        keys = self.get_document(client.get(metadata['jwks_uri']))['keys']
        candidates = [key for key in keys if isinstance(key, dict) and key.get('use', 'sig') == 'sig'
                      and key.get('alg', algorithm) == algorithm
                      and ('kid' not in header or key.get('kid') == header['kid'])]
        if len(candidates) != 1:
            raise ProviderError()
        signing_key = jwt.PyJWK.from_dict(candidates[0], algorithm=algorithm)
        claims = jwt.decode(token, signing_key.key, algorithms=[algorithm], audience=self.client_id,
                            issuer=self.issuer, options={'require': ['iss', 'sub', 'aud', 'exp', 'iat', 'nonce']})
        audience = claims['aud']
        if ((isinstance(audience, list) and len(audience) > 1 or 'azp' in claims)
                and claims.get('azp') != self.client_id):
            raise ProviderError()
        if claims['nonce'] != pending['nonce'] or not 1 <= len(claims['sub']) <= 255:
            raise ProviderError()
        return claims

    def policy(self, db):
        row = db.execute('SELECT policy FROM oidc_policies WHERE issuer=?', (self.issuer,)).fetchone()
        return Policy.model_validate_json(row['policy']) if row else Policy()

    def pending_invite(self, db, token_hash, now):
        return db.execute("""SELECT * FROM oidc_invitations
            WHERE issuer=? AND token_hash=? AND used=0 AND expires>?""",
            (self.issuer, token_hash, now)).fetchone()

    @staticmethod
    def link_exists(db, issuer, account_id):
        return db.execute('SELECT 1 FROM oidc_identities WHERE issuer=? AND account_id=?',
                          (issuer, account_id)).fetchone() is not None

    def create_account(self, db, username, role):
        user_id = random_token()
        db.execute("INSERT INTO accounts(id,username,hash,role) VALUES(?,?,'',?)", (user_id, username, role))
        return user_id

    def available_username(self, db, claims, now):
        preferred = claims.get('preferred_username')
        base = preferred if isinstance(preferred, str) and USERNAME.fullmatch(preferred) else 'sso-user'
        username = base
        while (db.execute('SELECT 1 FROM accounts WHERE username=?', (username,)).fetchone()
               or db.execute('SELECT 1 FROM oidc_invitations WHERE username=? AND used=0 AND expires>?',
                             (username, now)).fetchone()):
            username = f'{base[:55]}-{random_token()[:8]}'
        return username

    def resolve_account(self, db, claims, pending, now):
        subject = claims['sub']
        linked = db.execute('SELECT account_id FROM oidc_identities WHERE issuer=? AND subject=?',
                            (self.issuer, subject)).fetchone()
        invitation_hash = pending['invitation_hash']
        if invitation_hash:
            invitation = self.pending_invite(db, invitation_hash, now)
            if not invitation or linked:
                raise HTTPException(409, 'This invitation is unavailable for this identity.')
            account_id = invitation['account_id']
            if account_id:
                if (not db.execute('SELECT 1 FROM accounts WHERE id=? AND disabled=0', (account_id,)).fetchone()
                        or self.link_exists(db, self.issuer, account_id)):
                    raise HTTPException(409, 'This invitation is unavailable for this account.')
            else:
                if db.execute('SELECT 1 FROM accounts WHERE username=?', (invitation['username'],)).fetchone():
                    raise HTTPException(409, 'The invited username is already in use. Ask an admin for a new invitation.')
                account_id = self.create_account(db, invitation['username'], invitation['role'])
            db.execute('UPDATE oidc_invitations SET used=1 WHERE id=?', (invitation['id'],))
        elif linked:
            return linked['account_id']
        else:
            policy = self.policy(db)
            if not policy.automatic_creation:
                raise HTTPException(403, 'Ask an Airkeep admin for an invitation.')
            groups = claims.get(policy.group_claim)
            matches = (isinstance(groups, list) and all(isinstance(group, str) for group in groups)
                       and bool(set(groups).intersection(policy.admin_groups)))
            account_id = self.create_account(db, self.available_username(db, claims, now),
                                             'admin' if matches else 'member')
        db.execute('INSERT INTO oidc_identities VALUES(?,?,?)', (self.issuer, subject, account_id))
        return account_id


def install_oidc(app, oauth, settings, admin):
    oidc = OIDC(oauth, settings)
    store = oauth.store

    @app.post('/auth/oidc/start')
    def start(body: Start, request: Request):
        oauth.same_origin(request)
        state, nonce, verifier = random_token(), random_token(), random_token()
        invitation_hash = digest(body.invitation) if body.invitation else None
        now = time.time()
        with store.lock, store.db:
            db = store.db
            db.execute('BEGIN IMMEDIATE')
            transaction = db.execute('SELECT * FROM oauth_requests WHERE id=? AND code IS NULL AND expires>?',
                                     (digest(body.transaction), now)).fetchone()
            if not transaction:
                raise HTTPException(400, 'Start sign-in again.')
            if invitation_hash and (transaction['client'] != 'dashboard' or not oidc.pending_invite(db, invitation_hash, now)):
                raise HTTPException(400, 'This invitation has expired or is no longer available.')
            db.execute('DELETE FROM oidc_requests WHERE expires<=? OR transaction_id=?', (now, transaction['id']))
            db.execute('INSERT INTO oidc_requests VALUES(?,?,?,?,?,?,?)',
                       (digest(state), oidc.issuer, transaction['id'], nonce, verifier, invitation_hash, transaction['expires']))
        try:
            with httpx.Client(timeout=5, follow_redirects=False) as client:
                return {'redirect': oidc.authorization_url(oidc.metadata(client), state, nonce, verifier)}
        except (ProviderError, httpx.HTTPError, httpx.InvalidURL, KeyError, TypeError, ValueError):
            raise HTTPException(502, 'Single sign-on is unavailable. Try again or sign in with a local account.') from None

    @app.get('/auth/oidc/callback')
    def callback(state: Annotated[str, Query(pattern=TOKEN)] = '',
                 code: Annotated[str, Query(max_length=4096)] = '', error: str = ''):
        now = time.time()
        with store.lock, store.db:
            db = store.db
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute('DELETE FROM oidc_requests WHERE state_hash=? AND issuer=? AND expires>? RETURNING *',
                                 (digest(state), oidc.issuer, now)).fetchone()
            transaction = db.execute('SELECT * FROM oauth_requests WHERE id=? AND code IS NULL AND expires>?',
                                     (pending['transaction_id'], now)).fetchone() if pending else None
        destination = '/connect' if transaction and transaction['client'] != 'dashboard' else '/'
        if pending and pending['invitation_hash']:
            destination = '/invite'
        try:
            if not transaction or error or not code:
                raise HTTPException(400, 'Single sign-on did not complete. Start sign-in again.')
            with httpx.Client(timeout=5, follow_redirects=False) as client:
                claims = oidc.identity(client, code, pending)
            with store.lock, store.db:
                db = store.db
                db.execute('BEGIN IMMEDIATE')
                now = time.time()
                current = db.execute('SELECT * FROM oauth_requests WHERE id=? AND code IS NULL AND expires>?',
                                     (transaction['id'], now)).fetchone()
                if not current:
                    raise HTTPException(400, 'Start sign-in again.')
                account_id = oidc.resolve_account(db, claims, pending, now)
                account = db.execute('SELECT version FROM accounts WHERE id=? AND disabled=0', (account_id,)).fetchone()
                if not account:
                    raise HTTPException(403, 'Your Airkeep account is disabled.')
                redirect = oauth.complete(db, current, account_id, account['version'])
            return RedirectResponse(redirect, status_code=303)
        except HTTPException as exc:
            message = exc.detail
        except (ProviderError, httpx.HTTPError, httpx.InvalidURL, jwt.PyJWTError, KeyError, TypeError, ValueError):
            message = 'Single sign-on could not be verified. Try again or use a local account.'
        parameters = {'sso_error': message}
        if destination == '/connect':
            retry = random_token()
            with store.lock, store.db:
                changed = store.db.execute("""UPDATE oauth_requests SET id=?
                    WHERE id=? AND code IS NULL AND expires>?""",
                    (digest(retry), transaction['id'], time.time()))
                if changed.rowcount:
                    parameters['transaction'] = retry
        return RedirectResponse(destination + '?' + urlencode(parameters), status_code=303)

    def require_current_admin(identity):
        current = oauth.active(identity.grant)
        if not current or current['role'] != 'admin':
            raise HTTPException(403, 'Administrator access required.')

    @app.get('/v2/admin/oidc', dependencies=[Depends(admin)])
    def get_policy():
        with store.lock:
            return oidc.policy(store.db)

    @app.put('/v2/admin/oidc')
    def set_policy(body: Policy, identity: Principal = Depends(admin)):
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            require_current_admin(identity)
            store.db.execute('INSERT OR REPLACE INTO oidc_policies VALUES(?,?)',
                             (oidc.issuer, body.model_dump_json()))
        return body

    @app.get('/v2/admin/oidc/invitations', dependencies=[Depends(admin)])
    def invitations():
        return [dict(row) for row in store.rows("""SELECT i.id,COALESCE(i.username,a.username) AS username,
            COALESCE(a.role,i.role) AS role,i.expires,i.account_id
            FROM oidc_invitations i LEFT JOIN accounts a ON a.id=i.account_id
            WHERE i.issuer=? AND i.used=0 AND i.expires>? ORDER BY i.expires DESC""", (oidc.issuer, time.time()))]

    @app.post('/v2/admin/oidc/invitations', status_code=201)
    def invite(body: Invitation, identity: Principal = Depends(admin)):
        if bool(body.account_id) == bool(body.username):
            raise HTTPException(422, 'Choose an existing account or provide a new username.')
        token, invite_id = random_token(), random_token()
        now = time.time()
        with store.lock, store.db:
            db = store.db
            db.execute('BEGIN IMMEDIATE')
            require_current_admin(identity)
            if body.account_id:
                if not db.execute('SELECT 1 FROM accounts WHERE id=? AND disabled=0', (body.account_id,)).fetchone():
                    raise HTTPException(404, 'Active account not found.')
                if oidc.link_exists(db, oidc.issuer, body.account_id):
                    raise HTTPException(409, 'This account already has an SSO identity.')
            elif db.execute('SELECT 1 FROM accounts WHERE username=?', (body.username,)).fetchone():
                raise HTTPException(409, 'That username is already in use.')
            db.execute('DELETE FROM oidc_invitations WHERE expires<=? OR (issuer=? AND used=0 AND (account_id=? OR username=?))',
                       (now, oidc.issuer, body.account_id, body.username))
            db.execute('INSERT INTO oidc_invitations VALUES(?,?,?,?,?,?,?,0)',
                       (invite_id, oidc.issuer, digest(token), body.account_id, body.username, body.role, now+DAY))
        return {'id': invite_id, 'url': oauth.origin + '/invite#' + token}

    @app.delete('/v2/admin/oidc/invitations/{invite_id}', status_code=204)
    def revoke_invitation(invite_id: str, identity: Principal = Depends(admin)):
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            require_current_admin(identity)
            result = store.db.execute('DELETE FROM oidc_invitations WHERE issuer=? AND id=? AND used=0',
                                      (oidc.issuer, invite_id))
            if not result.rowcount:
                raise HTTPException(404, 'Pending invitation not found.')
