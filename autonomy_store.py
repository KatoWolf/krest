"""Durable AFK jobs, folder rules, versions and bounded learning evidence."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


class DutyStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT, label TEXT, payload TEXT, checkpoint TEXT,
                    status TEXT, created REAL, updated REAL, due REAL, attempts INTEGER DEFAULT 0,
                    priority INTEGER DEFAULT 0, rule_id TEXT DEFAULT '', result TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS rules (
                    id TEXT PRIMARY KEY, kind TEXT, label TEXT, payload TEXT, interval INTEGER,
                    enabled INTEGER, due REAL, learned INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, source TEXT, destination TEXT, hash TEXT, size INTEGER,
                    mtime INTEGER, created REAL, kind TEXT, job_id TEXT);
                CREATE INDEX IF NOT EXISTS versions_source ON versions(source, created);
                CREATE TABLE IF NOT EXISTS habits (
                    key TEXT PRIMARY KEY, kind TEXT, label TEXT, payload TEXT, days TEXT,
                    successes INTEGER DEFAULT 0, rejected INTEGER DEFAULT 0, rule_id TEXT DEFAULT '');
            """)
            db.execute(
                "UPDATE jobs SET status='waiting', result='Продолжу после простоя' WHERE status='running'"
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("payload", "checkpoint", "days"):
            if key in result:
                result[key] = json.loads(result[key])
        return result

    def enqueue(self, kind, label, payload, rule_id="", priority=0, unique=""):
        now = time.time()
        ident = unique or uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE id=?", (ident,)).fetchone():
                return ident
            if (
                db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('waiting','running','retry')"
                ).fetchone()[0]
                >= 200
            ):
                raise ValueError("В очереди уже 200 задач")
            db.execute(
                "INSERT OR IGNORE INTO jobs (id,kind,label,payload,checkpoint,status,created,updated,due,priority,rule_id) VALUES (?,?,?,?,?,'waiting',?,?,?,?,?)",
                (
                    ident,
                    kind,
                    label[:160],
                    json.dumps(payload, ensure_ascii=False),
                    "{}",
                    now,
                    now,
                    now,
                    priority,
                    rule_id,
                ),
            )
        return ident

    def get(self, ident):
        with closing(self.connect()) as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone())

    def update(self, ident, **values):
        if set(values) - {"status", "result", "checkpoint", "due", "attempts", "priority"}:
            raise ValueError("Неизвестное поле задачи")
        if "checkpoint" in values:
            values["checkpoint"] = json.dumps(values["checkpoint"], ensure_ascii=False)
        values["updated"] = time.time()
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                [*values.values(), ident],
            )

    def next(self):
        with closing(self.connect()) as db:
            return self.decode(
                db.execute(
                    "SELECT * FROM jobs WHERE status IN ('waiting','retry') AND due<=? ORDER BY priority DESC,created LIMIT 1",
                    (time.time(),),
                ).fetchone()
            )

    def interrupted_installers(self):
        with closing(self.connect()) as db:
            rows = [
                self.decode(r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE kind='software' AND status IN ('waiting','retry','failed')"
                )
            ]
        return [
            r
            for r in rows
            if r["checkpoint"].get("installer_started")
            and not r["checkpoint"].get("installer_finished")
        ]

    def jobs(self):
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT id,kind,label,status,created,updated,due,attempts,priority,rule_id,result FROM jobs ORDER BY CASE WHEN status IN ('running','waiting','retry') THEN 0 ELSE 1 END,updated DESC LIMIT 100"
            ).fetchall()
            return [dict(row) for row in rows]

    def add_rule(self, kind, label, payload, interval=86400, learned=False):
        ident = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            if db.execute("SELECT count(*) FROM rules").fetchone()[0] >= 50:
                raise ValueError("Допускается 50 правил")
            db.execute(
                "INSERT INTO rules VALUES (?,?,?,?,?,1,?,?)",
                (
                    ident,
                    kind,
                    label,
                    json.dumps(payload, ensure_ascii=False),
                    interval,
                    time.time(),
                    int(learned),
                ),
            )
        return ident

    def rules(self):
        with closing(self.connect()) as db:
            return [self.decode(row) for row in db.execute("SELECT * FROM rules ORDER BY label")]

    def rule_enable(self, ident, enabled):
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE rules SET enabled=?,due=? WHERE id=?", (int(enabled), time.time(), ident)
            )
            if not enabled:
                db.execute("UPDATE habits SET rejected=1 WHERE rule_id=?", (ident,))
                db.execute(
                    "UPDATE jobs SET status='cancelled',result='Правило выключено' WHERE rule_id=? AND status IN ('waiting','retry')",
                    (ident,),
                )

    def schedule(self):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('waiting','running','retry')"
            ).fetchone()[0]
            for row in db.execute(
                "SELECT * FROM rules WHERE enabled=1 AND due<=?", (time.time(),)
            ).fetchall():
                if active >= 200:
                    break
                if not db.execute(
                    "SELECT 1 FROM jobs WHERE rule_id=? AND status IN ('waiting','running','retry')",
                    (row["id"],),
                ).fetchone():
                    now = time.time()
                    db.execute(
                        "INSERT INTO jobs (id,kind,label,payload,checkpoint,status,created,updated,due,rule_id) VALUES (?,?,?,?,?,'waiting',?,?,?,?)",
                        (
                            uuid.uuid4().hex,
                            row["kind"],
                            row["label"],
                            row["payload"],
                            "{}",
                            now,
                            now,
                            now,
                            row["id"],
                        ),
                    )
                    active += 1
                db.execute(
                    "UPDATE rules SET due=? WHERE id=?", (time.time() + row["interval"], row["id"])
                )
            db.execute(
                "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE status IN ('completed','cancelled','failed') ORDER BY updated DESC LIMIT -1 OFFSET 200)"
            )

    def delete_rule(self, ident):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM rules WHERE id=?", (ident,))

    def forget_version(self, ident):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM versions WHERE id=?", (ident,))

    def version(self, source, destination, digest, stat, kind, job_id):
        ident = hashlib.sha256((str(destination) + kind).encode()).hexdigest()[:32]
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    str(source),
                    str(destination),
                    digest,
                    stat.st_size,
                    stat.st_mtime_ns,
                    time.time(),
                    kind,
                    job_id,
                ),
            )
        return ident

    def versions(self, source=None):
        with closing(self.connect()) as db:
            query = (
                "SELECT * FROM versions"
                + (" WHERE source=?" if source else "")
                + " ORDER BY created DESC LIMIT 200"
            )
            return [dict(row) for row in db.execute(query, (str(source),) if source else ())]

    def find_version(self, ident):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM versions WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("Версия не найдена")
            return dict(row)

    def observe(self, job, success=True, learn=True):
        # Only user-created, verified jobs supply evidence. Never learn from our
        # rules, recovery jobs, Telegram documents, failed or cancelled actions.
        if job["rule_id"] or job["kind"] not in {"backup", "copy", "archive"}:
            return
        from datetime import datetime

        raw = json.dumps(job["payload"], sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256((job["kind"] + raw).encode()).hexdigest()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute("SELECT checkpoint FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if not stored:
                return
            checkpoint = json.loads(stored[0])
            if success and checkpoint.get("observed"):
                return
            if success:
                checkpoint["observed"] = True
                db.execute(
                    "UPDATE jobs SET checkpoint=? WHERE id=?", (json.dumps(checkpoint), job["id"])
                )
            if not db.execute("SELECT 1 FROM habits WHERE key=?", (key,)).fetchone():
                if db.execute("SELECT count(*) FROM habits").fetchone()[0] >= 200:
                    return
            db.execute(
                "INSERT OR IGNORE INTO habits(key,kind,label,payload,days) VALUES (?,?,?,?,?)",
                (key, job["kind"], job["label"], raw, "[]"),
            )
            row = self.decode(db.execute("SELECT * FROM habits WHERE key=?", (key,)).fetchone())
            days = sorted(set(row["days"]) | {datetime.now().date().isoformat()})[-30:]
            successes = row["successes"] + int(success)
            rejected = row["rejected"] or not success
            db.execute(
                "UPDATE habits SET days=?,successes=?,rejected=? WHERE key=?",
                (json.dumps(days), successes, int(rejected), key),
            )
            if learn and not rejected and not row["rule_id"] and successes >= 5 and len(days) >= 3:
                if db.execute("SELECT count(*) FROM rules").fetchone()[0] < 50:
                    ident = uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO rules VALUES (?,?,?,?,86400,1,?,1)",
                        (ident, job["kind"], job["label"], raw, time.time() + 86400),
                    )
                    db.execute("UPDATE habits SET rule_id=? WHERE key=?", (ident, key))

    def habits(self):
        with closing(self.connect()) as db:
            return [
                self.decode(row)
                for row in db.execute("SELECT * FROM habits ORDER BY successes DESC LIMIT 50")
            ]
