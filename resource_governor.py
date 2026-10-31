"""Cooperative budgets for Krest's background work; voice and user actions stay live."""

from collections import deque
from contextlib import contextmanager
import copy
import os
import threading
import time

NAMES = {
    "incoming": "Входящие файлы",
    "habits": "Наблюдение привычек",
    "catalog": "Каталог программ",
    "index": "Индекс файлов",
    "vision": "Зрение игры",
    "afk": "Тяжёлые поручения",
}
DEFAULTS = {
    "enabled": True,
    "cpu_percent": 8,
    "game_cpu_percent": 4,
    "memory_mb": 1600,
    "automatic_relief": True,
}


def validate_settings(value):
    if any(type(value.get(k)) is not bool for k in ("enabled", "automatic_relief")):
        raise ValueError("Защита сессии: нужны переключатели")
    for key, low, high in (
        ("cpu_percent", 2, 30),
        ("game_cpu_percent", 1, 20),
        ("memory_mb", 512, 4096),
    ):
        if type(value.get(key)) is not int or not low <= value[key] <= high:
            raise ValueError(f"Бюджет {key}: от {low} до {high}")


class ResourceGovernor:
    def __init__(self, runtime, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self.lock = threading.RLock()
        self.since = clock()
        self.last_sample = None
        self.level = "normal"
        self.reason = "Фон укладывается в бюджет"
        self.over = self.under = 0
        self.voice_until = self.relief_until = 0
        self.rows = {
            key: {
                "cpu_seconds": 0.0,
                "elapsed_seconds": 0.0,
                "calls": 0,
                "next": 0.0,
                "reason": "Готова",
            }
            for key in NAMES
        }
        self.events = deque(maxlen=40)
        self.samples = deque(maxlen=12)
        self.cores = os.cpu_count() or 1

    def voice(self):
        self.voice_until = self.clock() + 2

    def tick(self):
        # PerformancePolicy already owns process sampling; never enumerate again.
        metrics = self.runtime.performance.metrics
        stamp = metrics.get("sampled_at")
        if stamp is None or stamp == self.last_sample:
            return
        self.last_sample = stamp
        cfg = self.runtime.settings().get("resource_guard", DEFAULTS)
        cpu, memory = metrics.get("cpu", 0), metrics.get("memory_mb", 0)
        limit = cfg["game_cpu_percent"] if self.runtime.performance.game else cfg["cpu_percent"]
        with self.lock:
            self.samples.append({"at": self.clock(), "cpu": cpu, "memory_mb": memory})
            high = cpu > limit or memory > cfg["memory_mb"]
            self.over = self.over + 1 if high else 0
            self.under = (
                self.under + 1 if cpu < limit * 0.65 and memory < cfg["memory_mb"] * 0.9 else 0
            )
            previous = self.level
            if not cfg["enabled"]:
                self.level, self.reason = "normal", "Автоматический бюджет выключен"
            elif self.over >= 2:
                self.level = "limited"
                self.reason = (
                    f"Крест: CPU {cpu}% / цель {limit}%; память {memory} / {cfg['memory_mb']} МБ"
                )
            elif self.under >= 3:
                self.level, self.reason = "normal", "Нагрузка снизилась; фон возобновлён"
            if previous != self.level:
                self.events.append({"at": time.time(), "text": self.reason})

    def relief(self, seconds=120):
        with self.lock:
            self.relief_until = max(self.relief_until, self.clock() + min(300, max(10, seconds)))
            self.events.append(
                {"at": time.time(), "text": "Фон приостановлен для освобождения ресурсов"}
            )
        return "Фон Крест приостановлен на две минуты. Голос, напоминания и друзья продолжают работать."

    def allowed(self, key):
        if key not in self.rows:
            raise ValueError("Неизвестная фоновая функция")
        now = self.clock()
        cfg = self.runtime.settings().get("resource_guard", DEFAULTS)
        with self.lock:
            reason = ""
            if self.runtime.stop_event.is_set():
                reason = "Крест завершается"
            elif now < self.voice_until or getattr(self.runtime, "current_command", ""):
                reason = "Приоритет голосовой или ручной команды"
            elif now < self.relief_until:
                reason = "Временно освобождаю ресурсы"
            elif cfg["enabled"] and self.level == "limited":
                reason = self.reason
            elif cfg["enabled"] and now < self.rows[key]["next"]:
                reason = "Функция израсходовала бюджет прохода; даю остальным ресурс"
            self.rows[key]["reason"] = reason or "Готова"
            return not reason, reason

    @contextmanager
    def measure(self, key):
        start, cpu = self.clock(), time.thread_time()
        try:
            yield
        finally:
            elapsed, used = max(0.0, self.clock() - start), max(0.0, time.thread_time() - cpu)
            with self.lock:
                row = self.rows[key]
                row["cpu_seconds"] += used
                row["elapsed_seconds"] += elapsed
                row["calls"] += 1
                # Only cooperative workers are delayed. No process suspension,
                # affinity changes or limits on microphone callbacks.
                budget = 0.02 if self.runtime.performance.game else 0.05
                if used > budget:
                    row["next"] = self.clock() + min(120, max(3, used / budget * 3))

    def snapshot(self):
        with self.lock:
            elapsed = max(1, self.clock() - self.since)
            rows = [
                dict(
                    id=key,
                    name=NAMES[key],
                    calls=row["calls"],
                    cpu_percent=round(row["cpu_seconds"] / elapsed / self.cores * 100, 3),
                    cpu_ms=round(row["cpu_seconds"] * 1000),
                    reason=row["reason"],
                    wait_seconds=max(0, round(row["next"] - self.clock())),
                )
                for key, row in self.rows.items()
            ]
            return {
                "level": self.level,
                "reason": self.reason,
                "functions": rows,
                "history": copy.deepcopy(list(self.events)),
                "samples": list(self.samples),
                "relief_seconds": max(0, round(self.relief_until - self.clock())),
                "note": "CPU функций — среднее время Python-потоков с запуска. Нативные потоки и дочерние процессы показаны отдельно. Это управление фоном, а не жёсткий лимит CPU всей программы.",
            }
