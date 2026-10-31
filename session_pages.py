"""Lazy session-protection page; all probes and API requests stay off the Tk thread."""

import tkinter as tk
from tkinter import ttk
import time


class SessionPages:
    def _guard_call(self, action, **values):
        def done(result):
            self.status_var.set(
                "Диагностика готова — результат в разделе «Разобраться с проблемой»"
                if action == "diagnose"
                else result
                if isinstance(result, str)
                else "Готово"
            )
            if action == "diagnose":
                self.guard_report.set(result)
            if action == "settings":
                self.guard_password.set("")
            if action == "game.install":
                self.guard_gsi.set(True)
            self._guard_refresh(force=True)

        self._background(lambda: self.runtime.session_guard.call(action, **values), done)

    def build_protection_tab(self):
        body = self.page_bodies["protection"]
        tabs = tk.Frame(body, bg=self.theme["bg"])
        tabs.pack(fill="x", pady=(0, 8))
        self.guard_panels = {}
        for key, label in (
            ("budget", "Нагрузка Крест"),
            ("network", "Загрузки и матч"),
            ("diagnosis", "Разобраться с проблемой"),
        ):
            ttk.Button(tabs, text=label, command=lambda k=key: self._guard_panel(k)).pack(
                side="left", padx=(0, 6)
            )
            self.guard_panels[key] = tk.Frame(body, bg=self.theme["bg"])
        cfg = self.config_mgr.snapshot()
        resource = cfg["resource_guard"]
        network = cfg["download_guard"]
        self.guard_enabled = tk.BooleanVar(value=resource["enabled"])
        self.guard_relief = tk.BooleanVar(value=resource["automatic_relief"])
        self.guard_cpu = tk.StringVar(value=str(resource["cpu_percent"]))
        self.guard_game_cpu = tk.StringVar(value=str(resource["game_cpu_percent"]))
        self.guard_memory = tk.StringVar(value=str(resource["memory_mb"]))
        self.guard_network = tk.BooleanVar(value=network["enabled"])
        self.guard_gsi = tk.BooleanVar(value=network["gsi"])
        self.guard_steam = tk.BooleanVar(value=network["steam_enabled"])
        self.guard_client = tk.StringVar(value=network["client"])
        self.guard_port = tk.StringVar(value=str(network["port"]))
        self.guard_user = tk.StringVar(value=network["username"])
        self.guard_password = tk.StringVar()
        self.guard_down = tk.StringVar(value=str(network["download_kib"]))
        self.guard_up = tk.StringVar(value=str(network["upload_kib"]))
        self.guard_game = tk.StringVar(value="Dota 2")
        self.guard_budget_status = tk.StringVar(value="Ожидаю замеры…")
        self.guard_network_status = tk.StringVar(value="Проверка ещё не запускалась")
        self.guard_report = tk.StringVar(
            value="Выберите проблему. Замер выполняется по запросу; постоянного сканирования процессов нет."
        )
        card = self.card(
            self.guard_panels["budget"],
            "Крест следит за собственным фоном",
            "Голос, напоминания и работа с друзьями остаются доступны. При превышении бюджета второстепенные задачи ждут снижения нагрузки.",
        )
        self.label(card, textvariable=self.guard_budget_status, wraplength=730).pack(
            fill="x", pady=8
        )
        ttk.Checkbutton(
            card, text="Автоматически регулировать фон", variable=self.guard_enabled
        ).pack(anchor="w")
        for title, var, options in [
            ("Целевой CPU Крест, %", self.guard_cpu, [2, 4, 6, 8, 10, 15, 20, 30]),
            ("Во время игры, %", self.guard_game_cpu, [1, 2, 3, 4, 6, 8, 10, 15, 20]),
            ("Порог памяти, МБ", self.guard_memory, [512, 768, 1024, 1600, 2048, 3072, 4096]),
        ]:
            self._guard_combo(card, title, var, [str(x) for x in options])
        self.label(
            card,
            "Это пороги для управления фоном, а не жёсткий лимит всей программы. Средний CPU функций учитывает их Python-поток; нативные потоки и процессы смотрите в «Нагрузка и файлы».",
            wraplength=730,
            muted=True,
        ).pack(fill="x", pady=8)
        self.guard_functions = self._table(
            card,
            {"name": "Функция", "cpu": "CPU, среднее %", "calls": "Проходы", "reason": "Состояние"},
            6,
        )
        self._buttons(
            card,
            [
                ("Сохранить бюджет", self._guard_save),
                ("Освободить ресурсы на 2 минуты", lambda: self._guard_call("relief")),
            ],
        )

        card = self.card(
            self.guard_panels["network"],
            "Загрузки уступают место матчу",
            "Крест меняет ограничения только у выбранного подключённого загрузчика. После матча возвращает прежние значения; ваши последующие ручные изменения сохраняются.",
        )
        self.label(card, textvariable=self.guard_network_status, wraplength=730).pack(
            fill="x", pady=8
        )
        ttk.Checkbutton(
            card,
            text="Ограничивать выбранный загрузчик во время матча",
            variable=self.guard_network,
        ).pack(anchor="w")
        self._guard_combo(card, "Загрузчик", self.guard_client, ["qbittorrent", "aria2"])
        for title, var in [
            ("Порт локального API", self.guard_port),
            ("Логин qBittorrent", self.guard_user),
            ("Загрузка в матче, КиБ/с", self.guard_down),
            ("Отдача в матче, КиБ/с", self.guard_up),
        ]:
            self.field(card, title, var)
        self.label(
            card, "Пароль qBittorrent / RPC-секрет aria2 (пусто — сохранить прежний)", muted=True
        ).pack(anchor="w", pady=(8, 4))
        ttk.Entry(card, textvariable=self.guard_password, show="•").pack(fill="x")
        self.label(
            card,
            "В загрузчике включите Web UI или RPC на 127.0.0.1. qBittorrent обычно использует порт 8080, aria2 — 6800. Используется обычный режим лимитов qBittorrent. Steam подключается отдельно ниже.",
            wraplength=730,
            muted=True,
        ).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Сохранить подключение", self._guard_save),
                ("Проверить сохранённое подключение", lambda: self._guard_call("downloads.test")),
            ],
        )
        card = self.card(
            self.guard_panels["network"],
            "Steam: пауза активных загрузок",
            "Используются доступные кнопки отдельных игр в разделе «Загрузки». Крест не переключает окно и не нажимает «Возобновить всё». Если Steam не предоставляет кнопку или результат не подтверждён, действие останавливается с объяснением.",
        )
        ttk.Checkbutton(
            card,
            text="Автоматически приостанавливать активные загрузки Steam",
            variable=self.guard_steam,
        ).pack(anchor="w")
        self.guard_steam_status = tk.StringVar(value="Автопауза выключена")
        self.label(card, textvariable=self.guard_steam_status, wraplength=730).pack(
            fill="x", pady=8
        )
        self._buttons(
            card,
            [
                ("Открыть загрузки Steam для подготовки", lambda: self._guard_call("steam.open")),
                ("Сохранить выбор Steam", self._guard_save),
            ],
        )
        self.label(
            card,
            "После матча возобновляются только подтверждённые загрузки, остановленные Крест. Если Steam перезапускался или состояние изменилось, потребуется проверка в Steam. Доступность управления в свёрнутом окне зависит от версии клиента.",
            wraplength=730,
            muted=True,
        ).pack(fill="x", pady=8)
        card = self.card(
            self.guard_panels["network"],
            "Как узнать о матче",
            "Dota 2 и CS2 передают состояние через Game State Integration. Для других игр скажите «матч начался» и «матч закончился». Запущенная игра сама по себе не считается матчем.",
        )
        ttk.Checkbutton(
            card, text="Принимать подтверждённое состояние от игры", variable=self.guard_gsi
        ).pack(anchor="w")
        self._guard_combo(card, "Игра", self.guard_game, ["Dota 2", "CS2"])
        self._buttons(
            card,
            [
                (
                    "Установить датчик игры",
                    lambda: self._guard_call(
                        "game.install", appid="570" if self.guard_game.get() == "Dota 2" else "730"
                    ),
                ),
                (
                    "Запустить игру с датчиком",
                    lambda: self._guard_call(
                        "game.launch", appid="570" if self.guard_game.get() == "Dota 2" else "730"
                    ),
                ),
            ],
        )
        self.label(
            card,
            "После установки перезапустите игру. Dota 2 требует -gamestateintegration: кнопка запуска передаёт его автоматически; для обычного запуска добавьте параметр один раз в Steam.",
            wraplength=730,
            muted=True,
        ).pack(fill="x", pady=8)
        self._buttons(
            card,
            [
                ("Матч начался", lambda: self._guard_call("match", active=True)),
                (
                    "Матч закончился / вернуть скорость",
                    lambda: self._guard_call("match", active=False),
                ),
            ],
        )

        card = self.card(
            self.guard_panels["diagnosis"],
            "Что мешает игре или звуку?",
            "Проверю CPU, память, активность диска и известные сбои Крест. Один замер не доказывает причину тормозов; если данных недостаточно, так и скажу.",
        )
        self._buttons(
            card,
            [
                ("Музыка заикается", lambda: self._guard_call("diagnose", problem="music")),
                ("Игра тормозит", lambda: self._guard_call("diagnose", problem="game")),
                ("Проблема с микрофоном", lambda: self._guard_call("diagnose", problem="voice")),
            ],
        )
        ttk.Checkbutton(
            card,
            text="При диагностике разрешать разгрузку собственного фона и штатное восстановление",
            variable=self.guard_relief,
        ).pack(anchor="w", pady=8)
        ttk.Button(card, text="Сохранить разрешённые исправления", command=self._guard_save).pack(
            anchor="w"
        )
        self.label(card, textvariable=self.guard_report, wraplength=730).pack(fill="x", pady=12)
        self.label(
            card,
            "Другие приложения не завершаются. Текущая установка не обрывается. Команды плееру и сообщения повторно не отправляются.",
            wraplength=730,
            muted=True,
        ).pack(fill="x")
        self._guard_panel("budget")
        self._guard_refresh(force=True)

    def _guard_panel(self, key):
        for panel in self.guard_panels.values():
            panel.pack_forget()
        self.guard_panels[key].pack(fill="both", expand=True)
        self.after_idle(lambda: self.canvases["protection"].yview_moveto(0))

    def _guard_combo(self, parent, label, variable, values):
        self.label(parent, label, muted=True).pack(anchor="w", pady=(8, 4))
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
        combo.pack(fill="x")
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            combo.bind(event, lambda _: "break")

    def _guard_save(self):
        try:
            resource = {
                "enabled": self.guard_enabled.get(),
                "automatic_relief": self.guard_relief.get(),
                "cpu_percent": int(self.guard_cpu.get()),
                "game_cpu_percent": int(self.guard_game_cpu.get()),
                "memory_mb": int(self.guard_memory.get()),
            }
            network = {
                "steam_enabled": self.guard_steam.get(),
                "enabled": self.guard_network.get(),
                "gsi": self.guard_gsi.get(),
                "client": self.guard_client.get(),
                "port": int(self.guard_port.get()),
                "username": self.guard_user.get(),
                "download_kib": int(self.guard_down.get()),
                "upload_kib": int(self.guard_up.get()),
            }
            password = self.guard_password.get()
        except ValueError:
            self.status_var.set("Порт и ограничения скорости должны быть целыми числами")
            return
        self._guard_call("settings", resource=resource, network=network, password=password)

    def _guard_refresh(self, force=False):
        if (
            not hasattr(self, "guard_functions")
            or not self.guard_functions.winfo_exists()
            or getattr(self, "_guard_busy", False)
        ):
            return
        if not force and (
            self.current_page != "protection"
            or self.state() not in ("normal", "zoomed")
            or time.monotonic() - getattr(self, "_guard_at", 0) < 5
        ):
            return
        self._guard_busy = True
        self._guard_at = time.monotonic()

        def work():
            try:
                return self.runtime.session_guard.snapshot()
            finally:
                self._guard_busy = False

        self._background(work, self._guard_display)

    def _guard_display(self, data):
        if not self.guard_functions.winfo_exists():
            return
        self.guard_steam_status.set(data.get("steam", {}).get("message", ""))
        self.guard_budget_status.set(
            data["budget"]["reason"]
            + (
                f" · разгрузка ещё {data['budget']['relief_seconds']} с"
                if data["budget"]["relief_seconds"]
                else ""
            )
        )
        self.guard_network_status.set(
            data["match"]["text"]
            + ("\n" + data["match"]["setup_message"] if data["match"].get("setup_message") else "")
            + "\n"
            + data["downloads"]["message"]
            + (
                "\nПовтор через " + str(data["downloads"]["retry_seconds"]) + " с"
                if data["downloads"]["retry_seconds"]
                else ""
            )
        )
        self._smart_rows(
            "guard_functions",
            [
                (r["id"], (r["name"], r["cpu_percent"], r["calls"], r["reason"]))
                for r in data["budget"]["functions"]
            ],
        )
        if data.get("report"):
            self.guard_report.set(data["report"]["text"])
