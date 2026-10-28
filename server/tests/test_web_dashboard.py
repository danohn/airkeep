import json
import time

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
import pytest

from app import create_app
from config import Settings
from domain import NamedAlertInput, Reading
from storage import Store
from web_routes import events_page, history_v2, maintain

TOKEN = 'native-bearer-'+'x'*40
PASSWORD = 'browser-owner-password'
HASH = PasswordHasher(time_cost=1,memory_cost=8192).hash(PASSWORD)
ORIGIN = 'https://airkeep.example.com'


def configured(tmp_path, **extra):
    return Settings(api_token=TOKEN,db_path=tmp_path/'data.db',mqtt_enabled=False,
                    web_password_hash=HASH,web_origin=ORIGIN,_env_file=None,**extra)


def sign_in(client):
    result = client.post('/auth/login',json={'password':PASSWORD},headers={'Origin':ORIGIN})
    assert result.status_code == 200
    return {'Origin':ORIGIN,'X-Airkeep-CSRF':result.json()['csrf']}


def test_owner_cookie_scopes_csrf_logout_and_native_compatibility(tmp_path):
    app=create_app(configured(tmp_path))
    with TestClient(app,base_url=ORIGIN) as client:
        assert client.get('/auth/session').json()=={'authenticated':False,'configured':True}
        assert client.get('/v2/admin/devices',headers={'Authorization':'Bearer '+TOKEN}).status_code==401
        assert client.post('/auth/login',json={'password':PASSWORD},headers={'Origin':'https://other.example.com'}).status_code==403
        headers=sign_in(client)
        cookie=client.cookies.get('airkeep_session')
        assert cookie and PASSWORD not in cookie and TOKEN not in cookie
        assert client.get('/v1/snapshot').status_code==200
        assert client.get('/v2/history?hours=168').status_code==200
        assert client.get('/v2/admin/overview').status_code==200
        assert client.post('/v2/alerts',json={}).status_code==403
        assert client.post('/v2/alerts',json={},headers=[(b'origin',ORIGIN.encode()),(b'x-airkeep-csrf',b'\xff')]).status_code==403
        assert client.post('/v2/alerts',json={},headers={**headers,'Origin':'https://other.example.com'}).status_code==403
        rule=client.post('/v2/alerts',json={'name':'Browser rule'},headers=headers).json()
        assert rule['name']=='Browser rule'
        assert client.put('/v2/alerts/'+rule['id'],json={**rule,'name':'Changed'},headers={**headers,'If-Match':'1'}).status_code==200
        assert client.put('/v2/alerts/'+rule['id'],json=rule,headers={**headers,'If-Match':'1'}).status_code==412
        assert client.put('/v2/devices/test',json={'token':'a'*64,'platform':'ios'},headers=headers).status_code==401
        assert client.post('/v1/activities',json={},headers=headers).status_code==401
        assert client.delete('/v2/widgets/test',headers=headers).status_code==401
        assert client.post('/v2/admin/devices',json={},headers=headers).status_code==405
        response=client.post('/auth/logout',headers=headers)
        assert response.status_code==200
        assert client.get('/v2/alerts').status_code==401
        assert client.get('/v1/snapshot',headers={'Authorization':'Bearer '+TOKEN}).status_code==200
        assert len(client.get('/v1/snapshot',headers={'Authorization':'Bearer '+TOKEN}).content)<16384


def test_cookie_attributes_expiry_rotation_and_disabling(tmp_path):
    settings=configured(tmp_path);app=create_app(settings)
    with TestClient(app,base_url=ORIGIN) as client:
        response=client.post('/auth/login',json={'password':PASSWORD},headers={'Origin':ORIGIN})
        cookie=response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'secure' in cookie and 'samesite=strict' in cookie and 'max-age=28800' in cookie
        app.state.store.execute('UPDATE web_sessions SET expires=0')
        assert client.get('/v2/admin/devices').status_code==401
        sign_in(client)
        settings.web_password_hash=PasswordHasher(time_cost=1,memory_cost=8192).hash('another-password')
        assert client.get('/v2/admin/devices').status_code==401
        settings.web_password_hash=HASH
        sign_in(client);settings.web_origin=''
        assert client.get('/v2/admin/devices').status_code==401


def test_login_rate_limit_and_unconfigured_browser(tmp_path):
    app=create_app(configured(tmp_path))
    with TestClient(app,base_url=ORIGIN) as client:
        for _ in range(5):
            assert client.post('/auth/login',json={'password':'wrong'},headers={'Origin':ORIGIN}).status_code==401
        response=client.post('/auth/login',json={'password':PASSWORD},headers={'Origin':ORIGIN})
        assert response.status_code==429 and response.headers['retry-after']=='300'
    settings=configured(tmp_path);settings.web_password_hash=''
    with TestClient(create_app(settings),base_url=ORIGIN) as client:
        assert not client.get('/auth/session').json()['configured']
        assert client.post('/auth/login',json={'password':PASSWORD},headers={'Origin':ORIGIN}).status_code==503


def test_insecure_cookie_configuration_is_loopback_only(tmp_path):
    with pytest.raises(ValueError):
        configured(tmp_path,web_secure_cookies=False)
    assert Settings(api_token=TOKEN,web_origin='http://127.0.0.1:4173',web_secure_cookies=False,_env_file=None)
    with pytest.raises(ValueError):
        Settings(api_token=TOKEN,web_origin='http://public.example.com',web_secure_cookies=False,_env_file=None)


def test_full_week_aggregation_original_timestamps_and_outage_gaps(tmp_path):
    store=Store(tmp_path/'data.db');now=time.time();start=now-7*86400
    values=[]
    for index in range(20161):
        at=start+index*30
        if 4000<=index<4010:
            continue
        reading=Reading(amsID='0',humidity=20+(index%10),temperature=25,rawIndex=2,observedAt=at)
        values.append(('0',at,reading.model_dump_json()))
    with store.lock,store.db:
        store.db.executemany('INSERT INTO samples VALUES(?,?,?)',values)
    history=history_v2(store,'0',168,now,90)
    assert history['bucketSeconds']==300 and len(history['series'])==2
    points=[p for s in history['series'] for p in s['points']]
    assert len(points)<2100
    assert points[-1]['lastObservedAt']==now
    assert points[0]['lastObservedAt']<=start+300
    assert all(p['humidityMin']<=p['humidityMean']<=p['humidityMax'] for p in points)
    assert sum(p['sampleCount'] for p in points)==len(values)
    assert history['series'][0]['points'][-1]['lastObservedAt']<history['series'][1]['points'][0]['lastObservedAt']-90
    assert len(store.history('0',start))==3000  # Legacy contract remains unchanged.
    store.close()


def test_transition_events_are_atomic_independent_and_keep_rule_snapshot(tmp_path):
    store=Store(tmp_path/'data.db');now=time.time()
    rule=store.create_rule(NamedAlertInput(name='Dry box',enabled=True,holdSeconds=0,threshold=30))
    store.save_reading(Reading(amsID='0',humidity=40,temperature=25,rawIndex=2,observedAt=now))
    store.evaluate_alerts(now,90)
    assert not store.rows('SELECT * FROM outbox')
    store.evaluate_alerts(now+1,90)
    assert len(store.rows('SELECT * FROM alert_events'))==1
    store.update_rule(rule['id'],NamedAlertInput(**{**rule,'name':'Renamed'}),1)
    store.save_reading(Reading(amsID='0',humidity=20,temperature=25,rawIndex=2,observedAt=now+30))
    store.evaluate_alerts(now+30,90)
    store.delete_rule(rule['id'],2)
    page=events_page(store,limit=1)
    assert page['events'][0]['type']=='recovered' and page['events'][0]['name']=='Renamed'
    previous=events_page(store,cursor=page['nextCursor'],limit=1)
    assert previous['events'][0]['name']=='Dry box' and previous['events'][0]['condition']['threshold']==30
    assert previous['nextCursor'] is None
    known=next(item for item in previous['filters']['rules'] if item['id']==rule['id'])
    assert known['deleted'] and known['name']=='Renamed'
    assert previous['filters']['amsIDs']==['0']
    assert len(events_page(store,rule_id=rule['id'])['events'])==2
    assert not events_page(store,ams='other')['events']
    for bad in ('not-base64','eA==','WzEsMl0='):
        with pytest.raises(Exception) as error:
            events_page(store,cursor=bad)
        assert error.value.status_code==400
    # A failed insert rolls back both the state transition and its delivery jobs.
    store.db.execute('CREATE TRIGGER reject_event BEFORE INSERT ON alert_events BEGIN SELECT RAISE(ABORT,"test"); END')
    second=store.create_rule(NamedAlertInput(name='Atomic',enabled=True,holdSeconds=0))
    store.save_reading(Reading(amsID='0',humidity=40,temperature=25,rawIndex=2,observedAt=now+60))
    with pytest.raises(Exception): store.evaluate_alerts(now+60,90)
    assert not store.get_rule(second['id'])['active']
    store.close()


def test_admin_redaction_pagination_retention_and_oldest_ams_age(tmp_path):
    app=create_app(configured(tmp_path));store=app.state.store;now=time.time()
    store.upsert_device('phone','a'*64,'io.airkeep','sandbox',True)
    store.save_reading(Reading(amsID='0',humidity=20,temperature=25,rawIndex=2,observedAt=now-600))
    store.save_reading(Reading(amsID='1',humidity=20,temperature=25,rawIndex=2,observedAt=now))
    for i in range(10200):
        store.execute('INSERT INTO push_attempts(at,kind,destination,fingerprint,status) VALUES(?,?,?,?,?)',(now,'alert','phone','fingerprint',200))
    maintain(store,now)
    assert store.rows('SELECT count(*) FROM push_attempts')[0][0]==10000
    with TestClient(app,base_url=ORIGIN) as client:
        sign_in(client)
        overview=client.get('/v2/admin/overview').json()
        assert overview['readingAgeSeconds']>=600
        first=client.get('/v2/admin/push-attempts?limit=2').json()
        second=client.get('/v2/admin/push-attempts',params={'before':first['nextCursor'],'limit':2}).json()
        assert set(r['id'] for r in first['attempts']).isdisjoint(r['id'] for r in second['attempts'])
        responses=''.join(client.get('/v2/admin/'+name).text for name in ('overview','devices','activities','widgets','queue','push-attempts'))
        assert 'a'*64 not in responses and TOKEN not in responses and HASH not in responses and str(tmp_path) not in responses


def test_parallel_password_verification_is_bounded(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    import threading
    from web_auth import BrowserAuth
    config=configured(tmp_path);store=Store(config.db_path);browser=BrowserAuth(store,config)
    guard=threading.Lock();running=0;maximum=0
    class Verifier:
        def verify(self,*args):
            nonlocal running,maximum
            with guard:
                running+=1;maximum=max(maximum,running)
            time.sleep(.01)
            with guard: running-=1
    browser.passwords=Verifier()
    def login(index):
        request=SimpleNamespace(headers={'origin':ORIGIN},client=SimpleNamespace(host=str(index)))
        return browser.login(request,PASSWORD)
    with ThreadPoolExecutor(max_workers=8) as workers:
        results=list(workers.map(login,range(8)))
    assert maximum==1 and len({result[0] for result in results})==8
    store.close()


def test_static_assets_security_headers_and_quoted_password_hash(tmp_path):
    dist=tmp_path/'web';(dist/'assets').mkdir(parents=True);(dist/'brand').mkdir()
    (dist/'index.html').write_text('<!doctype html><title>Airkeep</title>')
    (dist/'assets'/'main.js').write_text('console.log("Airkeep")')
    environment=tmp_path/'relay.env'
    environment.write_text("AIRKEEP_WEB_PASSWORD_HASH='"+HASH+"'\n")
    parsed=Settings(api_token=TOKEN,web_origin=ORIGIN,_env_file=environment)
    assert parsed.web_password_hash==HASH
    app=create_app(configured(tmp_path,web_dist_path=dist))
    with TestClient(app,base_url=ORIGIN) as client:
        response=client.get('/')
        assert response.status_code==200 and '<title>Airkeep</title>' in response.text
        assert response.headers['x-frame-options']=='DENY'
        assert "frame-ancestors 'none'" in response.headers['content-security-policy']
        assert client.get('/assets/main.js').status_code==200
        assert client.get('/assets/no-file').status_code==404
        assert client.get('/auth/session').headers['cache-control']=='no-store'


def test_activity_registration_metadata_preserves_unknown_legacy_creation(tmp_path):
    import sqlite3
    config=configured(tmp_path)
    with sqlite3.connect(config.db_path) as db:
        db.execute('CREATE TABLE activities(id TEXT PRIMARY KEY,token TEXT,environment TEXT,ams TEXT,expires REAL,last_sent REAL DEFAULT 0)')
        db.execute('INSERT INTO activities(id,token,environment,ams,expires) VALUES(?,?,?,?,?)',('old','a'*64,'sandbox','0',time.time()+3600))
    app=create_app(config)
    with TestClient(app,base_url=ORIGIN) as client:
        sign_in(client)
        old=client.get('/v2/admin/activities').json()[0]
        assert old['createdAt'] is None and old['lastRegisteredAt'] is None
        assert old['topic']=='io.airkeep.push-type.liveactivity'
        native={'Authorization':'Bearer '+TOKEN}
        body={'id':'old','token':'a'*64,'environment':'sandbox','amsID':'0','expiresAt':time.time()+3600}
        assert client.post('/v1/activities',json=body,headers=native).status_code==200
        refreshed=client.get('/v2/admin/activities').json()[0]
        assert refreshed['createdAt'] is None and refreshed['lastRegisteredAt'] is not None
        body['id']='new'
        assert client.post('/v1/activities',json=body,headers=native).status_code==200
        created=next(r for r in client.get('/v2/admin/activities').json() if r['id']=='new')
        assert created['createdAt'] and created['lastRegisteredAt']
        original=created['createdAt']
        assert client.post('/v1/activities',json=body,headers=native).status_code==200
        assert next(r for r in client.get('/v2/admin/activities').json() if r['id']=='new')['createdAt']==original
