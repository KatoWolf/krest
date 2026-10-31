"""Bounded incoming-file work while the PC is in use; heavy jobs stay in AFK."""

from datetime import datetime, timedelta
import hashlib
import os
from pathlib import Path
import threading
import time
import uuid

import psutil
from autonomy_files import clean_path, fingerprint, Paused
from file_moves import MoveJournal

GROUPS = {
    "архив": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "документ": {".pdf", ".doc", ".docx", ".odt", ".txt", ".rtf", ".xls", ".xlsx", ".ppt", ".pptx"},
    "картинка": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"},
    "видео": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
    "файл": set(),
}


def key_for(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def local_folder(value):
    path = clean_path(str(value))
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Для лёгкого фона выберите локальную папку")
    if not path.is_dir() or path == Path(path.anchor):
        raise ValueError("Выберите существующую отдельную папку, а не весь диск")
    return path


class LightBudget:
    def __init__(self, runtime):
        self.runtime = runtime
        self.next_check = 0
        self.reason = "Проверяю нагрузку"
        self.io = None
        self.io_at = 0
        self.voice_until = 0

    def voice(self):
        self.voice_until = time.monotonic() + 8

    def allowed(self):
        now = time.monotonic()
        cfg = self.runtime.settings()
        if cfg.get("safe_mode"):
            return False, "Безопасный режим"
        if cfg.get("file_background", {}).get("paused", False):
            return False, "Фоновая обработка приостановлена вами"
        if (
            now < self.voice_until
            or getattr(self.runtime, "current_command", "")
            or getattr(self.runtime, "_running", {})
            or now < getattr(self.runtime, "_active_until", 0)
        ):
            return False, "Приоритет вашей команды"
        if self.runtime.stop_event.is_set():
            return False, "Крест завершает работу"
        if now >= self.next_check:
            self.next_check = now + 3
            self.reason = ""
            try:
                if psutil.cpu_percent(interval=None) > 85:
                    self.reason = "Высокая нагрузка на процессор"
                elif psutil.virtual_memory().available < 512 * 1024**2:
                    self.reason = "Мало свободной памяти"
                io = psutil.disk_io_counters()
                if io and self.io and now > self.io_at:
                    elapsed = now - self.io_at
                    throughput = (
                        io.read_bytes + io.write_bytes - self.io.read_bytes - self.io.write_bytes
                    ) / elapsed
                    busy = (getattr(io, "busy_time", 0) - getattr(self.io, "busy_time", 0)) / (
                        elapsed * 1000
                    )
                    if throughput > 40 * 1024**2 or busy > 0.8:
                        self.reason = self.reason or "Диск занят; жду снижения нагрузки"
                self.io, self.io_at = io, now
            except (OSError, psutil.Error):
                self.reason = "Не удалось проверить нагрузку"
        return not self.reason, self.reason or "Лёгкий фон доступен, в том числе во время игры"

    def guard(self):
        allowed, reason = self.allowed()
        if not allowed:
            raise Paused(reason)


class IncomingFiles:
    def __init__(self, runtime, store):
        self.runtime, self.store = runtime, store
        self.budget = LightBudget(runtime)
        self.lock = threading.RLock()
        self.observed = {}
        self.iterators = {}
        self.journals = {}
        self.due = {}
        self.source_cursor = 0
        self.status = "Фон готов; включите сортировку или добавьте правило"
        self.example = None
        self.reference = None
        self.last_result = ""
        self._roots = {r["path"] for r in store.rows("incoming_roots", 100)}
        self._revision = 0
        self._snapshot = None
        self._snapshot_at = 0
        self._prune_at = 0
        self._prune_cursor = 0

    def sources(self):
        cfg = self.runtime.settings()
        app, policy = cfg.get("app_settings", {}), cfg.get("file_policy", {})
        selected = self.store.rows("incoming_sources", 20)
        rows = {r["path"]: dict(r) for r in selected}
        for folder in [app.get("downloads_path", ""), *policy.get("folders", [])]:
            if folder:
                rows.setdefault(
                    folder,
                    dict(id=key_for(folder), path=folder, label="Загрузки браузера", enabled=True),
                )
        for rule in self.runtime.smart.downloads.rules():
            folder = rule["source"]
            rows.setdefault(
                folder,
                dict(
                    id=key_for(folder),
                    path=folder,
                    label=rule.get("source_label") or "Папка правила",
                    enabled=True,
                ),
            )
        return list(rows.values())[:30]

    def _source_path(self, value):
        root = local_folder(value)
        import sys

        program = (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent
        )
        for protected in (Path(self.store.path).parent, program):
            if root == protected or root.is_relative_to(protected):
                raise ValueError("Папки программы и данных Крест нельзя автоматически разбирать")
        return root

    def _journal(self, root):
        with self.lock:
            return self._journal_locked(root)

    def _journal_locked(self, root):
        root = str(local_folder(root))
        if root not in self.journals:
            self.journals[root] = MoveJournal(root)
        if root not in self._roots:
            if len(self._roots) >= 100:
                raise ValueError("В истории уже 100 папок; проверьте старые источники")
            self.store.put("incoming_roots", dict(id=key_for(root), path=root), limit=100)
            self._roots.add(root)
        return self.journals[root]

    def _close_iterator(self, root):
        iterator = self.iterators.pop(root, None)
        if iterator:
            iterator.close()

    def close(self):
        with self.lock:
            for root in list(self.iterators):
                self._close_iterator(root)

    def tick(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            cfg = self.runtime.settings()
            sort = bool(
                cfg.get("app_settings", {}).get("downloads_auto_sort")
                and cfg.get("skills", {}).get("system_mon", True)
            )
            rules = self.runtime.smart.downloads.rules()
            active_rules = [r for r in rules if r["enabled"]]
            if not sort and not active_rules:
                self.status = "Сортировка выключена; активных правил нет"
                for root in list(self.iterators):
                    self._close_iterator(root)
                return
            allowed, reason = self.budget.allowed()
            if not allowed:
                self.status = reason
                return
            sources = [
                s
                for s in self.sources()
                if s["enabled"] and (sort or any(r["source"] == s["path"] for r in active_rules))
            ]
            live = {s["path"] for s in sources}
            for root in list(self.iterators):
                if root not in live:
                    self._close_iterator(root)
            if not sources:
                self.status = "Нет включённых папок"
                return
            source = sources[self.source_cursor % len(sources)]
            self.source_cursor += 1
            root = source["path"]
            if time.monotonic() < self.due.get(root, 0):
                return
            self._scan(source, active_rules, sort, cfg.get("file_policy", {}))
        except Paused as exc:
            self.status = str(exc)
        except (OSError, ValueError) as exc:
            self.status = str(exc)[:350]
        finally:
            self.lock.release()

    def _scan(self, source, rules, sort, policy):
        import fnmatch
        from skills.system_mon import get_category_for_extension, INCOMPLETE_DOWNLOAD_EXTENSIONS
        from task_watch import file_available

        root = str(self._source_path(source["path"]))
        journal = self._journal(root)
        journal.recover(self.budget.guard)
        iterator = self.iterators.get(root)
        if iterator is None:
            iterator = self.iterators[root] = os.scandir(root)
        game = bool(self.runtime.performance.game)
        deadline = time.monotonic() + (0.015 if game else 0.035)
        moved = 0
        count = 0
        batch = uuid.uuid4().hex
        for _ in range(16 if game else 64):
            self.budget.guard()
            if time.monotonic() >= deadline:
                break
            try:
                entry = next(iterator)
            except StopIteration:
                self._close_iterator(root)
                self.due[root] = time.monotonic() + 30
                break
            count += 1
            path = Path(entry.path)
            if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                continue
            if path.suffix.casefold() in INCOMPLETE_DOWNLOAD_EXTENSIONS or not path.suffix:
                continue
            if any(
                fnmatch.fnmatch(entry.name.casefold(), p.casefold())
                for p in policy.get("exclusions", [])
            ):
                continue
            try:
                path = clean_path(path)
                stamp = fingerprint(path)
                previous = self.observed.get(str(path))
                now = time.time()
                if not previous or previous[0] != stamp:
                    if previous or len(self.observed) < 5000:
                        self.observed[str(path)] = (stamp, now)
                    continue
                if (
                    now - previous[1] < 120
                    or now - path.stat().st_mtime < 120
                    or not file_available(path)
                ):
                    continue
                if journal.was_returned(path, stamp):
                    continue
                chosen = next(
                    (r for r in rules if self.runtime.smart.downloads.matches(r, path)), None
                )
                item = dict(
                    id=key_for(path),
                    source=str(path),
                    current=str(path),
                    label=source["label"],
                    stamp=stamp,
                    received=previous[1],
                    status="stable",
                )
                if chosen:
                    if chosen.get("mode", "copy") != "move":
                        item.update(
                            status="afk", detail="Проверенное копирование / распаковка — при AFK"
                        )
                        self._remember(item)
                        continue
                    destination = clean_path(chosen["destination"])
                elif sort:
                    category = get_category_for_extension(path.suffix.casefold())
                    for rule in policy.get("rules", []):
                        if path.suffix.casefold() in rule["extensions"]:
                            category = rule["category"]
                            break
                    if (
                        Path(category).name != category
                        or category in {".", ".."}
                        or any(c in category for c in "/\\")
                    ):
                        raise ValueError("Некорректное имя категории")
                    destination = clean_path(Path(root) / category)
                    destination.mkdir(exist_ok=True)
                else:
                    self._remember(item)
                    continue
                if not destination.is_dir():
                    raise ValueError("Папка результата недоступна: " + str(destination))
                if path.stat().st_dev != destination.stat().st_dev:
                    item.update(
                        status="afk",
                        detail="Перенос на другой диск — проверенное копирование при AFK",
                    )
                    self._remember(item)
                    continue
                record = journal.move(
                    path,
                    destination,
                    batch=batch,
                    label=source["label"],
                    received=previous[1],
                    guard=self.budget.guard,
                )
                item.update(
                    current=record["target"],
                    status="moved",
                    move_id=record["id"],
                    root=root,
                    detail=record["detail"],
                )
                self._remember(item)
                self.observed.pop(str(path), None)
                moved += 1
                if moved >= (2 if game else 6):
                    break
            except (OSError, ValueError) as exc:
                self.last_result = path.name + ": " + str(exc)[:200]
        if time.monotonic() >= self._prune_at:
            self._prune_at = time.monotonic() + 30
            keys = list(self.observed)
            if self._prune_cursor >= len(keys):
                self._prune_cursor = 0
            active = {s["path"] for s in self.sources() if s["enabled"]}
            for key in keys[self._prune_cursor : self._prune_cursor + 32]:
                if str(Path(key).parent) not in active or not Path(key).is_file():
                    self.observed.pop(key, None)
            self._prune_cursor += 32
        self.status = f"Лёгкий фон: проверено {count}, перемещено {moved}." + (
            " Игра продолжается." if game else ""
        )
        if moved:
            self.last_result = self.status
            self.runtime.events.put(("incoming_changed", None))
        self.runtime.download_status = self.status

    def _remember(self, item):
        previous = self.store.get("incoming_items", item["id"])
        if previous and previous.get("stamp") == item["stamp"]:
            item["received"] = previous["received"]
        if previous != item:
            self.store.put("incoming_items", item, limit=5000)
            self._revision += 1
            self._snapshot_at = 0

    def history(self, limit=200):
        items = {r["id"]: r for r in self.store.rows("incoming_items", 500)}
        roots = set(self._roots)
        roots.update(
            s["path"]
            for s in self.sources()
            if (Path(s["path"]) / ".krest-moves.sqlite3").is_file()
        )
        for root in roots:
            try:
                for row in self._journal(root).rows(100):
                    if row["status"] not in {
                        "moved",
                        "returned",
                        "attention",
                        "prepared",
                        "returning",
                        "prepared_copy",
                        "returning_copy",
                    }:
                        continue
                    ident = key_for(row["source"])
                    old = items.get(ident, {})
                    if (
                        old.get("received", 0) > row["received"]
                        or old.get("move_created", 0) > row["created"]
                    ):
                        continue
                    items[ident] = dict(
                        old,
                        id=ident,
                        source=row["source"],
                        current=row["source"]
                        if row["status"] in {"returned", "prepared", "prepared_copy"}
                        else row["target"],
                        status=row["status"],
                        label=row["label"],
                        received=row["received"],
                        move_id=row["id"],
                        move_created=row["created"],
                        root=root,
                        detail=row["detail"],
                    )
            except (OSError, ValueError):
                continue
        return sorted(items.values(), key=lambda r: r["received"], reverse=True)[:limit]

    def snapshot(self):
        with self.lock:
            if self._snapshot is None or time.monotonic() - self._snapshot_at > 5:
                self._snapshot = dict(items=self.history(), sources=self.sources())
                self._snapshot_at = time.monotonic()
            return {
                **self._snapshot,
                "status": self.status,
                "last_result": self.last_result,
                "paused": self.runtime.settings().get("file_background", {}).get("paused", False),
            }

    def find(self, group="файл", when="последний", query=""):
        self.reference = None
        self.example = None
        extensions = GROUPS.get(group)
        if extensions is None:
            raise ValueError("Ищу архивы, документы, картинки, видео или файлы")
        day = datetime.now().date() - timedelta(days=1 if when == "вчерашний" else 0)
        rows = [
            r
            for r in self.history(500)
            if (not extensions or Path(r["current"]).suffix.casefold() in extensions)
            and (when != "вчерашний" or datetime.fromtimestamp(r["received"]).date() == day)
            and (not query or query.casefold() in Path(r["current"]).name.casefold())
        ]
        rows = [r for r in rows if Path(r["current"]).is_file()]
        if not rows:
            raise ValueError("В истории Крест такого файла нет")
        row = rows[0]
        if (when == "вчерашний" or query) and len(rows) > 1:
            raise ValueError(
                "Найдено несколько файлов. Уточните название или выберите строку в истории"
            )
        self.reference = (row["id"], time.monotonic())
        self.example = dict(
            path=row["source"], suffix=Path(row["source"]).suffix, label=row["label"]
        )
        return row

    def chosen(self, ident=None):
        if not ident and self.reference and time.monotonic() - self.reference[1] < 300:
            ident = self.reference[0]
        row = next((r for r in self.history(500) if r["id"] == ident), None)
        if not row:
            raise ValueError("Сначала найдите файл голосом или выберите его в истории")
        self.reference = (row["id"], time.monotonic())
        self.example = dict(
            path=row["source"], suffix=Path(row["source"]).suffix, label=row["label"]
        )
        return row

    def call(self, action, **values):
        with self.lock:
            if action == "snapshot":
                return self.snapshot()
            if action == "sorting":
                with self.runtime.config_manager._lock:
                    cfg = self.runtime.settings()
                    cfg["app_settings"]["downloads_auto_sort"] = bool(values.get("enabled", True))
                    self.runtime.config_manager.save_config(cfg)
                return (
                    "Автосортировка включена"
                    if values.get("enabled", True)
                    else "Автосортировка выключена"
                )
            if action == "pause":
                with self.runtime.config_manager._lock:
                    cfg = self.runtime.settings()
                    cfg.setdefault("file_background", {})["paused"] = bool(
                        values.get("paused", True)
                    )
                    self.runtime.config_manager.save_config(cfg)
                return (
                    "Фоновая обработка файлов приостановлена"
                    if values.get("paused", True)
                    else "Лёгкая обработка файлов включена"
                )
            if action == "source":
                path = str(self._source_path(values["path"]))
                label = str(values.get("label") or Path(path).name).strip()[:80]
                if not label:
                    raise ValueError("Назовите источник: браузер, Telegram или Discord")
                rows = self.store.rows("incoming_sources", 20)
                if len(rows) >= 20 and not any(r["path"] == path for r in rows):
                    raise ValueError("Можно выбрать до 20 источников")
                self.store.put(
                    "incoming_sources",
                    dict(
                        id=key_for(path),
                        path=path,
                        label=label,
                        enabled=bool(values.get("enabled", True)),
                    ),
                    limit=20,
                )
                self._snapshot_at = 0
                self.runtime.smart.downloads.revision += 1
                return "Источник сохранён: " + label
            if action == "example":
                path = clean_path(values["path"])
                if not path.is_file() or not path.suffix:
                    raise ValueError("Выберите файл с расширением")
                self.example = dict(path=str(path), suffix=path.suffix, label=path.parent.name)
                return (
                    "Образец: "
                    + path.name
                    + ". Откройте папку назначения и скажите «такие файлы складывай сюда»"
                )
            if action == "learn":
                if not self.example:
                    raise ValueError(
                        "Сначала найдите файл голосом или выберите образец в меню файлов"
                    )
                destination = local_folder(values["destination"])
                source = str(Path(self.example["path"]).parent)
                return self.runtime.smart.downloads.add(
                    source,
                    str(destination),
                    "*" + self.example["suffix"].lower(),
                    unpack=False,
                    duplicates=False,
                    mode="move",
                    source_label=self.example["label"],
                )["text"]
            if action == "find":
                return self.find(**values)
            if action == "select":
                row = self.chosen(values.get("id"))
                return dict(
                    row,
                    text="Образец выбран. Откройте папку назначения и скажите: «такие файлы складывай сюда»",
                )
            if action in {"open", "reveal", "undo"}:
                row = self.chosen(values.get("id"))
                if action == "undo":
                    if not row.get("move_id"):
                        raise ValueError("Этот файл Крест не перемещала")
                    journal = self._journal(row["root"])
                    record = journal.get(row["move_id"])
                    if record and record["hash"]:
                        result = self.runtime.autonomy.call(
                            "enqueue",
                            kind="file_return",
                            values={"root": row["root"], "move_id": row["move_id"]},
                            label="Возврат: " + Path(row["source"]).name,
                        )
                        return result["text"]
                    restored = journal.undo(row["move_id"], guard=lambda: self._command_guard())
                    self._snapshot_at = 0
                    self._revision += 1
                    return "Вернула файл: " + restored
                path = clean_path(row["current"])
                if not path.is_file():
                    raise ValueError("Файл больше не находится по сохранённому пути")
                from object_actions import open_path

                return open_path(path, reveal=action == "reveal")
        raise ValueError("Неизвестное действие файлового помощника")

    def _command_guard(self):
        if self.runtime.stop_event.is_set() or self.runtime.command_stop.is_set():
            raise OSError("Возврат остановлен")
