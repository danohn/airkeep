import json
from pathlib import Path
import sqlite3
import threading

from domain import AlertRule, AlertState, Reading


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
        rows = self.rows("SELECT json FROM config WHERE id='rule'")
        return AlertRule.model_validate_json(rows[0][0]) if rows else AlertRule()

    def set_rule(self, rule: AlertRule):
        self.execute("INSERT OR REPLACE INTO config VALUES('rule',?)", (rule.model_dump_json(),))
        self.save_alert(AlertState())

    def alert(self):
        rows = self.rows("SELECT json FROM config WHERE id='alert'")
        return AlertState(**json.loads(rows[0][0])) if rows else AlertState()

    def save_alert(self, state):
        self.execute("INSERT OR REPLACE INTO config VALUES('alert',?)", (json.dumps(vars(state)),))

    def close(self):
        with self.lock:
            self.db.close()
