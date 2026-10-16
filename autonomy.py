"""One cooperative, durable worker for explicitly scoped AFK duties."""

import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

import psutil

from autonomy_activity import ActivityGate
from autonomy_files import FileJobs, Paused, clean_path, digest_file, MAX_BYTES, DISK_RESERVE
from autonomy_store import DutyStore

DEFAULT_AUTONOMY = {
    "paused": False,
    "enabled": True,
    "idle_minutes": 5,
    "learning": True,
    "min_free_memory_mb": 1024,
    "profile_backup": True,
    "health_checks": True,
    "telegram_unpack": False,
}
KINDS = {
    "backup": "Версии файлов",
    "copy": "Копирование",
    "offload": "Освобождение места",
    "archive": "Создание ZIP",
    "unpack": "Распаковка ZIP",
    "download": "Загрузка файла",
    "software": "Обновление программы",
    "health": "Проверка подключений",
    "profile_backup": "Копия настроек Крест",
    "restore": "Восстановление версии",
    "telegram": "Файл с телефона",
}


def validate_settings(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("Некорректные настройки дежурства")
    for key in (
        "enabled",
        "paused",
        "learning",
        "profile_backup",
        "health_checks",
        "telegram_unpack",
    ):
        if type(cfg.get(key)) is not bool:
            raise ValueError("Некорректный переключатель дежурства: " + key)
    if type(cfg.get("idle_minutes")) is not int or not 1 <= cfg["idle_minutes"] <= 120:
        raise ValueError("Простой до дежурства: 1–120 минут")
    if (
        type(cfg.get("min_free_memory_mb")) is not int
        or not 512 <= cfg["min_free_memory_mb"] <= 16384
    ):
        raise ValueError("Резерв памяти: 512–16384 МБ")


def validate_task(kind, values):
    if kind not in KINDS or not isinstance(values, dict):
        raise ValueError("Неизвестный вид задачи")
    payload = {}
    if kind in {"backup", "copy", "archive", "offload", "unpack"}:
        source, destination = (
            clean_path(str(values.get("source", ""))),
            clean_path(str(values.get("destination", ""))),
        )
        if source == Path(source.anchor) or destination == Path(destination.anchor):
            raise ValueError("Выберите отдельную папку, а не весь диск")
        if (
            source == destination
            or destination.is_relative_to(source)
            or source.is_relative_to(destination)
        ):
            raise ValueError("Папки источника и назначения должны быть раздельными")
        if kind == "offload":
            from configuration import CONFIG_PATH

            for root in (Path(__file__).resolve().parent, Path(CONFIG_PATH).parent):
                if source.is_relative_to(root) or root.is_relative_to(source):
                    raise ValueError("Папку Крест и её данных нельзя перемещать дежурством")
            for env in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "APPDATA", "LOCALAPPDATA"):
                root = os.getenv(env)
                if root and source.is_relative_to(Path(root)):
                    raise ValueError(
                        "Системные папки и папки установленных программ не перемещаются"
                    )
            for key, default, low, high in [("min_free_gb", 30, 1, 1000), ("age_days", 7, 1, 3650)]:
                value = values.get(key, default)
                if type(value) is not int or not low <= value <= high:
                    raise ValueError("Некорректный порог освобождения места")
                payload[key] = value
        payload.update(source=str(source), destination=str(destination))
    elif kind == "download":
        from urllib.parse import urlsplit

        url = str(values.get("url", ""))
        parsed = urlsplit(url)
        if (
            len(url) > 4000
            or parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Нужна ссылка HTTP(S) без логина и пароля")
        destination = clean_path(str(values.get("destination", "")))
        payload.update(url=url, destination=str(destination))
    elif kind == "software":
        package = str(values.get("package", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,149}", package):
            raise ValueError("Укажите точный ID пакета winget, например 7zip.7zip")
        payload["package"] = package
    elif kind == "restore":
        ident = str(values.get("version_id", ""))
        if not re.fullmatch("[a-f0-9]{32}", ident):
            raise ValueError("Выберите сохранённую версию")
        payload["version_id"] = ident
    elif kind == "telegram":
        file_id, filename = str(values.get("file_id", "")), str(values.get("filename", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{5,500}", file_id):
            raise ValueError("Некорректный файл Telegram")
        filename = Path(filename.replace("\\", "/")).name.rstrip(" .")
        if not filename or filename in {".", ".."} or ":" in filename or len(filename) > 180:
            raise ValueError("Некорректное имя файла")
        payload.update(file_id=file_id, filename=filename)
    return payload


class AutonomousDuty:
    def __init__(self, runtime, root, gate=None):
        self.runtime, self.root = runtime, Path(root)
        self.store = DutyStore(self.root / "autonomy.sqlite3")
        self.gate = gate or ActivityGate(runtime)
        self.stop = threading.Event()
        self.thread = None
        self.current = None
        self.paused = runtime.settings().get("autonomy", {}).get("paused", False)
        self.atomic = False
        self._cancel = set()
        self._snapshot_lock = threading.RLock()
        self._stamp = 0
        self._cached = {}
        self._progress_at = 0
        self._last_schedule = 0
        self._default_due = {}
        self._last_error = 0
        self.files = FileJobs(self.store, self.guard, self.progress)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.gate.start()
        self.thread = threading.Thread(target=self._run, daemon=True, name="krest-duty")
        self.thread.start()

    def guard(self):
        if self.stop.is_set() or not self.permitted():
            raise Paused("Продолжу при свободном компьютере")
        if self.current and self.current["id"] in self._cancel:
            raise Paused("Задача отменена")

    def permitted(self):
        return not self.stop.is_set() and not self.paused and self.gate.permitted()

    def progress(self, text):
        if self.current and time.monotonic() - self._progress_at >= 2:
            self._progress_at = time.monotonic()
            self.store.update(self.current["id"], result=str(text)[:500])
            self._stamp = 0

    def snapshot(self):
        with self._snapshot_lock:
            if time.monotonic() - self._stamp >= 2:
                self._cached = {
                    "jobs": self.store.jobs(),
                    "rules": self.store.rules(),
                    "versions": self.store.versions(),
                    "habits": self.store.habits(),
                }
                self._stamp = time.monotonic()
            return {
                **self._cached,
                "activity": self.gate.snapshot(),
                "paused": self.paused,
                "atomic": self.atomic,
                "current": self.current["id"] if self.current else "",
            }

    def report(self):
        data = self.snapshot()
        status = "Приостановлено вами" if self.paused else data["activity"]["reason"]
        rows = data["jobs"][:6]
        return (
            "Дежурство: "
            + status
            + "\n"
            + "\n".join(
                f"{r['label']}: {STATUS_NAMES.get(r['status'], r['status'])}. {r['result']}"
                for r in rows
            )
            if rows
            else "Дежурство: " + status + ". Очередь пуста."
        )

    def call(self, action, **values):
        if action == "snapshot":
            return self.snapshot()
        if action == "report":
            return self.report()
        if action == "software.list":
            return installed_software()
        if action in {"pause", "resume"}:
            self.paused = action == "pause"
            with self.runtime.config_manager._lock:
                cfg = self.runtime.config_manager.snapshot()
                cfg["autonomy"]["paused"] = self.paused
                self.runtime.config_manager.save_config(cfg)
            if not self.paused:
                self.gate.touch()
            return (
                "Дежурство приостановлено."
                if self.paused
                else "Дежурство продолжится только при подтверждённом AFK."
            )
        if action == "settings":
            options = values.get("options", {})
            if set(options) - set(DEFAULT_AUTONOMY):
                raise ValueError("Неизвестные настройки")
            with self.runtime.config_manager._lock:
                cfg = self.runtime.config_manager.snapshot()
                cfg["autonomy"].update(options)
                validate_settings(cfg["autonomy"])
                self.runtime.config_manager.save_config(cfg)
            self.gate.touch()
            return "Настройки дежурства сохранены."
        if action in {"enqueue", "rule.add"}:
            kind = values.get("kind", "")
            if kind == "telegram":
                raise ValueError("Приём файлов доступен через авторизованный Telegram-пульт")
            payload = validate_task(kind, values.get("values", {}))
            if kind == "offload" and (
                self.root.is_relative_to(payload["source"])
                or Path(payload["source"]).is_relative_to(self.root)
            ):
                raise ValueError("Папку данных Крест нельзя перемещать")
            label = str(values.get("label") or KINDS[kind])[:160]
            if action == "enqueue":
                ident = self.store.enqueue(kind, label, payload)
                text = "Задача поставлена в очередь AFK: "
            else:
                if kind in {"restore", "telegram"}:
                    raise ValueError("Это действие не повторяется автоматически")
                interval = values.get("interval", 86400)
                if type(interval) is not int or not 300 <= interval <= 30 * 86400:
                    raise ValueError("Период правила: от 5 минут до 30 дней")
                ident = self.store.add_rule(kind, label, payload, interval)
                text = "Правило будет выполняться только во время AFK: "
            self._stamp = 0
            return {"id": ident, "text": text + label}
        if action == "rule.run":
            rule = next((r for r in self.store.rules() if r["id"] == values.get("id")), None)
            if rule is None:
                raise ValueError("Правило не найдено")
            ident = self.store.enqueue(
                rule["kind"], rule["label"], rule["payload"], rule_id=rule["id"]
            )
            self._stamp = 0
            return {"id": ident, "text": "Правило поставлено в очередь AFK: " + rule["label"]}
        if action == "rule.delete":
            ident = str(values["id"])
            self.call("rule.enable", id=ident, enabled=False)
            self.store.delete_rule(ident)
            self._stamp = 0
            return "Правило удалено. Результаты и версии файлов сохранены."
        if action == "rule.enable":
            ident, enabled = str(values["id"]), bool(values["enabled"])
            self.store.rule_enable(ident, enabled)
            if not enabled and self.current and self.current["rule_id"] == ident:
                self._cancel.add(self.current["id"])
            self._stamp = 0
            return "Правило включено." if enabled else "Правило выключено."
        if action in {"cancel", "retry", "priority"}:
            job = self.store.get(str(values["id"]))
            if job is None:
                raise ValueError("Задача не найдена")
            ident = job["id"]
            if action == "cancel":
                if job["status"] == "completed":
                    raise ValueError(
                        "Задача уже завершена. Для возврата файла используйте «Версии файлов»."
                    )
                if self.atomic and self.current and self.current["id"] == ident:
                    raise ValueError(
                        "Начатая установка должна завершиться. Новые задачи можно приостановить."
                    )
                self._cancel.add(ident)
                if job["status"] != "running":
                    self.store.update(ident, status="cancelled", result="Отменено вами")
                self.store.observe(job, success=False)
            elif action == "retry":
                if job["status"] == "running":
                    raise ValueError("Задача уже выполняется")
                if job["status"] == "completed":
                    return self.call(
                        "enqueue", kind=job["kind"], values=job["payload"], label=job["label"]
                    )
                self._cancel.discard(ident)
                self.store.update(ident, status="waiting", due=time.time(), attempts=0)
            else:
                self.store.update(ident, priority=10)
            self._stamp = 0
            return "Очередь обновлена."
        raise ValueError("Неизвестное действие дежурства")

    def receive_telegram(self, file_id, filename, sender, message_id):
        cfg = self.runtime.settings()
        if not cfg.get("remote_control", {}).get("allow_uploads"):
            raise ValueError("Приём файлов выключен")
        if str(sender) != str(cfg.get("api_keys", {}).get("telegram_owner_id", "")):
            raise ValueError("Файл может передать только владелец пульта")
        from licensing import require_premium

        require_premium(cfg)
        payload = validate_task("telegram", {"file_id": file_id, "filename": filename})
        ident = self.store.enqueue(
            "telegram",
            "С телефона: " + payload["filename"],
            payload,
            rule_id="telegram",
            unique="tg-" + str(sender) + "-" + str(int(message_id)),
        )
        self._stamp = 0
        return ident

    def _schedule(self):
        now = time.time()
        self.store.schedule()
        cfg = self.runtime.settings().get("autonomy", DEFAULT_AUTONOMY)
        for kind, key, period in [
            ("profile_backup", "profile_backup", 86400),
            ("health", "health_checks", 1800),
        ]:
            if cfg.get(key):
                # Stable time buckets deduplicate across restarts and AFK sessions.
                ident = "auto-" + kind + "-" + str(int(now // period))
                pending = any(
                    r["kind"] == kind and r["status"] in {"waiting", "running", "retry"}
                    for r in self.store.jobs()
                )
                if self.store.get(ident) is None and not pending:
                    self.store.enqueue(
                        kind, KINDS[kind], {}, rule_id="builtin", unique=ident, priority=-1
                    )

    def run_once(self):
        if not self.permitted():
            return False
        for interrupted in self.store.interrupted_installers():
            checkpoint = interrupted["checkpoint"]
            try:
                process = psutil.Process(checkpoint.get("pid", -1))
                live = abs(process.create_time() - checkpoint.get("created", 0)) < 1
            except (psutil.Error, ValueError):
                live = False
            if live:
                self.atomic = True
                return False
            checkpoint["installer_finished"] = True
            self.store.update(
                interrupted["id"],
                status="failed",
                checkpoint=checkpoint,
                result="Крест перезапускалась во время установки. Проверьте версию программы; автоматический повтор запрещён.",
            )
        self.atomic = False
        if time.monotonic() - self._last_schedule > 30:
            self._schedule()
            self._last_schedule = time.monotonic()
        job = self.store.next()
        if not job:
            return False
        self.current = job
        self._progress_at = 0
        self.store.update(job["id"], status="running", result="Выполняется во время AFK")
        self._stamp = 0
        try:
            self.guard()
            result = self.execute(job)
            self.store.update(job["id"], status="completed", result=result[:2000])
        except Paused as exc:
            cancelled = job["id"] in self._cancel
            self.store.update(
                job["id"], status="cancelled" if cancelled else "waiting", result=str(exc)
            )
        except (ValueError, FileExistsError, zip_error()) as exc:
            self.store.update(job["id"], status="failed", result=str(exc)[:1000])
        except Exception as exc:
            attempts = job["attempts"] + 1
            self.store.update(
                job["id"],
                status="retry" if attempts < 4 else "failed",
                attempts=attempts,
                due=time.time() + min(3600, 60 * 2**attempts),
                result=str(exc)[:1000],
            )
        finally:
            self.current = None
            self.atomic = False
            self._stamp = 0
        if self.store.get(job["id"])["status"] == "completed":
            # Reporting must never turn a successful side effect into a retry.
            try:
                self.store.observe(
                    job, learn=self.runtime.settings().get("autonomy", {}).get("learning", True)
                )
                self.runtime.publish_notice(result, "Дежурство", "low", speak=False)
            except Exception:
                pass
        return True

    def execute(self, job):
        kind = job["kind"]
        if kind in {"backup", "copy", "offload"}:
            if kind == "offload":
                source, target = Path(job["payload"]["source"]), Path(job["payload"]["destination"])
                if not target.is_dir():
                    raise FileNotFoundError("Подключите диск назначения и создайте выбранную папку")
                if source.stat().st_dev == target.stat().st_dev:
                    raise ValueError("Для освобождения места нужен другой диск")
            return self.files.folder(job)
        if kind in {"archive", "unpack", "restore"}:
            return getattr(self.files, kind)(job)
        if kind in {"download", "telegram"}:
            return self._download(job)
        if kind == "software":
            return self._software(job)
        if kind == "profile_backup":
            from backup_service import create_backup, inspect_backup

            destination = self.root / "autonomy" / "profile"
            destination.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(destination).free < DISK_RESERVE + 64 * 1024**2:
                raise OSError("Недостаточно места для копии настроек с резервом 2 ГБ")
            path = destination / (job["id"] + ".zip")
            create_backup(self.runtime.config_manager, path, guard=self.guard)
            self.guard()
            inspect_backup(path)
            for old in sorted(
                destination.glob("auto-profile_backup-*.zip"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[7:]:
                self.guard()
                if not old.is_symlink():
                    old.unlink()
            return "Копия настроек проверена: " + str(path)
        if kind == "health":
            details = []
            if hasattr(self.runtime, "integrations"):
                self.guard()
                self.runtime.integrations.refresh()
                for _, (filename, _) in self.runtime.integrations.MODULES.items():
                    self.guard()
                    data = self.runtime.integrations.details(filename)
                    if data.get("error"):
                        details.append(filename + ": " + str(data["error"])[:180])
            thread = self.runtime._voice_thread
            if (
                self.runtime.settings().get("app_settings", {}).get("voice_auto_start")
                and not self.runtime._voice_stop.is_set()
                and thread
                and not thread.is_alive()
            ):
                self.guard()
                self.runtime.start_voice()
                details.append(
                    "Поток распознавания перезапущен; состояние видно на странице голоса"
                )
            return (
                "\n".join(details)
                or "Проверка подключений завершена. Ошибок управляемых процессов не обнаружено."
            )
        raise ValueError("Неизвестная задача")

    def _download(self, job):
        import requests
        from urllib.parse import urlsplit, unquote

        payload, checkpoint = job["payload"], job["checkpoint"]
        if job["kind"] == "telegram":
            cfg = self.runtime.settings()
            if not cfg.get("remote_control", {}).get("allow_uploads"):
                raise ValueError("Приём файлов выключен")
            token = cfg.get("api_keys", {}).get("krest_bot_token", "")
            self.guard()
            try:
                response = requests.get(
                    "https://api.telegram.org/bot" + token + "/getFile",
                    params={"file_id": payload["file_id"]},
                    timeout=(5, 10),
                )
                response.raise_for_status()
                result = response.json()["result"]
                if result.get("file_size", 0) > 20 * 1024**2:
                    raise ValueError("Файл Telegram превышает 20 МБ")
                url = "https://api.telegram.org/file/bot" + token + "/" + result["file_path"]
            except requests.RequestException:
                raise OSError("Telegram пока недоступен; повторю позже") from None
            root, name = self.root / "uploads", payload["filename"]
        else:
            url = payload["url"]
            root = clean_path(payload["destination"])
            name = Path(unquote(urlsplit(url).path).replace("\\", "/")).name or "download.bin"
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .")[:150] or "file.bin"
        if name.split(".")[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *("COM" + str(i) for i in range(1, 10)),
            *("LPT" + str(i) for i in range(1, 10)),
        }:
            name = "file-" + name
        self.guard()
        root.mkdir(parents=True, exist_ok=True)
        target = clean_path(root / (Path(name).stem + "-" + job["id"][-8:] + Path(name).suffix))
        temporary = clean_path(target.with_name("." + target.name + ".partial"))
        if target.exists():
            if not checkpoint.get("hash") or digest_file(target, self.guard) != checkpoint["hash"]:
                raise FileExistsError("Файл назначения уже существует или был изменён")
            return self._received(job, target)
        # Range resume is used only with a server validator; otherwise restart.
        offset = temporary.stat().st_size if temporary.exists() else 0
        headers = {"Accept-Encoding": "identity"}
        if offset and checkpoint.get("validator"):
            headers.update(Range=f"bytes={offset}-", **{"If-Range": checkpoint["validator"]})
        else:
            offset = 0
        self.guard()
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(5, 5)) as response:
                self.guard()
                if response.status_code == 416 and offset:
                    # May have crashed after the last byte, before verification.
                    # A fresh transfer is safer than publishing an unverified tail.
                    temporary.unlink(missing_ok=True)
                    checkpoint.pop("validator", None)
                    self.store.update(job["id"], checkpoint=checkpoint)
                    raise OSError("Сервер не принимает докачку; повторю загрузку целиком")
                response.raise_for_status()
                expected_total = 0
                validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
                if response.status_code == 206:
                    span = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                    )
                    if (
                        not offset
                        or not span
                        or int(span[1]) != offset
                        or not offset <= int(span[2]) < int(span[3])
                    ):
                        raise ValueError("Сервер вернул некорректный диапазон файла")
                    expected_total = int(span[3])
                    if validator and validator != checkpoint.get("validator"):
                        temporary.unlink(missing_ok=True)
                        checkpoint.pop("validator", None)
                        self.store.update(job["id"], checkpoint=checkpoint)
                        raise OSError("Файл на сервере изменился; повторю загрузку целиком")
                else:
                    offset = 0
                length = int(response.headers.get("Content-Length", "0"))
                expected_total = expected_total or (offset + length if length else 0)
                limit = 20 * 1024**2 if job["kind"] == "telegram" else MAX_BYTES
                if max(offset + length, expected_total) > limit:
                    raise ValueError("Размер превышает предел загрузки")
                if length and shutil.disk_usage(root).free < length + DISK_RESERVE:
                    raise OSError("Недостаточно места для загрузки")
                with temporary.open("ab" if offset else "wb") as output:
                    checkpoint["validator"] = validator
                    self.store.update(job["id"], checkpoint=checkpoint)
                    for chunk in response.iter_content(64 * 1024):
                        self.guard()
                        offset += len(chunk)
                        if offset > limit:
                            raise ValueError("Размер превышает предел загрузки")
                        if (
                            offset % (1024 * 1024) < len(chunk)
                            and shutil.disk_usage(root).free < DISK_RESERVE + 1024 * 1024
                        ):
                            raise OSError("Оставляю резерв 2 ГБ на диске назначения")
                        output.write(chunk)
                        self.progress(f"Получено {offset // 1024**2} МБ")
                    output.flush()
                    os.fsync(output.fileno())
                if expected_total and offset != expected_total:
                    raise OSError("Сервер передал только часть файла; продолжу докачку позже")
        except requests.RequestException:
            raise OSError(
                "Загрузка прервана сетью; сохранённые данные будут проверены при повторе"
            ) from None
        checkpoint["hash"] = digest_file(temporary, self.guard)
        self.store.update(job["id"], checkpoint=checkpoint)
        self.guard()
        if target.exists():
            raise FileExistsError("Файл назначения уже существует")
        os.rename(temporary, target)
        checkpoint["complete"] = True
        self.store.update(job["id"], checkpoint=checkpoint)
        return self._received(job, target)

    def _received(self, job, target):
        if (
            job["kind"] == "telegram"
            and self.runtime.settings()["autonomy"].get("telegram_unpack")
            and target.suffix.lower() == ".zip"
        ):
            self.store.enqueue(
                "unpack",
                "Распаковка: " + target.name,
                {"source": str(target), "destination": str(self.root / "received")},
                rule_id="telegram",
                unique=job["id"] + "-unpack",
            )
        return "Файл получен: " + str(target)

    def _software(self, job):
        if os.name != "nt":
            raise ValueError("Обновление программ доступно в Windows через winget")
        executable = shutil.which("winget")
        if not executable:
            raise ValueError("Не найден winget. Установите «Установщик приложений» Microsoft")
        checkpoint = job["checkpoint"]
        if checkpoint.get("installer_started"):
            # Do not repeat a potentially completed external side effect after a
            # crash. The user can explicitly queue a new upgrade after reviewing.
            pid = checkpoint.get("pid")
            try:
                process = psutil.Process(pid)
                live = abs(process.create_time() - checkpoint.get("created", 0)) < 1
            except psutil.Error:
                live = False
            if live:
                raise OSError("Предыдущий установщик ещё работает")
            raise ValueError(
                "Установка была запущена до перезапуска Крест. Проверьте установленную версию; повтор автоматически не запускается."
            )
        self.guard()
        package = job["payload"]["package"]
        log = self.root / "autonomy" / "installers" / (job["id"] + ".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        checkpoint["installer_started"] = True
        self.store.update(job["id"], checkpoint=checkpoint)
        self.atomic = True
        with log.open("ab") as output:
            try:
                self.guard()
            except Paused:
                checkpoint["installer_started"] = False
                self.store.update(job["id"], checkpoint=checkpoint)
                raise
            try:
                process = subprocess.Popen(
                    [
                        executable,
                        "upgrade",
                        "--id",
                        package,
                        "--exact",
                        "--source",
                        "winget",
                        "--silent",
                        "--disable-interactivity",
                        "--accept-source-agreements",
                        "--accept-package-agreements",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                checkpoint["installer_started"] = False
                self.store.update(job["id"], checkpoint=checkpoint)
                raise
            try:
                created = psutil.Process(process.pid).create_time()
            except psutil.NoSuchProcess:
                created = 0
            checkpoint.update(pid=process.pid, created=created)
            self.store.update(job["id"], checkpoint=checkpoint)
            while process.poll() is None:
                self.progress(
                    "Установщик работает. Начатая установка завершится и после возвращения к ПК."
                )
                if self.stop.wait(0.5):
                    raise Paused("Крест закрыта; установщик продолжает работу")
        code = process.returncode & 0xFFFFFFFF
        checkpoint["installer_finished"] = True
        self.store.update(job["id"], checkpoint=checkpoint)
        # APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE: already up to date.
        if code == 0x8A15002B:
            return package + ": доступных обновлений нет."
        if code:
            raise ValueError(f"Установщик {package} завершился с кодом {code:#x}. Журнал: {log}")
        return package + ": winget подтвердил завершение обновления. Журнал: " + str(log)

    def _run(self):
        if os.name == "nt":
            import ctypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentThread.restype = ctypes.c_void_p
            kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            kernel.SetThreadPriority(kernel.GetCurrentThread(), -1)
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                if time.monotonic() - self._last_error > 60:
                    self._last_error = time.monotonic()
                    self.runtime.events.put(("error", "Дежурство: " + str(exc)[:200]))
            self.stop.wait(2)

    def close(self):
        self.stop.set()
        self.gate.close()
        if self.thread:
            self.thread.join(timeout=2)


STATUS_NAMES = {
    "waiting": "ожидает AFK",
    "running": "выполняется",
    "retry": "ожидает повторения",
    "completed": "готово",
    "failed": "нужно внимание",
    "cancelled": "отменено",
}


def zip_error():
    import zipfile

    return zipfile.BadZipFile


def installed_software():
    """Read winget's machine-readable installed package export, never upgrade all."""
    import json
    import tempfile

    executable = shutil.which("winget") if os.name == "nt" else None
    if not executable:
        raise ValueError("Список доступен в Windows с установленным winget")
    with tempfile.TemporaryDirectory(prefix="krest-packages-") as folder:
        path = Path(folder) / "installed.json"
        result = subprocess.run(
            [
                executable,
                "export",
                "--output",
                str(path),
                "--source",
                "winget",
                "--accept-source-agreements",
                "--disable-interactivity",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not path.is_file():
            raise ValueError(
                "Winget не вернул список. Проверьте «Установщик приложений» Microsoft."
            )
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return sorted(
            {
                row["PackageIdentifier"]
                for source in data.get("Sources", [])
                for row in source.get("Packages", [])
                if re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{1,149}", str(row.get("PackageIdentifier", ""))
                )
            }
        )
