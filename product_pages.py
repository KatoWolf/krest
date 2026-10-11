"""Profile onboarding, music controls, and Premium UI."""

from ui_fonts import UI_FONT

from datetime import datetime
import time
import tkinter as tk
from tkinter import ttk
import licensing
from themes import PREMIUM_THEMES

from personality import PERSONALITIES


class ProductPages:
    def init_product_pages(self):
        cfg = self.config_mgr.snapshot()
        self.personality_var = tk.StringVar(value=PERSONALITIES[cfg["profile"]["personality"]])
        self.media_source_var = tk.StringVar(value=cfg["media"]["preferred_app"])
        self.media_status_var = tk.StringVar(
            value="Нажмите «Найти плееры». Крест выбирает играющую медиасессию автоматически."
        )
        self.music_busy = False
        self.music_refreshed_at = 0
        self.media_track_var = tk.StringVar(value="Включите музыку в любимом плеере")
        self.diagnostics_busy = False
        self.media_diagnostics = tk.StringVar(value="Проверка ничего не нажимает в плеере.")
        self.license_code_var = tk.StringVar()
        self.license_status_var = tk.StringVar()
        self.license_feedback_var = tk.StringVar()
        self._license_check_busy = False
        self._started = False
        self.onboarding = None

    def _start_after_profile(self):
        if self._started:
            return
        self._started = True
        self.runtime.start()
        self._background(self.integration.autostart)
        self._background(self._startup_checks)
        import os

        if os.name == "nt":
            self.ensure_tray()
            if not self.config_mgr.snapshot().get("applications"):
                self._scan_apps()

    def _show_welcome(self):
        if self.config_mgr.snapshot()["profile"]["onboarding_completed"]:
            self._start_after_profile()
            return
        frame = tk.Frame(self, bg=self.theme["bg"], padx=60, pady=50)
        self.onboarding = frame
        frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.label(frame, "КРЕСТ / ЗНАКОМСТВО", muted=True).pack(anchor="w", pady=(15, 30))
        self.label(frame, "Привет. Я Крест.", large=True).pack(anchor="w")
        self.label(
            frame,
            "Буду помогать с компьютером, музыкой и повседневными делами.\nКак мне к вам обращаться?",
            wraplength=620,
        ).pack(anchor="w", pady=20)
        self.welcome_name = ttk.Entry(frame, textvariable=self.owner_var, font=(UI_FONT, 16))
        self.welcome_name.pack(fill="x", pady=10)
        self.welcome_name.focus_set()
        self.label(frame, "Стиль общения", muted=True).pack(anchor="w", pady=(20, 6))
        ttk.Combobox(
            frame,
            textvariable=self.personality_var,
            values=list(PERSONALITIES.values()),
            state="readonly",
        ).pack(fill="x")
        self.welcome_error = self.label(frame, "", muted=True, wraplength=650)
        self.welcome_error.pack(anchor="w", pady=15)
        ttk.Button(
            frame,
            text="Познакомиться и войти",
            style="Accent.TButton",
            command=self._complete_welcome,
        ).pack(anchor="w", pady=15)
        self.label(
            frame,
            "Это локальный профиль. Регистрация в интернете и пароль не нужны.\nPremium можно подключить позже по коду доступа.",
            muted=True,
            wraplength=650,
        ).pack(anchor="w", pady=15)
        ttk.Button(frame, text="Выйти", command=self._real_quit).pack(anchor="w")
        self.welcome_name.bind("<Return>", lambda event: self._complete_welcome())

    def _complete_welcome(self):
        name = self.owner_var.get().strip()
        if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
            self.welcome_error.configure(text="Введите имя от 1 до 60 символов.")
            return
        if not self.save_all_settings(quiet=True):
            return
        self.onboarding.destroy()
        self.onboarding = None
        self.status_var.set(
            f"{name}, подключаю микрофон. Когда он будет готов, скажите: «Крест, который час?»"
        )
        self._start_after_profile()

    def build_music_tab(self):
        card = self.card(
            self.page_bodies["music"],
            "Сейчас играет",
            "Крест находит активный плеер автоматически.",
        )
        track = self.label(card, textvariable=self.media_track_var, wraplength=680)
        track.configure(font=(UI_FONT, 20, "bold"))
        track.pack(anchor="w", pady=(8, 14))
        self.label(card, textvariable=self.media_status_var, muted=True, wraplength=680).pack(
            anchor="w", pady=(0, 16)
        )
        for actions in [
            [
                ("Назад", "предыдущий трек"),
                ("Играть", "продолжи музыку"),
                ("Пауза", "пауза"),
                ("Далее", "следующий трек"),
            ],
            [
                ("−10 секунд", "перемотай назад"),
                ("+10 секунд", "перемотай вперед"),
                ("Нравится", "поставь лайк"),
                ("Убрать лайк", "убери лайк"),
            ],
        ]:
            row = tk.Frame(card, bg=self.theme["card"])
            row.pack(fill="x", pady=5)
            for title, command in actions:
                ttk.Button(
                    row,
                    text=title,
                    style="Accent.TButton" if title == "Играть" else "TButton",
                    command=lambda c=command: self._music_command(c),
                ).pack(side="left", padx=(0, 8))
        ttk.Button(card, text="Обновить трек", command=self._refresh_music).pack(
            anchor="w", pady=(14, 0)
        )
        card = self.card(
            self.page_bodies["music"],
            "Плеер и подключение",
            "Если одновременно играют несколько источников, выберите нужный здесь.",
        )
        ttk.Button(
            card, text="Подключить управление музыкой", command=self._install_music_components
        ).pack(anchor="w", pady=(0, 12))
        self.label(
            card,
            "Если подключение недоступно, Крест сама проверит и установит нужные компоненты. Команды PowerShell не нужны.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=(0, 8))
        sources = self.advanced(card, "Выбрать плеер вручную")
        self.music_tree = ttk.Treeview(
            sources, columns=("app", "track", "state"), show="headings", height=3
        )
        for key, title in [
            ("app", "Плеер"),
            ("track", "Исполнитель / трек"),
            ("state", "Состояние"),
        ]:
            self.music_tree.heading(key, text=title)
            self.music_tree.column(key, width=200 if key != "state" else 100)
        self.music_tree.pack(fill="x", pady=8)
        row = tk.Frame(sources, bg=self.theme["card"])
        row.pack(fill="x")
        for title, command in [
            ("Найти плееры", self._refresh_music),
            ("Выбрать источник", self._select_music),
            ("Автовыбор", self._auto_music),
        ]:
            ttk.Button(row, text=title, command=command).pack(side="left", padx=(0, 6))
        self.label(sources, textvariable=self.media_source_var, muted=True, wraplength=680).pack(
            anchor="w", pady=6
        )
        diagnostics = self.advanced(card, "Проверить доступные команды и лайки")
        self.label(
            diagnostics,
            "Лайки зависят от доступности кнопки в вашем плеере. Диагностика ничего не нажимает.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=8)
        ttk.Button(diagnostics, text="Диагностика плеера", command=self._diagnose_music).pack(
            anchor="w", pady=6
        )
        self.label(diagnostics, textvariable=self.media_diagnostics, wraplength=680).pack(
            anchor="w"
        )

    def _install_music_components(self):
        from app_paths import compiled

        if compiled():
            from ui_dialogs import messagebox

            messagebox.showinfo(
                "Управление музыкой",
                "Компоненты уже входят в установленную Крест. Полностью завершите Крест и запустите снова. Если ошибка повторится, переустановите программу; настройки сохранятся.",
                parent=self,
            )
            return
        import os
        import subprocess
        import sys
        from pathlib import Path

        if os.name != "nt":
            self.media_status_var.set("Прямое управление плеерами доступно в Windows.")
            return
        self.media_status_var.set("Открываю автоматическое подключение музыки…")

        def work():
            client = getattr(self.runtime, "client", None)
            info = client.call("environment") if client else {"executable": sys.executable}
            executable = Path(info["executable"])
            windowless = executable.with_name("pythonw.exe")
            python = windowless if windowless.exists() else executable
            return subprocess.Popen(
                [
                    str(python),
                    str(Path(__file__).with_name("setup_app.py")),
                    "--music",
                    "--python",
                    str(executable),
                ],
                cwd=Path(__file__).parent,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

        self._background(
            work,
            done=lambda _: self.media_status_var.set(
                "В открывшемся окне нажмите «Подключить музыку». После завершения — «Обновить трек»."
            ),
        )

    def _diagnose_music(self):
        from media_control import diagnostics

        if self.diagnostics_busy:
            return
        self.diagnostics_busy = True
        self.media_diagnostics.set("Проверяю возможности плеера…")
        selected = self.media_source_var.get()

        def work():
            try:
                return diagnostics(selected)
            except Exception as exc:
                return "Диагностика не завершена: " + str(exc)
            finally:
                self.post_ui(lambda: setattr(self, "diagnostics_busy", False))

        self._background(work, done=lambda text: self.media_diagnostics.set(text))

    def _music_command(self, command):
        if self.save_all_settings(quiet=True):
            self.runtime.submit(command)
            self.media_status_var.set("Команда отправлена. Результат появится здесь и в журнале.")

    def _refresh_music(self, quiet=False):
        if self.music_busy:
            return
        self.music_busy = True
        self.music_refreshed_at = time.monotonic()
        if not quiet:
            self.media_status_var.set("Ищу активные медиасессии…")

        def work():
            import media_control

            try:
                client = getattr(self.runtime, "client", None)
                rows = (
                    client.call("media_query")
                    if client
                    else media_control.query(
                        preferred=self.config_mgr.snapshot()["media"]["preferred_app"],
                        all_sessions=True,
                    )
                )

                def done():
                    self.music_tree.delete(*self.music_tree.get_children())
                    self.music_rows = rows
                    for i, data in enumerate(rows):
                        self.music_tree.insert(
                            "",
                            "end",
                            iid=str(i),
                            values=(
                                data["app_name"],
                                data["artist"] + " — " + data["title"],
                                "Играет" if data["is_playing"] else "На паузе",
                            ),
                        )
                    candidates = [v for v in rows if v.get("selected")]
                    if len(candidates) == 1:
                        current = candidates[0]
                        self.media_track_var.set(current["artist"] + " — " + current["title"])
                    else:
                        self.media_track_var.set(
                            "Выберите источник ниже" if rows else "Включите музыку в любимом плеере"
                        )
                    if not quiet:
                        self.media_status_var.set(
                            f"Найдено источников: {len(rows)}. Автовыбор включён."
                            if not self.media_source_var.get()
                            else f"Найдено источников: {len(rows)}. Источник закреплён."
                        )

                self.post_ui(done)
            except Exception as exc:
                self.post_ui(lambda message=str(exc): self.media_status_var.set(message))
            finally:
                self.post_ui(lambda: setattr(self, "music_busy", False))

        self._background(work)

    def _select_music(self):
        selected = self.music_tree.selection()
        if selected:
            self.media_source_var.set(self.music_rows[int(selected[0])]["app_id"])
            self._mark_dirty()
            self.save_all_settings(quiet=True)

    def _auto_music(self):
        self.media_source_var.set("")
        self._mark_dirty()
        self.save_all_settings(quiet=True)
        self.media_status_var.set(
            "Автовыбор включён: Крест найдёт играющий плеер при следующей команде."
        )

    def build_premium_tab(self):
        card = self.card(
            self.page_bodies["license"],
            "КРЕСТ Premium",
            "Подключение ботов и дополнительные темы оформления.",
        )
        self.label(card, textvariable=self.license_status_var, large=True).pack(anchor="w", pady=10)
        comparison = tk.Frame(card, bg=self.theme["card"])
        comparison.pack(fill="x", pady=(0, 12))
        for i, (title, details) in enumerate(
            [
                (
                    "Включено для всех",
                    "Голос и управление ПК\nМузыка, напоминания, навыки\nПомощники Warframe и Dota",
                ),
                (
                    "Возможности Premium",
                    "Боты Telegram, Discord и Steam\nСвои скрипты и мосты\nДополнительные темы оформления",
                ),
            ]
        ):
            tile = tk.Frame(comparison, bg=self.theme["sidebar"], padx=14, pady=12)
            tile.grid(row=0, column=i, sticky="nsew", padx=(0, 8) if i == 0 else (0, 0))
            comparison.columnconfigure(i, weight=1, uniform="editions")
            label = self.label(tile, title)
            label.configure(
                font=(UI_FONT, 11, "bold"), fg=self.theme["accent"] if i else self.theme["text"]
            )
            label.pack(anchor="w", pady=(0, 8))
            self.label(tile, details, muted=True, wraplength=320).pack(anchor="w")
        self.label(
            card,
            "Скопируйте код целиком и нажмите «Вставить и активировать». Вручную переписывать его не нужно.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=(8, 4))
        self.license_code_entry = self.field(
            card, "Код доступа", self.license_code_var, secret=True
        )
        self.license_code_entry.bind("<<Paste>>", lambda event: self._paste_premium())
        self.license_code_entry.bind("<Shift-Insert>", lambda event: self._paste_premium())
        self.license_code_entry.bind("<Control-KeyPress>", self._premium_shortcut)
        self.license_code_entry.bind("<Return>", self._premium_enter)
        self.license_code_entry.bind("<Button-3>", self._premium_context_menu)
        self.label(card, textvariable=self.license_feedback_var, muted=True, wraplength=680).pack(
            anchor="w"
        )
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=10)
        ttk.Button(
            row,
            text="Вставить и активировать",
            style="Accent.TButton",
            command=lambda: self._paste_premium(activate=True),
        ).pack(side="left")
        ttk.Button(row, text="Вставить из буфера", command=self._paste_premium).pack(
            side="left", padx=8
        )
        ttk.Button(row, text="Активировать", command=self._activate_premium).pack(side="left")
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=(0, 10))
        ttk.Button(row, text="Отключить на этом ПК", command=self._deactivate_premium).pack(
            side="left"
        )
        self.label(
            card,
            "Проверка кода выполняется на этом компьютере. Код выдаёт автор Крест. Имя профиля не является паролем.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w")
        card = self.card(
            self.page_bodies["license"],
            "Ваше оформление",
            "Все палитры собраны в разделе «Оформление».",
        )
        ttk.Button(card, text="Выбрать тему", command=lambda: self.switch_page("appearance")).pack(
            anchor="w"
        )
        self._update_license_view()

    def _update_license_view(self):
        state = licensing.status(self.config_mgr.snapshot())
        text = state["message"]
        if state["premium"]:
            text += " · " + (
                datetime.fromtimestamp(state["expires"]).strftime("до %d.%m.%Y")
                if state["expires"]
                else "бессрочно"
            )
        self.license_status_var.set(text)
        if hasattr(self, "edition_label"):
            self.edition_label.configure(text="PREMIUM" if state["premium"] else "ОБЫЧНАЯ ВЕРСИЯ")

    @staticmethod
    def _normalize_premium_code(text):
        if len(text) > 8192:
            raise ValueError("Слишком много текста. Скопируйте только код активации.")
        return "".join(text.removeprefix("\ufeff").split())

    def _paste_premium(self, activate=False):
        try:
            code = self._normalize_premium_code(self.clipboard_get())
        except tk.TclError:
            self.license_feedback_var.set("В буфере нет текста. Сначала скопируйте код активации.")
            return "break"
        except ValueError as exc:
            self.license_feedback_var.set(str(exc))
            return "break"
        if not code.startswith("KREST1.") or code.count(".") != 2:
            self.license_feedback_var.set(
                "В буфере не найден полный код. Скопируйте строку, начинающуюся с KREST1."
            )
            return "break"
        # Replace the whole field, so a repeated paste never duplicates the code.
        self.license_code_var.set(code)
        self.license_code_entry.icursor("end")
        self.license_code_entry.xview_moveto(1)
        self.license_feedback_var.set("Код вставлен целиком. Нажмите «Активировать».")
        if activate:
            self._activate_premium()
        return "break"

    def _premium_shortcut(self, event):
        # Windows supplies the same virtual key even when the Russian layout is active.
        import os

        if (
            event.keysym.casefold() in ("v", "cyrillic_em")
            or os.name == "nt"
            and event.keycode == 86
        ):
            return self._paste_premium()

    def _premium_enter(self, event=None):
        self._activate_premium()
        return "break"

    def _premium_context_menu(self, event):
        menu = getattr(self, "_premium_menu", None)
        if menu is None or not menu.winfo_exists():
            from ui_design import contrasting_text

            menu = self._premium_menu = tk.Menu(
                self.license_code_entry,
                tearoff=False,
                bg=self.theme["card"],
                fg=self.theme["text"],
                activebackground=self.theme["accent"],
                activeforeground=contrasting_text(self.theme["accent"]),
            )
            menu.add_command(label="Вставить из буфера", command=self._paste_premium)
            menu.add_command(
                label="Вставить и активировать", command=lambda: self._paste_premium(activate=True)
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _activate_premium(self):
        draft = self.config_mgr.snapshot()
        try:
            draft["license"]["code"] = self._normalize_premium_code(self.license_code_var.get())
        except ValueError as exc:
            self.license_feedback_var.set(str(exc))
            return
        state = licensing.status(draft)
        if not state["premium"]:
            self.license_feedback_var.set(
                state["message"] if draft["license"]["code"] else "Введите код доступа"
            )
            return
        # Preserve all pending settings; never silently discard edits during activation.
        try:
            self.config_mgr.save_config(draft)
        except (ValueError, OSError) as exc:
            self.license_feedback_var.set(str(exc))
            return
        self.license_code_var.set("")
        self.license_feedback_var.set("")
        self._update_license_view()
        self.status_var.set(
            "Premium активирован. Ботов можно подключить на соответствующих страницах."
        )

    def _deactivate_premium(self):
        draft = self.config_mgr.snapshot()
        draft["license"]["code"] = ""
        if draft["selected_theme"] in PREMIUM_THEMES:
            draft["selected_theme"] = "Amethyst Modern"
        try:
            self.config_mgr.save_config(draft)
        except (ValueError, OSError) as exc:
            self._report_error(str(exc))
            return
        self.license_code_var.set("")
        self.license_feedback_var.set("")
        self._background(self.integration.enforce_license)
        if self.theme_var.get() in PREMIUM_THEMES:
            self.apply_theme("Amethyst Modern")
        self._update_license_view()
