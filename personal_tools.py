"""Local clipboard snippets and game-session journals. No game input or recording."""

from contextlib import contextmanager
from pathlib import Path
import hashlib
import os
import re
import sqlite3
import threading
import time


class LocalStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()


class ClipboardHistory(LocalStore):
    def __init__(self, path, settings, backend=None):
        super().__init__(path)
        self.settings = settings
        self.backend = backend
        self.sequence = None
        self.error = ""
        self.last_tick = 0
        self.choices = []
        self.choices_at = 0
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY, digest TEXT UNIQUE, text TEXT, app TEXT, created REAL, pinned INTEGER DEFAULT 0)"
            )

    def port(self):
        if self.backend is None:
            from windows_clipboard import read, write

            self.backend = (read, write)
        return self.backend

    def store(self, text, application=""):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("Сохраняется только непустой текст до 4000 символов.")
        ignored = {
            s.strip().casefold() for s in self.settings()["clipboard"]["ignored_apps"].split(",")
        }
        if application.casefold() in ignored:
            raise ValueError("Для этого приложения история буфера отключена.")
        if re.search(
            r"-----BEGIN .*PRIVATE KEY|(?:password|пароль|api[_ -]?key|token)\s*[:=]", text, re.I
        ) or re.fullmatch(r"[A-Za-z0-9_+/=.-]{24,}", text.strip()):
            raise ValueError("Текст похож на пароль или ключ; в историю не добавлен.")
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.lock, self.db() as db:
            db.execute(
                "INSERT INTO clips(digest,text,app,created) VALUES(?,?,?,?) ON CONFLICT(digest) DO UPDATE SET created=excluded.created, app=excluded.app",
                (digest, text, application[:100], time.time()),
            )
            db.execute(
                "DELETE FROM clips WHERE pinned=0 AND id NOT IN (SELECT id FROM clips ORDER BY pinned DESC, created DESC LIMIT ?)",
                (self.settings()["clipboard"]["max_items"],),
            )
            return db.execute("SELECT id FROM clips WHERE digest=?", (digest,)).fetchone()[0]

    def tick(self):
        if not self.settings()["clipboard"]["enabled"] or time.monotonic() - self.last_tick < 1:
            return
        self.last_tick = time.monotonic()
        try:
            sequence, text, application = self.port()[0](self.sequence)
            self.sequence = sequence
            if text:
                self.store(text, application)
            self.error = ""
        except Exception as exc:
            self.error = str(exc)[:180]

    def rows(self, query=""):
        with self.db() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM clips ORDER BY pinned DESC, created DESC LIMIT 200"
                )
            ]
        return [r for r in rows if str(query).casefold() in r["text"].casefold()]

    def call(self, action, **values):
        if action == "list":
            return {
                "rows": self.rows(values.get("query", "")),
                "error": self.error,
                "enabled": self.settings()["clipboard"]["enabled"],
            }
        if action == "save":
            _, text, application = self.port()[0](None)
            self.store(text, application)
            return "Текст из буфера сохранён."
        if action == "clear":
            with self.lock, self.db() as db:
                db.execute("DELETE FROM clips WHERE pinned=0")
            return "История очищена. Закреплённые фрагменты сохранены."
        ident = int(values["id"])
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("Фрагмент уже удалён; обновите список.")
            if action == "copy":
                self.port()[1](row["text"])
                return "Фрагмент скопирован. Вставьте его в нужное окно."
            if action == "delete":
                db.execute("DELETE FROM clips WHERE id=?", (ident,))
            elif action == "pin":
                if (
                    not row["pinned"]
                    and db.execute("SELECT count(*) FROM clips WHERE pinned=1").fetchone()[0] >= 50
                ):
                    raise ValueError("Можно закрепить до 50 фрагментов.")
                db.execute("UPDATE clips SET pinned=? WHERE id=?", (not row["pinned"], ident))
            else:
                raise ValueError("Неизвестное действие буфера")
        return "Сохранено."

    def describe(self):
        with self.lock:
            rows = self.rows()[:5]
            self.choices, self.choices_at = [r["id"] for r in rows], time.monotonic()
            return (
                "\n".join(
                    f"{i + 1}. {' '.join(r['text'].split())[:90]}" for i, r in enumerate(rows)
                )
                or "История буфера пока пуста. Скажите «сохрани буфер» или включите историю в меню."
            )

    def choose(self, number):
        with self.lock:
            if time.monotonic() - self.choices_at > 120 or not 1 <= number <= len(self.choices):
                raise ValueError(
                    "Сначала скажите «покажи историю буфера» и выберите номер из списка."
                )
            return self.call("copy", id=self.choices[number - 1])


class GameSessions(LocalStore):
    def __init__(self, path):
        super().__init__(path)
        self.last_seen = 0
        self.closed = False
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY, game TEXT, started REAL, seen REAL, ended REAL, reason TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS moments(id INTEGER PRIMARY KEY, session INTEGER, kind TEXT, text TEXT, path TEXT, created REAL)"
            )
            db.execute(
                "UPDATE sessions SET ended=seen, reason='Ядро было остановлено' WHERE ended IS NULL"
            )
        self.active = None
        self.game = ""

    def tick(self, game):
        with self.lock:
            if self.closed:
                return
            now = time.time()
            if game != self.game:
                self.finish()
                if game:
                    with self.db() as db:
                        self.active = db.execute(
                            "INSERT INTO sessions(game,started,seen) VALUES(?,?,?)",
                            (str(game)[:160], now, now),
                        ).lastrowid
                        db.execute(
                            "DELETE FROM sessions WHERE id NOT IN (SELECT id FROM sessions ORDER BY id DESC LIMIT 100)"
                        )
                        db.execute(
                            "DELETE FROM moments WHERE session NOT IN (SELECT id FROM sessions)"
                        )
                    self.game = game
            if self.active and now - self.last_seen >= 15:
                with self.db() as db:
                    db.execute("UPDATE sessions SET seen=? WHERE id=?", (now, self.active))
                self.last_seen = now

    def finish(self):
        with self.lock:
            if self.active:
                with self.db() as db:
                    db.execute(
                        "UPDATE sessions SET ended=?, seen=?, reason='Завершена' WHERE id=?",
                        (time.time(), time.time(), self.active),
                    )
            self.active, self.game = None, ""

    def close(self):
        with self.lock:
            self.closed = True
            self.finish()

    def add(self, text="", path=None):
        with self.lock:
            if not self.active:
                raise ValueError(
                    "Игра пока не обнаружена. Запустите её; сессия начнётся автоматически."
                )
            text = str(text).strip()
            if not path and not 1 <= len(text) <= 1000:
                raise ValueError("Назовите заметку до 1000 символов.")
            if path and (not Path(path).is_file() or Path(path).suffix.lower() != ".png"):
                raise ValueError("Скриншот не найден")
            with self.db() as db:
                if (
                    db.execute(
                        "SELECT count(*) FROM moments WHERE session=?", (self.active,)
                    ).fetchone()[0]
                    >= 200
                ):
                    raise ValueError("В сессии уже 200 заметок и снимков.")
                db.execute(
                    "INSERT INTO moments(session,kind,text,path,created) VALUES(?,?,?,?,?)",
                    (
                        self.active,
                        "screenshot" if path else "note",
                        text,
                        str(Path(path).resolve()) if path else "",
                        time.time(),
                    ),
                )
            return (
                "Снимок добавлен в игровую сессию."
                if path
                else "Заметка сохранена в игровой сессии."
            )

    def snapshot(self, ident=None):
        with self.lock, self.db() as db:
            rows = [
                dict(r) for r in db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 100")
            ]
            ident = int(ident) if ident is not None else rows[0]["id"] if rows else None
            selected = next((r for r in rows if r["id"] == ident), None)
            moments = (
                [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM moments WHERE session=? ORDER BY id", (ident,)
                    )
                ]
                if selected
                else []
            )
            return {"rows": rows, "selected": selected, "moments": moments, "active": self.active}

    def report(self, ident=None):
        data = self.snapshot(ident)
        row = data["selected"]
        if not row:
            return "Игровых сессий пока нет. Крест начнёт журнал после обнаружения игры."
        minutes = max(0, int(((row["ended"] or time.time()) - row["started"]) / 60))
        notes = [m["text"] for m in data["moments"] if m["kind"] == "note"]
        shots = sum(m["kind"] == "screenshot" for m in data["moments"])
        lines = [
            f"{row['game']} · {minutes} мин. " + (row["reason"] or "Игра идёт."),
            f"Заметок: {len(notes)}. Скриншотов: {shots}.",
        ]
        return "\n".join(lines + ["• " + s for s in notes[-5:]])

    def open_capture(self, ident):
        with self.db() as db:
            row = db.execute(
                "SELECT path FROM moments WHERE id=? AND kind='screenshot'", (int(ident),)
            ).fetchone()
        if not row or not Path(row["path"]).is_file():
            raise ValueError("Файл снимка перемещён или удалён")
        if os.name != "nt":
            raise ValueError("Просмотр снимка доступен в Windows")
        os.startfile(row["path"])
        return "Снимок открыт."
