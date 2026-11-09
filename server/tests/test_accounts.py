import base64
import hashlib
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from accounts import NATIVE_CALLBACK, PASSWORD, bootstrap, recover
from app import create_app, tick
from config import Settings
from domain import AlertRule, Reading
from push import PushOutcome
from storage import Store

ORIGIN = 'https://airkeep.example.test'
ESP = 't' * 40
OWNER_PASSWORD = 'test owner password'
MEMBER_PASSWORD = 'test member password'
STATE = 's' * 43
VERIFIER = 'v' * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


def bearer(value):
    return {'Authorization': 'Bearer ' + value}


def begin(client, app='dashboard', installation=None, redirect=None):
    callback = ORIGIN + '/auth/callback' if app == 'dashboard' else NATIVE_CALLBACK
    payload = dict(client_id=app, redirect_uri=redirect or callback, response_type='code',
                   state=STATE, code_challenge=CHALLENGE, code_challenge_method='S256')
    if installation:
        payload['installation_id'] = installation
    response = client.post('/auth/authorize', json=payload)
    assert response.status_code == 200, response.text
    return response.json()['transaction'], callback


def code_form(client, username, password, *, new_password=None, app='dashboard', installation=None):
    transaction, callback = begin(client, app, installation)
    form = dict(username=username, password=password, transaction=transaction)
    if new_password:
        form['new_password'] = new_password
    response = client.post('/auth/credentials', json=form)
    assert response.status_code == 200, response.text
    assert 'set-cookie' not in response.headers
    data = response.json()
    if data.get('passwordChangeRequired'):
        return data
    redirect = urlsplit(data['redirect'])
    assert redirect.path == urlsplit(callback).path
    query = parse_qs(redirect.query)
    assert query['state'] == [STATE]
    return dict(client_id=app, grant_type='authorization_code', code=query['code'][0],
                redirect_uri=callback, code_verifier=VERIFIER)


def login(client, username, password, **kwargs):
    form = code_form(client, username, password, **kwargs)
    response = client.post('/auth/token', data=form)
    assert response.status_code == 200, response.text
    assert 'set-cookie' not in response.headers
    return response.json()


def account_app(settings):
    settings = settings.model_copy(update={'jwt_signing_key': 'x' * 64, 'web_origin': ORIGIN})
    store = Store(settings.db_path)
    try:
        bootstrap(store, 'Owner', PASSWORD.hash(OWNER_PASSWORD))
    finally:
        store.close()
    return create_app(settings)


def account_headers(client, app='dashboard', installation=None):
    client.headers['Origin'] = ORIGIN
    return bearer(login(client, 'Owner', OWNER_PASSWORD, app=app, installation=installation)['access_token'])


@pytest.fixture
def relay(tmp_path):
    settings = Settings(db_path=tmp_path/'relay.sqlite3', jwt_signing_key='x'*64,
                        mqtt_enabled=False, web_origin=ORIGIN, _env_file=None)
    store = Store(settings.db_path)
    store.set_rule(AlertRule(enabled=True, threshold=29, holdSeconds=0))
    store.save_reading(Reading(amsID='0', humidity=40, temperature=24, rawIndex=None, observedAt=time.time()))
    store.evaluate_alerts(time.time(),90)
    old_rule = store.rules()[0]
    store.upsert_device('legacy','f'*64,'io.airkeep','sandbox',True)
    backup_path = tmp_path/'rollback.sqlite3'
    with sqlite3.connect(backup_path) as backup:
        store.db.backup(backup)
    bootstrap(store,'Owner',PASSWORD.hash(OWNER_PASSWORD))
    store.close()
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN, headers={'Origin':ORIGIN}) as client:
        yield client, app.state.store, old_rule, backup_path


def test_bootstrap_is_reversible_and_rejects_the_old_shared_token(relay):
    client, store, old_rule, backup_path = relay
    rollback = Store(backup_path)
    assert rollback.rules() == [old_rule]
    assert rollback.rows('SELECT id FROM devices')[0]['id'] == 'legacy'
    rollback.close()
    assert not store.rows('SELECT * FROM devices')
    assert client.get('/auth/session').json()['mode'] == 'oauth'
    assert client.post('/auth/login',json={'password':OWNER_PASSWORD}).status_code == 404
    snapshot = client.get('/v1/snapshot',headers=bearer(ESP))
    assert snapshot.status_code == 401
    assert client.get('/v2/alerts',headers=bearer(ESP)).status_code == 401
    assert client.get('/v1/history',headers=bearer(ESP)).status_code == 401
    admin = bearer(login(client,'owner',OWNER_PASSWORD)['access_token'])
    assert client.get('/v2/alerts',headers=admin).json() == [old_rule]
    assert client.get('/v2/alert-events',headers=admin).json()['events'][0]['ruleID'] == old_rule['id']
    recover(store,'OWNER',PASSWORD.hash('recovered administrator'))
    assert client.get('/auth/me',headers=admin).status_code == 401
    assert login(client,'Owner','recovered administrator')['access_token']


def test_oauth_pkce_rotation_replay_and_revoke(relay):
    client, _, _, _ = relay
    form = code_form(client,'Owner',OWNER_PASSWORD)
    assert client.post('/auth/token',data={**form,'code_verifier':'w'*43}).status_code == 400
    assert client.post('/auth/token',data={**form,'redirect_uri':'https://attacker.example/'}).status_code == 400
    response = client.post('/auth/token',data=form)
    assert response.status_code == 200 and response.json()['expires_in'] == 600
    assert client.post('/auth/token',data=form).status_code == 400
    first = response.json()
    refresh = dict(grant_type='refresh_token',client_id='dashboard',refresh_token=first['refresh_token'])
    second = client.post('/auth/token',data=refresh).json()
    assert second['refresh_token'] != first['refresh_token']
    assert client.get('/auth/me',headers=bearer(second['access_token'])).status_code == 200
    assert client.post('/auth/token',data=refresh).status_code == 400
    assert client.get('/auth/me',headers=bearer(second['access_token'])).status_code == 401
    assert client.post('/auth/token',data={**refresh,'refresh_token':second['refresh_token']}).status_code == 400
    assert client.post('/auth/authorize',json={
        'client_id':'dashboard','redirect_uri':'https://attacker.example/','response_type':'code',
        'state':STATE,'code_challenge':CHALLENGE,'code_challenge_method':'S256',
    }).status_code == 400


def test_accounts_own_alerts_events_and_registrations(relay):
    client, store, old_rule, _ = relay
    admin = bearer(login(client,'Owner',OWNER_PASSWORD)['access_token'])
    created = client.post('/v2/admin/accounts',json={'username':'Member'},headers=admin)
    assert created.status_code == 201
    member_id, temporary = created.json()['id'], created.json()['temporaryPassword']
    assert client.post('/v2/admin/accounts',json={'username':'mEmBeR'},headers=admin).status_code == 409
    assert code_form(client,'Member',temporary) == {'passwordChangeRequired':True}
    member = bearer(login(client,'Member',temporary,new_password=MEMBER_PASSWORD)['access_token'])
    assert client.get('/v2/admin/overview',headers=member).status_code == 403
    assert client.get('/v2/alerts',headers=member).json() == []
    assert client.get('/v2/alerts/'+old_rule['id'],headers=member).status_code == 404
    assert client.delete('/v2/alerts/'+old_rule['id'],headers={**member,'If-Match':'2'}).status_code == 404
    assert client.get('/v2/alert-events',headers=member).json()['events'] == []
    rule = client.post('/v2/alerts',json={'name':'My rule','enabled':True,'holdSeconds':0},headers=member).json()
    iphone = bearer(login(client,'Member',MEMBER_PASSWORD,app='apple-ios',installation='member-phone')['access_token'])
    owner_phone = bearer(login(client,'Owner',OWNER_PASSWORD,app='apple-ios',installation='owner-phone')['access_token'])
    device = dict(token='a'*64,platform='ios',environment='sandbox',initialReceiveAlerts=True)
    assert client.put('/v2/devices/member-phone',json=device,headers=iphone).status_code == 200
    assert client.put('/v2/devices/owner-phone',json={**device,'token':'b'*64},headers=owner_phone).status_code == 200
    assert client.patch('/v2/devices/member-phone',json={'receiveAlerts':False},headers=owner_phone).status_code == 404
    store.evaluate_alerts(time.time(),90,multiuser=True)
    jobs = store.rows('SELECT device FROM outbox WHERE rule_id=?',(rule['id'],))
    assert [row['device'] for row in jobs] == ['member-phone']
    assert client.get('/v2/alert-events',headers=member).json()['events'][0]['ruleID'] == rule['id']
    assert all(event['ruleID'] != rule['id'] for event in client.get('/v2/alert-events',headers=admin).json()['events'])
    watch = bearer(client.post('/auth/watch',headers=iphone,json={'installation_id':'watch-id'}).json()['access_token'])
    assert client.get('/v1/snapshot',headers=watch).status_code == 200
    assert client.post('/v2/alerts',json={'name':'No'},headers=watch).status_code == 403
    assert client.post('/auth/revoke',headers=iphone).status_code == 204
    assert client.get('/v1/snapshot',headers=watch).status_code == 401
    assert not store.rows('SELECT * FROM outbox WHERE device=?',('member-phone',))
    assert client.patch('/v2/admin/accounts/'+member_id,json={'disabled':True},headers=admin).status_code == 204
    assert client.get('/v2/alerts',headers=member).status_code == 401
    owner_id = client.get('/auth/me',headers=admin).json()['id']
    assert client.patch('/v2/admin/accounts/'+owner_id,json={'role':'member'},headers=admin).status_code == 409


def test_expired_temporary_password_cannot_start_enrollment(relay):
    client, store, _, _ = relay
    admin = bearer(login(client,'Owner',OWNER_PASSWORD)['access_token'])
    new = client.post('/v2/admin/accounts',json={'username':'Expired'},headers=admin).json()
    store.execute('UPDATE accounts SET temporary_until=? WHERE id=?',(time.time()-1,new['id']))
    tx, _ = begin(client)
    result = client.post('/auth/credentials',json={'transaction':tx,'username':'Expired','password':new['temporaryPassword']})
    assert result.status_code == 401


async def test_watch_receives_its_own_copy_of_the_owners_alert(tmp_path):
    settings = Settings(db_path=tmp_path/'watch.db', mqtt_enabled=False, apns_team_id='TEAM',
                        apns_key_id='PHONE', apns_key_path='/phone-key',
                        watch_apns_key_id='WATCH', watch_apns_key_path='/watch-key', _env_file=None)
    app = account_app(settings)
    store = app.state.store
    push = app.state.push
    push.send_request = AsyncMock(return_value=PushOutcome(200))
    # Keep the background tick off so the test controls when readings are evaluated.
    client = TestClient(app)
    try:
        phone = account_headers(client, 'apple-ios', 'phone')
        dashboard = account_headers(client)
        grant = client.post('/auth/watch', headers=phone, json={'installation_id':'watch'})
        assert grant.status_code == 200
        watch = bearer(grant.json()['access_token'])
        phone_body = dict(platform='ios', token='a'*64, initialReceiveAlerts=True)
        watch_body = dict(platform='watch', token='b'*64)
        assert client.put('/v2/devices/phone', json=phone_body, headers=phone).json()['receiveAlerts']
        assert client.put('/v2/devices/watch', json=watch_body, headers=phone).status_code == 403
        assert client.put('/v2/devices/watch', json=phone_body, headers=watch).status_code == 403
        assert client.put('/v2/devices/another-watch', json=watch_body, headers=watch).status_code == 404
        assert not client.put('/v2/devices/watch', json=watch_body, headers=watch).json()['receiveAlerts']
        assert client.patch('/v2/devices/watch', json={'receiveAlerts':True}, headers=watch).json()['receiveAlerts']
        rotated = {**watch_body, 'token':'c'*64}
        assert client.put('/v2/devices/watch', json=rotated, headers=watch).json()['receiveAlerts']
        status = client.get('/v1/notifications/status', headers=watch,
                            params={'device':'watch','platform':'watch','bundle_id':'io.airkeep.watch'}).json()
        assert status['registered'] and status['receiveAlerts']
        assert client.post('/v2/alerts', headers=dashboard,
                           json={'name':'Watch test','enabled':True,'holdSeconds':0}).status_code == 201

        now = time.time()
        store.save_reading(Reading(amsID='0', humidity=80, temperature=None, rawIndex=None, observedAt=now))
        await tick(store, settings, push, now, multiuser=True)
        deliveries = {call.args[1]: call for call in push.send_request.await_args_list}
        assert set(deliveries) == {'io.airkeep', 'io.airkeep.watch'}
        phone_delivery, watch_delivery = deliveries['io.airkeep'], deliveries['io.airkeep.watch']
        assert watch_delivery.args[0] == rotated['token']
        assert watch_delivery.kwargs['push_type'] == 'alert'
        assert watch_delivery.args[3] == phone_delivery.args[3]
        assert watch_delivery.args[3]['aps']['alert']['title'].startswith('Watch test:')

        assert not client.patch('/v2/devices/watch', json={'receiveAlerts':False}, headers=watch).json()['receiveAlerts']
        assert not client.put('/v2/devices/watch', json=watch_body, headers=watch).json()['receiveAlerts']
        push.send_request.reset_mock()
        store.save_reading(Reading(amsID='0', humidity=10, temperature=None, rawIndex=None, observedAt=now+1))
        await tick(store, settings, push, now+1, multiuser=True)
        assert [call.args[1] for call in push.send_request.await_args_list] == ['io.airkeep']
        assert client.delete('/auth/watch', headers=phone).status_code == 204
        assert not store.rows("SELECT 1 FROM devices WHERE id='watch'")
    finally:
        client.close()
        store.close()
        await push.close()


def test_expiry_logout_and_password_change_stop_existing_access(relay):
    client, store, _, _ = relay
    tokens = login(client,'Owner',OWNER_PASSWORD)
    headers = bearer(tokens['access_token'])
    assert client.post('/auth/password',headers=headers,json={
        'password':OWNER_PASSWORD,'new_password':'new owner password',
    }).status_code == 204
    assert client.get('/auth/me',headers=headers).status_code == 401
    next_tokens = login(client,'Owner','new owner password')
    store.execute('UPDATE oauth_grants SET idle_until=? WHERE id=?',(time.time()-1,next_tokens['grant_id']))
    assert client.get('/auth/me',headers=bearer(next_tokens['access_token'])).status_code == 401
    assert client.post('/auth/token',data={
        'grant_type':'refresh_token','client_id':'dashboard','refresh_token':next_tokens['refresh_token'],
    }).status_code == 400


def test_native_authorization_redirect_and_grant_ownership(relay):
    client, _, _, _ = relay
    response = client.get('/auth/authorize',params=dict(
        client_id='apple-ios',redirect_uri=NATIVE_CALLBACK,response_type='code',
        state=STATE,code_challenge=CHALLENGE,code_challenge_method='S256',installation_id='phone'),
        follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'].startswith('/connect?transaction=')
    assert 'set-cookie' not in response.headers
    tokens = login(client,'Owner',OWNER_PASSWORD)
    admin = bearer(tokens['access_token'])
    assert client.get('/auth/grants',headers=admin).json()[0]['current']
    assert client.delete('/auth/grants/'+tokens['grant_id'],headers=admin).status_code == 204
    assert client.get('/auth/me',headers=admin).status_code == 401


def test_concurrent_refresh_detects_replay_and_bootstrap_cannot_repeat(relay):
    from concurrent.futures import ThreadPoolExecutor
    from accounts import OAuth
    client, store, _, _ = relay
    tokens = login(client,'Owner',OWNER_PASSWORD)
    settings = Settings(jwt_signing_key='x'*64,web_origin=ORIGIN,mqtt_enabled=False,_env_file=None)
    oauth = OAuth(store,settings)
    form = dict(client_id='dashboard',grant_type='refresh_token',refresh_token=tokens['refresh_token'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _:oauth.exchange(form),range(2)))
    assert sum(result is not None for result in outcomes) == 1
    assert oauth.active(tokens['grant_id']) is None
    with pytest.raises(ValueError,match='already exist'):
        bootstrap(store,'Another',PASSWORD.hash('another administrator'))
    assert client.get('/auth/me',headers=bearer(tokens['access_token'])).status_code == 401


def test_code_expiry_wrong_origin_and_unsupported_pkce_are_rejected(relay):
    client, store, _, _ = relay
    form = code_form(client,'Owner',OWNER_PASSWORD)
    store.execute('UPDATE oauth_requests SET expires=?',(time.time()-1,))
    assert client.post('/auth/token',data=form).status_code == 400
    tx, _ = begin(client)
    assert client.post('/auth/credentials',json={
        'transaction':tx,'username':'Owner','password':OWNER_PASSWORD,
    },headers={'Origin':'https://another.example'}).status_code == 403
    assert client.post('/auth/authorize',json=dict(
        client_id='dashboard',redirect_uri=ORIGIN+'/auth/callback',response_type='code',
        state=STATE,code_challenge=CHALLENGE,code_challenge_method='plain',
    )).status_code == 422
