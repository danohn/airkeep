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
