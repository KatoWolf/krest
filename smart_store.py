"""Small, bounded records for local automation; no per-frame or per-poll writes."""

from contextlib import contextmanager
import json
import sqlite3
import threading
import time
import uuid


class SmartStore:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.counts = {}
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, updated REAL, PRIMARY KEY(kind,id))"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, kind, limit=200):
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT value FROM records WHERE kind=? ORDER BY updated DESC LIMIT ?",
                    (kind, limit),
                )
            ]

    def get(self, kind, ident):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM records WHERE kind=? AND id=?", (kind, ident)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, value, *, limit=200):
        value = dict(value)
        value.setdefault("id", uuid.uuid4().hex)
        with self.lock, self.connect() as db:
            existed = (
                db.execute(
                    "SELECT 1 FROM records WHERE kind=? AND id=?", (kind, value["id"])
                ).fetchone()
                is not None
            )
            if kind not in self.counts:
                self.counts[kind] = db.execute(
                    "SELECT count(*) FROM records WHERE kind=?", (kind,)
                ).fetchone()[0]
            db.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                (kind, value["id"], json.dumps(value, ensure_ascii=False), time.time()),
            )
            if kind == "watches":
                # An old, still-running watch must survive newer completed jobs.
                inactive = [
                    r[0]
                    for r in db.execute(
                        "SELECT id,value FROM records WHERE kind=? ORDER BY updated DESC", (kind,)
                    )
                    if json.loads(r[1]).get("status") not in {"watching", "shutdown_wait"}
                ]
                db.executemany(
                    "DELETE FROM records WHERE kind=? AND id=?",
                    [(kind, ident) for ident in inactive[100:]],
                )
            elif not existed and self.counts[kind] + 1 > limit:
                db.execute(
                    "DELETE FROM records WHERE kind=? AND id NOT IN (SELECT id FROM records WHERE kind=? ORDER BY updated DESC LIMIT ?)",
                    (kind, kind, limit),
                )
            if not existed:
                self.counts[kind] = min(limit, self.counts[kind] + 1)
        return value

    def remove(self, kind, ident=None):
        with self.lock, self.connect() as db:
            self.counts.pop(kind, None)
            if ident is None:
                db.execute("DELETE FROM records WHERE kind=?", (kind,))
            else:
                db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, ident))


def export_settings(source, destination, guard):
    """Portable rules/profiles only. Active watches and per-file progress stay local."""
    from contextlib import closing

    with (
        closing(sqlite3.connect(source)) as original,
        closing(sqlite3.connect(destination)) as target,
    ):
        target.execute(
            "CREATE TABLE records (kind TEXT, id TEXT, value TEXT, updated REAL, PRIMARY KEY(kind,id))"
        )
        for kind, ident, raw, updated in original.execute(
            "SELECT kind,id,value,updated FROM records WHERE kind IN ('download_rules','audio_profiles','habits','ui_actions')"
        ):
            guard()
            value = json.loads(raw)
            if kind in {"download_rules", "ui_actions"}:
                value["enabled"] = False
            elif kind == "habits" and value["status"] == "enabled":
                value["status"] = "dismissed"
            target.execute(
                "INSERT INTO records VALUES (?,?,?,?)",
                (kind, ident, json.dumps(value, ensure_ascii=False), updated),
            )
        target.commit()
