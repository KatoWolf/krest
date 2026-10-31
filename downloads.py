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
        from autonomy_files import clean_path, fingerprint
        from file_moves import MoveJournal

        root = clean_path(Path(folder).expanduser().absolute())
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
            fast = None if preview else MoveJournal(root)
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
                    if (
                        automatic
                        and journal
                        and (
                            journal.was_restored(path, info)
                            or fast.was_returned(path, fingerprint(path))
                        )
                    ):
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

                    def guard():
                        if stop and stop.is_set():
                            raise InterruptedError("Сортировка остановлена")

                    fast.move(path, target_dir, batch=batch, guard=guard, locked=True)
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
