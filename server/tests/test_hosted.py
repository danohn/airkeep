"""Exercise hosted isolation using the real, restricted PostgreSQL role."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import psycopg
import pytest

from accounts import OAuth, PASSWORD
from config import Settings
from domain import NamedAlertInput
from hosted_ingest import Batch, enroll_relay, relay_status, save_batch
from postgres import PostgresStore

URL = os.environ.get('AIRKEEP_TEST_DATABASE_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Set AIRKEEP_TEST_DATABASE_URL to an isolated PostgreSQL runtime database.')
ORIGIN = 'https://dashboard.airkeep.io'
HOSTED_ADMIN = '/v2/admin/hosted'


@pytest.fixture
def store():
    value = PostgresStore(URL)
    yield value
    value.close()


@pytest.fixture
def users(store):
    identifiers = [str(uuid4()), str(uuid4())]
    for identifier in identifiers:
        store.execute("INSERT INTO accounts(id,username,hash,role) VALUES(?,?,?,'member')",
                      (identifier, identifier+'@example.com', PASSWORD.hash('correct horse battery staple')))
    yield identifiers
    for identifier in identifiers:
        store.execute('DELETE FROM accounts WHERE id=?', (identifier,))


def observation(humidity=45, *, when=None, replay=False, event=None):
    return {'eventID': event or str(uuid4()), 'replay': replay, 'amsID': '0',
            'humidity': humidity, 'temperature': 24, 'rawIndex': 2,
            'observedAt': when if when is not None else time.time()-1}


def enroll(store, user):
    from accounts import digest
    return digest(enroll_relay(store, user)['access_token'])


def test_database_policies_and_connection_reuse_fail_closed(store, users):
    first, second = users
    with store.tenant(first):
        store.create_rule(NamedAlertInput(name='Private'), first)
        assert len(store.rows('SELECT * FROM airkeep_data.alert_rules')) == 1
    assert store.rows('SELECT * FROM alert_rules') == []
    assert store.rows('SELECT * FROM airkeep_data.alert_rules') == []
    with store.tenant(second):
        assert store.rows('SELECT * FROM alert_rules') == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            store.execute('''INSERT INTO airkeep_data.alert_rules
                (tenant_id,id,name,revision,json,state,created,owner_id) VALUES(?,?,?,1,?,?,?,?)''',
                (first, str(uuid4()), 'Wrong tenant', '{}', '{}', time.time(), first))
    with store.tenant(first):
        assert len(store.rules(first)) == 1
    assert store.rows('SELECT * FROM alert_rules') == []


def test_identical_ams_ids_are_isolated_and_replay_is_history_only(store, users):
    first, second = users
    a, b = enroll(store, first), enroll(store, second)
    # Keep both live observations in the previous 30-second history bucket.
    recent = int(time.time() // 30) * 30 - 5
    save_batch(store, a, Batch(printerConnected=True, readings=[observation(40, when=recent)]))
    save_batch(store, b, Batch(printerConnected=True, readings=[observation(70, when=recent)]))
    with store.tenant(first):
        assert store.readings()[0].humidity == 40
        assert relay_status(store)['status'] == 'connected'
    with store.tenant(second):
        assert store.readings()[0].humidity == 70
    replay = observation(80, when=recent-3600, replay=True)
    save_batch(store, a, Batch(readings=[replay]))
    save_batch(store, a, Batch(readings=[replay]))
    save_batch(store, a, Batch(readings=[observation(55, when=recent-1)]))
    with store.tenant(first):
        assert store.readings()[0].humidity == 40
        assert len(store.rows('SELECT * FROM ingest_events')) == 3
        assert [sample['humidity'] for sample in store.history('0', recent-7200)] == [80, 40]


def test_heartbeat_does_not_refresh_sensor_and_reenrollment_revokes_old_token(store, users):
    key = enroll(store, users[0])
    observed = time.time()-3
    save_batch(store, key, Batch(printerConnected=True, readings=[observation(when=observed)]))
    save_batch(store, key, Batch(printerConnected=True))
    with store.tenant(users[0]):
        assert store.readings()[0].observedAt == observed
    enroll(store, users[0])
    with pytest.raises(HTTPException) as error:
        save_batch(store, key, Batch(printerConnected=True))
    assert error.value.status_code == 401


def test_small_clock_skew_preserves_live_observation_time(store, users):
    key = enroll(store, users[0])
    observed = time.time()+15
    save_batch(store, key, Batch(printerConnected=True, readings=[observation(when=observed)]))
    with store.tenant(users[0]):
        assert store.readings()[0].observedAt == observed


def test_alert_hold_restarts_after_observation_gap():
    from domain import AlertRule, AlertState, Reading

    state = AlertState()
    rule = AlertRule(enabled=True, holdSeconds=300)
    previous = Reading(amsID='0', humidity=80, temperature=None, rawIndex=None, observedAt=1000)
    assert state.evaluate(rule, previous, 1000, 90, require_continuity=True) is None
    resumed = previous.model_copy(update={'observedAt': 4600})
    assert state.evaluate(rule, resumed, 4600, 90, require_continuity=True) is None
    assert state.since == 4600


def test_cross_tenant_delivery_foreign_keys_and_generic_push(store, users):
    first, second = users
    oauth = OAuth(store, Settings(jwt_signing_key='x'*64, web_origin=ORIGIN, _env_file=None))
    key = enroll(store, first)
    save_batch(store, key, Batch(printerConnected=True, readings=[observation(80)]))
    with store.tenant(first), store.transaction():
        grant = oauth.grant(first, 'apple-ios', 'same-installation')
        store.upsert_device('same-installation', 'a'*64, 'io.airkeep', 'sandbox', True)
        store.execute('INSERT INTO registration_owners VALUES(?,?,?,?)',
                      ('device', 'same-installation', grant['grant_id'], first))
        rule = store.create_rule(NamedAlertInput(name='Private spool', enabled=True, holdSeconds=0), first)
        store.evaluate_alerts(time.time(), 90, True)
        payload = json.loads(store.rows('SELECT json FROM outbox')[0][0])
        assert 'Private spool' not in json.dumps(payload)
        assert '80' not in json.dumps(payload)
        job = store.rows('SELECT id FROM outbox')[0][0]
    def claim(_):
        with store.tenant(first), store.transaction():
            return store.claim_delivery('outbox', job)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, range(2))) == 1
    with store.tenant(second):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            store.execute('INSERT INTO outbox(device,json,next,created,rule_id) VALUES(?,?,?,?,?)',
                          ('same-installation', '{}', time.time(), time.time(), rule['id']))
        assert store.rows('SELECT * FROM devices') == []


@pytest.mark.asyncio
async def test_hosted_worker_pauses_alerts_on_disconnect_and_runs_maintenance(store, users):
    from app import tick
    from push import APNs

    user = users[0]
    key = enroll(store, user)
    now = time.time()
    save_batch(store, key, Batch(printerConnected=True, readings=[observation(80, when=now-1)]))
    settings = Settings(jwt_signing_key='x'*64, web_origin=ORIGIN, _env_file=None)
    push = APNs(settings)
    try:
        with store.tenant(user):
            rule = store.create_rule(NamedAlertInput(enabled=True, holdSeconds=20), user)
            await tick(store, settings, push, now, True)
            assert store.get_rule(rule['id'], user)['active'] is False
        save_batch(store, key, Batch(printerConnected=False))
        with store.tenant(user):
            await tick(store, settings, push, now+30, True)
            assert store.get_rule(rule['id'], user)['active'] is False
            assert store.rows('SELECT * FROM alert_events') == []
    finally:
        await push.close()


def test_concurrent_refresh_replay_revokes_grant(store, users):
    oauth = OAuth(store, Settings(jwt_signing_key='x'*64, web_origin=ORIGIN, _env_file=None))
    with store.tenant(users[0]), store.transaction():
        grant = oauth.grant(users[0], 'apple-ios', str(uuid4()))
    data = {'client_id': 'apple-ios', 'grant_type': 'refresh_token', 'refresh_token': grant['refresh_token']}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: oauth.exchange(data), range(2)))
    assert sum(result is not None for result in results) == 1
    assert oauth.active(grant['grant_id']) is None


def test_hosted_api_normal_entrypoints_scope_every_client(store, users, monkeypatch):
    from app import create_app
    from cryptography.fernet import Fernet
    monkeypatch.setenv('AIRKEEP_EMAIL_LINK_KEY', Fernet.generate_key().decode())
    settings = Settings(deployment='hosted', database_url=URL, jwt_signing_key='x'*64,
                        web_origin=ORIGIN, mqtt_enabled=False, _env_file=None)
    app = create_app(settings)
    oauth = OAuth(store, settings)
    clients = []
    for user in users:
        with store.tenant(user), store.transaction():
            clients.append(oauth.grant(user, 'apple-ios', str(uuid4()))['access_token'])
    a, b = enroll(store, users[0]), enroll(store, users[1])
    save_batch(store, a, Batch(printerConnected=True, readings=[observation(35)]))
    save_batch(store, b, Batch(printerConnected=True, readings=[observation(75)]))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get('/v1/snapshot').status_code == 401
        for token, humidity in zip(clients, (35, 75)):
            headers = {'Authorization': 'Bearer '+token}
            assert client.get('/v1/snapshot', headers=headers).json()['readings'][0]['humidity'] == humidity
            assert client.get('/v1/history', headers=headers).json()[0]['humidity'] == humidity
            assert client.get('/v2/history', headers=headers).json()['series'][0]['points'][0]['humidityMean'] == humidity
            assert client.get('/v2/admin/devices', headers=headers).status_code == 403
        assert client.get('/auth/session').json()['hosted'] is True
        assert client.get('/auth/session').json()['public_signup'] is False
        with store.tenant(users[1]), store.transaction():
            cookie = oauth.grant(users[1], 'dashboard', None)['access_token']
        client.cookies.set(oauth.cookie_name, cookie, domain='dashboard.airkeep.io', path='/')
        assert client.get('/v1/snapshot').json()['readings'][0]['humidity'] == 75
        lower_case = {'Authorization': 'bearer '+clients[0]}
        assert client.get('/v1/snapshot', headers=lower_case).json()['readings'][0]['humidity'] == 35
        assert client.get('/v1/snapshot', headers={'Authorization': 'Basic invalid'}).status_code == 401


@pytest.fixture
def hosted_admin(store, users, monkeypatch):
    from app import create_app
    from cryptography.fernet import Fernet
    from hosted_admin import set_hosted_role

    monkeypatch.setenv('AIRKEEP_EMAIL_LINK_KEY', Fernet.generate_key().decode())
    monkeypatch.setenv('AIRKEEP_TWILIO_API_KEY', 'test-key')
    monkeypatch.setenv('AIRKEEP_TWILIO_API_SECRET', 'test-secret')
    monkeypatch.setenv('AIRKEEP_EMAIL_FROM', 'noreply@example.com')
    settings = Settings(deployment='hosted', database_url=URL, jwt_signing_key=uuid4().hex,
                        web_origin=ORIGIN, public_signup=False, ios_topic='io.airkeep', mac_topic='io.airkeep',
                        _env_file=None)
    app = create_app(settings)
    oauth = app.state.hosted_accounts.oauth
    with store.transaction():
        browser = [oauth.grant(user, 'dashboard', None)['access_token'] for user in users]
    set_hosted_role(store, f' {users[0].upper()}@EXAMPLE.COM ', 'admin')
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app, browser


def sign_in(client, oauth, cookie):
    client.cookies.set(oauth.cookie_name, cookie, domain='dashboard.airkeep.io', path='/')


def test_hosted_admin_uses_live_roles_and_only_dashboard_sessions(store, users, hosted_admin):
    from hosted_admin import set_hosted_role

    client, app, browser = hosted_admin
    oauth = app.state.hosted_accounts.oauth
    actor, member = users
    path = HOSTED_ADMIN+'/health'
    assert client.get(path).status_code == 401
    sign_in(client, oauth, browser[1])
    assert client.get(path).status_code == 403
    assert client.post(HOSTED_ADMIN+f'/accounts/{actor}/suspend', headers={'Origin': ORIGIN}).status_code == 403
    with store.transaction():
        grants = [oauth.grant(actor, name, 'device-'+name) for name in ('apple-ios', 'apple-mac', 'esp32')]
    sign_in(client, oauth, browser[0])
    for grant in grants:
        assert client.get(path, headers={'Authorization': 'Bearer '+grant['access_token']}).status_code == 403
    assert client.get(path, headers={'Authorization': 'Bearer '+browser[0]}).status_code == 401
    assert client.get(path).status_code == 200
    assert client.get('/v2/admin/overview').status_code == 403
    target = HOSTED_ADMIN+f'/accounts/{member}/suspend'
    assert client.post(target).status_code == 403
    assert client.post(target, headers={'Origin': 'https://other.example.com'}).status_code == 403
    for action in ('suspend', 'reactivate', 'reset'):
        assert client.post(HOSTED_ADMIN+f'/accounts/{actor}/{action}', headers={'Origin': ORIGIN}).status_code == 409

    assert set_hosted_role(store, actor+'@example.com', 'member') == {'accountID': actor, 'role': 'member'}
    assert client.get(path).status_code == 403
    assert client.get('/auth/session').json()['account']['role'] == 'member'
    assert all(oauth.active(grant['grant_id']) for grant in grants)
    set_hosted_role(store, actor+'@example.com', 'admin')
    assert client.get(path).status_code == 200
    with pytest.raises(HTTPException) as missing:
        set_hosted_role(store, 'missing@example.com', 'admin')
    assert missing.value.status_code == 404


def test_hosted_admin_rechecks_actor_before_lifecycle_write(store, users, hosted_admin, monkeypatch):
    client, app, browser = hosted_admin
    oauth = app.state.hosted_accounts.oauth
    sign_in(client, oauth, browser[0])
    original = oauth.browser_session

    def resolve_before_demotion(request):
        identity = original(request)
        store.execute("UPDATE accounts SET role='member' WHERE id=?", (identity.user,))
        return identity

    monkeypatch.setattr(oauth, 'browser_session', resolve_before_demotion)
    response = client.post(HOSTED_ADMIN+f'/accounts/{users[1]}/suspend', headers={'Origin': ORIGIN})
    assert response.status_code == 403
    assert store.rows('SELECT disabled FROM accounts WHERE id=?', (users[1],))[0][0] == 0


def test_hosted_admin_diagnostics_are_paginated_scoped_and_redacted(store, users, hosted_admin, caplog):
    from push import PushOutcome
    from web_routes import record_attempt

    client, app, browser = hosted_admin
    sign_in(client, app.state.hosted_accounts.oauth, browser[0])
    caplog.set_level(logging.INFO, logger='uvicorn.error')
    now = time.time()
    request_id = str(uuid4())
    token = 'a'*64
    for user, device, humidity in zip(users, ('actor-only', 'member-only'), (30, 75)):
        save_batch(store, enroll(store, user), Batch(printerConnected=True, readings=[observation(humidity)]))
        with store.tenant(user):
            store.upsert_device(device, token, 'io.airkeep', 'production', True)
            rule = store.create_rule(NamedAlertInput(name='private rule'), user)
            store.execute('INSERT INTO outbox(device,json,next,created,rule_id) VALUES(?,?,?,?,?)',
                          (device, '{"private":"delivery payload"}', now, now, rule['id']))
            store.execute('INSERT INTO activities(id,token,environment,ams,expires) VALUES(?,?,?,?,?)',
                          (device, token, 'production', 'private-ams', now+60))
            store.execute('''INSERT INTO widget_installations(id,generation,token,environment,bundle_id,ams_ids,last_reason)
                             VALUES(?,1,?,?,?,?,?)''', (device, token, 'production', 'io.airkeep.watch', '["0"]', token))
            record_attempt(store, now, 'alert', device, token, 'production', 'io.airkeep', PushOutcome(410, 'Unregistered', request_id))
            record_attempt(store, now, 'alert', device, token, 'production', 'io.airkeep', PushOutcome(500, token, token))

    health = client.get(HOSTED_ADMIN+'/health').json()
    assert health['database']['status'] == 'ok' and health['database']['bytes'] > 0
    assert len(health['pushSetup']) == 6 and health['email']['configured'] is True
    accounts = []
    cursor = ''
    while cursor is not None:
        page = client.get(HOSTED_ADMIN+'/accounts', params={'cursor': cursor, 'limit': 1}).json()
        accounts.extend(page['accounts'])
        cursor = page['nextCursor']
    assert [row['id'] for row in accounts if row['id'] in users] == sorted(users)
    assert all(set(row) == {'id', 'email', 'role', 'disabled'} for row in accounts)

    account_path = HOSTED_ADMIN+f'/accounts/{users[1]}'
    overview = client.get(account_path+'/overview').json()
    assert overview['account']['id'] == users[1] and overview['printerConnected'] is True
    assert overview['relayStatus'] == 'connected'
    assert overview['counts'] == {'samples': 1, 'devices': 1, 'activities': 1, 'alert_rules': 1, 'outbox': 1}
    for route, key in (('devices', 'id'), ('activities', 'id'), ('widgets', 'id'), ('queue', 'deviceID')):
        result = client.get(account_path+'/'+route)
        assert result.status_code == 200 and [row[key] for row in result.json()] == ['member-only']
        assert not any(value in result.text for value in (token, 'actor-only', 'private-ams', 'delivery payload', 'Fingerprint', 'amsIDs', 'ruleID'))
    page = client.get(account_path+'/push-attempts?limit=1').json()
    assert page['attempts'][0]['reason'] == 'provider_error' and page['attempts'][0]['apns_id'] is None
    page = client.get(account_path+'/push-attempts', params={'before': page['nextCursor'], 'limit': 1}).json()
    assert page['nextCursor'] is None
    assert page['attempts'][0]['reason'] == 'Unregistered' and page['attempts'][0]['apns_id'] == request_id
    assert page['attempts'][0]['destination'] == 'member-only'
    assert 'fingerprint' not in page['attempts'][0] and 'tenant_id' not in page['attempts'][0]
    assert client.get(HOSTED_ADMIN+'/accounts/missing/overview').status_code == 404
    assert app.state.store.rows('SELECT id FROM airkeep_data.devices') == []
    assert client.get('/v1/snapshot').json()['readings'][0]['humidity'] == 30
    events = [json.loads(record.message) for record in caplog.records if record.name == 'uvicorn.error']
    assert {event['action'] for event in events if event['targetAccountID'] == users[1]} == {
        'overview', 'devices', 'activities', 'widgets', 'push-attempts', 'queue',
    }
    assert all(event['actorAccountID'] == users[0] and event['outcome'] == 'read' for event in events)
    assert not any(value in caplog.text for value in (token, 'member-only', 'private-ams', '@example.com'))


def test_hosted_admin_distinguishes_ios_and_mac_with_shared_push_topic(store, users, hosted_admin):
    client, app, browser = hosted_admin
    oauth = app.state.hosted_accounts.oauth
    sign_in(client, oauth, browser[0])

    for user, platform, token in zip(users, ('ios', 'mac'), ('a'*64, 'b'*64)):
        with store.transaction():
            native = oauth.grant(user, f'apple-{platform}', 'shared-device')['access_token']
        registration = client.put('/v2/devices/shared-device', headers={'Authorization': 'Bearer '+native},
                                  json={'platform': platform, 'environment': 'production', 'token': token})
        assert registration.status_code == 200

    for user, platform in zip(users, ('ios', 'mac')):
        devices = client.get(HOSTED_ADMIN+f'/accounts/{user}/devices').json()
        assert [(device['id'], device['platform'], device['topic']) for device in devices] == [
            ('shared-device', platform, 'io.airkeep'),
        ]


def test_hosted_admin_suspension_revokes_access_and_keeps_tenant_history(store, users, hosted_admin):
    from accounts import digest

    client, app, browser = hosted_admin
    oauth = app.state.hosted_accounts.oauth
    sign_in(client, oauth, browser[0])
    user = users[1]
    path = HOSTED_ADMIN+f'/accounts/{user}'
    origin = {'Origin': ORIGIN}
    now = time.time()
    old_relay = enroll(store, user)
    save_batch(store, old_relay, Batch(printerConnected=True, readings=[observation(75)]))
    with store.tenant(user), store.transaction():
        ios = oauth.grant(user, 'apple-ios', 'phone')
        watch = oauth.grant(user, 'watch', 'watch', parent=oauth.active(ios['grant_id']))
        store.upsert_device('phone', 'b'*64, 'io.airkeep', 'production', True)
        store.execute('INSERT INTO registration_owners VALUES(?,?,?,?)', ('device', 'phone', ios['grant_id'], user))
        fired = store.create_rule(NamedAlertInput(name='Historical', enabled=True, holdSeconds=0), user)
        holding = store.create_rule(NamedAlertInput(name='Still holding', enabled=True, holdSeconds=300), user)
        store.evaluate_alerts(now, 90, True)
        store.execute('''INSERT INTO oauth_device_requests
                         (device_hash,user_hash,installation,expires,next_poll,poll_interval,decision,owner,owner_version)
                         VALUES(?,?,?,?,?,5,'approved',?,0)''', (digest('device'), digest('user-code'), 'display', now+300, now, user))
        assert store.rows('SELECT COUNT(*) FROM outbox')[0][0] == 1
    assert client.post(path+'/reset', headers=origin).status_code == 202
    reset_hash = store.rows('SELECT hash FROM hosted_account_links WHERE account_id=?', (user,))[0][0]
    assert client.post(path+'/suspend', headers=origin).status_code == 204
    assert client.post(path+'/suspend', headers=origin).status_code == 204
    assert client.post(path+'/reset', headers=origin).status_code == 409
    assert store.rows('SELECT disabled,version FROM accounts WHERE id=?', (user,))[0] == {'disabled': 1, 'version': 1}
    assert not store.rows('SELECT 1 FROM oauth_device_requests WHERE owner=?', (user,))
    assert not store.rows('SELECT 1 FROM hosted_account_links WHERE account_id=?', (user,))
    assert not store.rows('SELECT 1 FROM hosted_email_outbox WHERE link_hash=? AND sealed_secret IS NOT NULL', (reset_hash,))
    assert not oauth.active(ios['grant_id']) and not oauth.active(watch['grant_id'])
    assert client.get('/v1/snapshot', headers={'Authorization': 'Bearer '+ios['access_token']}).status_code == 401
    with pytest.raises(HTTPException) as revoked:
        save_batch(store, old_relay, Batch(printerConnected=True))
    assert revoked.value.status_code == 401
    with store.tenant(user):
        assert not store.printer_connected(time.time())
        assert len(store.readings()) == 1 and store.rows('SELECT COUNT(*) FROM samples')[0][0] == 1
        assert store.rows('SELECT COUNT(*) FROM alert_events')[0][0] == 1
        assert not store.rows('SELECT id FROM devices') and not store.rows('SELECT id FROM outbox')
        rows = {row['id']: json.loads(row['state']) for row in store.rows('SELECT id,state FROM alert_rules')}
        assert rows[fired['id']] == {'active': True, 'since': None, 'last_observed': None}
        assert rows[holding['id']] == {'active': False, 'since': None, 'last_observed': None}
        assert store.get_rule(holding['id'], user)['revision'] == holding['revision']
    assert client.post(path+'/reactivate', headers=origin).status_code == 204
    assert store.rows('SELECT disabled,version FROM accounts WHERE id=?', (user,))[0] == {'disabled': 0, 'version': 2}
    sign_in(client, oauth, browser[1])
    assert client.get('/v1/snapshot').status_code == 401
    with store.transaction():
        new_browser = oauth.grant(user, 'dashboard', None)['access_token']
    sign_in(client, oauth, new_browser)
    assert client.get('/v1/snapshot').json()['printerConnected'] is False
    assert client.get('/v1/snapshot', headers={'Authorization': 'Bearer '+ios['access_token']}).status_code == 401
    assert enroll(store, user) != old_relay


def test_hosted_admin_email_links_reuse_quotas_and_audit_without_secrets(store, users, hosted_admin, monkeypatch, caplog):
    from hosted_accounts import limited
    import hosted_admin_routes

    client, app, browser = hosted_admin
    service = app.state.hosted_accounts
    sign_in(client, service.oauth, browser[0])
    headers = {'Origin': ORIGIN}
    now = time.time()
    monkeypatch.setattr(hosted_admin_routes, 'time', SimpleNamespace(time=lambda: now, monotonic=time.monotonic))
    caplog.set_level(logging.INFO, logger='uvicorn.error')
    email = f'{uuid4()}@example.com'
    member = users[1]
    stored_email = member+'@example.com'
    target = HOSTED_ADMIN+f'/accounts/{member}'

    def link_for(address):
        row = store.rows('''SELECT l.*,o.sealed_secret FROM hosted_account_links l
                            JOIN hosted_email_outbox o ON l.hash=o.link_hash WHERE l.email=?''', (address,))[0]
        return row, service.cipher.decrypt(row['sealed_secret'].encode()).decode()

    try:
        assert client.post('/auth/signup', headers=headers, json={'email': email}).status_code == 200
        assert not store.rows('SELECT hash FROM hosted_account_links WHERE email=?', (email,))
        invite_path = HOSTED_ADMIN+'/invitations'
        assert client.post(invite_path, headers=headers, json={'email': email, 'role': 'admin'}).status_code == 422
        invite = client.post(invite_path, headers=headers, json={'email': f' {email.upper()} '})
        assert invite.status_code == 202 and invite.json() == {'message': 'Invitation email queued.'}
        signup, signup_token = link_for(email)
        assert signup['kind'] == 'signup' and signup['expires']-signup['created'] == 3600
        assert not store.rows('SELECT 1 FROM accounts WHERE username=?', (email,))
        signup_body = {'token': signup_token, 'password': 'a long signup password'}
        assert client.post('/auth/signup/complete', headers=headers, json=signup_body).status_code == 200
        assert client.post('/auth/signup/complete', headers=headers, json=signup_body).status_code == 400
        assert store.rows('SELECT role FROM accounts WHERE username=?', (email,))[0][0] == 'member'
        assert client.post(invite_path, headers=headers, json={'email': email}).status_code == 409

        before = store.rows('SELECT hash,version FROM accounts WHERE id=?', (member,))[0]
        reset = client.post(target+'/reset', headers=headers, json={'email': 'unrelated@example.com'})
        assert reset.status_code == 202 and reset.json() == {'message': 'Password reset email queued.'}
        reset_link, reset_token = link_for(stored_email)
        assert reset_link['account_id'] == member and reset_link['account_version'] == before['version']
        assert reset_link['kind'] == 'reset' and reset_link['expires']-reset_link['created'] == 1200
        assert store.rows('SELECT hash,version FROM accounts WHERE id=?', (member,))[0] == before
        assert service.oauth.authenticate(browser[1], browser=True).user == member

        with store.transaction() as db:
            for _ in range(3):
                assert limited(db, service.oauth.key, 'email-account', stored_email, 4, 900, now)
        limited_reset = client.post(target+'/reset', headers=headers)
        assert limited_reset.status_code == 429 and limited_reset.headers['Retry-After'] == '900'
        with store.transaction() as db:
            for _ in range(17):
                assert limited(db, service.oauth.key, 'email-admin', users[0], 20, 900, now)
        assert client.post(invite_path, headers=headers, json={'email': f'{uuid4()}@example.com'}).status_code == 429
        reset_body = {'token': reset_token, 'password': 'a long replacement password'}
        assert client.post('/auth/password/reset', headers=headers, json=reset_body).status_code == 200
        assert client.post('/auth/password/reset', headers=headers, json=reset_body).status_code == 400
        with pytest.raises(HTTPException):
            service.oauth.authenticate(browser[1], browser=True)
        events = [json.loads(record.message) for record in caplog.records if record.name == 'uvicorn.error']
        assert [(event['action'], event['outcome']) for event in events] == [
            ('invite', 'queued'), ('reset', 'queued'), ('reset', 'rate_limited'), ('invite', 'rate_limited'),
        ]
        assert all(event['actorAccountID'] == users[0] for event in events)
        assert events[1]['targetAccountID'] == member and events[0]['targetAccountID'] is None
        assert all(secret not in caplog.text for secret in (email, stored_email, signup_token, reset_token))
    finally:
        store.execute('DELETE FROM accounts WHERE username=?', (email,))
        store.execute('DELETE FROM hosted_account_links WHERE email=?', (email,))


def test_hosted_account_deletion_requires_and_removes_authenticator(store, users, monkeypatch):
    from app import create_app
    from cryptography.fernet import Fernet
    import pyotp

    monkeypatch.setenv('AIRKEEP_EMAIL_LINK_KEY', Fernet.generate_key().decode())
    settings = Settings(deployment='hosted', database_url=URL, jwt_signing_key='x'*64,
                        web_origin=ORIGIN, _env_file=None)
    oauth = OAuth(store, settings)
    user = users[0]
    with store.transaction():
        cookie = oauth.grant(user, 'dashboard', None)['access_token']
    with TestClient(create_app(settings), base_url=ORIGIN, headers={'Origin': ORIGIN}) as client:
        client.cookies.set(oauth.cookie_name, cookie, domain='dashboard.airkeep.io', path='/')
        password = {'password': 'correct horse battery staple'}
        assert client.post('/auth/security/passkeys/options', json=password).status_code == 200
        setup = client.post('/auth/security/totp/setup', json=password)
        assert setup.status_code == 200, setup.text
        code = pyotp.TOTP(setup.json()['secret']).now()
        enabled = client.post('/auth/security/totp', json={'code': code})
        assert enabled.status_code == 200, enabled.text
        deletion = {**password, 'confirmation': 'DELETE'}
        assert client.request('DELETE', '/auth/account', json=deletion).status_code == 401
        deletion['verification_code'] = enabled.json()['recovery_codes'][0]
        assert client.request('DELETE', '/auth/account', json=deletion).status_code == 204
    for table in ('account_totp', 'account_recovery_codes', 'account_auth_challenges'):
        assert not store.rows(f'SELECT 1 FROM {table} WHERE account_id=?', (user,))
