"""Source-only Tk control centre. Tk widgets are accessed on the main thread only."""

from ui_fonts import UI_FONT, load_fonts

import copy
import importlib
import logging
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
import voice
from tkinter import ttk, filedialog
from ui_dialogs import messagebox, simpledialog
from core_pages import CorePages
from skill_import_pages import SkillImportPages
from warframe_pages import WarframePages, PAGE_NAMES
from appearance_pages import AppearancePages
from experience_pages import ExperiencePages
from personal_pages import PersonalPages
from autonomy_pages import AutonomyPages
from smart_pages import SmartPages
from brain_pages import BrainPages

from app_runtime import AssistantRuntime
from configuration import ConfigManager, DEFAULT_CONFIG, CONFIG_PATH
from integrations import IntegrationManager
from themes import THEMES, PREMIUM_THEMES
from guikrest import indicator_controller
from command_book import CommandBook
from command_catalog import CommandCatalogPages
from service_pages import ServicePages
from voice_page import VoicePages
from product_pages import ProductPages, PERSONALITIES
from workflow_pages import WorkflowPages, MODES, NOTICE_MODES
import licensing
from ui_design import apply_style, RoundedCard, Disclosure
from app_icons import set_process_identity, install_window_icon, load_image
from desktop_shell import DesktopShell

logger = logging.getLogger(__name__)
BASE_DIR = str(Path(__file__).resolve().parent)
TRAY_SUPPORTED = False  # Discovered lazily when the user minimizes to tray.


class ControlCenterApp(
    BrainPages,
    SmartPages,
    AutonomyPages,
    PersonalPages,
    ExperiencePages,
    WarframePages,
    CommandCatalogPages,
    AppearancePages,
    SkillImportPages,
    CorePages,
    DesktopShell,
    WorkflowPages,
    ProductPages,
    VoicePages,
    ServicePages,
    tk.Tk,
):
    NAV = PAGE_NAMES + [
        ("appearance", "Оформление"),
        ("actions", "Обзор"),
        ("voice", "Голос"),
        ("music", "Музыка"),
        ("skills", "Список команд"),
        ("favorites", "Мои команды"),
        ("timers", "Таймеры"),
        ("scenarios", "Сценарии"),
        ("inbox", "Уведомления"),
        ("maintenance", "Нагрузка и файлы"),
        ("api", "Telegram"),
        ("steam", "Steam / CS2"),
        ("bots", "Центр подключений"),
        ("discord_account", "Discord · мой аккаунт"),
        ("discord_bot", "Discord · серверный бот"),
        ("scripts", "Скрипты и мосты"),
        ("settings", "Настройки"),
        ("logs", "Журнал"),
        ("license", "Premium"),
        ("kernel", "Состояние ядра"),
        ("brain", "Мозг"),
        ("memory", "Память"),
        ("events", "События"),
        ("programs", "Программы и звук"),
        ("security", "Доступ и восстановление"),
        ("clipboard", "Буфер обмена"),
        ("sessions", "Игровые сессии"),
        ("autonomy", "Дежурство"),
        ("smart_files", "Обработка загрузок"),
        ("watches", "Наблюдение"),
        ("sound", "Звуковой диспетчер"),
        ("habits", "Привычки"),
    ]

    NAV_GROUPS = [
        ("actions", "Главная", ("actions", "voice")),
        ("skills", "Команды", ("skills", "favorites")),
        ("music", "Музыка", ("music", "sound")),
        ("timers", "Дела и сценарии", ("timers", "scenarios", "events", "memory")),
        ("autonomy", "Автоматизация", ("autonomy", "smart_files", "watches", "habits")),
        ("inbox", "Уведомления", ("inbox", "logs")),
        (
            "bots",
            "Подключения",
            ("bots", "steam", "api", "discord_account", "discord_bot", "scripts"),
        ),
        (
            "kernel",
            "Система",
            ("kernel", "brain", "programs", "clipboard", "maintenance", "security"),
        ),
        ("sessions", "Игры", ("sessions",) + tuple(code for code, _ in PAGE_NAMES)),
        ("settings", "Настройки", ("settings", "appearance", "license")),
    ]

    def __init__(self, remote=None, config_path=None):
        load_fonts()
        set_process_identity()
        super().__init__()
        self._window_icons = install_window_icon(self)
        self.title("KREST OS · Реликт")
        width = min(1180, max(900, self.winfo_screenwidth() - 60))
        height = min(820, max(600, self.winfo_screenheight() - 80))
        self.geometry(f"{width}x{height}")
        self.minsize(min(1020, width), min(700, height))
        self.protocol("WM_DELETE_WINDOW", self.on_close_window)
        self.bind("<Control-s>", lambda event: self.save_all_settings())
        self.config_mgr = ConfigManager(config_path) if config_path else ConfigManager()
        from local_setup import discover_settings, readiness

        self.setup_message = ""
        try:
            detected, changes = discover_settings(self.config_mgr.snapshot())
            if changes:
                self.config_mgr.save_config(detected)
                self.setup_message = "Определено автоматически: " + ", ".join(changes)
        except (OSError, ValueError) as exc:
            self.setup_message = "Автонастройка: " + str(exc)
        self.auto_setup_info = tk.StringVar(value=readiness(self.config_mgr.snapshot()))
        self.auto_setup_busy = False
        self.command_book = CommandBook(self.config_mgr.filepath.parent / "favorites.json")
        self.ui_queue = queue.Queue()
        self.closing = False
        self.current_page = "actions"
        self.init_desktop_shell()
        self.busy_services = set()
        self.auth_busy = False
        self.history = []
        self.aliases = self.config_mgr.snapshot().get("command_aliases", {})
        self.last_command = ""
        self._timer_rows = None
        self._reminder_rows = None
        self.runtime = remote or AssistantRuntime(
            self.config_mgr.snapshot,
            data_dir=self.config_mgr.filepath.parent,
            config_manager=self.config_mgr,
        )
        self.integration = remote.integrations if remote else IntegrationManager(self)
        self.init_voice_page()
        self.init_product_pages()
        self.init_workflows()
        self.init_warframe()
        self.service_configs = self.config_mgr.snapshot()["services"]
        self.service_autostart = {}
        self.service_restart = {}
        self.steam_vars = {
            k: (tk.BooleanVar(value=v) if isinstance(v, bool) else tk.StringVar(value=v))
            for k, v in self.config_mgr.config["steam"].items()
        }
        self.presence_vars = {
            k: (tk.BooleanVar(value=v) if isinstance(v, bool) else tk.StringVar(value=v))
            for k, v in self.config_mgr.config["discord_presence"].items()
        }
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
        self.reply_vars = {
            key: (
                tk.BooleanVar(self, value=value)
                if isinstance(value, bool)
                else tk.StringVar(self, value=value)
            )
            for key, value in self.config_mgr.config["auto_replies"].items()
        }
        self.account_vars = {
            key: (
                tk.BooleanVar(self, value=value)
                if isinstance(value, bool)
                else tk.StringVar(self, value=value)
            )
            for key, value in self.config_mgr.config["discord_account"].items()
        }
        self.bot_vars = {
            key: tk.BooleanVar(self, value=value)
            if isinstance(value, bool)
            else tk.StringVar(self, value=value)
            for key, value in self.config_mgr.config["discord_bot"].items()
        }
        self.tg_vars = {
            key: tk.BooleanVar(self, value=value)
            if isinstance(value, bool)
            else tk.StringVar(self, value=value)
            for key, value in self.config_mgr.config["telegram_user"].items()
        }
        self.status_var = tk.StringVar(
            value="Скажите «Крест, который час?» — после подключения микрофона."
        )
        self.queue_var = tk.StringVar(value="Нет команд в очереди")
        self.reminder_var = tk.StringVar(value="Нет непрочитанных напоминаний")
        self.voice_var = tk.StringVar(value="Голос выключен")
        self.dirty = False
        for variable in [
            self.owner_var,
            self.personality_var,
            self.profile_var,
            self.game_processes_var,
            self.notice_mode_var,
            self.game_notices_var,
            self.sender_notices_var,
            self.media_source_var,
            self.theme_var,
            *self.settings_vars.values(),
            *self.api_vars.values(),
            *self.skill_vars.values(),
            *self.reply_vars.values(),
            *self.account_vars.values(),
            *self.bot_vars.values(),
            *self.tg_vars.values(),
            *self.steam_vars.values(),
            *self.presence_vars.values(),
        ]:
            variable.trace_add("write", self._mark_dirty)
        if self.theme_var.get() in PREMIUM_THEMES and not self.config_mgr.is_premium():
            self.theme_var.set("Graphite")
        self.theme = THEMES.get(self.theme_var.get(), THEMES["Graphite"])

        class UILogHandler(logging.Handler):
            def emit(handler, record):
                self.ui_queue.put(lambda text=handler.format(record): self._append_log(text))

        self._log_handler = UILogHandler(level=logging.WARNING)
        logging.getLogger().addHandler(self._log_handler)
        self._base_scaling = float(self.tk.call("tk", "scaling"))
        self.tk.call(
            "tk",
            "scaling",
            self._base_scaling * self.config_mgr.snapshot()["accessibility"]["font_scale"] / 100,
        )
        from ui_design import MotionController

        self.motion = MotionController(self)
        self.build_ui()
        from quick_panel import show_panel

        self.bind("<Control-k>", lambda e: show_panel(self))
        self.bind("<Control-l>", lambda e: self._focus_command())
        self.bind("<F8>", lambda e: self._arm_voice())
        self.bind("<Escape>", lambda e: voice.stop_speaking())
        self.bind("<Unmap>", self._on_minimize)
        indicator_controller.attach(self)
        self._poll_job = self.after(
            150 if self.state() in ("normal", "zoomed") else 600, self._poll
        )
        self._show_welcome()
        if self.setup_message:
            self._append_log(self.setup_message)
        if self.config_mgr.load_error:
            self._append_log(
                "Настройки повреждены: "
                + self.config_mgr.load_error
                + ". При сохранении исходный файл будет скопирован в .broken."
            )
        for warning in getattr(self.config_mgr, "secret_errors", []):
            self._append_log(warning)
        for error in (self.runtime.state.load_error, self.command_book.load_error):
            if error:
                self._report_error(error + ". Исходный файл будет сохранён в .broken при записи.")

    def _mark_dirty(self, *args):
        self.dirty = True
        if hasattr(self, "save_button"):
            self.save_button.config(text="Сохранить •")

    def _style(self):
        apply_style(self, self.theme)

    def label(self, parent, text="", large=False, muted=False, **kwargs):
        label = tk.Label(
            parent,
            text=text,
            bg=parent.cget("bg"),
            fg=self.theme["sub_text" if muted else "text"],
            font=(UI_FONT, 20 if large else 12, "bold" if large else "normal"),
            anchor="w",
            justify="left",
            **kwargs,
        )
        if kwargs.get("wraplength"):
            maximum = int(kwargs["wraplength"])
            previous = [0]

            def wrap(event):
                if event.width < 30 or event.width == previous[0] or not label.winfo_exists():
                    return
                previous[0] = event.width
                available = event.width - max(4, label.winfo_x()) - 6
                label.configure(wraplength=max(20, min(maximum, available)))

            parent.bind("<Configure>", wrap, add="+")
        return label

    def card(self, parent, title, subtitle=""):
        surface = RoundedCard(parent, self.theme)
        surface.pack(fill="x", pady=(0, 16))
        frame = surface.inner
        heading = self.label(frame, title)
        heading.configure(font=(UI_FONT, 14, "bold"))
        heading.pack(anchor="w", pady=(0, 8))
        if subtitle:
            self.label(frame, subtitle, muted=True, wraplength=680).pack(anchor="w", pady=(0, 12))
        return frame

    def advanced(self, parent, title="Дополнительные настройки"):
        return Disclosure(parent, title, self.theme).body

    def field(self, parent, title, variable, secret=False, paste_button=False):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=5)
        self.label(row, title, width=24).pack(side="left")
        entry = ttk.Entry(row, textvariable=variable, show="•" if secret else "")
        entry.pack(side="left", fill="x", expand=True)
        from entry_clipboard import attach, paste

        attach(entry, self.theme)
        if secret or paste_button:
            ttk.Button(row, text="Вставить", command=lambda: paste(entry, replace=True)).pack(
                side="right", padx=(6, 0)
            )
        if secret:
            ttk.Button(
                row,
                text="Показать",
                command=lambda e=entry: e.configure(show="" if e.cget("show") else "•"),
            ).pack(side="right", padx=(6, 0))
        return entry

    def build_ui(self):
        if getattr(self, "_warm_job", None):
            self.after_cancel(self._warm_job)
            self._warm_job = None
        self._style()
        self.shell = tk.Frame(self, bg=self.theme["bg"])
        self.shell.pack(fill="both", expand=True)
        from PIL import ImageTk

        side = tk.Frame(self.shell, bg=self.theme["sidebar"], width=246, padx=14, pady=22)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self._brand_image = ImageTk.PhotoImage(load_image(44), master=self)
        brand = tk.Frame(side, bg=self.theme["sidebar"])
        brand.pack(fill="x", pady=(0, 8))
        from ui_design import BrandMark

        self.brand_mark = BrandMark(brand, self._brand_image, self.theme, self.motion.enabled)
        self.brand_mark.pack(side="left", padx=(0, 10))
        from ui_relic import wordmark

        wordmark(brand, self.theme, 140).pack(side="left")
        self.label(side, "РЕЛИКТ / ЛИЧНОЕ ЯДРО", muted=True).pack(anchor="w", padx=4, pady=(0, 18))
        self.edition_label = self.label(side, "ОБЫЧНАЯ ВЕРСИЯ", muted=True)
        self.edition_label.pack(side="bottom", anchor="w", pady=(14, 0))
        ttk.Button(
            side, text="Завершить работу", style="Nav.TButton", command=self._real_quit
        ).pack(side="bottom", fill="x")
        ttk.Button(
            side, text="Свернуть в трей", style="Nav.TButton", command=self.hide_to_tray
        ).pack(side="bottom", fill="x", pady=6)
        from ui_scroll import ScrollViewport

        self.nav_canvas = ScrollViewport(side, bg=self.theme["sidebar"], width=222)
        self.nav_canvas.pack(fill="both", expand=True)
        self.nav_frame = self.nav_canvas.content
        self.nav_buttons = {}
        self.group_buttons = {}
        for default, title, codes in self.NAV_GROUPS:
            button = ttk.Button(
                self.nav_frame,
                text=title,
                style="Nav.TButton",
                padding=(16, 11),
                command=lambda k=default: self.switch_page(k),
            )
            button.pack(fill="x", pady=4)
            self.group_buttons[default] = button
            for key in codes:
                self.nav_buttons[key] = button
        body = tk.Frame(self.shell, bg=self.theme["bg"], padx=24, pady=20)
        body.pack(side="left", fill="both", expand=True)
        from ui_relic import ThornVine

        self.relic_edges = []
        if self.config_mgr.snapshot().get("accessibility", {}).get("relic_edges", True):
            for right in (False, True):
                vine = ThornVine(body, self.theme, vertical=True, width=18)
                vine.place(
                    relx=1 if right else 0,
                    x=-20 if right else 2,
                    y=2,
                    relheight=1,
                    height=-4,
                    bordermode="outside",
                )
                self.relic_edges.append(vine)
        header = tk.Frame(body, bg=self.theme["bg"])
        header.pack(fill="x", pady=(0, 10))
        ttk.Button(
            header, text="Поиск  Ctrl+K", command=lambda: __import__("quick_panel").show_panel(self)
        ).pack(side="right", anchor="n")
        ttk.Button(header, text="Разбор команды", command=self._open_command_parser).pack(
            side="right", anchor="n", padx=6
        )
        self.page_title = tk.StringVar()
        self.page_subtitle = tk.StringVar()
        heading = tk.Frame(header, bg=self.theme["bg"])
        heading.pack(side="left", fill="both", expand=True)
        title = self.label(heading, textvariable=self.page_title, wraplength=700)
        title.configure(font=(UI_FONT, 24, "bold"))
        title.pack(anchor="w")
        self.label(heading, textvariable=self.page_subtitle, muted=True, wraplength=700).pack(
            anchor="w", pady=(6, 0)
        )
        self._subnav_group = None
        self._subnav_buttons = {}
        self.subnav = tk.Frame(body, bg=self.theme["bg"])
        self.subnav.pack(fill="x", pady=(0, 10))
        self.subnav.bind("<Configure>", lambda event: self._layout_subnav())
        self._subnav_columns = None
        footer = tk.Frame(body, bg=self.theme["bg"])
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.save_button = ttk.Button(
            footer,
            text="Сохранить" + (" •" if self.dirty else ""),
            style="Accent.TButton",
            command=self.save_all_settings,
        )
        self.save_button.pack(side="right")
        self.label(footer, textvariable=self.status_var, muted=True, wraplength=560).pack(
            side="left"
        )
        self.transition_bar = tk.Canvas(body, height=2, bg=self.theme["bg"], highlightthickness=0)
        self.transition_bar.pack(fill="x", pady=(0, 6))
        self._transition_job = None
        self.content = tk.Frame(body, bg=self.theme["bg"])
        self.content.pack(fill="both", expand=True)
        self.pages = {}
        self.page_bodies = {}
        self.canvases = {}
        for code, _ in self.NAV:
            page = tk.Frame(self.content, bg=self.theme["bg"])
            canvas = ScrollViewport(page, bg=self.theme["bg"])
            scroll = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
            scroll.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            canvas.set_scrollbar(scroll.set)
            inner = canvas.content
            self.pages[code] = page
            self.page_bodies[code] = inner
            self.canvases[code] = canvas
        self.bind_all("<MouseWheel>", self._wheel)
        self.bind_all("<Button-4>", self._wheel)
        self.bind_all("<Button-5>", self._wheel)
        self.wf_trees, self.wf_output_widgets = {}, {}
        self._built_pages = set()
        self.api_entries = {}
        self.service_labels, self.service_details, self.service_buttons = {}, {}, {}
        self._locked_pages = {}
        self._ensure_page("actions")
        from ui_design import style_titlebar

        self.after_idle(lambda: style_titlebar(self, self.theme))
        self.switch_page(self.current_page)
        # Keep unopened heavy pages lazy. Their Tk widgets and queries used to
        # compete with pointer/scroll events throughout the first minute.
        self._warm_pages = ["voice", "music"]
        self._last_interaction = time.monotonic()
        self._warm_job = self.after(3000, self._warm_next_page)

    def _warm_next_page(self):
        self._warm_job = None
        if self.closing or getattr(self, "_destroyed", False) or not self._warm_pages:
            return
        if (
            self.state() not in ("normal", "zoomed")
            or time.monotonic() - self._last_interaction < 2
        ):
            self._warm_job = self.after(600, self._warm_next_page)
            return
        if getattr(self.runtime.performance, "game", ""):
            self._warm_job = self.after(3000, self._warm_next_page)
            return
        code = self._warm_pages.pop(0)
        if code not in self.BOT_PAGES or self.config_mgr.is_premium():
            try:
                self._ensure_page(code)
            except Exception:
                self._built_pages.discard(code)
                logger.exception("Предзагрузка страницы %s", code)
        self._warm_job = self.after(1500, self._warm_next_page)

    BOT_PAGES = {"bots", "api", "steam", "discord_account", "discord_bot", "scripts"}
    CORE_PAGES = {
        "timers",
        "scenarios",
        "inbox",
        "maintenance",
        "kernel",
        "memory",
        "events",
        "programs",
        "security",
    }

    def _ensure_page(self, code):
        if code in self._built_pages:
            return
        if code in self.CORE_PAGES:
            self._built_pages.add(code)
            self.build_core_pages(code)
        elif code.startswith("wf_"):
            self._built_pages.add(code)
            if not hasattr(self, "wf_trees"):
                self.wf_trees, self.wf_output_widgets = {}, {}
            getattr(self, "build_" + code)()
        else:
            self._built_pages.add(code)
            builders = {"license": self.build_premium_tab, "appearance": self.build_appearance}
            builders.get(code, lambda: getattr(self, "build_" + code + "_tab")())()

    def _show_locked_page(self, code):
        if code not in self._locked_pages:
            frame = tk.Frame(self.content, bg=self.theme["bg"])
            card = self.card(
                frame, "Подключения · Premium", "Боты и мосты доступны после активации кода."
            )
            self.label(
                card,
                "Голос, музыка и помощник Warframe уже доступны в обычной версии.",
                wraplength=620,
                muted=True,
            ).pack(anchor="w", pady=16)
            ttk.Button(
                card,
                text="Открыть Premium",
                style="Accent.TButton",
                command=lambda: self.switch_page("license"),
            ).pack(anchor="w")
            self._locked_pages[code] = frame
        self._locked_pages[code].pack(fill="both", expand=True)

    def _wheel(self, event):
        self._last_interaction = time.monotonic()
        if isinstance(event.widget, (ttk.Treeview, tk.Text, tk.Listbox)) or (
            isinstance(event.widget, str) and "popdown" in event.widget
        ):
            return
        units = (
            -1.0
            if getattr(event, "num", 0) == 4
            else 1
            if getattr(event, "num", 0) == 5
            else -event.delta / 120
        )
        widget = event.widget
        while widget is not None:
            if widget is self.nav_canvas or widget is self.nav_frame:
                canvas = self.nav_canvas
                break
            widget = getattr(widget, "master", None)
        else:
            canvas = self.canvases[self.current_page]
        if not hasattr(self, "_wheel_pending"):
            self._wheel_pending = {}
        self._wheel_pending[canvas] = self._wheel_pending.get(canvas, 0) + units * 48
        if not getattr(self, "_wheel_job", None):
            self._wheel_job = self.after(16, self._flush_wheel)
        return "break"

    def _flush_wheel(self):
        self._wheel_job = None
        pending, self._wheel_pending = self._wheel_pending, {}
        for canvas, pixels in pending.items():
            if canvas.winfo_exists():
                canvas.yview_scroll(round(pixels), "units")

    def switch_page(self, code):
        self._last_interaction = time.monotonic()
        names = dict(self.NAV)
        for page in (
            self.pages.get(getattr(self, "current_page", "")),
            self._locked_pages.get(getattr(self, "current_page", "")),
        ):
            if page is not None:
                page.pack_forget()
        group = next(g for g in self.NAV_GROUPS if code in g[2])
        for key, button in self.group_buttons.items():
            button.configure(style="Selected.Nav.TButton" if key == group[0] else "Nav.TButton")
        self.page_title.set(names[code])
        subtitles = {
            "actions": "Всё нужное для вашего дня — в одном месте.",
            "skills": "Что Крест умеет сейчас: фразы, примеры и ваши команды.",
            "music": "Ваш плеер, текущий трек и управление воспроизведением.",
            "timers": "Освободите голову. Крест напомнит и выполнит привычные действия.",
            "autonomy": "Поручения выполняются, когда компьютер свободен.",
            "inbox": "События, которые важно не пропустить.",
            "steam": "Игры, боты и ваши сервисы.",
            "bots": "Подключения, состояние мостов и результаты действий.",
            "kernel": "Крест работает рядом — даже когда меню закрыто.",
            "wf_home": "Ваш второй пилот. Мир игры, планы и важные моменты.",
            "sessions": "Игровые сессии, заметки и помощник Warframe.",
            "settings": "Настройте под себя. Остальное Крест подберёт автоматически.",
        }
        self.page_subtitle.set(subtitles[group[0]])
        if self._subnav_group != group[0]:
            for child in self.subnav.winfo_children():
                child.destroy()
            self._subnav_buttons = {}
            self._subnav_columns = None
            self._subnav_group = group[0]
            if len(group[2]) > 1:
                for i, key in enumerate(group[2]):
                    button = ttk.Button(self.subnav, command=lambda k=key: self.switch_page(k))
                    button.grid(row=i // 4, column=i % 4, sticky="ew", padx=(0, 5), pady=(0, 4))
                    self._subnav_buttons[key] = button
                for i in range(4):
                    self.subnav.columnconfigure(i, weight=1, uniform="tabs")
        for key, button in self._subnav_buttons.items():
            short_names = {
                "sessions": "Сессии",
                "kernel": "Ядро",
                "security": "Доступ и копии",
                "programs": "Программы",
            }
            button.configure(
                text=short_names.get(key, names[key])
                + (
                    " · Premium"
                    if key in self.BOT_PAGES and not self.config_mgr.is_premium()
                    else ""
                ),
                style="Selected.Tab.TButton" if key == code else "Tab.TButton",
            )
        self._layout_subnav()
        self.after_idle(self._layout_subnav)
        self.current_page = code
        if code in self.BOT_PAGES and not self.config_mgr.is_premium():
            self._show_locked_page(code)
            return
        self._ensure_page(code)
        self.pages[code].pack(fill="both", expand=True)
        self.canvases[code]._schedule()
        self.after_idle(self.canvases[code].redraw)
        self._animate_transition()
        if code.startswith("wf_"):
            self.wf_refresh()
        if code == "skills":
            self.refresh_skills()
        if code == "logs":
            self.refresh_log()
        if code == "music":
            self._refresh_music()
        if code == "timers":
            self._update_timers()
            self._update_reminders()
        if code == "inbox":
            self._refresh_inbox()
        if code == "kernel":
            self._refresh_core()
        if code == "brain":
            self._brain_refresh(force=True)
        if code == "memory":
            self._refresh_memory()
        if code == "events":
            self.event_scenario_combo.configure(
                values=list(self.config_mgr.snapshot()["scenarios"])
            )

    def _layout_subnav(self):
        if not self._subnav_buttons or self.subnav.winfo_width() < 30:
            return
        widest = max(button.winfo_reqwidth() for button in self._subnav_buttons.values()) + 6
        columns = max(1, min(4, self.subnav.winfo_width() // widest))
        if columns == self._subnav_columns:
            return
        self._subnav_columns = columns
        for column in range(4):
            self.subnav.columnconfigure(
                column,
                weight=1 if column < columns else 0,
                uniform="tabs" if column < columns else "",
            )
        for index, button in enumerate(self._subnav_buttons.values()):
            button.grid_configure(row=index // columns, column=index % columns)

    def _preview_personality(self):
        from types import SimpleNamespace
        from personality import reply
        from action_replies import action_reply

        cfg = self._draft()
        state = SimpleNamespace(owner_name=cfg["owner_name"], config_getter=lambda: cfg)
        messagebox.showinfo(
            "Характер Крест",
            reply("greeting", state)
            + "\n\n"
            + reply("support", state)
            + "\n\nНа «переключи трек»: «"
            + action_reply("skills.media.next_track", "Включила следующий трек.", state)
            + "»",
            parent=self,
        )

    def _open_command_parser(self):
        from command_parser import open_parser

        open_parser(self)

    def _animate_transition(self):
        if self._transition_job:
            self.after_cancel(self._transition_job)
            self._transition_job = None
        self.transition_bar.delete("all")
        if not self.motion.enabled():
            return
        bar = self.transition_bar
        began = time.monotonic()
        item = bar.create_rectangle(0, 0, 0, 2, fill=self.theme["accent"], outline="")

        def frame():
            self._transition_job = None
            if not bar.winfo_exists():
                return
            elapsed = (time.monotonic() - began) / 0.18
            if elapsed >= 1:
                bar.delete("all")
                return
            width = bar.winfo_width()
            x = width * (1 - (1 - elapsed) ** 3)
            bar.coords(item, max(0, x - width * 0.3), 0, x, 2)
            self._transition_job = self.after(16, frame)

        frame()

    def build_actions_tab(self):
        from relic_dashboard import build

        build(self)

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
        if not self.manual_input.opened:
            self.manual_input.toggle()
        self.update_idletasks()
        self.canvases["actions"].yview_moveto(
            self.manual_input.winfo_y() / max(1, self.page_bodies["actions"].winfo_height())
        )
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
            "Пишите фразу с обращением «Крест» или без него. Например: «Крест дальше» → «Крест переключи трек». Кнопка «Добавить / обновить» сразу сохраняет фразу.",
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
        from speech_tools import alias_body, normalize_command

        wake = self.config_mgr.snapshot()["app_settings"]["wake_word"]
        key = alias_body(self.alias_name.get(), wake).casefold().strip(" .!?,")
        value = alias_body(self.alias_target.get(), wake)
        if not key or len(key) > 100 or not value or len(value) > 2000:
            self._report_error("Введите фразу до 100 и команду до 2000 символов")
            return
        if key not in self.aliases and len(self.aliases) >= 100:
            self._report_error("Можно сохранить до 100 своих фраз")
            return
        value = normalize_command(value)
        if not self.runtime.manager.match(value):
            self._report_error("Команда Крест не найдена. Выберите пример в «Списке команд».")
            return
        self._save_aliases({**self.aliases, key: value})

    def _remove_alias(self):
        aliases = dict(self.aliases)
        for key in self.alias_tree.selection():
            aliases.pop(key, None)
        self._save_aliases(aliases)

    def _save_aliases(self, aliases):
        cfg = self.config_mgr.snapshot()
        cfg["command_aliases"] = aliases
        try:
            self.config_mgr.save_config(cfg)
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
            return
        self.aliases = self.config_mgr.snapshot()["command_aliases"]
        self._refresh_aliases()
        self.refresh_skills()
        self.status_var.set("Свои фразы сохранены и уже работают.")

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
        ttk.Button(row, text="Отложить", command=lambda: self._handle_reminder(True)).pack(
            side="left", padx=6
        )
        ttk.Combobox(
            row, textvariable=self.snooze_delay, values=["5 минут", "15 минут", "1 час"], width=14
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
        self.field(card, "Напоминать в", self.daily_time)
        days_row = tk.Frame(card, bg=self.theme["card"])
        days_row.pack(fill="x", pady=6)
        for name, var in zip(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"], self.weekdays):
            ttk.Checkbutton(days_row, text=name, variable=var).pack(side="left")
        ttk.Button(card, text="Сохранить расписание", command=self._add_daily_reminder).pack(
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

        card = self.card(
            self.page_bodies["timers"],
            "История выполнения",
            "Последние 30 завершённых напоминаний.",
        )
        self.completed_label = self.label(
            card, "Пока нет выполненных дел", muted=True, wraplength=680
        )
        self.completed_label.pack(anchor="w", pady=12)

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
                from skills.timers_alarms import parse_timer_duration

                self.runtime.state.snooze(
                    selected[0],
                    parse_timer_duration(self.snooze_delay.get())[0],
                )
            else:
                self.runtime.state.complete(selected[0])
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
        self._update_reminders()
        self._update_timers()

    def _add_daily_reminder(self):
        try:
            label = self.reminder_label.get().strip()
            if not label or len(label) > 500:
                raise ValueError("Заполните поле «О чём напомнить» (до 500 символов)")
            self.runtime.state.add_alarm(
                self.daily_time.get().strip(),
                label,
                daily=True,
                weekdays=[i for i, var in enumerate(self.weekdays) if var.get()],
            )
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
            return
        self.status_var.set("Ежедневное напоминание сохранено")
        self._update_timers()

    def _update_reminders(self, update_rows=True):
        items = self.runtime.state.reminders()
        text = (
            f"Непрочитанных напоминаний: {len(items)}" if items else "Нет непрочитанных напоминаний"
        )
        if self.reminder_var.get() != text:
            self.reminder_var.set(text)
        if not update_rows:
            return
        if "timers" not in self._built_pages or items == self._reminder_rows:
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
        if "timers" not in self._built_pages:
            return
        rows = []
        for kind, items in self.runtime.state.snapshot().items():
            if kind == "completed_tasks":
                continue
            for item in items:
                seconds = max(0, int(item["end_time"] - time.time()))
                rows.append(
                    (
                        kind + ":" + item["id"],
                        (
                            {
                                "active_timers": "Таймер",
                                "active_alarms": "Будильник",
                                "daily_tasks": " / ".join(
                                    ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][d]
                                    for d in item.get("weekdays", range(7))
                                ),
                            }[kind],
                            item.get("label", ""),
                            f"{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}",
                        ),
                    )
                )
        if rows == self._timer_rows:
            return
        previous = dict(self._timer_rows or ())
        current = dict(rows)
        for key in self.timer_tree.get_children():
            if key not in current:
                self.timer_tree.delete(key)
        order_changed = list(previous) != list(current)
        for index, (key, values) in enumerate(rows):
            if not self.timer_tree.exists(key):
                self.timer_tree.insert("", "end", iid=key, values=values)
            elif previous.get(key) != values:
                self.timer_tree.item(key, values=values)
            if order_changed:
                self.timer_tree.move(key, "", index)
        self._timer_rows = rows

    def build_api_tab(self):
        self.build_reply_controls(self.page_bodies["api"])
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
                "Telegram User",
                "Авторизация личного аккаунта: API ID, API Hash и номер телефона.",
                [
                    ("telegram_api_id", "API ID", False),
                    ("telegram_api_hash", "API Hash", True),
                    ("telegram_phone", "Телефон", False),
                ],
                "tg_bridge.py",
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
            if filename:
                self.service_options(card, filename)
            if filename == "krest_bot.py":
                ttk.Button(
                    card,
                    text="Включить базовый пульт ПК",
                    style="Accent.TButton",
                    command=self._enable_telegram_remote,
                ).pack(anchor="w", pady=8)
                self.label(
                    card,
                    "Затем напишите боту /menu: музыка, микрофон Discord, скриншоты, дела, игры и файлы пульта. Доступ — только вашему ID в личном диалоге.",
                    muted=True,
                    wraplength=660,
                ).pack(anchor="w", pady=6)
            credentials = tk.Frame(card, bg=self.theme["card"])
            ttk.Button(
                card,
                text="Настройка доступа ▾",
                command=lambda frame=credentials: self._toggle_credentials(frame),
            ).pack(anchor="w", pady=4)
            for key, label, secret in fields:
                self.api_entries[key] = self.field(credentials, label, self.api_vars[key], secret)
            if filename == "tg_bridge.py":
                for key, caption in (
                    ("auto_reply", "Автоответы с моего Telegram-аккаунта"),
                    ("update_bio", "Обновлять описание моего профиля"),
                    ("follow_activity", "Учитывать игру в статусе и автоответах"),
                ):
                    ttk.Checkbutton(card, text=caption, variable=self.tg_vars[key]).pack(
                        anchor="w", pady=4
                    )
                self.field(card, "Описание профиля", self.tg_vars["bio_text"])
                self.build_status_controls(card, self.tg_vars, "telegram_user")
                self.label(
                    credentials,
                    "Телефон — полностью, с кодом страны. Код входа придёт способом, выбранным Telegram.",
                    muted=True,
                    wraplength=680,
                ).pack(anchor="w", pady=(4, 0))
                row = tk.Frame(credentials, bg=self.theme["card"])
                row.pack(fill="x", pady=8)
                self.code_var = tk.StringVar()
                code_entry = ttk.Entry(row, textvariable=self.code_var, show="•")
                code_entry.pack(side="left", fill="x", expand=True)
                from entry_clipboard import attach

                attach(code_entry, self.theme)
                self.auth_buttons = [
                    ttk.Button(row, text="Получить код", command=self.handle_tg_send_code),
                    ttk.Button(row, text="Войти", command=self.handle_tg_login),
                ]
                for button in self.auth_buttons:
                    button.pack(side="left", padx=5)
                self.auth_label = self.label(
                    credentials, "Авторизация Telegram", muted=True, wraplength=680
                )
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

    def _auto_setup(self):
        if self.auto_setup_busy:
            return
        from local_setup import discover_settings, readiness

        self.auto_setup_busy = True
        self.auto_setup_info.set("Ищу локальные модели и программы…")
        before = self.config_mgr.snapshot()

        def done(result):
            detected, changes = result
            current = self.config_mgr.snapshot()
            applied = []
            for section, variables in (
                ("app_settings", self.settings_vars),
                ("steam", self.steam_vars),
            ):
                for key, variable in variables.items():
                    if (
                        detected[section][key] != before[section][key]
                        and current[section][key] == before[section][key]
                        and str(variable.get()) == str(before[section][key])
                    ):
                        current[section][key] = detected[section][key]
                        applied.append((variable, detected[section][key]))
            try:
                if applied:
                    self.config_mgr.save_config(current)
                    dirty = self.dirty
                    for variable, value in applied:
                        variable.set(value)
                    self.dirty = dirty
                    self.save_button.configure(text="Сохранить •" if dirty else "Сохранить")
                self.auto_setup_info.set(readiness(current))
                self.status_var.set(
                    "Автонастройка завершена. "
                    + (
                        "Найдено: " + ", ".join(changes)
                        if applied
                        else "Ручные настройки сохранены."
                    )
                )
            except (OSError, ValueError) as exc:
                self._report_error(str(exc))
            finally:
                self.auto_setup_busy = False

        def work():
            try:
                return discover_settings(before)
            except Exception as exc:
                self.post_ui(lambda: setattr(self, "auto_setup_busy", False))
                self.post_ui(
                    lambda error=str(exc): self.auto_setup_info.set(
                        "Автонастройка не завершена: " + error
                    )
                )
                raise

        self._background(work, done=done)

    def build_settings_tab(self):
        card = self.card(
            self.page_bodies["settings"],
            "Крест настроится сама",
            "Микрофон и плеер выбираются автоматически. Модели и Steam ищутся на этом компьютере; ваши ручные настройки сохраняются.",
        )
        self.label(card, textvariable=self.auto_setup_info, muted=True, wraplength=680).pack(
            anchor="w", pady=(0, 12)
        )
        ttk.Button(card, text="Подобрать автоматически", command=self._auto_setup).pack(anchor="w")
        card = self.card(self.page_bodies["settings"], "Профиль и оформление")
        self.entry_owner_name = self.field(card, "Ваше имя", self.owner_var)
        self.label(card, "Стиль общения", muted=True).pack(anchor="w")
        ttk.Combobox(
            card,
            textvariable=self.personality_var,
            values=list(PERSONALITIES.values()),
            state="readonly",
        ).pack(fill="x", pady=8)
        from personality import CHARACTERS

        character = tk.StringVar()

        def describe(*_):
            key = next(
                (k for k, v in PERSONALITIES.items() if v == self.personality_var.get()), "friendly"
            )
            from companion_behavior import description

            character.set(CHARACTERS[key] + "\n\n" + description({"profile": {"personality": key}}))

        old_trace = getattr(self, "_character_trace", None)
        if old_trace:
            self.personality_var.trace_remove("write", old_trace)
        self._character_trace = self.personality_var.trace_add("write", describe)
        describe()
        self.label(card, textvariable=character, muted=True, wraplength=640).pack(
            anchor="w", pady=8
        )
        self.label(
            card,
            "Попробуйте голосом: «я вернулся», «у меня получилось», «помоги сосредоточиться», «пожелай удачи в игре».",
            muted=True,
            wraplength=640,
        ).pack(anchor="w", pady=(0, 10))
        ttk.Button(card, text="Пример ответа", command=self._preview_personality).pack(anchor="w")
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
            "Проверка в общей очереди дежурства каждые три минуты AFK. Файлы должны оставаться неизменными минимум две минуты; папки и незавершённые загрузки пропускаются. При возвращении, игре, просмотре или звонке — пауза.",
        )
        folder_details = self.advanced(card, "Изменить папку загрузок")
        self.field(folder_details, "Папка загрузок", self.settings_vars["downloads_path"])
        ttk.Button(folder_details, text="Выбрать папку", command=self._pick_downloads).pack(
            anchor="w"
        )
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
            ("voice_volume", "Громкость, 0–100"),
            ("weather_city", "Город для погоды"),
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
        if os.name == "nt":
            ttk.Button(
                card, text="Видимость значка в трее Windows", command=self.open_tray_settings
            ).pack(anchor="w", pady=6)
        card = self.advanced(card, "Микрофон, модели и время ожидания")
        self.field(card, "Микрофон (пусто = авто)", self.settings_vars["microphone"])
        self.field(card, "Ожидание команды, сек.", self.settings_vars["active_timeout"])
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

    def _draft(self):
        data = self.config_mgr.snapshot()
        data["scenarios"] = copy.deepcopy(self.scenario_drafts)
        data["performance"] = {
            "mode": MODES[self.profile_var.get()],
            "game_processes": self.game_processes_var.get().strip(),
        }
        data["notification_settings"] = {
            "mode": NOTICE_MODES[self.notice_mode_var.get()],
            "game_important_only": self.game_notices_var.get(),
            "announce_senders": self.sender_notices_var.get(),
        }
        data["owner_name"] = self.owner_var.get().strip()
        data["profile"]["onboarding_completed"] = data["profile"]["onboarding_completed"] or bool(
            data["owner_name"]
        )
        data["profile"]["personality"] = next(
            key for key, value in PERSONALITIES.items() if value == self.personality_var.get()
        )
        data["media"]["preferred_app"] = self.media_source_var.get()

        data["warframe"] = self.warframe_settings_draft()
        data["selected_theme"] = self.theme_var.get()
        data["command_aliases"] = dict(self.aliases)
        data["pronunciations"] = dict(self.pronunciations)
        data["steam"].update({key: var.get() for key, var in self.steam_vars.items()})
        data["discord_presence"].update({key: var.get() for key, var in self.presence_vars.items()})
        data["services"] = copy.deepcopy(self.service_configs)
        for key, var in self.service_autostart.items():
            data["services"].setdefault(key, {})["autostart"] = var.get()
        for key, var in self.service_restart.items():
            data["services"].setdefault(key, {})["auto_restart"] = var.get()
        data["app_settings"].update({key: var.get() for key, var in self.settings_vars.items()})
        data["api_keys"].update({key: var.get().strip() for key, var in self.api_vars.items()})
        data["skills"].update({key: var.get() for key, var in self.skill_vars.items()})
        data["auto_replies"].update({key: var.get() for key, var in self.reply_vars.items()})
        data["discord_account"].update({key: var.get() for key, var in self.account_vars.items()})
        data["discord_bot"].update({key: var.get() for key, var in self.bot_vars.items()})
        data["telegram_user"].update({key: var.get() for key, var in self.tg_vars.items()})
        if self.CORE_PAGES & self._built_pages:
            data.update(self.core_settings_draft())
        if "autonomy" in self._built_pages:
            data["autonomy"].update(self.duty_settings_draft())
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
        self.save_button.configure(text="Сохранить")
        self.status_var.set("Настройки сохранены. Для ботов и моделей нужен перезапуск.")
        self.home_greeting.configure(text="Привет, " + self.owner_var.get() + ".")
        self.refresh_skills()
        if "steam" in self._built_pages:
            self._update_steam(force=True)
        indicator_controller.enabled = self.config_mgr.config["app_settings"]["indicator_enabled"]
        return True

    def apply_theme(self, name):
        if name in PREMIUM_THEMES and not self.config_mgr.is_premium():
            self.theme_var.set(next(key for key, value in THEMES.items() if value is self.theme))
            self.status_var.set("Эта тема доступна в Premium. Активируйте код на странице Premium.")
            self.switch_page("license")
            return
        if name not in THEMES:
            return
        positions = {key: canvas.yview()[0] for key, canvas in self.canvases.items()}
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
                "workflow_source",
                "workflow_destination",
                "workflow_pattern",
                "workflow_unpack",
                "workflow_duplicates",
                "sound_profile",
            )
            if hasattr(self, key)
        }
        built_pages = set(self._built_pages)
        core_pending = self.core_draft_values() if self.CORE_PAGES & built_pages else None
        self.wf_preserve()
        self.shell.destroy()
        self._timer_rows = None
        self._reminder_rows = None
        self.build_ui()
        for code in built_pages:
            self._ensure_page(code)
        if core_pending:
            self.restore_core_draft(core_pending)
        self.command_entry.insert(0, pending_command)
        for key, value in pending_fields.items():
            target = getattr(self, key)
            if isinstance(target, tk.Variable):
                target.set(value)
            else:
                target.delete(0, "end")
                target.insert(0, value)
        self._update_reminders()

        def restore_positions():
            for key, position in positions.items():
                if key in self.canvases and self.canvases[key].winfo_exists():
                    self.canvases[key].yview_moveto(position)

        self.after_idle(restore_positions)

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
        if action in ("start", "restart") and not self.config_mgr.is_premium():
            self._report_error("Подключение ботов доступно в Premium")
            self.switch_page("license")
            return
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
        if not self.config_mgr.is_premium():
            self._report_error("Подключение Telegram доступно в Premium")
            self.switch_page("license")
            return
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
            try:
                return result.get(timeout=120)
            except queue.Empty:
                return None

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
                    message = bridge.send_code(
                        values["telegram_api_id"],
                        values["telegram_api_hash"],
                        values["telegram_phone"],
                    )
                    self.post_ui(lambda message=message: self.auth_label.configure(text=message))
            except Exception as exc:
                from integrations import redact

                error = redact(str(exc), self.config_mgr.snapshot())
                if code:
                    error = error.replace(code, "[код скрыт]")
                self.post_ui(lambda error=error: self.auth_label.configure(text=error))
                self.post_ui(lambda error=error: self._report_error(error))
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
        self._history_dirty = self._log_dirty = True

    def _flush_history(self):
        # A burst of bot/voice events needs one repaint, not one full Text
        # replacement per message. Collapsed pages and the tray keep only data.
        if self.state() not in ("normal", "zoomed"):
            return
        if getattr(self, "_history_dirty", False) and self.event_text.winfo_ismapped():
            self._render_history()
        if getattr(self, "_log_dirty", False) and self.current_page == "logs":
            self.refresh_log()

    def _render_history(self):
        self._history_dirty = False
        self.event_text.configure(state="normal")
        self.event_text.delete("1.0", "end")
        self.event_text.insert("end", "\n\n".join(self.history[-15:]))
        self.event_text.see("end")
        self.event_text.configure(state="disabled")

    def refresh_log(self):
        self._log_dirty = False
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
        frame_deadline = time.monotonic() + 0.008
        if (
            self.current_page.startswith("wf_")
            and self.state() in ("normal", "zoomed")
            and time.monotonic() - self.wf_refreshed > 4
        ):
            self.wf_refresh()
        if (
            self.current_page == "music"
            and self.state() in ("normal", "zoomed")
            and time.monotonic() - self.music_refreshed_at >= 8
        ):
            self._refresh_music(quiet=True)
        for _ in range(100):
            if time.monotonic() >= frame_deadline:
                break
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
            if time.monotonic() >= frame_deadline:
                break
            try:
                kind, value = self.runtime.events.get_nowait()
            except queue.Empty:
                break
            if kind == "ui_open":
                self.restore_window()
            elif kind == "skills_changed":
                self.refresh_skills()
                if "skills" in self._built_pages:
                    self._refresh_skill_imports()
            elif kind == "catalog_changed":
                self._refresh_apps()
            elif kind == "quick_panel":
                from quick_panel import show_panel

                show_panel(self)
            elif kind == "result":
                self.command_result.configure(
                    text=value.text[:2400],
                    fg=self.theme["text" if value.success else "danger"],
                )
                self._append_log(value.text)
                if value.skill_id.startswith("skills.media."):
                    self.media_status_var.set(value.text)
            elif kind == "speech":
                self.brand_mark.pulse()
                self._append_log(value)

            elif kind == "recognition":
                self._show_recognition(value)
            elif kind == "scenario_saved":
                self._append_log("Сценарий сохранён: " + value)
                saved = self.config_mgr.snapshot()["scenarios"]
                if value in saved:
                    self.scenario_drafts[value] = copy.deepcopy(saved[value])
                if hasattr(self, "scenario_combo"):
                    self.scenario_combo.configure(values=list(self.scenario_drafts))
                if hasattr(self, "event_scenario_combo"):
                    self.event_scenario_combo.configure(values=list(self.scenario_drafts))
            elif kind == "voice":
                self.voice_var.set(value)
            elif kind == "error":
                self._report_error(value)
            elif kind == "automation":
                self._append_log(value)
            elif kind == "notification":
                self.status_var.set(str(value)[:160])
                if self.tray_icon:
                    try:
                        self.tray_icon.notify(str(value)[:500], "Крест")
                    except Exception:
                        logger.warning("Уведомление сохранено в журнале; трей недоступен")
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
        indicator_controller.show_feedback(
            self.runtime.voice_feedback,
            enabled=self.config_mgr.config["app_settings"]["indicator_enabled"],
        )
        visible = self.state() in ("normal", "zoomed")
        self._flush_history()
        if visible and self.current_page in ("actions", "voice"):
            if time.monotonic() - getattr(self, "_meter_at", 0) >= 0.15:
                self._meter_at = time.monotonic()
                self._update_voice_meter()
        now = time.monotonic()
        if not hasattr(self, "_last_refresh") or now - self._last_refresh >= 1:
            self._last_refresh = now
            if visible and self.current_page in (
                "actions",
                "voice",
                "scenarios",
                "bots",
            ):
                self._refresh_experience()
            statuses = self.integration.refresh()
            for key, (filename, title) in self.integration.MODULES.items():
                if filename not in self.service_labels:
                    continue
                if not self.service_labels[filename].winfo_ismapped():
                    continue
                if filename not in self.busy_services:
                    detail = self.integration.details(filename)
                    self.service_labels[filename].configure(
                        text="● " + statuses[key],
                        fg=self.theme["accent"]
                        if detail["health"] == "ready"
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
                            or "Заполните настройки сервиса, сохраните и нажмите «Запустить»."
                        )
                    )
                    if detail["running"]:
                        bridge = detail["bridge"]
                        message = bridge.get(
                            "text", "События моста ещё не получены; доступен журнал процесса."
                        )
                        if detail["error"]:
                            message = detail["error"]
                        self.service_details[filename].configure(
                            text=f"PID {detail['pid']} · Работает {seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}\n"
                            + message
                        )
                else:
                    detail = self.integration.details(filename)
                for action, button in self.service_buttons[filename].items():
                    enabled = filename not in self.busy_services and (
                        not detail["running"] if action == "start" else detail["running"]
                    )
                    button.configure(state="normal" if enabled else "disabled")
            if visible and self.current_page == "timers":
                self._update_timers()
            if visible and self.current_page in ("actions", "timers"):
                self._update_reminders(update_rows=self.current_page == "timers")
            if visible and self.current_page == "kernel":
                self._refresh_component_report()
            self._duty_refresh()
            self._smart_refresh()
            self._brain_refresh()
            if visible and self.current_page in self.CORE_PAGES:
                self._update_workflows()
            license_state = licensing.status(self.config_mgr.snapshot())
            premium = license_state["premium"]
            if visible:
                self._update_license_view(license_state)
            if (
                visible
                and self.current_page == "discord_bot"
                and "discord_bot" in self._built_pages
            ):
                self._update_discord_bot()
            if self.current_page in self.BOT_PAGES and not premium:
                if self.pages[self.current_page].winfo_manager():
                    self.switch_page(self.current_page)
            if not premium and self.integration.processes and not self._license_check_busy:
                self._license_check_busy = True

                def enforce():
                    try:
                        self.integration.enforce_license()
                    finally:
                        self.post_ui(lambda: setattr(self, "_license_check_busy", False))

                self._background(enforce)
            if not premium and self.theme_var.get() in PREMIUM_THEMES:
                self.apply_theme("Graphite")
            if visible and self.current_page == "settings":
                self.download_label.configure(text=self.runtime.download_status)
            if visible and self.current_page == "steam" and "steam" in self._built_pages:
                self._update_steam()
            if visible and self.current_page == "actions":
                current, pending = self.runtime.command_status()
                queue_text = (
                    "Выполняется: " + current[:100] if current else "Ожидаю обращение «Крест»"
                ) + f" · В очереди: {pending}"
                if self.queue_var.get() != queue_text:
                    self.queue_var.set(queue_text)
                overview = (
                    f"{len(self.runtime.manager.catalog())} навыков готовы  ·  "
                    f"{len(self.runtime.state.snapshot()['active_timers'])} активных таймеров  ·  "
                    f"{len(self.integration.processes)} подключённых сервисов"
                )
                if self.overview_label.cget("text") != overview:
                    self.overview_label.configure(text=overview)
                from relic_dashboard import refresh

                refresh(self)
        if self.winfo_exists():
            self._poll_job = self.after(
                40 if self.state() in ("normal", "zoomed") else 600, self._poll
            )

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
        if self._tray_timeout:
            self.after_cancel(self._tray_timeout)
            self._tray_timeout = None
        self.microphone_test.stop.set()
        self.status_var.set("Завершение работы…")

        def close():
            try:
                if not hasattr(self.runtime, "client"):
                    self.integration.stop_all()
                self.runtime.close()
            except (OSError, ValueError, RuntimeError):
                logger.warning("Ядро уже завершилось или связь недоступна")
            finally:
                if self.tray_icon:
                    self.tray_icon.stop()
                self.post_ui(self._destroy_window)

        self._background(close)

    def detach_window(self):
        if not hasattr(self.runtime, "detach"):
            self.on_close_window()
            return
        if self.dirty and not self.save_all_settings():
            return
        self.closing = True
        self.microphone_test.stop.set()
        if self.tray_icon:
            self.tray_icon.stop()
        self.runtime.detach()
        self._destroy_window()

    def destroy(self):
        # Cancel Tcl timers before their Python callbacks/widgets are destroyed.
        self._destroyed = True
        try:
            for job in self.tk.call("after", "info"):
                # Preserve callback ownership: child.destroy() deletes its own Tcl command.
                self.tk.call("after", "cancel", job)
        except tk.TclError:
            pass
        super().destroy()

    def _destroy_window(self):
        self.after_cancel(self._poll_job)
        indicator_controller.stop()
        logging.getLogger().removeHandler(self._log_handler)
        self._destroyed = True
        self.destroy()


def open_menu_gui(remote=None, config_path=None, quick=False):
    app = ControlCenterApp(remote=remote, config_path=config_path)
    if quick and app.onboarding is None:
        from quick_panel import show_panel

        app.after(100, lambda: show_panel(app))
    app.mainloop()


if __name__ == "__main__":
    from main import main

    raise SystemExit(main())
