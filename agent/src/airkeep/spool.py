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
            CREATE TABLE IF NOT EXISTS quarantine (
                event_id TEXT PRIMARY KEY, rejected_at REAL NOT NULL,
                status INTEGER NOT NULL, data TEXT NOT NULL);
        ''')
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
            self.db.execute('DELETE FROM quarantine')
            self.db.execute('INSERT OR REPLACE INTO configuration VALUES (1,?)', (json.dumps(config),))

    def save_reading(self, reading):
        with self.lock, self.db:
            self._prune(time.time())
            previous = self.db.execute('SELECT observed FROM sampling WHERE ams_id=?', (reading.amsID,)).fetchone()
            # Resume sampling after the system clock is corrected backwards.
            if previous and 0 <= reading.observedAt - previous['observed'] < 30:
                return
            self.db.execute('INSERT INTO samples(event_id,ams_id,observed,data,replay) VALUES (?,?,?,?,?)',
                            (str(uuid.uuid4()), reading.amsID, reading.observedAt,
                             reading.model_dump_json(), 0))
            self.db.execute('INSERT OR REPLACE INTO sampling VALUES (?,?)', (reading.amsID, reading.observedAt))

    def _prune(self, now):
        self.db.execute('DELETE FROM samples WHERE observed < ?', (now - 86400,))
        self.db.execute('DELETE FROM sampling WHERE observed < ?', (now - 86400,))
        self.db.execute('DELETE FROM quarantine WHERE rejected_at < ?', (now - 86400,))

    def batch(self, now=None, limit=100):
        now = time.time() if now is None else now
        with self.lock, self.db:
            self._prune(now)
            # A fresh observation can resume live updates while history drains.
            # Previously attempted events remain replay even if still recent.
            rows = self.db.execute('''
                SELECT *, (replay=0 AND observed BETWEEN ? AND ?) AS live
                FROM samples
                ORDER BY live DESC, CASE WHEN live THEN observed END DESC, observed,sequence
                LIMIT ?
            ''', (now - 30, now, min(100, max(1, limit)))).fetchall()
        return [dict(json.loads(row['data']), eventID=row['event_id'],
                     replay=not bool(row['live'])) for row in rows]

    def acknowledge(self, readings):
        with self.lock, self.db:
            self.db.executemany('DELETE FROM samples WHERE event_id=?', [(item['eventID'],) for item in readings])

    def mark_replay(self, readings=None):
        with self.lock, self.db:
            if readings is None:
                self.db.execute('UPDATE samples SET replay=1')
            else:
                self.db.executemany('UPDATE samples SET replay=1 WHERE event_id=?',
                                    [(item['eventID'],) for item in readings])

    def quarantine(self, reading, status, now=None):
        now = time.time() if now is None else now
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO quarantine VALUES (?,?,?,?)',
                            (reading['eventID'], now, status, json.dumps(reading)))
            self.db.execute('DELETE FROM samples WHERE event_id=?', (reading['eventID'],))
            self._prune(now)
            self.db.execute('''DELETE FROM quarantine WHERE event_id NOT IN (
                SELECT event_id FROM quarantine ORDER BY rejected_at DESC,event_id LIMIT 100)''')

    def quarantined_count(self):
        with self.lock, self.db:
            self._prune(time.time())
            return self.db.execute('SELECT COUNT(*) FROM quarantine').fetchone()[0]

    def close(self):
        self.db.close()
