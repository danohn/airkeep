import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import logging
import re
import time
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from accounts import OAuth, install_accounts
from oidc import install_oidc
from collector import Collector
from config import Settings
from domain import AlertRule, NamedAlertInput, activity_payload, LIVE_ACTIVITY_DURATION_SECONDS, LIVE_ACTIVITY_UPDATE_SECONDS
from push import APNs
from storage import RuleConflict, Store
from web_auth import BrowserAuth
from web_routes import install_web, maintain, send_recorded
from widgets import WidgetRegistration, upsert_widget, read_widget, delete_widget, tick_widgets

log = logging.getLogger("airkeep")


class DeviceRegistration(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    token: str = Field(pattern=r"^[0-9a-fA-F]{64,256}$")
    platform: Literal["ios", "mac"]
    environment: Literal["sandbox", "production"] = "sandbox"


class DeviceUpsert(BaseModel):
    token: str = Field(pattern=r"^[0-9a-fA-F]{64,256}$")
    platform: Literal["ios", "mac", "watch"]
    environment: Literal["sandbox", "production"] = "sandbox"
    initialReceiveAlerts: bool = False


class DeviceSubscription(BaseModel):
    receiveAlerts: bool


class ActivityRegistration(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    token: str = Field(pattern=r"^[0-9a-fA-F]{64,256}$")
    environment: Literal["sandbox", "production"] = "sandbox"
    amsID: str = Field(default="0", min_length=1, max_length=8)
    expiresAt: float


async def tick(store: Store, settings: Settings, push: APNs, now: float, multiuser=False):
    readings = {r.amsID:r for r in store.readings()}
    store.evaluate_alerts(now,settings.stale_seconds,multiuser)
    # Do not deliver yesterday's threshold event after a long outage/configuration delay.
    store.execute("DELETE FROM outbox WHERE created<?", (now-3600,))
    store.execute("DELETE FROM activities WHERE expires<?", (now-3600,))
    outbox_filter = (""" AND EXISTS (SELECT 1 FROM registration_owners own
                        WHERE own.kind='device' AND own.id=d.id AND own.user=r.owner_id)"""
                     if multiuser else "")
    for job in store.rows('''SELECT o.*,d.token,d.topic,d.environment,d.registered_at FROM outbox o
        JOIN devices d ON d.id=o.device JOIN alert_rules r ON r.id=o.rule_id
        WHERE o.next<=? AND d.receive_alerts=1 AND json_extract(r.json,'$.enabled')=1''' + outbox_filter + " LIMIT 25",(now,)):
        if not store.rows('SELECT 1 FROM outbox o JOIN devices d ON d.id=o.device WHERE o.id=? AND d.receive_alerts=1',(job['id'],)):
            continue
        if not settings.apns_credentials(job['environment'],job['topic']):
            continue
        result = await send_recorded(store,push,job['token'],job['topic'],job['environment'],json.loads(job['json']),
                                     now=now,kind='alert',destination=job['device'])
        status = result.status or 503
        if status == 200 or status in (400,410) or job["attempts"] >= 8 or now-job["next"]>86400:
            store.execute("DELETE FROM outbox WHERE id=?",(job["id"],))
            if status == 410:
                with store.lock, store.db:
                    store.db.execute('BEGIN IMMEDIATE')
                    current = store.db.execute('SELECT token,environment,registered_at FROM devices WHERE id=?',(job['device'],)).fetchone()
                    if (current and current['token']==job['token'] and current['environment']==job['environment']
                            and current['registered_at']==job['registered_at']):
                        store.delete_device(job['device'])
        else:
            store.execute("UPDATE outbox SET attempts=attempts+1,next=? WHERE id=?",(now+max(min(3600,30*2**job["attempts"]),result.retry_after_seconds or 0),job["id"]))
    activity_filter = (" WHERE EXISTS (SELECT 1 FROM registration_owners o WHERE o.kind='activity' AND o.id=activities.id)"
                       if multiuser else "")
    for activity in store.rows("SELECT * FROM activities" + activity_filter):
        end = now >= activity["expires"]
        if not end and now - activity["last_sent"] < LIVE_ACTIVITY_UPDATE_SECONDS:
            continue
        if not settings.apns_credentials(activity['environment'],settings.ios_topic):
            continue
        result = await send_recorded(store,push,activity['token'],settings.ios_topic,activity['environment'],
            activity_payload(readings.get(activity['ams']),now,settings.stale_seconds,end=end,expires_at=activity['expires']),
            now=now,kind='liveactivity',destination=activity['id'])
        status = result.status or 503
        if status == 410 or (status == 200 and end):
            store.execute("DELETE FROM activities WHERE id=? AND token=? AND environment=? AND registered_at IS ?",(activity["id"],activity["token"],activity["environment"],activity["registered_at"]))
        elif status == 200:
            store.execute("UPDATE activities SET last_sent=? WHERE id=? AND token=? AND environment=? AND registered_at IS ?",(now,activity["id"],activity["token"],activity["environment"],activity["registered_at"]))

    await tick_widgets(store,settings,push,now,send_recorded,multiuser)
    maintain(store,now)

def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.db_path)
    if not settings.jwt_signing_key and store.rows("SELECT 1 FROM config WHERE id='accounts_enabled'"):
        store.close()
        raise RuntimeError('Set the JWT signing key to start a relay with accounts.')
    oauth = OAuth(store,settings) if settings.jwt_signing_key else None
    collector = Collector(settings,store) if settings.mqtt_enabled else None
    push = APNs(settings)

    async def monitor():
        while True:
            try:
                now = time.time()
                if oauth:
                    oauth.maintain(now)
                await tick(store,settings,push,now,bool(oauth))
                store.execute("DELETE FROM samples WHERE at<?",(time.time()-7*86400,))
            except Exception:
                log.exception("Monitor iteration failed")
            await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        if collector:
            if not settings.printer_ip or not settings.printer_serial or not settings.printer_code:
                raise RuntimeError("Set printer IP, serial and LAN access code in .env")
            collector.start()
        task = asyncio.create_task(monitor())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if collector:
            collector.stop()
        await push.close()
        store.close()

    app = FastAPI(title="Airkeep relay",version="0.3.0",lifespan=lifespan,docs_url=None,redoc_url=None)
    app.state.store, app.state.collector, app.state.push = store, collector, push
    security = HTTPBearer(auto_error=False)
    browser = BrowserAuth(store,settings)

    def connection(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if not oauth:
            raise HTTPException(401, 'Sign in to Airkeep.')
        if 'authorization' in request.headers:
            if credentials is None:
                raise HTTPException(401, 'Sign in to Airkeep.')
            return oauth.authenticate(credentials.credentials)
        return oauth.browser_session(request)

    def principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        identity = connection(request, credentials)
        if identity.client == 'esp32':
            raise HTTPException(403, 'This display connection is limited to live readings.')
        return identity

    def authorize_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is not None or oauth:
            return principal(request, credentials)
        browser.require(request)

    def snapshot_access(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if oauth:
            identity = connection(request, credentials)
            return None if identity.client == 'esp32' else identity
        return authorize_user(request, credentials)

    def admin(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if not oauth:
            return browser.require(request)
        identity = principal(request, credentials)
        if identity.role != 'admin' or identity.client != 'dashboard':
            raise HTTPException(403,'Administrator access required.')
        return identity

    def write_rules(identity=Depends(authorize_user)):
        if identity and identity.client == 'watch':
            raise HTTPException(403,'Manage alerts in the dashboard or Airkeep app.')
        return identity

    def native(identity=Depends(principal)):
        if identity and identity.client not in ('apple-ios','apple-mac'):
            raise HTTPException(403,'An Apple app connection is required.')
        return identity

    def alert_device(identity=Depends(principal)):
        if identity.client not in ('apple-ios', 'apple-mac', 'watch'):
            raise HTTPException(403, 'An Apple app connection is required.')
        return identity

    def device_topic(platform, identity):
        if (platform == 'watch') != (identity.client == 'watch'):
            raise HTTPException(403, 'Use the matching Apple app to register for alerts.')
        return {'ios': settings.ios_topic, 'mac': settings.mac_topic, 'watch': settings.watch_topic}[platform]

    def watch(identity=Depends(principal)):
        if identity and identity.client != 'watch':
            raise HTTPException(403,'An Apple Watch connection is required.')
        return identity

    @contextmanager
    def registration(identity, kind, identifier, claim=False):
        if identity is None:
            yield
            return
        with store.transaction():
            if not oauth.active(identity.grant):
                raise HTTPException(401,'Sign in to Airkeep.')
            if kind in ('device','widget') and identity.installation != identifier:
                raise HTTPException(404,'Registration not found.')
            owner = store.rows('SELECT grant_id FROM registration_owners WHERE kind=? AND id=?',(kind,identifier))
            if (owner and owner[0]['grant_id'] != identity.grant) or (not owner and not claim):
                raise HTTPException(404,'Registration not found.')
            yield
            if claim and not owner:
                store.execute('INSERT INTO registration_owners VALUES(?,?,?,?)',
                              (kind,identifier,identity.grant,identity.user))

    def check_push_destination(identity, table, identifier, token, environment, topic=None):
        if identity:
            column = {'devices':'topic','widget_installations':'bundle_id'}.get(table)
            extra = f' AND {column}=?' if column else ''
            args = (identifier,token.lower(),environment,topic) if column else (identifier,token.lower(),environment)
            if store.rows(f'SELECT 1 FROM {table} WHERE id<>? AND lower(token)=? AND environment=?{extra}',args):
                raise HTTPException(409,'This push destination is linked to another installation.')

    install_web(app,store,settings,browser,authorize_user,collector,admin,oauth)
    if oauth:
        install_accounts(app,oauth,principal,admin,connection)
        if settings.oidc_issuer:
            install_oidc(app, oauth, settings, admin)

    @app.put('/v2/widgets/{installation_id}')
    def register_widgets(installation_id: str, body: WidgetRegistration, identity=Depends(watch)):
        if not 1 <= len(installation_id) <= 128:
            raise HTTPException(422,'Invalid installation ID')
        with registration(identity,'widget',installation_id,claim=True):
            check_push_destination(identity,'widget_installations',installation_id,body.token,body.environment,body.bundleID)
            return upsert_widget(store,installation_id,body,settings)

    @app.get('/v2/widgets/{installation_id}')
    def widget_registration(installation_id: str, identity=Depends(watch)):
        with registration(identity,'widget',installation_id):
            return read_widget(store,installation_id,settings)

    @app.delete('/v2/widgets/{installation_id}')
    def remove_widgets(installation_id: str, identity=Depends(watch)):
        with registration(identity,'widget',installation_id):
            delete_widget(store,installation_id)
        return {'removed':True}

    @app.get("/health")
    def health():
        return {"ok":True}

    @app.get("/v1/snapshot")
    def snapshot(identity=Depends(snapshot_access)):
        now = time.time()
        value = {"serverTime":now,"staleAfter":settings.stale_seconds,
                 "printerConnected":bool(collector and collector.connected),
                 "readings":store.readings()}
        if not oauth:
            value.update(pushConfigured=settings.apns_ready,alertRule=store.rule(),alertActive=store.alert().active)
        elif identity:
            first = next(iter(store.rules(identity.user)),None)
            value.update(pushConfigured=settings.apns_ready,alertRule=first or AlertRule(),alertActive=bool(first and first['active']))
        return value

    @app.get("/v1/history",dependencies=[Depends(authorize_user)])
    def history(ams: str="0",hours: int=1):
        return store.history(ams,time.time()-min(168,max(1,hours))*3600)

    @app.get("/v1/alerts")
    def alerts(identity=Depends(authorize_user)):
        return next(iter(store.rules(identity.user)),AlertRule()) if identity else store.rule()

    @app.put("/v1/alerts",dependencies=[Depends(principal)])
    def set_alerts(rule: AlertRule):
        raise HTTPException(409,{'code':'client_update_required','message':'Update Airkeep to manage named alert rules.'})

    def expected_revision(value: str | None):
        if value is None:
            raise HTTPException(428,'Include the rule revision in If-Match')
        match = re.fullmatch(r'"([1-9][0-9]*)"|([1-9][0-9]*)',value)
        if not match:
            raise HTTPException(400,'Invalid If-Match revision')
        return int(match.group(1) or match.group(2))

    @app.get('/v2/alerts')
    def list_alerts(identity=Depends(authorize_user)):
        return store.rules(identity.user if identity else None)

    @app.post('/v2/alerts',status_code=201)
    def create_alert(draft: NamedAlertInput, response: Response, identity=Depends(write_rules)):
        result = store.create_rule(draft,identity.user if identity else None)
        response.headers['ETag'] = f'"{result["revision"]}"'
        return result

    @app.get('/v2/alerts/{rule_id}')
    def read_alert(rule_id: str, response: Response, identity=Depends(authorize_user)):
        try:
            result = store.get_rule(rule_id,identity.user if identity else None)
        except KeyError:
            raise HTTPException(404,'This alert was removed') from None
        response.headers['ETag'] = f'"{result["revision"]}"'
        return result

    @app.put('/v2/alerts/{rule_id}')
    def update_alert(rule_id: str, draft: NamedAlertInput, response: Response,
                     if_match: str | None = Header(default=None), identity=Depends(write_rules)):
        try:
            result = store.update_rule(rule_id,draft,expected_revision(if_match),identity.user if identity else None)
        except KeyError:
            raise HTTPException(404,'This alert was removed') from None
        except RuleConflict:
            raise HTTPException(412,'This alert changed on another device. Reload it before saving.') from None
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        response.headers['ETag'] = f'"{result["revision"]}"'
        return result

    @app.delete('/v2/alerts/{rule_id}',status_code=204)
    def delete_alert(rule_id: str, if_match: str | None = Header(default=None), identity=Depends(write_rules)):
        try:
            store.delete_rule(rule_id,expected_revision(if_match),identity.user if identity else None)
        except KeyError:
            raise HTTPException(404,'This alert was removed') from None
        except RuleConflict:
            raise HTTPException(412,'This alert changed on another device. Reload it before deleting.') from None
        return Response(status_code=204)

    @app.post("/v1/devices")
    def register_device(device: DeviceRegistration, identity=Depends(native)):
        topic = settings.ios_topic if device.platform == "ios" else settings.mac_topic
        with registration(identity,'device',device.id,claim=True):
            check_push_destination(identity,'devices',device.id,device.token,device.environment,topic)
            store.upsert_device(device.id,device.token,topic,device.environment,initial_receive=True)
        return {"registered":True,"pushConfigured":settings.apns_ready}

    @app.put('/v2/devices/{device_id}')
    def upsert_device(device_id: str, device: DeviceUpsert, identity=Depends(alert_device)):
        if not 1 <= len(device_id) <= 128:
            raise HTTPException(422,'Invalid device identifier')
        topic = device_topic(device.platform, identity)
        with registration(identity,'device',device_id,claim=True):
            check_push_destination(identity,'devices',device_id,device.token,device.environment,topic)
            enabled = store.upsert_device(device_id,device.token,topic,device.environment,device.initialReceiveAlerts)
        return {'registered':True,'receiveAlerts':enabled}

    @app.patch('/v2/devices/{device_id}')
    def subscribe_device(device_id: str, subscription: DeviceSubscription, identity=Depends(alert_device)):
        try:
            with registration(identity,'device',device_id):
                enabled = store.set_subscription(device_id,subscription.receiveAlerts)
        except KeyError:
            raise HTTPException(404,'Register this device before changing its subscription') from None
        return {'registered':True,'receiveAlerts':enabled}

    @app.get("/v1/notifications/status")
    def notification_status(
        device: str = Query(min_length=1, max_length=128),
        platform: Literal["ios", "mac", "watch"] = "ios",
        environment: Literal["sandbox", "production"] = "sandbox",
        bundle_id: str = Query(min_length=1, max_length=255),
        identity=Depends(alert_device),
    ):
        topic = device_topic(platform, identity)
        owned = not identity or bool(store.rows("SELECT 1 FROM registration_owners WHERE kind='device' AND id=? AND grant_id=?",(device,identity.grant)))
        rows = store.rows(
            "SELECT receive_alerts FROM devices WHERE id=? AND topic=? AND environment=?",
            (device, topic, environment),
        ) if topic == bundle_id and owned else []
        registered = bool(rows)
        configuration, reason = settings.notification_configuration(platform, environment, bundle_id)
        return {"registered": registered, "configuration": configuration,
                "reason": reason, "environment": environment,
                "receiveAlerts": bool(rows[0][0]) if registered else False}

    @app.delete("/v1/devices/{device_id}")
    def delete_device(device_id: str, identity=Depends(native)):
        with registration(identity,'device',device_id):
            store.delete_device(device_id)
        return {"removed":True}

    @app.post("/v1/activities")
    def register_activity(activity: ActivityRegistration, identity=Depends(native)):
        now = time.time()
        if not now < activity.expiresAt <= now+LIVE_ACTIVITY_DURATION_SECONDS+60:
            raise HTTPException(422,"Live Activity must end within eight hours")
        if identity and identity.client != 'apple-ios':
            raise HTTPException(403,'Start Live Activities on iPhone.')
        with registration(identity,'activity',activity.id,claim=True):
            check_push_destination(identity,'activities',activity.id,activity.token,activity.environment)
            # Token rotation must not extend the originally requested session.
            old = store.rows("SELECT expires FROM activities WHERE id=?",(activity.id,))
            expires = min(activity.expiresAt,old[0][0]) if old else activity.expiresAt
            store.execute("""INSERT INTO activities(id,token,environment,ams,expires,created_at,registered_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,environment=excluded.environment,
                ams=excluded.ams,expires=excluded.expires,last_sent=0,registered_at=excluded.registered_at""",
                (activity.id,activity.token,activity.environment,activity.amsID,expires,now,now))
        return {"registered":True}

    @app.delete("/v1/activities/{activity_id}")
    def end_activity(activity_id: str, identity=Depends(native)):
        with registration(identity,'activity',activity_id):
            store.execute("UPDATE activities SET expires=0,last_sent=0 WHERE id=?",(activity_id,))
        return {"ending":True}

    return app
