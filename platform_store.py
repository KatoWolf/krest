"""Small durable stores shared by the kernel and desktop clients."""

from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3
import time
import uuid


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        if os.name != "nt":
            self.path.chmod(0o600)
        try:
            with connection:
                yield connection
        finally:
            connection.close()


class JobStore(Database):
    TERMINAL = {"completed", "failed", "cancelled", "interrupted"}

    def __init__(self, path, recover=True):
        super().__init__(path)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request_key TEXT UNIQUE, created REAL, updated REAL, command TEXT, source TEXT, status TEXT, result TEXT, attempts INTEGER DEFAULT 0)"
            )
            if recover:
                db.execute(
                    "UPDATE jobs SET status='interrupted',result=?,updated=? WHERE status IN ('queued','running')",
                    (
                        "Крест была остановлена. Проверьте результат перед повторным запуском.",
                        time.time(),
                    ),
                )

    def create(self, command, source, request_key=None):
        ident, now = uuid.uuid4().hex, time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                row = db.execute(
                    "SELECT id FROM jobs WHERE request_key=?", (request_key,)
                ).fetchone()
                if row:
                    return row["id"], False
            db.execute(
                "INSERT INTO jobs(id,request_key,created,updated,command,source,status,result) VALUES (?,?,?,?,?,?,'queued','')",
                (ident, request_key, now, now, command[:2000], source[:80]),
            )
            db.execute(
                "DELETE FROM jobs WHERE status IN ('completed','failed','cancelled','interrupted') AND id NOT IN (SELECT id FROM jobs ORDER BY created DESC LIMIT 2000)"
            )
        return ident, True

    def transition(self, ident, status, result=""):
        if status not in self.TERMINAL | {"running"}:
            raise ValueError("Неизвестное состояние задания")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] in self.TERMINAL:
                return False
            db.execute(
                "UPDATE jobs SET status=?,result=?,updated=?,attempts=attempts+? WHERE id=?",
                (status, str(result)[:8000], time.time(), int(status == "running"), ident),
            )
            return True

    def list(self, limit=200):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM jobs ORDER BY created DESC LIMIT ?",
                    (min(2000, max(1, int(limit))),),
                )
            ]

    def get(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def by_request(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
            return dict(row) if row else None


class MemoryStore(Database):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory(key TEXT PRIMARY KEY,value TEXT,category TEXT,updated REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,created REAL,type TEXT,payload TEXT)"
            )

    def remember(self, key, value, category="preference"):
        if not isinstance(key, str) or not 1 <= len(key.strip()) <= 100 or len(str(value)) > 4000:
            raise ValueError("Название: 1–100 символов; значение: до 4000")
        if category not in ("preference", "note", "application", "correction"):
            raise ValueError("Неизвестная категория памяти")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                not db.execute(
                    "SELECT 1 FROM memory WHERE key=?", (key.strip().casefold(),)
                ).fetchone()
                and db.execute("SELECT COUNT(*) FROM memory").fetchone()[0] >= 1000
            ):
                raise ValueError("Память заполнена: удалите лишние записи (лимит 1000)")
            db.execute(
                "INSERT INTO memory VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,category=excluded.category,updated=excluded.updated",
                (key.strip().casefold(), str(value), category, time.time()),
            )

    def forget(self, key):
        with self.connect() as db:
            return db.execute("DELETE FROM memory WHERE key=?", (key.strip().casefold(),)).rowcount

    def list(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM memory ORDER BY category,key LIMIT 1000")
            ]

    def recall(self, key, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM memory WHERE key=?", (key.strip().casefold(),)
            ).fetchone()
            return row["value"] if row else default

    def event(self, kind, payload):
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > 16000:
            raise ValueError("Событие слишком велико")
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO events(created,type,payload) VALUES (?,?,?)",
                (time.time(), kind[:80], encoded),
            )
            db.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 2000)"
            )
            return cursor.lastrowid

    def events(self, after=0, limit=100):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?",
                    (int(after), min(500, int(limit))),
                )
            ]

    def recent_events(self, limit=30):
        with self.connect() as db:
            return list(
                reversed(
                    [
                        dict(row)
                        for row in db.execute(
                            "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                            (min(500, max(1, int(limit))),),
                        )
                    ]
                )
            )
