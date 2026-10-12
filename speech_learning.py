"""Profile-scoped correction evidence; only explicit approval activates a mapping."""

import hashlib
import time
from platform_store import Database


def profile_key(settings):
    return hashlib.sha256(settings.get("owner_name", "").strip().casefold().encode()).hexdigest()[
        :20
    ]


class SpeechLearning(Database):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS corrections(profile TEXT, heard TEXT, command TEXT, skill TEXT, hits INTEGER, status TEXT, updated REAL, PRIMARY KEY(profile,heard))"
            )

    def record(self, profile, heard, command, skill):
        heard = heard.casefold().strip(" ,.!?")
        if not 2 <= len(heard) <= 100 or heard == command.casefold() or len(command) > 500:
            return None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM corrections WHERE profile=? AND heard=?", (profile, heard)
            ).fetchone()
            same = bool(row and row["command"] == command and row["skill"] == skill)
            status = row["status"] if same else "pending"
            hits = min(999, row["hits"] + 1) if same else 1
            db.execute(
                "INSERT OR REPLACE INTO corrections VALUES (?,?,?,?,?,?,?)",
                (profile, heard, command, skill, hits, status, time.time()),
            )
            db.execute(
                "DELETE FROM corrections WHERE rowid IN (SELECT rowid FROM corrections ORDER BY updated DESC LIMIT -1 OFFSET 500)"
            )
        return {"heard": heard, "command": command, "hits": hits, "status": status}

    def list(self, profile):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT heard,command,skill,hits,status,updated FROM corrections WHERE profile=? ORDER BY updated DESC LIMIT 100",
                    (profile,),
                )
            ]

    def lookup(self, profile, heard):
        with self.connect() as db:
            row = db.execute(
                "SELECT command,skill FROM corrections WHERE profile=? AND heard=? AND status='approved'",
                (profile, heard.casefold().strip(" ,.!?")),
            ).fetchone()
            return dict(row) if row else None

    def decide(self, profile, heard, status):
        if status not in ("approved", "rejected", "pending"):
            raise ValueError("Неизвестное решение по исправлению")
        with self.connect() as db:
            if not db.execute(
                "UPDATE corrections SET status=?,updated=? WHERE profile=? AND heard=?",
                (status, time.time(), profile, heard),
            ).rowcount:
                raise ValueError("Исправление не найдено в этом профиле")
        return True
