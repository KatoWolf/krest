"""Incoming folders and move history, refreshed only with the visible Files page."""

from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

FILE_STATES = {
    "stable": "Готов к обработке",
    "afk": "Ждёт AFK",
    "moved": "Перемещён",
    "returned": "Возвращён",
    "attention": "Нужна проверка",
    "prepared": "Проверка после прерывания",
    "returning": "Проверка возврата",
    "prepared_copy": "Проверка копии при AFK",
    "returning_copy": "Продолжение возврата при AFK",
}


class IncomingPages:
    def _incoming_call(self, action, **values):
        self._smart_call("incoming." + action, **values)

    def _build_incoming_controls(self, parent):
        card = self.card(
            parent,
            "Файлы под присмотром",
            "Готовые файлы перемещаются небольшими порциями, даже во время игры. Копирование на другой диск, проверка содержимого и распаковка ждут AFK.",
        )
        self.incoming_status = tk.StringVar(value="Проверяю фон…")
        self.label(card, textvariable=self.incoming_status, wraplength=730).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Включить сортировку", lambda: self._incoming_call("sorting", enabled=True)),
                ("Пауза файлов", lambda: self._incoming_call("pause", paused=True)),
                ("Продолжить", lambda: self._incoming_call("pause", paused=False)),
            ],
        )
        sources = self.advanced(card, "Папки браузера и мессенджеров")
        self.incoming_sources = self._table(
            sources, {"label": "Источник", "path": "Папка", "enabled": "Включён"}, 4
        )
        self.incoming_label = tk.StringVar(value="Telegram")
        combo = ttk.Combobox(
            sources,
            textvariable=self.incoming_label,
            values=["Загрузки браузера", "Telegram", "Discord", "Другая папка"],
            state="readonly",
        )
        combo.pack(fill="x", pady=8)
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            combo.bind(event, lambda _: "break")
        self._buttons(
            sources,
            [
                ("Добавить папку…", self._incoming_add_source),
                ("Включить / выключить выбранную", self._incoming_toggle_source),
            ],
        )
        self.label(
            sources,
            "Источник определяется выбранной папкой. Крест не просматривает другие папки аккаунта.",
            wraplength=730,
            muted=True,
        ).pack(fill="x")

        card = self.card(
            parent,
            "Куда делся файл?",
            "История известных Крест файлов. Выберите строку, чтобы открыть, показать в папке или вернуть перемещение. Файлы с изменённым содержимым и занятые имена не перезаписываются.",
        )
        self.incoming_history = self._table(
            card, {"name": "Файл", "source": "Источник", "state": "Состояние"}, 6
        )
        self.incoming_detail = tk.StringVar(value="Выберите файл")
        self.incoming_history.bind("<<TreeviewSelect>>", lambda _: self._incoming_detail())
        self.label(card, textvariable=self.incoming_detail, wraplength=730).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Открыть", lambda: self._incoming_selected("open")),
                ("Показать в папке", lambda: self._incoming_selected("reveal")),
                ("Вернуть обратно", lambda: self._incoming_selected("undo")),
                ("Взять за образец", lambda: self._incoming_selected("select")),
            ],
        )
        self.incoming_search = tk.StringVar()
        self.field(card, "Название файла", self.incoming_search)
        self._buttons(
            card,
            [
                (
                    "Найти в истории",
                    lambda: self._smart_call(
                        "incoming.find", query=self.incoming_search.get(), done=self._incoming_found
                    ),
                ),
                ("Другой файл за образец…", self._incoming_sample),
            ],
        )
        self.label(
            card,
            "Голосом: «открой последний скачанный архив», «где вчерашний документ», «верни этот файл обратно». После выбора образца откройте папку назначения и скажите: «такие файлы складывай сюда».",
            wraplength=730,
            muted=True,
        ).pack(fill="x", pady=8)

    def _incoming_found(self, result):
        if not isinstance(result, dict) or not result.get("id"):
            return
        self.status_var.set(str(result["current"]))
        if self.incoming_history.exists(result["id"]):
            self.incoming_history.selection_set(result["id"])
            self.incoming_history.see(result["id"])
            self._incoming_detail()

    def _incoming_add_source(self):
        path = filedialog.askdirectory(title="Папка, куда сохраняются входящие файлы")
        if path:
            self._incoming_call("source", path=path, label=self.incoming_label.get())

    def _incoming_toggle_source(self):
        ids = self.incoming_sources.selection()
        row = next(
            (r for r in self._smart_data.get("incoming", {}).get("sources", []) if r["id"] in ids),
            None,
        )
        if row:
            self._incoming_call(
                "source", path=row["path"], label=row["label"], enabled=not row["enabled"]
            )

    def _incoming_sample(self):
        path = filedialog.askopenfilename(title="Файл для правила «такие файлы складывай сюда»")
        if path:
            self._incoming_call("example", path=path)

    def _incoming_selected(self, action):
        ids = self.incoming_history.selection()
        if not ids:
            self.status_var.set("Выберите один файл в истории")
            return
        self._incoming_call(action, id=ids[0])

    def _incoming_detail(self):
        ids = self.incoming_history.selection()
        row = next(
            (r for r in self._smart_data.get("incoming", {}).get("items", []) if r["id"] in ids),
            None,
        )
        if row:
            self.incoming_detail.set(
                f"Было: {row['source']}\nСейчас: {row['current']}\nВ истории с {datetime.fromtimestamp(row['received']):%d.%m %H:%M}. {row.get('detail', '')}"
            )

    def _incoming_display(self, data):
        if not hasattr(self, "incoming_history") or not self.incoming_history.winfo_exists():
            return
        self.incoming_status.set(
            data.get("status", "")
            + (
                "\n" + data["last_result"]
                if data.get("last_result") and data["last_result"] != data.get("status")
                else ""
            )
        )
        self._smart_rows(
            "incoming_sources",
            [
                (r["id"], (r["label"], r["path"], "Да" if r["enabled"] else "Нет"))
                for r in data.get("sources", [])
            ],
        )
        self._smart_rows(
            "incoming_history",
            [
                (
                    r["id"],
                    (
                        Path(r["current"]).name,
                        r["label"],
                        FILE_STATES.get(r["status"], r["status"]),
                    ),
                )
                for r in data.get("items", [])
            ],
        )
        self._incoming_detail()
