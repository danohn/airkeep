"""Verify enrollment and sign-in through the same PKCE entry points as clients."""
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
import pyotp
from webauthn.helpers import bytes_to_base64url, encode_cbor

import account_security
from accounts import OAuth, digest
from config import Settings
from fastapi import HTTPException
import pytest
from test_accounts import (
    ORIGIN, OWNER_PASSWORD, STATE, VERIFIER, begin, login, relay,
)


def exchange(client, response, callback, app='dashboard'):
    assert response.status_code == 200, response.text
    assert 'set-cookie' not in response.headers
    redirect = urlsplit(response.json()['redirect'])
    assert redirect._replace(query='').geturl() == callback
    query = parse_qs(redirect.query)
    assert query['state'] == [STATE]
    return client.post('/auth/token', data=dict(client_id=app, grant_type='authorization_code',
                       code=query['code'][0], redirect_uri=callback, code_verifier=VERIFIER))


def test_totp_enrollment_login_and_recovery_codes_are_single_use(relay, monkeypatch):
    client, store, *_ = relay
    clock = SimpleNamespace(now=1_800_000_000)
    monkeypatch.setattr(account_security, 'time', SimpleNamespace(time=lambda: clock.now))
    session = login(client, 'Owner', OWNER_PASSWORD)
    password = {'password': OWNER_PASSWORD}
    setup = client.post('/auth/security/totp/setup', headers=session, json=password)
    assert setup.status_code == 200, setup.text
    enrollment = setup.json()
    assert enrollment['qr_code'].startswith('data:image/png;base64,')
    assert not client.get('/auth/security', headers=session).json()['totp_enabled']
    otp = pyotp.TOTP(enrollment['secret'])
    code = otp.at(clock.now)
    enabled = client.post('/auth/security/totp', headers=session, json={'code': code})
    assert enabled.status_code == 200, enabled.text
    codes = enabled.json()['recovery_codes']
    assert len(set(codes)) == 8
    assert enrollment['secret'] not in store.rows('SELECT sealed_secret FROM account_totp')[0][0]
    assert not set(codes) & {row['code_hash'] for row in store.rows('SELECT code_hash FROM account_recovery_codes')}

    def credentials(transaction, verification_code=None):
        return client.post('/auth/credentials', json={
            'transaction': transaction, 'username': 'Owner', **password, 'verification_code': verification_code,
        })

    transaction, callback = begin(client, 'apple-ios', 'phone')
    response = credentials(transaction)
    assert response.json() == {'totpRequired': True}
    assert 'set-cookie' not in response.headers
    assert credentials(transaction, code).status_code == 401
    clock.now += 30
    fresh = otp.at(clock.now)
    native = exchange(client, credentials(transaction, fresh), callback, 'apple-ios')
    assert native.status_code == 200 and native.json()['refresh_token']
    transaction, callback = begin(client)
    assert credentials(transaction, fresh).status_code == 401
    recovered = exchange(client, credentials(transaction, codes[0]), callback)
    assert recovered.status_code == 200 and 'set-cookie' in recovered.headers
    transaction, _ = begin(client)
    assert credentials(transaction, codes[0]).status_code == 401
    assert client.post('/auth/password', headers=session, json={
        **password, 'new_password': 'a different test password',
    }).status_code == 401
    assert client.request('DELETE', '/auth/security/totp', headers=session, json=password).status_code == 403
    assert client.request('DELETE', '/auth/security/totp', headers=session,
                          json={**password, 'verification_code': codes[1]}).status_code == 204
    assert not store.rows('SELECT * FROM account_recovery_codes')
    assert 'redirect' in credentials(transaction).json()


def test_passkey_registration_and_pkce_sign_in(relay):
    client, store, *_ = relay
    session = login(client, 'Owner', OWNER_PASSWORD)
    options = client.post('/auth/security/passkeys/options', headers=session, json={'password': OWNER_PASSWORD})
    assert options.status_code == 200, options.text
    registration = options.json()
    rp_hash = hashlib.sha256(registration['rp']['id'].encode()).digest()
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_numbers()
    cose = encode_cbor({1: 2, 3: -7, -1: 1, -2: public_key.x.to_bytes(32), -3: public_key.y.to_bytes(32)})
    credential_id = b'test passkey credential id'
    encoded_id = bytes_to_base64url(credential_id)

    def client_data(kind, challenge):
        return json.dumps({'type': kind, 'origin': ORIGIN, 'challenge': challenge}).encode()

    def credential(**response):
        return {'id': encoded_id, 'rawId': encoded_id, 'type': 'public-key', 'response': response}

    auth_data = rp_hash + b'\x45' + bytes(20) + len(credential_id).to_bytes(2) + credential_id + cose
    attestation = encode_cbor({'fmt': 'none', 'attStmt': {}, 'authData': auth_data})
    registration_body = {'label': 'Test device', 'credential': credential(
        clientDataJSON=bytes_to_base64url(client_data('webauthn.create', registration['challenge'])),
        attestationObject=bytes_to_base64url(attestation),
    )}
    assert client.post('/auth/security/passkeys', headers=session, json=registration_body).status_code == 204
    assert client.post('/auth/security/passkeys', headers=session, json=registration_body).status_code == 400
    assert client.get('/auth/security', headers=session).json()['passkeys'][0]['label'] == 'Test device'

    transaction, callback = begin(client, 'apple-mac', 'mac')
    response = client.post('/auth/passkey/options', json={'transaction': transaction})
    assert response.json()['userVerification'] == 'required'
    data = client_data('webauthn.get', response.json()['challenge'])

    def assertion(flags, user_handle=registration['user']['id']):
        auth_data = rp_hash + bytes([flags]) + (1).to_bytes(4)
        signature = private_key.sign(auth_data + hashlib.sha256(data).digest(), ec.ECDSA(hashes.SHA256()))
        return credential(clientDataJSON=bytes_to_base64url(data), authenticatorData=bytes_to_base64url(auth_data),
                          signature=bytes_to_base64url(signature), userHandle=user_handle)

    def verify(value):
        return client.post('/auth/passkey/verify', json={'transaction': transaction, 'credential': value})

    assert verify(assertion(1)).status_code == 401
    assert verify(assertion(5, bytes_to_base64url(b'another account'))).status_code == 401
    valid = assertion(5)
    token = exchange(client, verify(valid), callback, 'apple-mac')
    assert token.status_code == 200 and token.json()['refresh_token']
    assert verify(valid).status_code == 400
    assert store.rows('SELECT sign_count FROM account_passkeys')[0]['sign_count'] == 1
    assert client.request('DELETE', '/auth/security/passkeys/' + encoded_id, headers=session,
                          json={'password': OWNER_PASSWORD}).status_code == 204
    assert not client.get('/auth/security', headers=session).json()['passkeys']
    transaction, _ = begin(client)
    options = client.post('/auth/passkey/options', json={'transaction': transaction})
    assert options.status_code == 200
    data = client_data('webauthn.get', options.json()['challenge'])
    assert verify(assertion(5)).status_code == 401


def test_completing_sign_in_preserves_the_first_authorization_code(relay):
    client, store, *_ = relay
    oauth = OAuth(store, Settings(web_origin=ORIGIN, jwt_signing_key='x'*64, _env_file=None))
    transaction, _ = begin(client)
    request = store.rows('SELECT * FROM oauth_requests WHERE id=?', (digest(transaction),))[0]
    account = store.rows("SELECT id,version FROM accounts WHERE username='Owner'")[0]
    with store.transaction() as db:
        oauth.complete(db, request, account['id'], account['version'])
    original = store.rows('SELECT code FROM oauth_requests WHERE id=?', (request['id'],))[0]['code']
    with pytest.raises(HTTPException) as error, store.transaction() as db:
        oauth.complete(db, request, account['id'], account['version'])
    assert error.value.status_code == 400
    assert store.rows('SELECT code FROM oauth_requests WHERE id=?', (request['id'],))[0]['code'] == original
