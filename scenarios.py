"""Named, explicit action sequences. No shell interpolation or inferred actions."""

import copy
from pathlib import Path
import subprocess
import threading

DEFAULT_SCENARIOS = {
    "Игра": [
        {"action": "profile", "value": "game"},
        {"action": "notifications", "value": "important"},
    ],
    "Работа": [
        {"action": "profile", "value": "balanced"},
        {"action": "notifications", "value": "all"},
    ],
    "Отдых": [
        {"action": "profile", "value": "economy"},
        {"action": "notifications", "value": "silent"},
    ],
}


def validate_scenarios(values):
    if not isinstance(values, dict) or len(values) > 30:
        raise ValueError("Допускается до 30 сценариев")
    for name, steps in values.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 60
            or not isinstance(steps, list)
            or not 1 <= len(steps) <= 30
        ):
            raise ValueError("Сценарий: название до 60 символов и 1–30 шагов")
        for step in steps:
            if not isinstance(step, dict) or step.get("action") not in (
                "command",
                "app",
                "wait",
                "profile",
                "notifications",
                "wait_process",
                "wait_window",
                "notice",
                "service",
            ):
                raise ValueError("Неизвестное действие сценария")
            value = step.get("value")
            if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                raise ValueError("Укажите значение шага до 2000 символов")
            if step["action"] == "wait" and (not value.isdigit() or not 0 < int(value) <= 60):
                raise ValueError("Пауза: от 1 до 60 секунд")
            if step["action"] == "profile" and value not in ("auto", "balanced", "game", "economy"):
                raise ValueError("Неизвестный профиль нагрузки")
            if step["action"] == "notifications" and value not in ("all", "important", "silent"):
                raise ValueError("Неизвестный режим уведомлений")
            if type(step.get("timeout", 30)) is not int or not 1 <= step.get("timeout", 30) <= 300:
                raise ValueError("Ожидание: 1–300 секунд")
            if type(step.get("temporary", False)) is not bool:
                raise ValueError("Временная настройка должна быть логическим значением")
            condition = step.get("condition")
            if condition is not None and (
                not isinstance(condition, dict)
                or condition.get("kind") not in ("process", "file", "game", "weekday")
                or not isinstance(condition.get("value"), str)
                or type(condition.get("invert", False)) is not bool
            ):
                raise ValueError("Условие: процесс, файл, игра или день недели")


def condition_matches(condition, runtime):
    if condition is None:
        return True
    kind, value = condition["kind"], condition["value"]
    if kind == "process":
        import psutil

        result = any(
            (p.info.get("name") or "").casefold() == value.casefold()
            for p in psutil.process_iter(["name"])
        )
    elif kind == "file":
        result = Path(value).is_file()
    elif kind == "game":
        result = bool(runtime.performance.game)
    elif kind == "weekday":
        from datetime import datetime

        result = str(datetime.now().weekday()) in value.split(",")
    else:
        raise ValueError("Неизвестное условие")
    return not result if condition.get("invert") else result


class ScenarioRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.skip = threading.Event()
        self.thread = None
        self.status = "Сценарии готовы"
        self._lock = threading.RLock()
        self.failed = None
        self.current = None

    def preview(self, name):
        values = self.runtime.settings().get("scenarios", DEFAULT_SCENARIOS)
        actual = next((n for n in values if n.casefold() == name.casefold()), None)
        if actual is None:
            raise ValueError("Сценарий не найден")
        steps = copy.deepcopy(values[actual])
        validate_scenarios({actual: steps})
        return actual, steps

    def start(self, name, start_at=0):
        with self._lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("Уже выполняется сценарий. Сначала остановите его.")
            actual, steps = self.preview(name)
            self.stop.clear()
            self.skip.clear()
            self.failed = None
            self.status = "Запускаю: " + actual
            self.thread = threading.Thread(
                target=self._run, args=(actual, steps, start_at), daemon=True, name="krest-scenario"
            )
            self.thread.start()

    def retry(self):
        with self._lock:
            if not self.failed:
                raise ValueError("Нет шага для повторения")
            name, index = self.failed
            self.start(name, index)

    def skip_wait(self):
        if self.current not in ("wait", "wait_process", "wait_window"):
            raise ValueError("Пропуск доступен только во время ожидания")
        self.skip.set()

    def cancel(self):
        self.stop.set()
        self.status = "Остановка запрошена; начатое действие завершится"

    def _wait(self, action, value, timeout):
        import time

        deadline = time.monotonic() + (int(value) if action == "wait" else timeout)
        while (
            not self.stop.is_set()
            and not self.runtime.stop_event.is_set()
            and not self.skip.is_set()
        ):
            if action == "wait_process" and condition_matches(
                {"kind": "process", "value": value}, self.runtime
            ):
                return
            if action == "wait_window":
                import os

                if os.name != "nt":
                    raise ValueError("Ожидание окна доступно в Windows")
                import pygetwindow

                if pygetwindow.getWindowsWithTitle(value):
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if action != "wait":
                    raise TimeoutError("Истекло время ожидания: " + value)
                return
            self.stop.wait(min(0.25, remaining))

    def _run(self, name, steps, start_at=0):
        restore = []
        outcome = "completed"
        number = start_at
        job = None
        try:
            if hasattr(self.runtime, "jobs"):
                job = self.runtime.jobs.create("Сценарий «" + name + "»", "scenario")[0]
                self.runtime.jobs.transition(job, "running")
            for number in range(start_at, len(steps)):
                step = steps[number]
                if self.stop.is_set() or self.runtime.stop_event.is_set():
                    self.status = f"{name}: остановлен перед шагом {number + 1}"
                    outcome = "cancelled"
                    return
                if not condition_matches(step.get("condition"), self.runtime):
                    continue
                self.skip.clear()
                action, value = step["action"], step["value"]
                self.current = action
                self.status = f"{name}: шаг {number + 1}/{len(steps)} · {value}"
                if action in ("wait", "wait_process", "wait_window"):
                    self._wait(action, value, step.get("timeout", 30))
                elif action == "profile":
                    if step.get("temporary"):
                        restore.append(("profile", self.runtime.performance.override))
                    self.runtime.performance.set_mode(value)
                elif action == "notifications":
                    if step.get("temporary"):
                        restore.append(("notifications", self.runtime.notification_mode))
                    self.runtime.notification_mode = value
                elif action == "app":
                    path = Path(value).expanduser()
                    if not path.is_absolute() or not path.is_file():
                        raise ValueError("Укажите полный путь к существующей программе")
                    subprocess.Popen([str(path)], shell=False)
                elif action == "notice":
                    self.runtime.publish_notice(value, "Сценарии")
                elif action == "service":
                    if not hasattr(self.runtime, "integrations"):
                        raise ValueError("Сервисами управляет постоянное ядро")
                    if value not in {v[0] for v in self.runtime.integrations.MODULES.values()}:
                        raise ValueError("Сервис не зарегистрирован")
                    self.runtime.integrations.start(value)
                    if not self.runtime.integrations.details(value)["running"]:
                        raise ValueError("Сервис не запущен; проверьте журнал")
                else:
                    if self.runtime.is_scenario_command(value):
                        raise ValueError("Сценарии не могут запускать другие сценарии")
                    result = self.runtime.execute(value, source="scenario")
                    if not result.success:
                        raise ValueError(result.text)
            outcome = "cancelled" if self.stop.is_set() else "completed"
            self.status = f"{name}: " + ("остановлен" if outcome == "cancelled" else "выполнен")
        except Exception as exc:
            self.failed = (name, number)
            outcome = "failed"
            self.status = f"{name}: остановлен из-за ошибки · {exc}"
        finally:
            self.current = None
            for kind, value in reversed(restore):
                try:
                    if kind == "profile":
                        self.runtime.performance.override = value
                        self.runtime.performance.tick(force=True)
                    else:
                        self.runtime.notification_mode = value
                except Exception as exc:
                    self.status += f" · Не удалось восстановить {kind}: {exc}"
                    outcome = "failed"
            try:
                if hasattr(self.runtime, "memory"):
                    self.runtime.memory.event(
                        "scenario_finished",
                        {"name": name, "outcome": outcome, "step": number + 1, "text": self.status},
                    )
                if job:
                    self.runtime.jobs.transition(job, outcome, self.status)
            except Exception as exc:
                self.status += f" · Не удалось записать результат: {exc}"
            self.runtime.publish_notice(self.status, source="Сценарии")
