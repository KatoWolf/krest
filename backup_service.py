"""Portable, secret-free data snapshots with bounded, validated restoration."""

from contextlib import closing
from pathlib import Path
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile

FILES = {
    "config.json",
    "assistant_state.json",
    "favorites.json",
    "memory.sqlite3",
    "jobs.sqlite3",
    "inbox.sqlite3",
    "warframe_state.json",
    "smart_automation.sqlite3",
}
MAX_TOTAL = 64 * 1024 * 1024


def create_backup(config, destination, guard=None):
    from secret_store import SECRET_KEYS
    from storage import atomic_write_json

    destination = Path(destination)
    guard = guard or (lambda: None)
    guard()
    root = config.filepath.parent
    with tempfile.TemporaryDirectory(prefix="krest-backup-") as temp:
        staging = Path(temp)
        cfg = copy.deepcopy(config.snapshot())
        for key in SECRET_KEYS:
            cfg["api_keys"][key] = ""
        cfg["license"]["code"] = ""
        for service in cfg.get("services", {}).values():
            service["token"] = ""
            service["autostart"] = False
        cfg["event_rules"] = [dict(rule, enabled=False) for rule in cfg.get("event_rules", [])]
        cfg["app_settings"]["downloads_auto_sort"] = False
        cfg["audio_focus"]["enabled"] = False
        cfg["sound_dispatcher"]["enabled"] = False
        cfg["autonomy"]["enabled"] = False
        atomic_write_json(staging / "config.json", cfg)
        for filename in FILES - {"config.json"}:
            guard()
            source = root / filename
            if not source.exists() or source.is_symlink():
                continue
            if filename == "smart_automation.sqlite3":
                from smart_store import export_settings

                export_settings(source, staging / filename, guard)
                continue
            if source.stat().st_size > MAX_TOTAL:
                raise ValueError("Файл слишком большой для резервной копии: " + filename)
            if filename.endswith(".sqlite3"):
                with (
                    closing(sqlite3.connect(source)) as original,
                    closing(sqlite3.connect(staging / filename)) as target,
                ):
                    original.backup(target, pages=128, progress=lambda *args: guard())
            else:
                with source.open("rb") as original, (staging / filename).open("wb") as target:
                    while True:
                        guard()
                        block = original.read(1024 * 1024)
                        if not block:
                            break
                        target.write(block)
        manifest = {}
        total = 0
        for item in staging.iterdir():
            guard()
            raw = item.read_bytes()
            total += len(raw)
            manifest[item.name] = hashlib.sha256(raw).hexdigest()
        if total > MAX_TOTAL:
            raise ValueError("Копия превышает 64 МБ")
        temporary = destination.with_name(destination.name + ".partial")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps({"version": 1, "files": manifest}))
                for name in manifest:
                    with (staging / name).open("rb") as source, archive.open(name, "w") as target:
                        while True:
                            guard()
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            target.write(block)
            guard()
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return str(destination)


def inspect_backup(path, guard=None):
    guard = guard or (lambda: None)
    guard()
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        if (
            len(items) > len(FILES) + 1
            or len({v.filename for v in items}) != len(items)
            or sum(v.file_size for v in items) > MAX_TOTAL
        ):
            raise ValueError("Некорректный или слишком большой архив")
        if any(v.filename not in FILES | {"manifest.json"} for v in items):
            raise ValueError("Архив содержит посторонние пути")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Неизвестная версия копии")
        if (
            set(manifest["files"]) != {v.filename for v in items} - {"manifest.json"}
            or "config.json" not in manifest["files"]
        ):
            raise ValueError("Состав копии не соответствует манифесту")
        result = {}
        for name in manifest["files"]:
            guard()
            with archive.open(name) as stream:
                blocks = []
                while block := stream.read(1024 * 1024):
                    guard()
                    blocks.append(block)
                result[name] = b"".join(blocks)
        for name, raw in result.items():
            if hashlib.sha256(raw).hexdigest() != manifest["files"][name]:
                raise ValueError("Контрольная сумма не совпала: " + name)
        from configuration import DEFAULT_CONFIG, merge, validate

        config_data = json.loads(result["config.json"])
        if config_data.get("schema_version", 1) not in (1, 2):
            raise ValueError("Настройки копии созданы новой версией Крест")
        validate(merge(DEFAULT_CONFIG, config_data))
        if "warframe_state.json" in result:
            from warframe_store import validate_state

            validate_state(json.loads(result["warframe_state.json"]))
        return result


def rehearse_backup(path, guard=None, temporary_root=None):
    """Restore to disposable storage, then open/check every restored database."""
    guard = guard or (lambda: None)
    contents = inspect_backup(path, guard)
    with tempfile.TemporaryDirectory(prefix="krest-restore-check-", dir=temporary_root) as folder:
        root = Path(folder)
        for name, raw in contents.items():
            guard()
            restored = root / name  # inspect_backup only allows fixed basenames.
            with restored.open("xb") as target:
                for offset in range(0, len(raw), 1024 * 1024):
                    guard()
                    target.write(raw[offset : offset + 1024 * 1024])
            if hashlib.sha256(restored.read_bytes()).digest() != hashlib.sha256(raw).digest():
                raise ValueError("Ошибка пробного восстановления: " + name)
            if name.endswith(".sqlite3"):
                interruption = []

                def progress():
                    try:
                        guard()
                    except Exception as exc:
                        interruption.append(exc)
                        return 1
                    return 0

                with closing(sqlite3.connect(restored)) as db:
                    db.set_progress_handler(progress, 1000)
                    try:
                        result = db.execute("PRAGMA integrity_check").fetchall()
                    except sqlite3.OperationalError:
                        if interruption:
                            raise interruption[0]
                        raise
                    if result != [("ok",)]:
                        raise ValueError("Повреждена база в копии: " + name)
            elif name.endswith(".json"):
                if not isinstance(json.loads(restored.read_text(encoding="utf-8")), (dict, list)):
                    raise ValueError("Некорректный JSON в копии: " + name)
        guard()
    return f"Пробное восстановление пройдено: {len(contents)} файлов; JSON и базы открываются. Рабочие данные не изменялись."


def restore_backup(path, config):
    """Offline restoration with an exact local rollback; portable exports omit secrets."""
    from process_lock import ProcessLock
    from datetime import datetime
    import shutil

    contents = inspect_backup(path)
    root = config.filepath.parent
    locks = [ProcessLock(root / name) for name in (".kernel.lock", ".ui.lock")]
    acquired = []
    try:
        for lock in locks:
            if not lock.acquire():
                raise ValueError(
                    "Сначала закройте меню и завершите ядро: python main.py --stop-kernel"
                )
            acquired.append(lock)
        rollback = root / ("before-restore-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        rollback.mkdir(mode=0o700)
        targets = {
            name: config.filepath if name == "config.json" else root / name for name in contents
        }
        existed = set()
        for name, target in targets.items():
            if target.is_symlink():
                raise ValueError("Файл данных является ссылкой: " + name)
            if target.exists():
                existed.add(name)
                if name.endswith(".sqlite3"):
                    with (
                        closing(sqlite3.connect(target)) as original,
                        closing(sqlite3.connect(rollback / name)) as saved,
                    ):
                        original.backup(saved)
                else:
                    shutil.copy2(target, rollback / name)
                if os.name != "nt":
                    (rollback / name).chmod(0o600)
        staged = []
        changed = []
        try:
            for name, raw in contents.items():
                target = targets[name]
                temporary = target.with_name(target.name + ".restoring")
                staged.append((name, temporary, target))
                with temporary.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                if name.endswith(".sqlite3"):
                    with closing(sqlite3.connect(temporary)) as db:
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("Повреждена база: " + name)
                elif name.endswith(".json"):
                    if not isinstance(json.loads(raw), (dict, list)):
                        raise ValueError("Некорректный JSON: " + name)
            for name, temporary, target in staged:
                if name.endswith(".sqlite3"):
                    # Closing the live DB checkpointed it; stale sidecars must not
                    # replay pages from the previous database onto restored data.
                    for suffix in ("-wal", "-shm", "-journal"):
                        target.with_name(target.name + suffix).unlink(missing_ok=True)
                os.replace(temporary, target)
                changed.append(name)
                if os.name != "nt":
                    target.chmod(0o600)
        except Exception:
            for name in reversed(changed):
                if name in existed:
                    shutil.copy2(rollback / name, targets[name])
                else:
                    targets[name].unlink(missing_ok=True)
            raise
        finally:
            for _, temporary, _ in staged:
                temporary.unlink(missing_ok=True)
        return str(rollback)
    finally:
        for lock in acquired:
            lock.close()
