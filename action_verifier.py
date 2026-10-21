"""Observe supported postconditions after dispatch, without delaying the voice lane."""

import collections
import queue
import threading
import time


class ActionVerifier:
    def __init__(self, notice):
        self.notice = notice
        self.rows = collections.deque(maxlen=30)
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=12)
        self.worker = None
        self.closed = threading.Event()

    def record(self, skill, value):
        if not isinstance(value, dict):
            return
        status = value.get("verification") or (
            "failed" if value.get("status") == "error" or value.get("success") is False else ""
        )
        if not status:
            return
        row = {
            "skill": skill,
            "status": status,
            "text": value.get("message", ""),
            "time": time.time(),
        }
        with self.lock:
            self.rows.append(row)
            if value.get("check"):
                try:
                    self.queue.put_nowait((row, dict(value["check"])))
                except queue.Full:
                    row.update(
                        status="unconfirmed", text="Проверка отложена: очередь наблюдений заполнена"
                    )
                if self.worker is None or not self.worker.is_alive():
                    self.worker = threading.Thread(
                        target=self._run, name="KrestEffectCheck", daemon=True
                    )
                    self.worker.start()

    def _run(self):
        while not self.closed.is_set():
            try:
                row, check = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                confirmed = False
                for _ in range(4):
                    if self.closed.wait(0.25):
                        return
                    if check["kind"] == "process":
                        import psutil
                        from pathlib import Path

                        confirmed = any(
                            p.info.get("exe")
                            and Path(p.info["exe"]).resolve() == Path(check["path"]).resolve()
                            for p in psutil.process_iter(["exe"])
                        )
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
                with self.lock:
                    row.update(
                        status="confirmed" if confirmed else "unconfirmed",
                        text="Изменение подтверждено приложением"
                        if confirmed
                        else "Приложение приняло запрос, но изменение пока не подтверждено",
                    )
            except Exception:
                with self.lock:
                    row.update(
                        status="unconfirmed",
                        text="Не удалось прочитать состояние приложения. Проверьте его доступность.",
                    )
            finally:
                self.queue.task_done()
            if row["status"] != "confirmed":
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
