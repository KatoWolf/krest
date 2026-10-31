"""Durable, exclusive same-volume moves. No file-content reads in the light path."""

from contextlib import contextmanager, closing
import ctypes
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import time
import uuid

from autonomy_files import clean_path, fingerprint
from process_lock import ProcessLock


@contextmanager
def closed_file(path):
    """Hold a Windows handle that excludes readers/writers but permits our rename."""
    if os.name != "nt":
        # Advisory lock on POSIX; production Windows enforces the sharing mode.
        import fcntl

        with Path(path).open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield stream
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
        return
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 4, None, 3, 0x80, None)
    if handle == w.HANDLE(-1).value:
        raise OSError("Файл открыт или недоступен: " + Path(path).name)
    import msvcrt

    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        yield stream


def file_revision(path):
    if os.name != "nt":
        return Path(path).stat().st_ctime_ns
    from ctypes import wintypes as w

    class Basic(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_longlong) for name in ("created", "accessed", "written", "changed")
        ] + [("attributes", w.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(str(path), 0, 7, None, 3, 0x80, None)
    if handle == w.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = Basic()
        if not kernel.GetFileInformationByHandleEx(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.changed
    finally:
        kernel.CloseHandle(handle)


def rename_exclusive(source, target):
    if os.name == "nt":
        # Unlike POSIX rename(), Windows rename refuses to replace a target.
        os.rename(source, target)
    else:
        os.link(source, target, follow_symlinks=False)
        source.unlink()


class MoveJournal:
    def __init__(self, root):
        self.root = clean_path(root)
        self.path = self.root / ".krest-moves.sqlite3"
        clean_path(self.path)
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS moves (
                id TEXT PRIMARY KEY, batch TEXT, source TEXT, target TEXT,
                stamp TEXT, created REAL, received REAL, label TEXT,
                status TEXT, detail TEXT DEFAULT '', hash TEXT DEFAULT '')""")
            if "revision" not in {r[1] for r in db.execute("PRAGMA table_info(moves)")}:
                db.execute("ALTER TABLE moves ADD COLUMN revision INTEGER DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS move_status ON moves(status)")

    def connect(self):
        clean_path(self.path)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def rows(self, limit=500):
        with closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM moves ORDER BY created DESC LIMIT ?", (limit,))
            ]

    def get(self, ident):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM moves WHERE id=?", (ident,)).fetchone()
            return dict(row) if row else None

    def _update(self, ident, status, detail=""):
        revision = 0
        if status in {"moved", "returned"}:
            row = self.get(ident)
            if row:
                revision = file_revision(row["source"] if status == "returned" else row["target"])
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE moves SET status=?,detail=?,revision=? WHERE id=?",
                (status, detail[:500], revision, ident),
            )

    def _prepare(self, source, target, stamp, batch, label, received, hash_value=""):
        ident = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO moves (id,batch,source,target,stamp,created,received,label,status,detail,hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    batch,
                    str(source),
                    str(target),
                    json.dumps(stamp),
                    time.time(),
                    received or time.time(),
                    label[:80],
                    "prepared_copy" if hash_value else "prepared",
                    "",
                    hash_value,
                ),
            )
        return ident

    @contextmanager
    def locked(self):
        lock = ProcessLock(self.root / ".krest-sort.lock")
        if not lock.acquire():
            raise OSError("Папка уже обрабатывается")
        try:
            yield
        finally:
            lock.close()

    @staticmethod
    def matches(path, stamp):
        try:
            return clean_path(path).is_file() and fingerprint(path) == stamp
        except (OSError, ValueError):
            return False

    def was_returned(self, source, stamp):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT stamp,revision FROM moves WHERE source=? AND status='returned' ORDER BY created DESC LIMIT 1",
                (str(source),),
            ).fetchone()
        return bool(row and json.loads(row[0]) == stamp and row[1] == file_revision(source))

    def move(
        self,
        source,
        destination,
        *,
        batch="",
        label="Загрузки",
        received=0,
        guard=lambda: None,
        locked=False,
    ):
        if not locked:
            with self.locked():
                return self.move(
                    source,
                    destination,
                    batch=batch,
                    label=label,
                    received=received,
                    guard=guard,
                    locked=True,
                )
        source, destination = clean_path(source), clean_path(destination)
        if source.parent != self.root or not source.is_file():
            raise ValueError("Файл не принадлежит выбранной папке")
        destination.mkdir(parents=True, exist_ok=True)
        clean_path(destination)
        if source.stat().st_dev != destination.stat().st_dev:
            raise ValueError("Другой том: требуется проверенное копирование при AFK")
        guard()
        with closed_file(source):
            stamp = fingerprint(source)
            for number in range(10000):
                target = destination / (
                    source.name if not number else f"{source.stem} ({number}){source.suffix}"
                )
                if target.exists():
                    continue
                guard()
                ident = self._prepare(
                    source, target, stamp, batch or uuid.uuid4().hex, label, received
                )
                try:
                    guard()
                    rename_exclusive(source, target)
                except FileExistsError:
                    self._update(ident, "aborted", "Имя занято; выбрано другое")
                    continue
                except OSError as exc:
                    # A partial POSIX link remains recoverable; never delete an unknown target.
                    self._update(ident, "prepared", str(exc))
                    raise
                self._update(ident, "moved", "Перемещено без копирования содержимого")
                return self.get(ident)
        raise OSError("Не удалось подобрать свободное имя")

    def finish_copy(
        self, source, target, stamp, hash_value, *, label="Загрузки", guard=lambda: None, batch=""
    ):
        source, target = clean_path(source), clean_path(target)
        with self.locked(), closed_file(source) as original, closed_file(target) as copied:
            if source.parent != self.root or not self.matches(source, stamp):
                raise ValueError("Исходник изменился; обе копии сохранены")
            if (
                locked_hash(original, guard) != hash_value
                or locked_hash(copied, guard) != hash_value
            ):
                raise ValueError("Копии различаются; оригинал сохранён")
            ident = self._prepare(
                source, target, stamp, batch or uuid.uuid4().hex, label, time.time(), hash_value
            )
            guard()
            source.unlink()
            self._update(ident, "moved", "Перенесено на другой диск после проверки SHA-256")
            return self.get(ident)

    def recover_copies(self, guard=lambda: None):
        with self.locked(), closing(self.connect()) as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM moves WHERE status IN ('prepared_copy','returning_copy') LIMIT 20"
                )
            ]
            for row in rows:
                guard()
                returning = row["status"] == "returning_copy"
                source, target = clean_path(row["source"]), clean_path(row["target"])
                if source.parent != self.root or source == target:
                    self._update(row["id"], "attention", "Некорректный путь в журнале")
                    continue
                if returning:
                    source, target = target, source
                if not target.is_file():
                    self._update(
                        row["id"], "attention", "Проверенная копия недоступна; исходник сохранён"
                    )
                    continue
                with closed_file(target) as copied:
                    if locked_hash(copied, guard) != row["hash"]:
                        self._update(row["id"], "attention", "Копия изменена; требуется проверка")
                        continue
                    if source.exists():
                        with closed_file(source) as original:
                            if (
                                not returning and not self.matches(source, json.loads(row["stamp"]))
                            ) or locked_hash(original, guard) != row["hash"]:
                                self._update(
                                    row["id"], "attention", "Исходник изменён; обе копии сохранены"
                                )
                                continue
                            guard()
                            source.unlink()
                    if returning:
                        with closing(self.connect()) as update, update:
                            update.execute(
                                "UPDATE moves SET stamp=? WHERE id=?",
                                (json.dumps(fingerprint(target)), row["id"]),
                            )
                    self._update(
                        row["id"],
                        "returned" if returning else "moved",
                        "Результат подтверждён после перезапуска",
                    )
        return len(rows)

    def undo_copy(self, ident, files, job, guard):
        row = self.get(ident)
        if (
            not row
            or row["status"] not in {"moved", "returning_copy", "returned"}
            or not row["hash"]
        ):
            raise ValueError("Перемещение не подходит для восстановления")
        if row["status"] == "returned":
            return "Файл уже возвращён: " + row["source"]
        source, target = clean_path(row["source"]), clean_path(row["target"])
        if source.parent != self.root:
            raise ValueError("Папка исходника изменилась")
        with self.locked():
            files.copy_file(target, source, job, expected_hash=row["hash"])
            with closed_file(target) as original, closed_file(source) as restored:
                if (
                    locked_hash(original, guard) != row["hash"]
                    or locked_hash(restored, guard) != row["hash"]
                ):
                    raise ValueError("Файл изменён; обе копии сохранены")
                self._update(ident, "returning_copy", "Завершаю проверенный возврат")
                guard()
                target.unlink()
                with closing(self.connect()) as db, db:
                    db.execute(
                        "UPDATE moves SET stamp=? WHERE id=?",
                        (json.dumps(fingerprint(source)), ident),
                    )
                self._update(ident, "returned", "Файл возвращён после проверки SHA-256")
        return "Вернула файл: " + str(source)

    def undo(self, ident, *, guard=lambda: None):
        with self.locked():
            row = self.get(ident)
            if not row or row["status"] != "moved":
                raise ValueError("Перемещение уже отменено или требует проверки")
            source, target = clean_path(row["source"]), clean_path(row["target"])
            if source.parent != self.root or source.exists():
                raise ValueError("Исходное имя занято; перезапись запрещена")
            if row["hash"]:
                raise ValueError("Возврат после копирования на другой диск выполняется при AFK")
            with closed_file(target):
                if not self.matches(target, json.loads(row["stamp"])) or row[
                    "revision"
                ] != file_revision(target):
                    raise ValueError(
                        "Файл изменён после перемещения; автоматический возврат остановлен"
                    )
                guard()
                self._update(ident, "returning", "Возвращаю файл")
                rename_exclusive(target, source)
                self._update(
                    ident,
                    "returned",
                    "Возвращён; повторная автосортировка запрещена до изменения файла",
                )
            return str(source)

    def undo_batch(self, batch=None, stop=None):
        rows = [r for r in self.rows() if r["status"] == "moved"]
        batch = batch or (rows[0]["batch"] if rows else "")
        result = {"restored": 0, "errors": []}
        for row in rows:
            if row["batch"] != batch or stop and stop.is_set():
                continue
            try:
                self.undo(row["id"])
                result["restored"] += 1
            except (OSError, ValueError) as exc:
                result["errors"].append(str(exc))
        return result

    def recover(self, guard=lambda: None):
        """Resolve recorded intent by file identity. Never replay a blind move."""
        with self.locked(), closing(self.connect()) as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM moves WHERE status IN ('prepared','returning') LIMIT 100"
                )
            ]
            for row in rows:
                guard()
                source, target = clean_path(row["source"]), clean_path(row["target"])
                if source.parent != self.root or source == target:
                    self._update(row["id"], "attention", "Некорректный путь в журнале")
                    continue
                if row["status"] == "returning":
                    source, target = target, source
                stamp = json.loads(row["stamp"])
                final = "returned" if row["status"] == "returning" else "moved"
                if self.matches(target, stamp) and not source.exists():
                    self._update(row["id"], final, "Результат подтверждён после перезапуска")
                elif self.matches(source, stamp) and not target.exists():
                    self._update(
                        row["id"],
                        "moved" if final == "returned" else "aborted",
                        "Операция не началась; файлы сохранены",
                    )
                elif self.matches(source, stamp) and self.matches(target, stamp):
                    with closed_file(source):
                        guard()
                        if self.matches(source, stamp) and self.matches(target, stamp):
                            source.unlink()
                            self._update(row["id"], final, "Завершено зафиксированное перемещение")
                else:
                    self._update(
                        row["id"],
                        "attention",
                        "Пути или файл изменились; требуется ручная проверка",
                    )
        return len(rows)


def locked_hash(stream, guard):
    result = hashlib.sha256()
    stream.seek(0)
    while True:
        guard()
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return result.hexdigest()
        result.update(chunk)
