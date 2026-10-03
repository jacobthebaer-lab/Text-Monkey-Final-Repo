"""Crash-safe browser inbox/outbox. Unknown sends have no retry transition."""
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


class Journal:
    def __init__(self, path, identity):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (phone TEXT, id TEXT, PRIMARY KEY(phone,id));
            CREATE TABLE IF NOT EXISTS inbox (guid TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS suppression (phone TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, item TEXT NOT NULL, state TEXT NOT NULL,
                baseline TEXT, observed_id TEXT, ack TEXT);
        ''')
        encoded = json.dumps(identity, sort_keys=True)
        prior = self.db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if prior and prior[0] != encoded:
            self.db.close()
            raise ValueError("Account, recipient, DOM or session changed: use a fresh journal")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('identity',?)", (encoded,))
            self.db.execute("UPDATE outbox SET state='unknown' WHERE state='sending'")

    def baseline(self, phone, bubbles):
        key = "baseline:" + phone
        if self.db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
            return False
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?)", [(phone, b.id) for b in bubbles])
            self.db.execute("INSERT INTO meta VALUES (?, ?)", (key, datetime.now(timezone.utc).isoformat()))
        return True

    def cutoff(self, phone):
        value = self.db.execute("SELECT value FROM meta WHERE key=?", ("baseline:" + phone,)).fetchone()[0]
        return datetime.fromisoformat(value)

    def ingest(self, phone, bubble, payload, keyword):
        with self.db:
            if self.db.execute("SELECT 1 FROM seen WHERE phone=? AND id=?", (phone, bubble.id)).fetchone():
                return
            self.db.execute("INSERT INTO seen VALUES (?,?)", (phone, bubble.id))
            if keyword == "stop":
                self.db.execute("INSERT OR IGNORE INTO suppression VALUES (?)", (phone,))
            # START cannot clear local suppression until backend consent is committed.
            if payload:
                self.db.execute("INSERT INTO inbox (guid,payload) VALUES (?,?)", (payload["guid"], json.dumps(payload)))

    def delivered(self, row, *, start=False):
        with self.db:
            self.db.execute("UPDATE inbox SET delivered=1 WHERE guid=?", (row["guid"],))
            if start:
                self.db.execute("DELETE FROM suppression WHERE phone=?", (json.loads(row["payload"])["phone"],))

    def suppressed(self, phone):
        return bool(self.db.execute("SELECT 1 FROM suppression WHERE phone=?", (phone,)).fetchone())

    def queue_send(self, item):
        encoded = json.dumps(item, sort_keys=True)
        key = str(item["id"])
        prior = self.db.execute("SELECT item FROM outbox WHERE id=?", (key,)).fetchone()
        if prior and prior[0] != encoded:
            raise ValueError("Outbound operation ID reused with different content")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO outbox(id,item,state) VALUES (?,?,'queued')", (key, encoded))

    def set_state(self, key, state, *, baseline=None, observed=None):
        with self.db:
            self.db.execute("UPDATE outbox SET state=?,baseline=COALESCE(?,baseline),observed_id=COALESCE(?,observed_id) WHERE id=?",
                            (state, json.dumps(baseline) if baseline is not None else None, observed, key))

    def refresh_queued(self, key, item):
        old = self.db.execute("SELECT item,state FROM outbox WHERE id=?", (key,)).fetchone()
        previous = json.loads(old['item'])
        if old['state'] != 'queued' or any(previous.get(k)!=item.get(k) for k in ('id','token','phone','session_id','purpose','delivery_mode')):
            raise ValueError("Only an unattempted unchanged operation may refresh its offer policy proof")
        with self.db:
            self.db.execute("UPDATE outbox SET item=? WHERE id=?", (json.dumps(item,sort_keys=True),key))

    def pending_inbox(self):
        return self.db.execute("SELECT * FROM inbox WHERE delivered=0 ORDER BY rowid").fetchall()

    def operations(self):
        return self.db.execute("SELECT * FROM outbox ORDER BY rowid").fetchall()

    def unresolved(self, phone):
        return any(json.loads(row["item"])["phone"] == phone
                   for row in self.db.execute("SELECT item FROM outbox WHERE state IN ('unknown','sending')"))

    def acked(self, key, outcome):
        with self.db:
            self.db.execute("UPDATE outbox SET ack=? WHERE id=?", (outcome, key))

    @staticmethod
    def guid(identity, phone, message_id):
        raw = json.dumps([identity, phone, message_id], sort_keys=True)
        return "GV:" + hashlib.sha256(raw.encode()).hexdigest()
