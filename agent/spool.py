"""Private configuration and bounded, transactional telemetry outbox."""
import json
import sqlite3
import threading
import time
import uuid


class Spool:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS configuration (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
                ams_id TEXT NOT NULL, observed REAL NOT NULL, data TEXT NOT NULL,
                replay INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sampling (ams_id TEXT PRIMARY KEY, observed REAL NOT NULL);
        ''')
        self.buffering = True
        self.mark_replay()

    def configuration(self):
        with self.lock:
            row = self.db.execute('SELECT data FROM configuration WHERE id=1').fetchone()
        return json.loads(row['data']) if row else None

    def configure(self, config):
        # Enrollment replacement and spool deletion commit together. Never send a
        # previous account's backlog using a new account's bearer token.
        with self.lock, self.db:
            self.db.execute('DELETE FROM samples')
            self.db.execute('DELETE FROM sampling')
            self.db.execute('INSERT OR REPLACE INTO configuration VALUES (1,?)', (json.dumps(config),))

    def save_reading(self, reading):
        with self.lock, self.db:
            self._prune(time.time())
            previous = self.db.execute('SELECT observed FROM sampling WHERE ams_id=?', (reading.amsID,)).fetchone()
            if previous and reading.observedAt - previous['observed'] < 30:
                return
            self.db.execute('INSERT INTO samples(event_id,ams_id,observed,data,replay) VALUES (?,?,?,?,?)',
                            (str(uuid.uuid4()), reading.amsID, reading.observedAt,
                             reading.model_dump_json(), int(self.buffering)))
            self.db.execute('INSERT OR REPLACE INTO sampling VALUES (?,?)', (reading.amsID, reading.observedAt))

    def _prune(self, now):
        self.db.execute('DELETE FROM samples WHERE observed < ?', (now - 86400,))
        self.db.execute('DELETE FROM sampling WHERE observed < ?', (now - 86400,))

    def batch(self, now=None):
        now = time.time() if now is None else now
        with self.lock, self.db:
            self._prune(now)
            rows = self.db.execute('SELECT * FROM samples ORDER BY observed,sequence LIMIT 100').fetchall()
        return [dict(json.loads(row['data']), eventID=row['event_id'],
                     replay=bool(row['replay'] or now - row['observed'] >= 30)) for row in rows]

    def acknowledge(self, readings):
        with self.lock, self.db:
            self.db.executemany('DELETE FROM samples WHERE event_id=?', [(item['eventID'],) for item in readings])
            self.buffering = False

    def mark_replay(self):
        with self.lock, self.db:
            self.db.execute('UPDATE samples SET replay=1')
            self.buffering = True

    def close(self):
        self.db.close()
