"""Live, searchable command reference built from the running skill registry."""

import re
import tkinter as tk
from tkinter import ttk, filedialog
from pathlib import Path

CATEGORIES = {
    "Clipboard": "Вычисления",
    "Media": "Медиа",
    "Meta": "Ассистент",
    "Timers": "Таймеры",
    "System": "Система",
    "Steam & Gaming": "Steam",
    "Web & Info": "Интернет",
    "Interactive": "Интерактив",
}
REQUIREMENTS = {
    "media": "Управление плеером требует Windows и открытого совместимого приложения. Лайк зависит от доступных кнопок плеера.",
    "discord_voice": "Нужен запущенный настольный Discord. «Крест, мут» — выключить микрофон; «Крест, анмут» — включить. Крест продолжает слышать вас. Бот и Premium не требуются.",
    "windows": "Действия выполняются на этом компьютере с правами текущего пользователя Windows.",
    "steam": "Для сетевых сведений нужны настроенный Steam и доступ к API. Локальный запуск использует установленный Steam.",
    "warframe": "Подробности зависят от подключения гида и доступных данных Warframe. Сетевой сводке нужен интернет.",
    "webo": "Поиск и сетевые сведения требуют интернета; запуск игры — установленного клиента и игры.",
    "timers_alarms": "Таймеры и напоминания работают, пока запущено ядро Крест; закрытие меню не останавливает ядро.",
}
MODULE_NAMES = {
    "basic": "Время и дата",
    "discord_voice": "Микрофон Discord",
    "interactive": "Общение",
    "meta": "Ассистент",
    "media": "Музыка и громкость",
    "utils": "Вычисления",
    "timers_alarms": "Таймеры и будильники",
    "system_mon": "Состояние компьютера",
    "webo": "Интернет и Steam",
    "windows": "Управление Windows",
    "steam": "Steam и инвентарь",
    "warframe": "Warframe-гид",
}


def rows(manager, settings):
    from speech_tools import canonical_aliases, normalize_command

    aliases = canonical_aliases(
        settings.get("command_aliases", {}),
        settings.get("app_settings", {}).get("wake_word", "крест"),
    )
    result = []
    for entry in manager.catalog():
        phrases = list(dict.fromkeys([*entry.examples, *entry.keywords]))
        own = []
        for alias, target in aliases.items():
            value = normalize_command(target).casefold().strip(" ,.!?")
            if value in entry.keywords or any(
                re.fullmatch(p.pattern, value, re.I) for p in entry.patterns
            ):
                own.append(alias)
        category = CATEGORIES.get(entry.category, entry.category)
        result.append(
            {
                "entry": entry,
                "id": entry.id,
                "description": entry.description,
                "category": category,
                "phrases": phrases,
                "aliases": own,
                "availability": manager.availability(entry, settings),
                "requirements": REQUIREMENTS.get(
                    entry.id.split(".")[-2],
                    "Дополнительные условия и права проверяются при выполнении команды.",
                ),
            }
        )
    return sorted(result, key=lambda r: (r["category"], r["description"]))


def search_rows(items, query="", category="Все разделы", available=False):
    words = query.casefold().replace("ё", "е").split()
    return [
        r
        for r in items
        if (category == "Все разделы" or r["category"] == category)
        and (not available or r["availability"] == "Доступен")
        and all(
            word
            in (
                r["description"] + " " + r["category"] + " " + " ".join(r["phrases"] + r["aliases"])
            )
            .casefold()
            .replace("ё", "е")
            for word in words
        )
    ]


def describe(row, wake="Крест"):
    lines = [
        row["description"],
        "",
        "Раздел: " + row["category"],
        "Состояние: " + row["availability"],
        row["requirements"],
        "",
        "Можно сказать:",
    ]
    lines += ["• " + wake + ", " + phrase for phrase in row["phrases"]]
    if not row["phrases"]:
        lines.append("Автор навыка не добавил примеры фраз.")
    if row["aliases"]:
        lines += ["", "Ваши фразы:", *["• " + wake + ", " + alias for alias in row["aliases"]]]
    parameters = sorted(
        {
            name
            for pattern in row["entry"].patterns
            for name in re.compile(pattern.pattern).groupindex
        }
    )
    if parameters:
        lines += [
            "",
            "Команда принимает значения: "
            + ", ".join(parameters)
            + ". Используйте пример выше и подставьте свои данные.",
        ]
    lines += ["", "Модуль: " + row["id"].rsplit(".", 1)[0]]
    return "\n".join(lines)


class CommandCatalogPages:
    def build_skills_tab(self):
        card = self.card(
            self.page_bodies["skills"],
            "Все команды Крест",
            "Найдите команду по описанию или фразе. Справа — все варианты произношения и условия работы. Импортированные навыки появляются здесь автоматически.",
        )
        self.skill_search = tk.StringVar()
        self.skill_category = tk.StringVar(value="Все разделы")
        self.skill_available = tk.BooleanVar(value=False)
        self.skill_count = tk.StringVar()
        search = ttk.Entry(card, textvariable=self.skill_search)
        search.pack(fill="x", pady=(4, 8))
        from entry_clipboard import attach

        attach(search, self.theme)
        toolbar = tk.Frame(card, bg=self.theme["card"])
        toolbar.pack(fill="x", pady=4)
        self.skill_categories = ttk.Combobox(
            toolbar, textvariable=self.skill_category, state="readonly"
        )
        self.skill_categories.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(toolbar, text="Включённые / доступные", variable=self.skill_available).pack(
            side="left", padx=10
        )
        self.label(card, textvariable=self.skill_count, muted=True).pack(anchor="w", pady=6)
        body = tk.Frame(card, bg=self.theme["card"])
        body.pack(fill="x")
        body.columnconfigure(0, weight=1, uniform="catalog")
        body.columnconfigure(1, weight=1, uniform="catalog")
        table = tk.Frame(body, bg=self.theme["card"])
        table.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.skill_tree = ttk.Treeview(
            table,
            columns=("category", "description", "status"),
            show="headings",
            height=6,
            displaycolumns=("description",),
            selectmode="browse",
        )
        for name, title, width in (
            ("category", "Раздел", 115),
            ("description", "Команда", 235),
            ("status", "Состояние", 125),
        ):
            self.skill_tree.heading(name, text=title)
            self.skill_tree.column(name, width=width, minwidth=80)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.skill_tree.yview)
        self.skill_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.skill_tree.pack(side="left", fill="both", expand=True)
        self.skill_tree.bind("<<TreeviewSelect>>", self._skill_selected)
        self.skill_tree.bind("<Double-1>", lambda _: self._copy_skill())
        from ui_fonts import UI_FONT

        details = tk.Frame(body, bg=self.theme["bg"])
        details.grid(row=0, column=1, sticky="nsew")
        self.skill_details = tk.Text(
            details,
            height=10,
            width=1,
            font=(UI_FONT, 10),
            wrap="word",
            bg=self.theme["bg"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="flat",
            padx=12,
            pady=12,
            state="disabled",
        )
        scroll_details = ttk.Scrollbar(details, command=self.skill_details.yview)
        self.skill_details.configure(yscrollcommand=scroll_details.set)
        scroll_details.pack(side="right", fill="y")
        self.skill_details.pack(side="left", fill="both", expand=True)
        self.skill_phrase = tk.StringVar()
        self.skill_phrases = ttk.Combobox(card, textvariable=self.skill_phrase, state="readonly")
        self.skill_phrases.pack(fill="x", pady=5)
        toolbar = tk.Frame(card, bg=self.theme["card"])
        toolbar.pack(fill="x", pady=6)
        for title, fn in (
            ("Скопировать фразу", self._copy_skill),
            ("Проверить понимание", self._inspect_skill),
            ("В избранное", self._favorite_skill),
            ("Сохранить список…", self._export_commands),
        ):
            ttk.Button(toolbar, text=title, command=fn).pack(side="left", padx=(0, 6))
        for var in (self.skill_search, self.skill_category, self.skill_available):
            var.trace_add("write", lambda *_: self._filter_skills())
        self.build_skill_imports()
        card = self.card(
            self.page_bodies["skills"],
            "Включённые модули",
            "Изменения переключателей применяются общей кнопкой «Сохранить».",
        )
        for name, var in self.skill_vars.items():
            ttk.Checkbutton(card, text=MODULE_NAMES.get(name, name), variable=var).pack(
                anchor="w", pady=3
            )
        ttk.Button(
            card,
            text="Перезагрузить навыки",
            command=lambda: self.runtime.submit("перезагрузи навыки"),
        ).pack(anchor="w", pady=8)

    def refresh_skills(self):
        if "skills" not in self._built_pages:
            return
        items = rows(self.runtime.manager, self.config_mgr.snapshot())
        self.catalog = {r["id"]: r["entry"] for r in items}
        self._catalog_rows = {r["id"]: r for r in items}
        self.skill_categories.configure(
            values=["Все разделы", *sorted({r["category"] for r in items})]
        )
        self._filter_skills()

    def _filter_skills(self):
        if not hasattr(self, "_catalog_rows"):
            return
        items = list(self._catalog_rows.values())
        selected = self.skill_tree.selection()
        visible = search_rows(
            items, self.skill_search.get(), self.skill_category.get(), self.skill_available.get()
        )
        self.skill_tree.delete(*self.skill_tree.get_children())
        for row in visible:
            self.skill_tree.insert(
                "",
                "end",
                iid=row["id"],
                values=(row["category"], row["description"], row["availability"]),
            )
        self.skill_count.set(
            f"Показано {len(visible)} из {len(items)} команд · Доступны по настройкам: {sum(r['availability'] == 'Доступен' for r in items)}"
        )
        chosen = (
            selected[0]
            if selected and self.skill_tree.exists(selected[0])
            else visible[0]["id"]
            if visible
            else ""
        )
        if chosen:
            self.skill_tree.selection_set(chosen)
        self._skill_selected()

    def _skill_selected(self, event=None):
        selected = self.skill_tree.selection()
        row = self._catalog_rows.get(selected[0]) if selected else None
        wake = self.config_mgr.snapshot()["app_settings"]["wake_word"]
        self.skill_details.configure(state="normal")
        self.skill_details.delete("1.0", "end")
        self.skill_details.insert(
            "1.0",
            describe(row, wake.capitalize())
            if row
            else "Ничего не найдено. Измените поиск или раздел.",
        )
        self.skill_details.configure(state="disabled")
        phrases = row["phrases"] + row["aliases"] if row else []
        self.skill_phrases.configure(values=phrases)
        self.skill_phrase.set(phrases[0] if phrases else "")

    def _copy_skill(self):
        phrase = self.skill_phrase.get()
        if phrase:
            self.clipboard_clear()
            self.clipboard_append(
                self.config_mgr.snapshot()["app_settings"]["wake_word"] + ", " + phrase
            )
            self.status_var.set("Фраза скопирована. Команда не выполнялась.")

    def _inspect_skill(self):
        from command_parser import open_parser

        if self.skill_phrase.get():
            open_parser(
                self,
                self.config_mgr.snapshot()["app_settings"]["wake_word"]
                + ", "
                + self.skill_phrase.get(),
            )

    def _favorite_skill(self):
        if self.skill_phrase.get():
            try:
                self.command_book.add(self.skill_phrase.get())
            except (OSError, ValueError) as exc:
                self._report_error(str(exc))
                return
            self._refresh_command_lists()
            self.status_var.set("Фраза добавлена в «Мои команды».")

    def _use_skill(self):
        self._copy_skill()

    def _export_commands(self):
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить текущий список команд",
            initialfile="Команды Крест.txt",
            defaultextension=".txt",
            filetypes=[("Текст", "*.txt")],
        )
        if path:
            settings = self.config_mgr.snapshot()
            text = "\n\n".join(
                describe(row, settings["app_settings"]["wake_word"].capitalize())
                for row in rows(self.runtime.manager, settings)
            )
            try:
                Path(path).write_text(text, encoding="utf-8")
                self.status_var.set("Список команд сохранён.")
            except OSError as exc:
                self._report_error(str(exc))
