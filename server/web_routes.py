"""Browser-only owner session and read-only operational views."""
import base64
import binascii
import hashlib
import json
from pathlib import Path
import time

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from web_auth import COOKIE, LIFETIME
from widgets import display_fingerprint


def token_fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else None


def record_attempt(store, now, kind, destination, token, environment, topic, outcome):
    store.execute('''INSERT INTO push_attempts(at,kind,destination,fingerprint,environment,topic,status,reason,apns_id)
        VALUES(?,?,?,?,?,?,?,?,?)''', (now,kind,destination,token_fingerprint(token),environment,topic,
                                      outcome.status,outcome.reason,outcome.apns_id))


async def send_recorded(store, push, token, topic, environment, payload, *, now, kind, destination,
                        expiration=None, collapse_id=None):
    from push import PushOutcome
    try:
        if hasattr(push, 'send_request'):
            result = await push.send_request(token,topic,environment,payload,
                push_type=kind,expiration=expiration,collapse_id=collapse_id)
        else:
            # Keep existing in-process push doubles and integer adapter compatible.
            result = PushOutcome(await push.send(token,topic,environment,payload,live=kind=='liveactivity'))
    except Exception:
        result = PushOutcome(None, 'connection_failed')
    record_attempt(store,now,kind,destination,token,environment,topic,result)
    return result


def maintain(store, now):
    store.execute('DELETE FROM web_sessions WHERE expires<=?', (now,))
    store.execute('DELETE FROM alert_events WHERE at<?', (now-7*86400,))
    store.execute('DELETE FROM push_attempts WHERE at<?', (now-7*86400,))
    store.execute('DELETE FROM push_attempts WHERE id NOT IN (SELECT id FROM push_attempts ORDER BY id DESC LIMIT 10000)')


def history_v2(store, ams, hours, now, stale):
    start = now-hours*3600
    raw = [json.loads(r[0]) for r in store.rows(
        'SELECT json FROM samples WHERE ams=? AND at>=? AND at<=? ORDER BY at', (ams,start,now))]
    width = 300 if hours > 24 else 30
    runs = []
    for reading in raw:
        if not runs or reading['observedAt']-runs[-1][-1]['observedAt'] > stale:
            runs.append([])
        runs[-1].append(reading)
    series = []
    for run in runs:
        groups = {}
        for reading in run:
            bucket = int(reading['observedAt']//width)*width
            groups.setdefault(bucket,[]).append(reading)
        points = []
        for bucket, readings in groups.items():
            values = [r['humidity'] for r in readings]
            points.append({'bucketStart':bucket,'humidityMean':sum(values)/len(values),
                           'humidityMin':min(values),'humidityMax':max(values),
                           'sampleCount':len(values),'lastObservedAt':readings[-1]['observedAt']})
        series.append({'points':points})
    return {'amsID':ams,'from':start,'to':now,'bucketSeconds':width,'series':series}


def events_page(store, *, ams=None, rule_id=None, cursor=None, limit=50):
    where, args = ['at>=?'], [time.time()-7*86400]
    if ams is not None:
        where.append('ams=?'); args.append(ams)
    if rule_id is not None:
        where.append('rule_id=?'); args.append(rule_id)
    if cursor:
        try:
            at, identifier = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(at,(int,float)) or not isinstance(identifier,str) or len(identifier)>80:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise HTTPException(400, 'Invalid event cursor.') from None
        where.append('(at<? OR (at=? AND id<?))'); args.extend((at,at,identifier))
    rows = store.rows('SELECT * FROM alert_events WHERE '+' AND '.join(where)+' ORDER BY at DESC,id DESC LIMIT ?', (*args,limit+1))
    selected = rows[:limit]
    next_cursor = None
    if len(rows)>limit:
        last = selected[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last['at'],last['id']]).encode()).decode()
    current={rule['id']:rule for rule in store.rules()}
    known={identifier:{'id':identifier,'name':rule['name'],'deleted':False} for identifier,rule in current.items()}
    event_rules=store.rows("SELECT rule_id,json_extract(json,'$.name') AS name,MAX(at) FROM alert_events WHERE at>=? GROUP BY rule_id",(time.time()-7*86400,))
    for row in event_rules:
        known.setdefault(row['rule_id'],{'id':row['rule_id'],'name':row['name'],'deleted':True})
    units={reading.amsID for reading in store.readings()} | {rule['amsID'] for rule in current.values()}
    units.update(row[0] for row in store.rows('SELECT DISTINCT ams FROM alert_events WHERE at>=?',(time.time()-7*86400,)))
    return {'events':[json.loads(r['json']) for r in selected], 'nextCursor':next_cursor,
            'filters':{'amsIDs':sorted(units),'rules':sorted(known.values(),key=lambda rule:(rule['name'].casefold(),rule['id']))}}


class Login(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


def install_web(app, store, settings, auth, authorize_user, collector):
    started = time.time()

    @app.middleware('http')
    async def browser_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith(('/v1/','/v2/','/auth/')):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/auth/session')
    def session(request: Request):
        try:
            row = auth.session(request)
        except HTTPException:
            return {'authenticated':False,'configured':auth.configured}
        return {'authenticated':True,'configured':True,'csrf':row['csrf'],'expiresAt':row['expires']}

    @app.post('/auth/login')
    def login(body: Login, request: Request, response: Response):
        if not auth.configured:
            raise HTTPException(503, 'Set up the browser password on your relay.')
        token, csrf = auth.login(request,body.password)
        response.set_cookie(COOKIE,token,max_age=LIFETIME,httponly=True,secure=settings.web_secure_cookies,
                            samesite='strict',path='/')
        return {'authenticated':True,'csrf':csrf,'expiresAt':time.time()+LIFETIME}

    @app.post('/auth/logout', dependencies=[Depends(auth.require)])
    def logout(request: Request, response: Response):
        store.execute('DELETE FROM web_sessions WHERE id=?',(auth.digest(request.cookies.get(COOKIE,'')),))
        response.delete_cookie(COOKIE,path='/',secure=settings.web_secure_cookies,httponly=True,samesite='strict')
        return {'authenticated':False}

    @app.get('/v2/history', dependencies=[Depends(authorize_user)])
    def history(ams: str=Query(default='0',min_length=1,max_length=8),hours: int=Query(default=1,ge=1,le=168)):
        return history_v2(store,ams,hours,time.time(),settings.stale_seconds)

    @app.get('/v2/alert-events', dependencies=[Depends(authorize_user)])
    def events(ams: str | None=None,rule_id: str | None=None,cursor: str | None=Query(default=None,max_length=500),
               limit: int=Query(default=50,ge=1,le=100)):
        return events_page(store,ams=ams,rule_id=rule_id,cursor=cursor,limit=limit)

    @app.get('/v2/admin/overview', dependencies=[Depends(auth.require)])
    def overview():
        latest = store.readings()
        now = time.time()
        checks = []
        for platform, topic in [('ios',settings.ios_topic),('mac',settings.mac_topic),('watch',settings.watch_topic)]:
            for environment in ('sandbox','production'):
                configuration, reason = settings.notification_configuration(platform,environment,topic)
                checks.append({'platform':platform,'topic':topic,'environment':environment,'configuration':configuration,'reason':reason})
        return {'version':app.version,'uptimeSeconds':now-started,'printerConnected':bool(collector and collector.connected),
                'readingAgeSeconds': max((now-r.observedAt for r in latest),default=None),
                'databaseBytes':store.rows('PRAGMA page_count')[0][0]*store.rows('PRAGMA page_size')[0][0],
                'historyDays':7,'eventHistoryDays':7,'pushSetup':checks,
                'counts':{table:store.rows('SELECT COUNT(*) FROM '+table)[0][0]
                    for table in ('samples','devices','activities','alert_rules','outbox')},
                'timelineStartedAt':json.loads(store.rows("SELECT json FROM config WHERE id='events_started_at'")[0][0])}

    @app.get('/v2/admin/devices', dependencies=[Depends(auth.require)])
    def devices():
        return [{'id':r['id'],'platform':'mac' if r['topic']==settings.mac_topic else 'ios',
                 'topic':r['topic'],'environment':r['environment'],'receiveAlerts':bool(r['receive_alerts']),
                 'tokenFingerprint':token_fingerprint(r['token']),'createdAt':r['created_at'],'lastRegisteredAt':r['registered_at']}
                for r in store.rows('SELECT * FROM devices ORDER BY registered_at DESC')]

    @app.get('/v2/admin/activities', dependencies=[Depends(auth.require)])
    def activities():
        return [{'id':r['id'],'amsID':r['ams'],'environment':r['environment'],'topic':settings.ios_topic+'.push-type.liveactivity',
                 'createdAt':r['created_at'],'lastRegisteredAt':r['registered_at'],'expiresAt':r['expires'],
                 'lastAcceptedAt':r['last_sent'] or None,'tokenFingerprint':token_fingerprint(r['token'])}
                for r in store.rows('SELECT * FROM activities ORDER BY expires DESC')]

    @app.get('/v2/admin/widgets', dependencies=[Depends(auth.require)])
    def widgets():
        now=time.time(); readings={r.amsID:r for r in store.readings()}
        result=[]
        for row in store.rows('SELECT * FROM widget_installations WHERE deleted=0 ORDER BY updated DESC'):
            active=bool(json.loads(row['ams_ids']))
            blocked=row['last_status'] is not None and 400<=row['last_status']<500 and row['last_status']!=429
            pending=active and not blocked and display_fingerprint(row,readings,now,settings.stale_seconds)!=row['last_fingerprint']
            result.append({'id':row['id'],'generation':row['generation'],'active':active,
                'amsIDs':json.loads(row['ams_ids']),'environment':row['environment'],'topic':row['bundle_id']+'.push-type.widgets',
                'tokenFingerprint':token_fingerprint(row['token']),'createdAt':row['created'],'updatedAt':row['updated'],
                'lastAcceptedAt':row['last_sent'] or None,'lastStatus':row['last_status'],'lastReason':row['last_reason'],
                'pending':pending,'nextAttemptAt':max(now,row['next_attempt'],row['last_attempt']+300) if pending else None})
        return result

    @app.get('/v2/admin/push-attempts', dependencies=[Depends(auth.require)])
    def attempts(before: int | None=None,limit: int=Query(default=50,ge=1,le=100)):
        rows = store.rows('SELECT * FROM push_attempts WHERE at>=? AND id<? ORDER BY id DESC LIMIT ?',
                          (time.time()-7*86400,before or 2**63-1,limit+1))
        return {'attempts':[dict(r) for r in rows[:limit]],'nextCursor':rows[limit-1]['id'] if len(rows)>limit else None}

    @app.get('/v2/admin/queue', dependencies=[Depends(auth.require)])
    def queue():
        return [{'id':r['id'],'deviceID':r['device'],'ruleID':r['rule_id'],'attempts':r['attempts'],
                 'nextAttemptAt':r['next'],'createdAt':r['created']} for r in store.rows('SELECT * FROM outbox ORDER BY next LIMIT 100')]

    if settings.web_dist_path.is_dir():
        app.mount('/assets', StaticFiles(directory=settings.web_dist_path/'assets'), name='assets')
        app.mount('/brand', StaticFiles(directory=settings.web_dist_path/'brand'), name='brand')

        @app.get('/', include_in_schema=False)
        def dashboard():
            return FileResponse(settings.web_dist_path/'index.html',headers={'Cache-Control':'no-cache'})
