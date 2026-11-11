"""Passkey ceremonies and authenticator verification for Airkeep accounts."""
import base64
from contextlib import contextmanager
import hashlib
import hmac
from io import BytesIO
import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
import pyotp
import qrcode
from qrcode.image.pure import PyPNGImage
from webauthn import (
    generate_authentication_options, generate_registration_options,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes, bytes_to_base64url, options_to_json_dict, parse_authentication_credential_json,
    parse_client_data_json, parse_registration_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, ResidentKeyRequirement,
    UserVerificationRequirement,
)

from accounts import Principal, VerificationCode, digest, lock_account

PASSKEY_TIMEOUT = 120
MAX_PASSKEYS = 10
INVALID_PASSKEY = (WebAuthnException, ValueError, TypeError)
INVALID_CODE = 'Enter a fresh authenticator code or an unused recovery code.'


class Transaction(BaseModel):
    transaction: str = Field(pattern=r'^[A-Za-z0-9_-]{43}$')


class Assertion(Transaction):
    credential: dict


class Confirmation(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    verification_code: VerificationCode | None = None


class Registration(BaseModel):
    credential: dict
    label: str = Field(default='Passkey', min_length=1, max_length=60)


class AuthenticatorCode(BaseModel):
    code: str = Field(pattern=r'^[0-9]{6}$')


def relying_party(origin):
    url = urlsplit(origin)
    try:
        ipaddress.ip_address(url.hostname)
    except ValueError:
        if url.scheme == 'https' or url.hostname == 'localhost':
            return url.hostname
    return None


def match_totp(secret, code, last_step):
    if not code or not re.fullmatch(r'[0-9]{6}', code):
        return None
    totp = pyotp.TOTP(secret)
    current = int(time.time()) // totp.interval
    for step in (current, current - 1, current + 1):
        if step > last_step and secrets.compare_digest(totp.generate_otp(step), code):
            return step
    return None


class AccountSecurity:
    def __init__(self, oauth):
        self.oauth, self.store = oauth, oauth.store
        self.rp_id = relying_party(oauth.origin)
        # Domain separation keeps the encryption key distinct from the JWT signing key.
        key = hmac.digest(oauth.key.encode(), b'airkeep account totp encryption v1', hashlib.sha256)
        self.cipher = Fernet(base64.urlsafe_b64encode(key))

    def totp_enabled(self, db, user):
        return bool(db.execute('SELECT 1 FROM account_totp WHERE account_id=?', (user,)).fetchone())

    def verify_totp(self, db, user, code, *, status=401):
        row = db.execute('SELECT * FROM account_totp WHERE account_id=?', (user,)).fetchone()
        if not row:
            return
        code = (code or '').strip()
        recovery = re.sub(r'[\s-]', '', code).lower()
        if re.fullmatch(r'[0-9a-f]{20}', recovery):
            if db.execute('DELETE FROM account_recovery_codes WHERE account_id=? AND code_hash=?',
                          (user, digest(recovery))).rowcount:
                return
        elif re.fullmatch(r'[0-9]{6}', code):
            try:
                secret = self.cipher.decrypt(row['sealed_secret'].encode()).decode()
            except InvalidToken:
                raise HTTPException(status, INVALID_CODE) from None
            step = match_totp(secret, code, row['last_step'])
            if step is not None:
                db.execute('UPDATE account_totp SET last_step=? WHERE account_id=?', (step, user))
                return
        raise HTTPException(status, INVALID_CODE)

    def recovery_codes(self, db, user):
        codes = [secrets.token_hex(10) for _ in range(8)]
        db.execute('DELETE FROM account_recovery_codes WHERE account_id=?', (user,))
        db.executemany('INSERT INTO account_recovery_codes(account_id,code_hash) VALUES(?,?)',
                       ((user, digest(code)) for code in codes))
        return {'recovery_codes': ['-'.join(code[i:i+5] for i in range(0, len(code), 5)) for code in codes]}

    def require_passkeys(self):
        if not self.rp_id:
            raise HTTPException(400, 'Open Airkeep on its HTTPS hostname to use passkeys.')

    def account(self, identity):
        row = lock_account(self.store, identity.user)
        if not row or row['disabled'] or not self.oauth.active(identity.grant):
            raise HTTPException(401, 'Sign in to Airkeep.')
        if not row['hash'] or row['temporary_until'] is not None:
            raise HTTPException(403, 'Set your own Airkeep password to manage sign-in methods.')
        return row

    @contextmanager
    def confirm(self, request, identity, body):
        self.oauth.rate_limit(request, identity.user, scope='security')
        users = self.store.rows('SELECT hash,version FROM accounts WHERE id=?', (identity.user,))
        if not self.oauth.verify(body.password, users[0]['hash'] if users else ''):
            raise HTTPException(403, 'Check your current password.')
        with self.store.transaction() as db:
            account = self.account(identity)
            if account['version'] != users[0]['version']:
                raise HTTPException(409, 'Your account changed. Sign in again.')
            self.verify_totp(db, identity.user, body.verification_code, status=403)
            yield db, account

    def transaction(self, db, binding):
        row = db.execute('SELECT * FROM oauth_requests WHERE id=? AND code IS NULL AND expires>?',
                         (binding, time.time())).fetchone()
        if not row:
            raise HTTPException(400, 'Start sign-in again.')
        return row

    def save_challenge(self, db, purpose, binding, value, expires, account=None):
        db.execute('''INSERT INTO account_auth_challenges(purpose,binding,value,account_id,version,expires)
            VALUES(?,?,?,?,?,?) ON CONFLICT(purpose,binding) DO UPDATE SET
            value=excluded.value,account_id=excluded.account_id,version=excluded.version,expires=excluded.expires''',
            (purpose, binding, value, account['id'] if account else None,
             account['version'] if account else None, expires))

    def challenge(self, db, purpose, binding, account=None):
        row = db.execute('''SELECT * FROM account_auth_challenges
            WHERE purpose=? AND binding=? AND expires>?''', (purpose, binding, time.time())).fetchone()
        if not row or (account and (row['account_id'], row['version']) != (account['id'], account['version'])):
            raise HTTPException(400, 'This request expired. Start again.')
        return row['value']

    def clear_challenge(self, db, purpose, binding):
        db.execute('DELETE FROM account_auth_challenges WHERE purpose=? AND binding=?', (purpose, binding))

    def login_options(self, body, request):
        self.require_passkeys()
        binding = digest(body.transaction)
        self.oauth.rate_limit(request, binding, scope='passkey')
        options = generate_authentication_options(rp_id=self.rp_id, timeout=PASSKEY_TIMEOUT*1000,
                                                  user_verification=UserVerificationRequirement.REQUIRED)
        with self.store.transaction() as db:
            tx = self.transaction(db, binding)
            self.save_challenge(db, 'passkey-login', binding, bytes_to_base64url(options.challenge),
                                min(tx['expires'], time.time()+PASSKEY_TIMEOUT))
        return options_to_json_dict(options)

    def verify_passkey(self, body, request):
        self.require_passkeys()
        binding = digest(body.transaction)
        self.oauth.rate_limit(request, binding, scope='passkey')
        error = HTTPException(401, 'Could not verify that passkey. Try again or use your password.')
        try:
            credential = parse_authentication_credential_json(body.credential)
            if parse_client_data_json(credential.response.client_data_json).cross_origin:
                raise error
        except INVALID_PASSKEY:
            raise error from None
        with self.store.transaction() as db:
            tx = self.transaction(db, binding)
            challenge = self.challenge(db, 'passkey-login', binding)
            row = db.execute('SELECT * FROM account_passkeys WHERE credential_id=?', (credential.id,)).fetchone()
            if not row or credential.response.user_handle != row['account_id'].encode():
                raise error
            account = lock_account(self.store, row['account_id'])
            if not account or account['disabled'] or not account['hash'] or account['temporary_until'] is not None:
                raise error
            try:
                result = verify_authentication_response(
                    credential=credential, expected_challenge=base64url_to_bytes(challenge),
                    expected_rp_id=self.rp_id, expected_origin=self.oauth.origin,
                    credential_public_key=base64url_to_bytes(row['public_key']),
                    credential_current_sign_count=row['sign_count'], require_user_verification=True,
                )
            except INVALID_PASSKEY:
                raise error from None
            db.execute('UPDATE account_passkeys SET sign_count=?,last_used_at=? WHERE credential_id=?',
                       (result.new_sign_count, time.time(), credential.id))
            self.clear_challenge(db, 'passkey-login', binding)
            return {'redirect': self.oauth.complete(db, tx, account['id'], account['version'])}

    def registration_options(self, request, identity, body):
        self.require_passkeys()
        with self.confirm(request, identity, body) as (db, account):
            rows = db.execute('SELECT credential_id FROM account_passkeys WHERE account_id=?',
                              (identity.user,)).fetchall()
            if len(rows) >= MAX_PASSKEYS:
                raise HTTPException(409, 'Remove a passkey before adding another.')
            options = generate_registration_options(
                rp_id=self.rp_id, rp_name='Airkeep', user_id=identity.user.encode(),
                user_name=account['username'], timeout=PASSKEY_TIMEOUT*1000,
                exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(row['credential_id']))
                                     for row in rows],
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED, require_resident_key=True,
                    user_verification=UserVerificationRequirement.REQUIRED),
            )
            self.save_challenge(db, 'passkey-setup', identity.grant, bytes_to_base64url(options.challenge),
                                time.time()+PASSKEY_TIMEOUT, account)
        return options_to_json_dict(options)

    def register_passkey(self, request, identity, body):
        self.require_passkeys()
        self.oauth.rate_limit(request, identity.user, scope='passkey-setup')
        error = HTTPException(400, 'Could not register that passkey. Try again.')
        try:
            credential = parse_registration_credential_json(body.credential)
            if parse_client_data_json(credential.response.client_data_json).cross_origin:
                raise error
        except INVALID_PASSKEY:
            raise error from None
        with self.store.transaction() as db:
            account = self.account(identity)
            challenge = self.challenge(db, 'passkey-setup', identity.grant, account)
            try:
                result = verify_registration_response(
                    credential=credential, expected_challenge=base64url_to_bytes(challenge),
                    expected_rp_id=self.rp_id, expected_origin=self.oauth.origin,
                    require_user_verification=True,
                )
            except INVALID_PASSKEY:
                raise error from None
            inserted = db.execute('''INSERT INTO account_passkeys
                (credential_id,account_id,label,public_key,sign_count,created_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(credential_id) DO NOTHING''',
                (bytes_to_base64url(result.credential_id), identity.user, body.label.strip() or 'Passkey',
                 bytes_to_base64url(result.credential_public_key), result.sign_count, time.time()))
            if not inserted.rowcount:
                raise HTTPException(409, 'That passkey is already registered.')
            self.clear_challenge(db, 'passkey-setup', identity.grant)
            self.change_version(db, identity.user)

    def change_version(self, db, user):
        db.execute('UPDATE accounts SET version=version+1 WHERE id=?', (user,))


def install_security(app, oauth, principal):
    factors, store = oauth.factors, oauth.store

    def dashboard(identity: Principal = Depends(principal)):
        if identity.client != 'dashboard':
            raise HTTPException(403, 'Manage sign-in methods in the dashboard.')
        return identity

    @app.post('/auth/passkey/options')
    def login_options(body: Transaction, request: Request):
        oauth.same_origin(request)
        return factors.login_options(body, request)

    @app.post('/auth/passkey/verify')
    def verify_passkey(body: Assertion, request: Request):
        oauth.same_origin(request)
        return factors.verify_passkey(body, request)

    @app.get('/auth/security')
    def status(identity: Principal = Depends(dashboard)):
        with store.transaction() as db:
            rows = db.execute('''SELECT credential_id,label,created_at,last_used_at FROM account_passkeys
                WHERE account_id=? ORDER BY created_at''', (identity.user,)).fetchall()
            enabled = factors.totp_enabled(db, identity.user)
        return {'passkeys': [dict(row) for row in rows], 'totp_enabled': enabled, 'passkeys_available': bool(factors.rp_id)}

    @app.post('/auth/security/passkeys/options')
    def registration_options(body: Confirmation, request: Request, identity: Principal = Depends(dashboard)):
        return factors.registration_options(request, identity, body)

    @app.post('/auth/security/passkeys', status_code=204)
    def register_passkey(body: Registration, request: Request, identity: Principal = Depends(dashboard)):
        factors.register_passkey(request, identity, body)

    @app.delete('/auth/security/passkeys/{credential_id}', status_code=204)
    def remove_passkey(credential_id: str, body: Confirmation, request: Request, identity: Principal = Depends(dashboard)):
        with factors.confirm(request, identity, body) as (db, account):
            if not db.execute('DELETE FROM account_passkeys WHERE account_id=? AND credential_id=?',
                              (identity.user, credential_id)).rowcount:
                raise HTTPException(404, 'Passkey not found.')
            factors.change_version(db, account['id'])

    @app.post('/auth/security/totp/setup')
    def setup_totp(body: Confirmation, request: Request, identity: Principal = Depends(dashboard)):
        secret = pyotp.random_base32()
        with factors.confirm(request, identity, body) as (db, account):
            if factors.totp_enabled(db, identity.user):
                raise HTTPException(409, 'Your authenticator is already enabled.')
            sealed = factors.cipher.encrypt(secret.encode()).decode()
            factors.save_challenge(db, 'totp-setup', identity.grant, sealed, time.time()+300, account)
        uri = pyotp.TOTP(secret).provisioning_uri(name=account['username'], issuer_name='Airkeep')
        image = BytesIO()
        qrcode.make(uri, image_factory=PyPNGImage, box_size=5).save(image)
        return {'secret': secret, 'qr_code': 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()}

    @app.post('/auth/security/totp')
    def enable_totp(body: AuthenticatorCode, request: Request, identity: Principal = Depends(dashboard)):
        oauth.rate_limit(request, identity.user, scope='totp-setup')
        with store.transaction() as db:
            account = factors.account(identity)
            sealed = factors.challenge(db, 'totp-setup', identity.grant, account)
            try:
                secret = factors.cipher.decrypt(sealed.encode()).decode()
            except InvalidToken:
                raise HTTPException(400, 'This setup expired. Start again.') from None
            step = match_totp(secret, body.code, -1)
            if step is None:
                raise HTTPException(400, 'Check the code in your authenticator and try again.')
            db.execute('INSERT INTO account_totp(account_id,sealed_secret,last_step) VALUES(?,?,?)',
                       (identity.user, sealed, step))
            factors.clear_challenge(db, 'totp-setup', identity.grant)
            factors.change_version(db, identity.user)
            return factors.recovery_codes(db, identity.user)

    @app.delete('/auth/security/totp', status_code=204)
    def disable_totp(body: Confirmation, request: Request, identity: Principal = Depends(dashboard)):
        with factors.confirm(request, identity, body) as (db, account):
            if db.execute('DELETE FROM account_totp WHERE account_id=?', (identity.user,)).rowcount:
                db.execute('DELETE FROM account_recovery_codes WHERE account_id=?', (identity.user,))
                factors.change_version(db, account['id'])

    @app.post('/auth/security/totp/recovery')
    def replace_recovery(body: Confirmation, request: Request, identity: Principal = Depends(dashboard)):
        with factors.confirm(request, identity, body) as (db, account):
            if not factors.totp_enabled(db, identity.user):
                raise HTTPException(409, 'Set up an authenticator first.')
            factors.change_version(db, account['id'])
            return factors.recovery_codes(db, identity.user)
