"""On-demand organizer UI: userbot friend picker, party, requests and dictionary."""

from datetime import datetime
import time
import tkinter as tk
from tkinter import ttk

from organizer_bridge import SERVICES
from organizer_commands import COMMANDS
from organizer_language import LABELS, PHRASES
from organizer_messages import preview as personality_preview
from personality import PERSONALITIES

PARTY_STATES = {
    "collecting": "Собираю состав",
    "ready": "Все готовы",
    "paused": "На паузе",
    "cancelled": "Отменён",
    "expired": "Время истекло",
    "started": "Запуск отправлен",
}
DELIVERY = {
    "queued": "В очереди",
    "sending": "Отправляется",
    "delivered": "Доставлено",
    "failed": "Ошибка",
    "uncertain": "Не подтверждено",
    "expired": "Истекло ожидание",
    "cancelled": "Отменено",
}
REQUESTS = {
    "waiting": "Ждёт завершения игры",
    "delivered": "Передано вам",
    "reply_queued": "Ответ в очереди",
    "answered": "Ответ доставлен",
    "reply_failed": "Ответ не доставлен",
    "reply_uncertain": "Доставка неизвестна; проверьте чат",
    "closed": "Закрыто",
}


class OrganizerPages:
    def _org_combo(self, parent, variable, values):
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
        combo.pack(fill="x", pady=6)
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            combo.bind(event, lambda _: "break")
        return combo
        return combo

    def build_organizer_tab(self):
        parent = self.page_bodies["organizer"]
        hero = tk.Frame(parent, bg=self.theme["bg"])
        hero.pack(fill="x", pady=(0, 12))
        self.org_status = tk.StringVar(value="Подключаюсь к организатору…")
        self.label(hero, textvariable=self.org_status, wraplength=730).pack(fill="x")
        bar = tk.Frame(hero, bg=self.theme["bg"])
        bar.pack(fill="x", pady=(8, 0))
        self.org_tabs = {}
        for i, (key, title) in enumerate(
            (
                ("party", "Сбор"),
                ("friends", "Друзья и настройки"),
                ("requests", "Просьбы друзей"),
                ("words", "Словарь и команды"),
            )
        ):
            button = ttk.Button(bar, text=title, command=lambda k=key: self._org_panel(k))
            button.grid(row=0, column=i, sticky="ew", padx=(0, 5))
            bar.columnconfigure(i, weight=1)
            self.org_tabs[key] = button
        self.org_panels = {
            key: tk.Frame(parent, bg=self.theme["bg"])
            for key in ("party", "friends", "requests", "words")
        }
        self._org_data = {}
        self._org_busy = False
        self._org_at = 0
        self._org_saved_prefs = None
        self._org_selected_contact = None
        self._org_dictionary_loaded = False
        self._org_build_party(self.org_panels["party"])
        self._org_build_friends(self.org_panels["friends"])
        self._org_build_requests(self.org_panels["requests"])
        self._org_build_words(self.org_panels["words"])
        self._org_panel(getattr(self, "_org_current", "party"))

    def _org_panel(self, key):
        self._org_current = key
        for code, frame in self.org_panels.items():
            frame.pack_forget()
            if code == key:
                frame.pack(fill="both", expand=True)
        if getattr(self, "_org_data", None):
            self._org_render(self._org_data)
        self.canvases["organizer"].yview_moveto(0)
        for code, button in self.org_tabs.items():
            button.configure(style="Selected.Nav.TButton" if code == key else "TButton")
        if key == "words" and not self._org_dictionary_loaded:
            self._org_dictionary()
            self._org_dictionary_loaded = True

    def _org_build_party(self, parent):
        card = self.card(
            parent,
            "Ваш следующий сбор",
            "В составе учитываетесь и вы. Запуск отправляет приглашения выбранным друзьям; остальные остаются резервом.",
        )
        self.org_plan = tk.StringVar()
        self.label(card, textvariable=self.org_plan, wraplength=730).pack(fill="x", pady=4)
        actions = self._buttons(
            card,
            [
                ("Созвать компанию", lambda: self._org_call("create")),
                ("Пауза", lambda: self._org_call("pause")),
                ("Продолжить", lambda: self._org_call("resume")),
                ("Отменить сбор", lambda: self._org_call("cancel")),
                ("Открыть игру и канал", lambda: self._org_call("launch")),
                ("Настроить сбор", lambda: self._org_panel("friends")),
            ],
        )
        self.org_action_buttons = {b.cget("text"): b for b in actions.winfo_children()}
        self.org_members = self._table(
            card, {"name": "Друг", "state": "Ответ", "eta": "Когда", "steam": "Steam"}, 7
        )
        for col, width in {"name": 160, "state": 220, "eta": 70, "steam": 160}.items():
            self.org_members.column(col, width=width)
        self.org_member_detail = tk.StringVar(
            value="Выберите друга, чтобы увидеть фразу и её разбор"
        )
        self.org_members.bind("<<TreeviewSelect>>", lambda _: self._org_member_detail())
        self.label(card, textvariable=self.org_member_detail, wraplength=730).pack(fill="x", pady=6)
        self._buttons(
            card,
            [
                ("Уточнить готовность", lambda: self._org_member_action("member_remind")),
                ("Исключить из этого сбора", lambda: self._org_member_action("member_remove")),
            ],
        )
        card = self.card(
            parent,
            "Доставка сообщений",
            "«В очереди» и «отправляется» не означают доставку. Неподтверждённая отправка автоматически не повторяется.",
        )
        self.org_delivery = self._table(
            card, {"name": "Друг", "kind": "Сообщение", "state": "Доставка"}, 5
        )
        self.org_steam_status = tk.StringVar()
        self.label(card, textvariable=self.org_steam_status, muted=True, wraplength=730).pack(
            fill="x"
        )

    def _org_build_friends(self, parent):
        card = self.card(
            parent,
            "Через кого общается Крест",
            "Telegram — контакты и личные диалоги. Discord — список друзей аккаунта. Серверный бот для этой функции не используется.",
        )
        self.org_messenger = tk.StringVar(value="Telegram")
        combo = self._org_combo(card, self.org_messenger, list(SERVICES.values()))
        combo.bind("<<ComboboxSelected>>", lambda _: self._org_change_service())
        self.org_connection = tk.StringVar()
        self.label(card, textvariable=self.org_connection, wraplength=730).pack(fill="x")
        self._buttons(
            card,
            [
                (
                    "Загрузить друзей",
                    lambda: self._org_call("contacts_load", service=self._org_service()),
                ),
                (
                    "Настроить юзербот",
                    lambda: self.switch_page(
                        "api" if self._org_service() == "tg_bridge.py" else "discord_account"
                    ),
                ),
            ],
        )
        self.org_search = tk.StringVar()
        search = self.field(card, "Найти друга", self.org_search)
        search.bind("<KeyRelease>", lambda _: self._org_render_friends())
        self.org_book = self._table(card, {"name": "Имя", "username": "Ник", "state": "Выбран"}, 6)
        self._buttons(card, [("Добавить выбранных в состав", self._org_add_selected)])
        self.label(
            card,
            "Можно выбрать нескольких с Ctrl или Shift. Добавление сохраняет выбор; сообщения отправятся только после начала сбора.",
            muted=True,
            wraplength=730,
        ).pack(fill="x")
        card = self.card(
            parent,
            "Состав и резерв",
            "Первые друзья получают приглашение. При отказе или отсутствии ответа за 10 минут Крест переходит к следующему.",
        )
        self.org_roster = self._table(
            card,
            {"number": "Порядок", "name": "Друг", "enabled": "В составе", "requests": "Просьбы"},
            5,
        )
        for col, width in {"number": 70, "name": 240, "enabled": 100, "requests": 100}.items():
            self.org_roster.column(col, width=width)
        self.org_roster.bind("<<TreeviewSelect>>", lambda _: self._org_edit_contact())
        self._buttons(
            card,
            [
                ("Выше", lambda: self._org_move(False)),
                ("Ниже", lambda: self._org_move(True)),
                ("Удалить", self._org_delete_contact),
            ],
        )
        editor = self.advanced(card, "Настроить выбранного друга")
        self.org_contact_name = tk.StringVar()
        self.org_contact_steam = tk.StringVar()
        self.org_contact_enabled = tk.BooleanVar(value=True)
        self.org_contact_requests = tk.BooleanVar(value=False)
        self.field(editor, "Обращаться по имени", self.org_contact_name)
        self.field(editor, "SteamID64 (необязательно)", self.org_contact_steam)
        ttk.Checkbutton(
            editor, text="Приглашать в компанию", variable=self.org_contact_enabled
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            editor,
            text="Принимать просьбы «передай владельцу…»",
            variable=self.org_contact_requests,
        ).pack(anchor="w", pady=4)
        self._buttons(
            editor,
            [
                ("Сохранить друга", self._org_save_contact),
                ("Найти профиль в Steam", lambda: self._org_call("steam_friends")),
            ],
        )
        self.org_steam_book = self._table(editor, {"name": "Друг Steam", "id": "SteamID64"}, 4)
        self.org_steam_book.bind("<<TreeviewSelect>>", lambda _: self._org_pick_steam())
        self.org_steam_friends_status = tk.StringVar(
            value="Список Steam загружается по кнопке; выберите профиль для привязки"
        )
        self.label(
            editor, textvariable=self.org_steam_friends_status, wraplength=730, muted=True
        ).pack(fill="x")
        card = self.card(
            parent,
            "Настройки сбора",
            "Время берётся с вашего компьютера. Крест уточнит готовность перед началом; истечение названных другом 20 минут само по себе не означает «готов».",
        )
        self.org_game = tk.StringVar(value="Dota 2")
        self.org_size = tk.StringVar(value="5")
        self.org_when = tk.StringVar(value="сейчас")
        self.org_channel = tk.StringVar()
        self.field(card, "Игра", self.org_game)
        self.field(card, "Людей вместе с вами", self.org_size)
        self.field(card, "Когда: сейчас / в 21:30", self.org_when)
        self.field(card, "Канал Discord (ссылка)", self.org_channel, paste_button=True)
        self._buttons(card, [("Сохранить настройки сбора", self._org_save_preferences)])
        card = self.card(
            parent,
            "Характер компании",
            "Приглашения и сводки используют выбранную личность Крест. Просмотр примеров ничего не отправляет и не меняет настройки.",
        )
        self.org_personality_active = tk.StringVar()
        self.label(card, textvariable=self.org_personality_active, wraplength=730).pack(fill="x")
        examples = self.advanced(card, "Посмотреть манеру общения")
        self.org_preview_style = tk.StringVar(value=PERSONALITIES["friendly"])
        self._org_preview_variant = 0
        self._org_personality_seen = None
        combo = self._org_combo(examples, self.org_preview_style, list(PERSONALITIES.values()))
        combo.bind("<<ComboboxSelected>>", lambda _: self._org_personality_preview())
        self.org_personality_examples = tk.StringVar()
        self.label(examples, textvariable=self.org_personality_examples, wraplength=730).pack(
            fill="x", pady=8
        )
        self._buttons(
            examples,
            [
                ("Другие реплики", self._org_next_personality_example),
                ("Выбрать личность в настройках", lambda: self.switch_page("settings")),
            ],
        )
        self._org_personality_preview()

    def _org_next_personality_example(self):
        self._org_preview_variant = (self._org_preview_variant + 1) % 4
        self._org_personality_preview()

    def _org_personality_preview(self):
        key = next(k for k, v in PERSONALITIES.items() if v == self.org_preview_style.get())
        data = personality_preview(
            {"profile": {"personality": key}, "owner_name": "Владелец"}, self._org_preview_variant
        )
        self.org_personality_examples.set(
            data["feature"]
            + f"\n\nВариант {self._org_preview_variant + 1} из 4"
            + "\nПриглашение: "
            + data["invitation"]
            + "\n\nУточнение: "
            + data["clarify"]
            + "\n\nСостав готов: "
            + data["ready"]
            + "\n\nПросьба друга: "
            + data["request"]
        )

    def _org_build_requests(self, parent):
        card = self.card(
            parent,
            "Что передали друзья",
            "Работает только для друзей с включённым приёмом просьб. Если вы играете, Крест передаст сообщение после игры. Текст друга не выполняется как команда компьютеру.",
        )
        self.org_requests = self._table(
            card, {"name": "От кого", "state": "Состояние", "text": "Просьба"}, 6
        )
        self.org_request_text = tk.StringVar(value="Выберите просьбу")
        self.org_requests.bind("<<TreeviewSelect>>", lambda _: self._org_request_detail())
        self.label(card, textvariable=self.org_request_text, wraplength=730).pack(fill="x", pady=8)
        self.org_reply = tk.StringVar()
        self.field(card, "Ваш ответ", self.org_reply)
        self._buttons(
            card,
            [
                ("Отправить выбранному другу", self._org_reply_request),
                ("Закрыть просьбу", self._org_close_request),
            ],
        )

    def _org_build_words(self, parent):
        card = self.card(
            parent,
            "Как Крест понимает друзей",
            "Сленг, разговорные ответы, время, транслит и частые сокращения. Неуверенный ответ не превращается в согласие.",
        )
        self.org_phrase = tk.StringVar()
        entry = self.field(card, "Проверить фразу", self.org_phrase)
        entry.bind("<Return>", lambda _: self._org_explain())
        self.org_explanation = tk.StringVar(
            value="Например: «мин через 20 залечу», «мб позже», «не готов»"
        )
        self._buttons(
            card,
            [("Разобрать ответ", self._org_explain), ("Найти в словаре", self._org_dictionary)],
        )
        self.label(card, textvariable=self.org_explanation, wraplength=730).pack(fill="x", pady=8)
        self.org_dictionary = self._table(card, {"phrase": "Фраза", "meaning": "Значение"}, 7)
        self.org_word_count = tk.StringVar()
        self.label(card, textvariable=self.org_word_count, muted=True).pack(anchor="w")
        self.org_alias_kind = tk.StringVar(value=LABELS["joined"])
        self._org_combo(
            card,
            self.org_alias_kind,
            [LABELS[k] for k in ("ready", "joined", "waiting", "declined", "uncertain")],
        )
        self._buttons(
            card,
            [
                ("Добавить фразу в личный словарь", self._org_save_alias),
                (
                    "Удалить личную фразу",
                    lambda: self._org_call(
                        "alias_delete", phrase=self.org_phrase.get().strip().casefold()
                    ),
                ),
            ],
        )
        self.org_aliases = tk.StringVar()
        self.label(card, textvariable=self.org_aliases, wraplength=730, muted=True).pack(
            fill="x", pady=6
        )
        card = self.card(
            parent,
            "Голосовые команды компании",
            "Выбор примера показывает команду целиком. «Копировать» не запускает её и не отправляет приглашения.",
        )
        self.org_commands = self._table(card, {"command": "Команда", "meaning": "Что сделает"}, 7)
        for i, (command, meaning) in enumerate(COMMANDS):
            self.org_commands.insert("", "end", iid=str(i), values=(command, meaning))
        self.org_command_detail = tk.StringVar(value=COMMANDS[0][0] + "\n" + COMMANDS[0][1])
        self.org_commands.bind("<<TreeviewSelect>>", lambda _: self._org_command_selected())
        self.label(card, textvariable=self.org_command_detail, wraplength=730).pack(fill="x")
        self._buttons(card, [("Копировать команду", self._org_copy_command)])

    def _org_service(self):
        return next(k for k, v in SERVICES.items() if v == self.org_messenger.get())

    def _org_call(self, action, done=None, **values):
        status = self.org_status

        def work():
            try:
                return True, self.runtime.organizer.call(action, **values)
            except Exception as exc:
                return False, str(exc)

        def show(result):
            if not self.winfo_exists() or self.org_status is not status:
                return
            ok, value = result
            message = value if isinstance(value, str) else "Готово"
            status.set(message)
            self.status_var.set(message)
            if done:
                done(ok, value)
            self._org_refresh(force=True)

        self._background(work, show)

    def _org_change_service(self):
        def done(ok, _):
            if not ok:
                source = self._org_data.get("preferences", {}).get("messenger", "tg_bridge.py")
                self.org_messenger.set(SERVICES[source])
            self._org_render_friends()

        self._org_call("preferences", done=done, messenger=self._org_service())

    def _org_save_preferences(self):
        try:
            size = int(self.org_size.get())
        except ValueError:
            self.org_status.set("Введите число игроков от 2 до 10")
            return
        self._org_call(
            "preferences",
            messenger=self._org_service(),
            game=self.org_game.get().strip(),
            size=size,
            when=self.org_when.get().strip(),
            channel=self.org_channel.get().strip(),
        )

    def _org_add_selected(self):
        selected, source = set(self.org_book.selection()), self._org_service()
        rows = [
            r
            for r in self._org_data.get("address_books", {}).get(source, [])
            if r["chat"] in selected
        ]
        existing = self._org_data.get("contacts", [])
        rows = [
            r
            for r in rows
            if not any(c["service"] == source and c["chat"] == r["chat"] for c in existing)
        ]
        if not rows:
            self.org_status.set("Выберите новых друзей в списке")
            return

        def work():
            count = 0
            names = {c["name"].casefold() for c in existing}
            try:
                for row in rows:
                    name = row["name"]
                    if name.casefold() in names:
                        name = name[:45] + " · " + row["chat"][-6:]
                    self.runtime.organizer.call(
                        "contact_save",
                        service=source,
                        chat=row["chat"],
                        name=name,
                        enabled=True,
                        requests=False,
                    )
                    names.add(name.casefold())
                    count += 1
                return f"Добавлено друзей: {count}. Сообщения ещё не отправлялись."
            except Exception as exc:
                return f"Добавлено: {count}. {exc}"

        status = self.org_status

        def done(text):
            if self.org_status is status:
                status.set(text)
                self._org_refresh(force=True)

        self._background(work, done)

    def _org_contact(self):
        selection = self.org_roster.selection()
        return next(
            (
                c
                for c in self._org_data.get("contacts", [])
                if selection and c["id"] == selection[0]
            ),
            None,
        )

    def _org_edit_contact(self):
        row = self._org_contact()
        if row and row["id"] != self._org_selected_contact:
            self._org_selected_contact = row["id"]
            self.org_contact_name.set(row["name"])
            self.org_contact_steam.set(row["steam"])
            self.org_contact_enabled.set(row["enabled"])
            self.org_contact_requests.set(row["requests"])

    def _org_save_contact(self):
        if row := self._org_contact():
            values = dict(
                row,
                name=self.org_contact_name.get().strip(),
                steam=self.org_contact_steam.get().strip(),
                enabled=self.org_contact_enabled.get(),
                requests=self.org_contact_requests.get(),
            )
            self._org_call("contact_save", **values)

    def _org_pick_steam(self):
        selection = self.org_steam_book.selection()
        if selection:
            self.org_contact_steam.set(selection[0])

    def _org_move(self, down):
        if row := self._org_contact():
            self._org_call("contact_move", id=row["id"], down=down)

    def _org_delete_contact(self):
        if row := self._org_contact():
            self._org_call("contact_delete", id=row["id"])

    def _org_member_detail(self):
        selection = self.org_members.selection()
        party = self._org_data.get("party") or {}
        row = next(
            (m for m in party.get("members", []) if selection and m["id"] == selection[0]), None
        )
        if row:
            self.org_member_detail.set(
                row["name"]
                + ": "
                + row.get("last_text", "Ответа ещё нет")
                + "\n"
                + row.get("detail", "")
            )

    def _org_member_action(self, action):
        selection = self.org_members.selection()
        if selection:
            self._org_call(action, id=selection[0])

    def _org_request(self):
        selected = self.org_requests.selection()
        return next(
            (r for r in self._org_data.get("requests", []) if selected and r["id"] == selected[0]),
            None,
        )

    def _org_request_detail(self):
        if row := self._org_request():
            self.org_request_text.set("От " + row["name"] + ": " + row["text"])

    def _org_reply_request(self):
        if row := self._org_request():
            self._org_call("request_reply", id=row["id"], text=self.org_reply.get())

    def _org_close_request(self):
        if row := self._org_request():
            self._org_call("request_done", id=row["id"])

    def _org_explain(self):
        def done(ok, value):
            if ok:
                self.org_explanation.set(
                    value["label"]
                    + ". "
                    + value["reason"]
                    + (
                        f"\nВремя: {datetime.fromtimestamp(value['due']):%d.%m %H:%M}"
                        if value.get("due")
                        else ""
                    )
                )

        self._org_call("interpret", done=done, text=self.org_phrase.get())

    def _org_dictionary(self):
        from organizer_language import normalize

        query = normalize(self.org_phrase.get())
        rows = [
            (kind + ":" + phrase, (phrase, LABELS[kind]))
            for kind, words in PHRASES.items()
            for phrase in sorted(words)
            if not query or query in phrase
        ]
        self._smart_rows("org_dictionary", rows)
        self.org_word_count.set(
            f"Встроенных фраз: {sum(map(len, PHRASES.values()))}. Показано: {len(rows)}"
        )

    def _org_save_alias(self):
        kind = next(k for k, v in LABELS.items() if v == self.org_alias_kind.get())
        self._org_call("alias_save", phrase=self.org_phrase.get(), kind=kind)

    def _org_command_selected(self):
        if selection := self.org_commands.selection():
            command, description = COMMANDS[int(selection[0])]
            self.org_command_detail.set(command + "\n" + description)

    def _org_copy_command(self):
        selection = self.org_commands.selection()
        index = int(selection[0]) if selection else 0
        self.clipboard_clear()
        self.clipboard_append(COMMANDS[index][0])
        self.org_status.set("Команда скопирована")

    def _org_render_friends(self):
        if not self._org_data:
            return
        source = self._org_service()
        rows = self._org_data.get("address_books", {}).get(source, [])
        query = self.org_search.get().casefold().strip()
        chosen = {c["chat"] for c in self._org_data["contacts"] if c["service"] == source}
        self._smart_rows(
            "org_book",
            [
                (
                    r["chat"],
                    (
                        r["name"],
                        "@" + r["username"] if r["username"] else "—",
                        "Да" if r["chat"] in chosen else "",
                    ),
                )
                for r in rows
                if not query or query in (r["name"] + " " + r["username"]).casefold()
            ],
        )
        contacts = [c for c in self._org_data["contacts"] if c["service"] == source]
        self._smart_rows(
            "org_roster",
            [
                (
                    c["id"],
                    (
                        i + 1,
                        c["name"],
                        "Да" if c["enabled"] else "Нет",
                        "Да" if c["requests"] else "Нет",
                    ),
                )
                for i, c in enumerate(contacts)
            ],
        )
        for i, c in enumerate(contacts):
            self.org_roster.move(c["id"], "", i)
        bridge = self._org_data.get("connections", {}).get(source, {})
        status = (
            "Юзербот подключён"
            if bridge.get("ready") and bridge.get("capable")
            else "Нужен подключённый юзербот из обновлённой версии Крест"
        )
        self.org_connection.set(
            status
            + "\n"
            + self._org_data.get("contact_status", {}).get(source, "Нажмите «Загрузить друзей»")
        )
        self._smart_rows(
            "org_steam_book",
            [
                (r["chat"], (r["name"], r["chat"]))
                for r in self._org_data.get("address_books", {}).get("steam", [])
            ],
        )
        self.org_steam_friends_status.set(
            self._org_data.get("contact_status", {}).get(
                "steam",
                "Steam используется для статусов и запуска игры. Сообщения идут через выбранный юзербот.",
            )
        )

    def _org_render(self, data):
        self._org_data = data
        persona = data.get("personality", "friendly")
        self.org_personality_active.set(
            "Активна: " + PERSONALITIES[persona] + ". " + data.get("personality_feature", "")
        )
        if persona != self._org_personality_seen:
            self._org_personality_seen = persona
            self.org_preview_style.set(PERSONALITIES[persona])
            self._org_personality_preview()
        prefs = data["preferences"]
        if self._org_saved_prefs != prefs:
            self.org_messenger.set(SERVICES[prefs["messenger"]])
            for var, key in (
                (self.org_game, "game"),
                (self.org_size, "size"),
                (self.org_when, "when"),
                (self.org_channel, "channel"),
            ):
                if self._org_saved_prefs is None or self._org_saved_prefs[key] != prefs[key]:
                    var.set(str(prefs[key]))
            self._org_saved_prefs = dict(prefs)
        party = data.get("party")
        state = (party or {}).get("status", "")
        allowed = {
            "Созвать компанию": state not in {"collecting", "ready", "paused"},
            "Пауза": state in {"collecting", "ready"},
            "Продолжить": state == "paused",
            "Отменить сбор": state in {"collecting", "ready", "paused"},
            "Открыть игру и канал": state == "ready",
            "Настроить сбор": True,
        }
        for title, button in self.org_action_buttons.items():
            button.configure(state="normal" if allowed[title] else "disabled")
        self.org_status.set(
            (PARTY_STATES.get(party["status"], "") + " · " if party else "") + data["summary"]
        )
        self.org_plan.set(
            f"{SERVICES[prefs['messenger']]} · {prefs['game']} · {prefs['size']} человек · {prefs['when']}\nВыбранных друзей: {sum(c['enabled'] and c['service'] == prefs['messenger'] for c in data['contacts'])}"
        )
        if self._org_current == "friends":
            self._org_render_friends()
        elif self._org_current == "party":
            members = (party or {}).get("members", [])
            self._smart_rows(
                "org_members",
                [
                    (
                        m["id"],
                        (
                            m["name"],
                            data["labels"].get(m["state"], m["state"]),
                            datetime.fromtimestamp(m["due"]).strftime("%H:%M")
                            if m.get("due")
                            else "—",
                            self._org_steam_text(data["steam"].get(m["id"], {})),
                        ),
                    )
                    for m in members
                ],
            )
            names = {c["id"]: c["name"] for c in data["contacts"]}
            kinds = {
                "invite": "Приглашение",
                "eta": "Уточнение времени",
                "question": "Ответ о сборе",
                "clarify": "Уточнение ответа",
                "ready": "Состав готов",
                "cancel": "Отмена",
                "reserve": "Резерв",
                "request_ack": "Просьба принята",
                "request_reply": "Ваш ответ",
                "removed": "Исключение из сбора",
                "remind": "Уточнение готовности",
                "promotion": "Приглашение из резерва",
            }
            self._smart_rows(
                "org_delivery",
                [
                    (
                        r["id"],
                        (
                            names.get(r["contact"], "Контакт удалён"),
                            kinds.get(r["purpose"], r["purpose"]),
                            DELIVERY[r["status"]],
                        ),
                    )
                    for r in reversed(data["outbox"][-20:])
                ],
            )
            self.org_steam_status.set(data["steam_error"])
        elif self._org_current == "requests":
            self._smart_rows(
                "org_requests",
                [
                    (r["id"], (r["name"], REQUESTS[r["status"]], r["text"][:100]))
                    for r in reversed(data["requests"])
                ],
            )
        elif self._org_current == "words":
            self.org_aliases.set(
                "Личные фразы: "
                + (
                    "; ".join(r["phrase"] + " → " + LABELS[r["kind"]] for r in data["aliases"])
                    or "пока нет"
                )
            )

    @staticmethod
    def _org_steam_text(value):
        if not value or not value.get("available"):
            return "Нет данных"
        if time.time() - value["updated"] > 300:
            return "Данные устарели"
        return value.get("game") or ("В сети" if value["online"] else "Офлайн / скрыт")

    def _org_refresh(self, force=False):
        if (
            self.current_page != "organizer"
            or self.state() not in {"normal", "zoomed"}
            or not hasattr(self, "org_members")
            or not self.org_members.winfo_exists()
            or self._org_busy
        ):
            return
        if not force and time.monotonic() - self._org_at < 2:
            return
        self._org_busy = True
        self._org_at = time.monotonic()
        tree = self.org_members

        def work():
            try:
                return self.runtime.organizer.call("snapshot")
            except Exception as exc:
                return str(exc)

        def done(data):
            if not tree.winfo_exists() or self.org_members is not tree:
                return
            self._org_busy = False
            if isinstance(data, str):
                self.org_status.set(data)
            else:
                self._org_render(data)

        self._background(work, done)
