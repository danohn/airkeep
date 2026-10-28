import asyncio
import json
import time
from fastapi.testclient import TestClient
import pytest

from app import create_app,tick
from config import Settings
from domain import NamedAlertInput,Reading
from push import PushOutcome
from storage import Store
from widgets import WidgetRegistration,delete_widget,upsert_widget

TOKEN='native-test-'+'x'*40


def settings(tmp_path):
    return Settings(api_token=TOKEN,db_path=tmp_path/'data.db',mqtt_enabled=False,
                    apns_team_id='TEAM',watch_apns_key_id='WATCH',watch_apns_key_path='/missing-test-key',_env_file=None)


def registration(generation=1,**changes):
    return WidgetRegistration(token='a'*64,platform='watch',environment='sandbox',bundleID='io.airkeep.watch',amsIDs=['0'],generation=generation,**changes)


def reading(humidity,at,temperature=25):
    return Reading(amsID='0',humidity=humidity,temperature=temperature,rawIndex=2,observedAt=at)


class Push:
    def __init__(self,result=PushOutcome(200,apns_id='request')):
        self.calls=[];self.result=result;self.callback=None
    async def send_request(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        if self.callback: self.callback()
        return self.result


def test_registry_auth_generation_idempotency_tombstones_and_cookie_isolation(tmp_path):
    app=create_app(settings(tmp_path));url='/v2/widgets/watch'
    with TestClient(app) as client:
        body=registration().model_dump()
        assert client.put(url,json=body).status_code==401
        client.headers['Authorization']='Bearer '+TOKEN
        first=client.put(url,json=body)
        assert first.status_code==200 and first.json()['generation']==1 and first.json()['active']
        assert first.json()['topic']=='io.airkeep.watch.push-type.widgets'
        assert set(first.json())=={'registered','active','generation','topic','configuration','reason'}
        assert client.put(url,json=body).status_code==200
        assert client.put(url,json={**body,'token':'b'*64}).status_code==409
        assert client.put(url,json={**body,'bundleID':'other.topic'}).status_code==422
        assert client.put(url,json={**body,'generation':0}).status_code==422
        assert client.put(url,json={**body,'generation':2,'amsIDs':[]}).json()['active'] is False
        assert client.put(url,json=body).status_code==409
        assert client.get(url).json()['generation']==2
        assert client.delete(url).status_code==200
        assert client.get(url).status_code==404
        conflict=client.put(url,json={**body,'generation':2}).json()['detail']
        assert conflict['deleted'] and conflict['generation']==2
        assert client.put(url,json={**body,'generation':3}).status_code==200
        store=app.state.store
        assert store.rows('SELECT count(*) FROM devices')[0][0]==0


async def test_meaningful_changes_coalesce_and_timestamp_only_updates_do_not_push(tmp_path):
    config=settings(tmp_path);store=Store(config.db_path);upsert_widget(store,'watch',registration(),config);push=Push()
    store.save_reading(reading(21.1,1000,25.01));await tick(store,config,push,1000)
    assert len(push.calls)==1
    args,kwargs=push.calls[0]
    assert args[3]=={'aps':{'content-changed':True}}
    assert kwargs=={'push_type':'widgets','expiration':1300,'collapse_id':'airkeep.widgets'}
    store.save_reading(reading(22.7,1100,25.2));await tick(store,config,push,1100)
    assert len(push.calls)==1
    store.save_reading(reading(23.1,1300,25.3));await tick(store,config,push,1300)
    assert len(push.calls)==2
    store.save_reading(reading(23.2,1800,25.31));await tick(store,config,push,1800)
    assert len(push.calls)==2
    store.save_reading(reading(23.2,5000,25.31));await tick(store,config,push,5000)
    assert len(push.calls)==2  # No scheduled heartbeat or timestamp-only hint.
    await tick(store,config,push,5300)
    assert len(push.calls)==3  # Stale display changes are meaningful.
    assert store.rows('SELECT count(*) FROM push_attempts')[0][0]==3
    store.close()


async def test_retry_after_pause_and_newer_registration_survive_late_results(tmp_path):
    config=settings(tmp_path);store=Store(config.db_path);upsert_widget(store,'watch',registration(),config)
    push=Push(PushOutcome(429,'TooManyRequests','request',900));store.save_reading(reading(20,1000))
    await tick(store,config,push,1000)
    assert store.rows('SELECT next_attempt FROM widget_installations')[0][0]==1900
    store.save_reading(reading(25,1800));await tick(store,config,push,1800)
    assert len(push.calls)==1
    upsert_widget(store,'watch',WidgetRegistration(**{**registration().model_dump(),'generation':2,'amsIDs':[]}),config)
    await tick(store,config,push,2000)
    assert len(push.calls)==1
    upsert_widget(store,'watch',WidgetRegistration(**{**registration().model_dump(),'generation':3}),config)
    push.result=PushOutcome(410,'Unregistered')
    push.callback=lambda:upsert_widget(store,'watch',WidgetRegistration(**{**registration().model_dump(),'generation':4,'token':'b'*64}),config)
    store.save_reading(reading(22,2200));await tick(store,config,push,2200)
    row=store.rows('SELECT * FROM widget_installations')[0]
    assert row['generation']==4 and row['token']=='b'*64 and not row['deleted']
    assert row['last_attempt']==2200
    await tick(store,config,push,2205)
    assert len(push.calls)==2  # A token replacement keeps the five-minute minimum.
    push.callback=None;push.result=PushOutcome(200)
    await tick(store,config,push,2500)
    assert len(push.calls)==3
    delete_widget(store,'watch');await tick(store,config,push,3000)
    assert len(push.calls)==3
    store.close()


async def test_permanent_failures_wait_for_new_registration_and_missing_keys_keep_budget(tmp_path):
    config=settings(tmp_path);store=Store(config.db_path);upsert_widget(store,'watch',registration(),config)
    push=Push(PushOutcome(400,'BadDeviceToken'));store.save_reading(reading(20,1000));await tick(store,config,push,1000)
    store.save_reading(reading(30,100000));await tick(store,config,push,100000)
    assert len(push.calls)==1
    upsert_widget(store,'watch',WidgetRegistration(**{**registration().model_dump(),'generation':2}),config)
    config.watch_apns_key_id=''
    await tick(store,config,push,101000)
    assert len(push.calls)==1 and store.rows('SELECT attempts FROM widget_installations')[0][0]==0
    # Ordinary humidity jobs also wait for their own environment's configured key.
    store.upsert_device('phone','c'*64,'io.airkeep','sandbox',True)
    store.create_rule(NamedAlertInput(name='Limit',enabled=True,holdSeconds=0))
    store.save_reading(reading(40,101000));await tick(store,config,push,101000)
    assert store.rows('SELECT attempts FROM outbox')[0][0]==0
    store.close()


async def test_old_alert_token_410_keeps_replacement_and_redacted_attempt(tmp_path):
    config=settings(tmp_path);config.apns_key_id='IOS';config.apns_key_path='/missing-test-key'
    store=Store(config.db_path);store.upsert_device('phone','a'*64,'io.airkeep','sandbox',True)
    store.create_rule(NamedAlertInput(name='Limit',enabled=True,holdSeconds=0));store.save_reading(reading(40,1000))
    push=Push(PushOutcome(410,'Unregistered','request'))
    push.callback=lambda:store.upsert_device('phone','b'*64,'io.airkeep','sandbox')
    await tick(store,config,push,1000)
    assert store.rows('SELECT token FROM devices')[0][0]=='b'*64
    attempt=store.rows('SELECT * FROM push_attempts')[0]
    assert attempt['reason']=='Unregistered' and attempt['apns_id']=='request'
    assert 'a'*64 not in json.dumps(dict(attempt))
    store.close()


async def test_restart_retries_fixed_provider_authorization_but_not_invalid_tokens(tmp_path):
    config=settings(tmp_path);store=Store(config.db_path)
    upsert_widget(store,'watch',registration(),config)
    upsert_widget(store,'invalid',registration(),config)
    store.execute('UPDATE widget_installations SET last_status=403,last_reason=?,next_attempt=999999 WHERE id=?',('InvalidProviderToken','watch'))
    store.execute('UPDATE widget_installations SET last_status=400,last_reason=? WHERE id=?',('BadDeviceToken','invalid'))
    store.close();store=Store(config.db_path)
    assert store.rows('SELECT last_status FROM widget_installations WHERE id=?',('watch',))[0][0] is None
    assert store.rows('SELECT last_status FROM widget_installations WHERE id=?',('invalid',))[0][0]==400
    store.save_reading(reading(20,1000));push=Push();await tick(store,config,push,1000)
    assert len(push.calls)==1 and push.calls[0][0][0]=='a'*64
    store.close()


async def test_old_410_does_not_remove_a_fresh_registration_of_the_same_native_token(tmp_path,monkeypatch):
    import storage
    config=settings(tmp_path);config.apns_key_id='IOS';config.apns_key_path='/missing-test-key'
    store=Store(config.db_path)
    monkeypatch.setattr(storage.time,'time',lambda:1000)
    store.upsert_device('phone','a'*64,'io.airkeep','sandbox',True)
    store.create_rule(NamedAlertInput(name='Limit',enabled=True,holdSeconds=0));store.save_reading(reading(40,1000))
    def register_again():
        monkeypatch.setattr(storage.time,'time',lambda:1100)
        store.upsert_device('phone','a'*64,'io.airkeep','sandbox')
    push=Push(PushOutcome(410,'Unregistered'));push.callback=register_again
    await tick(store,config,push,1000)
    assert store.rows('SELECT registered_at FROM devices WHERE id=?',('phone',))[0][0]==1100
    store.close()


@pytest.mark.parametrize('status',[200,410])
async def test_late_activity_outcome_keeps_a_fresh_same_token_registration(tmp_path,status):
    config=settings(tmp_path);config.apns_key_id='IOS';config.apns_key_path='/missing-test-key'
    store=Store(config.db_path)
    store.execute('INSERT INTO activities(id,token,environment,ams,expires,registered_at) VALUES(?,?,?,?,?,?)',
                  ('session','a'*64,'sandbox','0',10000,1000))
    push=Push(PushOutcome(status))
    push.callback=lambda:store.execute('UPDATE activities SET registered_at=1100 WHERE id=?',('session',))
    await tick(store,config,push,1000)
    row=store.rows('SELECT * FROM activities WHERE id=?',('session',))[0]
    assert row['registered_at']==1100 and row['last_sent']==0
    store.close()
