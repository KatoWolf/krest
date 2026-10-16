"""Cooperative file jobs. Originals survive until a verified copy is committed."""

import hashlib
import os
from pathlib import Path
import shutil
import stat
import time
import zipfile

CHUNK = 1024 * 1024
MAX_FILES = 10000
MAX_BYTES = 20 * 1024**3
DISK_RESERVE = 2 * 1024**3


def is_link(path):
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


class Paused(Exception):
    pass


def clean_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Нужен абсолютный путь")
    for node in (path, *path.parents):
        if is_link(node):
            raise ValueError("Ссылки и точки соединения не обслуживаются: " + str(node))
    return Path(os.path.abspath(path))


def digest_file(path, guard):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            guard()
            block = stream.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path):
    value = path.stat()
    return [value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino]


class FileJobs:
    def __init__(self, store, guard, progress):
        self.store, self.guard, self.progress = store, guard, progress

    def copy_file(self, source, destination, job, *, expected_hash=""):
        source, destination = clean_path(source), clean_path(destination)
        self.guard()
        if not source.is_file():
            raise FileNotFoundError("Файл пока недоступен: " + str(source))
        stamp = fingerprint(source)
        if stamp[0] > MAX_BYTES:
            raise ValueError("Файл превышает ограничение 20 ГБ одной операции")
        if time.time() - source.stat().st_mtime < 120:
            raise OSError("Файл недавно изменялся; повторю после стабилизации: " + source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        clean_path(destination)
        checkpoint = job["checkpoint"]
        key = str(destination)
        entry = checkpoint.setdefault("copies", {}).get(key, {})
        partial = destination.with_name("." + destination.name + "." + job["id"] + ".partial")
        clean_path(partial)
        if entry.get("stamp") != stamp:
            # This private temp belongs to this job only. Never delete a target.
            partial.unlink(missing_ok=True)
            entry = {"stamp": stamp}
            checkpoint["copies"][key] = entry
            self.store.update(job["id"], checkpoint=checkpoint)
        if destination.exists():
            # Recover a crash between committing the final file and recording it.
            known = entry.get("hash")
            if (
                known
                and destination.stat().st_size == stamp[0]
                and digest_file(destination, self.guard) == known
            ):
                return known
            raise FileExistsError("Файл назначения уже существует: " + str(destination))
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > stamp[0]:
            partial.unlink()
            offset = 0
        if shutil.disk_usage(destination.parent).free < stamp[0] - offset + DISK_RESERVE:
            raise OSError("Недостаточно места на диске назначения")
        with source.open("rb") as original, partial.open("ab") as output:
            original.seek(offset)
            while True:
                self.guard()
                block = original.read(CHUNK)
                if not block:
                    break
                output.write(block)
                offset += len(block)
                self.progress(
                    f"{source.name}: {offset // 1024**2} / {max(1, stamp[0] // 1024**2)} МБ"
                )
                # Bounded IO throughput; the voice process keeps its CPU budget.
                time.sleep(0.01)
            output.flush()
            os.fsync(output.fileno())
        original_hash = digest_file(source, self.guard)
        if expected_hash and original_hash != expected_hash:
            raise ValueError("Контрольная сумма сохранённой версии не совпала")
        if fingerprint(source) != stamp or digest_file(partial, self.guard) != original_hash:
            partial.unlink(missing_ok=True)
            raise OSError("Исходный файл изменился во время копирования; повторю позже")
        entry["hash"] = original_hash
        self.store.update(job["id"], checkpoint=checkpoint)
        self.guard()
        shutil.copystat(source, partial)
        # A hard link commits exclusively; never overwrite a file that appeared
        # while we were copying. Fall back to an exclusive copy on non-NTFS disks.
        try:
            os.link(partial, destination)
        except FileExistsError:
            raise
        except OSError:
            if destination.exists():
                raise FileExistsError(str(destination))
            # Windows rename is exclusive (unlike POSIX rename).
            if os.name == "nt":
                os.rename(partial, destination)
            else:
                raise OSError("Диск не поддерживает безопасную фиксацию копии")
        partial.unlink(missing_ok=True)
        return original_hash

    def entries(self, source):
        source = clean_path(source)
        if source.is_file():
            yield source, Path(source.name)
            return
        if not source.is_dir():
            raise FileNotFoundError("Папка пока недоступна: " + str(source))
        count, total = 0, 0
        for parent, directories, files in os.walk(source, followlinks=False):
            self.guard()
            directories[:] = sorted(
                d for d in directories if not d.startswith(".") and not is_link(Path(parent) / d)
            )
            for name in sorted(files):
                self.guard()
                path = Path(parent) / name
                if (
                    name.startswith(".")
                    or is_link(path)
                    or path.suffix.lower()
                    in {".partial", ".crdownload", ".part", ".tmp", ".download"}
                ):
                    continue
                clean_path(path)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                count += 1
                total += info.st_size
                if count > MAX_FILES or total > MAX_BYTES:
                    raise ValueError("Разделите задачу: предел 10 000 файлов или 20 ГБ за проход")
                if time.time() - info.st_mtime < 120:
                    continue
                yield path, path.relative_to(source)

    def folder(self, job):
        payload, checkpoint = job["payload"], job["checkpoint"]
        source, destination = clean_path(payload["source"]), clean_path(payload["destination"])
        if (
            destination == source
            or destination.is_relative_to(source)
            or source.is_relative_to(destination)
        ):
            raise ValueError("Папки источника и назначения должны быть раздельными")
        destination.mkdir(parents=True, exist_ok=True)
        done = set(checkpoint.get("done", []))
        moved = job["kind"] == "offload"
        root_free = payload.get("min_free_gb", 30) * 1024**3
        for path, relative in self.entries(source):
            self.guard()
            if str(relative) in done:
                continue
            if moved and shutil.disk_usage(source).free >= root_free:
                break
            info = path.stat()
            if moved and time.time() - info.st_mtime < payload.get("age_days", 7) * 86400:
                continue
            previous = [
                r
                for r in self.store.versions(path)
                if r["kind"] == job["kind"] and Path(r["destination"]).is_relative_to(destination)
            ]
            if not moved and previous:
                row = previous[0]
                if (
                    row["size"] == info.st_size
                    and row["mtime"] == info.st_mtime_ns
                    and Path(row["destination"]).is_file()
                ):
                    # Stat equality alone is not a backup guarantee.
                    if (
                        digest_file(path, self.guard) == row["hash"]
                        and digest_file(clean_path(row["destination"]), self.guard) == row["hash"]
                    ):
                        continue
            if job["kind"] == "backup":
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(job["created"]))
                target = destination / (stamp + "-" + job["id"][:8]) / relative
            else:
                target = destination / relative
                if target.exists() and str(target) not in checkpoint.get("copies", {}):
                    target = target.with_name(target.stem + "-" + job["id"][:8] + target.suffix)
            digest = self.copy_file(path, target, job)
            self.store.version(
                path, target, digest, info, "move" if moved else job["kind"], job["id"]
            )
            if moved:
                self.guard()
                if (
                    path.stat().st_size != info.st_size
                    or path.stat().st_mtime_ns != info.st_mtime_ns
                ):
                    raise OSError("Исходник изменился; копия сохранена, оригинал оставлен")
                path.unlink()
            done.add(str(relative))
            checkpoint["done"] = sorted(done)
            self.store.update(job["id"], checkpoint=checkpoint)
            if job["kind"] == "backup":
                self.prune_versions(path, destination)
        return f"Проверено и {'перемещено' if moved else 'сохранено'} файлов: {len(done)}. Папка: {destination}"

    def prune_versions(self, source, root):
        rows = [
            r
            for r in self.store.versions(source)
            if r["kind"] == "backup" and Path(r["destination"]).is_relative_to(root)
        ]
        for row in rows[5:]:
            self.guard()
            path = clean_path(row["destination"])
            # Only our recorded, unchanged old copies can be retired. Preserve
            # anything the user has edited, even if it exceeds the retention cap.
            if path.is_file() and digest_file(path, self.guard) == row["hash"]:
                self.guard()
                path.unlink()
                self.store.forget_version(row["id"])
            elif not path.exists():
                self.store.forget_version(row["id"])

    def archive(self, job):
        source = clean_path(job["payload"]["source"])
        destination = clean_path(job["payload"]["destination"])
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / (source.name + "-" + job["id"][:8] + ".zip")
        partial = target.with_suffix(".zip.partial")
        clean_path(partial)
        if target.exists() and job["checkpoint"].get("hash"):
            if digest_file(target, self.guard) == job["checkpoint"]["hash"]:
                return "Архив проверен: " + str(target)
        self.guard()
        try:
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
                count = 0
                for path, relative in self.entries(source):
                    stamp = fingerprint(path)
                    with (
                        path.open("rb") as src,
                        bundle.open(str(relative), "w", force_zip64=True) as dst,
                    ):
                        while True:
                            self.guard()
                            block = src.read(CHUNK)
                            if not block:
                                break
                            if shutil.disk_usage(destination).free < DISK_RESERVE + CHUNK:
                                raise OSError("Оставляю резерв 2 ГБ на диске назначения")
                            dst.write(block)
                    if stamp != fingerprint(path):
                        raise OSError("Файл изменился во время архивирования: " + path.name)
                    count += 1
                    self.progress(f"В архиве файлов: {count}")
            # CRC validation also respects a return from AFK.
            with zipfile.ZipFile(partial) as bundle:
                for row in bundle.infolist():
                    with bundle.open(row) as entry:
                        while True:
                            self.guard()
                            if not entry.read(CHUNK):
                                break
            job["checkpoint"]["hash"] = digest_file(partial, self.guard)
            self.store.update(job["id"], checkpoint=job["checkpoint"])
            self.guard()
            if target.exists():
                raise FileExistsError(str(target))
            os.rename(partial, target)
            return "Архив создан и проверен: " + str(target)
        finally:
            # A partial ZIP cannot be safely resumed; retry only this archive.
            partial.unlink(missing_ok=True)

    def unpack(self, job):
        source = clean_path(job["payload"]["source"])
        root = clean_path(job["payload"]["destination"])
        root.mkdir(parents=True, exist_ok=True)
        destination = root / (source.stem + "-" + job["id"][:8])
        destination.mkdir(exist_ok=True)
        done = set(job["checkpoint"].get("done", []))
        stamp = fingerprint(source)
        previous_stamp = job["checkpoint"].get("archive_stamp")
        if previous_stamp and previous_stamp != stamp:
            raise ValueError("ZIP изменился после начала распаковки. Создайте новое поручение.")
        job["checkpoint"]["archive_stamp"] = stamp
        self.store.update(job["id"], checkpoint=job["checkpoint"])
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(v.file_size for v in entries) > MAX_BYTES:
                raise ValueError("Архив превышает предел 10 000 файлов / 20 ГБ")
            names = set()
            for item in entries:
                name = item.filename.replace("\\", "/")
                parts = name.rstrip("/").split("/")
                if any(
                    not p
                    or p in {".", ".."}
                    or ":" in p
                    or p.rstrip(" .") != p
                    or p.split(".")[0].upper()
                    in {
                        "CON",
                        "PRN",
                        "AUX",
                        "NUL",
                        *("COM" + str(i) for i in range(1, 10)),
                        *("LPT" + str(i) for i in range(1, 10)),
                    }
                    for p in parts
                ) or name.startswith("/"):
                    raise ValueError("Архив содержит небезопасный путь")
                if (
                    name.casefold() in names
                    or stat.S_ISLNK(item.external_attr >> 16)
                    or item.flag_bits & 1
                ):
                    raise ValueError(
                        "Повторяющиеся пути, ссылки и зашифрованные ZIP не поддерживаются"
                    )
                names.add(name.casefold())
            for item in entries:
                self.guard()
                name = item.filename.replace("\\", "/")
                target = clean_path(destination.joinpath(*name.rstrip("/").split("/")))
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                known = job["checkpoint"].get("unpacked", {}).get(name)
                if target.is_file() and known:
                    if digest_file(target, self.guard) == known:
                        done.add(name)
                        continue
                    raise ValueError("Распакованный файл был изменён: " + str(target))
                target.parent.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(target.parent).free < item.file_size + DISK_RESERVE:
                    raise OSError("Недостаточно места для распаковки")
                partial = target.with_name("." + target.name + "." + job["id"] + ".partial")
                clean_path(partial)
                with archive.open(item) as src, partial.open("wb") as dst:
                    while True:
                        self.guard()
                        block = src.read(CHUNK)
                        if not block:
                            break
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                job["checkpoint"].setdefault("unpacked", {})[name] = digest_file(
                    partial, self.guard
                )
                self.store.update(job["id"], checkpoint=job["checkpoint"])
                self.guard()
                if target.exists():
                    raise FileExistsError("Файл распаковки уже существует: " + str(target))
                os.rename(partial, target)
                done.add(name)
                job["checkpoint"]["done"] = sorted(done)
                self.store.update(job["id"], checkpoint=job["checkpoint"])
        return "ZIP распакован с проверкой CRC: " + str(destination)

    def restore(self, job):
        row = self.store.find_version(job["payload"]["version_id"])
        source = clean_path(row["destination"])
        original = clean_path(row["source"])
        destination = (
            original
            if row["kind"] == "move" and not original.exists()
            else original.with_name(
                original.stem + "-восстановлено-" + job["id"][:8] + original.suffix
            )
        )
        self.copy_file(source, destination, job, expected_hash=row["hash"])
        return "Восстановлена проверенная копия: " + str(destination)
