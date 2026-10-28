import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from domain import AlertRule, AlertState, NamedAlertInput, Reading


class RuleConflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS samples(ams TEXT, at REAL, json TEXT, PRIMARY KEY(ams,at));
        CREATE TABLE IF NOT EXISTS latest(ams TEXT PRIMARY KEY,json TEXT);
        CREATE TABLE IF NOT EXISTS config(id TEXT PRIMARY KEY,json TEXT);
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,token TEXT,topic TEXT,environment TEXT);
        CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,token TEXT,environment TEXT,
            ams TEXT,expires REAL,last_sent REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY,device TEXT,json TEXT,attempts INTEGER DEFAULT 0,next REAL DEFAULT 0,created REAL DEFAULT 0);
        """)
        self.last_saved: dict[str, float] = {}
        if 'created' not in {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}:
            self.db.execute('ALTER TABLE outbox ADD COLUMN created REAL DEFAULT 0')
            self.db.commit()
        self._migrate_alerts()

    def _migrate_alerts(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.db.execute('''CREATE TABLE IF NOT EXISTS alert_rules(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,revision INTEGER NOT NULL,
                    json TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)''')
                if 'receive_alerts' not in {r[1] for r in self.db.execute('PRAGMA table_info(devices)')}:
                    self.db.execute('ALTER TABLE devices ADD COLUMN receive_alerts INTEGER NOT NULL DEFAULT 1')
                if 'rule_id' not in {r[1] for r in self.db.execute('PRAGMA table_info(outbox)')}:
                    self.db.execute('ALTER TABLE outbox ADD COLUMN rule_id TEXT')
                primary = self.db.execute("SELECT json FROM config WHERE id='primary_alert_id'").fetchone()
                if primary is None:
                    rule = self.db.execute("SELECT json FROM config WHERE id='rule'").fetchone()
                    state = self.db.execute("SELECT json FROM config WHERE id='alert'").fetchone()
                    identifier = str(uuid.uuid4())
                    self.db.execute('INSERT INTO alert_rules VALUES(?,?,?,?,?,?)',
                        (identifier,'Alert 1',1,rule[0] if rule else AlertRule().model_dump_json(),
                         state[0] if state else json.dumps(vars(AlertState())),time.time()))
                    self.db.execute("INSERT INTO config VALUES('primary_alert_id',?)",(json.dumps(identifier),))
                    self.db.execute('UPDATE outbox SET rule_id=? WHERE rule_id IS NULL',(identifier,))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def execute(self, sql, params=()):
        with self.lock, self.db:
            return self.db.execute(sql, params)

    def rows(self, sql, params=()):
        with self.lock:
            return list(self.db.execute(sql, params))

    def save_reading(self, reading: Reading):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO latest VALUES(?,?)", (reading.amsID, reading.model_dump_json()))
            if reading.observedAt - self.last_saved.get(reading.amsID, 0) >= 30:
                self.db.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?)", (reading.amsID, reading.observedAt, reading.model_dump_json()))
                self.last_saved[reading.amsID] = reading.observedAt

    def readings(self) -> list[Reading]:
        return [Reading.model_validate_json(r[0]) for r in self.rows("SELECT json FROM latest ORDER BY ams")]

    def history(self, ams: str, since: float):
        return [json.loads(r[0]) for r in self.rows("SELECT json FROM samples WHERE ams=? AND at>=? ORDER BY at LIMIT 3000", (ams,since))]

    def rule(self):
        rows = self.rows('SELECT json FROM alert_rules WHERE id=?',(self.primary_alert_id(),))
        return AlertRule.model_validate_json(rows[0][0]) if rows else AlertRule()

    def set_rule(self, rule: AlertRule):
        # Internal helper; legacy HTTP writes are intentionally read-only.
        self.execute('UPDATE alert_rules SET json=?,state=?,revision=revision+1 WHERE id=?',
                     (rule.model_dump_json(),json.dumps(vars(AlertState())),self.primary_alert_id()))

    def alert(self):
        rows = self.rows('SELECT state FROM alert_rules WHERE id=?',(self.primary_alert_id(),))
        return AlertState(**json.loads(rows[0][0])) if rows else AlertState()

    def save_alert(self, state):
        self.execute('UPDATE alert_rules SET state=? WHERE id=?',
                     (json.dumps(vars(state)),self.primary_alert_id()))

    def primary_alert_id(self):
        return json.loads(self.rows("SELECT json FROM config WHERE id='primary_alert_id'")[0][0])

    @staticmethod
    def _managed_rule(row):
        return {**json.loads(row['json']),'id':row['id'],'name':row['name'],
                'revision':row['revision'],'active':json.loads(row['state'])['active']}

    def rules(self):
        return [self._managed_rule(row) for row in self.rows('SELECT * FROM alert_rules ORDER BY created,id')]

    def get_rule(self, identifier):
        rows = self.rows('SELECT * FROM alert_rules WHERE id=?',(identifier,))
        if not rows:
            raise KeyError(identifier)
        return self._managed_rule(rows[0])

    def create_rule(self, draft: NamedAlertInput):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            names = {r[0] for r in self.db.execute('SELECT name FROM alert_rules')}
            index = 1
            while f'Alert {index}' in names:
                index += 1
            identifier = str(uuid.uuid4())
            rule = AlertRule.model_validate(draft.model_dump())
            self.db.execute('INSERT INTO alert_rules VALUES(?,?,?,?,?,?)',
                (identifier,draft.name or f'Alert {index}',1,rule.model_dump_json(),
                 json.dumps(vars(AlertState())),time.time()))
            return self.get_rule(identifier)

    def update_rule(self, identifier, draft: NamedAlertInput, revision: int):
        if not draft.name:
            raise ValueError('Enter an alert name')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            existing = self.get_rule(identifier)
            if existing['revision'] != revision:
                raise RuleConflict()
            rule = AlertRule.model_validate(draft.model_dump())
            changed = rule.model_dump() != AlertRule.model_validate(existing).model_dump()
            row = self.db.execute('SELECT state FROM alert_rules WHERE id=?',(identifier,)).fetchone()
            state = json.dumps(vars(AlertState())) if changed else row[0]
            self.db.execute('UPDATE alert_rules SET name=?,json=?,state=?,revision=revision+1 WHERE id=?',
                (draft.name,rule.model_dump_json(),state,identifier))
            if changed:
                self.db.execute('DELETE FROM outbox WHERE rule_id=?',(identifier,))
            return self.get_rule(identifier)

    def delete_rule(self, identifier, revision: int):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            existing = self.get_rule(identifier)
            if existing['revision'] != revision:
                raise RuleConflict()
            self.db.execute('DELETE FROM outbox WHERE rule_id=?',(identifier,))
            self.db.execute('DELETE FROM alert_rules WHERE id=?',(identifier,))

    def evaluate_alerts(self, now: float, stale: int):
        # CRUD, evaluation state, and queued events share the same transaction lock.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            readings = {r.amsID:r for r in self.readings()}
            for row in self.db.execute('SELECT * FROM alert_rules').fetchall():
                rule = AlertRule.model_validate_json(row['json'])
                state = AlertState(**json.loads(row['state']))
                reading = readings.get(rule.amsID)
                event = state.evaluate(rule,reading,now,stale)
                self.db.execute('UPDATE alert_rules SET state=? WHERE id=?',(json.dumps(vars(state)),row['id']))
                if not event or reading is None:
                    continue
                title = f"{row['name']}: humidity {'above your limit' if event == 'high' else 'recovered'}"
                ams = int(reading.amsID)+1 if reading.amsID.isdigit() else reading.amsID
                body = f'AMS {ams}: {reading.humidity:g}% RH. Your limit is {rule.threshold:g}%.'
                payload = json.dumps({'aps':{'alert':{'title':title,'body':body},'sound':'default',
                                             'thread-id':f'airkeep.ams.{rule.amsID}'},
                                      'amsID':rule.amsID,'ruleID':row['id']})
                self.db.execute('''INSERT INTO outbox(device,json,next,created,rule_id)
                    SELECT id,?,?,?,? FROM devices WHERE receive_alerts=1''',(payload,now,now,row['id']))

    def upsert_device(self, identifier, token, topic, environment, initial_receive=False):
        with self.lock, self.db:
            self.db.execute('''INSERT INTO devices(id,token,topic,environment,receive_alerts) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET token=excluded.token,topic=excluded.topic,environment=excluded.environment''',
                (identifier,token,topic,environment,int(initial_receive)))
            return bool(self.db.execute('SELECT receive_alerts FROM devices WHERE id=?',(identifier,)).fetchone()[0])

    def set_subscription(self, identifier, enabled):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM devices WHERE id=?',(identifier,)).fetchone() is None:
                raise KeyError(identifier)
            self.db.execute('UPDATE devices SET receive_alerts=? WHERE id=?',(int(enabled),identifier))
            if not enabled:
                self.db.execute('DELETE FROM outbox WHERE device=?',(identifier,))
            return bool(enabled)

    def delete_device(self, identifier):
        with self.lock, self.db:
            self.db.execute('DELETE FROM outbox WHERE device=?',(identifier,))
            self.db.execute('DELETE FROM devices WHERE id=?',(identifier,))

    def close(self):
        with self.lock:
            self.db.close()
