import asyncio
from contextlib import asynccontextmanager
import json
import logging
import secrets
import time
from typing import Literal
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from collector import Collector
from config import Settings
from domain import AlertRule, activity_payload
from push import APNs
from storage import Store

log = logging.getLogger("airkeep")


class DeviceRegistration(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    token: str = Field(pattern=r"^[0-9a-fA-F]{64,256}$")
    platform: Literal["ios", "mac"]
    environment: Literal["sandbox", "production"] = "sandbox"


class ActivityRegistration(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    token: str = Field(pattern=r"^[0-9a-fA-F]{64,256}$")
    environment: Literal["sandbox", "production"] = "sandbox"
    amsID: str = Field(default="0", min_length=1, max_length=8)
    expiresAt: float


async def tick(store: Store, settings: Settings, push: APNs, now: float):
    readings = {r.amsID:r for r in store.readings()}
    rule, state = store.rule(), store.alert()
    reading = readings.get(rule.amsID)
    event = state.evaluate(rule, reading, now, settings.stale_seconds)
    store.save_alert(state)
    if event and reading:
        title = "AMS humidity above your limit" if event == "high" else "AMS humidity recovered"
        body = f"AMS {int(reading.amsID)+1 if reading.amsID.isdigit() else reading.amsID}: {reading.humidity:g}% RH. Your limit is {rule.threshold:g}%."
        payload = {"aps":{"alert":{"title":title,"body":body},"sound":"default"},"amsID":reading.amsID}
        for device in store.rows("SELECT id FROM devices"):
            store.execute("INSERT INTO outbox(device,json,next,created) VALUES(?,?,?,?)", (device[0],json.dumps(payload),now,now))
    # Do not deliver yesterday's threshold event after a long outage/configuration delay.
    store.execute("DELETE FROM outbox WHERE created<?", (now-3600,))
    store.execute("DELETE FROM activities WHERE expires<?", (now-3600,))
    if not settings.apns_ready:
        return
    for job in store.rows("SELECT o.*,d.token,d.topic,d.environment FROM outbox o JOIN devices d ON d.id=o.device WHERE o.next<=? LIMIT 25",(now,)):
        try:
            status = await push.send(job["token"],job["topic"],job["environment"],json.loads(job["json"]))
        except Exception:
            status = 503
            log.warning("Push delivery deferred; check APNs configuration/connectivity")
        if status == 200 or status in (400,410) or job["attempts"] >= 8 or now-job["next"]>86400:
            store.execute("DELETE FROM outbox WHERE id=?",(job["id"],))
            if status == 410:
                store.execute("DELETE FROM devices WHERE id=?",(job["device"],))
        else:
            store.execute("UPDATE outbox SET attempts=attempts+1,next=? WHERE id=?",(now+min(3600,30*2**job["attempts"]),job["id"]))
    for activity in store.rows("SELECT * FROM activities"):
        end = now >= activity["expires"]
        if not end and now - activity["last_sent"] < 300:
            continue
        try:
            status = await push.send(activity["token"], settings.ios_topic, activity["environment"],
                activity_payload(readings.get(activity["ams"]),now,settings.stale_seconds,end=end),live=True)
        except Exception:
            status = 503
        if status == 410 or (status == 200 and end):
            store.execute("DELETE FROM activities WHERE id=?",(activity["id"],))
        elif status == 200:
            store.execute("UPDATE activities SET last_sent=? WHERE id=?",(now,activity["id"]))


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

    app = FastAPI(title="Airkeep relay",version="0.1.1",lifespan=lifespan,docs_url=None,redoc_url=None)
    app.state.store, app.state.collector, app.state.push = store, collector, push
    security = HTTPBearer(auto_error=False)

    def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is None or not secrets.compare_digest(credentials.credentials.encode(),settings.api_token.encode()):
            raise HTTPException(401,"Invalid bearer token")

    @app.get("/health")
    def health():
        return {"ok":True}

    @app.get("/v1/snapshot",dependencies=[Depends(authorize)])
    def snapshot():
        now = time.time()
        return {"serverTime":now,"staleAfter":settings.stale_seconds,
                "printerConnected":bool(collector and collector.connected),
                "pushConfigured":settings.apns_ready,"readings":store.readings(),
                "alertRule":store.rule(),"alertActive":store.alert().active}

    @app.get("/v1/history",dependencies=[Depends(authorize)])
    def history(ams: str="0",hours: int=1):
        return store.history(ams,time.time()-min(168,max(1,hours))*3600)

    @app.get("/v1/alerts",dependencies=[Depends(authorize)])
    def alerts():
        return store.rule()

    @app.put("/v1/alerts",dependencies=[Depends(authorize)])
    def set_alerts(rule: AlertRule):
        store.set_rule(rule)
        return rule

    @app.post("/v1/devices",dependencies=[Depends(authorize)])
    def register_device(device: DeviceRegistration):
        topic = settings.ios_topic if device.platform == "ios" else settings.mac_topic
        store.execute("INSERT OR REPLACE INTO devices VALUES(?,?,?,?)",(device.id,device.token,topic,device.environment))
        return {"registered":True,"pushConfigured":settings.apns_ready}

    @app.get("/v1/notifications/status", dependencies=[Depends(authorize)])
    def notification_status(
        device: str = Query(min_length=1, max_length=128),
        platform: Literal["ios", "mac"] = "ios",
        environment: Literal["sandbox", "production"] = "sandbox",
        bundle_id: str = Query(min_length=1, max_length=255),
    ):
        topic = settings.mac_topic if platform == "mac" else settings.ios_topic
        registered = bool(store.rows(
            "SELECT 1 FROM devices WHERE id=? AND topic=? AND environment=?",
            (device, topic, environment),
        )) and topic == bundle_id
        configuration, reason = settings.notification_configuration(platform, environment, bundle_id)
        return {"registered": registered, "configuration": configuration,
                "reason": reason, "environment": environment}

    @app.delete("/v1/devices/{device_id}",dependencies=[Depends(authorize)])
    def delete_device(device_id: str):
        store.execute("DELETE FROM outbox WHERE device=?",(device_id,))
        store.execute("DELETE FROM devices WHERE id=?",(device_id,))
        return {"removed":True}

    @app.post("/v1/activities",dependencies=[Depends(authorize)])
    def register_activity(activity: ActivityRegistration):
        now = time.time()
        if not now < activity.expiresAt <= now+4*3600+60:
            raise HTTPException(422,"Live Activity must end within four hours")
        # Token rotation must not extend the originally requested session.
        old = store.rows("SELECT expires FROM activities WHERE id=?",(activity.id,))
        expires = min(activity.expiresAt,old[0][0]) if old else activity.expiresAt
        store.execute("INSERT OR REPLACE INTO activities(id,token,environment,ams,expires) VALUES(?,?,?,?,?)",
                      (activity.id,activity.token,activity.environment,activity.amsID,expires))
        return {"registered":True}

    @app.delete("/v1/activities/{activity_id}",dependencies=[Depends(authorize)])
    def end_activity(activity_id: str):
        store.execute("UPDATE activities SET expires=0,last_sent=0 WHERE id=?",(activity_id,))
        return {"ending":True}

    return app
