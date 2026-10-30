"""Lazy recipe editor; updates only while this page is visible."""

from pathlib import PureWindowsPath
import time
import tkinter as tk
from tkinter import ttk
from learned_model import step_label
from action_brain import LABELS


class LearnedPages:
    def build_learned_tab(self):
        parent = self.page_bodies["learned"]
        card = self.card(
            parent,
            "Покажи один раз",
            "Научите Крест кнопкам своей программы. Без кода, записи экрана и постоянного наблюдения.",
        )
        self.learned_status = tk.StringVar(value="Загружаю действия…")
        self.label(card, textvariable=self.learned_status, wraplength=730).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                (
                    "Научить новому действию",
                    lambda: self._show_learned_editor(
                        not getattr(self, "_learned_editor_open", False)
                    ),
                ),
                ("Остановить действие", lambda: self._learned_call("stop")),
            ],
        )
        self.learned_editor = tk.Frame(card, bg=card.cget("bg"))
        card = self.learned_editor
        self._show_learned_editor(getattr(self, "_learned_editor_open", False))
        self._learned_was_draft = False
        self.label(
            card,
            "1. Назовите действие.  2. Откройте программу.\n3. Наведите на кнопку и нажмите Ctrl+Alt+F9 или скажите «Крест, запомни кнопку». Затем нажмите её вручную и покажите следующий шаг.\n4. Скажите «Крест, сохрани действие».",
            muted=True,
            wraplength=730,
        ).pack(fill="x", pady=6)
        self.learned_name = tk.StringVar(value=getattr(self, "_learned_name_draft", ""))
        self.learned_name.trace_add(
            "write", lambda *_: setattr(self, "_learned_name_draft", self.learned_name.get())
        )
        entry = ttk.Entry(card, textvariable=self.learned_name)
        entry.pack(fill="x", pady=8)
        from entry_clipboard import attach

        attach(entry, self.theme)
        self._buttons(
            card,
            [
                (
                    "Начать обучение",
                    lambda: self._learned_call("begin", name=self.learned_name.get()),
                ),
                ("Убрать последний шаг", lambda: self._learned_call("undo_step")),
                ("Сохранить действие", lambda: self._learned_call("save")),
                ("Отменить обучение", lambda: self._learned_call("cancel")),
            ],
        )
        self.learned_draft = tk.StringVar()
        self.label(card, textvariable=self.learned_draft, wraplength=730).pack(fill="x", pady=6)
        card = self.card(
            parent,
            "Мои действия",
            "Откройте программу в исходном разделе. Выберите действие или назовите его голосом — Крест выполнит сохранённые шаги.",
        )
        self.learned_tree = self._table(
            card,
            {"name": "Название", "program": "Программа", "steps": "Шаги", "state": "Состояние"},
            5,
        )
        self.learned_details = tk.StringVar(value="Выберите действие")
        self.learned_tree.bind("<<TreeviewSelect>>", lambda _: self._learned_details())
        self.label(card, textvariable=self.learned_details, wraplength=730).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Выполнить", lambda: self._learned_run(False)),
                ("Проверить первый шаг", lambda: self._learned_run(True)),
                ("Обновить", lambda: self._learned_refresh(force=True)),
                ("Включить / выключить", self._learned_toggle),
                ("Удалить действие", self._learned_delete),
            ],
        )
        card = self.card(
            parent,
            "Последний запуск",
            "Для вкладок и переключателей доступно подтверждение состояния. Для обычной кнопки отображается факт отправки нажатия; изменение интерфейса или неоднозначная цель останавливают следующие шаги.",
        )
        self.learned_progress = tk.StringVar(value="Запусков пока нет")
        self.label(card, textvariable=self.learned_progress, wraplength=730).pack(fill="x", pady=6)
        self._learned_data = {}
        self._learned_busy = False
        self._learned_at = 0

    def _show_learned_editor(self, opened):
        self._learned_editor_open = opened
        if opened:
            self.learned_editor.pack(fill="x")
        else:
            self.learned_editor.pack_forget()

    def _learned_call(self, action, **values):
        def work():
            try:
                return self.runtime.learned.call(action, **values)
            except Exception as exc:
                return str(exc)

        def done(result):
            if self.winfo_exists():
                self.status_var.set(str(result))
                self._learned_refresh(force=True)

        self._background(work, done)

    def _learned_selected(self):
        selection = self.learned_tree.selection()
        return next(
            (
                r
                for r in self._learned_data.get("rows", [])
                if selection and r["id"] == selection[0]
            ),
            None,
        )

    def _learned_details(self):
        row = self._learned_selected()
        self.learned_details.set(
            "Голосом: «Крест, выполни действие "
            + row["name"]
            + "»\n"
            + "\n".join(f"{i}. {step_label(s)}" for i, s in enumerate(row["steps"], 1))
            if row
            else "Выберите действие"
        )

    def _learned_run(self, probe):
        if row := self._learned_selected():
            command = ("проверь действие " if probe else "выполни действие ") + row["name"]
            self._background(lambda: self.runtime.submit(command))

    def _learned_toggle(self):
        if row := self._learned_selected():
            self._learned_call("enable", id=row["id"], enabled=not row.get("enabled"))

    def _learned_delete(self):
        if row := self._learned_selected():
            self._learned_call("delete", id=row["id"])

    def _learned_refresh(self, force=False):
        if (
            self.current_page != "learned"
            or self.state() not in {"normal", "zoomed"}
            or getattr(self, "_learned_busy", False)
        ):
            return
        if not force and time.monotonic() - getattr(self, "_learned_at", 0) < 2:
            return
        if not hasattr(self, "learned_tree") or not self.learned_tree.winfo_exists():
            return
        tree = self.learned_tree
        self._learned_busy = True
        self._learned_at = time.monotonic()

        def read():
            try:
                return self.runtime.learned.call("snapshot")
            except Exception as exc:
                return str(exc)

        def show(data):
            if not tree.winfo_exists() or self.learned_tree is not tree:
                return
            self._learned_busy = False
            if isinstance(data, str):
                self.learned_status.set(data)
                return
            self._learned_data = data
            self.learned_status.set(data["status"])
            draft = data.get("draft")
            if draft:
                self._show_learned_editor(True)
            elif self._learned_was_draft:
                self._show_learned_editor(False)
            self._learned_was_draft = bool(draft)
            self.learned_draft.set(
                (
                    "Черновик: "
                    + draft["name"]
                    + "\n"
                    + "\n".join(f"{i}. {step_label(s)}" for i, s in enumerate(draft["steps"], 1))
                )
                if draft
                else "Обучение не запущено"
            )
            self._smart_rows(
                "learned_tree",
                [
                    (
                        r["id"],
                        (
                            r["name"],
                            PureWindowsPath(r["steps"][0]["exe"]).name,
                            len(r["steps"]),
                            "Готово" if r.get("enabled") else "Выключено",
                        ),
                    )
                    for r in data["rows"]
                ],
            )
            self._learned_details()
            self.learned_progress.set(
                "\n".join(
                    f"{i}. {s['label']} — "
                    + (
                        "Нажатие отправлено"
                        if s["status"] == "reported"
                        else LABELS.get(s["status"], s["status"])
                    )
                    + (": " + s["text"] if s["text"] else "")
                    for i, s in enumerate(data["progress"], 1)
                )
                or "Запусков пока нет"
            )

        self._background(read, show)
