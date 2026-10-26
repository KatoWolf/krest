"""Deterministic action planner and one-shot conditions, sharing existing workers."""

from pathlib import Path
import threading
import time
import uuid

from action_contracts import contract
from brain_store import BrainStore

LABELS = {
    "waiting": "Жду условия",
    "queued": "В очереди",
    "running": "Выполняю",
    "verifying": "Проверяю результат",
    "confirmed": "Подтверждено",
    "reported": "Навык сообщил об успехе",
    "completed": "Завершено",
    "failed": "Ошибка",
    "cancelled": "Остановлено",
    "interrupted": "Прервано",
    "unconfirmed": "Результат не подтверждён",
    "superseded": "Заменено новым действием",
    "dispatching": "Передаю на выполнение",
    "pending": "Ожидает",
    "not_run": "Не запускался",
}
INSPECTION_SKILLS = {"skills.brain.explain", "skills.brain.status", "skills.brain.preview"}


class ActionBrain:
    def __init__(self, runtime, root):
        self.runtime = runtime
        self.store = BrainStore(Path(root) / "action_brain.sqlite3")
        self.lock = threading.RLock()
        self.active = {}
        self.goals = {}
        self.last_tick = 0
        self.last_plan = ""
        for kind in ("plans", "goals"):
            for row in self.store.rows(kind, active=True):
                if kind == "goals" and row["status"] == "waiting":
                    row["armed"] = False
                    if row["condition"] == "after_game":
                        row["reason"] = "После перезапуска жду новую игру и её завершение"
                    self.goals[row["id"]] = row
                else:
                    row.update(
                        status="interrupted",
                        reason="Крест перезапущена. Проверьте результат; автоматического повтора нет.",
                    )
                    for step in row.get("steps", []):
                        if step.get("status") in {"running", "verifying"}:
                            step["status"] = "interrupted"
                        elif step.get("status") == "pending":
                            step["status"] = "not_run"
                self.store.put(kind, row)

    def preview(self, text):
        from command_parser import parse_command
        from command_sequence import parameter_error

        rt = self.runtime
        parsed = parse_command(rt.manager, text, rt.settings())
        if parsed["availability"] != "Доступен":
            raise ValueError(parsed["availability"])
        steps = parsed.get("steps") or [{"command": parsed["normalized"], "skill": parsed["skill"]}]
        result = []
        for step in steps:
            found = rt.manager.match(step["command"])
            if not found or found[0].id != step["skill"]:
                raise ValueError("Команда изменилась; назовите её заново")
            entry, params = found
            if problem := parameter_error(entry, params, step["command"]):
                raise ValueError(problem)
            result.append(
                dict(
                    command=step["command"],
                    skill=entry.id,
                    description=entry.description,
                    **contract(entry),
                )
            )
        return {"steps": result, "game": rt.performance.game, "profile": rt.performance.active}

    def start(self, ident, text, source, queue_ms):
        with self.lock:
            row = dict(
                id=ident,
                command=text[:2000],
                source=source,
                status="running",
                steps=[],
                created=time.time(),
                game=self.runtime.performance.game,
                profile=self.runtime.performance.active,
                queue_ms=queue_ms,
                reason="Разбираю команду",
            )
            self.active[ident] = row
            self.store.put("plans", row)

    def plan(self, ident, steps):
        with self.lock:
            row = self.active.get(ident)
            if row is None:
                return
            row["steps"] = [
                dict(step, status="pending", result="", verification_id="") for step in steps
            ]
            row["reason"] = "План составлен; доступность проверяется перед каждым действием"
            self.store.put("plans", row)

    def step(self, ident, index, *, result=None, verification=None):
        with self.lock:
            row = self.active.get(ident)
            if not row or index >= len(row["steps"]):
                return
            step = row["steps"][index]
            if result is None and verification is None:
                step["status"] = "running"
                row["reason"] = f"Шаг {index + 1}: {step['command']}"
            elif verification is not None:
                step.update(status=verification["status"], result=verification["text"])
            else:
                state = result.verification or ("reported" if result.success else "failed")
                step.update(
                    status="verifying" if state == "pending" else state,
                    result=result.text[:2000],
                    verification_id=result.verification_id,
                )
            self.store.put("plans", row)

    def finish(self, ident, result, *, status=None):
        with self.lock:
            row = self.active.get(ident)
            if row is None:
                return
            row.update(
                status=status or ("completed" if result.success else "failed"),
                reason=result.text[:2000],
                finished=time.time(),
            )
            if row["status"] == "completed" and any(
                s["status"] == "verifying" for s in row["steps"]
            ):
                row["status"] = "verifying"
            elif row["status"] == "completed" and any(
                s["status"] in {"unconfirmed", "superseded"} for s in row["steps"]
            ):
                row["status"] = "unconfirmed"
            if row["status"] != "verifying":
                for step in row["steps"]:
                    if step["status"] == "pending":
                        step["status"] = "not_run"
                    elif step["status"] == "running":
                        step.update(status=row["status"], result=row["reason"])
                    elif step["status"] == "verifying":
                        step.update(status="unconfirmed", result="Ожидание результата прервано")
                self.active.pop(ident, None)
            self.store.put("plans", row)
            if result.skill_id not in INSPECTION_SKILLS and result.handled:
                self.last_plan = ident

    def expired(self, text, source, elapsed):
        if source != "voice" or elapsed < 10:
            return False
        try:
            return any(step["transient"] for step in self.preview(text)["steps"])
        except ValueError:
            return False

    def add_goal(self, text, condition):
        if condition not in {"after_game", "idle"}:
            raise ValueError("Условие: после игры или когда компьютер свободен")
        plan = self.preview(text)
        if any(not step["idle" if condition == "idle" else "deferred"] for step in plan["steps"]):
            raise ValueError(
                "Такое действие требует команды в момент выполнения. После игры доступны запуск программ, таймеры, напоминания и погода; при AFK — поручения дежурству."
            )
        rt = self.runtime
        if condition == "after_game" and not rt.performance.game:
            raise ValueError("Игра сейчас не обнаружена. Дайте это поручение во время игры.")
        with self.lock:
            if len(self.goals) >= 30:
                raise ValueError("Уже ожидают 30 поручений; отмените ненужные")
            if any(
                g["condition"] == condition and g["steps"] == plan["steps"]
                for g in self.goals.values()
            ):
                return "Такое поручение уже ожидает своего условия."
            row = dict(
                id=uuid.uuid4().hex,
                command=text,
                steps=plan["steps"],
                condition=condition,
                status="waiting",
                armed=bool(rt.performance.game),
                game=rt.performance.game,
                created=time.time(),
                reason="Жду завершения игры"
                if condition == "after_game"
                else "Жду подтверждённого AFK",
            )
            self.store.put("goals", row)
            self.goals[row["id"]] = row
        return "Поручение сохранено: " + row["reason"].lower() + ", затем «" + text + "»."

    def _valid_goal(self, row):
        current = self.preview(row["command"])["steps"]
        if [(s["command"], s["skill"]) for s in current] != [
            (s["command"], s["skill"]) for s in row["steps"]
        ]:
            raise ValueError("Команды поручения изменились. Создайте поручение заново.")

    def guard(self, source):
        if not source.startswith("brain:"):
            return ""
        with self.lock:
            row = self.goals.get(source[6:])
            if not row or row["status"] not in {"dispatching", "queued", "running"}:
                return "Поручение отменено или уже завершено"
            self._valid_goal(row)
            if row["condition"] == "after_game" and self.runtime.performance.game:
                return "Началась новая игра; поручение остановлено"
            if row["condition"] == "idle" and not self.runtime.autonomy.permitted():
                return "Компьютер снова используется; поручение остановлено"
            if row["status"] != "running":
                row.update(
                    status="running", reason="Условие повторно проверено; выполняю поручение"
                )
                self.store.put("goals", row)
        return ""

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last_tick < 3:
            return
        self.last_tick = now
        with self.lock:
            for ident, row in list(self.active.items()):
                if row["status"] != "verifying":
                    continue
                changed = False
                for step in row["steps"]:
                    if step["status"] == "verifying":
                        value = self.runtime.verifier.get(step["verification_id"])
                        if value is None:
                            value = {
                                "status": "unconfirmed",
                                "text": "Проверка результата больше недоступна",
                            }
                        if value["status"] != "pending":
                            step.update(status=value["status"], result=value["text"])
                            changed = True
                if changed:
                    if not any(s["status"] == "verifying" for s in row["steps"]):
                        row["status"] = (
                            "unconfirmed"
                            if any(
                                s["status"] in {"unconfirmed", "superseded"} for s in row["steps"]
                            )
                            else "completed"
                        )
                        row["reason"] = "\n".join(
                            f"{s['command']}: {s['result']}" for s in row["steps"]
                        )[:2000]
                        self.active.pop(ident, None)
                    self.store.put("plans", row)
            for ident, row in list(self.goals.items()):
                if row["status"] in {"queued", "running"}:
                    job = self.runtime.jobs.by_request("brain:" + ident)
                    if job and job["status"] in self.runtime.jobs.TERMINAL:
                        plan = self.store.get("plans", job["id"])
                        if plan and plan["status"] == "verifying":
                            continue
                        row.update(
                            status=plan["status"] if plan else job["status"],
                            reason=plan["reason"] if plan else job["result"],
                        )
                        self.store.put("goals", row)
                        self.goals.pop(ident, None)
                    continue
                if row["status"] != "waiting":
                    continue
                if row["condition"] == "after_game":
                    if self.runtime.performance.game:
                        if not row["armed"]:
                            row["armed"] = True
                            self.store.put("goals", row)
                        continue
                    ready = row["armed"]
                else:
                    ready = (
                        self.runtime.autonomy.gate.snapshot().get("allowed", False)
                        and not self.runtime.autonomy.paused
                    )
                if not ready or self.runtime.stop_event.is_set():
                    continue
                try:
                    self._valid_goal(row)
                    row["status"] = "dispatching"
                    self.store.put("goals", row)
                    submitted = self.runtime.submit(
                        row["command"], source="brain:" + ident, request_key="brain:" + ident
                    )
                    row["status"] = "queued" if submitted else "waiting"
                    row["reason"] = (
                        "Условие выполнено; ожидаю очереди"
                        if submitted
                        else "Очередь занята; жду свободного места"
                    )
                except Exception as exc:
                    row.update(status="interrupted", reason=str(exc)[:1000])
                    self.goals.pop(ident, None)
                self.store.put("goals", row)

    def cancel_goal(self, ident):
        with self.lock:
            row = self.goals.get(ident)
            if not row:
                raise ValueError("Поручение уже завершено")
            row.update(status="cancelled", reason="Остановлено пользователем")
            self.store.put("goals", row)
            self.goals.pop(ident, None)
        job = self.runtime.jobs.by_request("brain:" + ident)
        if job:
            self.runtime.cancel_job(job["id"])
        return "Поручение остановлено. Уже отправленные действия сохраняются."

    def snapshot(self):
        plans = self.store.rows("plans")
        from brain_store import ACTIVE

        plans.sort(key=lambda row: row["status"] not in ACTIVE)
        return {
            "plans": plans[:40],
            "goals": self.store.rows("goals"),
            "game": self.runtime.performance.game,
            "profile": self.runtime.performance.active,
            "activity": self.runtime.autonomy.gate.snapshot(),
            "queue": self.runtime.command_status()[1],
        }

    def explain(self):
        row = (
            self.store.get("plans", self.last_plan)
            if self.last_plan
            else next(
                (
                    row
                    for row in self.store.rows("plans")
                    if not any(step.get("skill") in INSPECTION_SKILLS for step in row["steps"])
                ),
                None,
            )
        )
        if not row:
            return "Выполненных планов пока нет."
        return (
            "Команда: "
            + row["command"]
            + ". "
            + LABELS.get(row["status"], row["status"])
            + ". "
            + row["reason"]
            + "\n"
            + "\n".join(
                f"{i}. {s['command']} — {LABELS.get(s['status'], s['status'])}: {s['result']}"
                for i, s in enumerate(row["steps"], 1)
            )
        )

    def call(self, action, **values):
        if action == "snapshot":
            return self.snapshot()
        if action == "preview":
            return self.preview(str(values["text"])[:2000])
        if action == "explain":
            return self.explain()
        if action == "add":
            return self.add_goal(str(values["text"])[:2000], values["condition"])
        if action == "cancel":
            return self.cancel_goal(values["id"])
        if action == "stop":
            count = self.runtime.clear_pending()
            current = getattr(self.runtime._job_local, "plan_id", None)
            with self.runtime._jobs_lock:
                for ident, token in self.runtime._active_jobs.items():
                    if ident != current:
                        token.set()
                        count += 1
            self.runtime.scenarios.cancel()
            return (
                f"Остановка запрошена для {count} команд. Уже отправленные действия не отменяются."
            )
        raise ValueError("Неизвестное действие диспетчера")
