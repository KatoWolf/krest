"""Conservative downloads sorting, shared by manual and background commands."""

import os
from pathlib import Path
import shutil
import threading
import time

_sort_lock = threading.Lock()


class _CancellableReader:
    def __init__(self, source, stop):
        self.source, self.stop = source, stop

    def read(self, size):
        if self.stop and self.stop.is_set():
            raise InterruptedError("Сортировка остановлена")
        return self.source.read(size)


class DownloadSorter:
    def __init__(self):
        self.observed = {}
        self.root = None

    def scan(self, folder, *, automatic=True, now=None, stop=None):
        from skills.system_mon import get_category_for_extension, INCOMPLETE_DOWNLOAD_EXTENSIONS

        now = time.time() if now is None else now
        root = Path(folder).expanduser()
        if not root.is_dir():
            raise ValueError("Папка загрузок не найдена: " + str(root))
        if root != self.root:
            self.observed.clear()
            self.root = root
        result = {"moved_count": 0, "skipped_count": 0, "errors": [], "moved_by_category": {}}
        if not _sort_lock.acquire(blocking=False):
            return result
        try:
            seen = set()
            for path in root.iterdir():
                if stop and stop.is_set():
                    break
                seen.add(path.name)
                try:
                    if path.is_symlink() or not path.is_file() or path.name.startswith("."):
                        continue
                    ext = path.suffix.lower()
                    if not ext or ext in INCOMPLETE_DOWNLOAD_EXTENSIONS:
                        continue
                    info = path.stat()
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
                    target_dir = root / folder_name
                    if target_dir.is_symlink():
                        raise ValueError("Папка категории является ссылкой: " + folder_name)
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
                    try:
                        with output, path.open("rb") as source:
                            shutil.copyfileobj(
                                _CancellableReader(source, stop), output, length=1024 * 1024
                            )
                            output.flush()
                            os.fsync(output.fileno())
                        after = path.stat()
                        if (after.st_size, after.st_mtime_ns) != stamp or (stop and stop.is_set()):
                            target.unlink()
                            result["skipped_count"] += 1
                            continue
                        shutil.copystat(path, target)
                        path.unlink()
                    except Exception:
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
            _sort_lock.release()
