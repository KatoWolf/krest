"""Voice controls without importing heavy speech libraries on GUI startup."""

import tkinter as tk
from tkinter import ttk
import voice
from microphone_test import MicrophoneTest
from speech_tools import split_wake, normalize_command


class VoicePages:
    def init_voice_page(self):
        self.microphone_test = MicrophoneTest()
        self.audio_test_busy = False
        self.heard_var = tk.StringVar(value="Пока ничего не распознано")
        self.understood_var = tk.StringVar(value="—")
        self.recognition_status = tk.StringVar(value="Голос выключен")
        self.correction_var = tk.StringVar()
        self.mic_test_status = tk.StringVar(
            value="Запишите короткую фразу или измерьте фон в тишине."
        )
        self.pronunciations = self.config_mgr.snapshot().get("pronunciations", {})
        self.pron_word = tk.StringVar()
        self.pron_reading = tk.StringVar()

    def build_voice_tab(self):
        info = self.card(
            self.page_bodies["voice"],
            "От голоса до действия",
            "Vosk — время вычислений распознавателя; пауза — ожидание конца фразы. Очередь и выполнение измеряются отдельно, озвучка в них не входит.",
        )
        self.voice_trace = self.label(
            info, "После команды появится измерение задержки", wraplength=680
        )
        self.voice_trace.pack(anchor="w", pady=8)
        self.label(
            info,
            "Можно сказать: «Крест, переключи трек и поставь таймер на две минуты». "
            "До пяти действий по порядку; при ошибке цепочка остановится. "
            "«Крест, верни как было» — отменить последнее поддерживаемое изменение. "
            "В игровом значке: зелёный — услышала, голубой — распознаёт, "
            "жёлтый — выполняет, ярко-зелёный — готово, красный — ошибка.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=8)
        practice = self.card(
            self.page_bodies["voice"],
            "Проверка слуха за три фразы",
            "Проверьте настоящее распознавание на вашем микрофоне. На 90 секунд голосовые действия "
            "приостанавливаются: тестовые фразы ничего не переключают. Звук не записывается.",
        )
        row = tk.Frame(practice, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        for caption, action in (("Начать проверку", "start"), ("Закончить проверку", "stop")):
            ttk.Button(row, text=caption, command=lambda a=action: self._hearing_action(a)).pack(
                side="left", padx=(0, 8)
            )
        self.hearing_result = self.label(practice, "Проверка ещё не запускалась", wraplength=680)
        self.hearing_result.pack(fill="x", pady=8)
        card = self.card(
            self.page_bodies["voice"],
            "Голос Крест",
            "Распознавание и озвучка работают локально. Ctrl+Alt+V — слушать команду без обращения даже в игре и трее. В окне Крест: F8 — слушать; Escape — прервать ответ.",
        )
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        for title, fn in (
            ("Включить", self._start_voice),
            ("Выключить", self.runtime.stop_voice),
            ("Слушать команду · F8", self._arm_voice),
            ("Прервать ответ", voice.stop_speaking),
        ):
            ttk.Button(row, text=title, command=fn).pack(side="left", padx=(0, 5))
        ttk.Button(
            card, text="Играю в наушниках · слышать во время ответа", command=self._headphone_preset
        ).pack(anchor="w", pady=(4, 10))
        ttk.Checkbutton(
            card,
            text="Включать голос при запуске Крест",
            variable=self.settings_vars["voice_auto_start"],
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            card,
            text="Автоматически скачивать отсутствующие модели с официальных серверов",
            variable=self.settings_vars["voice_auto_models"],
        ).pack(anchor="w", pady=4)
        self.label(card, textvariable=self.voice_var, muted=True).pack(anchor="w")
        self.mic_meter = ttk.Progressbar(card, maximum=100)
        self.mic_meter.pack(fill="x", pady=8)
        self.mic_level_label = self.label(card, "Микрофон выключен", muted=True, wraplength=680)
        self.mic_level_label.pack(anchor="w")
        ttk.Checkbutton(
            card,
            text="Автоматически подстраивать чувствительность к фону",
            variable=self.settings_vars["speech_auto_threshold"],
        ).pack(anchor="w", pady=4)
        tuning = self.advanced(card, "Точная настройка распознавания")
        ttk.Checkbutton(
            card,
            text="Выполнять команды сразу после распознанного конца речи",
            variable=self.settings_vars["voice_fast_commands"],
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            card,
            text="Автоматически определять наушники по устройству вывода",
            variable=self.settings_vars["voice_headphones_auto"],
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            card,
            text="Я в наушниках: слышать команды во время ответа",
            variable=self.settings_vars["voice_headphones"],
        ).pack(anchor="w", pady=4)
        for key, label in (
            ("speech_pause_ms", "Пауза в конце, мс"),
            ("speech_threshold", "Порог речи, 20–5000"),
            ("speech_confidence", "Порог уверенности, %"),
            ("tts_threads", "Потоки CPU, 1–4"),
        ):
            self.field(tuning, label, self.settings_vars[key])
        ttk.Checkbutton(
            card,
            text="Сохранять звуковой эффект Крест",
            variable=self.settings_vars["voice_effect"],
        ).pack(anchor="w", pady=4)
        ttk.Checkbutton(
            card,
            text="Короткая озвучка длинных ответов",
            variable=self.settings_vars["short_voice_answers"],
        ).pack(anchor="w", pady=4)
        self.label(
            card,
            "Пауза, порог речи и уверенность применяются после сохранения. Потоки CPU — после перезапуска голоса. Число уверенности — оценка распознавателя, а не гарантия правильности.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=8)
        card = self.card(self.page_bodies["voice"], "Что услышала и поняла")
        self.label(card, textvariable=self.heard_var, wraplength=680).pack(anchor="w", pady=4)
        self.label(card, textvariable=self.understood_var, wraplength=680).pack(anchor="w", pady=4)
        self.label(card, textvariable=self.recognition_status, muted=True, wraplength=680).pack(
            anchor="w", pady=4
        )
        correction = self.advanced(card, "Исправить распознанную фразу")
        self.field(correction, "Правильная команда", self.correction_var)
        ttk.Button(
            correction, text="Запомнить исправление", command=self._save_speech_correction
        ).pack(anchor="w", pady=5)
        self.label(
            card,
            "Исправление сохраняется как ваша фраза и ничего не выполняет. Его можно удалить в «Мои команды».",
            muted=True,
            wraplength=680,
        ).pack(anchor="w")
        card = self.card(
            self.page_bodies["voice"],
            "Проверка микрофона",
            "Крест выбирает системный микрофон и подстраивается к фону. Для проверки голос временно приостанавливается и затем возвращается сам. Запись остаётся только в памяти.",
        )
        self.label(
            card,
            "Оставьте поле пустым для автоматического выбора. При отключении устройства Крест попробует другие доступные входы.",
            muted=True,
            wraplength=680,
        ).pack(anchor="w", pady=6)
        ttk.Button(
            card,
            text="Автоматически подключить микрофон",
            style="Accent.TButton",
            command=self._auto_connect_microphone,
        ).pack(anchor="w", pady=4)
        ttk.Button(
            card,
            text="Разрешение микрофона в Windows",
            command=self._microphone_permissions,
        ).pack(anchor="w", pady=4)
        advanced = self.advanced(card, "Выбрать конкретный микрофон")
        self.field(advanced, "Имя / индекс микрофона", self.settings_vars["microphone"])
        ttk.Button(
            card,
            text="Настроить порог автоматически · 5 секунд тишины",
            style="Accent.TButton",
            command=lambda: self._microphone_action("auto"),
        ).pack(anchor="w", pady=8)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x", pady=8)
        for title, fn in (
            ("Записать 5 секунд", lambda: self._microphone_action("record")),
            ("Измерить фон", lambda: self._microphone_action("calibrate")),
            ("Прослушать запись", lambda: self._microphone_action("play")),
        ):
            ttk.Button(row, text=title, command=fn).pack(side="left", padx=(0, 5))
        ttk.Button(card, text="Применить измеренный порог", command=self._apply_mic_threshold).pack(
            anchor="w", pady=6
        )
        self.label(card, textvariable=self.mic_test_status, wraplength=680).pack(anchor="w")
        card = self.card(
            self.page_bodies["voice"],
            "Произношение слов",
            "Например: Discord → диск+орд. Плюс ставится перед ударной гласной. Голос и модель не меняются.",
        )
        self.field(card, "Слово / название", self.pron_word)
        self.field(card, "Как произносить", self.pron_reading)
        self.pron_tree = ttk.Treeview(card, columns=("word", "reading"), show="headings", height=5)
        self.pron_tree.heading("word", text="Слово")
        self.pron_tree.heading("reading", text="Произношение")
        self.pron_tree.pack(fill="x", pady=8)
        row = tk.Frame(card, bg=self.theme["card"])
        row.pack(fill="x")
        ttk.Button(row, text="Добавить / обновить", command=self._add_pronunciation).pack(
            side="left"
        )
        ttk.Button(row, text="Удалить", command=self._remove_pronunciation).pack(
            side="left", padx=5
        )
        ttk.Button(row, text="Произнести слово", command=self._preview_pronunciation).pack(
            side="left"
        )
        self._refresh_pronunciations()
        self._build_learning_controls(self.page_bodies["voice"])

    def _start_voice(self):
        if self.audio_test_busy:
            self._report_error("Дождитесь завершения проверки микрофона")
            return
        self.voice_var.set("Подключаю голос: ищу модели и микрофон…")
        self._background(self.runtime.start_voice)

    def _arm_voice(self):
        if self.audio_test_busy:
            self._report_error("Дождитесь завершения проверки микрофона")
        else:
            self.runtime.arm_voice()
        return "break"

    def _auto_connect_microphone(self):
        if self.audio_test_busy:
            return
        changes = {"microphone": "", "speech_auto_threshold": True, "voice_auto_start": True}
        cfg = self.config_mgr.snapshot()
        cfg["app_settings"].update(changes)
        try:
            self.config_mgr.save_config(cfg)
        except (OSError, ValueError) as exc:
            self._report_error("Не удалось сохранить настройки микрофона: " + str(exc))
            return
        dirty = self.dirty
        for key, value in changes.items():
            self.settings_vars[key].set(value)
        self.dirty = dirty
        self.save_button.configure(text="Сохранить •" if dirty else "Сохранить")
        self._microphone_action("reconnect")

    def _headphone_preset(self):
        changes = {
            "voice_headphones": True,
            "voice_fast_commands": True,
            "speech_auto_threshold": True,
            "voice_auto_start": True,
        }
        cfg = self.config_mgr.snapshot()
        cfg["app_settings"].update(changes)
        try:
            self.config_mgr.save_config(cfg)
        except (OSError, ValueError) as exc:
            self._report_error(str(exc))
            return
        dirty = self.dirty
        for key, value in changes.items():
            self.settings_vars[key].set(value)
        self.dirty = dirty
        self.save_button.configure(text="Сохранить •" if dirty else "Сохранить")
        self.status_var.set(
            "Режим наушников сохранён: Крест слышит команды и во время своего ответа"
        )
        self.runtime.start_voice()

    def _microphone_permissions(self):
        import os

        if os.name == "nt":
            self._background(lambda: os.startfile("ms-settings:privacy-microphone"))
        else:
            self.mic_test_status.set("Разрешите доступ к микрофону в настройках вашей системы.")

    def _microphone_action(self, action):
        if self.audio_test_busy:
            return
        resume = bool(self.runtime._voice_thread and self.runtime._voice_thread.is_alive())
        device = self.settings_vars["microphone"].get()
        previous_threshold = self.settings_vars["speech_threshold"].get()
        self.audio_test_busy = True
        self.mic_test_status.set(
            "Пять секунд тишины: измеряю фон…"
            if action in ("calibrate", "auto")
            else "Идёт проверка…"
        )

        def work():
            try:
                if resume:
                    self.runtime.stop_voice()
                    # RemoteRuntime proxies have no join; wait on their refreshed state.
                    import time

                    deadline = time.monotonic() + 30
                    while self.runtime._voice_thread and self.runtime._voice_thread.is_alive():
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "Голос ещё освобождает микрофон. Повторите проверку."
                            )
                        time.sleep(0.05)
                if action == "reconnect":
                    result = "Автовыбор включён. Подключаю микрофон; его состояние показано выше."
                elif action == "play":
                    self.microphone_test.playback()
                    result = "Воспроизведение завершено"
                else:
                    result = self.microphone_test.record(
                        device, calibrate=action in ("calibrate", "auto")
                    )
                if action == "auto":
                    value = self.microphone_test.noise_threshold
                    self.post_ui(
                        lambda: self._save_auto_threshold(value, device, previous_threshold)
                    )
                else:
                    self.post_ui(lambda: self.mic_test_status.set(result))
            except Exception as exc:
                message = "Проверка не удалась: " + str(exc)
                self.post_ui(lambda: self.mic_test_status.set(message))
                raise
            finally:
                if (resume or action == "reconnect") and not self.closing:
                    self.runtime.start_voice()
                self.post_ui(lambda: setattr(self, "audio_test_busy", False))

        self._background(work)

    def _save_auto_threshold(self, value, device, previous_threshold):
        if value is None:
            self.mic_test_status.set("Не удалось измерить фон. Настройки сохранены прежними.")
            return
        if (
            self.settings_vars["microphone"].get() != device
            or self.settings_vars["speech_threshold"].get() != previous_threshold
        ):
            self.mic_test_status.set(
                "Во время проверки вы изменили настройки. Автоматический порог не применён."
            )
            return
        cfg = self.config_mgr.snapshot()
        cfg["app_settings"]["speech_threshold"] = value
        cfg["app_settings"]["microphone"] = device
        try:
            self.config_mgr.save_config(cfg)
        except (OSError, ValueError) as exc:
            self.mic_test_status.set("Не удалось сохранить порог: " + str(exc))
            return
        dirty = self.dirty
        self.settings_vars["speech_threshold"].set(value)
        self.dirty = dirty
        self.save_button.configure(text="Сохранить •" if dirty else "Сохранить")
        self.mic_test_status.set(
            f"Порог {value} сохранён. Включите голос и проверьте обычную команду."
        )

    def _apply_mic_threshold(self):
        value = self.microphone_test.noise_threshold
        if value is None:
            self._report_error("Сначала измерьте фон")
        else:
            self.settings_vars["speech_threshold"].set(value)
            self.mic_test_status.set(
                "Порог подставлен. Сохраните настройки и проверьте обычную речь."
            )

    def _show_recognition(self, value):
        self.last_recognition = value
        self.heard_var.set("Услышала: " + value["heard"])
        self.understood_var.set("Поняла: " + (value["command"] or "Обращение"))
        score = value.get("confidence")
        self.recognition_status.set(
            value["status"] + (f" · Оценка Vosk: {score:.0%}" if score is not None else "")
        )

    def _save_speech_correction(self):
        latest = getattr(self, "last_recognition", {})
        heard = latest.get("heard", "")
        wake = self.config_mgr.snapshot()["app_settings"]["wake_word"]
        _, phrase = split_wake(heard, wake)
        key = phrase.casefold().strip(" .!?,")
        target = normalize_command(self.correction_var.get().strip(), self.aliases)
        if not key or len(key) > 100 or not target or self.runtime.manager.match(target) is None:
            self._report_error("Нужны распознанная фраза (до 100 символов) и существующая команда")
            return
        if key not in self.aliases and len(self.aliases) >= 100:
            self._report_error("В словаре уже 100 фраз")
            return
        self.aliases[key] = target
        self._mark_dirty()
        self._refresh_aliases()
        if self.save_all_settings():
            self.recognition_status.set(
                "Исправление сохранено. Повторите фразу — команда сейчас не выполнялась."
            )

    def _refresh_pronunciations(self):
        self.pron_tree.delete(*self.pron_tree.get_children())
        for key, value in self.pronunciations.items():
            self.pron_tree.insert("", "end", iid=key, values=(key, value))

    def _add_pronunciation(self):
        word, reading = self.pron_word.get().strip(), self.pron_reading.get().strip()
        if (
            not word
            or not reading
            or len(word) > 80
            or len(reading) > 160
            or (word not in self.pronunciations and len(self.pronunciations) >= 100)
        ):
            self._report_error(
                "Укажите слово до 80 и произношение до 160 символов; максимум 100 записей"
            )
            return
        self.pronunciations[word] = reading
        self._mark_dirty()
        self._refresh_pronunciations()

    def _remove_pronunciation(self):
        for word in self.pron_tree.selection():
            self.pronunciations.pop(word, None)
        self._mark_dirty()
        self._refresh_pronunciations()

    def _preview_pronunciation(self):
        if not voice.is_enabled():
            self._report_error("Для предпрослушивания включите голос")
            return
        if self.save_all_settings():
            voice.speak(self.pron_word.get())

    def _hearing_action(self, action):
        def work():
            if action == "start":
                self.runtime.start_voice()
            return self.runtime.hearing.call(action)

        self._background(work, lambda result: self._refresh_hearing(result))

    def _refresh_hearing(self, snapshot=None):
        widget = getattr(self, "hearing_result", None)
        if widget is None or not widget.winfo_exists() or not widget.winfo_ismapped():
            return
        data = self.runtime.hearing.snapshot() if snapshot is None else snapshot
        text = data.get("message", "")
        if data.get("active"):
            text += f"\n«{data['expected']}» · Осталось {data['remaining']} с"
        for row in data.get("rows", [])[-6:]:
            text += "\n" + ("Верно" if row["passed"] else "Повторить") + ": «" + row["heard"] + "»"
            if row.get("pause_ms") is not None:
                text += f" · пауза {row['pause_ms']} мс"
            if row.get("audio_queue_ms") is not None:
                text += f" · буфер {row['audio_queue_ms']} мс"
        if widget.cget("text") != text:
            widget.configure(text=text)

    def _update_voice_meter(self):
        data = voice.telemetry()
        if self.runtime.voice_status and not self.audio_test_busy:
            from game_overlay import LABELS

            feedback = self.runtime.voice_feedback
            stage = feedback.get("stage")
            self.voice_var.set(
                LABELS[stage] + (" · " + feedback["text"] if feedback.get("text") else "")
                if stage in LABELS and stage != "idle"
                else self.runtime.voice_status
            )
        rms = self.microphone_test.level if self.audio_test_busy else data.get("rms", 0)
        peak = self.microphone_test.peak if self.audio_test_busy else data.get("peak", 0)
        for key in ("mic_meter", "home_meter"):
            widget = getattr(self, key, None)
            if widget is not None and widget.winfo_exists() and widget.winfo_ismapped():
                widget["value"] = min(100, peak / 32768 * 100)
        text = f"Уровень: {rms:.0f} · Пик: {peak / 32768:.0%}"
        if data.get("threshold"):
            text += f" · Порог речи: {data['threshold']}"
        if data.get("device_name"):
            text = (
                data["device_name"]
                + (" · подключён" if data.get("connected") else " · ожидает подключения")
                + "\n"
                + text
            )
        if data.get("mic_error"):
            text += "\n" + data["mic_error"][:240]
        if data.get("mic_hint"):
            text += "\n" + data["mic_hint"][:240]
        if data.get("recovery"):
            text += "\n" + data["recovery"]
        if data.get("headphones"):
            text += "\nНаушники: слышу команды во время ответа"
        if data.get("scheduler") == "windows_audio":
            text += " · Приоритет аудио Windows"
        if peak >= 32700:
            text += " · Перегрузка: уменьшите усиление"
        if data.get("dropped"):
            text += f" · Пропущено блоков: {data['dropped']}"
        if data.get("partial"):
            text += "\nСлышу: " + data["partial"][:180]
        if data.get("connected") and data.get("tts_ready") is False:
            text += "\nСлышу вас. Озвучка недоступна — ответы пока видны в меню."
        if data.get("tts_ready") is False and data.get("tts_progress"):
            text += "\n" + data["tts_progress"][:300]
        if data.get("tts_error"):
            text += "\n" + data["tts_error"][:300]
        for key in ("mic_level_label", "home_mic_detail"):
            widget = getattr(self, key, None)
            if widget is not None and widget.winfo_exists() and widget.winfo_ismapped():
                value = (
                    text
                    if data or self.audio_test_busy
                    else (
                        "Микрофон и модели подключаются автоматически. Ход подготовки показан выше."
                    )
                )
                if widget.cget("text") != value:
                    widget.configure(text=value)
