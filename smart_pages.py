"""Lazy, diff-updated controls for selected automation, sharing the Relic widgets."""

from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk, filedialog

from audio_dispatcher import PROFILE_NAMES
from routine_learning import TRIGGERS
from task_watch import STATES

SMART_PAGES = {"smart_files", "watches", "sound", "habits"}


class SmartPages:
    def _smart_call(self, action, done=None, **values):
        def finish(result):
            if isinstance(result, str):
                self.status_var.set(result)
            elif isinstance(result, dict) and result.get("text"):
                self.status_var.set(result["text"])
            if done:
                done(result)
            self._smart_refresh(force=True)

        self._background(lambda: self.runtime.smart.call(action, **values), finish)

    def build_smart_files_tab(self):
        parent = self.page_bodies["smart_files"]
        card = self.card(
            parent,
            "Работа с выбранными файлами",
            "Выделите объекты в Проводнике и скажите «Крест, упакуй это». Архив с проверкой CRC появится в папке archives рядом с данными Крест.",
        )
        self.object_info = tk.StringVar(value="Для работы из меню можно выбрать файлы здесь.")
        self.label(card, textvariable=self.object_info, wraplength=680).pack(fill="x", pady=6)
        self._buttons(
            card,
            [
                ("Выбрать файлы", self._smart_pick_objects),
                ("Выбрать папку", self._smart_pick_object_folder),
                ("Упаковать выбранное", lambda: self.runtime.submit("упакуй это")),
                ("Последний скриншот", lambda: self.runtime.submit("открой последний скриншот")),
            ],
        )
        card = self.card(
            parent,
            "Правила для загрузок",
            "После 20 минут простоя: проверить стабильность, сохранить проверенную копию, при необходимости распаковать ZIP. Исходники сохраняются, установщики не запускаются.",
        )
        self.download_rule_tree = self._table(
            card,
            {"pattern": "Какие файлы", "source": "Откуда", "destination": "Куда", "state": "Режим"},
            4,
        )
        self.download_rule_detail = tk.StringVar()
        self.download_rule_tree.bind("<<TreeviewSelect>>", lambda e: self._download_rule_details())
        self.label(card, textvariable=self.download_rule_detail, wraplength=680, muted=True).pack(
            fill="x"
        )
        self._buttons(
            card,
            [
                ("Включить / выключить", self._smart_toggle_rule),
                (
                    "Удалить правило",
                    lambda: self._smart_selected("download_rule_tree", "downloads.change"),
                ),
            ],
        )
        self.workflow_source = tk.StringVar(
            value=self.config_mgr.snapshot()["app_settings"]["downloads_path"]
        )
        self.workflow_destination = tk.StringVar()
        self.workflow_pattern = tk.StringVar(value="*.zip")
        self.workflow_unpack = tk.BooleanVar(value=True)
        self.workflow_duplicates = tk.BooleanVar(value=True)
        for title, variable in (
            ("Папка загрузки", self.workflow_source),
            ("Папка результата", self.workflow_destination),
        ):
            row = tk.Frame(card, bg=card.cget("bg"))
            row.pack(fill="x", pady=5)
            self.label(row, title, width=18).pack(side="left")
            ttk.Entry(row, textvariable=variable, state="readonly").pack(
                side="left", fill="x", expand=True
            )
            ttk.Button(row, text="Выбрать…", command=lambda v=variable: self._smart_folder(v)).pack(
                side="left", padx=(6, 0)
            )
        self.field(card, "Маска имени, например *мод*.zip", self.workflow_pattern)
        ttk.Checkbutton(
            card, text="Распаковывать ZIP в отдельную папку", variable=self.workflow_unpack
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            card,
            text="Находить точные дубликаты в папке результата",
            variable=self.workflow_duplicates,
        ).pack(anchor="w", pady=4)
        ttk.Button(
            card,
            text="Создать правило",
            style="Accent.TButton",
            command=lambda: self._smart_call(
                "downloads.add",
                source=self.workflow_source.get(),
                destination=self.workflow_destination.get(),
                pattern=self.workflow_pattern.get(),
                unpack=self.workflow_unpack.get(),
                duplicates=self.workflow_duplicates.get(),
            ),
        ).pack(anchor="w", pady=10)
        card = self.card(
            parent,
            "Результаты обработки",
            "Повтор доступен для выбранного файла. Ошибка не выдаётся за готовый результат.",
        )
        self.workflow_results = self._table(card, {"source": "Файл", "status": "Состояние"}, 4)
        self.workflow_result_detail = tk.StringVar()
        self.workflow_results.bind(
            "<<TreeviewSelect>>",
            lambda e: self._smart_details(
                "workflow_results", "results", self.workflow_result_detail, "result"
            ),
        )
        self.label(card, textvariable=self.workflow_result_detail, wraplength=680).pack(fill="x")
        ttk.Button(
            card,
            text="Повторить при AFK",
            command=lambda: self._smart_selected("workflow_results", "downloads.retry"),
        ).pack(anchor="w", pady=8)
        self._smart_refresh(force=True)

    @staticmethod
    def _smart_folder(variable):
        value = filedialog.askdirectory(title="Выберите папку")
        if value:
            variable.set(value)

    def _smart_pick_objects(self):
        paths = filedialog.askopenfilenames(title="Файлы для команды «упакуй это»")
        if paths:
            self._smart_call(
                "objects.select",
                done=lambda result: self.object_info.set(
                    "Выбрано: " + ", ".join(Path(p).name for p in result["paths"])
                ),
                paths=list(paths),
                folder=str(Path(paths[0]).parent),
            )

    def _smart_toggle_rule(self):
        ids = self.download_rule_tree.selection()
        row = next(
            (r for r in getattr(self, "_smart_data", {}).get("downloads", []) if r["id"] in ids),
            None,
        )
        if row:
            self._smart_call("downloads.change", ident=row["id"], enabled=not row["enabled"])

    def _smart_pick_object_folder(self):
        path = filedialog.askdirectory(title="Папка для файловой команды")
        if path:
            self._smart_call(
                "objects.select",
                paths=[path],
                folder=path,
                done=lambda result: self.object_info.set("Выбрана папка: " + result["folder"]),
            )

    def _smart_selected(self, tree_name, action):
        ids = getattr(self, tree_name).selection()
        if ids:
            self._smart_call(
                action, **{("ident" if action == "downloads.change" else "id"): ids[0]}
            )

    def _download_rule_details(self):
        ids = self.download_rule_tree.selection()
        row = next((r for r in self._smart_data.get("downloads", []) if r["id"] in ids), None)
        if row:
            self.download_rule_detail.set(
                f"{row['source']} → {row['destination']}\nZIP: {'распаковать' if row['unpack'] else 'сохранить'} · Дубликаты: {'проверять' if row['duplicates'] else 'не искать'}"
            )

    def build_watches_tab(self):
        parent = self.page_bodies["watches"]
        card = self.card(
            parent,
            "Крест дождётся результата",
            "Наблюдение включается для выбранной операции. Steam проверяется по локальному манифесту; копирование Крест — по результату проверки файлов.",
        )
        self.watch_tree = self._table(card, {"name": "Задача", "status": "Состояние"}, 5)
        self.watch_detail = tk.StringVar()
        self.watch_tree.bind(
            "<<TreeviewSelect>>",
            lambda e: self._smart_details("watch_tree", "watches", self.watch_detail, "detail"),
        )
        self.label(card, textvariable=self.watch_detail, wraplength=680).pack(fill="x")
        self._buttons(
            card,
            [
                ("Отменить выбранное", lambda: self._smart_selected("watch_tree", "watch.cancel")),
                ("Обновить", lambda: self._smart_refresh(force=True)),
            ],
        )
        card = self.card(
            parent,
            "Новое наблюдение",
            "Для файла проверяется закрытие и стабильность две минуты. Закрытие чужого процесса сообщает только о завершении процесса — успех не предполагается.",
        )
        self.watch_kind = tk.StringVar(value="Игра Steam")
        self.watch_target = tk.StringVar()
        self.watch_telegram = tk.BooleanVar(value=False)
        self.watch_shutdown = tk.BooleanVar(value=False)
        combo = ttk.Combobox(
            card,
            textvariable=self.watch_kind,
            values=["Игра Steam", "Задача Крест", "Файл", "Папка", "Процесс"],
            state="readonly",
        )
        combo.pack(fill="x", pady=6)
        combo.bind("<<ComboboxSelected>>", lambda e: self._watch_choices_changed())
        self.watch_choice_combo = ttk.Combobox(
            card, textvariable=self.watch_target, state="readonly"
        )
        self.watch_choice_combo.pack(fill="x", pady=6)
        self._buttons(
            card,
            [
                ("Обновить список", self._watch_load_choices),
                ("Выбрать файл…", self._watch_pick_file),
                ("Выбрать папку…", self._watch_pick_folder),
            ],
        )
        ttk.Checkbutton(
            card, text="Отправить результат владельцу Telegram-пульта", variable=self.watch_telegram
        ).pack(anchor="w", pady=5)
        self.watch_power_button = ttk.Checkbutton(
            card,
            text="После подтверждённого успеха выключить ПК, когда он свободен",
            variable=self.watch_shutdown,
        )
        self.watch_power_button.pack(anchor="w", pady=5)
        self.label(
            card,
            "Перед выключением остаётся 60 секунд на «Крест, отмени выключение». При возвращении новые действия ожидают следующего AFK.",
            muted=True,
            wraplength=680,
        ).pack(fill="x", pady=6)
        ttk.Button(card, text="Наблюдать", style="Accent.TButton", command=self._watch_add).pack(
            anchor="w", pady=10
        )
        self._watch_load_choices()
        self._smart_refresh(force=True)

    def _watch_load_choices(self):
        self._smart_call("watch.choices", done=self._watch_loaded)

    def _watch_loaded(self, rows):
        self._watch_choices = rows
        self._watch_choices_changed()

    def _watch_choices_changed(self):
        kind = self.watch_kind.get()
        data = getattr(self, "_watch_choices", {})
        if kind == "Игра Steam":
            self._watch_targets = {
                r["name"] + " · " + r["appid"]: (
                    str(Path(r["library"]) / "steamapps" / ("appmanifest_" + r["appid"] + ".acf")),
                    r["name"],
                )
                for r in data.get("games", [])
            }
        elif kind == "Задача Крест":
            self._watch_targets = {
                r["label"] + " · " + r["id"][:8]: (r["id"], r["label"])
                for r in data.get("jobs", [])
            }
        elif kind == "Процесс":
            self._watch_targets = {
                r["name"] + " · PID " + str(r["pid"]): (r["pid"], r["name"])
                for r in data.get("processes", [])
            }
        else:
            self._watch_targets = {}
        choices = list(self._watch_targets)
        self.watch_choice_combo.configure(values=choices)
        self.watch_target.set(choices[0] if choices else "")
        self.watch_power_button.configure(
            state="normal" if kind in {"Игра Steam", "Задача Крест"} else "disabled"
        )
        self.watch_shutdown.set(False)

    def _watch_pick_file(self):
        path = filedialog.askopenfilename(title="Файл, за готовностью которого следить")
        if path:
            self.watch_kind.set("Файл")
            self._watch_choices_changed()
            self._watch_targets = {path: (path, Path(path).name)}
            self.watch_choice_combo.configure(values=[path])
            self.watch_target.set(path)

    def _watch_add(self):
        selection = self._watch_targets.get(self.watch_target.get())
        if not selection:
            self.status_var.set("Выберите конкретную задачу или файл")
            return
        kind = {
            "Игра Steam": "steam",
            "Задача Крест": "duty",
            "Файл": "file",
            "Папка": "folder",
            "Процесс": "process",
        }[self.watch_kind.get()]
        self._smart_call(
            "watch.add",
            kind=kind,
            target=selection[0],
            label=selection[1],
            telegram=self.watch_telegram.get(),
            shutdown=self.watch_shutdown.get(),
        )

    def _watch_pick_folder(self):
        path = filedialog.askdirectory(title="Папка для наблюдения за загрузками")
        if path:
            self.watch_kind.set("Папка")
            self._watch_choices_changed()
            self._watch_targets = {path: (path, "Файлы: " + Path(path).name)}
            self.watch_choice_combo.configure(values=[path])
            self.watch_target.set(path)

    def build_sound_tab(self):
        card = self.card(
            self.page_bodies["sound"],
            "Звуковой диспетчер",
            "Сохраните нужную громкость и устройства один раз. Крест применяет профиль при смене режима и для новых аудиосессий; ручные изменения внутри режима сохраняются.",
        )
        cfg = self.config_mgr.snapshot()["sound_dispatcher"]
        self.sound_vars = {k: tk.BooleanVar(value=v) for k, v in cfg.items()}
        for key, caption in (
            ("enabled", "Автоматически переключать сохранённые профили"),
            ("auto_calls", "Определять звонки Discord, Zoom, Teams, Skype и Telegram"),
            ("restore_devices", "Восстанавливать сохранённые устройства ввода и вывода"),
        ):
            ttk.Checkbutton(
                card,
                text=caption,
                variable=self.sound_vars[key],
                command=lambda: self._smart_call(
                    "settings",
                    section="sound_dispatcher",
                    options={k: v.get() for k, v in self.sound_vars.items()},
                ),
            ).pack(anchor="w", pady=5)
        self.sound_status = tk.StringVar()
        self.label(card, textvariable=self.sound_status, wraplength=680).pack(fill="x", pady=8)
        self.sound_tree = self._table(
            card, {"name": "Профиль", "apps": "Приложений", "devices": "Устройства"}, 4
        )
        self.sound_profile = tk.StringVar(value="Обычный")
        ttk.Combobox(
            card,
            textvariable=self.sound_profile,
            values=list(PROFILE_NAMES.values()),
            state="readonly",
        ).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Запомнить текущий звук", lambda: self._sound_action("audio.capture")),
                ("Применить сейчас", lambda: self._sound_action("audio.apply")),
                ("Удалить профиль", lambda: self._sound_action("audio.delete")),
            ],
        )
        self.label(
            card,
            "Сначала сохраните обычный профиль. Затем настройте приложения для игры / звонка / наушников и сохраните соответствующий профиль. Звонок определяется по активному захвату микрофона, а не по запущенному процессу.\nГолосом: «запомни звук для игры», «приглуши всё кроме Дискорда», «верни обычный звук».",
            muted=True,
            wraplength=680,
        ).pack(fill="x", pady=8)
        self._smart_refresh(force=True)

    def _sound_action(self, action):
        key = next(k for k, v in PROFILE_NAMES.items() if v == self.sound_profile.get())
        self._smart_call(action, profile=key)

    def build_habits_tab(self):
        card = self.card(
            self.page_bodies["habits"],
            "Привычки, которые можно автоматизировать",
            "Крест ищет одинаковые цепочки из 2–5 успешных команд после запуска, игры или подключения наушников. После трёх повторений в отдельных сессиях предлагает правило. Экран и нажатия не записываются.",
        )
        self.habits_enabled = tk.BooleanVar(
            value=self.config_mgr.snapshot()["smart_tools"]["learn_routines"]
        )
        ttk.Checkbutton(
            card,
            text="Замечать повторяющиеся команды Крест",
            variable=self.habits_enabled,
            command=lambda: self._smart_call(
                "settings",
                section="smart_tools",
                options={"learn_routines": self.habits_enabled.get()},
            ),
        ).pack(anchor="w", pady=6)
        self.habit_tree = self._table(
            card, {"name": "Когда", "count": "Повторений", "state": "Состояние"}, 6
        )
        self.habit_detail = tk.StringVar(
            value="Выберите предложение: здесь появятся все шаги до включения."
        )
        self.habit_tree.bind("<<TreeviewSelect>>", lambda e: self._habit_details())
        self.label(card, textvariable=self.habit_detail, wraplength=680).pack(fill="x", pady=10)
        self._buttons(
            card,
            [
                (
                    "Включить выбранное",
                    lambda: self._smart_selected("habit_tree", "habit.activate"),
                ),
                (
                    "Не предлагать / отключить",
                    lambda: self._smart_selected("habit_tree", "habit.dismiss"),
                ),
                ("Очистить наблюдения", lambda: self._smart_call("habit.clear")),
            ],
        )
        self.label(
            card,
            "Запуск программ, точная громкость и звуковые режимы. Удаление файлов, выключение ПК и отправка сообщений в такие предложения не попадают. Включённые сценарии также доступны в «Событиях».",
            muted=True,
            wraplength=680,
        ).pack(fill="x", pady=8)
        self._smart_refresh(force=True)

    def _habit_details(self):
        ids = self.habit_tree.selection()
        row = next((r for r in self._smart_data.get("habits", []) if r["id"] in ids), None)
        if row:
            self.habit_detail.set(
                TRIGGERS[row["event"]]
                + (" · " + row["match"] if row["match"] else "")
                + "\n"
                + "\n".join(f"{i}. {s}" for i, s in enumerate(row["steps"], 1))
            )

    def _smart_details(self, tree, section, variable, field):
        if not getattr(self, tree).winfo_exists():
            return
        ids = getattr(self, tree).selection()
        row = next((r for r in self._smart_data.get(section, []) if r["id"] in ids), None)
        if row:
            value = row.get(field, "") + ("\n" + row["delivery"] if row.get("delivery") else "")
            if variable.get() != value:
                variable.set(value)

    def _smart_refresh(self, force=False):
        if getattr(self, "_smart_busy", False):
            return
        if not force and (
            self.current_page not in SMART_PAGES
            or self.state() not in ("normal", "zoomed")
            or time.monotonic() - getattr(self, "_smart_at", 0) < 5
        ):
            return
        self._smart_at = time.monotonic()
        self._smart_busy = True

        def work():
            try:
                return self.runtime.smart.snapshot()
            finally:
                self._smart_busy = False

        self._background(work, self._smart_display)

    def _smart_rows(self, name, rows):
        tree = getattr(self, name, None)
        if tree is None or not tree.winfo_exists():
            return
        if not hasattr(tree, "_smart_max_height"):
            tree._smart_max_height = int(tree.cget("height"))
        height = min(tree._smart_max_height, max(1, len(rows)))
        if int(tree.cget("height")) != height:
            tree.configure(height=height)
        keys = {ident for ident, _ in rows}
        for ident in tree.get_children():
            if ident not in keys:
                tree.delete(ident)
        for ident, values in rows:
            if not tree.exists(ident):
                tree.insert("", "end", iid=ident, values=values)
            elif tuple(str(v) for v in tree.item(ident, "values")) != tuple(str(v) for v in values):
                tree.item(ident, values=values)

    def _smart_display(self, data):
        if getattr(self, "closing", False) or getattr(self, "_destroyed", False):
            return
        self._smart_data = data
        self._smart_rows(
            "download_rule_tree",
            [
                (
                    r["id"],
                    (
                        r["pattern"],
                        Path(r["source"]).name,
                        Path(r["destination"]).name,
                        "Включено" if r["enabled"] else "Пауза",
                    ),
                )
                for r in data["downloads"]
            ],
        )
        self._smart_rows(
            "workflow_results",
            [
                (
                    r["id"],
                    (
                        Path(r["source"]).name,
                        {
                            "completed": "Готово",
                            "waiting": "Ожидает AFK",
                            "retry": "Повтор позже",
                            "failed": "Ошибка",
                        }.get(r.get("status"), "В обработке"),
                    ),
                )
                for r in data["results"]
            ],
        )
        self._smart_rows(
            "watch_tree", [(r["id"], (r["label"], STATES[r["status"]])) for r in data["watches"]]
        )
        self._smart_rows(
            "habit_tree",
            [
                (
                    r["id"],
                    (
                        TRIGGERS[r["event"]] + (" · " + r["match"] if r["match"] else ""),
                        r["count"],
                        {
                            "learning": "Изучаю",
                            "suggested": "Можно включить",
                            "enabled": "Включено",
                            "dismissed": "Отключено",
                        }[r["status"]],
                    ),
                )
                for r in data["habits"]
            ],
        )
        self._smart_rows(
            "sound_tree",
            [
                (
                    r["id"],
                    (
                        PROFILE_NAMES[r["id"]],
                        len(r["levels"]),
                        " · ".join(
                            d["name"]
                            for d in r["devices"]
                            if d["id"] in {r.get("output"), r.get("input")}
                        ),
                    ),
                )
                for r in data["audio_profiles"]
            ],
        )
        if hasattr(self, "sound_status") and self.sound_status.get() != data["audio_status"]:
            self.sound_status.set(data["audio_status"])
        for name, section, variable, field in (
            ("workflow_results", "results", "workflow_result_detail", "result"),
            ("watch_tree", "watches", "watch_detail", "detail"),
        ):
            if hasattr(self, name):
                self._smart_details(name, section, getattr(self, variable), field)
