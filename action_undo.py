"""Bounded session undo. Restore only the exact resource the action changed."""

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

_collector = ContextVar("krest_undo", default=None)


def record(lane, restore):
    collector = _collector.get()
    if collector is not None:
        collector.append((lane, restore))


class UndoHistory:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.Lock()
        self.entries = deque(maxlen=20)
        self.busy = False

    @contextmanager
    def capture(self, label):
        # One chain step = one undo item. Nested captures belong to their parent.
        if _collector.get() is not None:
            yield
            return
        changes = []
        token = _collector.set(changes)
        try:
            yield
        finally:
            _collector.reset(token)
            if changes:
                with self.lock:
                    self.entries.append(dict(label=label, changes=changes, at=time.monotonic()))

    def undo(self):
        with self.lock:
            if self.busy:
                return {"status": "error", "message": "Предыдущая отмена ещё выполняется."}
            while self.entries and time.monotonic() - self.entries[-1]["at"] > 1800:
                self.entries.pop()
            if not self.entries:
                return {
                    "status": "error",
                    "message": "Нет сохранённого обратимого действия за последние 30 минут. Могу вернуть громкость, таймеры, будильники и ручную сортировку.",
                }
            item = self.entries.pop()
            self.busy = True
        token = _collector.set(None)
        try:
            while item["changes"]:
                lane, restore = item["changes"][-1]
                lock = self.runtime._lane_locks[lane]
                if not lock.acquire(blocking=False):
                    raise ValueError(
                        "Это действие ещё занято. Повторите отмену после его завершения."
                    )
                try:
                    if self.runtime.command_stop.is_set():
                        raise ValueError(
                            "Отмена остановлена; уже восстановленные изменения сохранены."
                        )
                    restore()
                    item["changes"].pop()
                finally:
                    lock.release()
            return {"status": "success", "message": "Вернула как было: «" + item["label"] + "»."}
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "status": "error",
                "message": "Не удалось отменить «" + item["label"] + "»: " + str(exc),
            }
        finally:
            _collector.reset(token)
            with self.lock:
                if item["changes"]:
                    # Newer actions remain above this failed/partially undone one.
                    self.entries.append(item)
                    self.entries = deque(sorted(self.entries, key=lambda v: v["at"]), maxlen=20)
                self.busy = False


def record_volume(before, after, device):
    if before == after:
        return

    def restore():
        from skills.media import endpoint_volume

        with endpoint_volume(with_device=True) as (volume, current_device):
            if current_device != device:
                raise ValueError("Устройство вывода сменилось. Громкость не изменена.")
            level, muted = float(volume.GetMasterVolumeLevelScalar()), bool(volume.GetMute())
            if abs(level - after[0]) > 0.005 or muted != after[1]:
                raise ValueError("Громкость уже изменена вручную или другой программой.")
            volume.SetMasterVolumeLevelScalar(before[0], None)
            volume.SetMute(before[1], None)

    record("volume", restore)


def record_state(state, previous):
    if _collector.get() is None:
        return
    import copy

    for kind in ("active_timers", "active_alarms", "daily_tasks"):
        before = {v["id"]: v for v in previous[kind]}
        after = {v["id"]: copy.deepcopy(v) for v in getattr(state, kind)}
        for ident in before.keys() | after.keys():
            old, new = before.get(ident), after.get(ident)
            if old == new:
                continue

            def restore(kind=kind, ident=ident, old=old, new=new):
                with state._change():
                    items = getattr(state, kind)
                    current = next((v for v in items if v["id"] == ident), None)
                    if current != new:
                        raise ValueError("Таймер или задача уже изменены либо завершились.")
                    if (new and new["end_time"] <= time.time()) or (
                        old and old["end_time"] <= time.time()
                    ):
                        raise ValueError(
                            "Время этой задачи уже прошло; отмена не изменит напоминания."
                        )
                    items[:] = [v for v in items if v["id"] != ident]
                    if old:
                        items.append(dict(old))

            record("general", restore)


def record_sort(folder, result, stop):
    if not result.get("moved_count") or not result.get("batch"):
        return
    batch = result["batch"]

    def restore():
        from sort_history import SortHistory

        result = SortHistory(folder).undo_last(stop=stop(), batch_id=batch)
        if result["errors"]:
            raise ValueError(
                f"Возвращено файлов: {result['restored']}. " + "; ".join(result["errors"][:3])
            )

    record("files", restore)
