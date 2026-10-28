import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import create_app, tick
from config import Settings
from domain import AlertRule, NamedAlertInput, Reading
from storage import RuleConflict, Store

TOKEN = 'test-token-' + 'x' * 40


def sample(humidity, at, ams='0'):
    return Reading(amsID=ams, humidity=humidity, temperature=25.6, rawIndex=2, observedAt=at)


def test_legacy_migration_is_atomic_and_preserves_state_and_subscribers(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE config(id TEXT PRIMARY KEY,json TEXT);
            CREATE TABLE devices(id TEXT PRIMARY KEY,token TEXT,topic TEXT,environment TEXT);
            CREATE TABLE outbox(id INTEGER PRIMARY KEY,device TEXT,json TEXT,attempts INTEGER DEFAULT 0,
                next REAL DEFAULT 0,created REAL DEFAULT 0);''')
        rule = AlertRule(enabled=True,threshold=25,holdSeconds=300)
        db.execute("INSERT INTO config VALUES('rule',?)",(rule.model_dump_json(),))
        db.execute("INSERT INTO config VALUES('alert',?)",(json.dumps({'since':1000,'active':True}),))
        db.execute('INSERT INTO devices VALUES(?,?,?,?)',('phone','a'*64,'example.app','sandbox'))
        db.execute('INSERT INTO outbox(device,json) VALUES(?,?)',('phone','{}'))
    store = Store(path)
    identifier = store.rules()[0]['id']
    assert store.rule() == rule
    assert store.alert().active and store.alert().since == 1000
    assert store.rules()[0]['name'] == 'Alert 1'
    assert store.rows('SELECT receive_alerts FROM devices')[0][0] == 1
    assert store.rows('SELECT rule_id FROM outbox')[0][0] == identifier
    store.close()
    store = Store(path)
    assert len(store.rules()) == 1 and store.rules()[0]['id'] == identifier
    assert store.alert().active
    store.close()


def test_versioned_api_auth_conflicts_and_compact_legacy_snapshot(tmp_path):
    app = create_app(Settings(api_token=TOKEN,db_path=tmp_path/'test.db',mqtt_enabled=False,_env_file=None))
    with TestClient(app) as client:
        assert client.get('/v2/alerts').status_code == 401
        client.headers['Authorization'] = 'Bearer ' + TOKEN
        first = client.get('/v2/alerts').json()[0]
        created = client.post('/v2/alerts',json={}).json()
        assert created['name'] == 'Alert 2' and not created['enabled']
        assert created['id'] != first['id']
        url = '/v2/alerts/' + created['id']
        draft = {**created,'name':'PETG warning','threshold':35}
        assert client.put(url,json=draft).status_code == 428
        assert client.put(url,json=draft,headers={'If-Match':'*'}).status_code == 400
        saved = client.put(url,json=draft,headers={'If-Match':'"1"'})
        assert saved.status_code == 200 and saved.json()['revision'] == 2
        assert saved.headers['ETag'] == '"2"'
        assert client.put(url,json=draft,headers={'If-Match':'"1"'}).status_code == 412
        assert client.delete(url,headers={'If-Match':'"1"'}).status_code == 412
        for index in range(30):
            assert client.post('/v2/alerts',json={'name':f'Rule {index}'}).status_code == 201
        snapshot = client.get('/v1/snapshot')
        assert len(snapshot.content) < 16384
        assert 'alertRules' not in snapshot.json()
        assert client.get('/v1/alerts').json()['threshold'] == first['threshold']
        assert client.put('/v1/alerts',json={}).json()['detail']['code'] == 'client_update_required'
        assert client.delete(url,headers={'If-Match':'"2"'}).status_code == 204
        assert client.get(url).status_code == 404
        assert client.put(url,json=draft,headers={'If-Match':'"2"'}).status_code == 404


def test_independent_rules_stale_hold_recovery_rename_and_restart(tmp_path):
    path = tmp_path/'test.db'
    store = Store(path)
    store.upsert_device('phone','a'*64,'example.app','sandbox',True)
    warning = store.create_rule(NamedAlertInput(name='Warning',enabled=True,threshold=25,holdSeconds=60))
    high = store.create_rule(NamedAlertInput(name='High',enabled=True,threshold=35,holdSeconds=60))
    other = store.create_rule(NamedAlertInput(name='Other AMS',amsID='1',enabled=True,threshold=30,holdSeconds=0))
    store.save_reading(sample(40,1000))
    store.evaluate_alerts(1000,90)
    store.save_reading(sample(40,1060))
    store.evaluate_alerts(1060,90)
    assert len(store.rows('SELECT * FROM outbox')) == 2
    assert store.get_rule(warning['id'])['active'] and store.get_rule(high['id'])['active']
    assert not store.get_rule(other['id'])['active']
    store.update_rule(warning['id'],NamedAlertInput(**{**warning,'name':'Renamed'}),warning['revision'])
    assert store.get_rule(warning['id'])['active']
    store.close()
    store = Store(path)
    store.evaluate_alerts(1065,90)
    assert len(store.rows('SELECT * FROM outbox')) == 2
    store.save_reading(sample(31,1070))
    store.evaluate_alerts(1070,90)
    assert store.get_rule(warning['id'])['active'] and not store.get_rule(high['id'])['active']
    assert len(store.rows('SELECT * FROM outbox')) == 3
    store.evaluate_alerts(1200,90)
    assert store.get_rule(warning['id'])['active']  # Old readings never invent a recovery.
    store.save_reading(sample(20,1201))
    store.save_reading(sample(40,1201,'1'))
    store.evaluate_alerts(1201,90)
    assert not store.get_rule(warning['id'])['active'] and store.get_rule(other['id'])['active']
    titles = [json.loads(r['json'])['aps']['alert']['title'] for r in store.rows('SELECT * FROM outbox')]
    assert any('Renamed: humidity recovered' in title for title in titles)
    store.close()


def test_disable_delete_and_opt_out_remove_only_their_jobs(tmp_path):
    store = Store(tmp_path/'test.db')
    for device in ('phone','mac'):
        store.upsert_device(device,'a'*64,'example.app','sandbox',True)
    first = store.create_rule(NamedAlertInput(name='First',enabled=True,holdSeconds=0))
    second = store.create_rule(NamedAlertInput(name='Second',enabled=True,holdSeconds=0))
    store.save_reading(sample(40,1000))
    store.evaluate_alerts(1000,90)
    store.execute('INSERT INTO activities(id,token,environment,ams,expires) VALUES(?,?,?,?,?)',
                  ('live','b'*64,'sandbox','0',30000))
    store.set_subscription('phone',False)
    assert {r['device'] for r in store.rows('SELECT * FROM outbox')} == {'mac'}
    assert not store.upsert_device('phone','c'*64,'example.app','sandbox',True)
    store.update_rule(first['id'],NamedAlertInput(**{**first,'enabled':False}),first['revision'])
    assert {r['rule_id'] for r in store.rows('SELECT * FROM outbox')} == {second['id']}
    store.delete_rule(second['id'],second['revision'])
    assert not store.rows('SELECT * FROM outbox')
    assert len(store.rows('SELECT * FROM activities')) == 1
    assert store.rows('SELECT receive_alerts FROM devices WHERE id=?',('mac',))[0][0] == 1
    store.close()


def test_simultaneous_edits_allow_only_one_writer(tmp_path):
    path = tmp_path/'test.db'
    first, second = Store(path), Store(path)
    rule = first.rules()[0]
    def save(store, name):
        try:
            store.update_rule(rule['id'],NamedAlertInput(**{**rule,'name':name}),rule['revision'])
            return 'saved'
        except RuleConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(save,first,'Phone'),executor.submit(save,second,'Mac')]
        assert sorted(f.result() for f in futures) == ['conflict','saved']
    first.close()
    second.close()


def test_device_subscription_api_and_legacy_refresh_do_not_undo_opt_out(tmp_path):
    app = create_app(Settings(api_token=TOKEN,db_path=tmp_path/'test.db',mqtt_enabled=False,_env_file=None))
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + TOKEN
        registration = {'token':'a'*64,'platform':'ios','environment':'sandbox'}
        assert not client.put('/v2/devices/phone',json=registration).json()['receiveAlerts']
        assert client.patch('/v2/devices/phone',json={'receiveAlerts':True}).json()['receiveAlerts']
        assert client.put('/v2/devices/phone',json=registration).json()['receiveAlerts']
        assert not client.patch('/v2/devices/phone',json={'receiveAlerts':False}).json()['receiveAlerts']
        assert client.post('/v1/devices',json={**registration,'id':'phone'}).status_code == 200
        assert not client.put('/v2/devices/phone',json={**registration,'initialReceiveAlerts':True}).json()['receiveAlerts']
        assert client.patch('/v2/devices/missing',json={'receiveAlerts':True}).status_code == 404
        status = client.get('/v1/notifications/status',params={'device':'phone','bundle_id':'io.airkeep'}).json()
        assert status['registered'] and status['receiveAlerts'] is False
