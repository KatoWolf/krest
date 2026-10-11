"""Separate control panel for a Discord server bot, with live bridge results."""

import time
import tkinter as tk
from tkinter import ttk
import uuid
import webbrowser

from entry_clipboard import attach


class DiscordBotPages:
    def build_discord_bot_tab(self):
        page = self.page_bodies["discord_bot"]
        card = self.card(
            page,
            "Серверный бот",
            "Подключите Bot Token из Discord Developer Portal. Здесь управляется отдельный бот на ваших серверах.",
        )
        self.api_entries["discord_token"] = self.field(
            card, "Bot Token", self.api_vars["discord_token"], True
        )
        self.service_controls(card, "ds_session.py")
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        ttk.Button(
            row,
            text="Developer Portal",
            command=lambda: webbrowser.open("https://discord.com/developers/applications"),
        ).pack(side="left")
        ttk.Button(row, text="Пригласить бота на сервер", command=self._invite_discord_bot).pack(
            side="left", padx=8
        )
        self.discord_bot_info = tk.StringVar(
            value="После подключения здесь появятся имя бота и серверы."
        )
        self.label(card, textvariable=self.discord_bot_info, wraplength=680).pack(
            anchor="w", pady=8
        )

        card = self.card(
            page,
            "Статус и автоответы",
            "Эти настройки относятся только к серверному боту. Сохранённые изменения применяются во время работы.",
        )
        self.field(card, "Текст активности", self.bot_vars["activity_text"])
        ttk.Combobox(
            card,
            textvariable=self.bot_vars["status"],
            values=["online", "idle", "dnd", "invisible"],
            state="readonly",
        ).pack(fill="x", pady=5)
        self.label(
            card,
            "online — в сети · idle — нет на месте · dnd — не беспокоить · invisible — невидимый",
            muted=True,
            wraplength=680,
        ).pack(anchor="w")
        ttk.Checkbutton(
            card, text="Не беспокоить, пока я играю", variable=self.bot_vars["follow_activity"]
        ).pack(anchor="w", pady=6)
        ttk.Checkbutton(
            card, text="Автоответы в личных сообщениях бота", variable=self.bot_vars["auto_reply"]
        ).pack(anchor="w")
        ttk.Button(card, text="Применить настройки", command=self.save_all_settings).pack(
            anchor="w", pady=8
        )

        card = self.card(
            page,
            "Серверы и каналы",
            "Выберите получателя. Сообщение отправляется от имени бота только после нажатия кнопки; массовые упоминания отключены.",
        )
        if not hasattr(self, "discord_guild_var"):
            self.discord_guild_var, self.discord_channel_var = tk.StringVar(), tk.StringVar()
        self.discord_guilds = ttk.Combobox(
            card, textvariable=self.discord_guild_var, state="readonly"
        )
        self.discord_guilds.pack(fill="x", pady=5)
        self.discord_guilds.bind("<<ComboboxSelected>>", self._select_discord_guild)
        self.discord_channels = ttk.Combobox(
            card, textvariable=self.discord_channel_var, state="readonly"
        )
        self.discord_channels.pack(fill="x", pady=5)
        ttk.Button(card, text="Обновить серверы и каналы", command=self._refresh_discord_bot).pack(
            anchor="w", pady=5
        )
        if not hasattr(self, "discord_message_var"):
            self.discord_message_var = tk.StringVar()
        entry = ttk.Entry(card, textvariable=self.discord_message_var)
        entry.pack(fill="x", pady=8)
        attach(entry, self.theme)
        self.discord_send_button = ttk.Button(
            card, text="Отправить в выбранный канал", command=self._send_discord_message
        )
        self.discord_send_button.pack(anchor="w")
        self.discord_action_var = tk.StringVar(
            value="Бот должен иметь права «Просматривать канал» и «Отправлять сообщения»."
        )
        self.label(card, textvariable=self.discord_action_var, wraplength=680, muted=True).pack(
            anchor="w", pady=8
        )
        self._discord_overview = self._discord_channels = None
        self._discord_guild_map, self._discord_channel_map = {}, {}
        if not hasattr(self, "_discord_pending"):
            self._discord_pending = None
        self._update_discord_bot()
        self.build_reply_controls(page)

    def _bot_request(self, command, **payload):
        self._background(lambda: self.integration.send("ds_session.py", command, **payload))

    def _refresh_discord_bot(self):
        self._bot_request("refresh")
        guild = self._discord_guild_map.get(self.discord_guild_var.get())
        if guild:
            self._bot_request("channels", guild_id=guild)

    def _select_discord_guild(self, event=None):
        self.discord_channel_var.set("")
        self.discord_channels.configure(values=[])
        self._discord_channel_map = {}
        self._discord_channels = None
        guild = self._discord_guild_map.get(self.discord_guild_var.get())
        if guild:
            self._bot_request("channels", guild_id=guild)

    def _invite_discord_bot(self):
        overview = (
            self.integration.details("ds_session.py")["bridge"].get("data", {}).get("overview", {})
        )
        ident = str(overview.get("id", ""))
        if ident.isdigit():
            webbrowser.open(
                "https://discord.com/oauth2/authorize?client_id="
                + ident
                + "&scope=bot&permissions=3072"
            )
        else:
            self._report_error("Сначала подключите бота, чтобы получить его идентификатор.")

    def _send_discord_message(self):
        if self._discord_pending:
            return
        guild = self._discord_guild_map.get(self.discord_guild_var.get())
        channel = self._discord_channel_map.get(self.discord_channel_var.get())
        text = self.discord_message_var.get().strip()
        if not guild or not channel or not channel["send"]:
            self._report_error("Выберите сервер и канал, в который бот может отправлять сообщения.")
            return
        if not 1 <= len(text) <= 2000:
            self._report_error("Введите сообщение длиной от 1 до 2000 символов.")
            return
        ident = uuid.uuid4().hex
        self._discord_pending = (ident, text, time.monotonic())
        self.discord_send_button.configure(state="disabled")
        self.discord_action_var.set("Отправка…")

        def send():
            try:
                self.integration.send(
                    "ds_session.py",
                    "send_message",
                    guild_id=guild,
                    channel_id=channel["id"],
                    text=text,
                    request_id=ident,
                )
            except Exception:
                self.post_ui(lambda: self._discord_send_failed(ident))
                raise

        self._background(send)

    def _discord_send_failed(self, ident):
        if self._discord_pending and self._discord_pending[0] == ident:
            self._discord_pending = None
            self.discord_action_var.set("Запрос не отправлен. Проверьте подключение бота.")

    def _update_discord_bot(self):
        detail = self.integration.details("ds_session.py")
        ready = detail["running"] and detail["bridge"].get("ready", False)
        data = detail["bridge"].get("data", {})
        overview = data.get("overview")
        if overview and overview != self._discord_overview:
            self._discord_overview = overview
            self.discord_bot_info.set(
                f"{overview['name']} · Серверов: {overview['guild_count']} · Задержка: {overview.get('latency_ms', '—')} мс"
            )
            self._discord_guild_map = {
                g["name"] + " · " + g["id"]: g["id"] for g in overview.get("guilds", [])
            }
            self.discord_guilds.configure(values=list(self._discord_guild_map))
            if self.discord_guild_var.get() not in self._discord_guild_map:
                self.discord_guild_var.set(next(iter(self._discord_guild_map), ""))
                if ready:
                    self._select_discord_guild()
        channels = data.get("channels")
        guild = self._discord_guild_map.get(self.discord_guild_var.get())
        if channels and channels.get("guild_id") == guild and channels != self._discord_channels:
            self._discord_channels = channels
            self._discord_channel_map = {
                "#" + c["name"] + (" · только чтение" if not c["send"] else "") + " · " + c["id"]: c
                for c in channels.get("items", [])
            }
            self.discord_channels.configure(values=list(self._discord_channel_map))
            if self.discord_channel_var.get() not in self._discord_channel_map:
                preferred = self.api_vars["discord_channel_id"].get()
                self.discord_channel_var.set(
                    next(
                        (
                            name
                            for name, c in self._discord_channel_map.items()
                            if c["id"] == preferred and c["send"]
                        ),
                        next(
                            (name for name, c in self._discord_channel_map.items() if c["send"]), ""
                        ),
                    )
                )
            self.discord_action_var.set(
                f"Каналов доступно: {channels['count']} · Показано: {len(self._discord_channel_map)}"
            )
        if self._discord_pending:
            ident, text, began = self._discord_pending
            action = data.get("action", {})
            if action.get("id") == ident:
                self.discord_action_var.set(action["text"])
                if action.get("ok") and self.discord_message_var.get().strip() == text:
                    self.discord_message_var.set("")
                self._discord_pending = None
            elif not ready or time.monotonic() - began > 40:
                self.discord_action_var.set(
                    "Подтверждение не получено. Проверьте канал перед повторной отправкой."
                )
                self._discord_pending = None
        self.discord_send_button.configure(
            state="normal" if ready and not self._discord_pending else "disabled"
        )
