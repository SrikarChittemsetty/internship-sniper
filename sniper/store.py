"""SQLite seen-store: the poller only ever alerts on genuine deltas."""
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    uid        TEXT PRIMARY KEY,
    company    TEXT,
    title      TEXT,
    url        TEXT,
    first_seen INTEGER,          -- epoch seconds we first observed it
    notified   INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    val TEXT
);
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def is_seen(self, uid):
        cur = self.db.execute("SELECT 1 FROM seen WHERE uid = ?", (uid,))
        return cur.fetchone() is not None

    def mark_seen(self, job, notified):
        self.db.execute(
            "INSERT OR IGNORE INTO seen (uid, company, title, url, first_seen, notified)"
            " VALUES (?,?,?,?,?,?)",
            (job["uid"], job["company"], job["title"], job["url"],
             int(time.time()), 1 if notified else 0),
        )

    def new_jobs(self, jobs):
        """Filter to jobs never seen before (by uid)."""
        return [j for j in jobs if not self.is_seen(j["uid"])]

    def get_meta(self, key, default=None):
        cur = self.db.execute("SELECT val FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def set_meta(self, key, val):
        self.db.execute(
            "INSERT INTO meta (key, val) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET val = excluded.val",
            (key, str(val)),
        )

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
