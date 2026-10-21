"""One lightweight coordinator for selected watches, sound and routine suggestions."""

from pathlib import Path
import logging
import threading

from smart_store import SmartStore
from object_actions import ObjectActions
from download_workflows import DownloadWorkflows
from task_watch import TaskWatch
from routine_learning import RoutineLearning
from audio_dispatcher import AudioDispatcher

logger = logging.getLogger(__name__)


class SmartTools:
    def __init__(self, runtime, root):
        self.runtime = runtime
        self.store = SmartStore(Path(root) / "smart_automation.sqlite3")
        self.objects = ObjectActions(runtime, root)
        self.downloads = DownloadWorkflows(runtime, self.store)
        self.watches = TaskWatch(runtime, self.store)
        self.habits = RoutineLearning(runtime, self.store)
        self.audio = AudioDispatcher(runtime, self.store)
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="krest-selected-tasks")
        self.thread.start()

    def _run(self):
        while not self.stop.wait(3):
            for component in (self.watches, self.audio, self.habits):
                if self.stop.is_set():
                    return
                try:
                    component.tick()
                except Exception:
                    logger.exception("Сбой выбранной автоматизации: %s", type(component).__name__)

    def snapshot(self):
        return {
            "downloads": self.downloads.rules(),
            "results": self.store.rows("download_items", 30),
            "watches": [{k: v for k, v in r.items() if k != "known"} for r in self.watches.rows()],
            "habits": self.store.rows("habits", 100),
            "audio_profiles": self.store.rows("audio_profiles", 4),
            "audio_status": self.audio.status,
        }

    def call(self, action, **values):
        if action == "snapshot":
            return self.snapshot()
        if action == "objects.select":
            return self.objects.select(values.get("paths", []), values.get("folder", ""))
        if action == "objects.current":
            return self.objects.current()
        if action == "downloads.add":
            return self.downloads.add(**values)
        if action == "downloads.change":
            return self.downloads.change(**values)
        if action == "downloads.retry":
            row = self.store.get("download_items", values["id"])
            if not row:
                raise ValueError("Результат обработки не найден")
            row.update(status="waiting", result="Повтор запрошен")
            self.store.put("download_items", row, limit=5000)
            return "Обработка повторится при AFK"
        if action == "watch.add":
            return self.watches.add(**values)
        if action == "watch.steam":
            return self.watches.steam(**values)
        if action == "watch.choices":
            import psutil
            from steam_service import local_games

            return {
                "games": local_games(),
                "jobs": [
                    r
                    for r in self.runtime.autonomy.store.jobs()
                    if r["status"] in {"waiting", "running", "retry"}
                ],
                "processes": [
                    {"pid": p.info["pid"], "name": p.info["name"]}
                    for p in psutil.process_iter(["pid", "name"])
                    if p.info["name"]
                ][:500],
            }
        if action == "watch.cancel":
            return self.watches.cancel(values.get("id"))
        if action == "audio.capture":
            return self.audio.capture(values["profile"])
        if action == "audio.apply":
            with self.audio.lock:
                return self.audio.apply(values["profile"])
        if action == "audio.delete":
            self.store.remove("audio_profiles", values["profile"])
            self.audio.signature = None
            return "Звуковой профиль удалён"
        if action == "habit.activate":
            return self.habits.activate(values["id"])
        if action == "habit.dismiss":
            return self.habits.dismiss(values["id"])
        if action == "habit.clear":
            for row in self.store.rows("habits", 100):
                if row["status"] != "enabled":
                    self.store.remove("habits", row["id"])
            return "Наблюдения привычек очищены; включённые правила сохранены"
        if action == "settings":
            section = values["section"]
            allowed = {
                "smart_tools": {"learn_routines"},
                "sound_dispatcher": {"enabled", "auto_calls", "restore_devices"},
            }
            options = values.get("options", {})
            if (
                section not in allowed
                or set(options) - allowed[section]
                or any(type(v) is not bool for v in options.values())
            ):
                raise ValueError("Некорректные настройки автоматизации")
            with self.runtime.config_manager._lock:
                cfg = self.runtime.config_manager.snapshot()
                cfg[section].update(options)
                self.runtime.config_manager.save_config(cfg)
            return "Настройки автоматизации сохранены"
        raise ValueError("Неизвестное действие автоматизации")

    def close(self):
        self.stop.set()
        self.watches.close()
        if self.thread:
            self.thread.join(timeout=2)
