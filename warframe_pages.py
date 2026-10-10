"""Warframe workspace. All network/disk requests run away from Tk's event thread."""

from ui_fonts import UI_FONT

import time
import tkinter as tk
from tkinter import ttk, filedialog
from ui_dialogs import messagebox
from warframe_api import CYCLES, local_time, remaining
from warframe_config import DEFAULT_WARFRAME

PAGE_NAMES = [
    ("wf_home", "Навигатор"),
    ("wf_goals", "Цели и дела"),
    ("wf_arsenal", "Арсенал"),
    ("wf_market", "Рынок"),
    ("wf_journal", "Кузница и журнал"),
    ("wf_settings", "Помощник"),
]
CATEGORIES = {
    "Варфреймы": "Warframes",
    "Основное оружие": "Primary",
    "Вторичное оружие": "Secondary",
    "Ближний бой": "Melee",
    "Стражи": "Sentinels",
    "Арчвинг": "Archwing",
    "Ресурсы": "Resources",
    "Компоненты и чертежи": "Misc",
    "Реликвии": "Relics",
    "Моды": "Mods",
}
PERIODS = {"Ежедневно": "daily", "Еженедельно": "weekly", "Один раз": "once"}


class WarframePages:
    def init_warframe(self):
        cfg = self.config_mgr.snapshot()["warframe"]
        self.wf_vars = {
            key: (
                tk.BooleanVar(self, value=value)
                if isinstance(value, bool)
                else tk.StringVar(self, value=value)
            )
            for key, value in cfg.items()
            if key != "cycles"
        }
        self.wf_cycles = {key: tk.BooleanVar(self, value=key in cfg["cycles"]) for key in CYCLES}
        self.wf_fields = {}
        self.wf_rows = {}
        self.wf_data = {}
        self.wf_outputs = {}
        self.wf_texts = {}
        self.wf_refreshing = False
        self.wf_refreshed = 0
        self.wf_vision = tk.StringVar(self, value="Ожидаю активное окно Warframe")
        self.wf_status = tk.StringVar(self, value="Игра не обнаружена")
        self.wf_source = tk.StringVar(
            self, value="Данные мира ещё не запрошены. Личные записи доступны без сети."
        )
        for var in [*self.wf_vars.values(), *self.wf_cycles.values()]:
            var.trace_add("write", self._mark_dirty)

    def wf_var(self, key, default=""):
        if key not in self.wf_fields:
            self.wf_fields[key] = tk.StringVar(self, value=default)
        return self.wf_fields[key]

    def wf_form(self, parent, fields):
        frame = tk.Frame(parent, bg=parent.cget("bg"))
        frame.pack(fill="x", pady=(4, 8))
        for i, (key, label, default, options) in enumerate(fields):
            cell = tk.Frame(frame, bg=parent.cget("bg"))
            cell.grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, 10), pady=5)
            frame.columnconfigure(i % 3, weight=1, uniform="form")
            self.label(cell, label, muted=True).pack(anchor="w", pady=(0, 4))
            var = self.wf_var(key, default)
            widget = (
                ttk.Combobox(cell, textvariable=var, values=options, state="readonly", width=18)
                if options
                else ttk.Entry(cell, textvariable=var, width=18)
            )
            widget.pack(fill="x")
        return frame

    def wf_buttons(self, parent, buttons):
        frame = tk.Frame(parent, bg=parent.cget("bg"))
        frame.pack(fill="x", pady=(8, 10))
        for i, (label, command) in enumerate(buttons):
            ttk.Button(
                frame, text=label, command=command, style="Accent.TButton" if i == 0 else "TButton"
            ).grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, 8), pady=(0, 6))
            frame.columnconfigure(i % 3, weight=1)

    def wf_table(self, parent, key, columns, height=5):
        frame = tk.Frame(parent, bg=parent.cget("bg"))
        frame.pack(fill="x", pady=6)
        tree = ttk.Treeview(
            frame,
            columns=[c[0] for c in columns],
            show="headings",
            height=height,
            selectmode="browse",
        )
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=60)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y")
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(fill="x", expand=True)
        if not hasattr(self, "wf_trees"):
            self.wf_trees = {}
        self.wf_trees[key] = tree
        return tree

    def wf_text(self, parent, key, height=8, editable=False):
        frame = tk.Frame(parent, bg=parent.cget("bg"))
        frame.pack(fill="x", pady=6)
        text = tk.Text(
            frame,
            height=height,
            wrap="word",
            bg=self.theme["sidebar"],
            fg=self.theme["text"],
            insertbackground=self.theme["accent"],
            selectbackground=self.theme["accent"],
            relief="flat",
            padx=14,
            pady=12,
            font=(UI_FONT, 10),
        )
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="x", expand=True)
        if editable:
            text.insert("1.0", self.wf_var(key).get())
            self.wf_texts[key] = text
        else:
            text.insert(
                "1.0", self.wf_outputs.get(key, "Выберите действие — результат появится здесь.")
            )
            text.configure(state="disabled")
            if not hasattr(self, "wf_output_widgets"):
                self.wf_output_widgets = {}
            self.wf_output_widgets[key] = text
        return text

    def wf_preserve(self):
        for key, widget in self.wf_texts.items():
            if widget.winfo_exists():
                self.wf_var(key).set(widget.get("1.0", "end-1c"))

    def wf_output(self, text, page=None):
        key = page or (self.current_page if self.current_page.startswith("wf_") else "wf_home")
        self.wf_outputs[key] = str(text)
        widget = getattr(self, "wf_output_widgets", {}).get(key)
        if widget and widget.winfo_exists():
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", str(text))
            widget.configure(state="disabled")

    def wf_request(self, action, values):
        if hasattr(self.runtime, "client"):
            client = self.runtime.client
            ident = client.call("warframe.request", action=action, values=values)
            fetch = lambda: client.call("warframe.result", id=ident)
        else:
            ident = self.runtime.warframe.request(action, values)
            fetch = lambda: self.runtime.warframe.result(ident)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            value = fetch()
            if value["status"] == "failed":
                raise ValueError(value["error"])
            if value["status"] == "completed":
                return value["result"]
            time.sleep(0.2)
        raise RuntimeError(
            "Запрос ещё выполняется. Результат можно проверить повторным действием позже."
        )

    def wf_call(self, action, done=None, **values):
        page = self.current_page if self.current_page.startswith("wf_") else "wf_home"
        self.wf_output("Выполняю…", page)

        def work():
            try:
                result = self.wf_request(action, values)
            except Exception as exc:
                self.post_ui(
                    lambda error=str(exc): self.wf_output("Не удалось выполнить: " + error, page)
                )
                return

            def finish():
                info = result.get("info", {})
                prefix = (
                    "Сохранённые данные, актуальность не подтверждена.\n"
                    if result.get("stale") or info.get("stale")
                    else ""
                )
                self.wf_output(prefix + result.get("text", "Готово."), page)
                if done:
                    done(result)
                self.wf_refresh()

            self.post_ui(finish)

        self._background(work)

    def wf_refresh(self):
        if self.wf_refreshing:
            return
        self.wf_refreshing = True
        self.wf_refreshed = time.monotonic()

        def work():
            try:
                data = (
                    self.runtime.client.call("warframe.snapshot")
                    if hasattr(self.runtime, "client")
                    else self.runtime.warframe.snapshot()
                )
                self.post_ui(lambda: self.wf_render(data))
            except Exception as exc:
                self.post_ui(lambda error=str(exc): self.wf_source.set(error))
            finally:
                self.post_ui(lambda: setattr(self, "wf_refreshing", False))

        self._background(work)

    def wf_fill(self, key, rows, values):
        tree = self.wf_trees[key]
        selected = tree.selection()
        position = tree.yview()
        mapping = {}
        prepared = []
        for i, row in enumerate(rows):
            ident = str(row.get("id") or i)
            while ident in mapping or ident == "_empty":
                ident += "_" + str(i)
            mapping[ident] = row
            prepared.append((ident, tuple(str(v) for v in values(row))))
        existing = [
            (ident, tree.item(ident, "values"))
            for ident in tree.get_children()
            if ident != "_empty"
        ]
        if existing != prepared or not prepared and not tree.get_children():
            tree.delete(*tree.get_children())
            for ident, cells in prepared:
                tree.insert("", "end", iid=ident, values=cells)
            if not prepared:
                tree.insert(
                    "",
                    "end",
                    iid="_empty",
                    values=("Пока нет записей",) + ("",) * (len(tree["columns"]) - 1),
                )
            if selected and selected[0] in mapping:
                tree.selection_set(selected[0])
            if position:
                tree.yview_moveto(position[0])
        self.wf_rows[key] = mapping

    def wf_selected(self, key):
        selected = self.wf_trees[key].selection()
        if not selected:
            self.wf_output("Сначала выберите запись в списке.")
            return None
        return self.wf_rows.get(key, {}).get(selected[0])

    def wf_selected_action(self, key, action, **values):
        row = self.wf_selected(key)
        if row:
            self.wf_call(action, id=row["id"], **values)

    def wf_delete(self, key, section=None):
        row = self.wf_selected(key)
        if row and messagebox.askyesno("Удалить запись", f"Удалить «{row['name']}»?", parent=self):
            self.wf_call("remove", section=section or key, id=row["id"])

    def wf_render(self, data):
        self.wf_data = data
        state = data["state"]
        status = data["status"]
        label = (
            "WARFRAME ОБНАРУЖЕН · КРЕСТ РЯДОМ"
            if status["running"]
            else "ГОТОВА К ИГРЕ · WARFRAME НЕ ЗАПУЩЕН"
        )
        if status["busy"]:
            label += " · " + status["operation"]
        self.wf_vision.set(
            data.get("status", {}).get("vision", {}).get("status", "Ожидаю активное окно Warframe")
        )
        self.wf_status.set(label)
        source = (
            ("Сохранённые данные" if status["stale"] else "Обновлено")
            + " · "
            + local_time(status["updated"])
            if status["updated"]
            else "Нажмите «Обновить мир» для первой сводки."
        )
        if status["error"]:
            source += "\n" + status["error"][:220]
        if status.get("overlay_error"):
            source += "\nИндикатор: " + status["overlay_error"]
        self.wf_source.set(source)
        self.wf_fill("cycles", data["cycles"], lambda r: (r["name"], r["state"], r["remaining"]))
        self.wf_render_events()
        self.wf_fill(
            "goals",
            state["goals"],
            lambda r: (
                r["name"],
                f"{r.get('progress', 0)}/{r.get('target', 1)}",
                "Готово"
                if r.get("done")
                else f"{r.get('minutes', 15)} мин · приоритет {r.get('priority', 2)}",
            ),
        )
        self.wf_fill(
            "tasks",
            state["tasks"],
            lambda r: (
                r["name"],
                "Готово" if r["done_now"] else "Ожидает",
                local_time(r["next_reset"]) if r["next_reset"] else "Без повтора",
            ),
        )
        self.wf_fill(
            "foundry",
            state["foundry"],
            lambda r: (
                r["name"],
                "Получено"
                if r.get("claimed")
                else remaining(r["ends"] - time.time())
                if r["ends"] > time.time()
                else "Проверьте в игре",
                local_time(r["ends"]),
            ),
        )
        self.wf_fill(
            "builds",
            state["builds"],
            lambda r: (r["name"], len(r["mods"]), r.get("notes", "")[:80]),
        )
        self.wf_fill(
            "watches",
            state["watches"],
            lambda r: (
                r["name"],
                str(r["price"]) + " пл.",
                "Включено" if r.get("enabled") else "Пауза",
            ),
        )
        self.wf_fill(
            "journal",
            list(reversed(state["journal"])),
            lambda r: (local_time(r["created"]), r["name"], r.get("notes", "")[:90]),
        )
        self.wf_fill(
            "trades",
            list(reversed(state["trades"])),
            lambda r: (
                r["name"],
                "Продажа" if r.get("kind") == "sell" else "Покупка",
                f"{r.get('quantity', 1)} × {r.get('price', 0)}",
            ),
        )
        owned = [
            {"id": "item:" + k, "name": k, "value": v, "kind": "Ресурс / предмет"}
            for k, v in state["inventory"].items()
        ]
        owned += [
            {"id": "mod:" + k, "name": k, "value": v, "kind": "Ранг мода"}
            for k, v in state["mod_ranks"].items()
        ]
        self.wf_fill("collection", owned, lambda r: (r["name"], r["value"], r["kind"]))

    def wf_render_events(self, *_):
        rows = self.wf_data.get("events", [])
        query = self.wf_var("event_filter").get().casefold().strip()
        self.wf_fill(
            "events",
            [
                r
                for r in rows
                if query in (r["name"] + " " + r["reward"] + " " + r["kind"]).casefold()
            ],
            lambda r: (r["kind"], r["name"], r["reward"]),
        )

    def build_warframe_pages(self):
        self.wf_trees = {}
        self.wf_output_widgets = {}
        self.wf_texts = {}
        self.build_wf_home()
        self.build_wf_goals()
        self.build_wf_arsenal()
        self.build_wf_market()
        self.build_wf_journal()
        self.build_wf_settings()
        if self.wf_data:
            self.wf_render(self.wf_data)

    def build_wf_home(self):
        page = self.page_bodies["wf_home"]
        card = self.card(
            page,
            "Ваш второй пилот",
            "Мир игры, личные планы и важные события. Цели и таймеры остаются с вами после закрытия меню.",
        )
        self.label(card, textvariable=self.wf_status, muted=True).pack(anchor="w", pady=(0, 12))
        self.label(card, textvariable=self.wf_vision, muted=True, wraplength=650).pack(
            anchor="w", pady=(0, 10)
        )
        self.wf_buttons(
            card,
            [
                ("Обновить мир", lambda: self.wf_call("refresh")),
                ("Запустить Warframe", lambda: self.wf_call("launch")),
                ("Мои цели", lambda: self.switch_page("wf_goals")),
            ],
        )
        self.label(card, textvariable=self.wf_source, muted=True, wraplength=670).pack(anchor="w")
        card = self.card(
            page,
            "Ритм открытых миров",
            "Показываем время из источника. Просроченная фаза не продлевается догадкой.",
        )
        self.wf_table(
            card,
            "cycles",
            [("world", "Мир", 290), ("state", "Фаза", 140), ("left", "До смены", 130)],
            4,
        )
        card = self.card(
            page, "Сейчас в системе", "Разломы, вторжения, события, вылазка, Архонт и Ночная Волна."
        )
        entry = ttk.Entry(card, textvariable=self.wf_var("event_filter"))
        entry.pack(fill="x", pady=4)
        entry.bind("<KeyRelease>", self.wf_render_events)
        self.wf_table(
            card,
            "events",
            [
                ("kind", "Активность", 130),
                ("name", "Где / задача", 330),
                ("reward", "Награда", 180),
            ],
            7,
        )
        self.wf_buttons(
            card,
            [
                ("Баро и покупки", lambda: self.wf_call("baro")),
                ("Ночная Волна", lambda: self.wf_call("nightwave")),
                ("Добавить в дела", self.wf_event_task),
            ],
        )
        self.wf_text(card, "wf_home", 10)

    def wf_event_task(self):
        row = self.wf_selected("events")
        if row:
            self.wf_call("add_task", name=row["name"][:200], period="once", notes=row["reward"])

    def build_wf_goals(self):
        page = self.page_bodies["wf_goals"]
        card = self.card(
            page,
            "К чему идём",
            "Для плана сессии используются ваши оценки времени. Приоритет 5 — самый высокий.",
        )
        self.wf_form(
            card,
            [
                ("goal_name", "Цель", "", None),
                ("goal_target", "Нужно, шт.", "1", None),
                ("goal_minutes", "Около минут", "15", None),
                ("goal_priority", "Приоритет", "2", ["1", "2", "3", "4", "5"]),
                ("goal_progress", "Уже сделано", "1", None),
                ("plan_minutes", "Есть времени, мин", "30", None),
            ],
        )
        self.wf_buttons(
            card,
            [
                (
                    "Добавить цель",
                    lambda: self.wf_call(
                        "add_goal",
                        name=self.wf_var("goal_name").get(),
                        target=self.wf_var("goal_target").get(),
                        minutes=self.wf_var("goal_minutes").get(),
                        priority=self.wf_var("goal_priority").get(),
                    ),
                ),
                (
                    "Обновить прогресс",
                    lambda: self.wf_selected_action(
                        "goals", "progress", value=self.wf_var("goal_progress").get()
                    ),
                ),
                (
                    "План на сессию",
                    lambda: self.wf_call("plan", minutes=self.wf_var("plan_minutes").get()),
                ),
            ],
        )
        self.wf_table(
            card,
            "goals",
            [("name", "Цель", 340), ("progress", "Прогресс", 100), ("state", "Оценка", 180)],
        )
        ttk.Button(
            card, text="Удалить выбранную цель", command=lambda: self.wf_delete("goals")
        ).pack(anchor="e")
        self.wf_buttons(
            card,
            [
                ("Загрузить для правки", lambda: self.wf_load_record("goals")),
                ("Сохранить правки", self.wf_edit_goal),
            ],
        )
        card = self.card(
            page,
            "Игровые привычки",
            "Сброс по UTC: ежедневный или в понедельник. Отметка сохраняется для текущего периода.",
        )
        self.wf_form(
            card,
            [
                ("task_name", "Задача", "", None),
                ("task_period", "Повтор", "Ежедневно", list(PERIODS)),
                ("task_hour", "Час сброса UTC", "0", [str(i) for i in range(24)]),
                ("task_minutes", "Около минут", "15", None),
            ],
        )
        self.wf_buttons(
            card,
            [
                (
                    "Добавить задачу",
                    lambda: self.wf_call(
                        "add_task",
                        name=self.wf_var("task_name").get(),
                        period=PERIODS[self.wf_var("task_period").get()],
                        reset_hour=self.wf_var("task_hour").get(),
                        minutes=self.wf_var("task_minutes").get(),
                    ),
                ),
                ("Выполнено", lambda: self.wf_selected_action("tasks", "complete_task")),
                ("Удалить", lambda: self.wf_delete("tasks")),
            ],
        )
        self.wf_table(
            card,
            "tasks",
            [
                ("name", "Задача", 330),
                ("state", "Состояние", 110),
                ("reset", "Следующий сброс", 180),
            ],
        )
        self.wf_buttons(
            card,
            [
                ("Загрузить для правки", lambda: self.wf_load_record("tasks")),
                ("Сохранить правки", self.wf_edit_task),
                (
                    "Вернуть в дела",
                    lambda: self.wf_selected_action("tasks", "complete_task", completed=False),
                ),
            ],
        )
        self.wf_text(card, "wf_goals", 7)
        card = self.card(
            page,
            "Моя коллекция",
            "Отмечайте наличие ресурсов и ранги модов. Для кошелька используйте ducats и credits. Игра не предоставляет Крест личный инвентарь через публичный API.",
        )
        self.wf_form(
            card,
            [
                ("owned_name", "Название", "", None),
                ("owned_value", "Количество / ранг", "0", None),
                (
                    "owned_kind",
                    "Что сохраняем",
                    "Ресурс / предмет",
                    ["Ресурс / предмет", "Ранг мода"],
                ),
            ],
        )
        self.wf_buttons(
            card,
            [
                (
                    "Запомнить наличие",
                    lambda: self.wf_call(
                        "collection",
                        name=self.wf_var("owned_name").get(),
                        quantity=self.wf_var("owned_value").get(),
                        mod=self.wf_var("owned_kind").get() == "Ранг мода",
                    ),
                )
            ],
        )
        self.wf_table(
            card,
            "collection",
            [("name", "Предмет", 330), ("value", "Наличие", 100), ("kind", "Тип записи", 190)],
            4,
        )
        self.wf_form(
            card,
            [
                ("alias_ru", "Как я называю", "", None),
                ("alias_en", "Название в справочнике", "", None),
            ],
        )
        ttk.Button(
            card,
            text="Запомнить название",
            command=lambda: self.wf_call(
                "alias", name=self.wf_var("alias_ru").get(), value=self.wf_var("alias_en").get()
            ),
        ).pack(anchor="w")

    def build_wf_arsenal(self):
        page = self.page_bodies["wf_arsenal"]
        card = self.card(
            page,
            "Арсенал и маршрут фарма",
            "Справочник WFCD загружается по разделам и хранится локально. Подойдут английские названия и ваши соответствия.",
        )
        self.wf_form(
            card,
            [
                ("catalog_query", "Что ищем", "", None),
                ("catalog_category", "Раздел", "Варфреймы", list(CATEGORIES)),
            ],
        )
        self.wf_buttons(
            card,
            [
                (
                    "Найти",
                    lambda: self.wf_call(
                        "search",
                        done=lambda r: self.wf_fill(
                            "catalog",
                            r["rows"],
                            lambda x: (x["name"], x.get("type", ""), x.get("description", "")[:90]),
                        ),
                        query=self.wf_var("catalog_query").get(),
                        category=CATEGORIES[self.wf_var("catalog_category").get()],
                    ),
                ),
                (
                    "Обновить справочник",
                    lambda: self.wf_call(
                        "refresh_catalog",
                        category=CATEGORIES[self.wf_var("catalog_category").get()],
                    ),
                ),
                (
                    "Где фармить ресурс",
                    lambda: self.wf_call(
                        "farm", query=self.wf_var("catalog_query").get(), category="Resources"
                    ),
                ),
            ],
        )
        self.wf_table(
            card,
            "catalog",
            [("name", "Название", 230), ("type", "Тип", 110), ("desc", "Описание", 290)],
            6,
        )
        self.wf_buttons(
            card,
            [
                ("Рецепт и нехватка", lambda: self.wf_catalog_action("recipe")),
                ("Таймер по рецепту", self.wf_catalog_timer),
                ("Открыть Wiki", self.wf_open_wiki),
            ],
        )
        self.wf_text(card, "wf_arsenal", 10)
        card = self.card(
            page,
            "Реликвии и Следы Бездны",
            "Награды и шанс для каждого улучшения. Пометка хранилища — из справочника, актуальность зависит от даты его обновления.",
        )
        self.wf_form(
            card,
            [
                ("relic_query", "Награда или реликвия", "", None),
                (
                    "trace_tier",
                    "Улучшение",
                    "radiant",
                    ["intact", "exceptional", "flawless", "radiant"],
                ),
                ("trace_count", "Реликвий", "4", None),
                ("trace_owned", "Следов в наличии", "0", None),
            ],
        )
        self.wf_buttons(
            card,
            [
                (
                    "Подобрать реликвии",
                    lambda: self.wf_call(
                        "relics",
                        done=lambda r: self.wf_fill(
                            "relics",
                            r["rows"],
                            lambda x: (
                                x["relic"],
                                x["item"],
                                str(x["chance"]) + "%",
                                "Да" if x["vaulted"] else "Нет" if x["vaulted"] is False else "?",
                            ),
                        ),
                        query=self.wf_var("relic_query").get(),
                    ),
                ),
                (
                    "Рассчитать следы",
                    lambda: self.wf_call(
                        "traces",
                        tier=self.wf_var("trace_tier").get(),
                        count=self.wf_var("trace_count").get(),
                        owned=self.wf_var("trace_owned").get(),
                    ),
                ),
            ],
        )
        self.wf_table(
            card,
            "relics",
            [
                ("name", "Реликвия", 220),
                ("reward", "Награда", 220),
                ("chance", "Шанс", 70),
                ("vault", "Хранилище", 90),
            ],
            6,
        )
        card = self.card(
            page,
            "Мои сборки",
            "Один мод на строку: Vitality:10. Проверка считает недостающие моды и усиление; совместимость слотов, полярности и вместимость проверяйте в игре.",
        )
        self.wf_form(
            card,
            [
                ("build_name", "Название сборки", "", None),
                ("build_url", "Ссылка https://", "", None),
                ("build_notes", "Комментарий", "", None),
            ],
        )
        self.wf_text(card, "build_mods", 6, True)
        self.wf_buttons(
            card,
            [
                ("Сохранить сборку", self.wf_save_build),
                ("Проверить моды", lambda: self.wf_selected_action("builds", "build_report")),
                ("Удалить сборку", lambda: self.wf_delete("builds")),
            ],
        )
        tree = self.wf_table(
            card,
            "builds",
            [("name", "Сборка", 270), ("mods", "Модов", 80), ("notes", "Комментарий", 280)],
        )
        tree.bind("<Double-1>", lambda e: self.wf_build_details())
        self.wf_buttons(
            card, [("Редактировать", self.wf_edit_build), ("Новая сборка", self.wf_new_build)]
        )

    def wf_load_record(self, section):
        row = self.wf_selected(section)
        if not row:
            return
        prefix = "goal" if section == "goals" else "task"
        for name in ("name", "minutes"):
            self.wf_var(prefix + "_" + name).set(row.get(name, 15))
        if section == "goals":
            for name in ("target", "progress", "priority"):
                self.wf_var("goal_" + name).set(row.get(name, 1))
        else:
            self.wf_var("task_hour").set(row.get("reset_hour", 0))
            self.wf_var("task_period").set(
                next(k for k, v in PERIODS.items() if v == row["period"])
            )

    def wf_edit_goal(self):
        self.wf_selected_action(
            "goals",
            "edit_goal",
            name=self.wf_var("goal_name").get(),
            target=self.wf_var("goal_target").get(),
            minutes=self.wf_var("goal_minutes").get(),
            priority=self.wf_var("goal_priority").get(),
        )

    def wf_edit_task(self):
        self.wf_selected_action(
            "tasks",
            "edit_task",
            name=self.wf_var("task_name").get(),
            period=PERIODS[self.wf_var("task_period").get()],
            reset_hour=self.wf_var("task_hour").get(),
            minutes=self.wf_var("task_minutes").get(),
        )

    def wf_edit_build(self):
        row = self.wf_selected("builds")
        if not row:
            return
        self._wf_edit_build_id = row["id"]
        for key in ("name", "url", "notes"):
            self.wf_var("build_" + key).set(row.get(key, ""))
        widget = self.wf_texts["build_mods"]
        widget.delete("1.0", "end")
        widget.insert("1.0", "\n".join(f"{n}:{r}" for n, r in row["mods"].items()))
        self.wf_output(
            "Редактируется: " + row["name"] + ". Кнопка «Сохранить сборку» обновит её.",
            "wf_arsenal",
        )

    def wf_new_build(self):
        self._wf_edit_build_id = None
        for key in ("build_name", "build_url", "build_notes", "build_mods"):
            self.wf_var(key).set("")
        self.wf_texts["build_mods"].delete("1.0", "end")

    def wf_catalog_action(self, action):
        row = self.wf_selected("catalog")
        if row:
            self.wf_call(
                action,
                query=row["name"],
                category=CATEGORIES[self.wf_var("catalog_category").get()],
            )

    def wf_catalog_timer(self):
        row = self.wf_selected("catalog")
        if row:
            self.wf_call(
                "foundry_start",
                name=row["name"],
                category=CATEGORIES[self.wf_var("catalog_category").get()],
            )

    def wf_open_wiki(self):
        row = self.wf_selected("catalog")
        if row:
            import webbrowser
            from warframe_logic import wiki_url

            webbrowser.open(wiki_url(row["name"]))

    def wf_save_build(self):
        self.wf_preserve()
        self.wf_call(
            "save_build",
            id=getattr(self, "_wf_edit_build_id", None),
            name=self.wf_var("build_name").get(),
            mods=self.wf_var("build_mods").get(),
            notes=self.wf_var("build_notes").get(),
            url=self.wf_var("build_url").get(),
        )

    def wf_build_details(self):
        row = self.wf_selected("builds")
        if row:
            self.wf_output(
                row["name"]
                + "\n"
                + "\n".join(f"{n}:{r}" for n, r in row["mods"].items())
                + "\n"
                + row.get("notes", "")
                + "\n"
                + row.get("url", ""),
                "wf_arsenal",
            )

    def build_wf_market(self):
        page = self.page_bodies["wf_market"]
        card = self.card(
            page,
            "Рынок без лишних окон",
            "Публичные предложения Warframe Market. Вы выбираете продавца и сами отправляете сообщение в игре.",
        )
        self.wf_form(
            card,
            [
                ("market_query", "Предмет", "", None),
                ("market_rank", "Ранг мода", "Любой", ["Любой"] + [str(i) for i in range(11)]),
                ("watch_price", "Оповестить до, пл.", "20", None),
            ],
        )
        self.wf_buttons(
            card,
            [
                (
                    "Найти предмет",
                    lambda: self.wf_call(
                        "market_search",
                        done=lambda r: self.wf_fill(
                            "market_items",
                            r["rows"],
                            lambda x: (x["name"], x.get("ru", ""), x["slug"]),
                        ),
                        query=self.wf_var("market_query").get(),
                    ),
                ),
                ("Предложения продажи", self.wf_orders),
                ("Следить за ценой", self.wf_watch),
            ],
        )
        self.wf_table(
            card,
            "market_items",
            [("name", "Предмет", 240), ("ru", "Название RU", 240), ("slug", "Идентификатор", 160)],
        )
        self.wf_table(
            card,
            "orders",
            [
                ("seller", "Продавец", 230),
                ("price", "Платина", 90),
                ("status", "Статус", 130),
                ("rank", "Ранг / шт.", 130),
            ],
            6,
        )
        ttk.Button(
            card, text="Скопировать сообщение выбранному продавцу", command=self.wf_copy_order
        ).pack(anchor="w", pady=6)
        self.wf_text(card, "wf_market", 7)
        card = self.card(
            page,
            "Сигналы цены",
            "Проверка не чаще двух минут, только свежие предложения. Повторное уведомление — не чаще раза в 30 минут.",
        )
        self.wf_table(
            card,
            "watches",
            [("name", "Предмет", 340), ("price", "Порог", 100), ("state", "Состояние", 180)],
        )
        self.wf_buttons(
            card,
            [
                ("Включить / пауза", self.wf_toggle_watch),
                ("Удалить правило", lambda: self.wf_delete("watches")),
            ],
        )
        card = self.card(
            page,
            "Журнал сделок",
            "Ручной учёт. Цена за одну штуку; баланс вычисляется по последним 300 записанным сделкам.",
        )
        self.wf_form(
            card,
            [
                ("trade_name", "Предмет", "", None),
                ("trade_kind", "Операция", "Продажа", ["Продажа", "Покупка"]),
                ("trade_price", "Платина за шт.", "0", None),
                ("trade_quantity", "Штук", "1", None),
                ("trade_notes", "Заметка", "", None),
            ],
        )
        ttk.Button(
            card,
            text="Записать сделку",
            command=lambda: self.wf_call(
                "trade",
                name=self.wf_var("trade_name").get(),
                kind="sell" if self.wf_var("trade_kind").get() == "Продажа" else "buy",
                price=self.wf_var("trade_price").get(),
                quantity=self.wf_var("trade_quantity").get(),
                notes=self.wf_var("trade_notes").get(),
            ),
        ).pack(anchor="w", pady=6)
        self.wf_table(
            card,
            "trades",
            [("name", "Предмет", 350), ("kind", "Операция", 120), ("sum", "Шт. × платина", 170)],
        )
        ttk.Button(
            card, text="Удалить ошибочную запись", command=lambda: self.wf_delete("trades")
        ).pack(anchor="e")

    def wf_market_selection(self):
        return self.wf_selected("market_items")

    def wf_orders(self):
        row = self.wf_market_selection()
        if not row:
            return

        def done(result):
            self._wf_order_item = row
            self.wf_fill(
                "orders",
                result["rows"],
                lambda r: (
                    r["seller"],
                    r["price"],
                    r["status"],
                    f"{r.get('rank', '—')} / {r['quantity']}",
                ),
            )

        self.wf_call("orders", done=done, slug=row["slug"], rank=self.wf_var("market_rank").get())

    def wf_watch(self):
        row = self.wf_market_selection()
        if row:
            rank = self.wf_var("market_rank").get()
            self.wf_call(
                "watch_price",
                name=row["name"],
                slug=row["slug"],
                price=self.wf_var("watch_price").get(),
                rank=None if rank == "Любой" else int(rank),
            )

    def wf_toggle_watch(self):
        row = self.wf_selected("watches")
        if row:
            self.wf_call("watch_toggle", id=row["id"], enabled=not row.get("enabled"))

    def wf_copy_order(self):
        row = self.wf_selected("orders")
        item = getattr(self, "_wf_order_item", None)
        if not row or not item:
            return
        rank = f" (rank {row['rank']})" if row.get("rank") is not None else ""
        text = f"/w {row['seller']} Hi! I want to buy: {item['name']}{rank} for {row['price']} platinum. (warframe.market)"
        self.clipboard_clear()
        self.clipboard_append(text)
        self.wf_output(
            "Скопировано. Перед отправкой проверьте актуальность предложения:\n" + text, "wf_market"
        )

    def build_wf_journal(self):
        page = self.page_bodies["wf_journal"]
        card = self.card(
            page,
            "Кузница под присмотром",
            "Это ваши локальные таймеры. Сначала начните изготовление в игре, затем отметьте его здесь. Завершение проверяйте в игре.",
        )
        self.wf_form(
            card,
            [
                ("foundry_name", "Предмет", "", None),
                ("foundry_hours", "Часов", "23", None),
                ("foundry_minutes", "Дополнительно минут", "0", None),
            ],
        )
        self.wf_buttons(
            card,
            [
                ("Запустить таймер", self.wf_foundry_start),
                ("Предмет получен", lambda: self.wf_selected_action("foundry", "foundry_claim")),
                ("Удалить таймер", lambda: self.wf_delete("foundry")),
            ],
        )
        self.wf_table(
            card,
            "foundry",
            [
                ("name", "Предмет", 290),
                ("left", "Осталось", 170),
                ("end", "Расчётное завершение", 180),
            ],
        )
        card = self.card(
            page,
            "Помнить, где остановились",
            "Сессии записываются автоматически. «Крест, варфрейм запомни …» оставит игровую метку.",
        )
        self.wf_form(card, [("bookmark_name", "Что запомнить", "", None)])
        self.wf_buttons(
            card,
            [
                (
                    "Сохранить метку",
                    lambda: self.wf_call("bookmark", name=self.wf_var("bookmark_name").get()),
                ),
                ("Последние записи", lambda: self.wf_call("journal")),
                ("Удалить запись", lambda: self.wf_delete("journal")),
            ],
        )
        tree = self.wf_table(
            card,
            "journal",
            [("date", "Когда", 120), ("name", "Метка", 230), ("notes", "Подробности", 290)],
            8,
        )
        tree.bind("<Double-1>", lambda e: self.wf_journal_details())
        self.wf_text(card, "wf_journal", 8)
        self.wf_buttons(
            card, [("Экспорт блокнота", self.wf_export), ("Импорт блокнота", self.wf_import)]
        )

    def wf_foundry_start(self):
        try:
            seconds = (
                float(self.wf_var("foundry_hours").get().replace(",", ".")) * 3600
                + float(self.wf_var("foundry_minutes").get()) * 60
            )
        except ValueError:
            self.wf_output("Укажите числовую длительность.")
            return
        self.wf_call("foundry_start", name=self.wf_var("foundry_name").get(), seconds=seconds)

    def wf_journal_details(self):
        row = self.wf_selected("journal")
        if row:
            self.wf_output(
                local_time(row["created"]) + " · " + row["name"] + "\n" + row.get("notes", ""),
                "wf_journal",
            )

    def wf_export(self):
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Экспорт записей Warframe",
            defaultextension=".json",
            initialfile="warframe-notebook.json",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            self.wf_call("export", path=path)

    def wf_import(self):
        path = filedialog.askopenfilename(
            parent=self, title="Импорт записей Warframe", filetypes=[("JSON", "*.json")]
        )
        if path and messagebox.askyesno(
            "Заменить блокнот",
            "Текущие записи Warframe будут заменены. Сначала экспортируйте их, если они нужны. Слежение за ценами из файла будет выключено.",
            parent=self,
        ):
            self.wf_call("import", path=path)

    def build_wf_settings(self):
        page = self.page_bodies["wf_settings"]
        card = self.card(
            page,
            "Крест во время игры",
            "Обнаружение процесса автоматически включает помощника. По умолчанию ничего не захватывается с экрана.",
        )
        for key, label in [
            ("enabled", "Помощник Warframe включён"),
            ("auto_summary", "Краткая сводка после запуска игры"),
            ("auto_profile", "Игровой профиль и только важные уведомления"),
            ("health_notify", "Следить за памятью, диском и доступными датчиками температуры"),
        ]:
            ttk.Checkbutton(card, text=label, variable=self.wf_vars[key]).pack(anchor="w", pady=5)
        self.wf_config_combo(card, "platform", "Платформа", ["pc", "ps4", "xb1", "swi"])
        self.field(card, "Сценарий при запуске", self.wf_vars["start_scenario"])
        self.field(card, "Путь .exe / .lnk", self.wf_vars["launch_path"])
        ttk.Button(card, text="Найти файл игры", command=self.wf_pick_game).pack(anchor="e")
        self.field(card, "Проверка мира, секунд", self.wf_vars["poll_seconds"])
        card = self.card(
            page,
            "Живой крест",
            "Голубой — наблюдает за сервисами; зелёный — слышит звук; золотой — выполняет; фиолетовый — отвечает; оранжевый — новость; красный — нужна проверка.",
        )
        preview = tk.Canvas(card, height=90, bg=self.theme["card"], highlightthickness=0, width=130)
        preview.pack(anchor="w")
        from game_overlay import draw_cross

        draw_cross(preview, 52, labels=True, reduced=True)
        for key, label in [
            ("overlay_enabled", "Маленький крест поверх Warframe"),
            ("overlay_labels", "Подпись состояния под крестом"),
            ("overlay_foreground_only", "Показывать только когда окно игры на переднем плане"),
        ]:
            ttk.Checkbutton(card, text=label, variable=self.wf_vars[key]).pack(anchor="w", pady=4)
        self.wf_config_combo(
            card,
            "overlay_corner",
            "Угол экрана",
            ["top-left", "top-right", "bottom-left", "bottom-right"],
        )
        self.field(card, "Размер, 32–96 px", self.wf_vars["overlay_size"])
        self.label(
            card,
            "Для наложения используйте оконный режим без рамки. Чтение экрана выполняется отдельно через Windows OCR; его состояние видно в «Навигаторе». Кадры не сохраняются.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=8)
        card = self.card(page, "О чём напоминать")
        for key, label in [
            ("notifications", "События мира и смена фаз"),
            ("baro_notify", "Прибытие и отъезд Баро"),
            ("reset_notify", "Приближение сброса невыполненных дел"),
            ("market_notify", "Проверять сохранённые правила цены"),
            ("market_online_only", "Предложения только онлайн-продавцов"),
        ]:
            ttk.Checkbutton(card, text=label, variable=self.wf_vars[key]).pack(anchor="w", pady=4)
        self.field(card, "За сколько минут", self.wf_vars["notify_minutes"])
        self.field(card, "Слова наград через ,", self.wf_vars["reward_keywords"])
        for key, label in CYCLES.items():
            ttk.Checkbutton(card, text=label, variable=self.wf_cycles[key]).pack(anchor="w", pady=3)
        card = self.card(
            page,
            "Звук, ресурсы и игровые моменты",
            "Звук выбранных программ вернётся после игры, если вы не меняли его вручную. Запись клипа требует включённой фоновой записи Xbox Game Bar.",
        )
        self.field(card, "Программы .exe через ,", self.wf_vars["volume_apps"])
        self.field(card, "Их громкость, %", self.wf_vars["volume_percent"])
        for key, label in [
            ("vision_enabled", "Читать активное окно игры локально каждые 15 секунд"),
            ("capture_on_bookmark", "При метке сохранять скриншот активного окна Warframe"),
            ("clip_on_bookmark", "При метке запросить последний клип Game Bar"),
        ]:
            ttk.Checkbutton(card, text=label, variable=self.wf_vars[key]).pack(anchor="w", pady=5)
        for key, label in [
            ("memory_limit_percent", "Порог памяти, %"),
            ("disk_free_gb", "Минимум на диске, ГБ"),
            ("temperature_limit", "Порог датчика, °C"),
        ]:
            self.field(card, label, self.wf_vars[key])
        self.wf_text(card, "wf_settings", 4)

    def wf_config_combo(self, parent, key, label, values):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=5)
        self.label(row, label, width=24).pack(side="left")
        ttk.Combobox(row, textvariable=self.wf_vars[key], values=values, state="readonly").pack(
            side="left", fill="x", expand=True
        )

    def wf_pick_game(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="Warframe или Launcher",
            filetypes=[("Программа или ярлык", "*.exe *.lnk")],
        )
        if path:
            self.wf_vars["launch_path"].set(path)

    def warframe_settings_draft(self):
        result = {
            key: (int(var.get()) if type(DEFAULT_WARFRAME[key]) is int else var.get())
            for key, var in self.wf_vars.items()
        }
        result["cycles"] = [key for key, var in self.wf_cycles.items() if var.get()]
        return result
