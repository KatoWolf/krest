"""AFK duty control surface. No file scans, COM or RPC on the Tk thread."""

import datetime as dt
import tkinter as tk
from tkinter import ttk, filedialog
from ui_fonts import UI_FONT

from autonomy import DEFAULT_AUTONOMY, KINDS, STATUS_NAMES


class AutonomyPages:
    def _duty_call(self, action, done=None, **values):
        def display(result):
            self.status_var.set(str(result.get("text", "") if isinstance(result, dict) else result))
            if done:
                done(result)
            self._duty_refresh(force=True)

        self._background(lambda: self.runtime.autonomy.call(action, **values), display)

    def build_autonomy_tab(self):
        self._duty_actions_shown = None
        body = self.page_bodies["autonomy"]
        card = self.card(
            body,
            "Дежурю, пока вас нет",
            "Одна задача за раз. Возвращение к ПК, игра, просмотр или звонок приостанавливают работу.",
        )
        self.duty_status = tk.StringVar(value="Проверяю состояние…")
        self.label(card, textvariable=self.duty_status, wraplength=740).pack(
            anchor="w", pady=(0, 12)
        )
        row = tk.Frame(card, bg=card.cget("bg"))
        row.pack(fill="x")
        for text, action in [("Приостановить", "pause"), ("Продолжить при AFK", "resume")]:
            ttk.Button(row, text=text, command=lambda a=action: self._duty_call(a)).pack(
                side="left", padx=(0, 8)
            )
        ttk.Button(
            row,
            text="Копия настроек",
            command=lambda: self._duty_call("enqueue", kind="profile_backup"),
        ).pack(side="left")
        self.label(
            card,
            "Начатая установка обновления завершается даже после вашего возвращения. Следующая ждёт AFK.",
            muted=True,
            wraplength=740,
        ).pack(anchor="w", pady=(12, 0))

        card = self.card(
            body,
            "Очередь и результаты",
            "Выберите строку, чтобы увидеть подробности и доступные действия.",
        )
        style = ttk.Style(self)
        style.configure("Duty.TNotebook", background=self.theme["card"], borderwidth=0)
        style.configure(
            "Duty.TNotebook.Tab",
            background=self.theme["sidebar"],
            foreground=self.theme["text"],
            padding=(12, 8),
            font=(UI_FONT, 11),
            borderwidth=1,
        )
        style.map(
            "Duty.TNotebook.Tab",
            background=[("selected", self.theme["glow"]), ("active", self.theme["card_hover"])],
            foreground=[("selected", self.theme["text"]), ("active", self.theme["text"])],
        )
        style.configure("Duty.TFrame", background=self.theme["card"])
        tabs = ttk.Notebook(card, style="Duty.TNotebook")
        tabs.pack(fill="x")
        self.duty_trees = {}
        self.duty_rows = {}
        self.duty_drawn = {}
        for key, title, columns in [
            (
                "jobs",
                "Задачи",
                [
                    ("label", "Задача", 320),
                    ("status", "Состояние", 190),
                    ("updated", "Обновлено", 140),
                ],
            ),
            (
                "rules",
                "Правила",
                [
                    ("label", "Правило", 320),
                    ("enabled", "Состояние", 190),
                    ("interval", "Период", 140),
                ],
            ),
            (
                "versions",
                "Версии файлов",
                [
                    ("source", "Исходный файл", 400),
                    ("kind", "Копия", 130),
                    ("created", "Создана", 140),
                ],
            ),
            (
                "habits",
                "Привычки",
                [
                    ("label", "Повторяемая задача", 350),
                    ("successes", "Выполнений", 110),
                    ("rule_id", "Результат", 220),
                ],
            ),
        ]:
            frame = ttk.Frame(tabs, padding=8, style="Duty.TFrame")
            tabs.add(frame, text=title)
            tree = ttk.Treeview(
                frame,
                columns=[c[0] for c in columns],
                show="headings",
                height=6,
                selectmode="browse",
            )
            scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)
            for field, caption, width in columns:
                tree.heading(field, text=caption)
                tree.column(field, width=width, minwidth=80)
            tree.bind("<<TreeviewSelect>>", lambda event, k=key: self._duty_selected(k))
            self.duty_trees[key] = tree
        self.duty_detail = tk.StringVar()
        self.label(card, textvariable=self.duty_detail, muted=True, wraplength=740).pack(
            anchor="w", pady=12
        )
        row = tk.Frame(card, bg=card.cget("bg"))
        row.pack(fill="x")
        self.duty_action_buttons = []
        for label, action in [
            ("Отменить", "cancel"),
            ("Повторить", "retry"),
            ("В начало очереди", "priority"),
            ("Включить / выключить", "rule.enable"),
            ("В очередь", "rule.run"),
            ("Удалить правило", "rule.delete"),
            ("Восстановить", "restore"),
        ]:
            button = ttk.Button(
                row, text=label, command=lambda a=action: self._duty_selected_action(a)
            )
            self.duty_action_buttons.append((action, button))
        self.duty_selection = ("jobs", "")
        tabs.bind(
            "<<NotebookTabChanged>>",
            lambda e: self._duty_selected(list(self.duty_trees)[tabs.index(tabs.select())]),
        )

        card = self.card(
            body,
            "Новое поручение",
            "Папки и программы выбираются один раз. Правило само дождётся свободного компьютера.",
        )
        self.duty_form = {
            name: tk.StringVar(value=value)
            for name, value in {
                "kind": KINDS["backup"],
                "label": "",
                "source": "",
                "destination": "",
                "url": "",
                "package": "",
                "age_days": "7",
                "min_free_gb": "30",
                "interval": "Каждый день",
            }.items()
        }
        self.duty_repeat = tk.BooleanVar(value=True)
        kind = ttk.Combobox(
            card,
            state="readonly",
            textvariable=self.duty_form["kind"],
            values=[
                KINDS[k]
                for k in (
                    "backup",
                    "copy",
                    "offload",
                    "archive",
                    "unpack",
                    "download",
                    "software",
                    "health",
                )
            ],
        )
        kind.pack(fill="x", pady=(0, 10))
        self.duty_kind_widget = kind
        kind.bind("<<ComboboxSelected>>", self._duty_form_changed)
        self.duty_fields = {}
        for name, title in [
            ("label", "Название правила"),
            ("source", "Источник"),
            ("destination", "Папка назначения"),
            ("url", "Ссылка на файл"),
            ("package", "Программа · ID winget"),
            ("age_days", "Не изменялось дней"),
            ("min_free_gb", "Оставить свободно, ГБ"),
        ]:
            line = tk.Frame(card, bg=card.cget("bg"))
            self.duty_fields[name] = line
            self.label(line, title, width=24).pack(side="left")
            if name == "package":
                self.duty_packages = ttk.Combobox(line, textvariable=self.duty_form[name])
                self.duty_packages.pack(side="left", fill="x", expand=True)
                ttk.Button(line, text="Найти установленные", command=self._duty_find_packages).pack(
                    side="right", padx=(8, 0)
                )
            else:
                ttk.Entry(line, textvariable=self.duty_form[name]).pack(
                    side="left", fill="x", expand=True
                )
            if name in {"source", "destination"}:
                ttk.Button(line, text="Папка…", command=lambda n=name: self._duty_pick(n)).pack(
                    side="left", padx=(8, 0)
                )
            if name == "source":
                ttk.Button(
                    line, text="Файл…", command=lambda: self._duty_pick("source", file=True)
                ).pack(side="left", padx=(8, 0))
        self.duty_hint = tk.StringVar()
        self.label(card, textvariable=self.duty_hint, muted=True, wraplength=740).pack(
            anchor="w", pady=(12, 8)
        )
        row = tk.Frame(card, bg=card.cget("bg"))
        row.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(row, text="Повторять автоматически", variable=self.duty_repeat).pack(
            side="left"
        )
        ttk.Combobox(
            row,
            state="readonly",
            textvariable=self.duty_form["interval"],
            width=18,
            values=["Каждый час", "Каждый день", "Каждую неделю"],
        ).pack(side="left", padx=12)
        ttk.Button(
            row, text="Добавить поручение", style="Accent.TButton", command=self._duty_add
        ).pack(side="right")
        self._duty_form_changed()

        card = self.card(
            body,
            "Как дежурить",
            "Без снимков экрана и нейросетей. Проверка активности идёт отдельно от меню и распознавания речи.",
        )
        cfg = {**DEFAULT_AUTONOMY, **self.config_mgr.snapshot().get("autonomy", {})}
        self.duty_options = {}
        for key, title in [
            ("enabled", "Дежурство включено"),
            ("profile_backup", "Ежедневно сохранять настройки Крест · последние 7 копий"),
            ("health_checks", "Проверять управляемые подключения каждые 30 минут простоя"),
            ("learning", "Создавать правила по повторяющимся поручениям"),
            ("telegram_unpack", "Автоматически распаковывать ZIP, полученные через Telegram-пульт"),
        ]:
            var = self.duty_options[key] = tk.BooleanVar(value=cfg[key])
            ttk.Checkbutton(card, text=title, variable=var).pack(anchor="w", pady=3)
        for key, title, low, high in [
            ("idle_minutes", "Начинать после простоя, минут", 1, 120),
            ("min_free_memory_mb", "Оставлять свободную память, МБ", 512, 16384),
        ]:
            row = tk.Frame(card, bg=card.cget("bg"))
            row.pack(fill="x", pady=6)
            self.label(row, title).pack(side="left")
            self.duty_options[key] = tk.StringVar(value=str(cfg[key]))
            ttk.Spinbox(row, textvariable=self.duty_options[key], from_=low, to=high, width=8).pack(
                side="right"
            )
        self.label(
            card,
            "Обучение: 5 успешных ручных поручений одного вида с теми же путями минимум в 3 разных дня. "
            "Только копирование, версии файлов и архивирование. Отмена задачи запрещает обучение этому повтору. "
            "Это распознавание повторов, без наблюдения за экраном и без обучения управлению игрой.",
            muted=True,
            wraplength=740,
        ).pack(anchor="w", pady=10)
        ttk.Button(card, text="Сохранить режим дежурства", command=self._duty_settings).pack(
            anchor="e"
        )
        self.label(
            card,
            "Голосом: «статус дежурства», «останови дежурство», «продолжи дежурство», "
            "«сохрани настройки в очередь», «запусти правило архив документов».",
            muted=True,
            wraplength=740,
        ).pack(anchor="w", pady=(12, 0))
        self._duty_refresh(force=True)

    def _duty_form_changed(self, event=None):
        kind = next(k for k, title in KINDS.items() if title == self.duty_form["kind"].get())
        visible = {"label"}
        if kind in {"backup", "copy", "offload", "archive", "unpack"}:
            visible |= {"source", "destination"}
        if kind == "download":
            visible |= {"url", "destination"}
        if kind == "software":
            visible.add("package")
        if kind == "offload":
            visible |= {"age_days", "min_free_gb"}
        for name, widget in self.duty_fields.items():
            widget.pack_forget()
        # Keep fields above the explanation and footer, even after switching type.
        previous = self.duty_kind_widget
        for name, widget in self.duty_fields.items():
            if name in visible:
                widget.pack(fill="x", pady=4, after=previous)
                previous = widget
        hints = {
            "backup": "Пять последних версий каждого файла. Неизменённые файлы пропускаются. Восстановление создаёт отдельную копию.",
            "offload": "Только выбранная папка и другой диск. Перенос старых файлов до указанного запаса места; исходник удаляется после проверки копии. Перенос можно отменить восстановлением версии.",
            "software": "Только выбранная установленная программа. Winget обновляет её без перезагрузки ПК. Начатую установку не прерываем; приложения с открытыми документами лучше закрыть заранее.",
            "unpack": "ZIP распакуется в отдельную папку. Пути за пределами архива, ссылки и зашифрованные архивы отклоняются.",
            "health": "Проверка подключений Крест. Используются существующие ограниченные попытки восстановления; ключи и доступы не изменяются.",
        }
        self.duty_hint.set(
            hints.get(
                kind,
                "Прогресс сохраняется. Загрузка и копирование продолжаются после паузы; создание ZIP при прерывании начинается заново.",
            )
            + (
                " За проход: до 10 000 файлов / 20 ГБ. Файлы должны не меняться 2 минуты."
                if kind in {"backup", "copy", "offload", "archive"}
                else ""
            )
        )

    def _duty_pick(self, field, file=False):
        value = (filedialog.askopenfilename if file else filedialog.askdirectory)(
            parent=self,
            title="Выберите " + ("источник" if field == "source" else "папку назначения"),
        )
        if value:
            self.duty_form[field].set(value)

    def _duty_find_packages(self):
        self.status_var.set("Читаю список установленных программ winget…")
        self._background(
            lambda: self.runtime.autonomy.call("software.list"),
            lambda rows: (
                self.duty_packages.configure(values=rows),
                self.status_var.set(f"Найдено программ: {len(rows)}. Выберите из списка."),
            ),
        )

    def _duty_add(self):
        values = {k: v.get().strip() for k, v in self.duty_form.items()}
        selected_kind = values.pop("kind")
        kind = next(k for k, title in KINDS.items() if title == selected_kind)
        label = values.pop("label")
        interval = {"Каждый час": 3600, "Каждый день": 86400, "Каждую неделю": 604800}[
            values.pop("interval")
        ]
        try:
            if kind == "offload":
                for key in ("age_days", "min_free_gb"):
                    values[key] = int(values[key])
        except ValueError:
            self.status_var.set("Возраст файлов и свободное место задаются целыми числами.")
            return
        self._duty_call(
            "rule.add" if self.duty_repeat.get() else "enqueue",
            kind=kind,
            values=values,
            label=label,
            interval=interval,
        )

    def _duty_settings(self):
        try:
            options = self.duty_settings_draft()
        except ValueError:
            self.status_var.set("Укажите простой и резерв памяти целыми числами.")
            return
        self._duty_call("settings", options=options)

    def duty_settings_draft(self):
        return {
            k: v.get() if k not in {"idle_minutes", "min_free_memory_mb"} else int(v.get())
            for k, v in self.duty_options.items()
        }

    def _duty_refresh(self, force=False):
        import time

        if not hasattr(self, "duty_trees") or getattr(self, "_duty_fetching", False):
            return
        if not force and (
            self.current_page != "autonomy"
            or self.state() not in ("normal", "zoomed")
            or time.monotonic() - getattr(self, "_duty_fetched", 0) < 3
        ):
            return
        self._duty_fetching = True
        self._duty_fetched = time.monotonic()

        def fetch():
            try:
                return self.runtime.autonomy.snapshot()
            finally:
                self.post_ui(lambda: setattr(self, "_duty_fetching", False))

        self._background(fetch, self._duty_display)

    def _duty_display(self, data):
        if not self.duty_trees["jobs"].winfo_exists():
            return
        status = "Приостановлено вами" if data["paused"] else data["activity"]["reason"]
        if data["atomic"]:
            status += " · Начатый установщик завершает работу"
        if self.duty_status.get() != status:
            self.duty_status.set(status)
        for key, tree in self.duty_trees.items():
            rows = {r.get("id", r.get("key")): r for r in data[key]}
            self.duty_rows[key] = rows
            previous = self.duty_drawn.get(key, {})
            for ident in previous.keys() - rows.keys():
                tree.delete(ident)
            for index, (ident, row) in enumerate(rows.items()):
                if previous.get(ident) == row:
                    continue
                values = []
                for field in tree["columns"]:
                    value = row.get(field, "")
                    if field in {"updated", "created"}:
                        value = dt.datetime.fromtimestamp(value).strftime("%d.%m %H:%M")
                    elif field == "status":
                        value = STATUS_NAMES.get(value, value)
                    elif field == "enabled":
                        value = ("Включено" if value else "Выключено") + (
                            " · изучено" if row.get("learned") else ""
                        )
                    elif field == "interval":
                        value = f"{value // 3600} ч" if value >= 3600 else f"{value // 60} мин"
                    elif field == "kind":
                        value = "Перенос" if value == "move" else KINDS.get(value, value)
                    elif field == "rule_id":
                        value = (
                            "Не обучать"
                            if row["rejected"]
                            else "Правило создано"
                            if value
                            else f"Дней: {len(row['days'])} / 3"
                        )
                    values.append(value)
                if tree.exists(ident):
                    tree.item(ident, values=values)
                    tree.move(ident, "", index)
                else:
                    tree.insert("", index, iid=ident, values=values)
            self.duty_drawn[key] = rows
        self._duty_selected(self.duty_selection[0])

    def _duty_selected(self, kind):
        selection = self.duty_trees[kind].selection()
        ident = selection[0] if selection else ""
        self.duty_selection = kind, ident
        row = self.duty_rows.get(kind, {}).get(ident, {})
        text = row.get("result", "")
        if kind == "versions" and row:
            text = (
                "Сохранено: "
                + row["destination"]
                + "\nВосстановление будет выполнено при AFK. Текущий файл останется на месте."
            )
        elif kind == "rules" and row:
            text = " · ".join(str(v) for v in row["payload"].values()) or KINDS[row["kind"]]
        elif kind == "habits":
            text = "Повторные задачи учитываются только после успешного выполнения. Автоматические правила не обучают сами себя."
        detail = text or "Выберите строку."
        if self.duty_detail.get() != detail:
            self.duty_detail.set(detail)
        presentation = kind, bool(row)
        if presentation == getattr(self, "_duty_actions_shown", None):
            return
        self._duty_actions_shown = presentation
        for action, button in self.duty_action_buttons:
            button.pack_forget()
            available = (
                (kind == "jobs" and action in {"cancel", "retry", "priority"})
                or (kind == "rules" and action.startswith("rule."))
                or (kind == "versions" and action == "restore")
            )
            if available:
                button.configure(state="normal" if row else "disabled")
                button.pack(side="left", padx=(0, 8))

    def _duty_selected_action(self, action):
        kind, ident = self.duty_selection
        row = self.duty_rows.get(kind, {}).get(ident)
        if not row:
            return
        if action == "restore":
            self._duty_call("enqueue", kind="restore", values={"version_id": ident})
        else:
            self._duty_call(
                action,
                id=ident,
                **({"enabled": not row["enabled"]} if action == "rule.enable" else {}),
            )
