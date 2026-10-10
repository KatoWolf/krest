"""Source-only Tk control centre. Tk widgets are accessed on the main thread only."""

import copy
import importlib
import logging
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

from app_runtime import AssistantRuntime
from configuration import ConfigManager, DEFAULT_CONFIG, CONFIG_PATH
from integrations import IntegrationManager
from themes import THEMES
from guikrest import indicator_controller
from command_book import CommandBook

logger = logging.getLogger(__name__)
BASE_DIR = str(Path(__file__).resolve().parent)
TRAY_SUPPORTED = False  # Discovered lazily when the user minimizes to tray.


class ControlCenterApp(tk.Tk):
    NAV = [
        ("actions", "Обзор"),
        ("skills", "Навыки"),
        ("favorites", "Мои команды"),
        ("timers", "Таймеры"),
        ("api", "Подключения"),
        ("dota", "Dota 2"),
        ("settings", "Настройки"),
        ("logs", "Журнал"),
        ("license", "О программе"),
    ]

    def __init__(self):
        super().__init__()
        self.title("КРЕСТ — центр управления")
        self.geometry("1120x790")
        self.minsize(900, 650)
        self.protocol("WM_DELETE_WINDOW", self.on_close_window)
        self.bind("<Control-s>", lambda event: self.save_all_settings())
        self.config_mgr = ConfigManager()
        self.command_book = CommandBook(self.config_mgr.filepath.parent / "favorites.json")
        self.ui_queue = queue.Queue()
        self.closing = False
        self.current_page = "actions"
        self.tray_icon = None
        self.busy_services = set()
        self.auth_busy = False
        self.history = []
        self.aliases = self.config_mgr.snapshot().get("command_aliases", {})
        self.last_command = ""
        self._timer_rows = None
        self._reminder_rows = None
        self.runtime = AssistantRuntime(
            self.config_mgr.snapshot, data_dir=self.config_mgr.filepath.parent
        )
        self.integration = IntegrationManager(self)
        self.owner_var = tk.StringVar(value=self.config_mgr.config["owner_name"])
        self.theme_var = tk.StringVar(value=self.config_mgr.config["selected_theme"])
        self.settings_vars = {}
        for key, value in self.config_mgr.config["app_settings"].items():
            self.settings_vars[key] = (
                tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=value)
            )
        self.api_vars = {
            key: tk.StringVar(value=value)
            for key, value in self.config_mgr.config["api_keys"].items()
        }
        self.skill_vars = {
            key: tk.BooleanVar(value=value)
            for key, value in self.config_mgr.config["skills"].items()
        }
        self.dota_vars = {
            key: (
                tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=value)
            )
            for key, value in self.config_mgr.config["dota_guide"].items()
        }
        self.status_var = tk.StringVar(value="Готово. Введите команду или выберите навык.")
        self.queue_var = tk.StringVar(value="Нет команд в очереди")
        self.reminder_var = tk.StringVar(value="Нет непрочитанных напоминаний")
        self.voice_var = tk.StringVar(value="Голос выключен")
        self.dirty = False
        for variable in [
            self.owner_var,
            self.theme_var,
            *self.settings_vars.values(),
            *self.api_vars.values(),
            *self.skill_vars.values(),
            *self.dota_vars.values(),
        ]:
            variable.trace_add("write", self._mark_dirty)
        self.theme = THEMES.get(self.theme_var.get(), THEMES["Amethyst Modern"])

        class UILogHandler(logging.Handler):
            def emit(handler, record):
                self.ui_queue.put(lambda text=handler.format(record): self._append_log(text))

        self._log_handler = UILogHandler(level=logging.WARNING)
        logging.getLogger().addHandler(self._log_handler)
        self.build_ui()
        self.bind("<Control-l>", lambda e: self._focus_command())
        self.bind("<Unmap>", self._on_minimize)
        indicator_controller.attach(self)
        self.runtime.start()
        self._poll_job = self.after(100, self._poll)
        if self.config_mgr.load_error:
            self._append_log(
                "Настройки повреждены: "
                + self.config_mgr.load_error
                + ". При сохранении исходный файл будет скопирован в .broken."
            )
        for error in (self.runtime.state.load_error, self.command_book.load_error):
            if error:
                self._report_error(error + ". Исходный файл будет сохранён в .broken при записи.")

    def _mark_dirty(self, *args):
        self.dirty = True
        if hasattr(self, "save_button"):
            self.save_button.config(text="Сохранить изменения •")

    def _style(self):
        self.configure(bg=self.theme["bg"])
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            ".",
            background=self.theme["card"],
            foreground=self.theme["text"],
            font=("Segoe UI", 10),
        )
        style.configure(
            "TButton",
            background=self.theme["card_hover"],
            foreground=self.theme["text"],
            padding=(12, 7),
            borderwidth=0,
        )
        style.map("TButton", background=[("active", self.theme["accent"])])
        style.configure("Accent.TButton", background=self.theme["accent"], foreground="white")
        style.configure(
            "TEntry",
            fieldbackground=self.theme["sidebar"],
            foreground=self.theme["text"],
            insertcolor=self.theme["text"],
            padding=6,
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.theme["sidebar"],
            foreground=self.theme["text"],
            padding=6,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.theme["sidebar"])],
            foreground=[("readonly", self.theme["text"])],
        )
        style.configure(
            "Treeview",
            background=self.theme["sidebar"],
            fieldbackground=self.theme["sidebar"],
            foreground=self.theme["text"],
            rowheight=29,
            borderwidth=0,
        )
        style.configure(
            "Treeview.Heading",
            background=self.theme["card"],
            foreground=self.theme["sub_text"],
            padding=8,
        )
        style.map("Treeview", background=[("selected", self.theme["accent"])])

    def label(self, parent, text="", large=False, muted=False, **kwargs):
        return tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg=self.theme["sub_text" if muted else "text"],
            font=("Segoe UI", 18 if large else 10, "bold" if large else "normal"),
            anchor="w",
            justify="left",
            **kwargs,
        )

    def card(self, parent, title, subtitle=""):
        frame = tk.Frame(parent, bg=self.theme["card"], padx=18, pady=16)
        frame.pack(fill="x", pady=(0, 12))
        self.label(frame, title, large=True).pack(anchor="w", pady=(0, 8))
        if subtitle:
            self.label(frame, subtitle, muted=True, wraplength=690).pack(anchor="w", pady=(0, 12))
        return frame

    def field(self, parent, title, variable, secret=False):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=5)
        self.label(row, title, width=24).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable, show="•" if secret else "")
        entry.pack(side="left", fill="x", expand=True)
        if secret:
            ttk.Button(
                row,
                text="Показать",
                command=lambda e=entry: e.configure(show="" if e.cget("show") else "•"),
            ).pack(side="right", padx=(6, 0))
        return entry

    def build_ui(self):
        self._style()
        self.shell = tk.Frame(self, bg=self.theme["bg"])
        self.shell.pack(fill="both", expand=True)
        side = tk.Frame(self.shell, bg=self.theme["sidebar"], width=205, padx=16, pady=24)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self.label(side, "✝ КРЕСТ", large=True).pack(anchor="w", pady=(0, 4))
        self.label(side, "Личный ассистент", muted=True).pack(anchor="w", pady=(0, 28))
        self.nav_buttons = {}
        for key, title in self.NAV:
            button = ttk.Button(side, text=title, command=lambda k=key: self.switch_page(k))
            button.pack(fill="x", pady=4)
            self.nav_buttons[key] = button
        ttk.Button(side, text="Завершить работу", command=self._real_quit).pack(
            side="bottom", fill="x"
        )
        body = tk.Frame(self.shell, bg=self.theme["bg"], padx=22, pady=20)
        body.pack(side="left", fill="both", expand=True)
        footer = tk.Frame(body, bg=self.theme["bg"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.save_button = ttk.Button(
            footer,
            text="Сохранить изменения" + (" •" if self.dirty else ""),
            style="Accent.TButton",
            command=self.save_all_settings,
        )
        self.save_button.pack(side="right")
        self.label(footer, textvariable=self.status_var, muted=True, wraplength=470).pack(
            side="left"
        )
        self.content = tk.Frame(body, bg=self.theme["bg"])
        self.content.pack(fill="both", expand=True)
        self.pages = {}
        self.page_bodies = {}
        self.canvases = {}
        for code, _ in self.NAV:
            page = tk.Frame(self.content, bg=self.theme["bg"])
            canvas = tk.Canvas(page, bg=self.theme["bg"], highlightthickness=0)
            scroll = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            scroll.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            canvas.configure(yscrollcommand=scroll.set)
            inner = tk.Frame(canvas, bg=self.theme["bg"])
            window = canvas.create_window((0, 0), window=inner, anchor="nw")
            inner.bind(
                "<Configure>",
                lambda event, c=canvas: c.configure(scrollregion=c.bbox("all")),
            )
            canvas.bind(
                "<Configure>",
                lambda event, c=canvas, w=window: c.itemconfigure(w, width=event.width),
            )
            self.pages[code] = page
            self.page_bodies[code] = inner
            self.canvases[code] = canvas
        self.bind_all("<MouseWheel>", self._wheel)
        self.bind_all("<Button-4>", self._wheel)
        self.bind_all("<Button-5>", self._wheel)
        self.build_actions_tab()
        self.build_skills_tab()
        self.build_favorites_tab()
        self.build_timers_tab()
        self.build_api_tab()
        self.build_dota_tab()
        self.build_settings_tab()
        self.build_logs_tab()
        self.build_about_tab()
        self.switch_page(self.current_page)

    def _wheel(self, event):
        if isinstance(event.widget, (ttk.Treeview, tk.Text)):
            return
        units = (
            -1
            if getattr(event, "num", 0) == 4
            else 1
            if getattr(event, "num", 0) == 5
            else -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        )
        self.canvases[self.current_page].yview_scroll(units * 3, "units")

    def switch_page(self, code):
        for key, page in self.pages.items():
            page.pack_forget()
            self.nav_buttons[key].configure(style="Accent.TButton" if key == code else "TButton")
        self.pages[code].pack(fill="both", expand=True)
        self.current_page = code
        if code == "skills":
            self.refresh_skills()
        if code == "logs":
            self.refresh_log()

    def build_actions_tab(self):
        page = self.page_bodies["actions"]
        card = self.card(
            page,
            "На связи, " + self.owner_var.get(),
            "Все навыки работают через единое ядро. Команду можно ввести текстом или произнести после обращения.",
        )
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")
        self.label(row, textvariable=self.voice_var).pack(side="left")
        ttk.Button(row, text="Включить голос", command=self.runtime.start_voice).pack(side="right")
        ttk.Button(row, text="Выключить", command=self.runtime.stop_voice).pack(
            side="right", padx=6
        )
        card = self.card(
            page,
            "Команда",
            "Например: «таймер на пять минут», «посчитай 12 * 8», «помощь».",
        )
        self.command_entry = ttk.Entry(card)
        self.command_entry.pack(fill="x", pady=5)
        self.command_entry.bind("<Return>", lambda e: self.execute_command())
        self.command_entry.bind("<Up>", lambda e: self._repeat_command())
        self.command_entry.bind("<Down>", lambda e: self._repeat_command(1))
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        ttk.Button(
            row, text="Выполнить", style="Accent.TButton", command=self.execute_command
        ).pack(side="left")
        ttk.Button(row, text="В избранное", command=self._save_favorite).pack(side="left", padx=6)
        ttk.Button(row, text="Очистить очередь", command=self._clear_pending).pack(side="left")
        self.label(card, textvariable=self.queue_var, muted=True, wraplength=680).pack(anchor="w")
        self.command_result = self.label(card, "Готов к командам.", wraplength=700)
        self.command_result.pack(anchor="w", fill="x")
        card = self.card(page, "Состояние")
        self.overview_label = self.label(card, "", wraplength=700)
        self.overview_label.pack(anchor="w")
        ttk.Button(
            card, textvariable=self.reminder_var, command=lambda: self.switch_page("timers")
        ).pack(anchor="w", pady=(8, 0))
        card = self.card(page, "Последние события")
        self.event_text = tk.Text(
            card,
            height=8,
            bg=self.theme["sidebar"],
            fg=self.theme["text"],
            font=("Segoe UI", 10),
            relief="flat",
            wrap="word",
            state="disabled",
        )
        self.event_text.pack(fill="both", expand=True)
        self._render_history()

    def execute_command(self):
        text = self.command_entry.get().strip()
        if text and self.runtime.submit(text):
            self.last_command = text
            self.command_book.remember(text)
            self._refresh_command_lists()
            self.command_result.configure(text="Выполняю: " + text)
            self._append_log("Вы: " + text)
            self.command_entry.delete(0, "end")

    def _repeat_command(self, offset=-1):
        text = self.command_book.navigate(offset, self.command_entry.get())
        self.command_entry.delete(0, "end")
        self.command_entry.insert(0, text)
        return "break"

    def _focus_command(self):
        self.switch_page("actions")
        self.canvases["actions"].yview_moveto(0)
        self.command_entry.focus_set()
        return "break"

    def _clear_pending(self):
        count = self.runtime.clear_pending()
        self.status_var.set(
            f"Удалено из очереди: {count}. Уже запущенная команда продолжит работу."
        )

    def _save_favorite(self):
        text = self.command_entry.get().strip() or self.last_command
        try:
            self.command_book.add(text)
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
            return
        self._refresh_command_lists()
        self.status_var.set("Команда сохранена в разделе «Мои команды».")

    def _build_aliases(self):
        card = self.card(
            self.page_bodies["favorites"],
            "Свои фразы",
            "Крест подставит заданную команду при точном совпадении вашей фразы — голосом или текстом, без ИИ. Изменения применятся после сохранения.",
        )
        self.alias_name = tk.StringVar()
        self.alias_target = tk.StringVar()
        self.field(card, "Моя фраза", self.alias_name)
        self.field(card, "Команда Крест", self.alias_target)
        self.alias_tree = ttk.Treeview(
            card, columns=("phrase", "command"), show="headings", height=5
        )
        self.alias_tree.heading("phrase", text="Моя фраза")
        self.alias_tree.heading("command", text="Команда")
        self.alias_tree.pack(fill="x", pady=8)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")
        ttk.Button(row, text="Добавить / обновить", command=self._add_alias).pack(side="left")
        ttk.Button(row, text="Удалить", command=self._remove_alias).pack(side="left", padx=6)
        self._refresh_aliases()

    def _refresh_aliases(self):
        self.alias_tree.delete(*self.alias_tree.get_children())
        for key, value in self.aliases.items():
            self.alias_tree.insert("", "end", iid=key, values=(key, value))

    def _add_alias(self):
        key, value = self.alias_name.get().casefold().strip(), self.alias_target.get().strip()
        if not key or len(key) > 100 or not value or len(value) > 2000:
            self._report_error("Введите фразу до 100 и команду до 2000 символов")
            return
        if key not in self.aliases and len(self.aliases) >= 100:
            self._report_error("Можно сохранить до 100 своих фраз")
            return
        self.aliases[key] = value
        self._mark_dirty()
        self._refresh_aliases()

    def _remove_alias(self):
        for key in self.alias_tree.selection():
            self.aliases.pop(key, None)
        self._mark_dirty()
        self._refresh_aliases()

    def build_favorites_tab(self):
        self.command_lists = {}
        for key, title, subtitle in (
            ("favorites", "Избранное", "Сохранённые вами команды доступны после перезапуска."),
            (
                "history",
                "История ввода",
                "Последние 100 текстовых команд этой сессии. На диск не записываются.",
            ),
        ):
            card = self.card(self.page_bodies["favorites"], title, subtitle)
            tree = ttk.Treeview(
                card, columns=("command",), show="headings", height=6, selectmode="browse"
            )
            tree.heading("command", text="Команда")
            tree.column("command", width=600)
            tree.pack(fill="x")
            self.command_lists[key] = tree
            tree.bind("<Double-1>", lambda e, k=key: self._use_saved_command(k))
            row = tk.Frame(card, bg=self.theme["card"])
            row.pack(fill="x", pady=8)
            ttk.Button(
                row, text="Подставить команду", command=lambda k=key: self._use_saved_command(k)
            ).pack(side="left")
            ttk.Button(
                row,
                text="Удалить выбранную" if key == "favorites" else "Очистить историю",
                command=lambda k=key: self._clear_saved_command(k),
            ).pack(side="left", padx=6)
        self._refresh_command_lists()
        self._build_aliases()

    def _refresh_command_lists(self):
        for key, tree in getattr(self, "command_lists", {}).items():
            selected = tree.selection()
            tree.delete(*tree.get_children())
            values = (
                self.command_book.favorites
                if key == "favorites"
                else list(reversed(self.command_book.history))
            )
            for index, value in enumerate(values):
                tree.insert("", "end", iid=str(index), values=(value,))
            if selected and tree.exists(selected[0]):
                tree.selection_set(selected[0])

    def _use_saved_command(self, kind):
        tree = self.command_lists[kind]
        if tree.selection():
            text = tree.item(tree.selection()[0], "values")[0]
            self._focus_command()
            self.command_entry.delete(0, "end")
            self.command_entry.insert(0, text)

    def _clear_saved_command(self, kind):
        tree = self.command_lists[kind]
        try:
            if kind == "history":
                self.command_book.clear_history()
                self.last_command = ""
            elif tree.selection():
                self.command_book.remove(tree.item(tree.selection()[0], "values")[0])
        except OSError as exc:
            self._report_error(str(exc))
        self._refresh_command_lists()

    def build_skills_tab(self):
        card = self.card(
            self.page_bodies["skills"],
            "Каталог навыков",
            "Выберите команду, чтобы увидеть примеры. Двойной щелчок перенесёт её в поле ввода.",
        )
        self.skill_search = tk.StringVar()
        search = ttk.Entry(card, textvariable=self.skill_search)
        search.pack(fill="x", pady=(0, 10))
        self.skill_search.trace_add("write", lambda *args: self.refresh_skills())
        self.skill_tree = ttk.Treeview(
            card,
            columns=("category", "description", "status"),
            show="headings",
            height=13,
        )
        for name, title, width in [
            ("category", "Раздел", 110),
            ("description", "Навык", 380),
            ("status", "Состояние", 150),
        ]:
            self.skill_tree.heading(name, text=title)
            self.skill_tree.column(name, width=width, minwidth=80)
        self.skill_tree.pack(fill="x")
        self.skill_tree.bind("<<TreeviewSelect>>", self._skill_selected)
        self.skill_tree.bind("<Double-1>", lambda e: self._use_skill())
        self.skill_details = self.label(card, "", wraplength=700, muted=True)
        self.skill_details.pack(anchor="w", pady=10)
        ttk.Button(card, text="Подставить команду", command=self._use_skill).pack(anchor="w")
        card = self.card(
            self.page_bodies["skills"],
            "Модули",
            "Переключатели применяются после сохранения настроек.",
        )
        for name, var in self.skill_vars.items():
            names = {
                "basic": "Время и дата",
                "interactive": "Интерактивные команды",
                "meta": "Управление ассистентом",
                "media": "Медиа и громкость",
                "utils": "Вычисления и конвертер",
                "timers_alarms": "Таймеры и будильники",
                "system_mon": "Состояние компьютера",
                "webo": "Интернет и Steam",
                "windows": "Управление Windows",
                "gid": "Гид Dota 2",
            }
            ttk.Checkbutton(card, text=names.get(name, name), variable=var).pack(anchor="w", pady=3)
        ttk.Button(
            card,
            text="Перезагрузить навыки",
            command=lambda: self.runtime.submit("перезагрузи навыки"),
        ).pack(anchor="w", pady=10)

    def refresh_skills(self):
        if not hasattr(self, "skill_tree"):
            return
        selected = self.skill_tree.selection()
        self.skill_tree.delete(*self.skill_tree.get_children())
        query = self.skill_search.get().casefold()
        self.catalog = {item.id: item for item in self.runtime.manager.catalog()}
        categories = {
            "Clipboard": "Вычисления",
            "Media": "Медиа",
            "Meta": "Ассистент",
            "Timers": "Таймеры",
            "System": "Система",
            "Steam & Gaming": "Steam",
            "Web & Info": "Интернет",
            "Interactive": "Интерактив",
        }
        for item in sorted(self.catalog.values(), key=lambda e: (e.category, e.description)):
            if (
                query
                and query
                not in (
                    item.description + " " + item.category + " " + " ".join(item.keywords)
                ).casefold()
            ):
                continue
            self.skill_tree.insert(
                "",
                "end",
                iid=item.id,
                values=(
                    categories.get(item.category, item.category),
                    item.description,
                    self.runtime.manager.availability(item),
                ),
            )
        if selected and self.skill_tree.exists(selected[0]):
            self.skill_tree.selection_set(selected[0])

    def _skill_selected(self, event=None):
        selected = self.skill_tree.selection()
        if selected:
            entry = self.catalog[selected[0]]
            examples = ", ".join(entry.examples or entry.keywords[:5]) or "; ".join(
                p.pattern for p in entry.patterns[:2]
            )
            self.skill_details.configure(text=entry.description + "\nКоманды: " + examples)

    def _use_skill(self):
        selected = self.skill_tree.selection()
        if not selected:
            return
        item = self.catalog[selected[0]]
        examples = item.examples or item.keywords
        if not examples:
            self.status_var.set("Введите команду по шаблону из описания навыка.")
            return
        self.switch_page("actions")
        self.command_entry.delete(0, "end")
        self.command_entry.insert(0, examples[0])
        self.command_entry.focus_set()

    def build_timers_tab(self):
        card = self.card(
            self.page_bodies["timers"],
            "Сработавшие напоминания",
            "Остаются здесь до подтверждения, даже после перезапуска программы.",
        )
        self.reminder_tree = ttk.Treeview(
            card, columns=("label", "time"), show="headings", height=4, selectmode="browse"
        )
        self.reminder_tree.heading("label", text="Напоминание")
        self.reminder_tree.heading("time", text="Было запланировано")
        self.reminder_tree.column("label", width=420)
        self.reminder_tree.column("time", width=170)
        self.reminder_tree.pack(fill="x")
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Готово", command=lambda: self._handle_reminder(False)).pack(
            side="left"
        )
        ttk.Button(
            row, text="Отложить на 5 минут", command=lambda: self._handle_reminder(True)
        ).pack(side="left", padx=6)
        card = self.card(self.page_bodies["timers"], "Новое напоминание")
        self.reminder_label = tk.StringVar()
        self.reminder_delay = tk.StringVar(value="20 минут")
        self.field(card, "О чём напомнить", self.reminder_label)
        self.field(card, "Через сколько", self.reminder_delay)
        ttk.Button(card, text="Создать напоминание", command=self._add_reminder).pack(
            anchor="w", pady=6
        )
        self.daily_time = tk.StringVar(value="09:00")
        self.field(card, "Каждый день в", self.daily_time)
        ttk.Button(card, text="Напоминать ежедневно", command=self._add_daily_reminder).pack(
            anchor="w", pady=6
        )
        card = self.card(
            self.page_bodies["timers"],
            "Таймеры и будильники",
            "Таймеры продолжают работать при выключенном голосе. Список сохраняется между запусками.",
        )
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        self.timer_input = ttk.Entry(row)
        self.timer_input.insert(0, "5 минут")
        self.timer_input.pack(side="left", fill="x", expand=True)
        ttk.Button(
            row,
            text="Добавить таймер",
            command=lambda: self.runtime.submit("таймер на " + self.timer_input.get()),
        ).pack(side="right", padx=8)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        self.alarm_input = ttk.Entry(row)
        self.alarm_input.insert(0, "08:30")
        self.alarm_input.pack(side="left", fill="x", expand=True)
        ttk.Button(
            row,
            text="Добавить будильник",
            command=lambda: self.runtime.submit("будильник на " + self.alarm_input.get()),
        ).pack(side="right", padx=8)
        self.timer_tree = ttk.Treeview(
            card, columns=("kind", "label", "remaining"), show="headings", height=9
        )
        for key, title in [
            ("kind", "Тип"),
            ("label", "Напоминание"),
            ("remaining", "Осталось"),
        ]:
            self.timer_tree.heading(key, text=title)
        self.timer_tree.pack(fill="x", pady=10)
        ttk.Button(card, text="Отменить выбранный", command=self._cancel_timer).pack(anchor="w")
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=12)
        for title, cmd in [
            ("Секундомер: старт", "запусти секундомер"),
            ("Проверить", "проверь секундомер"),
            ("Стоп", "останови секундомер"),
        ]:
            ttk.Button(row, text=title, command=lambda c=cmd: self.runtime.submit(c)).pack(
                side="left", padx=3
            )

    def _cancel_timer(self):
        try:
            for selected in self.timer_tree.selection():
                kind, item_id = selected.split(":", 1)
                self.runtime.state.cancel(kind, item_id)
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
        self._update_timers()

    def _add_reminder(self):
        from skills.timers_alarms import parse_timer_duration

        try:
            label = self.reminder_label.get().strip()
            if not label or len(label) > 500:
                raise ValueError("Введите текст напоминания длиной от 1 до 500 символов")
            seconds, _ = parse_timer_duration(self.reminder_delay.get())
            self.runtime.state.add_timer(seconds, label)
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
            return
        self.status_var.set("Напоминание создано: " + label)
        self.reminder_label.set("")
        self._update_timers()

    def _handle_reminder(self, snooze):
        selected = self.reminder_tree.selection()
        if not selected:
            return
        try:
            if snooze:
                self.runtime.state.snooze(selected[0])
            else:
                self.runtime.state.acknowledge(selected[0])
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
        self._update_reminders()
        self._update_timers()

    def _add_daily_reminder(self):
        try:
            label = self.reminder_label.get().strip()
            if not label or len(label) > 500:
                raise ValueError("Заполните поле «О чём напомнить» (до 500 символов)")
            self.runtime.state.add_alarm(self.daily_time.get().strip(), label, daily=True)
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
            return
        self.status_var.set("Ежедневное напоминание сохранено")
        self._update_timers()

    def _update_reminders(self):
        items = self.runtime.state.reminders()
        self.reminder_var.set(
            f"Непрочитанных напоминаний: {len(items)}" if items else "Нет непрочитанных напоминаний"
        )
        if items == self._reminder_rows:
            return
        self._reminder_rows = items
        selected = self.reminder_tree.selection()
        self.reminder_tree.delete(*self.reminder_tree.get_children())
        for item in items:
            self.reminder_tree.insert(
                "",
                "end",
                iid=item["id"],
                values=(
                    item.get("label", "Напоминание"),
                    time.strftime("%d.%m %H:%M", time.localtime(item["end_time"])),
                ),
            )
        if selected and self.reminder_tree.exists(selected[0]):
            self.reminder_tree.selection_set(selected[0])

    def _update_timers(self):
        selected = self.timer_tree.selection()
        rows = []
        for kind, items in self.runtime.state.snapshot().items():
            for item in items:
                seconds = max(0, int(item["end_time"] - time.time()))
                rows.append(
                    (
                        kind + ":" + item["id"],
                        (
                            {
                                "active_timers": "Таймер",
                                "active_alarms": "Будильник",
                                "daily_tasks": "Ежедневно",
                            }[kind],
                            item.get("label", ""),
                            f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}",
                        ),
                    )
                )
        if rows == self._timer_rows:
            return
        self._timer_rows = rows
        self.timer_tree.delete(*self.timer_tree.get_children())
        for key, values in rows:
            self.timer_tree.insert("", "end", iid=key, values=values)
        for key in selected:
            if self.timer_tree.exists(key):
                self.timer_tree.selection_add(key)

    def build_api_tab(self):
        self.api_entries = {}
        self.service_labels = {}
        self.service_details = {}
        self.service_buttons = {}
        groups = [
            (
                "Telegram Terminal",
                "Токен BotFather и ваш числовой ID. Команды бота управляют этим компьютером.",
                [
                    ("krest_bot_token", "Bot Token", True),
                    ("telegram_owner_id", "ID владельца", False),
                    ("telegram_channel_id", "ID канала (необязательно)", False),
                ],
                "krest_bot.py",
            ),
            (
                "Discord",
                "Используйте токен бота из Developer Portal. Автоответы работают в личных сообщениях боту.",
                [
                    ("discord_token", "Bot Token", True),
                    ("discord_channel_id", "Channel ID", False),
                ],
                "ds_session.py",
            ),
            (
                "Telegram User",
                "Авторизация личного аккаунта: API ID, API Hash и номер телефона.",
                [
                    ("telegram_api_id", "API ID", False),
                    ("telegram_api_hash", "API Hash", True),
                    ("telegram_phone", "Телефон", False),
                ],
                "tg_bridge.py",
            ),
            (
                "Steam",
                "Используется командами внутри Telegram Terminal.",
                [
                    ("steam_api_key", "Steam API Key", True),
                    ("steam_id", "Steam ID64", False),
                ],
                None,
            ),
        ]
        for title, description, fields, filename in groups:
            card = self.card(self.page_bodies["api"], title, description)
            if filename:
                label = self.label(card, "● Не запущен")
                label.pack(anchor="w", pady=(0, 4))
                self.service_labels[filename] = label
                details = self.label(card, "", muted=True, wraplength=680)
                details.pack(anchor="w")
                self.service_details[filename] = details
                row = tk.Frame(card, bg=self.theme["card"])
                row.pack(fill="x", pady=10)
                self.service_buttons[filename] = {}
                for action, caption in (
                    ("start", "Запустить"),
                    ("stop", "Остановить"),
                    ("restart", "Перезапустить"),
                ):
                    button = ttk.Button(
                        row,
                        text=caption,
                        style="Accent.TButton" if action == "start" else "TButton",
                        command=lambda f=filename, a=action: self._integration_action(f, a),
                    )
                    button.pack(side="left", padx=(0, 6))
                    self.service_buttons[filename][action] = button
                ttk.Button(
                    row, text="Журнал →", command=lambda f=filename: self._open_service_log(f)
                ).pack(side="right")
            credentials = tk.Frame(card, bg=self.theme["card"])
            ttk.Button(
                card,
                text="Настройка доступа ▾",
                command=lambda frame=credentials: self._toggle_credentials(frame),
            ).pack(anchor="w", pady=4)
            for key, label, secret in fields:
                self.api_entries[key] = self.field(credentials, label, self.api_vars[key], secret)
            if filename == "tg_bridge.py":
                row = tk.Frame(credentials, bg=self.theme["card"])
                row.pack(fill="x", pady=8)
                self.code_var = tk.StringVar()
                ttk.Entry(row, textvariable=self.code_var, show="•").pack(
                    side="left", fill="x", expand=True
                )
                self.auth_buttons = [
                    ttk.Button(row, text="Получить код", command=self.handle_tg_send_code),
                    ttk.Button(row, text="Войти", command=self.handle_tg_login),
                ]
                for button in self.auth_buttons:
                    button.pack(side="left", padx=5)
                self.auth_label = self.label(credentials, "Авторизация Telegram", muted=True)
                self.auth_label.pack(anchor="w")
        self.label(
            self.page_bodies["api"],
            "«Запущен» означает работающий процесс. Авторизация и сетевое подключение проверяются по журналу.",
            muted=True,
            wraplength=700,
        ).pack(anchor="w")

    def _toggle_credentials(self, frame):
        if frame.winfo_manager():
            frame.pack_forget()
        else:
            frame.pack(fill="x", pady=8)

    def _open_service_log(self, filename):
        self.log_source.set(filename)
        self.switch_page("logs")

    def build_dota_tab(self):
        card = self.card(
            self.page_bodies["dota"],
            "Dota 2 — игровой гид",
            "Локальный GSI-сервер получает состояние матча. Файл игры создаётся только кнопкой «Настроить GSI».",
        )
        for key, label in [
            ("gsi_port", "Порт GSI"),
            ("warning_lead_time", "Предупреждать за, сек."),
            ("process_name", "Процесс игры"),
        ]:
            self.field(card, label, self.dota_vars[key])
        ttk.Checkbutton(
            card,
            text="Включать гид при обнаружении матча",
            variable=self.dota_vars["auto_mode"],
        ).pack(anchor="w", pady=8)
        self.dota_label = self.label(card, "Сервер выключен", wraplength=700)
        self.dota_label.pack(anchor="w", pady=8)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")
        for title, action in [
            ("Запустить сервер", "start"),
            ("Остановить", "stop"),
            ("Настроить GSI", "setup"),
        ]:
            ttk.Button(row, text=title, command=lambda a=action: self._dota_action(a)).pack(
                side="left", padx=4
            )
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=10)
        for title, command in [
            ("Ручной старт матча", "матч начался"),
            ("Рошан убит", "убили рошана"),
            ("Статус", "статус гида"),
        ]:
            ttk.Button(row, text=title, command=lambda c=command: self.runtime.submit(c)).pack(
                side="left", padx=4
            )
        self.label(
            card,
            "Тайминги сохранены из вашего исходного гида. После обновлений Dota их нужно сверять с правилами игры.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=10)

    def build_settings_tab(self):
        card = self.card(self.page_bodies["settings"], "Профиль и оформление")
        self.entry_owner_name = self.field(card, "Ваше имя", self.owner_var)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=6)
        self.label(row, "Тема", width=24).pack(side="left")
        combo = ttk.Combobox(
            row, textvariable=self.theme_var, values=list(THEMES), state="readonly"
        )
        combo.pack(side="left", fill="x", expand=True)
        combo.bind("<<ComboboxSelected>>", lambda e: self.apply_theme(self.theme_var.get()))
        card = self.card(
            self.page_bodies["settings"],
            "Автосортировка загрузок",
            "После включения Крест проверяет выбранную папку каждые 30 секунд. Файлы должны оставаться неизменными минимум две минуты. Папки и незавершённые загрузки пропускаются.",
        )
        self.field(card, "Папка загрузок", self.settings_vars["downloads_path"])
        ttk.Button(card, text="Выбрать папку", command=self._pick_downloads).pack(anchor="w")
        ttk.Checkbutton(
            card,
            text="Сортировать загрузки автоматически",
            variable=self.settings_vars["downloads_auto_sort"],
        ).pack(anchor="w", pady=8)
        self.download_label = self.label(
            card, self.runtime.download_status, muted=True, wraplength=680
        )
        self.download_label.pack(anchor="w")
        card = self.card(
            self.page_bodies["settings"],
            "Голос и поведение",
            "Изменения модели и микрофона применяются после выключения и повторного включения голоса.",
        )
        for key, title in [
            ("wake_word", "Обращение"),
            ("active_timeout", "Ожидание команды, сек."),
            ("voice_volume", "Громкость, 0–100"),
            ("weather_city", "Город для погоды"),
            ("microphone", "Имя / индекс микрофона"),
        ]:
            self.field(card, title, self.settings_vars[key])
        for key, title in [
            ("autostart", "Автозапуск с Windows"),
            ("voice_enabled", "Озвучивать ответы"),
            ("indicator_enabled", "Показывать индикатор речи"),
            ("minimize_to_tray", "Сворачивать в трей"),
            ("close_to_tray", "При закрытии скрывать в трей"),
        ]:
            toggle = ttk.Checkbutton(card, text=title, variable=self.settings_vars[key])
            if key == "autostart" and os.name != "nt":
                toggle.configure(state="disabled")
            toggle.pack(anchor="w", pady=5)
        for key, title in [
            ("vosk_path", "Папка Vosk"),
            ("silero_path", "Файл Silero .pt"),
        ]:
            row = tk.Frame(card, bg=self.theme["card"])
            row.pack(fill="x", pady=5)
            self.field(row, title, self.settings_vars[key])
            ttk.Button(row, text="Обзор", command=lambda k=key: self._pick_model(k)).pack(
                anchor="e"
            )
        ttk.Button(card, text="Проверить аудиоустройства", command=self._audio_devices).pack(
            anchor="w", pady=8
        )
        ttk.Button(card, text="Проверить готовность голоса", command=self._voice_diagnostics).pack(
            anchor="w", pady=4
        )

    def build_logs_tab(self):
        card = self.card(self.page_bodies["logs"], "Журнал событий")
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        self.log_source = tk.StringVar(value="Приложение")
        combo = ttk.Combobox(
            row,
            textvariable=self.log_source,
            values=["Приложение", *self.service_labels],
            state="readonly",
        )
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_log())
        ttk.Button(row, text="Обновить", command=self.refresh_log).pack(side="left", padx=8)
        ttk.Button(row, text="Очистить экран", command=self._clear_history).pack(side="left")
        self.log_text = tk.Text(
            card,
            height=24,
            bg=self.theme["sidebar"],
            fg=self.theme["text"],
            font=("Consolas", 10),
            wrap="word",
            relief="flat",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)

    def build_about_tab(self):
        card = self.card(
            self.page_bodies["license"],
            "КРЕСТ · исходный код",
            "Python-проект для дальнейшей разработки. Сборка EXE и установщик не создаются.",
        )
        self.label(
            card,
            "Подключено 10 модулей навыков, общий планировщик, голос, Telegram, Discord и Dota GSI.\n\n"
            "Системные команды Windows требуют запуска на Windows. Голос требует моделей Vosk/Silero и аудиоустройств.\n\n"
            "Ключи и Telegram-сессии хранятся локально. В журнале токены скрываются.\n\n"
            "README содержит запуск, зависимости, примеры команд и ограничения проверок.",
            wraplength=690,
        ).pack(anchor="w")
        ttk.Button(card, text="Проверить модули", command=self._diagnostics).pack(
            anchor="w", pady=15
        )

    def _draft(self):
        data = self.config_mgr.snapshot()
        data["owner_name"] = self.owner_var.get().strip()
        data["selected_theme"] = self.theme_var.get()
        data["command_aliases"] = dict(self.aliases)
        data["app_settings"].update({key: var.get() for key, var in self.settings_vars.items()})
        data["api_keys"].update({key: var.get().strip() for key, var in self.api_vars.items()})
        data["skills"].update({key: var.get() for key, var in self.skill_vars.items()})
        data["dota_guide"].update({key: var.get() for key, var in self.dota_vars.items()})
        return data

    def save_all_settings(self, quiet=False):
        previous = self.config_mgr.snapshot()
        try:
            self.config_mgr.save_config(self._draft())
            if (
                previous["app_settings"]["autostart"]
                != self.config_mgr.config["app_settings"]["autostart"]
            ):
                from autostart import set_enabled

                try:
                    set_enabled(self.config_mgr.config["app_settings"]["autostart"])
                except Exception:
                    self.config_mgr.save_config(previous)
                    raise
        except (OSError, ValueError, RuntimeError, tk.TclError) as exc:
            self.status_var.set("Не удалось сохранить настройки")
            messagebox.showerror("Настройки", str(exc), parent=self)
            return False
        self.dirty = False
        self.save_button.configure(text="Сохранить изменения")
        self.status_var.set("Настройки сохранены. Для ботов и моделей нужен перезапуск.")
        self.refresh_skills()
        if not self.config_mgr.config["skills"].get("gid", True):
            from skills import gid

            self._background(gid.stop_service)
        indicator_controller.enabled = self.config_mgr.config["app_settings"]["indicator_enabled"]
        return True

    def apply_theme(self, name):
        if name not in THEMES:
            return
        self.theme = THEMES[name]
        self.theme_var.set(name)
        pending_command = self.command_entry.get()
        pending_fields = {
            key: getattr(self, key).get()
            for key in (
                "timer_input",
                "alarm_input",
                "reminder_label",
                "reminder_delay",
                "skill_search",
                "daily_time",
                "alias_name",
                "alias_target",
            )
        }
        self.shell.destroy()
        self._timer_rows = None
        self._reminder_rows = None
        self.build_ui()
        self.command_entry.insert(0, pending_command)
        for key, value in pending_fields.items():
            target = getattr(self, key)
            if isinstance(target, tk.Variable):
                target.set(value)
            else:
                target.delete(0, "end")
                target.insert(0, value)
        self._update_reminders()

    def post_ui(self, callback):
        self.ui_queue.put(callback)

    def _pick_downloads(self):
        folder = filedialog.askdirectory(parent=self, title="Папка для сортировки загрузок")
        if folder:
            self.settings_vars["downloads_path"].set(folder)

    def _background(self, operation, done=None):
        def worker():
            try:
                value = operation()
                if done:
                    self.post_ui(lambda value=value: done(value))
            except Exception as exc:
                from integrations import redact

                error = redact(str(exc), self.config_mgr.snapshot())
                self.post_ui(lambda error=error: self._report_error(error))

        threading.Thread(target=worker, daemon=True).start()

    def _report_error(self, text):
        self.status_var.set(str(text)[:160])
        self._append_log("Ошибка: " + str(text))

    def _integration_action(self, filename, action):
        if filename in self.busy_services:
            return
        if action in ("start", "restart") and not self.save_all_settings(quiet=True):
            return
        self.busy_services.add(filename)
        self.service_labels[filename].configure(text="Выполняется…")

        def operation():
            try:
                result = getattr(self.integration, action)(filename)
                self.post_ui(lambda: self._append_log(filename + ": " + result))
            except Exception as exc:
                from integrations import redact

                self.integration.errors[filename] = redact(str(exc), self.config_mgr.snapshot())
                raise
            finally:
                self.post_ui(lambda: self.busy_services.discard(filename))

        self._background(operation)

    def _auth(self, login=False):
        if self.auth_busy or not self.save_all_settings(quiet=True):
            return
        code = self.code_var.get().strip()
        if login and not code:
            self._report_error("Введите код из Telegram")
            return
        self.auth_busy = True
        for button in self.auth_buttons:
            button.configure(state="disabled")
        self.auth_label.configure(text="Проверка кода…" if login else "Запрос кода…")
        values = self.config_mgr.snapshot()["api_keys"]

        def password_callback():
            result = queue.Queue()
            self.post_ui(
                lambda: result.put(
                    simpledialog.askstring(
                        "Telegram 2FA", "Облачный пароль:", show="•", parent=self
                    )
                )
            )
            return result.get(timeout=120)

        def operation():
            try:
                self.integration.stop("tg_bridge.py")
                bridge = importlib.import_module("tg_bridge")
                if login:
                    bridge.login(code, password_callback=password_callback)
                    self.post_ui(
                        lambda: self.auth_label.configure(
                            text="Вход выполнен. Теперь можно запустить мост."
                        )
                    )
                    self.post_ui(lambda: self.code_var.set(""))
                else:
                    bridge.send_code(
                        values["telegram_api_id"],
                        values["telegram_api_hash"],
                        values["telegram_phone"],
                    )
                    self.post_ui(
                        lambda: self.auth_label.configure(text="Код отправлен. Проверьте Telegram.")
                    )
            except Exception:
                self.post_ui(
                    lambda: self.auth_label.configure(
                        text="Авторизация не завершена; подробности в журнале."
                    )
                )
                raise
            finally:
                self.post_ui(self._auth_finished)

        self._background(operation)

    def _auth_finished(self):
        self.auth_busy = False
        for button in self.auth_buttons:
            button.configure(state="normal")

    def handle_tg_send_code(self):
        self._auth()

    def handle_tg_login(self):
        self._auth(login=True)

    def _dota_action(self, action):
        if action != "stop" and not self.save_all_settings(quiet=True):
            return
        if action != "stop" and not self.config_mgr.config["skills"].get("gid", True):
            self._report_error("Модуль Dota 2 выключен на странице навыков")
            return
        settings = self.config_mgr.snapshot()["dota_guide"]

        def operation():
            from skills import gid

            if action == "start":
                port = gid.start_service(settings)
                return f"GSI слушает порт {port}"
            if action == "stop":
                gid.stop_service()
                return "Dota GSI остановлен"
            gid.GSI_PORT = int(settings["gsi_port"])
            if not gid.auto_setup_gsi_config():
                raise RuntimeError(
                    "Не удалось создать GSI-конфиг: проверьте путь Steam и существующий файл. Подробности в журнале."
                )
            return "GSI-конфиг создан. Перезапустите Dota 2."

        self._background(operation, self._append_log)

    def _pick_model(self, key):
        value = (
            filedialog.askdirectory(parent=self)
            if key == "vosk_path"
            else filedialog.askopenfilename(
                parent=self, filetypes=[("PyTorch model", "*.pt"), ("Все файлы", "*")]
            )
        )
        if value:
            self.settings_vars[key].set(value)

    def _audio_devices(self):
        def operation():
            try:
                import sounddevice
            except ImportError as exc:
                raise RuntimeError("Установите requirements-voice.txt для работы с аудио") from exc
            devices = sounddevice.query_devices()
            return (
                "\n".join(
                    f"{i}: {d['name']}"
                    for i, d in enumerate(devices)
                    if d["max_input_channels"] > 0
                )
                or "Микрофоны не найдены"
            )

        self._background(
            operation,
            lambda result: messagebox.showinfo("Доступные микрофоны", result, parent=self),
        )

    def _voice_diagnostics(self):
        from diagnostics import voice_report

        draft = self._draft()
        self._background(
            lambda: voice_report(draft),
            lambda text: messagebox.showinfo("Готовность голоса", text, parent=self),
        )

    def _diagnostics(self):
        errors = self.runtime.manager.errors
        text = f"Загружено навыков: {len(self.runtime.manager.catalog())}\n"
        text += (
            "Ошибок загрузки нет."
            if not errors
            else "\n".join(f"{key}: {value}" for key, value in errors.items())
        )
        messagebox.showinfo("Диагностика", text, parent=self)

    def _append_log(self, text):
        from integrations import redact

        text = redact(str(text), self.config_mgr.snapshot())
        if getattr(self, "_last_log_text", None) == text:
            return
        self._last_log_text = text
        self.history.append(time.strftime("%H:%M:%S") + "  " + text)
        self.history = self.history[-250:]
        self._render_history()
        if self.current_page == "logs":
            self.refresh_log()

    def _render_history(self):
        self.event_text.configure(state="normal")
        self.event_text.delete("1.0", "end")
        self.event_text.insert("end", "\n\n".join(self.history[-15:]))
        self.event_text.see("end")
        self.event_text.configure(state="disabled")

    def refresh_log(self):
        source = self.log_source.get()
        text = (
            "\n\n".join(self.history)
            if source == "Приложение"
            else self.integration.log_tail(source)
        )
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_history(self):
        self.history.clear()
        self._last_log_text = None
        self._render_history()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _poll(self):
        for _ in range(100):
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                logger.exception("Ошибка обновления интерфейса")
            if getattr(self, "_destroyed", False):
                return
        for _ in range(100):
            try:
                kind, value = self.runtime.events.get_nowait()
            except queue.Empty:
                break
            if kind == "result":
                self.command_result.configure(
                    text=value.text[:2400],
                    fg=self.theme["text" if value.success else "danger"],
                )
                self._append_log(value.text)
            elif kind == "speech":
                self._append_log(value)
                if self.config_mgr.config["app_settings"]["indicator_enabled"]:
                    indicator_controller.pulse(min(8, len(value) / 12))
            elif kind == "voice":
                self.voice_var.set(value)
            elif kind == "error":
                self._report_error(value)
            elif kind == "automation":
                self._append_log(value)
            elif kind == "alarm":
                self.status_var.set("Сработало напоминание: " + value.get("label", ""))
                self.bell()
                if self.tray_icon:
                    try:
                        self.tray_icon.notify(
                            value.get("label", "Напоминание"), "КРЕСТ — напоминание"
                        )
                    except Exception:
                        logger.warning("Уведомление трея недоступно; напоминание сохранено в меню")
            elif kind == "shutdown":
                self._real_quit()
        now = time.monotonic()
        if not hasattr(self, "_last_refresh") or now - self._last_refresh >= 1:
            self._last_refresh = now
            statuses = self.integration.refresh()
            for key, (filename, title) in self.integration.MODULES.items():
                if filename not in self.busy_services:
                    detail = self.integration.details(filename)
                    self.service_labels[filename].configure(
                        text="● " + statuses[key],
                        fg=self.theme["accent"]
                        if detail["running"]
                        else self.theme["danger"]
                        if detail["error"]
                        else self.theme["sub_text"],
                    )
                    seconds = detail["uptime"]
                    self.service_details[filename].configure(
                        text=(
                            f"PID {detail['pid']} · Работает {seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}"
                            if detail["running"]
                            else detail["error"]
                            or "Откройте «Настройка доступа», сохраните данные и запустите сервис."
                        )
                    )
                else:
                    detail = self.integration.details(filename)
                for action, button in self.service_buttons[filename].items():
                    enabled = filename not in self.busy_services and (
                        not detail["running"] if action == "start" else detail["running"]
                    )
                    button.configure(state="normal" if enabled else "disabled")
            self._update_timers()
            self._update_reminders()
            self.download_label.configure(text=self.runtime.download_status)
            current, pending = self.runtime.command_status()
            self.queue_var.set(
                ("Выполняется: " + current[:100] if current else "Готов к командам")
                + f" · В очереди: {pending}"
            )
            from skills import gid

            info = gid.service_status()
            self.dota_label.configure(
                text=(f"GSI: порт {info['port']}" if info["running"] else "GSI выключен")
                + f" · Время матча: {info['match_seconds'] // 60}:{info['match_seconds'] % 60:02}"
            )
            self.overview_label.configure(
                text=f"Навыков: {len(self.runtime.manager.catalog())} · Таймеров: {len(self.runtime.state.snapshot()['active_timers'])} · "
                + ("Режим сна" if self.runtime.state.is_sleeping else "Активен")
                + "\n"
                + " · ".join(
                    f"{self.integration.MODULES[key][1]}: {value}"
                    for key, value in statuses.items()
                )
            )
        if self.winfo_exists():
            self._poll_job = self.after(100, self._poll)

    def _on_minimize(self, event):
        if (
            event.widget is self
            and self.state() == "iconic"
            and self.config_mgr.config["app_settings"].get("minimize_to_tray")
        ):
            self.hide_to_tray()

    def hide_to_tray(self):
        try:
            if self.tray_icon is None:
                import pystray
                from PIL import Image, ImageDraw

                image = Image.new("RGB", (64, 64), "#111520")
                draw = ImageDraw.Draw(image)
                draw.line((32, 10, 32, 54), fill="#a78bfa", width=5)
                draw.line((18, 25, 46, 25), fill="#a78bfa", width=5)
                menu = pystray.Menu(
                    pystray.MenuItem(
                        "Открыть",
                        lambda *args: self.post_ui(self.deiconify),
                        default=True,
                    ),
                    pystray.MenuItem("Выход", lambda *args: self.post_ui(self._real_quit)),
                )
                self.tray_icon = pystray.Icon("krest", image, "КРЕСТ", menu)
                self.tray_icon.run_detached()
            self.withdraw()
            return True
        except Exception as exc:
            self.tray_icon = None
            self._report_error("Трей недоступен: " + str(exc))
            return False

    def on_close_window(self):
        if self.config_mgr.config["app_settings"].get("close_to_tray") and self.hide_to_tray():
            return
        self._real_quit()

    def _real_quit(self):
        if self.closing:
            return
        if self.dirty:
            answer = messagebox.askyesnocancel(
                "Настройки", "Сохранить изменения перед выходом?", parent=self
            )
            if answer is None:
                return
            if answer and not self.save_all_settings():
                return
        self.closing = True
        self.status_var.set("Завершение работы…")

        def close():
            self.integration.stop_all()
            self.runtime.close()
            if self.tray_icon:
                self.tray_icon.stop()
            self.post_ui(self._destroy_window)

        self._background(close)

    def _destroy_window(self):
        self.after_cancel(self._poll_job)
        indicator_controller.stop()
        logging.getLogger().removeHandler(self._log_handler)
        self._destroyed = True
        self.destroy()


def open_menu_gui():
    app = ControlCenterApp()
    app.mainloop()


if __name__ == "__main__":
    open_menu_gui()
