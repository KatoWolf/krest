"""Selected download folders, processed by the existing cooperative AFK worker."""

import fnmatch
import hashlib
from pathlib import Path
import time
import uuid

from autonomy_files import (
    FileJobs,
    Paused,
    MAX_BYTES,
    clean_path,
    digest_file,
    fingerprint,
    is_link,
)

INCOMPLETE = {".part", ".partial", ".crdownload", ".tmp", ".download"}


class DownloadWorkflows:
    def __init__(self, runtime, store):
        self.runtime, self.store = runtime, store
        self.observed = {}
        self.revision = 0

    def rules(self):
        return self.store.rows("download_rules", 30)

    def add(self, source, destination, pattern="*.zip", unpack=True, duplicates=True):
        source, destination = clean_path(source), clean_path(destination)
        if not source.is_dir() or not destination.is_dir():
            raise ValueError("Выберите существующие папки загрузки и результата")
        if (
            source == destination
            or source.is_relative_to(destination)
            or destination.is_relative_to(source)
        ):
            raise ValueError("Папки загрузок и результата должны быть раздельными")
        pattern = str(pattern).strip()
        if not pattern or len(pattern) > 100 or any(c in pattern for c in "/\\:"):
            raise ValueError("Укажите маску имени, например *.zip или *мод*.zip")
        if any(type(v) is not bool for v in (unpack, duplicates)):
            raise ValueError("Некорректные переключатели обработки")
        rows = self.rules()
        if len(rows) >= 30:
            raise ValueError("Допускается до 30 правил обработки загрузок")
        if any(
            r["source"] == str(source) and r["pattern"].casefold() == pattern.casefold()
            for r in rows
        ):
            raise ValueError("Для этой папки и маски правило уже существует")
        row = self.store.put(
            "download_rules",
            {
                "source": str(source),
                "destination": str(destination),
                "pattern": pattern,
                "unpack": unpack,
                "duplicates": duplicates,
                "enabled": True,
            },
            limit=30,
        )
        self.revision += 1
        return {
            "id": row["id"],
            "text": "Правило включено: "
            + pattern
            + ". Обработка только во время AFK; исходники сохраняются.",
        }

    @staticmethod
    def matches(rule, path):
        path = Path(path)
        return (
            rule.get("enabled")
            and path.parent == Path(rule["source"])
            and any(
                fnmatch.fnmatch(path.name.casefold(), p.strip().casefold())
                for p in rule["pattern"].split(";")
            )
        )

    def reserved(self, path):
        return any(self.matches(r, path) for r in self.rules())

    def enabled(self):
        return any(r["enabled"] for r in self.rules())

    def change(self, ident, enabled=None):
        row = self.store.get("download_rules", ident)
        if row is None:
            raise ValueError("Правило не найдено")
        if enabled is None:
            self.store.remove("download_rules", ident)
        else:
            row["enabled"] = bool(enabled)
            self.store.put("download_rules", row, limit=30)
        self.revision += 1
        return "Правила загрузок обновлены"

    def run(self, duty):
        processed = skipped = 0
        errors = []
        now = time.time()
        seen = set()
        for rule in self.rules():
            duty.guard()
            if not rule["enabled"]:
                continue
            root, destination = clean_path(rule["source"]), clean_path(rule["destination"])
            if not root.is_dir() or not destination.is_dir():
                errors.append("Папка правила недоступна: " + str(root))
                continue
            count = 0
            for path in root.iterdir():
                duty.guard()
                count += 1
                if count > 2000:
                    errors.append("Разделите папку загрузок: максимум 2000 объектов за проход")
                    break
                if (
                    path.name.startswith(".")
                    or is_link(path)
                    or not path.is_file()
                    or path.suffix.casefold() in INCOMPLETE
                    or not self.matches(rule, path)
                ):
                    continue
                key = rule["id"] + ":" + str(path)
                seen.add(key)
                stamp = fingerprint(path)
                previous = self.observed.get(key)
                if previous is None or previous[0] != stamp:
                    self.observed[key] = (stamp, now)
                    skipped += 1
                    continue
                if now - previous[1] < 120 or now - path.stat().st_mtime < 120:
                    skipped += 1
                    continue
                ident = hashlib.sha256(key.encode()).hexdigest()
                existing = self.store.get("download_items", ident)
                if existing and existing.get("stamp") != stamp:
                    existing = None
                if existing and existing.get("status") in {"completed", "failed"}:
                    continue
                if (
                    existing
                    and existing.get("status") == "retry"
                    and now < existing.get("retry_at", 0)
                ):
                    continue
                item = existing or {
                    "id": ident,
                    "rule": rule["id"],
                    "source": str(path),
                    "stamp": stamp,
                    "checkpoint": {},
                    "job_id": uuid.uuid4().hex,
                }

                rule_state = [-1, False]

                def guard():
                    duty.guard()
                    if rule_state[0] != self.revision:
                        current = self.store.get("download_rules", rule["id"])
                        rule_state[:] = [self.revision, bool(current and current["enabled"])]
                    if not rule_state[1]:
                        raise Paused("Правило обработки загрузок отключено")

                class CheckpointStore:
                    def update(inner, job_id, **values):
                        item.update(values)
                        self.store.put("download_items", item, limit=60000)

                files = FileJobs(CheckpointStore(), guard, duty.progress)
                job = {"id": item["job_id"], "payload": {}, "checkpoint": item["checkpoint"]}
                try:
                    if stamp[0] > MAX_BYTES:
                        raise ValueError("За одну операцию поддерживаются файлы до 20 ГБ")
                    # Guard is checked on each chunk. Compare only same-size files in
                    # the explicitly chosen destination, never scan the whole disk.
                    content_hash = digest_file(path, guard)
                    if fingerprint(path) != stamp:
                        raise OSError("Загрузка ещё изменяется")
                    duplicate = None
                    if rule["duplicates"]:
                        for index, candidate in enumerate(destination.iterdir()):
                            guard()
                            if index >= 500:
                                break
                            if (
                                not is_link(candidate)
                                and candidate.is_file()
                                and candidate.stat().st_size == stamp[0]
                                and digest_file(candidate, guard) == content_hash
                            ):
                                duplicate = candidate
                                break
                    target = destination / path.name
                    if duplicate:
                        target = duplicate
                    else:
                        if target.exists() and str(target) not in job["checkpoint"].get(
                            "copies", {}
                        ):
                            target = destination / (
                                path.stem + "-" + item["job_id"][:8] + path.suffix
                            )
                        files.copy_file(path, target, job, expected_hash=content_hash)
                    message = (
                        "Найдена точная копия: " if duplicate else "Подготовлен файл: "
                    ) + str(target)
                    if rule["unpack"] and path.suffix.casefold() == ".zip":
                        job["payload"] = {"source": str(target), "destination": str(destination)}
                        message += "\n" + files.unpack(job)
                    if fingerprint(path) != stamp:
                        raise OSError("Исходник изменился во время обработки")
                    item.update(status="completed", result=message, destination=str(target))
                    processed += 1
                except Paused:
                    item.update(status="waiting", result="Продолжу при AFK")
                    self.store.put("download_items", item, limit=60000)
                    raise
                except OSError as exc:
                    item.update(status="retry", result=str(exc)[:500], retry_at=now + 300)
                    errors.append(path.name + ": " + str(exc))
                except Exception as exc:
                    item.update(status="failed", result=str(exc)[:500])
                    errors.append(path.name + ": " + str(exc))
                self.store.put("download_items", item, limit=60000)
        self.observed = {k: v for k, v in self.observed.items() if k in seen}
        return f"Обработка загрузок: готово {processed}; ожидают стабильности {skipped}." + (
            " Ошибки: " + "; ".join(errors[:3]) if errors else ""
        )
