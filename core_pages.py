"""Visible controls for kernel jobs, memory, event rules and machine integrations."""

from ui_fonts import UI_FONT

from datetime import datetime
from pathlib import Path
import json
import tkinter as tk
from tkinter import ttk, filedialog
import uuid

from ui_dialogs import messagebox, simpledialog

EVENT_LABELS = {
    "Запуск Крест": "startup",
    "Пробуждение компьютера": "wake",
    "Разблокировка Windows": "unlock",
    "Блокировка Windows": "lock",
    "Изменение устройств": "device_change",
    "Подключение наушников": "headphones_connected",
    "Отключение наушников": "headphones_disconnected",
    "Запуск игры": "game_started",
    "Закрытие игры": "game_stopped",
    "Изменение файла": "file_changed",
}
STATUS_LABELS = {
    "queued": "В очереди",
    "running": "Выполняется",
    "completed": "Завершено",
    "failed": "Ошибка",
    "cancelled": "Отменено",
    "interrupted": "Прервано",
}


class CorePages:
    def build_core_pages(self, code=None):
        builders = {
            "kernel": [self._build_kernel],
            "memory": [self._build_memory],
            "events": [self._build_events],
            "programs": [self._build_programs],
            "security": [self._build_security],
            "scenarios": [self.build_workflows, self._extend_scenarios],
            "inbox": [self._build_inbox],
            "maintenance": [self._build_maintenance, self._extend_files],
            "timers": [self.build_timers_tab, self._extend_task_controls],
        }
        before = {k: getattr(self, k, None) for k in self.CORE_VARIABLES}
        for name in [code] if code else builders:
            for build in builders[name]:
                build()
        for key in (
            "morning",
            "evening",
            "remote_enabled",
            "remote_locked",
            "remote_uploads",
            "reduce_motion",
            "font_scale",
            "extra_folders",
            "file_exclusions",
        ):
            variable = getattr(self, key, None)
            if variable is not None and variable is not before[key]:
                variable.trace_add("write", self._mark_dirty)
        if code in (None, "security"):
            self.remote_commands.bind("<KeyRelease>", lambda e: self._mark_dirty())

    def _table(self, parent, columns, height=4):
        tree = ttk.Treeview(parent, columns=tuple(columns), show="headings", height=height)
        for name, title in columns.items():
            tree.heading(name, text=title)
            tree.column(
                name,
                width=360 if name in ("value", "command", "service", "name") else 150,
                minwidth=60,
            )
        tree.pack(fill="x", pady=10)
        return tree

    def _buttons(self, parent, items):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=8)
        for i, (label, action) in enumerate(items):
            ttk.Button(row, text=label, command=action).grid(
                row=i // 3, column=i % 3, sticky="ew", padx=(0, 6), pady=3
            )
        return row

    def _build_kernel(self):
        card = self.card(
            self.page_bodies["kernel"],
            "Крест продолжает работать",
            "Таймеры, сценарии и сервисы принадлежат отдельному процессу. Окно можно закрыть, сохранив ядро.",
        )
        self.kernel_info = tk.StringVar(value="Подключение…")
        self.label(card, textvariable=self.kernel_info, wraplength=680).pack(fill="x")
        self._buttons(
            card,
            [
                ("Обновить состояние", self._refresh_core),
                ("Закрыть только окно", self.detach_window),
                ("Создать резервную копию", self._backup),
            ],
        )
        self.health_tree = self._table(
            card, {"service": "Сервис", "health": "Мост", "memory": "Память, МБ"}, 4
        )
        card = self.card(
            self.page_bodies["kernel"],
            "Журнал выполнения",
            "Повторное выполнение запускается только по вашему нажатию. Проверьте результат прерванного действия перед повтором.",
        )
        self.job_tree = self._table(
            card, {"time": "Время", "command": "Команда", "status": "Состояние"}, 7
        )
        self.job_result = tk.StringVar()
        self.label(card, textvariable=self.job_result, wraplength=680, muted=True).pack(fill="x")
        self.job_tree.bind("<<TreeviewSelect>>", lambda e: self._job_detail())
        self._buttons(
            card,
            [
                ("Обновить", self._refresh_jobs),
                ("Остановить задание", self._cancel_job),
                ("Повторить выбранное", self._repeat_job),
            ],
        )
        self._refresh_jobs()

    def _refresh_jobs(self):
        rows = self.runtime.jobs.list(100)
        self._job_rows = {r["id"]: r for r in rows}
        self.job_tree.delete(*self.job_tree.get_children())
        for row in rows:
            self.job_tree.insert(
                "",
                "end",
                iid=row["id"],
                values=(
                    datetime.fromtimestamp(row["created"]).strftime("%d.%m %H:%M:%S"),
                    row["command"],
                    STATUS_LABELS[row["status"]],
                ),
            )

    def _job_detail(self):
        selected = self.job_tree.selection()
        self.job_result.set(self._job_rows[selected[0]]["result"][:1400] if selected else "")

    def _cancel_job(self):
        selected = self.job_tree.selection()
        if selected:
            row = self.runtime.jobs.get(selected[0])
            if row:
                self.job_result.set(self.runtime.cancel_job(row["id"]))
            self._refresh_jobs()

    def _repeat_job(self):
        selected = self.job_tree.selection()
        if selected:
            row = self.runtime.jobs.get(selected[0])
            if (
                row
                and row["status"] in ("failed", "interrupted", "cancelled")
                and messagebox.askyesno(
                    "Повторить действие",
                    "Ранее начатое действие могло успеть изменить систему. Повторить:\n"
                    + row["command"],
                    parent=self,
                )
            ):
                self.runtime.submit(row["command"])
                self._refresh_jobs()

    def _refresh_core(self):
        cached = getattr(self.runtime, "cached", None)
        if cached:
            uptime = int(__import__("time").time() - cached["started"])
            error = cached.get("windows_events_error")
            text = f"Ядро подключено · PID {cached['pid']} · работает {uptime // 3600} ч {uptime % 3600 // 60} мин"
            text += (
                "\nБезопасный режим"
                if cached["safe_mode"]
                else "\nОбычный режим · Ctrl+Alt+Space — быстрая панель"
            )
            if error:
                text += "\n" + error
        else:
            text = "Встроенный режим разработки. Постоянное ядро запускается через main.py."
        self.kernel_info.set(text)
        self.health_tree.delete(*self.health_tree.get_children())
        labels = {
            "ready": "Готов",
            "starting": "Ожидание моста",
            "stopped": "Остановлен",
            "error": "Ошибка",
        }
        for filename, title in self.integration.MODULES.values():
            detail = self.integration.details(filename)
            self.health_tree.insert(
                "",
                "end",
                values=(
                    title,
                    labels.get(detail.get("health"), "Нет данных"),
                    detail.get("resources", {}).get("memory_mb", "—"),
                ),
            )
        self._refresh_jobs()

    def _build_memory(self):
        card = self.card(
            self.page_bodies["memory"],
            "То, что Крест помнит",
            "Локальные записи, которые вы можете прочитать, исправить, выгрузить и удалить.",
        )
        self.memory_tree = self._table(
            card, {"key": "Название", "value": "Значение", "category": "Тип"}
        )
        self.memory_key, self.memory_value = tk.StringVar(), tk.StringVar()
        self.field(card, "Название", self.memory_key)
        self.field(card, "Значение", self.memory_value)
        self.memory_tree.bind("<<TreeviewSelect>>", lambda e: self._select_memory())
        self._buttons(
            card,
            [
                ("Запомнить", self._save_memory),
                ("Забыть выбранное", self._forget_memory),
                ("Обновить", self._refresh_memory),
                ("Экспорт памяти", self._export_memory),
                ("Импорт памяти", self._import_memory),
            ],
        )
        card = self.card(
            self.page_bodies["memory"],
            "Ваш день",
            "Сводка использует ваши таймеры и дела. Привычки предлагаются после повторений в три разных дня.",
        )
        self.briefing_text = tk.StringVar()
        self.label(card, textvariable=self.briefing_text, wraplength=680).pack(fill="x")
        self._buttons(
            card,
            [
                ("План на сегодня", lambda: self.runtime.submit("план на сегодня")),
                ("Предложения привычек", lambda: self.runtime.submit("предложи привычки")),
            ],
        )
        cfg = self.config_mgr.snapshot()["briefings"]
        self.morning = tk.StringVar(value=cfg["morning"])
        self.evening = tk.StringVar(value=cfg["evening"])
        self.field(card, "Утренняя сводка, ЧЧ:ММ", self.morning)
        self.field(card, "Вечерняя сводка, ЧЧ:ММ", self.evening)
        self.label(card, "Пустое время отключает автоматическую сводку.", muted=True).pack(
            anchor="w"
        )
        ttk.Button(card, text="Сохранить расписание", command=self._save_briefings).pack(
            anchor="w", pady=10
        )
        self._refresh_memory()

    def _refresh_memory(self):
        self.memory_tree.delete(*self.memory_tree.get_children())
        self._memory_rows = self.runtime.memory.list()
        for i, row in enumerate(self._memory_rows):
            self.memory_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(
                    row["key"],
                    row["value"],
                    {
                        "note": "Заметка",
                        "preference": "Предпочтение",
                        "application": "Программа",
                        "correction": "Исправление",
                    }[row["category"]],
                ),
            )
        from assistant_memory import briefing

        self.briefing_text.set(briefing(self.runtime.state))

    def _select_memory(self):
        selected = self.memory_tree.selection()
        if selected:
            row = self._memory_rows[int(selected[0])]
            self.memory_key.set(row["key"])
            self.memory_value.set(row["value"])

    def _save_memory(self):
        try:
            self.runtime.memory.remember(self.memory_key.get(), self.memory_value.get(), "note")
            self._refresh_memory()
        except (ValueError, OSError) as exc:
            self._report_error(str(exc))

    def _forget_memory(self):
        selected = self.memory_tree.selection()
        if selected:
            self.runtime.memory.forget(self._memory_rows[int(selected[0])]["key"])
            self._refresh_memory()

    def _export_memory(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".json", initialfile="krest-memory.json"
        )
        if path:
            from assistant_memory import export_memory

            self._background(
                lambda: export_memory(self.runtime.memory, path),
                lambda _: self.status_var.set("Память сохранена"),
            )

    def _import_memory(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("Память Крест", "*.json")])
        if path:
            from assistant_memory import import_memory

            self._background(
                lambda: import_memory(self.runtime.memory, path), lambda _: self._refresh_memory()
            )

    def _save_briefings(self):
        self._save_section(
            "briefings",
            {"morning": self.morning.get().strip(), "evening": self.evening.get().strip()},
        )

    def _save_section(self, key, value):
        try:
            cfg = self.config_mgr.snapshot()
            cfg[key] = value
            self.config_mgr.save_config(cfg)
            self.status_var.set("Сохранено. Ядро подхватит изменение автоматически.")
            return True
        except (ValueError, OSError) as exc:
            self._report_error(str(exc))
            return False

    def _build_events(self):
        card = self.card(
            self.page_bodies["events"],
            "Когда это случится",
            "Автоматизация запускает только выбранные вами сценарии. При занятости ядра повторные события не создают очередь одинаковых запусков.",
        )
        self.event_tree = self._table(
            card, {"event": "Событие", "scenario": "Сценарий", "enabled": "Включено"}
        )
        self.event_name = tk.StringVar(value=next(iter(EVENT_LABELS)))
        self.event_scenario = tk.StringVar()
        self.event_path = tk.StringVar()
        self.event_cooldown = tk.StringVar(value="60")
        self.event_enabled = tk.BooleanVar(value=False)
        ttk.Combobox(
            card, textvariable=self.event_name, values=list(EVENT_LABELS), state="readonly"
        ).pack(fill="x", pady=5)
        self.event_scenario_combo = ttk.Combobox(
            card,
            textvariable=self.event_scenario,
            values=list(self.config_mgr.snapshot()["scenarios"]),
            state="readonly",
        )
        self.event_scenario_combo.pack(fill="x", pady=5)
        self.field(card, "Файл (для изменения файла)", self.event_path)
        self.field(card, "Пауза между запусками, сек.", self.event_cooldown)
        ttk.Checkbutton(card, text="Включить это правило", variable=self.event_enabled).pack(
            anchor="w"
        )
        self._buttons(
            card,
            [
                ("Добавить правило", self._add_event),
                ("Включить / выключить", self._toggle_event),
                ("Удалить", self._delete_event),
            ],
        )
        self._refresh_events()
        card = self.card(
            self.page_bodies["events"],
            "Последние события",
            "Изменения устройств, игры и результаты сценариев — в одном журнале.",
        )
        self.event_log = tk.StringVar(value="Нажмите «Обновить».")
        self.label(card, textvariable=self.event_log, wraplength=680, muted=True).pack(fill="x")
        ttk.Button(card, text="Обновить события", command=self._event_history).pack(
            anchor="w", pady=10
        )

    def _refresh_events(self):
        self.event_tree.delete(*self.event_tree.get_children())
        for row in self.config_mgr.snapshot()["event_rules"]:
            title = next((k for k, v in EVENT_LABELS.items() if v == row["event"]), row["event"])
            self.event_tree.insert(
                "",
                "end",
                iid=row["id"],
                values=(title, row["scenario"], "Да" if row.get("enabled") else "Нет"),
            )

    def _add_event(self):
        try:
            row = {
                "id": uuid.uuid4().hex[:12],
                "event": EVENT_LABELS[self.event_name.get()],
                "scenario": self.event_scenario.get(),
                "enabled": self.event_enabled.get(),
                "cooldown": int(self.event_cooldown.get()),
            }
            if row["event"] == "file_changed":
                row["path"] = self.event_path.get().strip()
            rules = self.config_mgr.snapshot()["event_rules"] + [row]
            if self._save_section("event_rules", rules):
                self._refresh_events()
        except ValueError as exc:
            self._report_error(str(exc))

    def _toggle_event(self):
        selected = self.event_tree.selection()
        rules = self.config_mgr.snapshot()["event_rules"]
        for row in rules:
            if row["id"] in selected:
                row["enabled"] = not row.get("enabled", False)
        if self._save_section("event_rules", rules):
            self._refresh_events()

    def _delete_event(self):
        selected = self.event_tree.selection()
        if self._save_section(
            "event_rules",
            [v for v in self.config_mgr.snapshot()["event_rules"] if v["id"] not in selected],
        ):
            self._refresh_events()

    def _event_history(self):
        rows = self.runtime.memory.recent_events(15)
        names = {value: title for title, value in EVENT_LABELS.items()}
        names.update({"rule_started": "Запущено правило", "scenario_finished": "Сценарий завершён"})
        lines = []
        for row in rows:
            payload = json.loads(row["payload"])
            detail = (
                payload.get("text")
                or payload.get("device")
                or payload.get("path")
                or payload.get("process")
                or payload.get("rule")
                or ""
            )
            lines.append(
                datetime.fromtimestamp(row["created"]).strftime("%H:%M")
                + " · "
                + names.get(row["type"], row["type"])
                + (" · " + str(detail)[:180] if detail else "")
            )
        self.event_log.set("\n".join(lines) or "Событий пока нет.")

    def _build_programs(self):
        card = self.card(
            self.page_bodies["programs"],
            "Ваши программы",
            "Крест находит ярлыки меню «Пуск» и приложения Windows. Короткое имя позволяет запускать программу привычной фразой.",
        )
        self.app_tree = self._table(card, {"name": "Программа", "kind": "Источник"}, 7)
        self.app_alias = tk.StringVar()
        self.field(card, "Короткое имя", self.app_alias)
        self._buttons(
            card,
            [
                ("Найти программы", self._scan_apps),
                ("Сохранить имя", self._alias_app),
                ("Открыть выбранную", self._launch_app),
            ],
        )
        self._refresh_apps()
        card = self.card(
            self.page_bodies["programs"],
            "Микшер приложений",
            "Громкость меняется для выбранной программы. Если у неё несколько аудиосессий, Крест попросит уточнить источник.",
        )
        self.audio_tree = self._table(
            card, {"name": "Процесс", "volume": "Громкость, %", "muted": "Без звука"}, 4
        )
        self.app_volume = tk.StringVar(value="50")
        self.field(card, "Громкость, 0–100", self.app_volume)
        self._buttons(
            card, [("Обновить микшер", self._scan_audio), ("Изменить громкость", self._volume_app)]
        )
        self.output_var = tk.StringVar()
        self.output_combo = ttk.Combobox(card, textvariable=self.output_var, state="readonly")
        self.output_combo.pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Найти устройства", self._scan_outputs),
                ("Использовать устройство", self._set_output),
            ],
        )

    def _refresh_apps(self):
        if "programs" not in self._built_pages:
            return
        self._apps = self.config_mgr.snapshot()["applications"]
        self.app_tree.delete(*self.app_tree.get_children())
        for i, row in enumerate(self._apps):
            self.app_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(row["name"], "Windows" if row["kind"] == "app_id" else "Ярлык"),
            )

    def _scan_apps(self):
        from app_catalog import scan_apps

        def finish(rows):
            if rows and self._save_section("applications", rows):
                self._refresh_apps()
            else:
                self.status_var.set("Программы не найдены. Автопоиск доступен на Windows.")

        self._background(scan_apps, finish)

    def _alias_app(self):
        selected = self.app_tree.selection()
        if selected:
            aliases = self.config_mgr.snapshot()["application_aliases"]
            aliases[self.app_alias.get().casefold().strip()] = self._apps[int(selected[0])]["name"]
            self._save_section("application_aliases", aliases)

    def _launch_app(self):
        selected = self.app_tree.selection()
        if selected:
            self.runtime.submit("открой программу " + self._apps[int(selected[0])]["name"])

    def _scan_audio(self):
        from windows_audio import sessions

        def finish(rows):
            self._audio_rows = rows
            self.audio_tree.delete(*self.audio_tree.get_children())
            for i, row in enumerate(rows):
                self.audio_tree.insert(
                    "",
                    "end",
                    iid=str(i),
                    values=(row["name"], row["volume"], "Да" if row["muted"] else "Нет"),
                )

        self._background(sessions, finish)

    def _volume_app(self):
        selected = self.audio_tree.selection()
        if selected:
            from windows_audio import set_app_volume

            name = self._audio_rows[int(selected[0])]["name"]
            try:
                percent = int(self.app_volume.get())
            except ValueError:
                self._report_error("Громкость должна быть числом 0–100")
                return
            self._background(lambda: set_app_volume(name, percent), lambda _: self._scan_audio())

    def _scan_outputs(self):
        from windows_audio import output_devices

        def finish(rows):
            self._outputs = rows
            self.output_combo.configure(values=[v["name"] for v in rows])
            if rows:
                self.output_combo.current(0)

        self._background(output_devices, finish)

    def _set_output(self):
        index = self.output_combo.current()
        if index >= 0:
            from windows_audio import set_default_output

            ident = self._outputs[index]["id"]
            self._background(lambda: set_default_output(ident), self._append_log)

    def _build_security(self):
        cfg = self.config_mgr.snapshot()
        card = self.card(
            self.page_bodies["security"],
            "Удалённый доступ",
            "Бот принимает команды только от владельца в личном диалоге. Разрешены точные фразы из этого списка; команды выполняет ядро Крест.",
        )
        self.remote_enabled = tk.BooleanVar(value=cfg["remote_control"]["enabled"])
        self.remote_locked = tk.BooleanVar(value=cfg["remote_control"]["block_when_locked"])
        self.remote_uploads = tk.BooleanVar(value=cfg["remote_control"]["allow_uploads"])
        for text, variable in [
            ("Разрешить удалённые команды", self.remote_enabled),
            ("Приостанавливать их при блокировке Windows", self.remote_locked),
            ("Принимать файлы от владельца, до 20 МБ", self.remote_uploads),
        ]:
            ttk.Checkbutton(card, text=text, variable=variable).pack(anchor="w")
        self.remote_commands = tk.Text(
            card,
            height=7,
            width=1,
            wrap="word",
            bg=self.theme["sidebar"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="flat",
            padx=12,
            pady=10,
            font=(UI_FONT, 10),
        )
        self.remote_commands.pack(fill="x", pady=10)
        self.remote_commands.insert("1.0", "\n".join(cfg["remote_control"]["allowed_commands"]))
        self.flow_buttons(
            card,
            [
                ("Базовые команды", self._default_remote_commands),
                ("Сохранить доступ", self._save_remote),
            ],
        )
        card = self.card(
            self.page_bodies["security"],
            "Доступность и движение",
            "Настройки оформления доступны в обычной версии. Изменение масштаба применяется после следующего запуска.",
        )
        self.reduce_motion = tk.BooleanVar(value=cfg["accessibility"]["reduce_motion"])
        self.font_scale = tk.StringVar(value=str(cfg["accessibility"]["font_scale"]))
        ttk.Checkbutton(
            card, text="Уменьшить движение и отключить анимации", variable=self.reduce_motion
        ).pack(anchor="w")
        ttk.Combobox(
            card,
            textvariable=self.font_scale,
            values=["90", "100", "110", "125", "150"],
            state="readonly",
        ).pack(fill="x", pady=10)
        ttk.Button(
            card,
            text="Сохранить доступность",
            command=lambda: self._save_section(
                "accessibility",
                {
                    "reduce_motion": self.reduce_motion.get(),
                    "font_scale": int(self.font_scale.get()),
                },
            ),
        ).pack(anchor="w")
        card = self.card(
            self.page_bodies["security"],
            "Восстановление и диагностика",
            "Копия включает задачи, память, историю и настройки. Токены и код Premium исключаются; автоматизации в копии отключены.",
        )
        self.flow_buttons(
            card,
            [
                ("Создать копию", self._backup),
                ("Проверить копию", self._inspect_backup),
                ("Экспорт диагностики", self._export_diagnostics),
                ("Проверить обновления", self._check_updates),
            ],
        )
        self.label(
            card,
            (
                "Восстановление выполняется при остановленном ядре:\nKrest.exe --restore путь_к_копии.zip\nБезопасный запуск: Krest.exe --safe-mode"
                if __import__("app_paths").compiled()
                else "Восстановление выполняется при остановленном ядре:\npython main.py --restore путь_к_копии.zip\nБезопасный запуск: python main.py --safe-mode"
            ),
            muted=True,
            wraplength=680,
        ).pack(fill="x", pady=10)

    def _default_remote_commands(self):
        from remote_policy import DEFAULT_ALLOWED

        self.remote_commands.delete("1.0", "end")
        self.remote_commands.insert("1.0", "\n".join(DEFAULT_ALLOWED))

    def _save_remote(self):
        self._save_section(
            "remote_control",
            {
                "enabled": self.remote_enabled.get(),
                "block_when_locked": self.remote_locked.get(),
                "allow_uploads": self.remote_uploads.get(),
                "allowed_commands": [
                    v.strip()
                    for v in self.remote_commands.get("1.0", "end").splitlines()
                    if v.strip()
                ],
            },
        )

    def _backup(self):
        path = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".zip",
            initialfile="krest-backup-" + datetime.now().strftime("%Y%m%d-%H%M") + ".zip",
        )
        if path:
            from backup_service import create_backup

            self._background(
                lambda: create_backup(self.config_mgr, path),
                lambda value: self.status_var.set("Копия сохранена: " + value),
            )

    def _inspect_backup(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("Копия Крест", "*.zip")])
        if path:
            from backup_service import inspect_backup

            self._background(
                lambda: inspect_backup(path),
                lambda rows: messagebox.showinfo(
                    "Копия проверена",
                    "Контрольные суммы совпали. Файлы:\n" + "\n".join(rows),
                    parent=self,
                ),
            )

    def _check_updates(self):
        from app_paths import compiled

        if compiled():
            messagebox.showinfo(
                "Обновление Крест",
                "Для установленной программы используйте новый установщик от автора. Перед обновлением завершите Крест через меню. Настройки, модели и скриншоты сохранятся.",
                parent=self,
            )
            return
        from source_update import SourceUpdater

        def finish(result):
            text = (
                (
                    "Доступны новые исходники:\n"
                    + result["summary"]
                    + "\nЗакройте меню и ядро, затем: python main.py --update"
                )
                if result["available"]
                else "Новых коммитов в main нет."
            )
            messagebox.showinfo("Обновления", text, parent=self)

        self._background(SourceUpdater(Path(__file__).parent, self.config_mgr).check, finish)

    def _export_diagnostics(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".json", initialfile="krest-diagnostics.json"
        )
        if path:
            from support_report import export_report

            self._background(
                lambda: export_report(self.runtime, self.config_mgr, path),
                lambda _: self.status_var.set(
                    "Диагностика сохранена без содержимого личных заметок и команд"
                ),
            )

    def _extend_files(self):
        cfg = self.config_mgr.snapshot()["file_policy"]
        card = self.card(
            self.page_bodies["maintenance"],
            "Правила ваших папок",
            "Дополнительные папки проверяются вместе с загрузками. Незавершённые и недавно изменённые файлы остаются на месте.",
        )
        self.extra_folders = tk.StringVar(value="; ".join(cfg["folders"]))
        self.file_exclusions = tk.StringVar(value="; ".join(cfg["exclusions"]))
        self.rule_extensions = tk.StringVar()
        self.rule_category = tk.StringVar()
        self.field(card, "Папки через ;", self.extra_folders)
        self.field(card, "Исключения, например *.keep", self.file_exclusions)
        self.file_rule_tree = self._table(
            card, {"extensions": "Расширения", "category": "Папка"}, 4
        )
        self.field(card, "Расширения через ,", self.rule_extensions)
        self.field(card, "Категория", self.rule_category)
        self._file_rules = list(cfg["rules"])
        self._buttons(
            card,
            [
                ("Добавить правило", self._add_file_rule),
                ("Удалить правило", self._delete_file_rule),
                ("Сохранить правила", self._save_files),
                ("Найти дубликаты", self._duplicates),
            ],
        )
        self._draw_file_rules()

    def _draw_file_rules(self):
        self.file_rule_tree.delete(*self.file_rule_tree.get_children())
        for i, row in enumerate(self._file_rules):
            self.file_rule_tree.insert(
                "", "end", iid=str(i), values=(", ".join(row["extensions"]), row["category"])
            )

    def _add_file_rule(self):
        from file_tools import validate_policy

        row = {
            "extensions": [
                v.strip().lower() for v in self.rule_extensions.get().split(",") if v.strip()
            ],
            "category": self.rule_category.get().strip(),
        }
        try:
            validate_policy({"rules": [row]})
            self._file_rules.append(row)
            self._mark_dirty()
            self._draw_file_rules()
        except ValueError as exc:
            self._report_error(str(exc))

    def _delete_file_rule(self):
        selected = {int(v) for v in self.file_rule_tree.selection()}
        self._file_rules = [v for i, v in enumerate(self._file_rules) if i not in selected]
        self._mark_dirty()
        self._draw_file_rules()

    def _save_files(self):
        self._save_section(
            "file_policy",
            {
                "folders": [v.strip() for v in self.extra_folders.get().split(";") if v.strip()],
                "exclusions": [
                    v.strip() for v in self.file_exclusions.get().split(";") if v.strip()
                ],
                "rules": self._file_rules,
            },
        )

    def _duplicates(self):
        from file_tools import find_duplicates

        folder = self.config_mgr.snapshot()["app_settings"]["downloads_path"]

        def finish(result):
            text = (
                "\n\n".join("\n".join(v["paths"]) for v in result["groups"][:15])
                or "Одинаковых файлов не найдено."
            )
            if result["limited"]:
                text += "\nДостигнут лимит проверки: 5000 файлов или 512 МБ чтения."
            if result["errors"]:
                text += "\nОшибки: " + "; ".join(result["errors"][:3])
            messagebox.showinfo("Дубликаты — файлы сохранены", text[:3500], parent=self)

        self._background(lambda: find_duplicates(folder, self.runtime.stop_event), finish)

    def _extend_scenarios(self):
        card = self.card(
            self.page_bodies["scenarios"],
            "Условия выбранного шага",
            "Условие проверяется перед шагом. Невыполненное условие пропускает шаг. Для противоположной ветки включите обратное условие.",
        )
        self.condition_kind = tk.StringVar(value="Всегда")
        self.condition_value = tk.StringVar()
        self.condition_invert = tk.BooleanVar(value=False)
        self.step_temporary = tk.BooleanVar(value=False)
        self.step_timeout = tk.StringVar(value="30")
        ttk.Combobox(
            card,
            textvariable=self.condition_kind,
            values=["Всегда", "Процесс", "Файл", "Игра", "Дни недели"],
            state="readonly",
        ).pack(fill="x", pady=5)
        self.field(card, "Имя / путь / дни 0–6 через ,", self.condition_value)
        self.field(card, "Лимит ожидания, секунд", self.step_timeout)
        ttk.Checkbutton(card, text="Обратное условие", variable=self.condition_invert).pack(
            anchor="w"
        )
        ttk.Checkbutton(
            card, text="Вернуть профиль / уведомления после сценария", variable=self.step_temporary
        ).pack(anchor="w")
        self._buttons(
            card,
            [
                ("Применить к выбранному шагу", self._apply_step_options),
                ("Показать план", self._preview_scenario),
                ("Пропустить ожидание", lambda: self._background(self.runtime.scenarios.skip_wait)),
                ("Повторить с ошибочного шага", self._retry_scenario),
            ],
        )

    def _apply_step_options(self):
        selected = self.scenario_tree.selection()
        if not selected:
            return
        try:
            row = dict(self.scenario_steps[int(selected[0])])
            kinds = {"Процесс": "process", "Файл": "file", "Игра": "game", "Дни недели": "weekday"}
            row.pop("condition", None)
            if self.condition_kind.get() != "Всегда":
                row["condition"] = {
                    "kind": kinds[self.condition_kind.get()],
                    "value": self.condition_value.get(),
                    "invert": self.condition_invert.get(),
                }
            row["timeout"] = int(self.step_timeout.get())
            row["temporary"] = self.step_temporary.get()
            from scenarios import validate_scenarios

            validate_scenarios({"Проверка": [row]})
            self.scenario_steps[int(selected[0])] = row
            self.status_var.set("Условия добавлены к шагу. Сохраните сценарий.")
        except ValueError as exc:
            self._report_error(str(exc))

    def _preview_scenario(self):
        from workflow_pages import ACTIONS, MODES, NOTICE_MODES

        actions = {value: title for title, value in ACTIONS.items()}
        choices = {value: title for title, value in {**MODES, **NOTICE_MODES}.items()}
        kinds = {
            "file": "существует файл",
            "process": "работает процесс",
            "game": "запущена игра",
            "weekday": "день недели",
        }
        lines = []
        for i, row in enumerate(self.scenario_steps, 1):
            value = (
                choices.get(row["value"], row["value"])
                if row["action"] in ("profile", "notifications")
                else row["value"]
            )
            line = f"{i}. {actions[row['action']]}: {value}"
            condition = row.get("condition")
            if condition:
                line += (
                    " · если "
                    + ("не " if condition.get("invert") else "")
                    + kinds[condition["kind"]]
                    + " "
                    + condition["value"]
                )
            if row.get("temporary"):
                line += " · с восстановлением настройки"
            lines.append(line)
        messagebox.showinfo("План сценария", "\n".join(lines) or "Добавьте шаги.", parent=self)

    def _retry_scenario(self):
        if messagebox.askyesno(
            "Повторить шаг",
            "Проверьте результат ранее начатого действия. Повторить с шага, на котором сценарий остановился?",
            parent=self,
        ):
            self._background(self.runtime.scenarios.retry)

    def _extend_task_controls(self):
        card = self.card(
            self.page_bodies["timers"],
            "Изменить ближайшее напоминание",
            "Выберите задачу в таблице выше. Перенос изменяет только ближайшее срабатывание, сохраняя дни и время будущих повторов.",
        )
        self._buttons(
            card,
            [("Перенести выбранное", self._move_task), ("Пропустить ближайшее", self._skip_task)],
        )

    def _selected_task(self):
        selected = self.timer_tree.selection()
        if not selected:
            return None
        return selected[0].split(":")[-1]

    def _move_task(self):
        ident = self._selected_task()
        if ident:
            value = simpledialog.askstring(
                "Перенос напоминания", "Новая дата и время: ДД.ММ.ГГГГ ЧЧ:ММ", parent=self
            )
            if value:
                try:
                    self.runtime.state.move_occurrence(
                        ident, datetime.strptime(value, "%d.%m.%Y %H:%M").timestamp()
                    )
                    self._update_timers()
                except (ValueError, RuntimeError) as exc:
                    self._report_error(str(exc))

    def _skip_task(self):
        ident = self._selected_task()
        if ident:
            try:
                self.runtime.state.skip_occurrence(ident)
                self._update_timers()
            except (ValueError, RuntimeError) as exc:
                self._report_error(str(exc))

    CORE_VARIABLES = (
        "memory_key",
        "memory_value",
        "morning",
        "evening",
        "event_name",
        "event_scenario",
        "event_path",
        "event_cooldown",
        "event_enabled",
        "app_alias",
        "app_volume",
        "remote_enabled",
        "remote_locked",
        "remote_uploads",
        "reduce_motion",
        "font_scale",
        "extra_folders",
        "file_exclusions",
        "rule_extensions",
        "rule_category",
        "condition_kind",
        "condition_value",
        "condition_invert",
        "step_temporary",
        "step_timeout",
    )

    def core_draft_values(self):
        import copy

        return {
            "variables": {
                key: getattr(self, key).get() for key in self.CORE_VARIABLES if hasattr(self, key)
            },
            "remote": self.remote_commands.get("1.0", "end-1c")
            if "security" in self._built_pages
            else None,
            "rules": copy.deepcopy(getattr(self, "_file_rules", None)),
        }

    def restore_core_draft(self, draft):
        for key, value in draft["variables"].items():
            getattr(self, key).set(value)
        if draft["remote"] is not None:
            self.remote_commands.delete("1.0", "end")
            self.remote_commands.insert("1.0", draft["remote"])
        if draft["rules"] is not None:
            self._file_rules = draft["rules"]
            self._draw_file_rules()

    def core_settings_draft(self):
        result = {}
        if "memory" in self._built_pages:
            result["briefings"] = {
                "morning": self.morning.get().strip(),
                "evening": self.evening.get().strip(),
            }
        if "security" in self._built_pages:
            result["accessibility"] = {
                "reduce_motion": self.reduce_motion.get(),
                "font_scale": int(self.font_scale.get()),
            }
            result["remote_control"] = {
                "enabled": self.remote_enabled.get(),
                "block_when_locked": self.remote_locked.get(),
                "allow_uploads": self.remote_uploads.get(),
                "allowed_commands": [
                    v.strip()
                    for v in self.remote_commands.get("1.0", "end").splitlines()
                    if v.strip()
                ],
            }
        if "maintenance" in self._built_pages:
            result["file_policy"] = {
                "folders": [v.strip() for v in self.extra_folders.get().split(";") if v.strip()],
                "exclusions": [
                    v.strip() for v in self.file_exclusions.get().split(";") if v.strip()
                ],
                "rules": self._file_rules,
            }
        return result
