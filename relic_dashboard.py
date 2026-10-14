"""Voice-first Relic home. No model imports, network requests or idle animation."""

import tkinter as tk
from tkinter import ttk
from ui_fonts import UI_FONT
from ui_design import RoundedCard
from ui_relic import RelicEmblem, StateTile, ThornButton


def build(app):
    page, colours = app.page_bodies["actions"], app.theme
    surface = RoundedCard(page, colours, padding=22)
    surface.pack(fill="x", pady=(0, 12))
    card = surface.inner
    top = tk.Frame(card, bg=colours["card"])
    top.pack(fill="x")
    RelicEmblem(top, colours, 136).pack(side="right", padx=(16, 0))
    text = tk.Frame(top, bg=colours["card"])
    text.pack(side="left", fill="both", expand=True)
    app.label(text, "РЕЛИКТ  /  ГОЛОСОВОЕ УПРАВЛЕНИЕ", muted=True).pack(anchor="w", pady=(0, 10))
    app.home_greeting = app.label(
        text,
        "Привет, " + app.owner_var.get()[:24] + "." if app.owner_var.get() else "Привет. Я Крест.",
    )
    app.home_greeting.configure(font=(UI_FONT, 27, "bold"))
    app.home_greeting.pack(anchor="w")
    app.label(text, "Ваш голос — начало действия.", muted=True).pack(anchor="w", pady=(6, 14))
    app.label(text, textvariable=app.voice_var, wraplength=470).pack(anchor="w")
    app.home_meter = ttk.Progressbar(text, maximum=100)
    app.home_meter.pack(fill="x", pady=(9, 0))
    app.home_mic_detail = app.label(
        card, "Микрофон подбирается автоматически", muted=True, wraplength=700
    )
    app.home_mic_detail.pack(anchor="w", pady=(10, 12))
    controls = tk.Frame(card, bg=colours["card"])
    controls.pack(fill="x")
    for i, (label, callback) in enumerate(
        (
            ("Включить голос", app._start_voice),
            ("Пауза", app.runtime.stop_voice),
            ("Проверить слух", lambda: app.switch_page("voice")),
            ("Все команды", lambda: app.switch_page("skills")),
        )
    ):
        ThornButton(
            controls,
            colours,
            text=label,
            command=callback,
            style="Accent.TButton" if i == 0 else "TButton",
        ).pack(side="left", padx=(0, 7))

    row = tk.Frame(page, bg=colours["bg"])
    row.pack(fill="x", pady=(0, 12))
    app.home_tiles = {}
    for index, (key, title, target) in enumerate(
        (
            ("tasks", "ВАШ ДЕНЬ", "timers"),
            ("notices", "ВХОДЯЩИЕ", "inbox"),
            ("services", "ПОДКЛЮЧЕНИЯ", "bots"),
        )
    ):
        row.columnconfigure(index, weight=1, uniform="home")
        tile = StateTile(row, colours, title, lambda c=target: app.switch_page(c))
        tile.grid(row=0, column=index, sticky="ew", padx=(0, 8 if index < 2 else 0))
        app.home_tiles[key] = tile

    card = app.card(page, "Последнее обращение")
    app.label(card, textvariable=app.heard_var, muted=True, wraplength=700).pack(anchor="w")
    app.label(card, textvariable=app.understood_var, wraplength=700).pack(anchor="w", pady=5)
    app.home_trace = app.label(card, "", muted=True, wraplength=700)
    app.home_trace.pack(anchor="w")
    app.command_result = app.label(card, "Попробуйте: «Крест, который час?»", wraplength=700)
    app.command_result.pack(anchor="w", fill="x", pady=(5, 0))

    shortcuts = tk.Frame(page, bg=colours["bg"])
    shortcuts.pack(fill="x", pady=(0, 12))
    for index, (label, callback) in enumerate(
        (
            ("План на сегодня", lambda: app.runtime.submit("план на сегодня")),
            ("Таймер · 5 минут", lambda: app.runtime.submit("таймер на пять минут")),
            ("Моя музыка", lambda: app.switch_page("music")),
        )
    ):
        shortcuts.columnconfigure(index, weight=1, uniform="shortcuts")
        ThornButton(shortcuts, colours, text=label, command=callback).grid(
            row=0, column=index, sticky="ew", padx=(0, 8 if index < 2 else 0)
        )
    app.overview_label = app.label(page, "Крест готова к работе", muted=True, wraplength=700)
    app.overview_label.pack(anchor="w", pady=(0, 8))
    ttk.Button(page, textvariable=app.reminder_var, command=lambda: app.switch_page("timers")).pack(
        anchor="w", pady=(0, 12)
    )

    manual = app.advanced(page, "Написать вместо голоса")
    app.manual_input = manual.master
    row = tk.Frame(manual, bg=colours["card"])
    row.pack(fill="x", padx=12, pady=12)
    app.command_entry = ttk.Entry(row, font=(UI_FONT, 12))
    app.command_entry.pack(side="left", fill="x", expand=True)
    app.command_entry.bind("<Return>", lambda e: app.execute_command())
    app.command_entry.bind("<Up>", lambda e: app._repeat_command())
    app.command_entry.bind("<Down>", lambda e: app._repeat_command(1))
    ttk.Button(row, text="Выполнить", command=app.execute_command).pack(side="right", padx=(8, 0))
    app.label(manual, textvariable=app.queue_var, muted=True, wraplength=700).pack(
        anchor="w", padx=12
    )
    tools = tk.Frame(manual, bg=colours["card"])
    tools.pack(fill="x", padx=12, pady=12)
    for label, callback in (
        ("Сохранить команду", app._save_favorite),
        ("Очистить очередь", app._clear_pending),
        ("Мои скриншоты", lambda: app.runtime.submit("открой скриншоты")),
    ):
        ttk.Button(tools, text=label, command=callback).pack(side="left", padx=(0, 8))
    history = app.advanced(page, "Последние события")
    app.event_text = tk.Text(
        history,
        height=7,
        bg=colours["sidebar"],
        fg=colours["text"],
        font=(UI_FONT, 11),
        relief="flat",
        wrap="word",
        state="disabled",
    )
    app.event_text.pack(fill="both", expand=True)
    app._render_history()


def refresh(app):
    if app.current_page != "actions":
        return
    tasks = len(app.runtime.state.snapshot()["active_timers"])
    services = len(app.integration.processes)
    unread = len(app.runtime.center.list(unread=True))
    app.home_tiles["tasks"].set(f"Таймеров: {tasks}" if tasks else "Открыть дела")
    app.home_tiles["services"].set(f"Запущено: {services}" if services else "Подключить")
    app.home_tiles["notices"].set(f"Новых: {unread}" if unread else "Нет новых")
