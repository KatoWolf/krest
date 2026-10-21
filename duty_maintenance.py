"""Cancellable AFK maintenance; no install or restore into a live profile."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from autonomy_files import DISK_RESERVE, MAX_BYTES, clean_path, digest_file, is_link, Paused


def verify_profile(duty):
    from backup_service import rehearse_backup

    folder = duty.root / "autonomy" / "profile"
    candidates = [p for p in folder.glob("*.zip") if p.is_file() and not is_link(p)]
    if not candidates:
        raise ValueError("Сначала создайте резервную копию настроек Крест")
    path = max(candidates, key=lambda p: p.stat().st_mtime)
    if shutil.disk_usage(folder).free < DISK_RESERVE + 64 * 1024**2:
        raise OSError("Не хватает места для пробного восстановления с резервом 2 ГБ")
    return rehearse_backup(path, duty.guard, folder) + " Копия: " + path.name


def verify_version(duty, job):
    version = duty.store.find_version(job["payload"]["version_id"])
    if not version:
        raise ValueError("Сохранённая версия больше не найдена")
    source = clean_path(version["destination"])
    duty.guard()
    if is_link(source) or not source.is_file():
        raise ValueError("Файл копии отсутствует или стал ссылкой")
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("Копия превышает предел проверки 20 ГБ")
    root = duty.root / "autonomy"
    root.mkdir(exist_ok=True)
    if shutil.disk_usage(root).free < DISK_RESERVE + source.stat().st_size:
        raise OSError("Не хватает места для пробного восстановления с резервом 2 ГБ")
    with tempfile.TemporaryDirectory(prefix="restore-check-", dir=root) as folder:
        target = Path(folder) / "restored"
        with source.open("rb") as reader, target.open("xb") as writer:
            while block := reader.read(1024 * 1024):
                duty.guard()
                writer.write(block)
        duty.guard()
        if digest_file(target, duty.guard) != version["hash"]:
            raise ValueError("Пробное восстановление не прошло: контрольная сумма копии изменилась")
    return "Версия восстановлена во временную папку и проверена по SHA-256: " + version["source"]


def _stop_download(process):
    """Only this owned winget download; never used for installers."""
    import psutil

    try:
        parent = psutil.Process(process.pid)
        owned = parent.children(recursive=True) + [parent]
        for child in owned:
            try:
                child.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(owned, timeout=2)
        for child in alive:
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()


def audit_versions(duty, job):
    """A daily bounded sample, rotating so older copies are also exercised."""
    rows = duty.store.versions()
    if not rows:
        return "Сохранённых версий файлов пока нет. Создайте поручение «Версии файлов»."
    offset = (int(time.time() // 86400) * 5) % len(rows)
    chosen = (rows[offset:] + rows[:offset])[:5]
    done = set(job["checkpoint"].get("verified", []))
    for row in chosen:
        duty.guard()
        if row["id"] not in done:
            duty.progress("Пробное восстановление: " + Path(row["source"]).name)
            verify_version(duty, {"payload": {"version_id": row["id"]}})
            done.add(row["id"])
            duty.store.update(job["id"], checkpoint={"verified": sorted(done)})
    return f"Пробное восстановление версий пройдено: {len(chosen)}. Хеши совпали; исходники не изменены."


def prepare_software(duty, job):
    executable = shutil.which("winget") if os.name == "nt" else None
    if not executable:
        raise ValueError("Подготовка обновлений требует winget в Windows")
    duty.guard()
    root = clean_path(str(duty.root / "autonomy" / "prepared" / job["id"]))
    root.mkdir(parents=True, exist_ok=True)
    if is_link(root):
        raise ValueError("Папка подготовки не должна быть ссылкой")
    if shutil.disk_usage(root).free < DISK_RESERVE + 1024**3:
        raise OSError("Для загрузки нужен запас 1 ГБ сверх резерва 2 ГБ")
    manifest = root / "prepared.json"
    if manifest.is_file():
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        files = saved.get("files", {})
        if saved.get("package") != job["payload"]["package"] or not files:
            raise ValueError("Некорректный отчёт подготовки установщика")
        for name, checksum in files.items():
            path = root / name
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or is_link(path)
                or not path.is_file()
                or digest_file(path, duty.guard) != checksum
            ):
                raise ValueError("Подготовленный установщик изменился; создайте новое поручение")
        return "Установщик подготовлен и проверен; установка не запускалась: " + str(root)
    log = root / "winget.log"
    previous_files = root / "files"
    if previous_files.exists():
        duty.guard()
        # Only this job's uncommitted download directory, never a live install.
        if is_link(previous_files):
            raise ValueError("Папка загрузки стала ссылкой")
        shutil.rmtree(previous_files)
    started = time.monotonic()
    with log.open("wb") as output:
        duty.guard()
        process = subprocess.Popen(
            [
                executable,
                "download",
                "--id",
                job["payload"]["package"],
                "--exact",
                "--source",
                "winget",
                "--download-directory",
                str(root / "files"),
                "--skip-dependencies",
                "--disable-interactivity",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lease = None
        try:
            from process_lease import DownloadLease

            lease = DownloadLease(process)
            while process.poll() is None:
                duty.guard()
                if time.monotonic() - started > 900:
                    raise OSError("Winget не закончил загрузку за 15 минут")
                if shutil.disk_usage(root).free < DISK_RESERVE:
                    raise OSError("Загрузка приостановлена: осталось меньше 2 ГБ")
                if duty.stop.wait(0.5):
                    raise Paused("Крест закрывается")
        except BaseException:
            _stop_download(process)
            raise
        finally:
            if lease is not None:
                lease.close()
    if process.returncode:
        raise ValueError(
            "Winget не подготовил установщик (код "
            + str(process.returncode)
            + "). Обновите «Установщик приложений» Microsoft. Журнал: "
            + str(log)
        )
    files = {}
    total = 0
    for path in (root / "files").rglob("*"):
        duty.guard()
        if is_link(path):
            raise ValueError("Ссылка внутри загруженного пакета")
        if path.is_file():
            total += path.stat().st_size
            if total > MAX_BYTES or len(files) >= 100:
                raise ValueError("Превышен размер подготовленного пакета")
            files[str(path.relative_to(root))] = digest_file(path, duty.guard)
    if not files:
        raise ValueError("Winget завершился без файлов установщика")
    from storage import atomic_write_json

    duty.guard()
    atomic_write_json(
        manifest, {"package": job["payload"]["package"], "files": files, "created": time.time()}
    )
    return (
        "Установщик скачан, проверен winget и сохранён с SHA-256. Установка не запускалась: "
        + str(root)
    )
