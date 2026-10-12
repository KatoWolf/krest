"""One shared application state, independent of the entry point."""

from storage import read_json


from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import copy
import json
import math
import logging
import shutil
import threading
import time
import uuid

from storage import atomic_write_json

_current = ContextVar("assistant_state", default=None)
_COLLECTIONS = (
    "active_timers",
    "active_alarms",
    "daily_tasks",
    "pending_reminders",
    "completed_tasks",
)


def _valid_item(item, seen):
    if not isinstance(item, dict):
        return False
    ident, deadline = item.get("id"), item.get("end_time")
    valid = (
        isinstance(ident, str)
        and bool(ident)
        and ident not in seen
        and type(deadline) in (int, float)
        and 0 < deadline < 253402300800
        and isinstance(item.get("label", ""), str)
    )
    if not valid:
        return False
    try:
        datetime.fromtimestamp(deadline)
    except (ValueError, OverflowError, OSError):
        return False
    return True


class AssistantState:
    def __init__(self, filepath=None):
        self._lock = threading.RLock()
        self.filepath = Path(filepath) if filepath else None
        self.is_muted = False
        self.is_sleeping = False
        self.pending_prompt = None
        self.start_time = time.time()
        self.active_timers = []
        self.active_alarms = []
        self.pending_reminders = []
        self.daily_tasks = []
        self.completed_tasks = []
        self.last_target = None
        self.load_error = ""
        self.stopwatch_start_time = None
        self.shutdown_requested = threading.Event()
        self.skill_manager = None
        self.config_getter = lambda: {}
        if self.filepath and self.filepath.exists():
            try:
                saved = read_json(self.filepath)
                if not isinstance(saved, dict):
                    raise ValueError("Ожидался объект с таймерами")
                seen = set()
                for key in _COLLECTIONS:
                    values = saved.get(key, [])
                    if not isinstance(values, list):
                        self.load_error = "Повреждён список напоминаний"
                        continue
                    for item in values:
                        if key == "completed_tasks" and (
                            not isinstance(item, dict)
                            or type(item.get("completed_at")) not in (int, float)
                            or not 0 < item["completed_at"] < 253402300800
                        ):
                            self.load_error = "Повреждена история выполнения"
                            continue
                        if key == "daily_tasks":
                            try:
                                datetime.strptime(item.get("time_str", ""), "%H:%M")
                                days = item.get("weekdays", list(range(7)))
                                if (
                                    not days
                                    or not isinstance(days, list)
                                    or any(type(v) is not int or v not in range(7) for v in days)
                                ):
                                    raise ValueError("Дни недели")
                            except (ValueError, AttributeError, TypeError):
                                self.load_error = "Повреждено время ежедневного напоминания"
                                continue
                        if _valid_item(item, seen):
                            item.setdefault("label", "Напоминание")
                            getattr(self, key).append(item)
                            seen.add(item["id"])
                        else:
                            self.load_error = "Некоторые напоминания повреждены и пропущены"
            except (ValueError, OSError) as exc:
                self.load_error = str(exc)
            if self.load_error:
                logging.getLogger(__name__).warning("Состояние: %s", self.load_error)

    @property
    def owner_name(self):
        return self.config_getter().get("owner_name", "")

    def persist(self):
        with self._lock:
            if self.filepath:
                if self.load_error and self.filepath.exists():
                    shutil.copy2(
                        self.filepath,
                        self.filepath.with_name(self.filepath.name + f".broken-{time.time_ns()}"),
                    )
                atomic_write_json(
                    self.filepath, {**self.snapshot(), "pending_reminders": self.reminders()}
                )
                self.load_error = ""

    @contextmanager
    def _change(self):
        """Do not report success or lose a reminder when saving fails."""
        with self._lock:
            previous = {key: copy.deepcopy(getattr(self, key)) for key in _COLLECTIONS}
            previous_target = self.last_target
            try:
                yield
                self.persist()
            except Exception:
                for key, items in previous.items():
                    setattr(self, key, items)
                self.last_target = previous_target
                raise

    def reminders(self):
        with self._lock:
            return [dict(item) for item in self.pending_reminders]

    def acknowledge(self, item_id):
        with self._change():
            self.pending_reminders[:] = [v for v in self.pending_reminders if v["id"] != item_id]

    def snooze(self, item_id, seconds=300):
        self._validate_duration(seconds)
        with self._change():
            item = next((v for v in self.pending_reminders if v["id"] == item_id), None)
            if item is None:
                raise ValueError("Напоминание уже закрыто")
            timer = dict(
                id=uuid.uuid4().hex,
                label=item["label"],
                end_time=time.time() + seconds,
                total_seconds=seconds,
            )
            if "task_id" in item:
                timer["task_id"] = item["task_id"]
            self.active_timers.append(timer)
            self.last_target = (timer["id"], time.monotonic())
            self.pending_reminders.remove(item)
            return dict(timer)

    @staticmethod
    def _validate_duration(seconds):
        if (
            type(seconds) not in (int, float)
            or not 0 < seconds <= 365 * 86400
            or not math.isfinite(seconds)
        ):
            raise ValueError("Длительность должна быть больше нуля и не больше года")

    def snapshot(self):
        with self._lock:
            return {
                k: [dict(item) for item in getattr(self, k)]
                for k in ("active_timers", "active_alarms", "daily_tasks", "completed_tasks")
            }

    def add_timer(self, seconds, label):
        self._validate_duration(seconds)
        with self._change():
            item = dict(
                id=uuid.uuid4().hex,
                end_time=time.time() + seconds,
                label=label,
                total_seconds=seconds,
            )
            self.active_timers.append(item)
            self.last_target = (item["id"], time.monotonic())
            return dict(item)

    def add_alarm(self, time_str, label="Будильник", now=None, daily=False, weekdays=None):
        now = now or datetime.now()
        parsed = datetime.strptime(time_str, "%H:%M")
        target = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        days = list(range(7)) if weekdays is None else sorted(set(weekdays))
        if not days or any(type(day) is not int or day not in range(7) for day in days):
            raise ValueError("Выберите хотя бы один день недели")
        while target <= now or (daily and target.weekday() not in days):
            target += timedelta(days=1)
        with self._change():
            item = dict(
                id=uuid.uuid4().hex,
                end_time=target.timestamp(),
                time_str=time_str,
                label=label,
            )
            if daily:
                item["weekdays"] = days
            self.last_target = (item["id"], time.monotonic())
            (self.daily_tasks if daily else self.active_alarms).append(item)
            return dict(item)

    def cancel(self, kind, item_id=None):
        if kind not in ("active_timers", "active_alarms", "daily_tasks"):
            raise ValueError("Неизвестный тип таймера")
        with self._change():
            items = getattr(self, kind)
            before = len(items)
            items[:] = [item for item in items if item_id and item["id"] != item_id]
            return before - len(items)

    def extend_timer(self, item_id, seconds):
        self._validate_duration(seconds)
        with self._change():
            item = next((v for v in self.active_timers if v["id"] == item_id), None)
            if item is None or item["end_time"] <= time.time():
                raise ValueError("Этот таймер уже закончился")
            self._validate_duration(item["end_time"] - time.time() + seconds)
            item["end_time"] += seconds
            item["total_seconds"] = item.get("total_seconds", 0) + seconds
            self.last_target = (item_id, time.monotonic())

    def complete(self, item_id):
        with self._change():
            item = next((v for v in self.pending_reminders if v["id"] == item_id), None)
            if item is None:
                raise ValueError("Напоминание уже закрыто")
            self.completed_tasks.append(dict(item, completed_at=time.time()))
            self.completed_tasks[:] = self.completed_tasks[-500:]
            self.pending_reminders.remove(item)

    def move_occurrence(self, item_id, timestamp):
        """Move just the next daily occurrence; subsequent days keep their clock."""
        if (
            type(timestamp) not in (int, float)
            or not time.time() < timestamp <= time.time() + 365 * 86400
        ):
            raise ValueError("Выберите будущую дату в пределах года")
        with self._change():
            item = next(
                (
                    v
                    for k in ("daily_tasks", "active_alarms", "active_timers")
                    for v in getattr(self, k)
                    if v["id"] == item_id
                ),
                None,
            )
            if item is None:
                raise ValueError("Задача уже завершилась")
            item["end_time"] = timestamp
            self.last_target = (item_id, time.monotonic())

    def skip_occurrence(self, item_id):
        with self._change():
            item = next((v for v in self.daily_tasks if v["id"] == item_id), None)
            if item is None:
                raise ValueError("Выберите повторяющуюся задачу")
            clock = datetime.strptime(item["time_str"], "%H:%M")
            target = datetime.fromtimestamp(max(time.time(), item["end_time"])).replace(
                hour=clock.hour, minute=clock.minute, second=0, microsecond=0
            ) + timedelta(days=1)
            while target.weekday() not in item.get("weekdays", list(range(7))):
                target += timedelta(days=1)
            item["end_time"] = target.timestamp()

    def take_due(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if not any(
                item["end_time"] <= now
                for kind in ("active_timers", "active_alarms", "daily_tasks")
                for item in getattr(self, kind)
            ):
                return []
            return self._collect_due(now)

    def _collect_due(self, now):
        with self._change():
            due = []
            for kind in ("active_timers", "active_alarms"):
                items = getattr(self, kind)
                due.extend(dict(item, kind=kind) for item in items if item["end_time"] <= now)
                items[:] = [item for item in items if item["end_time"] > now]
            self.pending_reminders.extend(dict(item) for item in due)
            for task in self.daily_tasks:
                if task["end_time"] <= now:
                    reminder = dict(
                        task, id=uuid.uuid4().hex, task_id=task["id"], kind="daily_tasks"
                    )
                    due.append(reminder)
                    self.pending_reminders.append(dict(reminder))
                    clock = datetime.strptime(task["time_str"], "%H:%M")
                    target = datetime.fromtimestamp(now).replace(
                        hour=clock.hour, minute=clock.minute, second=0, microsecond=0
                    )
                    while target.timestamp() <= now or target.weekday() not in task.get(
                        "weekdays", list(range(7))
                    ):
                        target += timedelta(days=1)
                    task["end_time"] = target.timestamp()
            return due


state = AssistantState()


def get_state():
    return _current.get() or state


@contextmanager
def use_state(value):
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)
