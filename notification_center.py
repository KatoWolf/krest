"""Durable grouped inbox, separate from the legacy cross-process delivery queue."""

from contextlib import contextmanager
import sqlite3
import time


class NotificationCenter:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY, created REAL, updated REAL, source TEXT, text TEXT, priority TEXT, count INTEGER, unread INTEGER)"
            )
            if "kind" not in {row[1] for row in db.execute("PRAGMA table_info(inbox)")}:
                db.execute("ALTER TABLE inbox ADD COLUMN kind TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, text, source="Система", priority="normal", now=None, kind=""):
        if priority not in ("low", "normal", "high"):
            raise ValueError("Неизвестный приоритет уведомления")
        now = time.time() if now is None else now
        text, source = str(text)[:4000], str(source)[:80]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM inbox WHERE source=? AND text=? AND priority=? AND kind=? AND updated>=? AND unread=1 ORDER BY id DESC LIMIT 1",
                (source, text, priority, kind, now - 60),
            ).fetchone()
            if row:
                db.execute("UPDATE inbox SET updated=?,count=count+1 WHERE id=?", (now, row["id"]))
                ident = row["id"]
            else:
                ident = db.execute(
                    "INSERT INTO inbox(created,updated,source,text,priority,count,unread,kind) VALUES (?,?,?,?,?,1,1,?)",
                    (now, now, source, text, priority, kind),
                ).lastrowid
            db.execute(
                "DELETE FROM inbox WHERE id NOT IN (SELECT id FROM inbox ORDER BY updated DESC LIMIT 1000)"
            )
        return ident, not bool(row)

    def list(self, unread=False):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM inbox "
                    + ("WHERE unread=1 " if unread else "")
                    + "ORDER BY updated DESC LIMIT 300"
                )
            ]

    def mark_read(self, ident=None):
        with self.connect() as db:
            if ident is None:
                db.execute("UPDATE inbox SET unread=0")
            else:
                db.execute("UPDATE inbox SET unread=0 WHERE id=?", (ident,))


def should_speak(priority, mode, gaming, important_only=True):
    if mode == "silent":
        return False
    if mode == "important" or (gaming and important_only):
        return priority == "high"
    return True
