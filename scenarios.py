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


class ScenarioRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = threading.Event()
        self.thread = None
        self.status = "Сценарии готовы"
        self._lock = threading.Lock()

    def start(self, name):
        with self._lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("Уже выполняется сценарий. Сначала остановите его.")
            values = self.runtime.settings().get("scenarios", DEFAULT_SCENARIOS)
            actual = next((n for n in values if n.casefold() == name.casefold()), None)
            if actual is None:
                raise ValueError("Сценарий не найден")
            steps = copy.deepcopy(values[actual])
            validate_scenarios({actual: steps})
            self.stop.clear()
            self.status = "Запускаю: " + actual
            self.thread = threading.Thread(
                target=self._run, args=(actual, steps), daemon=True, name="krest-scenario"
            )
            self.thread.start()

    def cancel(self):
        self.stop.set()
        self.status = "Остановка запрошена; начатое действие завершится"

    def _run(self, name, steps):
        try:
            for number, step in enumerate(steps, 1):
                if self.stop.is_set() or self.runtime.stop_event.is_set():
                    self.status = f"{name}: остановлен перед шагом {number}"
                    return
                self.status = f"{name}: шаг {number}/{len(steps)} · {step['value']}"
                action, value = step["action"], step["value"]
                if action == "wait":
                    self.stop.wait(int(value))
                elif action == "profile":
                    self.runtime.performance.set_mode(value)
                elif action == "notifications":
                    self.runtime.notification_mode = value
                elif action == "app":
                    path = Path(value).expanduser()
                    if not path.is_absolute() or not path.is_file():
                        raise ValueError("Укажите полный путь к существующей программе")
                    subprocess.Popen([str(path)], shell=False)
                else:
                    if self.runtime.is_scenario_command(value):
                        raise ValueError("Сценарии не могут запускать другие сценарии")
                    result = self.runtime.execute(value, source="scenario")
                    if not result.success:
                        raise ValueError(result.text)
            self.status = f"{name}: " + ("остановлен" if self.stop.is_set() else "выполнен")
        except Exception as exc:
            self.status = f"{name}: остановлен из-за ошибки · {exc}"
        finally:
            self.runtime.publish_notice(self.status, source="Сценарии")
