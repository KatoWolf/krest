"""Conservative downloads sorting, shared by manual and background commands."""

import fnmatch
import os
import hashlib
import uuid
from pathlib import Path
import shutil
import threading
import time

_sort_lock = threading.Lock()


class _CancellableReader:
    def __init__(self, source, stop):
        self.source, self.stop = source, stop
        self.hash = hashlib.sha256()

    def read(self, size):
        if self.stop and self.stop.is_set():
            raise InterruptedError("Сортировка остановлена")
        data = self.source.read(size)
        self.hash.update(data)
        return data


class DownloadSorter:
    def __init__(self, policy=None):
        self.policy = policy or {}
        self.observed = {}
        self.root = None

    def scan(self, folder, *, automatic=True, now=None, stop=None, preview=False):
        from skills.system_mon import get_category_for_extension, INCOMPLETE_DOWNLOAD_EXTENSIONS

        now = time.time() if now is None else now
        root = Path(folder).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Папка загрузок не найдена: " + str(root))
        if root != self.root:
            self.observed.clear()
            self.root = root
        result = {
            "preview": [],
            "moved_count": 0,
            "skipped_count": 0,
            "errors": [],
            "moved_by_category": {},
        }
        if not _sort_lock.acquire(blocking=False):
            return result
        from process_lock import ProcessLock

        process_lock = ProcessLock(root / ".krest-sort.lock")
        try:
            if not preview and not process_lock.acquire():
                raise ValueError("Сортировка этой папки уже выполняется")
            from sort_history import SortHistory

            journal = None if preview else SortHistory(root)
            batch = uuid.uuid4().hex
            result["batch"] = batch
            seen = set()
            for path in root.iterdir():
                if stop and stop.is_set():
                    break
                seen.add(path.name)
                try:
                    if path.is_symlink() or not path.is_file() or path.name.startswith("."):
                        continue
                    if any(
                        fnmatch.fnmatch(path.name.casefold(), pattern.casefold())
                        for pattern in self.policy.get("exclusions", [])
                    ):
                        result["skipped_count"] += 1
                        continue
                    ext = path.suffix.lower()
                    if not ext or ext in INCOMPLETE_DOWNLOAD_EXTENSIONS:
                        continue
                    info = path.stat()
                    if automatic and journal and journal.was_restored(path, info):
                        result["skipped_count"] += 1
                        continue
                    stamp = (info.st_size, info.st_mtime_ns)
                    previous = self.observed.get(path.name)
                    if previous is None or previous[:2] != stamp:
                        self.observed[path.name] = (*stamp, now)
                    stable = (
                        previous is not None and previous[:2] == stamp and now - previous[2] >= 120
                    )
                    if now - info.st_mtime < 120 or (automatic and not stable):
                        result["skipped_count"] += 1
                        continue
                    folder_name = get_category_for_extension(ext)
                    for rule in self.policy.get("rules", []):
                        if ext in rule["extensions"]:
                            folder_name = rule["category"]
                            break
                    if (
                        Path(folder_name).name != folder_name
                        or folder_name in (".", "..")
                        or "/" in folder_name
                        or "\\" in folder_name
                    ):
                        raise ValueError("Категория должна быть названием папки")
                    target_dir = root / folder_name
                    if target_dir.is_symlink():
                        raise ValueError("Папка категории является ссылкой: " + folder_name)
                    if preview:
                        result["preview"].append(
                            {"source": str(path), "category": folder_name, "bytes": info.st_size}
                        )
                        continue
                    if shutil.disk_usage(root).free < info.st_size + 10 * 1024 * 1024:
                        raise OSError("Недостаточно свободного места для безопасного копирования")
                    target_dir.mkdir(exist_ok=True)
                    target = None
                    # Exclusive creation preserves existing same-name files on every OS.
                    for number in range(10000):
                        candidate = target_dir / (
                            path.name if number == 0 else f"{path.stem} ({number}){path.suffix}"
                        )
                        try:
                            output = candidate.open("xb")
                            target = candidate
                            break
                        except FileExistsError:
                            continue
                    if target is None:
                        raise OSError("Слишком много файлов с одинаковым именем")
                    record_id = None
                    try:
                        with output, path.open("rb") as source:
                            reader = _CancellableReader(source, stop)
                            shutil.copyfileobj(reader, output, length=1024 * 1024)
                            output.flush()
                            os.fsync(output.fileno())
                        after = path.stat()
                        if (after.st_size, after.st_mtime_ns) != stamp or (stop and stop.is_set()):
                            target.unlink()
                            result["skipped_count"] += 1
                            continue
                        shutil.copystat(path, target)
                        record_id = journal.record(batch, path, target, reader.hash.hexdigest())
                        path.unlink()
                    except Exception:
                        try:
                            if record_id is not None:
                                journal.discard(record_id)
                        finally:
                            target.unlink(missing_ok=True)
                        raise
                    self.observed.pop(path.name, None)
                    result["moved_count"] += 1
                    result["moved_by_category"][folder_name] = (
                        result["moved_by_category"].get(folder_name, 0) + 1
                    )
                except (OSError, ValueError) as exc:
                    result["errors"].append(f"{path.name}: {exc}")
            self.observed = {k: v for k, v in self.observed.items() if k in seen}
            return result
        finally:
            process_lock.close()
            _sort_lock.release()
