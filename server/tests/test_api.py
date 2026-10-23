import time
from fastapi.testclient import TestClient
from app import create_app, tick
from config import Settings
from domain import AlertRule, Reading
from storage import Store

TOKEN="test-token-"+"x"*40

def test_auth_snapshot_history_and_rule(tmp_path):
    settings=Settings(api_token=TOKEN,db_path=tmp_path/"test.db",mqtt_enabled=False,_env_file=None)
    app=create_app(settings)
    with TestClient(app) as client:
        assert client.get('/v1/snapshot').status_code==401
        client.headers['Authorization']='Bearer '+TOKEN
        app.state.store.save_reading(Reading(amsID='0',humidity=24,temperature=31.6,rawIndex=3,observedAt=time.time()))
        assert client.get('/v1/snapshot').json()['readings'][0]['humidity']==24
        assert len(client.get('/v1/history').json())==1
        assert client.put('/v1/alerts',json={'enabled':True,'threshold':30,'holdSeconds':300,'hysteresis':3}).status_code==200
        assert client.put('/v1/alerts',json={'threshold':5,'hysteresis':6}).status_code==422
        assert client.post('/v1/activities',json={'id':'test','token':'a'*64,'expiresAt':time.time()+86400}).status_code==422

class Push:
    def __init__(self): self.calls=[]; self.status=503
    async def send(self,*args,**kwargs): self.calls.append((args,kwargs)); return self.status

def test_eight_hour_activity_registration_and_token_rotation_preserve_expiry(tmp_path):
    settings=Settings(api_token=TOKEN,db_path=tmp_path/'test.db',mqtt_enabled=False,_env_file=None)
    app=create_app(settings)
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer '+TOKEN
        now=time.time()
        activity={'id':'session','token':'a'*64,'expiresAt':now+8*3600}
        assert client.post('/v1/activities',json=activity).status_code==200
        activity.update(token='b'*64,expiresAt=now+8*3600+10)
        assert client.post('/v1/activities',json=activity).status_code==200
        row=app.state.store.rows('SELECT token,expires FROM activities WHERE id=?',('session',))[0]
        assert row['token']=='b'*64
        assert row['expires']==now+8*3600
        for expiry in (now-1,now+8*3600+120):
            activity['expiresAt']=expiry
            assert client.post('/v1/activities',json=activity).status_code==422

async def test_live_updates_remain_fresh_between_pushes_and_end_on_time(tmp_path):
    settings=Settings(api_token=TOKEN,db_path=tmp_path/'test.db',mqtt_enabled=False,
        apns_team_id='T',apns_key_id='K',apns_key_path='not-read',_env_file=None)
    store=Store(settings.db_path)
    try:
        store.execute('INSERT INTO activities(id,token,environment,ams,expires) VALUES(?,?,?,?,?)',
            ('session','a'*64,'sandbox','0',1000+8*3600))
        push=Push()
        push.status=200
        store.save_reading(Reading(amsID='0',humidity=24,temperature=25.6,rawIndex=2,observedAt=1000))
        await tick(store,settings,push,1000)
        first=push.calls[0][0][3]['aps']
        await tick(store,settings,push,1299)
        assert len(push.calls)==1
        store.save_reading(Reading(amsID='0',humidity=25,temperature=25.6,rawIndex=2,observedAt=1300))
        await tick(store,settings,push,1300)
        second=push.calls[1][0][3]['aps']
        assert first['stale-date'] > second['timestamp']
        assert second['content-state']['connected'] is True
        # A missing sensor update cannot be kept fresh by the push schedule.
        await tick(store,settings,push,1600)
        old=push.calls[2][0][3]['aps']
        assert old['content-state']['connected'] is False
        assert old['stale-date']==1600
        await tick(store,settings,push,1000+8*3600)
        assert push.calls[-1][0][3]['aps']['event']=='end'
        assert not store.rows('SELECT * FROM activities')
    finally:
        store.close()

async def test_outbox_retries_without_repeating_alert(tmp_path):
    settings=Settings(api_token=TOKEN,db_path=tmp_path/'test.db',mqtt_enabled=False,apns_team_id='T',apns_key_id='K',apns_key_path='not-read',_env_file=None)
    store=Store(settings.db_path)
    store.set_rule(AlertRule(enabled=True,holdSeconds=0))
    store.execute('INSERT INTO devices VALUES(?,?,?,?)',('one','a'*64,'com.example.app','sandbox'))
    store.save_reading(Reading(amsID='0',humidity=35,temperature=31.6,rawIndex=2,observedAt=1000))
    push=Push()
    await tick(store,settings,push,1000)
    assert len(push.calls)==1
    assert len(store.rows('SELECT * FROM outbox'))==1
    await tick(store,settings,push,1005)
    assert len(push.calls)==1
    push.status=200
    await tick(store,settings,push,1031)
    assert len(push.calls)==2
    assert not store.rows('SELECT * FROM outbox')
    store.close()
