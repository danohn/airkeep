"""Exercise hosted isolation using the real, restricted PostgreSQL role."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
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
    recent = time.time()-2
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
        assert len(store.history('0', recent-7200)) == 2


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
