"""Service pages kept separate from the main Tk control centre."""

import ast
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog
import uuid
import webbrowser

from steam_service import read_cache
from discord_bot_pages import DiscordBotPages


class ServicePages(DiscordBotPages):
    def _startup_checks(self):
        import psutil

        errors = self.runtime.manager.errors
        disk = psutil.disk_usage(str(self.config_mgr.filepath.parent))
        text = f"Проверка ядра: навыков {len(self.runtime.manager.catalog())}, ошибок {len(errors)}; свободно {disk.free / 1024**3:.1f} ГБ; очередь уведомлений открыта."
        self.post_ui(lambda: self._append_log(text))
        if disk.free < 1024**3:
            self.runtime.notifications.put("Мало свободного места на диске Крест: менее 1 ГБ")

    def service_controls(self, parent, filename):
        label = self.label(parent, "● Не запущен")
        label.pack(anchor="w", pady=4)
        self.service_labels[filename] = label
        details = self.label(parent, "", muted=True, wraplength=680)
        details.pack(anchor="w")
        self.service_details[filename] = details
        row = tk.Frame(parent, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        self.service_buttons[filename] = {}
        for action, caption in (
            ("start", "Запустить"),
            ("stop", "Остановить"),
            ("restart", "Перезапустить"),
        ):
            button = ttk.Button(
                row,
                text=caption,
                command=lambda f=filename, a=action: self._integration_action(f, a),
                style="Accent.TButton" if action == "start" else "TButton",
            )
            button.pack(side="left", padx=(0, 5))
            self.service_buttons[filename][action] = button
        ttk.Button(row, text="Журнал →", command=lambda: self._open_service_log(filename)).pack(
            side="right"
        )
        if filename.startswith("custom-"):
            ttk.Button(
                parent,
                text="Запросить обновление через мост",
                command=lambda: self._background(
                    lambda: self.integration.send(filename, "refresh")
                ),
            ).pack(anchor="w", pady=4)
        self.service_options(parent, filename)

    def service_options(self, parent, filename):
        if filename not in self.service_autostart:
            self.service_autostart[filename] = tk.BooleanVar(
                value=self.service_configs.get(filename, {}).get("autostart", False)
            )
            self.service_autostart[filename].trace_add("write", self._mark_dirty)
        ttk.Checkbutton(
            parent, text="Запускать вместе с Крест", variable=self.service_autostart[filename]
        ).pack(anchor="w", pady=4)
        if filename not in self.service_restart:
            self.service_restart[filename] = tk.BooleanVar(
                value=self.service_configs.get(filename, {}).get("auto_restart", False)
            )
            self.service_restart[filename].trace_add("write", self._mark_dirty)
        ttk.Checkbutton(
            parent,
            text="Восстанавливать после сбоя (до 3 попыток за 10 минут)",
            variable=self.service_restart[filename],
        ).pack(anchor="w", pady=4)
        options = (
            parent
            if filename.startswith("custom-")
            else self.advanced(parent, "Заменить встроенный скрипт")
        )
        row = tk.Frame(options, bg=self.theme["card"])
        row.pack(fill="x")
        ttk.Button(
            row, text="Найти скрипт бота…", command=lambda: self._choose_service_script(filename)
        ).pack(side="left")
        self.label(
            row,
            self.service_configs.get(filename, {}).get("path") or "Встроенный скрипт",
            muted=True,
            wraplength=460,
        ).pack(side="left", padx=8)

    def _choose_service_script(self, filename):
        path = filedialog.askopenfilename(
            parent=self, title="Выберите скрипт бота", filetypes=[("Python", "*.py")]
        )
        if not path:
            return
        try:
            ast.parse(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, SyntaxError, UnicodeError) as exc:
            self._report_error("Скрипт не прочитан: " + str(exc))
            return
        self.service_configs.setdefault(filename, {})["path"] = path
        self._mark_dirty()
        self.status_var.set("Скрипт выбран. Сохраните настройки и перезапустите сервис.")

    def build_steam_tab(self):
        card = self.card(
            self.page_bodies["steam"],
            "Steam · библиотека и CS2",
            "Автоматическое обновление AppID, инвентаря и предложений обмена. Крест не принимает и не отправляет обмены.",
        )
        self.api_entries["steam_api_key"] = self.field(
            card, "Steam Web API Key", self.api_vars["steam_api_key"], True
        )
        self.api_entries["steam_id"] = self.field(
            card, "SteamID64", self.api_vars["steam_id"], paste_button=True
        )
        advanced = self.advanced(card, "Папка Steam и частота обновления")
        self.field(advanced, "Папка Steam", self.steam_vars["install_path"])
        ttk.Button(advanced, text="Найти папку Steam…", command=self._choose_steam_folder).pack(
            anchor="w"
        )
        self.field(advanced, "Проверять каждые, мин.", self.steam_vars["poll_minutes"])
        ttk.Checkbutton(
            card,
            text="Доставлять события Steam мне в Telegram",
            variable=self.steam_vars["telegram_notifications"],
        ).pack(anchor="w", pady=6)
        self.label(
            card,
            "Для доставки используются Bot Token и ID владельца со страницы Telegram.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w")
        self.service_controls(card, "steam_bot.py")
        ttk.Button(card, text="Обновить сейчас", command=self._refresh_steam).pack(
            anchor="w", pady=6
        )
        self.steam_status = self.label(
            card,
            "Монитор ещё не запущен. Локальная библиотека доступна без ключа.",
            muted=True,
            wraplength=680,
        )
        self.steam_status.pack(anchor="w")
        card = self.card(
            self.page_bodies["steam"],
            "Мои игры",
            "AppID подставляется автоматически. Для неустановленной игры Steam предложит установку.",
        )
        self.steam_search = tk.StringVar()
        ttk.Entry(card, textvariable=self.steam_search).pack(fill="x")
        self.steam_search.trace_add("write", lambda *args: self._update_steam(force=True))
        self.steam_games = ttk.Treeview(
            card,
            columns=("name", "id", "installed"),
            show="headings",
            height=7,
            selectmode="browse",
        )
        for key, title, width in (
            ("name", "Игра", 420),
            ("id", "AppID", 95),
            ("installed", "На компьютере", 130),
        ):
            self.steam_games.heading(key, text=title)
            self.steam_games.column(key, width=width)
        self.steam_games.pack(fill="x", pady=8)
        ttk.Button(
            card, text="Открыть выбранную игру в Steam", command=self._launch_steam_selection
        ).pack(anchor="w")
        card = self.card(
            self.page_bodies["steam"],
            "Инвентарь CS2",
            "Недоступность обмена не всегда означает временный бан. Срок показывается только если Steam сообщил его явно.",
        )
        self.steam_inventory_status = self.label(card, "Нет данных", wraplength=680)
        self.steam_inventory_status.pack(anchor="w", pady=5)
        self.steam_items = ttk.Treeview(
            card, columns=("name", "amount", "state", "until"), show="headings", height=8
        )
        for key, title, width in (
            ("name", "Предмет", 300),
            ("amount", "Кол-во", 60),
            ("state", "Обмен", 135),
            ("until", "Ожидаемая дата", 160),
        ):
            self.steam_items.heading(key, text=title)
            self.steam_items.column(key, width=width)
        self.steam_items.pack(fill="x")
        card = self.card(self.page_bodies["steam"], "Обмены и уведомления")
        self.steam_account = self.label(card, "Нет данных", wraplength=680)
        self.steam_account.pack(anchor="w")
        ttk.Button(
            card,
            text="Открыть предложения обмена",
            command=lambda: webbrowser.open("https://steamcommunity.com/my/tradeoffers/"),
        ).pack(anchor="w", pady=8)
        self.steam_notices = self.label(card, "", wraplength=680)
        self.steam_notices.pack(anchor="w")
        self._steam_stamp = None
        self._update_steam(force=True)

    def _choose_steam_folder(self):
        value = filedialog.askdirectory(parent=self, title="Папка Steam (внутри steamapps)")
        if value:
            self.steam_vars["install_path"].set(value)

    def _refresh_steam(self):
        if not self.save_all_settings():
            return
        if not self.integration.details("steam_bot.py")["running"]:
            self._integration_action("steam_bot.py", "start")
        else:
            self._background(lambda: self.integration.send("steam_bot.py", "refresh"))
        self.status_var.set("Обновление Steam запрошено; повторные запросы ограничены по частоте.")

    def _launch_steam_selection(self):
        selected = self.steam_games.selection()
        if selected and selected[0].isdigit():
            webbrowser.open("steam://rungameid/" + selected[0])

    def _update_steam(self, force=False):
        path = self.config_mgr.filepath.parent / "steam_state.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return
        if stamp == self._steam_stamp and not force:
            return
        self._steam_stamp = stamp
        data = read_cache(path)
        if data.get("account", "") != str(
            self.config_mgr.snapshot()["api_keys"].get("steam_id", "")
        ):
            data = {}
        query = self.steam_search.get().casefold()
        selected = self.steam_games.selection()
        self.steam_games.delete(*self.steam_games.get_children())
        for game in data.get("games", []):
            if query in game["name"].casefold() or query in game["appid"]:
                self.steam_games.insert(
                    "",
                    "end",
                    iid=game["appid"],
                    values=(
                        game["name"],
                        game["appid"],
                        "Установлена" if game.get("installed") else "Нет",
                    ),
                )
        if selected and self.steam_games.exists(selected[0]):
            self.steam_games.selection_set(selected[0])
        inv = data.get("inventories", {}).get("730:2", {})
        counts = inv.get("summary", {})
        self.steam_inventory_status.configure(
            text=f"Всего: {counts.get('total', '—')} · Можно обменять: {counts.get('tradable', '—')} · Недоступны: {counts.get('locked', '—')} · Неизвестно: {counts.get('unknown', '—')}\nОбновлено: {self._date(inv.get('updated'))}"
        )
        self.steam_items.delete(*self.steam_items.get_children())
        for item in inv.get("items", []):
            self.steam_items.insert(
                "",
                "end",
                values=(
                    item["name"],
                    item["amount"],
                    "Доступен"
                    if item["tradable"] is True
                    else "Недоступен"
                    if item["tradable"] is False
                    else "Неизвестно",
                    self._date(item.get("unlock_at")),
                ),
            )
        errors = "\n".join(f"{k}: {v}" for k, v in data.get("errors", {}).items())
        self.steam_status.configure(
            text=f"Игр: {len(data.get('games', []))} · Библиотека аккаунта: {self._date(data.get('owned_updated'))}\n"
            + (errors or "Ошибок последней проверки нет")
        )
        bans = data.get("bans", {})
        offers = data.get("offers", [])
        economy = {
            "none": "нет",
            "banned": "есть запрет",
            "probation": "ограничение",
            "unknown": "неизвестно",
        }.get(bans.get("economy_ban"), "неизвестно")
        self.steam_account.configure(
            text=f"Ограничение экономики аккаунта: {economy}. API не сообщает дату его снятия.\nПредложения: входящих {sum(o['direction'] == 'in' for o in offers)}, исходящих {sum(o['direction'] == 'out' for o in offers)}. Проверено: {self._date(data.get('offers_updated'))}"
        )
        self.steam_notices.configure(
            text="\n\n".join(
                self._date(n["time"]) + " · " + n["text"] for n in data.get("notices", [])[-8:]
            )
            or "Новых событий пока нет."
        )

    @staticmethod
    def _date(value):
        if not value:
            return "неизвестно"
        try:
            return datetime.fromtimestamp(value).strftime("%d.%m.%Y %H:%M")
        except (ValueError, OverflowError, TypeError, OSError):
            return "неизвестно"

    def build_reply_controls(self, page):
        from auto_replies import STYLES, render_reply

        card = self.card(
            page,
            "Голос Крест в переписке",
            "Общий стиль Telegram User, Discord Bot и личной Discord-сессии. Изменения применяются после сохранения.",
        )
        ttk.Checkbutton(
            card, text="Отвечать на входящие личные сообщения", variable=self.reply_vars["enabled"]
        ).pack(anchor="w", pady=4)
        choice = tk.StringVar(self, value=STYLES[self.reply_vars["style"].get()])
        combo = ttk.Combobox(
            card, textvariable=choice, values=list(STYLES.values()), state="readonly"
        )
        combo.pack(fill="x", pady=5)
        combo.bind(
            "<<ComboboxSelected>>",
            lambda _: self.reply_vars["style"].set(
                next(key for key, label in STYLES.items() if label == choice.get())
            ),
        )

        def sync_style(*_):
            choice.set(STYLES[self.reply_vars["style"].get()])

        trace = self.reply_vars["style"].trace_add("write", sync_style)
        combo.bind("<Destroy>", lambda _: self.reply_vars["style"].trace_remove("write", trace))
        preview = self.label(card, "", wraplength=650)
        preview.pack(anchor="w", pady=12)

        def show(*_):
            if preview.winfo_exists():
                preview.configure(
                    text=render_reply(
                        self.owner_var.get(),
                        {"is_playing": True},
                        {
                            "style": self.reply_vars["style"].get(),
                            "share_activity": self.reply_vars["share_activity"].get(),
                        },
                    )
                )

        show()
        ttk.Button(card, text="Предпросмотр ответа", command=show).pack(anchor="w")
        advanced = self.advanced(card, "Частота, приватность и удаление")
        self.field(
            advanced, "Пауза между ответами контакту, сек.", self.reply_vars["cooldown_seconds"]
        )
        self.field(
            advanced, "Удалять через, сек. (0 — сохранять)", self.reply_vars["delete_seconds"]
        )
        ttk.Checkbutton(
            advanced,
            text="Сообщать собеседнику, что я играю или работаю",
            variable=self.reply_vars["share_activity"],
        ).pack(anchor="w", pady=4)

    def build_discord_account_tab(self):
        account = self.card(
            self.page_bodies["discord_account"],
            "Личный Discord-аккаунт",
            "Вход в ваш аккаунт, автоответы в личных сообщениях и ваш текстовый статус. Настройки серверного бота находятся на соседней вкладке.",
        )
        self.field(account, "Токен личной сессии", self.api_vars["discord_user_token"], True)
        for key, label in (
            ("enabled", "Разрешить личную сессию"),
            ("auto_reply", "Автоответы в личных сообщениях"),
            ("update_status", "Обновлять мой текстовый статус"),
            ("follow_activity", "Показывать, когда я играю"),
        ):
            ttk.Checkbutton(account, text=label, variable=self.account_vars[key]).pack(
                anchor="w", pady=4
            )
        self.field(account, "Текст статуса", self.account_vars["status_text"])
        self.service_controls(account, "discord_account.py")
        self.build_reply_controls(self.page_bodies["discord_account"])
        card = self.card(
            self.page_bodies["discord_account"],
            "Discord · мой статус",
            "Rich Presence обновляет активность вашего личного аккаунта через открытый настольный Discord. Пользовательский токен не нужен. Это не изменение произвольного custom status и не автоответчик личных сообщений аккаунта.",
        )
        for key, caption in (
            ("application_id", "Application ID"),
            ("details", "Заголовок активности"),
            ("state", "Текст активности"),
        ):
            self.field(card, caption, self.presence_vars[key])
        ttk.Checkbutton(
            card,
            text="Обновлять по состоянию Крест / Dota",
            variable=self.presence_vars["follow_activity"],
        ).pack(anchor="w", pady=8)
        self.service_controls(card, "discord_presence.py")
        ttk.Button(
            card,
            text="Открыть Discord Developer Portal",
            command=lambda: webbrowser.open("https://discord.com/developers/applications"),
        ).pack(anchor="w", pady=8)

    def build_scripts_tab(self):
        card = self.card(
            self.page_bodies["scripts"],
            "Подключить свой скрипт",
            "Выберите доверенный Python-скрипт. Крест управляет процессом и журналом; события и команды подключаются через bridge_sdk. Подробности и пример — в README.",
        )
        self.script_title, self.script_path, self.script_token = (tk.StringVar() for _ in range(3))
        self.field(card, "Название", self.script_title)
        self.field(card, "Файл .py", self.script_path)
        self.field(card, "Ключ бота", self.script_token, True)
        ttk.Button(card, text="Найти скрипт бота…", command=self._pick_script).pack(
            anchor="w", pady=4
        )
        ttk.Button(
            card, text="Добавить и запустить", command=self._register_script, style="Accent.TButton"
        ).pack(anchor="w", pady=8)
        ttk.Button(
            card, text="Проверить ядро", command=lambda: self._background(self._startup_checks)
        ).pack(anchor="w", pady=4)
        ttk.Button(
            card,
            text="Проверить уведомления",
            command=lambda: self.runtime.notifications.put(
                "Тестовое уведомление: мост Крест работает"
            ),
        ).pack(anchor="w", pady=4)
        builtins = {f for f, _ in type(self.integration).MODULES.values()}
        for filename, title in self.integration.MODULES.values():
            if filename not in builtins:
                card = self.card(self.page_bodies["scripts"], title)
                self.service_controls(card, filename)
                ttk.Button(
                    card,
                    text="Отключить и убрать из Крест",
                    command=lambda f=filename: self._remove_script(f),
                ).pack(anchor="w", pady=6)

    def _remove_script(self, filename):
        def finish(result):
            record = self.service_configs.pop(filename, None)
            variable = self.service_autostart.pop(filename, None)
            restart_variable = self.service_restart.pop(filename, None)
            if not self.save_all_settings():
                if record is not None:
                    self.service_configs[filename] = record
                if variable is not None:
                    self.service_autostart[filename] = variable
                if restart_variable is not None:
                    self.service_restart[filename] = restart_variable
                return
            self.integration.MODULES.pop(filename, None)
            self.integration.reload_registry()
            self.apply_theme(self.theme_var.get())
            self.status_var.set("Скрипт отключён. Сам файл остался на диске.")

        self._background(lambda: self.integration.stop(filename), finish)

    def _pick_script(self):
        value = filedialog.askopenfilename(parent=self, filetypes=[("Python", "*.py")])
        if value:
            self.script_path.set(value)
            if not self.script_title.get():
                self.script_title.set(Path(value).stem)

    def _register_script(self):
        path = Path(self.script_path.get()).expanduser()
        try:
            if path.suffix.lower() != ".py" or not path.is_file():
                raise ValueError("Выберите существующий файл .py")
            ast.parse(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, SyntaxError, UnicodeError) as exc:
            self._report_error(str(exc))
            return
        filename = "custom-" + uuid.uuid4().hex + ".py"
        self.service_configs[filename] = {
            "path": str(path.resolve()),
            "title": self.script_title.get().strip() or path.stem,
            "token": self.script_token.get(),
            "autostart": False,
        }
        self._mark_dirty()
        if not self.save_all_settings():
            self.service_configs.pop(filename, None)
            return
        self.integration.reload_registry()
        self.apply_theme(self.theme_var.get())
        self.switch_page("scripts")
        self._integration_action(filename, "start")
