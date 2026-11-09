from urllib.parse import urlsplit, parse_qs
import secrets

from device_flow import GRANT
from test_accounts import ORIGIN, OWNER_PASSWORD, bearer, login, relay


def start(client, installation='a' * 32):
    response = client.post('/auth/device/code', data={'client_id': 'esp32', 'installation_id': installation})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def redeem(client, pair):
    return client.post('/auth/token', data={'client_id': 'esp32', 'grant_type': GRANT,
                                          'device_code': pair['device_code']})


def ready(store):
    store.execute("UPDATE oauth_device_requests SET next_poll=0")


def test_pairing_grant_is_single_use_and_scoped(relay):
    client, store, *_ = relay
    owner = login(client, 'Owner', OWNER_PASSWORD)
    pair = start(client)
    assert pair['verification_uri'] == ORIGIN + '/device'
    assert parse_qs(urlsplit(pair['verification_uri_complete']).fragment)['user_code'] == [pair['user_code']]
    assert pair['device_code'] not in pair['verification_uri_complete']
    saved = dict(store.rows('SELECT * FROM oauth_device_requests')[0])
    assert pair['device_code'] not in saved.values() and pair['user_code'] not in saved.values()
    assert redeem(client, pair).json()['error'] == 'slow_down'
    ready(store)
    assert redeem(client, pair).json()['error'] == 'authorization_pending'
    code = {'user_code': pair['user_code'].lower().replace('-', ' ')}
    assert client.post('/auth/device/verify', json=code).status_code == 401
    assert client.post('/auth/device/verify', json=code, headers=owner).json()['user_code'] == pair['user_code']
    assert client.post('/auth/device/decision', json={**code, 'approve': True},
                       headers={**owner, 'Origin': 'https://other.example.test'}).status_code == 403
    assert client.post('/auth/device/decision', json={**code, 'approve': True}, headers=owner).status_code == 204
    ready(store)
    response = redeem(client, pair)
    assert response.status_code == 200
    tokens = response.json()
    assert redeem(client, pair).json()['error'] == 'invalid_grant'
    display = bearer(tokens['access_token'])
    snapshot = client.get('/v1/snapshot', headers=display)
    assert snapshot.status_code == 200 and len(snapshot.content) < 16384
    assert set(snapshot.json()) == {'serverTime', 'staleAfter', 'printerConnected', 'readings'}
    for path in ['/auth/me', '/v1/history', '/v2/history', '/v2/alerts', '/v2/admin/overview']:
        assert client.get(path, headers=display).status_code == 403
    assert client.post('/auth/device/decision', json={**code, 'approve': True}, headers=display).status_code == 403
    assert client.get('/v1/snapshot', headers=bearer(secrets.token_urlsafe())).status_code == 401
    grants = client.get('/auth/grants', headers=owner).json()
    assert any(row['client'] == 'esp32' and row['display_suffix'] == 'aaaa' for row in grants)
    form = {'grant_type': 'refresh_token', 'client_id': 'esp32', 'refresh_token': tokens['refresh_token']}
    renewed = client.post('/auth/token', data=form).json()
    assert renewed['refresh_token'] != tokens['refresh_token']
    assert client.delete('/auth/grants/' + tokens['grant_id'], headers=owner).status_code == 204
    assert client.get('/v1/snapshot', headers=bearer(renewed['access_token'])).status_code == 401
    assert client.post('/auth/token', data={**form, 'refresh_token': renewed['refresh_token']}).json()['error'] == 'invalid_grant'


def test_pairing_denial_expiry_and_guess_limits(relay):
    client, store, *_ = relay
    owner = login(client, 'Owner', OWNER_PASSWORD)
    pair = start(client)
    assert client.post('/auth/device/decision', headers=owner,
                       json={'user_code': pair['user_code'], 'approve': False}).status_code == 204
    assert redeem(client, pair).json()['error'] == 'access_denied'
    pair = start(client)
    store.execute('UPDATE oauth_device_requests SET expires=0')
    assert redeem(client, pair).json()['error'] == 'expired_token'
    assert client.post('/auth/device/verify', headers=owner,
                       json={'user_code': pair['user_code']}).status_code == 404
    for _ in range(18):
        assert client.post('/auth/device/verify', headers=owner, json={'user_code': 'AAAAAAAA'}).status_code == 404
    assert client.post('/auth/device/verify', headers=owner, json={'user_code': 'AAAAAAAA'}).status_code == 429


def test_display_lifetimes_and_self_revocation(relay):
    import time
    from accounts import DAY
    client, store, *_ = relay
    owner = login(client, 'Owner', OWNER_PASSWORD)
    pair = start(client)
    assert client.post('/auth/device/decision', json={'user_code': pair['user_code'], 'approve': True},
                       headers=owner).status_code == 204
    ready(store)
    tokens = redeem(client, pair).json()
    assert 0 < tokens['expires_in'] <= 600
    grant = store.rows('SELECT expires,idle_until FROM oauth_grants WHERE id=?', (tokens['grant_id'],))[0]
    assert abs(grant['expires'] - time.time() - 90 * DAY) < 10
    assert abs(grant['idle_until'] - time.time() - 30 * DAY) < 10
    headers = bearer(tokens['access_token'])
    assert client.post('/auth/revoke', headers=headers).status_code == 204
    assert client.get('/v1/snapshot', headers=headers).status_code == 401
