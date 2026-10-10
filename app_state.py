"""One shared application state, independent of the entry point."""

from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import json
import threading
import time
import uuid

from storage import atomic_write_json

_current = ContextVar("assistant_state", default=None)


class AssistantState:
    def __init__(self, filepath=None):
        self._lock = threading.RLock()
        self.filepath = Path(filepath) if filepath else None
        self.is_muted = False
        self.is_sleeping = False
        self.dota_guide_active = False
        self.pending_prompt = None
        self.start_time = time.time()
        self.active_timers = []
        self.active_alarms = []
        self.stopwatch_start_time = None
        self.shutdown_requested = threading.Event()
        self.skill_manager = None
        self.config_getter = lambda: {}
        if self.filepath and self.filepath.exists():
            try:
                saved = json.loads(self.filepath.read_text(encoding="utf-8"))
                for key in ("active_timers", "active_alarms"):
                    values = saved.get(key, [])
                    setattr(
                        self,
                        key,
                        [
                            v
                            for v in values
                            if isinstance(v, dict)
                            and isinstance(v.get("end_time"), (int, float))
                            and v.get("id")
                        ],
                    )
            except (ValueError, OSError, AttributeError):
                pass

    @property
    def owner_name(self):
        return self.config_getter().get("owner_name", "КатоВульф")

    def persist(self):
        if self.filepath:
            atomic_write_json(self.filepath, self.snapshot())

    def snapshot(self):
        with self._lock:
            return {
                k: [dict(item) for item in getattr(self, k)]
                for k in ("active_timers", "active_alarms")
            }

    def add_timer(self, seconds, label):
        if not 0 < seconds <= 365 * 86400:
            raise ValueError("Длительность должна быть больше нуля и не больше года")
        with self._lock:
            item = dict(
                id=uuid.uuid4().hex,
                end_time=time.time() + seconds,
                label=label,
                total_seconds=seconds,
            )
            self.active_timers.append(item)
            self.persist()
            return item

    def add_alarm(self, time_str, label="Будильник", now=None):
        now = now or datetime.now()
        parsed = datetime.strptime(time_str, "%H:%M")
        target = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        with self._lock:
            item = dict(
                id=uuid.uuid4().hex,
                end_time=target.timestamp(),
                time_str=time_str,
                label=label,
            )
            self.active_alarms.append(item)
            self.persist()
            return item

    def cancel(self, kind, item_id=None):
        if kind not in ("active_timers", "active_alarms"):
            raise ValueError("Неизвестный тип таймера")
        with self._lock:
            items = getattr(self, kind)
            before = len(items)
            items[:] = [item for item in items if item_id and item["id"] != item_id]
            self.persist()
            return before - len(items)

    def take_due(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            due = []
            for kind in ("active_timers", "active_alarms"):
                items = getattr(self, kind)
                due.extend(dict(item, kind=kind) for item in items if item["end_time"] <= now)
                items[:] = [item for item in items if item["end_time"] > now]
            if due:
                self.persist()
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
