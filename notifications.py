"""Small cross-process notification queue for independently managed bridges."""

from contextlib import contextmanager
import os
import json
import queue
import sqlite3
import time
from pathlib import Path


class NotificationQueue:
    def __init__(self, path=None):
        self.path = str(
            path
            or Path(os.getenv("KREST_DATA_DIR", Path(__file__).resolve().parent))
            / "notifications.sqlite3"
        )
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, created REAL, text TEXT)"
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put(self, text, source=None, priority="normal"):
        if source is not None:
            text = json.dumps(
                {"krest_notice": 1, "text": str(text), "source": str(source), "priority": priority},
                ensure_ascii=False,
            )
        with self._connect() as connection:
            connection.execute("DELETE FROM notifications WHERE created < ?", (time.time() - 300,))
            connection.execute(
                "INSERT INTO notifications(created,text) VALUES (?,?)",
                (time.time(), str(text)),
            )
            connection.execute(
                "DELETE FROM notifications WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 100)"
            )

    put_nowait = put

    def get_nowait(self):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM notifications WHERE created < ?", (time.time() - 300,))
            row = connection.execute(
                "SELECT id,text FROM notifications ORDER BY id LIMIT 1"
            ).fetchone()
            if row is not None:
                connection.execute("DELETE FROM notifications WHERE id=?", (row[0],))
        if row is None:
            raise queue.Empty
        return row[1]
