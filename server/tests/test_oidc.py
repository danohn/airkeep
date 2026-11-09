import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

import oidc
from accounts import NATIVE_CALLBACK, PASSWORD, bootstrap
from app import create_app
from config import Settings
from storage import Store
from test_accounts import ORIGIN, OWNER_PASSWORD, STATE, VERIFIER, begin, bearer, browser_headers, login

ISSUER = 'https://identity.example.test/oidc/'
CALLBACK = ORIGIN + '/auth/oidc/callback'
CLIENT_ID = 'airkeep-test'
SECRET = 'test-provider-secret'
POLICY = '/v2/admin/oidc'
INVITES = POLICY + '/invitations'


class Provider:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}
        self.unavailable = False

    def respond(self, request):
        if self.unavailable:
            return httpx.Response(503)
        if request.url.path == '/oidc/.well-known/openid-configuration':
            return httpx.Response(200, json={
                'issuer': ISSUER, 'authorization_endpoint': ISSUER + 'authorize',
                'token_endpoint': ISSUER + 'token', 'jwks_uri': ISSUER + 'keys',
                'id_token_signing_alg_values_supported': ['RS256'],
            })
        if request.url.path == '/oidc/keys':
            key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={'keys': [{**key, 'kid': 'current', 'use': 'sig'}]})
        assert request.url.path == '/oidc/token'
        expected = base64.b64encode(f'{CLIENT_ID}:{SECRET}'.encode()).decode()
        assert request.headers['authorization'] == 'Basic ' + expected
        form = parse_qs(request.content.decode())
        assert form['grant_type'] == ['authorization_code']
        assert form['redirect_uri'] == [CALLBACK]
        token, challenge = self.codes.pop(form['code'][0])
        proof = hashlib.sha256(form['code_verifier'][0].encode()).digest()
        assert base64.urlsafe_b64encode(proof).decode().rstrip('=') == challenge
        return httpx.Response(200, json={'id_token': token, 'access_token': 'provider-only'})

    def authorize(self, redirect, subject='user-123', **overrides):
        url = urlsplit(redirect)
        assert url.path == '/oidc/authorize'
        query = {name: values[0] for name, values in parse_qs(url.query).items()}
        assert query['scope'] == 'openid profile custom'
        assert query['code_challenge_method'] == 'S256' and query['code_challenge'] != VERIFIER
        assert query['redirect_uri'] == CALLBACK and query['state'] != STATE
        now = int(time.time())
        claims = dict(iss=ISSUER, sub=subject, aud=CLIENT_ID, iat=now, exp=now+60,
                      nonce=query['nonce'], preferred_username='Owen', groups=[])
        wrong_signature = overrides.pop('wrong_signature', False)
        claims.update(overrides)
        token = jwt.encode(claims, self.wrong_key if wrong_signature else self.key,
                           algorithm='RS256', headers={'kid': 'current'})
        code = secrets.token_urlsafe()
        self.codes[code] = (token, query['code_challenge'])
        return CALLBACK + '?' + urlencode(dict(state=query['state'], code=code))


@pytest.fixture
def sso(tmp_path, monkeypatch):
    settings = Settings(db_path=tmp_path/'oidc.sqlite3', jwt_signing_key='k'*64,
                        mqtt_enabled=False, web_origin=ORIGIN, oidc_issuer=ISSUER,
                        oidc_client_id=CLIENT_ID, oidc_client_secret=SECRET, oidc_scopes='custom',
                        oidc_label='Test identity', _env_file=None)
    store = Store(settings.db_path)
    bootstrap(store, 'Owner', PASSWORD.hash(OWNER_PASSWORD))
    store.close()
    provider = Provider()
    original = httpx.Client
    monkeypatch.setattr(oidc.httpx, 'Client', lambda **kwargs: original(
        transport=httpx.MockTransport(provider.respond), **kwargs))
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as client:
        admin = login(client, 'Owner', OWNER_PASSWORD)
        yield client, app.state.store, provider, admin


def request_provider(client, provider, *, app='dashboard', invitation=None, subject='user-123', **claims):
    transaction, _ = begin(client, app=app, installation='phone-oidc' if app != 'dashboard' else None)
    start = client.post('/auth/oidc/start', json={'transaction': transaction, 'invitation': invitation})
    assert start.status_code == 200, start.text
    assert 'set-cookie' not in start.headers
    return provider.authorize(start.json()['redirect'], subject, **claims)


def exchange(client, response, app='dashboard'):
    assert response.status_code == 303 and 'set-cookie' not in response.headers
    redirect = urlsplit(response.headers['location'])
    callback = ORIGIN + '/auth/callback' if app == 'dashboard' else NATIVE_CALLBACK
    assert redirect._replace(query='').geturl() == callback, response.headers['location']
    query = parse_qs(redirect.query)
    assert query['state'] == [STATE]
    result = client.post('/auth/token', data=dict(grant_type='authorization_code', client_id=app,
                        redirect_uri=callback, code=query['code'][0], code_verifier=VERIFIER))
    assert result.status_code == 200
    if app == 'dashboard':
        return browser_headers(client, result)
    assert 'set-cookie' not in result.headers
    return result.json()


def test_creation_roles_and_native_grant(sso):
    client, store, provider, admin = sso
    assert client.get('/auth/session').json()['oidc_label'] == 'Test identity'
    url = request_provider(client, provider)
    assert 'invitation' in client.get(url, follow_redirects=False).headers['location']
    assert not store.rows('SELECT * FROM oidc_identities')
    assert client.put(POLICY, headers=admin, json={
        'automatic_creation': True, 'group_claim': 'teams', 'admin_groups': ['Airkeep admins'],
    }).status_code == 200
    url = request_provider(client, provider, teams=['airkeep admins'], app='apple-ios')
    tokens = exchange(client, client.get(url, follow_redirects=False), 'apple-ios')
    native = bearer(tokens['access_token'])
    member = client.get('/auth/me', headers=native).json()
    assert member['role'] == 'member' and not member['local_password']
    assert client.get(POLICY, headers=native).status_code == 403
    assert client.post(INVITES, headers=native, json={'username': 'another'}).status_code == 403
    refresh = client.post('/auth/token', data=dict(client_id='apple-ios', grant_type='refresh_token',
                                                  refresh_token=tokens['refresh_token']))
    assert refresh.status_code == 200 and refresh.json()['access_token'] != tokens['access_token']
    url = request_provider(client, provider, teams=['Airkeep admins'], preferred_username='Changed')
    assert client.get('/auth/me', headers=exchange(
        client, client.get(url, follow_redirects=False))).json() == member
    for subject, teams, role in [('admin', ['Airkeep admins'], 'admin'),
                                 ('malformed', ['Airkeep admins', 42], 'member')]:
        url = request_provider(client, provider, subject=subject, teams=teams)
        headers = exchange(client, client.get(url, follow_redirects=False))
        assert client.get('/auth/me', headers=headers).json()['role'] == role
    owner_id = client.get('/auth/me', headers=admin).json()['id']
    assert client.patch(f'/v2/admin/accounts/{owner_id}', headers=admin, json={'role': 'member'}).status_code == 409
    assert client.patch(f'/v2/admin/accounts/{member["id"]}', headers=admin, json={'disabled': True}).status_code == 204
    assert client.get('/auth/me', headers=native).status_code == 401


def test_invitations_expire_replace_revoke_and_redeem_once(sso):
    client, store, provider, admin = sso

    def invite():
        response = client.post(INVITES, headers=admin, json={'username': 'Invited', 'role': 'member'})
        assert response.status_code == 201, response.text
        return response.json()['id'], urlsplit(response.json()['url']).fragment

    first, old = invite()
    assert old not in str([dict(row) for row in store.rows('SELECT * FROM oidc_invitations')])
    assert old not in client.get(INVITES, headers=admin).text
    _, expired = invite()
    assert client.delete(f'{INVITES}/{first}', headers=admin).status_code == 404
    store.execute('UPDATE oidc_invitations SET expires=?', (time.time()-1,))
    revoked, token = invite()
    assert client.delete(f'{INVITES}/{revoked}', headers=admin).status_code == 204
    for invalid in (old, expired, token):
        transaction, _ = begin(client)
        assert client.post('/auth/oidc/start', json={
            'transaction': transaction, 'invitation': invalid,
        }).status_code == 400

    _, token = invite()
    callbacks = [request_provider(client, provider, invitation=token, subject=f'person-{index}') for index in range(2)]
    with ThreadPoolExecutor(max_workers=2) as threads:
        responses = list(threads.map(lambda url: client.get(url, follow_redirects=False), callbacks))
    successes = [response for response in responses if '/auth/callback?' in response.headers['location']]
    assert len(successes) == 1
    headers = exchange(client, successes[0])
    assert client.get('/auth/me', headers=headers).json()['username'] == 'Invited'
    assert len(store.rows('SELECT * FROM oidc_identities')) == 1
    assert client.get(callbacks[0], follow_redirects=False).headers['location'].startswith('/?sso_error=')


def test_invite_links_local_account_without_moving_data(sso):
    client, _, provider, admin = sso
    me = client.get('/auth/me', headers=admin).json()
    rule = client.post('/v2/alerts', headers=admin, json={'name': 'Keep my alert'}).json()
    invitation = client.post(INVITES, headers=admin, json={'account_id': me['id']}).json()
    url = request_provider(client, provider, invitation=urlsplit(invitation['url']).fragment)
    sso_headers = exchange(client, client.get(url, follow_redirects=False))
    assert client.get('/auth/me', headers=sso_headers).json() == me
    assert rule['id'] in {item['id'] for item in client.get('/v2/alerts', headers=sso_headers).json()}
    assert login(client, 'Owner', OWNER_PASSWORD)['Cookie']


@pytest.mark.parametrize('claims', [{'nonce':'wrong'}, {'aud':'wrong'}, {'iss':'https://wrong.test/'},
                                   {'exp':1}, {'wrong_signature':True}])
def test_invalid_id_token_cannot_create_accounts(sso, claims):
    client, store, provider, admin = sso
    client.put(POLICY, headers=admin, json={'automatic_creation': True})
    url = request_provider(client, provider, **claims)
    assert client.get(url, follow_redirects=False).headers['location'].startswith('/?sso_error=')
    assert not store.rows('SELECT * FROM oidc_identities')


def test_state_replay_and_native_local_fallback(sso):
    client, _, provider, _ = sso
    url = request_provider(client, provider, app='apple-mac')
    query = parse_qs(urlsplit(url).query)
    invalid = client.get(CALLBACK, params={'state': 'x'*43, 'code': query['code'][0]}, follow_redirects=False)
    assert invalid.headers['location'].startswith('/?sso_error=')
    denied = client.get(CALLBACK, params={'state': query['state'][0], 'error': 'access_denied'}, follow_redirects=False)
    fallback = urlsplit(denied.headers['location'])
    assert fallback.path == '/connect'
    transaction = parse_qs(fallback.query)['transaction'][0]
    credentials = client.post('/auth/credentials', json={
        'transaction': transaction, 'username': 'Owner', 'password': OWNER_PASSWORD,
    })
    assert credentials.json()['redirect'].startswith(NATIVE_CALLBACK + '?')
    assert client.get(url, follow_redirects=False).headers['location'].startswith('/?sso_error=')
    provider.unavailable = True
    transaction, _ = begin(client)
    assert client.post('/auth/oidc/start', json={'transaction': transaction}).status_code == 502


def test_oidc_dashboard_session_can_approve_display(sso):
    from device_flow import GRANT
    client, store, provider, admin = sso
    assert client.put(POLICY, headers=admin, json={'automatic_creation': True}).status_code == 200
    callback = request_provider(client, provider, subject='display-owner')
    member = exchange(client, client.get(callback, follow_redirects=False))
    display = client.post('/auth/device/code', data={'client_id': 'esp32', 'installation_id': 'c' * 32}).json()
    assert client.post('/auth/device/decision', headers=member,
                       json={'user_code': display['user_code'], 'approve': True}).status_code == 204
    store.execute('UPDATE oauth_device_requests SET next_poll=0')
    result = client.post('/auth/token', data={'client_id': 'esp32', 'grant_type': GRANT,
                                             'device_code': display['device_code']})
    assert result.status_code == 200
    tokens = result.json()
    path = '/auth/grants/' + tokens['grant_id']
    assert client.delete(path, headers=admin).status_code == 404
    assert client.delete(path, headers=member).status_code == 204
