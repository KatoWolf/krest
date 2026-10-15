"""Compact companion diagnostics and learning controls, shared with the kernel."""

import tkinter as tk
from tkinter import ttk
import time


class ExperiencePages:
    def _companion_action(self, action, **values):
        def finish(result):
            self.status_var.set(
                str(result) if not isinstance(result, bool) else "Изменение сохранено"
            )
            self._refresh_experience(force=True)

        self._background(lambda: self.runtime.companion.call(action, **values), finish)

    def _build_learning_controls(self, page):
        card = self.card(
            page,
            "Обучение вашему произношению",
            "После ошибки скажите «Крест, я сказал переключи трек». Повторяющиеся исправления появятся здесь. Применяются только подтверждённые записи этого профиля.",
        )
        self.learning_tree = ttk.Treeview(
            card, columns=("heard", "command", "hits", "state"), show="headings", height=5
        )
        for key, title, width in (
            ("heard", "Услышала", 210),
            ("command", "Вы имели в виду", 220),
            ("hits", "Повторов", 75),
            ("state", "Решение", 130),
        ):
            self.learning_tree.heading(key, text=title)
            self.learning_tree.column(key, width=width)
        self.learning_tree.pack(fill="x", pady=10)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")

        def decide(status):
            selected = self.learning_tree.selection()
            if selected:
                self._companion_action(
                    "learning.decide",
                    heard=self.learning_tree.item(selected[0], "values")[0],
                    status=status,
                )

        for caption, status in (
            ("Запомнить", "approved"),
            ("Не использовать", "rejected"),
            ("Сбросить решение", "pending"),
        ):
            ttk.Button(row, text=caption, command=lambda s=status: decide(s)).pack(
                side="left", padx=(0, 8)
            )
        self._learning_stamp = None
        self._refresh_experience(force=True)

    def _build_scenario_voice(self):
        card = self.card(
            self.page_bodies["scenarios"],
            "Сценарий голосом",
            "Скажите: «Крест, создай сценарий Вечер из действий приглуши музыку, включи не беспокоить». После чтения шагов — «сохрани сценарий». Разделяйте шаги словами «затем» или «потом».",
        )
        self.scenario_draft_label = self.label(card, "Черновика пока нет", wraplength=680)
        self.scenario_draft_label.pack(anchor="w", pady=8)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")
        for label, action in (("Сохранить черновик", "draft.save"), ("Отменить", "draft.cancel")):
            ttk.Button(row, text=label, command=lambda a=action: self._companion_action(a)).pack(
                side="left", padx=(0, 8)
            )

    def _refresh_experience(self, force=False):
        self._refresh_hearing()
        snapshot = self.runtime.companion.snapshot()
        trace = snapshot.get("trace", {})
        parts = []
        for key, label in (
            ("capture_ms", "Захват звука"),
            ("audio_queue_ms", "Буфер звука"),
            ("decoder_ms", "Vosk"),
            ("pause_ms", "Пауза"),
            ("queue_ms", "Очередь"),
            ("execution_ms", "Действие"),
        ):
            if trace.get(key) is not None:
                parts.append(f"{label}: {trace[key]} мс")
        text = (
            " · ".join(parts) or "После команды здесь появятся задержки распознавания и выполнения"
        )
        if trace.get("skill"):
            text += (
                "\n"
                + trace["skill"].split(".")[-1]
                + (" · выполнено" if trace.get("success") else " · ошибка")
            )
        if snapshot.get("topic"):
            text += f"\nПродолжение без обращения: {snapshot['topic']} · {snapshot['remaining']} с"
        for name in ("home_trace", "voice_trace"):
            widget = getattr(self, name, None)
            if widget is not None and widget.winfo_exists() and widget.cget("text") != text:
                widget.configure(text=text)
        tree = getattr(self, "learning_tree", None)
        rows = snapshot.get("learning", [])
        if (
            tree is not None
            and tree.winfo_exists()
            and (force or rows != getattr(self, "_learning_stamp", None))
        ):
            selected = tree.selection()
            tree.delete(*tree.get_children())
            states = {
                "approved": "Запомнено",
                "pending": "Ждёт решения",
                "rejected": "Не использовать",
            }
            for row in rows:
                tree.insert(
                    "",
                    "end",
                    iid=row["heard"],
                    values=(row["heard"], row["command"], row["hits"], states[row["status"]]),
                )
            if selected and tree.exists(selected[0]):
                tree.selection_set(selected[0])
            self._learning_stamp = rows
        widget = getattr(self, "scenario_draft_label", None)
        if widget is not None and widget.winfo_exists():
            value = (snapshot.get("draft") or {}).get("text", "Черновика пока нет")
            if widget.cget("text") != value:
                widget.configure(text=value)
        for filename, widget in getattr(self, "bot_health_labels", {}).items():
            if widget.winfo_exists() and (force or widget.winfo_ismapped()):
                detail = self.integration.details(filename)
                bridge = detail.get("bridge", {})
                text = (
                    ("Процесс работает" if detail["running"] else "Процесс остановлен")
                    + " · "
                    + (
                        "Мост готов"
                        if bridge.get("ready") and detail["running"]
                        else "Мост не подключён"
                    )
                )
                if bridge.get("last_event"):
                    text += f"\nПоследний ответ сервиса: {int(max(0, time.time() - bridge['last_event']))} с назад"
                receipt = detail.get("last_action", {})
                if receipt:
                    text += (
                        "\nДействие: "
                        + receipt.get("text", receipt.get("command", ""))
                        + " · "
                        + {
                            "pending": "ожидает ответа",
                            "confirmed": "подтверждено",
                            "failed": "ошибка",
                        }.get(receipt.get("status"), "")
                    )
                if detail.get("error"):
                    text += "\n" + detail["error"][:220]
                if bridge.get("diagnostic"):
                    text += "\n" + bridge["diagnostic"]
                    text += f" · ожидают ответа: {bridge.get('pending', 0)}"
                if widget.cget("text") != text:
                    widget.configure(text=text)

    def build_bots_tab(self):
        page = self.page_bodies["bots"]
        self.bot_health_labels = {}
        card = self.card(
            page,
            "Совместимость приложений",
            "Проверка доступных действий плеера и Discord после их обновления.",
        )
        for label, kind in (
            ("Проверить плеер", "music"),
            ("Проверить микрофон Discord", "discord"),
        ):
            ttk.Button(card, text=label, command=lambda k=kind: self._check_adapter(k)).pack(
                side="left", padx=(0, 8)
            )
        routes = {
            "steam_bot.py": "steam",
            "tg_bridge.py": "api",
            "krest_bot.py": "api",
            "discord_account.py": "discord_account",
            "ds_session.py": "discord_bot",
            "discord_presence.py": "discord_account",
        }
        for _, (filename, title) in self.integration.MODULES.items():
            card = self.card(page, title)
            label = self.label(card, "Проверяю состояние…", wraplength=650)
            label.pack(anchor="w", pady=8)
            self.bot_health_labels[filename] = label
            row = tk.Frame(card, bg=self.theme["card"])
            row.pack(fill="x")
            ttk.Button(
                row,
                text="Открыть панель",
                command=lambda f=filename: self.switch_page(routes.get(f, "scripts")),
            ).pack(side="left")
            ttk.Button(
                row, text="Проверить файлы", command=lambda f=filename: self._check_bot_project(f)
            ).pack(side="left", padx=8)
            ttk.Button(
                row, text="Журнал", command=lambda f=filename: self._open_service_log(f)
            ).pack(side="left")
        self._refresh_experience(force=True)

    def _check_bot_project(self, filename):
        from bot_health import inspect_project

        self.status_var.set("Проверяю Python, файл запуска и зависимости…")
        self._background(
            lambda: inspect_project(self.integration.path(filename)),
            lambda result: self._show_bot_check(result),
        )

    def _show_bot_check(self, text):
        from ui_dialogs import messagebox

        messagebox.showinfo("Результат проверки", text, parent=self)

    def _check_adapter(self, kind):
        from adapter_checks import check

        self.status_var.set("Проверяю доступные действия приложения…")
        self._background(
            lambda: check(
                kind,
                self.runtime.memory,
                self.config_mgr.snapshot().get("media", {}).get("preferred_app", ""),
            ),
            self._show_bot_check,
        )
