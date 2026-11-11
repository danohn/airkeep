"""Enrollment and telemetry ingestion for a tenant's single LAN relay."""
import json
import math
import secrets
import time
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from accounts import digest
from domain import Reading
from postgres import tenant_id


class Observation(Reading):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    eventID: UUID
    replay: bool = False
    amsID: str = Field(min_length=1, max_length=8, pattern=r'^[A-Za-z0-9_-]+$')
    humidity: float = Field(ge=1, le=100)
    temperature: float | None = Field(default=None, ge=0, le=100)
    rawIndex: int | None = Field(default=None, ge=1, le=5)

    @field_validator('observedAt')
    @classmethod
    def valid_timestamp(cls, value):
        if not math.isfinite(value) or value < time.time()-7*86400 or value > time.time()+30:
            raise ValueError('Observation time must be within seven days and at most 30 seconds ahead.')
        return value


class Batch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    printerConnected: bool | None = None
    readings: list[Observation] = Field(default_factory=list, max_length=100)


def enroll_relay(store, user):
    token = secrets.token_urlsafe(32)
    with store.tenant(user), store.transaction():
        rows = store.rows('SELECT id FROM printers WHERE tenant_id=?', (user,))
        printer = rows[0]['id'] if rows else str(uuid4())
        if not rows:
            store.execute('INSERT INTO printers(tenant_id,id) VALUES(?,?)', (user, printer))
        store.execute('UPDATE relay_credentials SET revoked=1 WHERE "user"=?', (user,))
        store.execute('UPDATE printers SET heartbeat=NULL,connected=0 WHERE tenant_id=? AND id=?', (user, printer))
        store.execute('INSERT INTO relay_credentials(hash,"user",printer_id,created) VALUES(?,?,?,?)',
                      (digest(token), user, printer, time.time()))
    return {'access_token': token, 'token_type': 'Bearer'}


def relay_status(store, now=None):
    now = time.time() if now is None else now
    rows = store.rows('SELECT * FROM printers WHERE tenant_id=?', (tenant_id.get(),))
    if not rows:
        return {'status': 'unpaired', 'printerID': None}
    row = rows[0]
    if row['heartbeat'] is None or now-row['heartbeat'] > 90:
        status = 'relay_offline'
    elif not row['connected']:
        status = 'printer_disconnected'
    elif not store.readings():
        status = 'waiting_for_ams'
    elif all(now-reading.observedAt > 90 for reading in store.readings()):
        status = 'stale_readings'
    else:
        status = 'connected'
    return {'status': status, 'printerID': row['id']}


def install_ingest(app, store, principal):
    @app.post('/v1/ingest', status_code=204)
    async def ingest(request: Request):
        # Bound memory before decoding JSON, including requests without a length.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, 'Telemetry batch is too large.')
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or len(header) > 128:
            raise HTTPException(401, 'Pair this relay again.')
        token_hash = digest(header[7:])
        from starlette.concurrency import run_in_threadpool
        try:
            batch = Batch.model_validate_json(body)
        except ValueError:
            raise HTTPException(422, 'Invalid telemetry batch.') from None
        await run_in_threadpool(save_batch, store, token_hash, batch)
        return Response(status_code=204)

    @app.get('/v1/relay')
    def status(identity=Depends(principal)):
        return relay_status(store)

    @app.delete('/v1/relay', status_code=204)
    def remove(replace: bool = False, identity=Depends(principal)):
        if identity.client not in ('dashboard', 'apple-ios', 'apple-mac'):
            raise HTTPException(403, 'Manage the relay in Airkeep.')
        with store.transaction():
            store.execute('UPDATE relay_credentials SET revoked=1 WHERE "user"=?', (identity.user,))
            store.execute('UPDATE printers SET heartbeat=NULL,connected=0 WHERE tenant_id=?', (identity.user,))
            if replace:
                for table in ('outbox', 'alert_events', 'alert_rules', 'latest', 'samples', 'ingest_events', 'printers'):
                    store.execute(f'DELETE FROM {table} WHERE tenant_id=?', (identity.user,))
        return Response(status_code=204)


def save_batch(store, token_hash, batch):
    now = time.time()
    # Credential lookup is control-plane data. All telemetry is written inside
    # the resulting tenant transaction and the credential is rechecked under lock.
    credentials = store.rows('''SELECT c.* FROM relay_credentials c JOIN accounts a ON a.id=c."user"
                               WHERE c.hash=? AND c.revoked=0 AND a.disabled=0''', (token_hash,))
    if not credentials:
        raise HTTPException(401, 'Pair this relay again.')
    credential = credentials[0]
    user, printer = credential['user'], credential['printer_id']
    with store.tenant(user), store.transaction():
        if not store.rows('SELECT 1 FROM relay_credentials WHERE hash=? AND revoked=0', (token_hash,)):
            raise HTTPException(401, 'Pair this relay again.')
        if batch.printerConnected is not None:
            store.execute('UPDATE printers SET heartbeat=?,connected=? WHERE tenant_id=? AND id=?',
                          (now, int(batch.printerConnected), user, printer))
        for value in batch.readings:
            accepted = store.execute('''INSERT INTO ingest_events(tenant_id,printer_id,id,at) VALUES(?,?,?,?)
                                       ON CONFLICT DO NOTHING RETURNING id''',
                                    (user, printer, str(value.eventID), value.observedAt))
            if not accepted.rowcount:
                continue
            reading = Reading.model_validate(value.model_dump())
            data = reading.model_dump_json()
            # A bucket bounds history volume and keeps retries idempotent.
            at = int(value.observedAt//30)*30
            store.execute('''INSERT INTO samples(tenant_id,printer_id,ams,at,json) VALUES(?,?,?,?,?)
                             ON CONFLICT(tenant_id,printer_id,ams,at) DO UPDATE SET json=excluded.json
                             WHERE (samples.json::jsonb->>'observedAt')::double precision < (excluded.json::jsonb->>'observedAt')::double precision''',
                          (user, printer, value.amsID, at, data))
            if not value.replay and now-90 <= value.observedAt <= now:
                store.execute('''INSERT INTO latest(tenant_id,printer_id,ams,json) VALUES(?,?,?,?)
                                 ON CONFLICT(tenant_id,printer_id,ams) DO UPDATE SET json=excluded.json
                                 WHERE (latest.json::jsonb->>'observedAt')::double precision < (excluded.json::jsonb->>'observedAt')::double precision''',
                              (user, printer, value.amsID, data))
