"""Watch WidgetKit registrations and coalesced refresh hints."""
import hashlib
import json
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator


class WidgetRegistration(BaseModel):
    token: str = Field(pattern=r'^[0-9a-fA-F]{64,256}$')
    platform: Literal['watch']
    environment: Literal['sandbox', 'production']
    bundleID: str = Field(min_length=1, max_length=255)
    amsIDs: list[str] = Field(max_length=16)
    generation: int = Field(gt=0, le=9007199254740991)

    @field_validator('amsIDs')
    @classmethod
    def valid_units(cls, values):
        if any(not 1 <= len(value) <= 8 for value in values):
            raise ValueError('Invalid AMS ID')
        return sorted(set(values))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def widget_status(row, settings):
    configuration, reason = settings.notification_configuration('watch', row['environment'], row['bundle_id'])
    return {'registered': True, 'active': bool(json.loads(row['ams_ids'])),
            'generation': row['generation'], 'topic': row['bundle_id'] + '.push-type.widgets',
            'configuration': configuration, 'reason': reason}


def read_widget(store, identifier, settings):
    rows = store.rows('SELECT * FROM widget_installations WHERE id=? AND deleted=0', (identifier,))
    if not rows:
        raise HTTPException(404, 'Register this Watch to enable widget updates.')
    return widget_status(rows[0], settings)


def upsert_widget(store, identifier, registration, settings):
    if registration.bundleID != settings.watch_topic:
        raise HTTPException(422, 'The Watch app topic must match the relay configuration.')
    data = registration.model_dump()
    data['token'] = data['token'].lower()
    now = time.time()
    with store.lock, store.db:
        store.db.execute('BEGIN IMMEDIATE')
        old = store.db.execute('SELECT * FROM widget_installations WHERE id=?', (identifier,)).fetchone()
        if old:
            same = (not old['deleted'] and old['token'] == data['token'] and old['environment'] == data['environment']
                    and old['bundle_id'] == data['bundleID'] and json.loads(old['ams_ids']) == data['amsIDs'])
            if data['generation'] < old['generation'] or (data['generation'] == old['generation'] and not same):
                raise HTTPException(409, {'code':'widget_registration_conflict','generation':old['generation'],'deleted':bool(old['deleted']),'message':'A newer Watch registration is already saved.'})
            if data['generation'] == old['generation']:
                return widget_status(old, settings)
        store.db.execute('''INSERT INTO widget_installations(id,generation,token,environment,bundle_id,ams_ids,created,updated)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,deleted=0,
            token=excluded.token,environment=excluded.environment,bundle_id=excluded.bundle_id,ams_ids=excluded.ams_ids,
            updated=excluded.updated,created=COALESCE(widget_installations.created,excluded.created),last_sent=0,last_fingerprint=NULL,next_attempt=0,attempts=0,last_status=NULL,last_reason=NULL''',
            (identifier,data['generation'],data['token'],data['environment'],data['bundleID'],json.dumps(data['amsIDs']),now,now))
        row = store.db.execute('SELECT * FROM widget_installations WHERE id=?', (identifier,)).fetchone()
        return widget_status(row, settings)


def delete_widget(store, identifier):
    # Retain the highest generation so an old in-flight PUT cannot resurrect it.
    store.execute('''UPDATE widget_installations SET deleted=1,token=NULL,environment=NULL,bundle_id=NULL,ams_ids='[]',created=NULL,updated=NULL,last_sent=0,last_fingerprint=NULL,
                     next_attempt=0,attempts=0,last_status=NULL,last_reason=NULL WHERE id=?''', (identifier,))


def display_fingerprint(row, readings, now, stale):
    values = []
    for ams in json.loads(row['ams_ids']):
        reading = readings.get(ams)
        values.append({'ams': ams, 'humidity': round(reading.humidity) if reading else None,
                       'temperature': round(reading.temperature,1) if reading and reading.temperature is not None else None,
                       'stale': reading is None or now-reading.observedAt > stale})
    return fingerprint(values)


async def tick_widgets(store, settings, push, now, send_recorded):
    readings = {r.amsID: r for r in store.readings()}
    for row in store.rows("SELECT * FROM widget_installations WHERE deleted=0 AND ams_ids!='[]'"):
        if not settings.apns_credentials(row['environment'],row['bundle_id']):
            continue
        if row['last_status'] is not None and 400 <= row['last_status'] < 500 and row['last_status'] != 429:
            continue
        current = display_fingerprint(row, readings, now, settings.stale_seconds)
        if current == row['last_fingerprint'] or now < max(row['next_attempt'], row['last_attempt']+300):
            continue
        reserved=store.execute('''UPDATE widget_installations SET last_attempt=? WHERE id=? AND generation=? AND token=? AND environment=?
            AND last_attempt<=? AND next_attempt<=? AND deleted=0''',
            (now,row['id'],row['generation'],row['token'],row['environment'],now-300,now))
        if reserved.rowcount!=1:
            continue
        outcome = await send_recorded(store, push, row['token'], row['bundle_id'], row['environment'],
            {'aps': {'content-changed': True}}, now=now, kind='widgets', destination=row['id'],
            expiration=now+300, collapse_id='airkeep.widgets')
        where = (row['id'],row['generation'],row['token'],row['environment'])
        if outcome.status == 410:
            store.execute('''UPDATE widget_installations SET deleted=1,token=NULL,environment=NULL,bundle_id=NULL,ams_ids='[]',created=NULL,updated=NULL,last_sent=0,last_fingerprint=NULL,next_attempt=0,attempts=0,last_status=NULL,last_reason=NULL
                WHERE id=? AND generation=? AND token=? AND environment=?''', where)
        elif outcome.status == 200:
            store.execute('''UPDATE widget_installations SET last_sent=?,last_attempt=?,last_fingerprint=?,attempts=0,
                next_attempt=0,last_status=200,last_reason=NULL WHERE id=? AND generation=? AND token=? AND environment=?''',
                (now,now,current,*where))
        else:
            attempts = min(row['attempts']+1, 12)
            delay = max(300, min(3600, 30*2**attempts), outcome.retry_after_seconds or 0)
            # Permanent token/topic errors wait for a fresh registration.
            if outcome.status is not None and 400 <= outcome.status < 500 and outcome.status != 429:
                delay = 86400
            store.execute('''UPDATE widget_installations SET last_attempt=?,attempts=?,next_attempt=?,last_status=?,last_reason=?
                WHERE id=? AND generation=? AND token=? AND environment=?''',
                (now,attempts,now+delay,outcome.status,outcome.reason,*where))
