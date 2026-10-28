import asyncio
from contextlib import asynccontextmanager
import json
import logging
import re
import secrets
import time
from typing import Literal
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

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
    platform: Literal["ios", "mac"]
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


async def tick(store: Store, settings: Settings, push: APNs, now: float):
    readings = {r.amsID:r for r in store.readings()}
    store.evaluate_alerts(now,settings.stale_seconds)
    # Do not deliver yesterday's threshold event after a long outage/configuration delay.
    store.execute("DELETE FROM outbox WHERE created<?", (now-3600,))
    store.execute("DELETE FROM activities WHERE expires<?", (now-3600,))
    for job in store.rows('''SELECT o.*,d.token,d.topic,d.environment,d.registered_at FROM outbox o
        JOIN devices d ON d.id=o.device JOIN alert_rules r ON r.id=o.rule_id
        WHERE o.next<=? AND d.receive_alerts=1 AND json_extract(r.json,'$.enabled')=1 LIMIT 25''',(now,)):
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
    for activity in store.rows("SELECT * FROM activities"):
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

    await tick_widgets(store,settings,push,now,send_recorded)
    maintain(store,now)

def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.db_path)
    collector = Collector(settings,store) if settings.mqtt_enabled else None
    push = APNs(settings)

    async def monitor():
        while True:
            try:
                await tick(store,settings,push,time.time())
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

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(),settings.api_token.encode()):
            raise HTTPException(401,"Invalid bearer token")

    def authorize_user(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is not None:
            return authorize(credentials)
        return browser.require(request)

    install_web(app,store,settings,browser,authorize_user,collector)

    @app.put('/v2/widgets/{installation_id}',dependencies=[Depends(authorize)])
    def register_widgets(installation_id: str, registration: WidgetRegistration):
        if not 1 <= len(installation_id) <= 128:
            raise HTTPException(422,'Invalid installation ID')
        return upsert_widget(store,installation_id,registration,settings)

    @app.get('/v2/widgets/{installation_id}',dependencies=[Depends(authorize)])
    def widget_registration(installation_id: str):
        return read_widget(store,installation_id,settings)

    @app.delete('/v2/widgets/{installation_id}',dependencies=[Depends(authorize)])
    def remove_widgets(installation_id: str):
        delete_widget(store,installation_id)
        return {'removed':True}

    @app.get("/health")
    def health():
        return {"ok":True}

    @app.get("/v1/snapshot",dependencies=[Depends(authorize_user)])
    def snapshot():
        now = time.time()
        return {"serverTime":now,"staleAfter":settings.stale_seconds,
                "printerConnected":bool(collector and collector.connected),
                "pushConfigured":settings.apns_ready,"readings":store.readings(),
                "alertRule":store.rule(),"alertActive":store.alert().active}

    @app.get("/v1/history",dependencies=[Depends(authorize_user)])
    def history(ams: str="0",hours: int=1):
        return store.history(ams,time.time()-min(168,max(1,hours))*3600)

    @app.get("/v1/alerts",dependencies=[Depends(authorize_user)])
    def alerts():
        return store.rule()

    @app.put("/v1/alerts",dependencies=[Depends(authorize)])
    def set_alerts(rule: AlertRule):
        raise HTTPException(409,{'code':'client_update_required','message':'Update Airkeep to manage named alert rules.'})

    def expected_revision(value: str | None):
        if value is None:
            raise HTTPException(428,'Include the rule revision in If-Match')
        match = re.fullmatch(r'"([1-9][0-9]*)"|([1-9][0-9]*)',value)
        if not match:
            raise HTTPException(400,'Invalid If-Match revision')
        return int(match.group(1) or match.group(2))

    @app.get('/v2/alerts',dependencies=[Depends(authorize_user)])
    def list_alerts():
        return store.rules()

    @app.post('/v2/alerts',status_code=201,dependencies=[Depends(authorize_user)])
    def create_alert(draft: NamedAlertInput, response: Response):
        result = store.create_rule(draft)
        response.headers['ETag'] = f'"{result["revision"]}"'
        return result

    @app.get('/v2/alerts/{rule_id}',dependencies=[Depends(authorize_user)])
    def read_alert(rule_id: str, response: Response):
        try:
            result = store.get_rule(rule_id)
        except KeyError:
            raise HTTPException(404,'This alert was removed') from None
        response.headers['ETag'] = f'"{result["revision"]}"'
        return result

    @app.put('/v2/alerts/{rule_id}',dependencies=[Depends(authorize_user)])
    def update_alert(rule_id: str, draft: NamedAlertInput, response: Response,
                     if_match: str | None = Header(default=None)):
        try:
            result = store.update_rule(rule_id,draft,expected_revision(if_match))
        except KeyError:
            raise HTTPException(404,'This alert was removed') from None
        except RuleConflict:
            raise HTTPException(412,'This alert changed on another device. Reload it before saving.') from None
        except ValueError as error:
            raise HTTPException(422,str(error)) from None
        response.headers['ETag'] = f'"{result["revision"]}"'
        return result

    @app.delete('/v2/alerts/{rule_id}',status_code=204,dependencies=[Depends(authorize_user)])
    def delete_alert(rule_id: str, if_match: str | None = Header(default=None)):
        try:
            store.delete_rule(rule_id,expected_revision(if_match))
        except KeyError:
            raise HTTPException(404,'This alert was removed') from None
        except RuleConflict:
            raise HTTPException(412,'This alert changed on another device. Reload it before deleting.') from None
        return Response(status_code=204)

    @app.post("/v1/devices",dependencies=[Depends(authorize)])
    def register_device(device: DeviceRegistration):
        topic = settings.ios_topic if device.platform == "ios" else settings.mac_topic
        store.upsert_device(device.id,device.token,topic,device.environment,initial_receive=True)
        return {"registered":True,"pushConfigured":settings.apns_ready}

    @app.put('/v2/devices/{device_id}',dependencies=[Depends(authorize)])
    def upsert_device(device_id: str, device: DeviceUpsert):
        if not 1 <= len(device_id) <= 128:
            raise HTTPException(422,'Invalid device identifier')
        topic = settings.ios_topic if device.platform == 'ios' else settings.mac_topic
        enabled = store.upsert_device(device_id,device.token,topic,device.environment,device.initialReceiveAlerts)
        return {'registered':True,'receiveAlerts':enabled}

    @app.patch('/v2/devices/{device_id}',dependencies=[Depends(authorize)])
    def subscribe_device(device_id: str, subscription: DeviceSubscription):
        try:
            enabled = store.set_subscription(device_id,subscription.receiveAlerts)
        except KeyError:
            raise HTTPException(404,'Register this device before changing its subscription') from None
        return {'registered':True,'receiveAlerts':enabled}

    @app.get("/v1/notifications/status", dependencies=[Depends(authorize)])
    def notification_status(
        device: str = Query(min_length=1, max_length=128),
        platform: Literal["ios", "mac"] = "ios",
        environment: Literal["sandbox", "production"] = "sandbox",
        bundle_id: str = Query(min_length=1, max_length=255),
    ):
        topic = settings.mac_topic if platform == "mac" else settings.ios_topic
        registration = store.rows(
            "SELECT receive_alerts FROM devices WHERE id=? AND topic=? AND environment=?",
            (device, topic, environment),
        ) if topic == bundle_id else []
        registered = bool(registration)
        configuration, reason = settings.notification_configuration(platform, environment, bundle_id)
        return {"registered": registered, "configuration": configuration,
                "reason": reason, "environment": environment,
                "receiveAlerts": bool(registration[0][0]) if registration else False}

    @app.delete("/v1/devices/{device_id}",dependencies=[Depends(authorize)])
    def delete_device(device_id: str):
        store.delete_device(device_id)
        return {"removed":True}

    @app.post("/v1/activities",dependencies=[Depends(authorize)])
    def register_activity(activity: ActivityRegistration):
        now = time.time()
        if not now < activity.expiresAt <= now+LIVE_ACTIVITY_DURATION_SECONDS+60:
            raise HTTPException(422,"Live Activity must end within eight hours")
        # Token rotation must not extend the originally requested session.
        old = store.rows("SELECT expires FROM activities WHERE id=?",(activity.id,))
        expires = min(activity.expiresAt,old[0][0]) if old else activity.expiresAt
        store.execute("""INSERT INTO activities(id,token,environment,ams,expires,created_at,registered_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,environment=excluded.environment,
            ams=excluded.ams,expires=excluded.expires,last_sent=0,registered_at=excluded.registered_at""",
            (activity.id,activity.token,activity.environment,activity.amsID,expires,now,now))
        return {"registered":True}

    @app.delete("/v1/activities/{activity_id}",dependencies=[Depends(authorize)])
    def end_activity(activity_id: str):
        store.execute("UPDATE activities SET expires=0,last_sent=0 WHERE id=?",(activity_id,))
        return {"ending":True}

    return app
