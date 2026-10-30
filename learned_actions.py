"""Explicit user-taught UI recipes. No idle recording and no replay after failure."""

import copy
import os
import threading
import time
import uuid
from learned_model import MAX_STEPS, name_key, same_app, step_label, validate_step


class LearnedActions:
    def __init__(self, runtime, store):
        self.runtime, self.store = runtime, store
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.stop = threading.Event()
        self.rows = {r["id"]: r for r in store.rows("ui_actions", 30)}
        self.draft = None
        self.status = "Откройте программу и начните обучение"
        self.progress = []
        self.playing = False
        self.current = ""
        self.capture_busy = False

    def native(self, action, **data):
        if os.name != "nt":
            raise ValueError("Обученные кнопки работают в Windows")
        from desktop_transport import request

        reply = request({"operation": "learned_action", "action": action, "data": data}, timeout=8)
        if reply.get("error"):
            raise ValueError(reply["error"])
        if not isinstance(reply.get("result"), dict):
            raise ValueError("Помощник не подтвердил результат; повтор остановлен")
        return reply["result"]

    def _changed(self, text):
        self.status = text
        self.runtime.events.put(("learned_changed", text))
        return text

    def _draft(self):
        if not self.draft or time.monotonic() > self.draft["expires"]:
            self.draft = None
            raise ValueError("Начните обучение: «Крест, учись действию название»")
        return self.draft

    def begin(self, name):
        name = name_key(name)
        with self.lock:
            if self.draft and time.monotonic() > self.draft["expires"]:
                self.draft = None
            if self.playing or self.capture_busy:
                raise ValueError("Сначала остановите текущее действие")
            if self.draft:
                raise ValueError("Сохраните или отмените текущий черновик")
            if len(self.rows) >= 30:
                raise ValueError("Сохранено 30 действий; удалите ненужное")
            if any(name_key(row["name"]) == name for row in self.rows.values()):
                raise ValueError("Это название уже занято")
            self.draft = {
                "id": uuid.uuid4().hex,
                "name": name,
                "steps": [],
                "expires": time.monotonic() + 900,
            }
            return self._changed(
                "Учу действие «"
                + name
                + "». Наведи на кнопку и скажи «запомни кнопку» или нажми Ctrl+Alt+F9. Затем нажми её вручную."
            )

    def capture(self):
        with self.lock:
            draft = self._draft()
            if self.capture_busy or self.playing:
                raise ValueError("Предыдущий шаг ещё обрабатывается")
            if len(draft["steps"]) >= MAX_STEPS:
                raise ValueError("В действии допускается до 12 шагов")
            ident = draft["id"]
            self.capture_busy = True
        try:
            step = validate_step(
                self.native(
                    "capture", exclude_pids=[os.getpid()], exclude_titles=["KREST OS · Реликт"]
                )["step"]
            )
            with self.lock:
                draft = self._draft()
                if draft["id"] != ident:
                    raise ValueError("Обучение было отменено")
                if draft["steps"] and not same_app(step["exe"], draft["steps"][0]["exe"]):
                    raise ValueError(
                        "Один навык обучается в одной программе. Для другой сохраните отдельное действие"
                    )
                draft["steps"].append(step)
                draft["expires"] = time.monotonic() + 900
                return self._changed(
                    f"Шаг {len(draft['steps'])}: {step_label(step)}. Запомнила; нажатие остаётся за тобой."
                )
        finally:
            with self.lock:
                self.capture_busy = False

    def save(self):
        with self.lock:
            draft = self._draft()
            if self.capture_busy:
                raise ValueError("Дождитесь чтения кнопки")
            if not draft["steps"]:
                raise ValueError("Сначала покажите хотя бы одну кнопку")
            row = {k: copy.deepcopy(v) for k, v in draft.items() if k != "expires"}
            row.update(created=time.time(), enabled=True)
            self.store.put("ui_actions", row, limit=30)
            self.rows[row["id"]] = row
            self.draft = None
            return self._changed(
                "Сохранила. Для запуска: «Крест, выполни действие " + row["name"] + "»."
            )

    def _find(self, name):
        key = name_key(name)
        row = next((r for r in self.rows.values() if name_key(r["name"]) == key), None)
        if row is None or not row.get("enabled"):
            raise ValueError("Действие не найдено или отключено")
        if not isinstance(row["steps"], list) or not 1 <= len(row["steps"]) <= MAX_STEPS:
            raise ValueError("Некорректный список шагов")
        steps = [validate_step(s) for s in row["steps"]]
        if any(not same_app(s["exe"], steps[0]["exe"]) for s in steps):
            raise ValueError("Шаги относятся к разным программам")
        return copy.deepcopy(row)

    def run(self, name, *, probe=False):
        if not self.operation_lock.acquire(blocking=False):
            raise ValueError("Уже выполняется обученное действие")
        try:
            with self.lock:
                if self.draft or self.capture_busy:
                    raise ValueError("Сначала сохраните или отмените обучение")
                row = self._find(name)
                self.stop.clear()
                self.playing = True
                self.current = row["id"]
                self.progress = [
                    {"label": step_label(s), "status": "pending", "text": ""} for s in row["steps"]
                ]
            token = self.runtime.command_stop
            if probe:
                result = self.native("locate", step=row["steps"][0])
                with self.lock:
                    self.progress = []
                    message = self._changed(
                        "Первый шаг доступен. Ничего не нажимала."
                        if result.get("found")
                        else "Первый шаг не найден. Откройте исходный раздел программы."
                    )
                return {
                    "success": result.get("found", False),
                    "message": message,
                }
            if self.stop.is_set() or token.is_set():
                raise ValueError("Выполнение остановлено до переключения окна")
            active = self.native("activate", step=row["steps"][0])
            expected_window = active.get("window")
            previous_window = (row["steps"][0]["window_class"], row["steps"][0]["window_title"])
            for index, step in enumerate(row["steps"]):
                window = (step["window_class"], step["window_title"])
                if window != previous_window:
                    expected_window = None  # An explicitly taught dialog transition.
                with self.lock:
                    self.progress[index]["status"] = "running"
                    self._changed(f"{row['name']}: шаг {index + 1} — {step_label(step)}")
                deadline = time.monotonic() + 8
                while True:
                    if self.stop.is_set() or token.is_set():
                        raise ValueError("Остановлено. Уже отправленные действия не отменяются")
                    # A negative lookup has no side effects. Only that lookup
                    # may be retried; timeout/uncertain invocation is never replayed.
                    result = self.native("perform", step=step, expected_window=expected_window)
                    if result.get("found"):
                        break
                    if time.monotonic() >= deadline:
                        raise ValueError(
                            "Элемент не появился: "
                            + step_label(step)
                            + ". Проверьте исходный раздел или обучите действие заново"
                        )
                    self.runtime.stop_event.wait(0.25)
                expected_window = result.get("window")
                previous_window = window
                with self.lock:
                    self.progress[index].update(status=result["status"], text=result["text"])
                if result["status"] not in {"confirmed", "reported"}:
                    raise ValueError("Результат шага не подтверждён; следующие шаги остановлены")
            with self.lock:
                text = self._changed(
                    f"Действие «{row['name']}»: отправлено шагов — {len(row['steps'])}."
                )
                self.store.put(
                    "ui_runs",
                    {
                        "name": row["name"],
                        "time": time.time(),
                        "success": True,
                        "steps": self.progress,
                    },
                    limit=50,
                )
            return {"success": True, "message": text}
        except Exception as exc:
            with self.lock:
                for step in self.progress:
                    if step["status"] == "running":
                        step.update(status="failed", text=str(exc))
                    elif step["status"] == "pending":
                        step["status"] = "not_run"
                self._changed(str(exc))
            return {"success": False, "message": str(exc)}
        finally:
            with self.lock:
                self.playing = False
                self.current = ""
            self.operation_lock.release()

    def snapshot(self):
        with self.lock:
            if self.draft and time.monotonic() > self.draft["expires"]:
                self.draft = None
            return copy.deepcopy(
                {
                    "rows": list(self.rows.values()),
                    "draft": self.draft,
                    "status": self.status,
                    "progress": self.progress,
                    "playing": self.playing,
                    "capture_busy": self.capture_busy,
                }
            )

    def names(self):
        with self.lock:
            return [row["name"] for row in self.rows.values() if row.get("enabled")]

    def call(self, action, **values):
        if action == "snapshot":
            return self.snapshot()
        if action == "begin":
            return self.begin(values["name"])
        if action == "capture":
            return self.capture()
        if action == "save":
            return self.save()
        if action == "stop":
            self.stop.set()
            return "Остановка запрошена; начатое нажатие может завершиться"
        with self.lock:
            if action == "cancel":
                self.draft = None
                return self._changed("Обучение отменено")
            if action == "undo_step":
                draft = self._draft()
                if self.capture_busy:
                    raise ValueError("Дождитесь чтения кнопки")
                if draft["steps"]:
                    draft["steps"].pop()
                return self._changed("Последний шаг убран")
            if action in {"delete", "enable"}:
                ident = values["id"]
                if ident not in self.rows or self.current == ident:
                    raise ValueError("Действие не найдено или ещё выполняется")
                if action == "delete":
                    self.store.remove("ui_actions", ident)
                    del self.rows[ident]
                else:
                    row = dict(self.rows[ident], enabled=bool(values["enabled"]))
                    self.store.put("ui_actions", row, limit=30)
                    self.rows[ident] = row
                return self._changed("Список действий обновлён")
        raise ValueError("Неизвестная операция обучения")
