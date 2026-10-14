"""Local history pages; all RPC and clipboard work stays off the Tk thread."""

import datetime as dt
import tkinter as tk
from tkinter import ttk
from ui_fonts import UI_FONT
from ui_relic import ThornButton


def stamp(value):
    return dt.datetime.fromtimestamp(value).strftime("%d.%m %H:%M")


class PersonalPages:
    def _personal(self, section, action, values=None, done=None):
        self._background(lambda: self.runtime.personal_tools(section, action, values), done)

    def build_clipboard_tab(self):
        card = self.card(
            self.page_bodies["clipboard"],
            "Под рукой",
            "Локальная история текста: поиск, закрепление и повторное копирование.",
        )
        self.clip_enabled = tk.BooleanVar(value=self.config_mgr.snapshot()["clipboard"]["enabled"])
        ttk.Checkbutton(
            card,
            text="Запоминать скопированный текст автоматически",
            variable=self.clip_enabled,
            command=self._toggle_clipboard,
        ).pack(anchor="w")
        self.label(
            card,
            "До 100 фрагментов по 4000 символов. Изображения не сохраняются. Менеджеры паролей из списка исключений пропускаются; распознавание других секретов не гарантируется.",
            muted=True,
            wraplength=760,
        ).pack(anchor="w", pady=(4, 12))
        row = tk.Frame(card, bg=card.cget("bg"))
        row.pack(fill="x")
        self.clip_query = tk.StringVar()
        query = ttk.Entry(row, textvariable=self.clip_query)
        query.pack(side="left", fill="x", expand=True)
        query.bind("<Return>", lambda e: self._refresh_clips())
        ttk.Button(row, text="Найти", command=self._refresh_clips).pack(side="left", padx=8)
        ttk.Button(row, text="Сохранить текущий", command=lambda: self._clip_action("save")).pack(
            side="left"
        )
        self.clip_tree = ttk.Treeview(
            card, columns=("text", "app", "time"), show="headings", height=7
        )
        for key, label, width in (
            ("text", "Фрагмент", 380),
            ("app", "Приложение", 160),
            ("time", "Скопирован", 120),
        ):
            self.clip_tree.heading(key, text=label)
            self.clip_tree.column(key, width=width, minwidth=80)
        self.clip_tree.pack(fill="x", pady=12)
        self.clip_rows = {}
        self.clip_tree.bind("<<TreeviewSelect>>", self._clip_selected)
        self.clip_preview = tk.Text(
            card,
            height=5,
            wrap="word",
            bg=self.theme["sidebar"],
            fg=self.theme["text"],
            font=(UI_FONT, 12),
            relief="flat",
            state="disabled",
        )
        self.clip_preview.pack(fill="x")
        row = tk.Frame(card, bg=card.cget("bg"))
        row.pack(fill="x", pady=(12, 0))
        for index, (text, action) in enumerate(
            (
                ("Скопировать", "copy"),
                ("Закрепить / открепить", "pin"),
                ("Удалить", "delete"),
                ("Очистить историю", "clear"),
            )
        ):
            row.columnconfigure(index % 2, weight=1, uniform="clipboard-actions")
            ThornButton(
                row, self.theme, text=text, command=lambda a=action: self._clip_action(a)
            ).grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 8))
        self.clip_status = tk.StringVar()
        self.label(card, textvariable=self.clip_status, muted=True, wraplength=740).pack(
            anchor="w", pady=8
        )
        self.label(
            card,
            "Голосом: «сохрани буфер» → «покажи историю буфера» → «скопируй фрагмент два».",
            muted=True,
            wraplength=740,
        ).pack(anchor="w")
        self._refresh_clips()

    def _toggle_clipboard(self):
        self._personal(
            "clipboard",
            "enable",
            {"enabled": self.clip_enabled.get()},
            lambda value: (self.clip_status.set(value), self._refresh_clips()),
        )

    def _refresh_clips(self):
        query = self.clip_query.get()
        tree = self.clip_tree

        def display(data):
            if not tree.winfo_exists() or tree is not self.clip_tree:
                return
            tree.delete(*tree.get_children())
            self.clip_rows = {str(row["id"]): row for row in data["rows"]}
            for ident, row in self.clip_rows.items():
                tree.insert(
                    "",
                    "end",
                    iid=ident,
                    values=(
                        ("★ " if row["pinned"] else "") + " ".join(row["text"].split())[:90],
                        row["app"],
                        stamp(row["created"]),
                    ),
                )
            self.clip_enabled.set(data["enabled"])
            self.clip_status.set(data["error"] or f"Фрагментов: {len(data['rows'])}")
            self._clip_selected()

        self._personal("clipboard", "list", {"query": query}, display)

    def _clip_selected(self, event=None):
        ids = self.clip_tree.selection()
        text = self.clip_rows.get(ids[0], {}).get("text", "") if ids else ""
        self.clip_preview.configure(state="normal")
        self.clip_preview.delete("1.0", "end")
        self.clip_preview.insert("1.0", text)
        self.clip_preview.configure(state="disabled")

    def _clip_action(self, action):
        selected = self.clip_tree.selection()
        if action not in ("save", "clear") and not selected:
            self.clip_status.set("Выберите фрагмент в списке.")
            return
        self._personal(
            "clipboard",
            action,
            {"id": selected[0]} if selected else {},
            lambda value: (self.status_var.set(value), self._refresh_clips()),
        )

    def build_sessions_tab(self):
        card = self.card(
            self.page_bodies["sessions"],
            "История игры",
            "Сессия начинается при обнаружении игры. Заметки и снимки добавляются только по вашей команде.",
        )
        self.session_rows = ttk.Treeview(
            card, columns=("game", "date", "status"), show="headings", height=5
        )
        for key, text, width in (
            ("game", "Игра", 280),
            ("date", "Начало", 150),
            ("status", "Состояние", 190),
        ):
            self.session_rows.heading(key, text=text)
            self.session_rows.column(key, width=width, minwidth=100)
        self.session_rows.pack(fill="x")
        self.session_rows.bind("<<TreeviewSelect>>", self._select_session)
        self.session_report = tk.StringVar()
        self.label(card, textvariable=self.session_report, wraplength=740).pack(anchor="w", pady=14)
        self.session_moments = ttk.Treeview(
            card, columns=("kind", "text"), show="headings", height=5
        )
        self.session_moments.heading("kind", text="Событие")
        self.session_moments.column("kind", width=140, stretch=False)
        self.session_moments.heading("text", text="Содержание")
        self.session_moments.column("text", width=500)
        self.session_moments.pack(fill="x")
        self.session_moments.bind("<<TreeviewSelect>>", self._session_moment_selected)
        self.session_detail = tk.Text(
            card,
            height=4,
            wrap="word",
            bg=self.theme["sidebar"],
            fg=self.theme["text"],
            font=(UI_FONT, 12),
            relief="flat",
            state="disabled",
        )
        self.session_detail.pack(fill="x", pady=(8, 0))
        self.session_details = {}
        row = tk.Frame(card, bg=card.cget("bg"))
        row.pack(fill="x", pady=12)
        for label, callback in (
            ("Обновить", self._refresh_sessions),
            ("Открыть снимок", self._open_session_capture),
            ("Сделать скриншот", lambda: self.runtime.submit("сделай скриншот")),
        ):
            ThornButton(row, self.theme, text=label, command=callback).pack(
                side="left", padx=(0, 8)
            )
        self.label(
            card,
            "В игре скажите: «Крест, заметка для игры проверить реликвии». После — «Крест, итоги игровой сессии». Хранятся последние 100 сессий.",
            muted=True,
            wraplength=740,
        ).pack(anchor="w")
        self._session_rendering = False
        self._refresh_sessions()

    def _refresh_sessions(self):
        tree = self.session_rows

        def display(data):
            if not tree.winfo_exists() or tree is not self.session_rows:
                return
            self._session_rendering = True
            tree.delete(*tree.get_children())
            for row in data["rows"]:
                tree.insert(
                    "",
                    "end",
                    iid=str(row["id"]),
                    values=(row["game"], stamp(row["started"]), row["reason"] or "Игра идёт"),
                )
            self._show_session(data)
            self._session_rendering = False

        self._personal("sessions", "list", done=display)

    def _select_session(self, event=None):
        selected = self.session_rows.selection()
        if selected and not self._session_rendering:
            tree = self.session_rows

            def display(data):
                if (
                    tree.winfo_exists()
                    and tree is self.session_rows
                    and tree.selection() == selected
                ):
                    self._show_session(data)

            self._personal("sessions", "list", {"id": selected[0]}, display)

    def _show_session(self, data):
        self.session_report.set("\n".join(data["report"].splitlines()[:2]))
        self.session_moments.delete(*self.session_moments.get_children())
        self.session_details = {
            str(row["id"]): row["text"] or row["path"] for row in data["moments"]
        }
        for row in data["moments"]:
            self.session_moments.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    "Снимок" if row["kind"] == "screenshot" else "Заметка",
                    row["text"] or row["path"],
                ),
            )

    def _session_moment_selected(self, event=None):
        selected = self.session_moments.selection()
        text = self.session_details.get(selected[0], "") if selected else ""
        self.session_detail.configure(state="normal")
        self.session_detail.delete("1.0", "end")
        self.session_detail.insert("1.0", text)
        self.session_detail.configure(state="disabled")

    def _open_session_capture(self):
        selected = self.session_moments.selection()
        if selected:
            self._personal("sessions", "open", {"id": selected[0]}, self.status_var.set)
        else:
            self.status_var.set("Выберите снимок из событий сессии.")
