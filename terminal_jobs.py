"""One bounded watcher for long Telegram jobs; persistent results stay in the kernel."""

import threading
import time


class JobWatcher:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.thread = None

    def add(self, key, lookup, report):
        with self.lock:
            if key in self.pending:
                return True
            if len(self.pending) >= 32:
                return False
            self.pending[key] = (time.monotonic() + 900, lookup, report)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(
                    target=self._run, daemon=True, name="krest-telegram-jobs"
                )
                self.thread.start()
            return True

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            rows = list(self.pending.items())
        for key, (deadline, lookup, report) in rows:
            try:
                job = lookup(key)
            except Exception:
                job = None
            if job and job["status"] not in ("queued", "running"):
                text = job.get("result") or "Действие завершено: " + job["status"]
            elif now >= deadline:
                text = "Ожидание отчёта завершено. Последний результат доступен по /history; повторять команду не требуется."
            else:
                continue
            with self.lock:
                self.pending.pop(key, None)
            try:
                report(text)
            except Exception:
                # No replay of commands when Telegram delivery fails.
                pass

    def _run(self):
        while True:
            self.poll()
            with self.lock:
                if not self.pending:
                    self.thread = None
                    return
            time.sleep(1)


watcher = JobWatcher()


def history_text(rows):
    from datetime import datetime

    names = {
        "queued": "в очереди",
        "running": "выполняется",
        "completed": "готово",
        "failed": "ошибка",
        "cancelled": "отменено",
        "interrupted": "прервано",
    }
    return (
        "\n\n".join(
            datetime.fromtimestamp(row["created"]).strftime("%d.%m %H:%M")
            + " · "
            + names.get(row["status"], row["status"])
            + "\n"
            + row["command"][:150]
            + "\n"
            + str(row.get("result", ""))[:300]
            for row in rows[:8]
        )
        or "История команд этого пульта пока пуста."
    )
