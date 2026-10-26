"""Observe supported postconditions after dispatch, without delaying the voice lane."""

import collections
import queue
import threading
import time
import uuid


class ActionVerifier:
    def __init__(self, notice):
        self.notice = notice
        self.rows = collections.deque(maxlen=128)
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=12)
        self.worker = None
        self.closed = threading.Event()
        self.done = {}
        self.latest = {}

    def record(self, skill, value):
        if not isinstance(value, dict):
            return ""
        status = value.get("verification") or (
            "failed" if value.get("status") == "error" or value.get("success") is False else ""
        )
        if not status:
            return ""
        row = {
            "id": uuid.uuid4().hex,
            "skill": skill,
            "status": status,
            "text": value.get("message", ""),
            "time": time.time(),
        }
        with self.lock:
            check = value.get("check") if isinstance(value.get("check"), dict) else {}
            context = (
                value.get("voice_context") if isinstance(value.get("voice_context"), dict) else {}
            )
            resource = (
                "music:" + str(check.get("source") or context.get("player"))
                if check.get("source") or context.get("player")
                else "process:" + str(check["path"])
                if check.get("kind") == "process"
                else ""
            )
            if resource:
                previous = self.latest.get(resource)
                for old in self.rows:
                    if old.get("id") == previous and old["status"] == "pending":
                        self._complete(
                            old, "superseded", "Для этого приложения уже отправлено новое действие"
                        )
                self.latest[resource] = row["id"]
            self.rows.append(row)
            alive = {r.get("id") for r in self.rows}
            self.done = {ident: event for ident, event in self.done.items() if ident in alive}
            self.latest = {key: ident for key, ident in self.latest.items() if ident in alive}
            self.done[row["id"]] = threading.Event()
            if check:
                try:
                    self.queue.put_nowait((row, dict(check)))
                except queue.Full:
                    row.update(
                        status="unconfirmed", text="Проверка отложена: очередь наблюдений заполнена"
                    )
                    self.done[row["id"]].set()
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(
                        target=self._run, name="KrestEffectCheck", daemon=True
                    )
                    self.worker.start()
            else:
                if row["status"] == "pending":
                    row.update(
                        status="unconfirmed", text="Навык не указал способ проверки результата"
                    )
                self.done[row["id"]].set()
        return row["id"]

    def _complete(self, row, status, text):
        with self.lock:
            if row["status"] == "superseded":
                return
            row.update(status=status, text=text)
            if event := self.done.get(row["id"]):
                event.set()

    def get(self, ident):
        with self.lock:
            row = next((r for r in self.rows if r["id"] == ident), None)
            return dict(row) if row else None

    def wait(self, ident, stop, timeout=5):
        until = time.monotonic() + timeout
        while not stop.is_set():
            row = self.get(ident)
            if not row or row["status"] != "pending":
                return row or {"status": "unconfirmed", "text": "Проверка результата недоступна"}
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            with self.lock:
                event = self.done.get(ident)
            if event:
                event.wait(min(0.1, remaining))
        return {
            "status": "unconfirmed",
            "text": "Ожидание подтверждения остановлено"
            if stop.is_set()
            else "Приложение ещё не подтвердило результат",
        }

    def _run(self):
        while not self.closed.is_set():
            try:
                row, check = self.queue.get(timeout=1)
            except queue.Empty:
                # Retire after an idle second; record() restarts under this same
                # lock so a new check cannot be stranded during shutdown.
                with self.lock:
                    if self.queue.empty():
                        self.worker = None
                        return
                continue
            try:
                if row["status"] != "pending":
                    continue
                confirmed = False
                for _ in range(4):
                    if self.closed.wait(0.25):
                        return
                    if row["status"] != "pending":
                        break
                    if check["kind"] == "process":
                        import psutil
                        from pathlib import Path

                        target = Path(check["path"]).resolve()
                        for process in psutil.process_iter(["name"]):
                            if (
                                process.info.get("name") or ""
                            ).casefold() != target.name.casefold():
                                continue
                            try:
                                if Path(process.exe()).resolve() == target:
                                    confirmed = True
                                    break
                            except (psutil.Error, OSError):
                                continue
                    elif check["kind"] == "music":
                        from media_control import query

                        state = query(check.get("source", ""))
                        if check["action"] in ("next", "prev"):
                            confirmed = bool(
                                state.get("title") and state.get("title") != check.get("before")
                            )
                        else:
                            confirmed = state.get("status") == check.get("expected")
                    if confirmed:
                        break
                self._complete(
                    row,
                    "confirmed" if confirmed else "unconfirmed",
                    "Изменение подтверждено приложением"
                    if confirmed
                    else "Приложение приняло запрос, но изменение пока не подтверждено",
                )
            except Exception:
                self._complete(
                    row,
                    "unconfirmed",
                    "Не удалось прочитать состояние приложения. Проверьте его доступность.",
                )
            finally:
                self.queue.task_done()
            if row["status"] == "unconfirmed":
                self.notice(row["text"], "Проверка действия", "low", speak=False)

    def summary(self):
        with self.lock:
            rows = list(self.rows)[-5:]
        if not rows:
            return "Проверяемых действий пока нет. Поддерживаются плеер, микрофон Discord и запуск EXE из каталога."
        return "\n".join(
            (
                "Подтверждено"
                if r["status"] == "confirmed"
                else "Проверяю"
                if r["status"] == "pending"
                else "Не подтверждено"
            )
            + ": "
            + r["text"]
            for r in reversed(rows)
        )
