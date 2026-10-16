"""Durable grouped inbox, separate from the legacy cross-process delivery queue."""

from contextlib import contextmanager
import sqlite3
import time
import json


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
            for column, declaration in (
                ("target", "TEXT NOT NULL DEFAULT '{}'"),
                ("after_game", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in {row[1] for row in db.execute("PRAGMA table_info(inbox)")}:
                    db.execute(f"ALTER TABLE inbox ADD COLUMN {column} {declaration}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS chat_mutes(source TEXT, chat TEXT, until REAL, PRIMARY KEY(source,chat))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS inbox_after_game ON inbox(after_game) WHERE after_game=1"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, text, source="Система", priority="normal", now=None, kind="", target=None):
        if priority not in ("low", "normal", "high"):
            raise ValueError("Неизвестный приоритет уведомления")
        now = time.time() if now is None else now
        text, source = str(text)[:4000], str(source)[:80]
        target = target if isinstance(target, dict) else {}
        encoded = json.dumps(
            {k: str(target[k])[:100] for k in ("chat", "sender", "message") if k in target},
            ensure_ascii=False,
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM inbox WHERE source=? AND text=? AND priority=? AND kind=? AND target=? AND updated>=? AND unread=1 ORDER BY id DESC LIMIT 1",
                (source, text, priority, kind, encoded, now - 60),
            ).fetchone()
            if row:
                db.execute("UPDATE inbox SET updated=?,count=count+1 WHERE id=?", (now, row["id"]))
                ident = row["id"]
            else:
                ident = db.execute(
                    "INSERT INTO inbox(created,updated,source,text,priority,count,unread,kind,target) VALUES (?,?,?,?,?,1,1,?,?)",
                    (now, now, source, text, priority, kind, encoded),
                ).lastrowid
            db.execute(
                "DELETE FROM inbox WHERE id NOT IN (SELECT id FROM inbox ORDER BY updated DESC LIMIT 1000)"
            )
        return ident, not bool(row)

    def list(self, unread=False):
        with self.connect() as db:
            return [
                dict(r, target=json.loads(r["target"]))
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

    def unread_count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM inbox WHERE unread=1").fetchone()[0]

    def mute(self, source, chat, until):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO chat_mutes VALUES (?,?,?)", (source, str(chat), until)
            )

    def muted(self, source, chat):
        with self.connect() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM chat_mutes WHERE source=? AND chat=? AND until>?",
                    (source, str(chat), time.time()),
                ).fetchone()
            )

    def after_game(self, ident):
        with self.connect() as db:
            db.execute("UPDATE inbox SET after_game=1 WHERE id=?", (ident,))

    def take_after_game(self):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM inbox WHERE after_game=1 LIMIT 1").fetchone() is None:
                return []
            db.execute("BEGIN IMMEDIATE")
            rows = [dict(r) for r in db.execute("SELECT * FROM inbox WHERE after_game=1")]
            db.execute("UPDATE inbox SET after_game=0 WHERE after_game=1")
            return rows


def should_speak(priority, mode, gaming, important_only=True):
    if mode == "silent":
        return False
    if mode == "important" or (gaming and important_only):
        return priority == "high"
    return True
