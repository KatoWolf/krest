"""Action decisions are written on changes; idle operation never writes a journal."""

import json
import time
from platform_store import Database


ACTIVE = {"waiting", "queued", "running", "verifying", "dispatching"}


class BrainStore(Database):
    def __init__(self, path):
        super().__init__(path)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS decisions (kind TEXT,id TEXT,status TEXT,updated REAL,"
                "payload TEXT,PRIMARY KEY(kind,id))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS decisions_recent ON decisions(kind,updated DESC)"
            )

    def put(self, kind, row):
        with self.connect() as db:
            if kind == "plans":
                # Traces may lose their last update on power loss. Goal dispatch
                # claims retain FULL durability to prevent an automatic replay.
                db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?)",
                (kind, row["id"], row["status"], time.time(), json.dumps(row, ensure_ascii=False)),
            )
            db.execute(
                "DELETE FROM decisions WHERE kind=? AND id IN (SELECT id FROM decisions "
                "WHERE kind=? AND status NOT IN ('waiting','queued','running','verifying','dispatching') "
                "ORDER BY updated DESC LIMIT -1 OFFSET 200)",
                (kind, kind),
            )

    def rows(self, kind, *, active=False):
        where = (
            " AND status IN ('waiting','queued','running','verifying','dispatching')"
            if active
            else ""
        )
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM decisions WHERE kind=?"
                    + where
                    + " ORDER BY updated DESC LIMIT 300",
                    (kind,),
                )
            ]

    def get(self, kind, ident):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM decisions WHERE kind=? AND id=?", (kind, ident)
            ).fetchone()
        return json.loads(row[0]) if row else None
