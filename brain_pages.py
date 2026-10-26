"""Visible-only planner snapshots; incremental table updates on the Tk thread."""

from datetime import datetime
import time
import tkinter as tk
from action_brain import LABELS


class BrainPages:
    def build_brain_tab(self):
        card = self.card(
            self.page_bodies["brain"],
            "Мозг Крест",
            "Понимает поручение, составляет порядок действий и отслеживает результат.",
        )
        self.brain_context = tk.StringVar(value="Читаю состояние…")
        self.label(card, textvariable=self.brain_context, wraplength=720).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Обновить", lambda: self._brain_refresh(force=True)),
                ("Разобрать команду", self._brain_parser),
                ("Остановить команды", lambda: self._brain_action("stop")),
            ],
        )
        card = self.card(
            self.page_bodies["brain"],
            "Поручения с условием",
            "«Крест, после игры таймер на две минуты». При AFK доступны поручения дежурству: «когда компьютер свободен сохрани настройки в очередь». Условия проверяются автоматически.",
        )
        self.brain_goals = self._table(
            card, {"command": "Поручение", "condition": "Условие", "status": "Состояние"}, 4
        )
        self._buttons(card, [("Отменить выбранное поручение", self._brain_cancel_goal)])
        card = self.card(
            self.page_bodies["brain"],
            "Ход действий",
            "Запрос приложению и подтверждённый результат показаны отдельно. Повторно действия сами не отправляются.",
        )
        self.brain_plans = self._table(
            card, {"time": "Время", "command": "Команда", "status": "Результат"}, 6
        )
        self.brain_detail = tk.StringVar(value="Выберите команду, чтобы увидеть её шаги.")
        self.label(card, textvariable=self.brain_detail, wraplength=720).pack(fill="x", pady=10)
        self.brain_plans.bind("<<TreeviewSelect>>", lambda _: self._brain_details())
        self.brain_goals.bind("<<TreeviewSelect>>", lambda _: self._brain_details(goal=True))
        self._buttons(card, [("Остановить выбранную команду", self._brain_cancel_plan)])
        self._brain_rows = {"plans": {}, "goals": {}}
        self._brain_busy = False
        self._brain_refreshed = 0

    def _brain_parser(self):
        from command_parser import open_parser

        open_parser(self)

    def _brain_refresh(self, force=False):
        if (
            self.current_page != "brain"
            or self.state() not in {"normal", "zoomed"}
            or getattr(self, "_brain_busy", False)
        ):
            return
        if not force and time.monotonic() - getattr(self, "_brain_refreshed", 0) < 5:
            return
        if not hasattr(self, "brain_plans") or not self.brain_plans.winfo_exists():
            return
        self._brain_busy = True
        self._brain_refreshed = time.monotonic()
        tree = self.brain_plans

        def read():
            try:
                return self.runtime.brain.call("snapshot")
            except Exception as exc:
                return str(exc)

        def display(data):
            if not tree.winfo_exists() or self.brain_plans is not tree:
                return
            self._brain_busy = False
            if isinstance(data, str):
                self.brain_context.set(data)
                return
            self.brain_context.set(
                ("Игра: " + data["game"] if data["game"] else "Игры не обнаружены")
                + f" · В очереди: {data['queue']}\n"
                + data["activity"].get("reason", "")
                + "\nГолос и команды меню имеют приоритет перед отложенными поручениями."
            )
            self._brain_rows = {
                kind: {r["id"]: r for r in data[kind]} for kind in ("plans", "goals")
            }
            self._smart_rows(
                "brain_plans",
                [
                    (
                        r["id"],
                        (
                            datetime.fromtimestamp(r["created"]).strftime("%H:%M:%S"),
                            r["command"],
                            LABELS.get(r["status"], r["status"]),
                        ),
                    )
                    for r in data["plans"]
                ],
            )
            self._smart_rows(
                "brain_goals",
                [
                    (
                        r["id"],
                        (
                            r["command"],
                            "После игры" if r["condition"] == "after_game" else "При AFK",
                            LABELS.get(r["status"], r["status"]),
                        ),
                    )
                    for r in data["goals"]
                ],
            )
            for kind in ("plans", "goals"):
                target = getattr(self, "brain_" + kind)
                order = tuple(r["id"] for r in data[kind])
                if target.get_children() != order:
                    for index, ident in enumerate(order):
                        target.move(ident, "", index)
            self._brain_details(goal=getattr(self, "_brain_selected_goal", False))

        self._background(read, display)

    def _brain_details(self, goal=False):
        self._brain_selected_goal = goal
        tree = self.brain_goals if goal else self.brain_plans
        if not tree.winfo_exists():
            return
        selected = tree.selection()
        row = self._brain_rows["goals" if goal else "plans"].get(selected[0]) if selected else None
        if row:
            self.brain_detail.set(
                row["reason"]
                + "\n"
                + "\n".join(
                    f"{i}. {s['command']} — {LABELS.get(s.get('status'), 'Запланировано')}"
                    + (": " + s["result"] if s.get("result") else "")
                    for i, s in enumerate(row["steps"], 1)
                )
            )

    def _brain_action(self, action, **values):
        def work():
            try:
                return self.runtime.brain.call(action, **values)
            except Exception as exc:
                return str(exc)

        def done(text):
            if self.winfo_exists():
                self.status_var.set(str(text))
                self._brain_refresh(force=True)

        self._background(work, done)

    def _brain_cancel_goal(self):
        if selected := self.brain_goals.selection():
            self._brain_action("cancel", id=selected[0])

    def _brain_cancel_plan(self):
        if selected := self.brain_plans.selection():
            self._background(
                lambda: self.runtime.cancel_job(selected[0]),
                lambda text: self.status_var.set(str(text)),
            )
