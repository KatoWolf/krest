"""Scenario editor, grouped inbox, and background-work controls."""

import copy
import tkinter as tk
from tkinter import ttk, filedialog
from ui_dialogs import messagebox
import time

ACTIONS = {
    "Команда": "command",
    "Программа": "app",
    "Пауза, секунды": "wait",
    "Профиль нагрузки": "profile",
    "Уведомления": "notifications",
    "Ждать процесс": "wait_process",
    "Ждать окно": "wait_window",
    "Сообщение": "notice",
    "Запустить сервис": "service",
}
MODES = {"Автоматически": "auto", "Обычный": "balanced", "Игра": "game", "Экономный": "economy"}
NOTICE_MODES = {"Все": "all", "Только важные": "important", "Без озвучки": "silent"}


class WorkflowPages:
    def init_workflows(self):
        cfg = self.config_mgr.snapshot()
        self.scenario_drafts = copy.deepcopy(cfg["scenarios"])
        self.scenario_name = tk.StringVar(value="Игра")
        self.scenario_steps = copy.deepcopy(self.scenario_drafts.get("Игра", []))
        self.step_action = tk.StringVar(value="Команда")
        self.step_action.trace_add("write", lambda *args: self._step_choices())
        self.step_value = tk.StringVar()
        self.scenario_status = tk.StringVar()
        self.profile_var = tk.StringVar(
            value=next(k for k, v in MODES.items() if v == cfg["performance"]["mode"])
        )
        self.game_processes_var = tk.StringVar(value=cfg["performance"]["game_processes"])
        self.notice_mode_var = tk.StringVar(
            value=next(
                k for k, v in NOTICE_MODES.items() if v == cfg["notification_settings"]["mode"]
            )
        )
        self.game_notices_var = tk.BooleanVar(
            value=cfg["notification_settings"]["game_important_only"]
        )
        self.sender_notices_var = tk.BooleanVar(
            value=cfg["notification_settings"]["announce_senders"]
        )
        self.notice_unread = tk.BooleanVar(value=False)
        self.resource_var = tk.StringVar(value="Ожидаю первый замер")
        self.sort_status = tk.StringVar(
            value="Предпросмотр не перемещает файлы. Отмена возвращает последнюю партию."
        )
        self.sort_busy = False
        self.weekdays = [tk.BooleanVar(value=True) for _ in range(7)]
        self.snooze_delay = tk.StringVar(value="5 минут")
        self._inbox_rows = None

    def _notice_action(self, action):
        selected = self.inbox_tree.selection()
        if not selected:
            self._report_error("Выберите сообщение в списке уведомлений")
            return
        ident, text = int(selected[0]), self.notice_reply.get()

        def invoke(what):
            if hasattr(self.runtime, "client"):
                return self.runtime.client.call("conversation", action=what, ident=ident, text=text)
            return self.runtime.conversations.call(what, ident, text)

        def done(message):
            self.status_var.set(message)
            if action == "draft":
                if messagebox.askyesno("Отправить ответ?", message, parent=self):
                    self._background(lambda: invoke("confirm"), done=self.status_var.set)
                else:
                    self._background(lambda: invoke("cancel"), done=self.status_var.set)

        self._background(lambda: invoke(action), done=done)

    def build_workflows(self):
        card = self.card(
            self.page_bodies["scenarios"],
            "Сценарии",
            "Шаги выполняются по порядку. При ошибке оставшиеся шаги останавливаются. Начатое действие не откатывается автоматически.",
        )
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        self.scenario_combo = ttk.Combobox(
            row, textvariable=self.scenario_name, values=list(self.scenario_drafts)
        )
        self.scenario_combo.pack(side="left", fill="x", expand=True)
        self.scenario_combo.bind("<<ComboboxSelected>>", lambda e: self._load_scenario())
        ttk.Button(row, text="Новый", command=self._new_scenario).pack(side="left", padx=6)
        self.scenario_tree = ttk.Treeview(
            card, columns=("action", "value"), show="headings", height=5
        )
        self.scenario_tree.heading("action", text="Действие")
        self.scenario_tree.heading("value", text="Значение")
        self.scenario_tree.column("action", width=160)
        self.scenario_tree.column("value", width=450)
        self.scenario_tree.pack(fill="x", pady=10)
        self.scenario_tree.bind("<<TreeviewSelect>>", lambda e: self._edit_scenario_step())
        ttk.Combobox(
            card, textvariable=self.step_action, values=list(ACTIONS), state="readonly"
        ).pack(fill="x")
        self.label(card, "Значение шага", muted=True).pack(anchor="w", pady=(10, 4))
        self.step_value_entry = ttk.Combobox(card, textvariable=self.step_value)
        self.step_value_entry.pack(fill="x")
        self._step_choices()
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=6)
        for title, fn in [
            ("Добавить", self._add_scenario_step),
            ("Заменить", lambda: self._add_scenario_step(True)),
            ("Выбрать программу", self._scenario_program),
            ("Удалить шаг", self._remove_scenario_step),
        ]:
            ttk.Button(row, text=title, command=fn).pack(side="left", padx=(0, 5))
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=6)
        for title, fn in [
            ("↑", lambda: self._move_step(-1)),
            ("↓", lambda: self._move_step(1)),
            ("Сохранить сценарий", self._save_scenario),
            ("Удалить сценарий", self._delete_scenario),
        ]:
            ttk.Button(row, text=title, command=fn).pack(side="left", padx=(0, 5))
        self.label(
            card,
            "Добавляйте команды, например «громкость на 30», программы и паузы. Для профиля и уведомлений выберите режим из списка. Сначала сохраните сценарий, затем запустите его.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=10)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")
        ttk.Button(
            row, text="Запустить сохранённый", style="Accent.TButton", command=self._run_scenario
        ).pack(side="left")
        ttk.Button(row, text="Остановить", command=self.runtime.scenarios.cancel).pack(
            side="left", padx=6
        )
        self.label(card, textvariable=self.scenario_status, wraplength=680).pack(
            anchor="w", pady=10
        )
        self._draw_scenario()

    def _draw_scenario(self):
        self.scenario_tree.delete(*self.scenario_tree.get_children())
        for i, step in enumerate(self.scenario_steps):
            label = next(k for k, v in ACTIONS.items() if v == step["action"])
            choices = (
                MODES
                if step["action"] == "profile"
                else NOTICE_MODES
                if step["action"] == "notifications"
                else {}
            )
            value = next((k for k, v in choices.items() if v == step["value"]), step["value"])
            self.scenario_tree.insert("", "end", iid=str(i), values=(label, value))

    def _step_choices(self):
        if not hasattr(self, "step_value_entry") or not self.step_value_entry.winfo_exists():
            return
        choices = (
            MODES
            if self.step_action.get() == "Профиль нагрузки"
            else NOTICE_MODES
            if self.step_action.get() == "Уведомления"
            else {}
        )
        self.step_value_entry.configure(
            values=list(choices), state="readonly" if choices else "normal"
        )
        if choices and self.step_value.get() not in choices:
            self.step_value.set(next(iter(choices)))

    def _load_scenario(self):
        self.scenario_steps = copy.deepcopy(self.scenario_drafts.get(self.scenario_name.get(), []))
        self._draw_scenario()

    def _new_scenario(self):
        self.scenario_name.set("Новый сценарий")
        self.scenario_steps = []
        self._draw_scenario()

    def _edit_scenario_step(self):
        selected = self.scenario_tree.selection()
        if selected:
            step = self.scenario_steps[int(selected[0])]
            self.step_action.set(next(k for k, v in ACTIONS.items() if v == step["action"]))
            choices = (
                MODES
                if step["action"] == "profile"
                else NOTICE_MODES
                if step["action"] == "notifications"
                else {}
            )
            self.step_value.set(
                next((k for k, v in choices.items() if v == step["value"]), step["value"])
            )
            if hasattr(self, "condition_kind"):
                condition = step.get("condition", {})
                self.condition_kind.set(
                    {
                        "process": "Процесс",
                        "file": "Файл",
                        "game": "Игра",
                        "weekday": "Дни недели",
                    }.get(condition.get("kind"), "Всегда")
                )
                self.condition_value.set(condition.get("value", ""))
                self.condition_invert.set(condition.get("invert", False))
                self.step_temporary.set(step.get("temporary", False))
                self.step_timeout.set(str(step.get("timeout", 30)))

    def _add_scenario_step(self, replace=False):
        step = {"action": ACTIONS[self.step_action.get()], "value": self.step_value.get().strip()}
        if step["action"] == "profile":
            step["value"] = MODES.get(step["value"], step["value"])
        elif step["action"] == "notifications":
            step["value"] = NOTICE_MODES.get(step["value"], step["value"])
        from scenarios import validate_scenarios

        try:
            validate_scenarios({"Шаг": [step]})
        except ValueError as exc:
            self._report_error(str(exc))
            return
        selected = self.scenario_tree.selection()
        if replace and selected:
            self.scenario_steps[int(selected[0])] = step
        elif not replace:
            self.scenario_steps.append(step)
        self._draw_scenario()

    def _scenario_program(self):
        path = filedialog.askopenfilename(parent=self, title="Программа для сценария")
        if path:
            self.step_action.set("Программа")
            self.step_value.set(path)

    def _remove_scenario_step(self):
        for i in sorted(map(int, self.scenario_tree.selection()), reverse=True):
            self.scenario_steps.pop(i)
        self._draw_scenario()

    def _move_step(self, direction):
        selected = self.scenario_tree.selection()
        if selected:
            i = int(selected[0])
            j = i + direction
            if 0 <= j < len(self.scenario_steps):
                self.scenario_steps[i], self.scenario_steps[j] = (
                    self.scenario_steps[j],
                    self.scenario_steps[i],
                )
                self._draw_scenario()
                self.scenario_tree.selection_set(str(j))

    def _save_scenario(self):
        from scenarios import validate_scenarios

        name = self.scenario_name.get().strip()
        values = copy.deepcopy(self.scenario_drafts)
        values[name] = copy.deepcopy(self.scenario_steps)
        try:
            validate_scenarios(values)
        except ValueError as exc:
            self._report_error(str(exc))
            return
        self.scenario_drafts = values
        self._mark_dirty()
        if self.save_all_settings(quiet=True):
            self.scenario_combo.configure(values=list(values))
            self.status_var.set("Сценарий сохранён")

    def _delete_scenario(self):
        self.scenario_drafts.pop(self.scenario_name.get(), None)
        self._mark_dirty()
        self.save_all_settings(quiet=True)
        self.scenario_combo.configure(values=list(self.scenario_drafts))
        self.scenario_steps = []
        self._draw_scenario()

    def _run_scenario(self):
        try:
            self.runtime.scenarios.start(self.scenario_name.get())
        except ValueError as exc:
            self._report_error(str(exc))

    def _build_inbox(self):
        card = self.card(
            self.page_bodies["inbox"],
            "Центр уведомлений",
            "Повторы за минуту объединяются. Прочтение уведомления не отмечает задачу выполненной.",
        )
        ttk.Combobox(
            card, textvariable=self.notice_mode_var, values=list(NOTICE_MODES), state="readonly"
        ).pack(fill="x", pady=5)
        ttk.Checkbutton(
            card, text="В игре озвучивать только важные", variable=self.game_notices_var
        ).pack(anchor="w")
        ttk.Checkbutton(
            card,
            text="Коротко называть отправителя сообщения, в том числе в игре",
            variable=self.sender_notices_var,
        ).pack(anchor="w")
        self.label(
            card,
            "«Крест, кто написал?» — сводка пропущенных уведомлений. Содержимое переписки не зачитывается.",
            muted=True,
            wraplength=660,
        ).pack(anchor="w", pady=8)
        ttk.Button(card, text="Применить режим уведомлений", command=self._apply_notices).pack(
            anchor="w", pady=8
        )
        ttk.Checkbutton(
            card,
            text="Только непрочитанные",
            variable=self.notice_unread,
            command=self._refresh_inbox,
        ).pack(anchor="w")
        self.inbox_tree = ttk.Treeview(
            card,
            columns=("time", "source", "priority", "count", "text"),
            show="headings",
            height=10,
        )
        for column, title, width in [
            ("time", "Время", 90),
            ("source", "Источник", 100),
            ("priority", "Важность", 75),
            ("count", "×", 35),
            ("text", "Уведомление", 370),
        ]:
            self.inbox_tree.heading(column, text=title)
            self.inbox_tree.column(column, width=width)
        self.inbox_tree.pack(fill="x", pady=10)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")
        ttk.Button(row, text="Прочитано", command=self._read_notice).pack(side="left")
        ttk.Button(row, text="Прочитать всё", command=lambda: self._read_notice(True)).pack(
            side="left", padx=6
        )
        self.notice_details = tk.StringVar()
        self.label(card, textvariable=self.notice_details, wraplength=690).pack(anchor="w", pady=10)
        self.inbox_tree.bind("<<TreeviewSelect>>", lambda e: self._notice_details())
        self._inbox_rows = None

        actions = self.card(
            self.page_bodies["inbox"],
            "Ответить и вернуться позже",
            "Выберите уведомление выше. Перед отправкой проверьте адресата и текст.",
        )
        self.notice_reply = tk.StringVar()
        self.field(actions, "Мой ответ", self.notice_reply)
        row = tk.Frame(actions, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        for caption, action in (
            ("Подготовить ответ", "draft"),
            ("Напомнить после игры", "after_game"),
            ("Тишина на час", "mute"),
        ):
            ttk.Button(row, text=caption, command=lambda a=action: self._notice_action(a)).pack(
                side="left", padx=(0, 6)
            )

    def _refresh_inbox(self):
        rows = self.runtime.center.list(self.notice_unread.get())
        if rows == self._inbox_rows:
            return
        self._inbox_rows = rows
        selected = self.inbox_tree.selection()
        self.inbox_tree.delete(*self.inbox_tree.get_children())
        for row in rows:
            self.inbox_tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    time.strftime("%d.%m %H:%M", time.localtime(row["updated"])),
                    row["source"],
                    {"low": "Низкая", "normal": "Обычная", "high": "Важное"}[row["priority"]],
                    row["count"],
                    ("● " if row["unread"] else "") + row["text"],
                ),
            )
        if selected and self.inbox_tree.exists(selected[0]):
            self.inbox_tree.selection_set(selected[0])

    def _notice_details(self):
        selected = self.inbox_tree.selection()
        if selected:
            row = next((v for v in self._inbox_rows or [] if str(v["id"]) == selected[0]), None)
            if row:
                self.notice_details.set(row["text"])

    def _read_notice(self, all_rows=False):
        selected = self.inbox_tree.selection()
        if all_rows:
            self.runtime.center.mark_read()
        elif selected:
            self.runtime.center.mark_read(int(selected[0]))
        self._refresh_inbox()

    def _build_maintenance(self):
        card = self.card(
            self.page_bodies["maintenance"],
            "Нагрузка и фоновые задачи",
            "Замеры включают Крест и её дочерние процессы. CPU указан как доля всей мощности процессора; память — сумма RSS.",
        )
        ttk.Combobox(
            card, textvariable=self.profile_var, values=list(MODES), state="readonly"
        ).pack(fill="x")
        self.field(card, "Процессы игр через запятую", self.game_processes_var)
        ttk.Button(card, text="Применить профиль", command=self._apply_profile).pack(
            anchor="w", pady=8
        )
        self.label(card, textvariable=self.resource_var, wraplength=680).pack(anchor="w")
        self.label(
            card,
            "В режимах «Игра» и «Экономный» автосортировка приостановлена; замеры и опрос Steam/Discord Presence выполняются реже. Распознавание речи и сроки напоминаний сохраняются.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=10)
        card = self.card(
            self.page_bodies["maintenance"],
            "Загрузки: просмотр и отмена",
            "Журнал хранится в папке загрузок. Изменённые файлы и занятые имена при отмене пропускаются. Ручная сортировка выполняется и в игровом режиме.",
        )
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        for title, action in [
            ("Предпросмотр", "preview"),
            ("Сортировать", "sort"),
            ("Журнал", "history"),
            ("Отменить последнюю", "undo"),
        ]:
            ttk.Button(row, text=title, command=lambda a=action: self._sort_action(a)).pack(
                side="left", padx=(0, 5)
            )
        self.label(card, textvariable=self.sort_status, wraplength=680).pack(anchor="w", pady=8)
        self.sort_tree = ttk.Treeview(
            card, columns=("source", "target", "state"), show="headings", height=10
        )
        for key, title in [("source", "Файл"), ("target", "Куда"), ("state", "Состояние")]:
            self.sort_tree.heading(key, text=title)
        self.sort_tree.pack(fill="x")

    def _apply_profile(self):
        if self.save_all_settings(quiet=True):
            mode = MODES[self.profile_var.get()]
            self._background(lambda: self.runtime.performance.set_mode(mode))

    def _apply_notices(self):
        if self.save_all_settings(quiet=True):
            self.runtime.notification_mode = None

    def _sort_action(self, action):
        if self.sort_busy:
            return
        if not self.save_all_settings(quiet=True):
            return
        folder = self.config_mgr.snapshot()["app_settings"]["downloads_path"]
        self.sort_busy = True
        self.sort_status.set("Выполняю…")

        def work():
            from downloads import DownloadSorter
            from sort_history import SortHistory
            from pathlib import Path

            try:
                if action == "preview":
                    result = DownloadSorter(self.config_mgr.snapshot().get("file_policy", {})).scan(
                        folder, automatic=False, preview=True, stop=self.runtime.stop_event
                    )
                    rows = [
                        (Path(v["source"]).name, v["category"], "Будет перемещён")
                        for v in result["preview"]
                    ]
                    message = (
                        f"К перемещению: {len(rows)}; пока не готовы: {result['skipped_count']}"
                    )
                elif action == "sort":
                    result = DownloadSorter(self.config_mgr.snapshot().get("file_policy", {})).scan(
                        folder, automatic=False, stop=self.runtime.stop_event
                    )
                    rows = []
                    message = f"Перемещено: {result['moved_count']}"
                elif action == "undo":
                    result = SortHistory(folder).undo_last(self.runtime.stop_event)
                    rows = []
                    message = f"Возвращено: {result['restored']}"
                else:
                    result = {}
                    rows = [
                        (
                            Path(v["source"]).name,
                            v["target"],
                            "Возвращён" if v["undone"] else "Перемещён",
                        )
                        for v in SortHistory(folder).list()
                    ]
                    message = f"Записей: {len(rows)}"
                if result.get("errors"):
                    message += " · " + "; ".join(result["errors"][:5])

                def done():
                    self.sort_tree.delete(*self.sort_tree.get_children())
                    for row in rows:
                        self.sort_tree.insert("", "end", values=row)
                    self.sort_status.set(message)

                self.post_ui(done)
            except Exception as exc:
                self.post_ui(lambda m=str(exc): self.sort_status.set(m))
            finally:
                self.post_ui(lambda: setattr(self, "sort_busy", False))

        self._background(work)

    def _update_workflows(self):
        self.scenario_status.set(self.runtime.scenarios.status)
        metrics = self.runtime.performance.metrics
        self.resource_var.set(
            f"Сейчас: {next(k for k, v in MODES.items() if v == self.runtime.performance.active)} · CPU {metrics['cpu']}% · RAM {metrics['memory_mb']} МБ · процессов {metrics['processes']}"
        )
        if self.current_page == "inbox":
            self._refresh_inbox()
        if self.current_page == "timers":
            rows = self.runtime.state.snapshot()["completed_tasks"][-30:]
            self.completed_label.configure(
                text="\n".join(
                    time.strftime("%d.%m %H:%M", time.localtime(v["completed_at"]))
                    + " · "
                    + v["label"]
                    for v in reversed(rows)
                )
                or "Пока нет выполненных дел"
            )
