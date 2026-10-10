"""A durable move log. Undo never overwrites an occupied source path."""

import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import time
from contextlib import contextmanager


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class SortHistory:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError("Папка загрузок не найдена: " + str(self.root))
        self.path = self.root / ".krest-sort-history.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Журнал сортировки не должен быть ссылкой")
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS moves(id INTEGER PRIMARY KEY, batch TEXT, created REAL, source TEXT, target TEXT, hash TEXT, undone INTEGER DEFAULT 0, restored_size INTEGER, restored_mtime INTEGER)"
            )

    @contextmanager
    def connect(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            with db:
                yield db
        except sqlite3.Error as exc:
            raise OSError("Журнал сортировки: " + str(exc)) from exc
        finally:
            if db is not None:
                db.close()

    def record(self, batch, source, target, hash_value):
        with self.connect() as db:
            return db.execute(
                "INSERT INTO moves(batch,created,source,target,hash) VALUES (?,?,?,?,?)",
                (batch, time.time(), str(source), str(target), hash_value),
            ).lastrowid

    def discard(self, ident):
        with self.connect() as db:
            db.execute("DELETE FROM moves WHERE id=?", (ident,))

    def list(self):
        with self.connect() as db:
            return [dict(v) for v in db.execute("SELECT * FROM moves ORDER BY id DESC LIMIT 500")]

    def was_restored(self, path, info):
        with self.connect() as db:
            row = db.execute(
                "SELECT restored_size,restored_mtime FROM moves WHERE source=? AND undone=1 ORDER BY id DESC LIMIT 1",
                (str(path),),
            ).fetchone()
        return row and (info.st_size, info.st_mtime_ns) == (
            row["restored_size"],
            row["restored_mtime"],
        )

    def undo_last(self, stop=None):
        from downloads import _sort_lock, _CancellableReader

        result = {"restored": 0, "errors": []}
        if not _sort_lock.acquire(blocking=False):
            raise ValueError("Сортировка ещё выполняется")
        from process_lock import ProcessLock

        process_lock = ProcessLock(self.root / ".krest-sort.lock")
        try:
            if not process_lock.acquire():
                raise ValueError("Сортировка этой папки уже выполняется")
            with self.connect() as db:
                batch = db.execute(
                    "SELECT batch FROM moves WHERE undone=0 ORDER BY id DESC LIMIT 1"
                ).fetchone()
                rows = (
                    []
                    if not batch
                    else [
                        dict(v)
                        for v in db.execute(
                            "SELECT * FROM moves WHERE batch=? AND undone=0 ORDER BY id DESC",
                            (batch["batch"],),
                        )
                    ]
                )
            for row in rows:
                if stop and stop.is_set():
                    break
                source, target = Path(row["source"]), Path(row["target"])
                try:
                    if (
                        source.parent != self.root
                        or target.parent.parent != self.root
                        or source.is_symlink()
                        or target.is_symlink()
                        or target.parent.is_symlink()
                    ):
                        raise ValueError("Путь изменился или является ссылкой")
                    if source.exists():
                        raise ValueError("Исходное имя занято; перезапись запрещена")
                    if not target.is_file() or digest(target) != row["hash"]:
                        raise ValueError("Файл изменён или перемещён после сортировки")
                    created = False
                    try:
                        with source.open("xb") as output:
                            created = True
                            with target.open("rb") as stream:
                                reader = _CancellableReader(stream, stop)
                                shutil.copyfileobj(reader, output, 1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
                        if (
                            reader.hash.hexdigest() != row["hash"]
                            or digest(target) != row["hash"]
                            or (stop and stop.is_set())
                        ):
                            raise ValueError("Файл изменился во время восстановления")
                        shutil.copystat(target, source)
                        # Mark before removing the destination. A crash may leave two copies,
                        # but never loses the only copy or authorizes an overwrite.
                        info = source.stat()
                        with self.connect() as db:
                            db.execute(
                                "UPDATE moves SET undone=1,restored_size=?,restored_mtime=? WHERE id=?",
                                (info.st_size, info.st_mtime_ns, row["id"]),
                            )
                        created = False
                        target.unlink()
                        result["restored"] += 1
                    except Exception:
                        if created:
                            source.unlink(missing_ok=True)
                        raise
                except (OSError, ValueError) as exc:
                    result["errors"].append(source.name + ": " + str(exc))
            return result
        finally:
            process_lock.close()
            _sort_lock.release()
