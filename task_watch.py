"""Bounded observations of selected operations; a closed process is not success."""

import os
from pathlib import Path
import re
import queue
import subprocess
import threading
import time

from autonomy_files import clean_path, fingerprint, is_link

ACTIVE = {"watching", "shutdown_wait"}
STATES = {
    "watching": "Наблюдаю",
    "ready": "Файл стабилен",
    "completed": "Завершено",
    "unknown": "Результат неизвестен",
    "failed": "Ошибка",
    "cancelled": "Отменено",
    "shutdown_wait": "Ожидаю AFK для выключения",
    "shutdown_sent": "Выключение запланировано",
    "review": "Нужно проверить",
}


def file_available(path):
    """On Windows, require the writer to release its handle before signalling readiness."""
    if os.name != "nt":
        return True
    import ctypes
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
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == w.HANDLE(-1).value:
        return False
    kernel.CloseHandle(handle)
    return True


def steam_progress(path):
    path = clean_path(path)
    if not path.is_file():
        return {"state": "watching", "text": "Ожидаю манифест Steam"}
    if path.stat().st_size > 512 * 1024:
        raise ValueError("Манифест Steam слишком большой")
    values = dict(
        re.findall(r'"(\w+)"\s+"([^"\n]*)"', path.read_text(encoding="utf-8", errors="replace"))
    )
    if not values.get("appid", "").isdigit() or "StateFlags" not in values:
        raise ValueError("Steam ещё не записал корректный манифест")
    flags = int(values["StateFlags"])
    total = int(values.get("BytesToDownload", "0"))
    done = int(values.get("BytesDownloaded", "0"))
    stage_total = int(values.get("BytesToStage", "0"))
    staged = int(values.get("BytesStaged", "0"))
    # FullyInstalled and optional AppRunning only. A paused update must not
    # look finished just because downloaded bytes stopped changing.
    ready = bool(flags & 4) and not flags & ~(4 | 64) and done >= total and staged >= stage_total
    progress = (
        f"{min(100, int(done * 100 / total))}% загружено"
        if total > 0
        else "Ожидаю состояние установки Steam"
    )
    return {
        "state": "completed" if ready else "watching",
        "text": "Steam подтвердил установку"
        if ready
        else ("Загрузка Steam на паузе" if flags & 512 else progress),
        "progress": done,
    }


class TaskWatch:
    def __init__(self, runtime, store):
        self.runtime, self.store = runtime, store
        self.lock = threading.RLock()
        self.observed = {}
        self.last_tick = 0
        self.closed = threading.Event()
        self.deliveries = queue.Queue(maxsize=32)
        self.delivery_thread = None
        self.delivery_lock = threading.Lock()
        # Restart must never arm an old power action silently.
        for row in self.rows():
            if row.get("delivery") == "Ожидает отправки в Telegram":
                row["delivery"] = (
                    "Крест перезапускалась; доставка не подтверждена. Автоматического повтора нет."
                )
                self.store.put("watches", row)
            if row["status"] in ACTIVE and row.get("shutdown"):
                row.update(
                    shutdown=False, detail="Крест перезапускалась: автоматическое выключение снято"
                )
                if row["status"] == "shutdown_wait":
                    row["status"] = "completed"
                self.store.put("watches", row)

    def rows(self):
        return self.store.rows("watches", 120)

    def add(self, kind, target, label="", telegram=False, shutdown=False):
        with self.lock:
            if sum(r["status"] in ACTIVE for r in self.rows()) >= 20:
                raise ValueError("Одновременно можно наблюдать до 20 задач")
            if kind not in {"file", "folder", "steam", "duty", "process"}:
                raise ValueError("Неизвестный вид наблюдения")
            if any(type(v) is not bool for v in (telegram, shutdown)):
                raise ValueError("Некорректные настройки наблюдения")
            if shutdown and kind not in {"steam", "duty"}:
                raise ValueError(
                    "Выключение доступно после подтверждённой установки Steam или задачи Крест"
                )
            known = {}
            if kind in {"file", "folder", "steam"}:
                target = str(clean_path(target))
                if kind == "folder":
                    known = self._folder_files(target)
                if kind == "steam" and not re.fullmatch(r"appmanifest_\d+\.acf", Path(target).name):
                    raise ValueError("Выберите манифест игры Steam")
            elif kind == "duty":
                job = self.runtime.autonomy.store.get(str(target))
                if job is None:
                    raise ValueError("Задача дежурства не найдена")
                label = label or job["label"]
            else:
                import psutil

                process = psutil.Process(int(target))
                target = {
                    "pid": process.pid,
                    "created": process.create_time(),
                    "name": process.name(),
                }
            if telegram:
                self._telegram_config()
            if any(
                r["kind"] == kind and r["target"] == target and r["status"] in ACTIVE
                for r in self.rows()
            ):
                raise ValueError("Эта задача уже наблюдается")
            row = self.store.put(
                "watches",
                {
                    "kind": kind,
                    "target": target,
                    "label": (str(label).strip() or str(target))[:180],
                    "telegram": telegram,
                    "shutdown": shutdown,
                    "status": "watching",
                    "detail": "Наблюдение включено",
                    "created": time.time(),
                    **({"known": known} if kind == "folder" else {}),
                },
                limit=100,
            )
            return {
                "id": row["id"],
                "text": "Наблюдаю: "
                + row["label"]
                + (
                    ". После успешного завершения дождусь AFK и запланирую выключение с минутой на отмену."
                    if shutdown
                    else ""
                ),
            }

    def steam(self, name, **options):
        from steam_service import local_games
        from speech_tools import normalize_command

        key = normalize_command(name)
        cfg = self.runtime.settings()
        alias = normalize_command(cfg.get("application_aliases", {}).get(key, key))
        rows = local_games()
        from name_vocabulary import NameVocabulary

        vocabulary = NameVocabulary()
        vocabulary.update([], rows)
        resolved = vocabulary.resolve("запусти игру " + key)
        if resolved.startswith("запусти игру "):
            alias = (
                normalize_command(resolved[len("запусти игру ") :])
                if resolved != "запусти игру " + key
                else alias
            )
        matched = [
            r for r in rows if key == r["appid"] or normalize_command(r["name"]) in {key, alias}
        ]
        if not matched:
            matched = [r for r in rows if key in normalize_command(r["name"])]
        if len(matched) != 1:
            raise ValueError(
                "Назовите игру точнее или выберите её манифест в меню. Найдено: "
                + str(len(matched))
            )
        row = matched[0]
        return self.add(
            "steam",
            str(Path(row["library"]) / "steamapps" / ("appmanifest_" + row["appid"] + ".acf")),
            row["name"],
            **options,
        )

    def last_duty(self, kind=None, **options):
        jobs = [
            r
            for r in self.runtime.autonomy.store.jobs()
            if r["status"] in {"waiting", "running", "retry"} and (not kind or r["kind"] == kind)
        ]
        if len(jobs) != 1:
            raise ValueError(
                "Выберите конкретную задачу дежурства в меню «Наблюдение»: активных задач "
                + str(len(jobs))
            )
        return self.add("duty", jobs[0]["id"], **options)

    def cancel(self, ident=None):
        with self.lock:
            rows = [
                r
                for r in self.rows()
                if (r["id"] == ident if ident else r["status"] in ACTIVE | {"shutdown_sent"})
            ]
            for row in rows:
                if row["status"] == "shutdown_sent":
                    self._abort_shutdown()
                row.update(status="cancelled", shutdown=False, detail="Отменено вами")
                self.store.put("watches", row)
            return "Наблюдение отменено: " + str(len(rows))

    def cancel_power(self):
        count = 0
        with self.lock:
            for row in self.rows():
                if row.get("shutdown") and row["status"] in ACTIVE | {"shutdown_sent"}:
                    row.update(
                        shutdown=False, status="cancelled", detail="Отложенное выключение отменено"
                    )
                    self.store.put("watches", row)
                    count += 1
        return "Отложенное выключение после задач отменено" if count else ""

    @staticmethod
    def _abort_shutdown():
        if os.name == "nt":
            subprocess.run(
                ["shutdown.exe", "/a"],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def _telegram_config(self):
        from licensing import require_premium

        cfg = self.runtime.settings()
        require_premium(cfg)
        keys = cfg["api_keys"]
        token, owner = keys.get("krest_bot_token", ""), keys.get("telegram_owner_id", "")
        if not token or not str(owner).isdigit():
            raise ValueError("Укажите токен Telegram-пульта и ID владельца в подключениях")
        return token, str(owner)

    def _notify(self, row):
        text = row["label"] + ": " + row["detail"]
        self.runtime.publish_notice(text, "Наблюдение", "normal", speak=False)
        if row["telegram"] and not self.closed.is_set():
            row["delivery"] = "Ожидает отправки в Telegram"
            try:
                self.deliveries.put_nowait((row["id"], text[:3000]))
            except queue.Full:
                row["delivery"] = "Очередь Telegram заполнена; результат сохранён в меню"
                return
            with self.delivery_lock:
                if self.delivery_thread is None or not self.delivery_thread.is_alive():
                    self.delivery_thread = threading.Thread(
                        target=self._send_pending, daemon=True, name="krest-watch-delivery"
                    )
                    self.delivery_thread.start()

    def _send_pending(self):
        # Exists only while a user-requested Telegram delivery is pending.
        # Slow networking cannot hold the watch/cancel lock or delay audio.
        while not self.closed.is_set():
            with self.delivery_lock:
                try:
                    ident, text = self.deliveries.get_nowait()
                except queue.Empty:
                    self.delivery_thread = None
                    return
            if self.closed.is_set():
                return
            try:
                import requests

                token, owner = self._telegram_config()
                response = requests.post(
                    "https://api.telegram.org/bot" + token + "/sendMessage",
                    json={"chat_id": owner, "text": text},
                    timeout=(3, 5),
                )
                if not response.ok or not response.json().get("ok"):
                    raise ValueError("Telegram не подтвердил доставку")
                delivery = "Отправлено владельцу в Telegram"
            except Exception:
                delivery = "Telegram не подтвердил доставку; результат сохранён здесь. Повторной отправки нет."
            with self.lock:
                row = self.store.get("watches", ident)
                if row:
                    row["delivery"] = delivery
                    self.store.put("watches", row)

    def close(self):
        self.closed.set()

    def probe(self, row, now):
        kind, target = row["kind"], row["target"]
        if kind == "folder":
            return self._probe_folder(row, now)
        if kind == "duty":
            job = self.runtime.autonomy.store.get(target)
            if not job:
                return "unknown", "Задача больше не найдена"
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job["status"], job["result"] or job["status"]
            return "watching", job["result"] or "Задача в очереди AFK"
        if kind == "process":
            import psutil

            try:
                process = psutil.Process(target["pid"])
                alive = (
                    abs(process.create_time() - target["created"]) < 0.1 and process.is_running()
                )
            except psutil.NoSuchProcess:
                alive = False
            return (
                ("watching", "Процесс работает")
                if alive
                else ("unknown", "Процесс завершился; код результата недоступен")
            )
        if kind == "steam":
            data = steam_progress(target)
            previous = self.observed.get(row["id"])
            self.observed[row["id"]] = (data["state"], now)
            if data["state"] == "completed" and (not previous or previous[0] != "completed"):
                return "watching", "Проверяю подтверждение установки повторно"
            return data["state"], data["text"]
        path = clean_path(target)
        if not path.is_file() or is_link(path):
            self.observed.pop(row["id"], None)
            return "watching", "Ожидаю выбранный файл"
        if path.suffix.casefold() in {".part", ".partial", ".crdownload", ".tmp", ".download"}:
            return "watching", "Выберите окончательное имя файла без временного расширения"
        if any(
            path.with_name(path.name + suffix).exists()
            for suffix in (".part", ".partial", ".crdownload", ".download")
        ):
            self.observed.pop(row["id"], None)
            return "watching", "Есть временная загрузка рядом с файлом"
        stamp = fingerprint(path)
        previous = self.observed.get(row["id"])
        if not previous or previous[0] != stamp:
            self.observed[row["id"]] = (stamp, now)
            return "watching", "Файл появился или изменился; проверяю стабильность"
        if now - previous[1] >= 120 and file_available(path):
            return (
                "ready",
                "Файл закрыт и не менялся две минуты. Это проверка готовности файла, а не подтверждение результата программы.",
            )
        return "watching", "Ожидаю закрытия и стабильности файла"

    @staticmethod
    def _folder_files(folder):
        root = clean_path(folder)
        if not root.is_dir():
            raise ValueError("Папка наблюдения недоступна")
        result = {}
        for index, path in enumerate(root.iterdir()):
            if index >= 2000:
                raise ValueError("В папке больше 2000 объектов; выберите отдельную папку загрузки")
            if (
                not path.name.startswith(".")
                and not is_link(path)
                and path.is_file()
                and path.suffix.casefold()
                not in {".part", ".partial", ".crdownload", ".tmp", ".download"}
            ):
                result[path.name] = fingerprint(path)
        return result

    def _probe_folder(self, row, now):
        state = self.observed.setdefault(row["id"], {"at": -100, "files": {}})
        if now - state["at"] < 20:
            return "watching", row["detail"]
        state["at"] = now
        current = self._folder_files(row["target"])
        known = row.setdefault("known", {})
        ready = []
        for name, stamp in current.items():
            if known.get(name) == stamp:
                continue
            previous = state["files"].get(name)
            if not previous or previous[0] != stamp:
                state["files"][name] = (stamp, now)
                continue
            path = Path(row["target"]) / name
            temporary = any(
                path.with_name(name + suffix).exists()
                for suffix in (".part", ".partial", ".crdownload", ".download")
            )
            if now - previous[1] >= 120 and not temporary and file_available(path):
                known[name] = stamp
                ready.append(name)
        row["known"] = {k: v for k, v in known.items() if k in current}
        state["files"] = {
            k: v for k, v in state["files"].items() if k in current and row["known"].get(k) != v[0]
        }
        if ready:
            row["folder_notice"] = (
                "Новые файлы закрыты и стабильны: "
                + ", ".join(ready[:10])
                + (f" и ещё {len(ready) - 10}" if len(ready) > 10 else "")
            )
            return "watching", row["folder_notice"]
        return "watching", "Слежу за новыми и изменёнными файлами в выбранной папке"

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last_tick < 5:
            return
        self.last_tick = now
        with self.lock:
            for row in self.rows():
                if self.closed.is_set():
                    return
                if row["status"] not in ACTIVE:
                    continue
                previous = (row["status"], row["detail"])
                try:
                    if row["status"] == "shutdown_wait":
                        duty = self.runtime.autonomy
                        if not duty.permitted() or duty.atomic or duty.current:
                            continue
                        if os.name != "nt":
                            raise ValueError("Выключение доступно в Windows")
                        # Persist before the one irreversible dispatch: never replay
                        # after a crash or an ambiguous native response.
                        row.update(
                            status="review",
                            detail="Запрос выключения отправляется; при сбое проверьте Windows",
                        )
                        self.store.put("watches", row)
                        result = subprocess.run(
                            ["shutdown.exe", "/s", "/t", "60"],
                            capture_output=True,
                            text=True,
                            errors="replace",
                            timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        if result.returncode:
                            raise ValueError("Windows не приняла запрос выключения")
                        row.update(
                            status="shutdown_sent",
                            detail="Выключение через 60 секунд. Для отмены: «Крест, отмени выключение»",
                        )
                        self._notify(row)
                    else:
                        state, detail = self.probe(row, now)
                        row.update(status=state, detail=detail[:2000])
                        if row.pop("folder_notice", None):
                            self.store.put("watches", row)
                            self._notify(row)
                        if state != "watching":
                            if state == "completed" and row["shutdown"]:
                                row["status"] = "shutdown_wait"
                                row["detail"] += ". Выключение ожидает свободного компьютера."
                            # Claim terminal notification before sending, never spam on retry.
                            self.store.put("watches", row)
                            self._notify(row)
                except Exception as exc:
                    row["detail"] = "Проверка недоступна: " + str(exc)[:250]
                    if row["status"] != "watching":
                        row["status"] = "review"
                if (row["status"], row["detail"]) != previous:
                    self.store.put("watches", row)
