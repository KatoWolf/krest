"""Release-time smoke check: own child kernel, authenticated RPC, command and shutdown."""

import time
import uuid


def check_kernel(config_path):
    from kernel_client import KernelClient
    from process_lock import ProcessLock
    from configuration import ConfigManager

    check_duty()

    # The packaged GUI must be able to export its standalone Discord adapter.
    from bot_project import connect_project
    from pathlib import Path
    from tempfile import TemporaryDirectory
    import ast

    with TemporaryDirectory(prefix="krest-bot-export-") as folder:
        script = Path(folder) / "bot.py"
        script.write_text("pass\n", encoding="utf-8")
        launcher = Path(connect_project(script))
        ast.parse(launcher.read_text(encoding="utf-8"))
        ast.parse(launcher.with_name("krest_bridge_adapter.py").read_text(encoding="utf-8"))
        if script.read_text(encoding="utf-8") != "pass\n":
            raise RuntimeError("Экспорт моста изменил исходник бота")

    if config_path.exists():
        raise RuntimeError("Проверка сборки требует новый временный профиль")
    cfg = ConfigManager(config_path)
    draft = cfg.snapshot()
    draft["owner_name"] = "Проверка сборки"
    draft["profile"]["onboarding_completed"] = True
    draft["warframe"]["enabled"] = False
    draft["warframe"]["overlay_enabled"] = False
    draft["app_settings"]["voice_auto_start"] = False
    cfg.save_config(draft)
    from performance import PerformancePolicy

    profile = {"performance": {"mode": "game", "game_processes": ""}}
    activity = PerformancePolicy(lambda: profile, config_path.parent / "activity-test.json")
    activity.refresh_activity()
    if activity.defer_files:
        raise RuntimeError("Игровой профиль без игры блокирует автосортировку")
    profile["performance"]["mode"] = "economy"
    activity.refresh_activity()
    if not activity.defer_files or "экономный" not in activity.file_pause_reason:
        raise RuntimeError("Экономный профиль потерял паузу фоновой сортировки")
    from hearing_practice import HearingPractice
    from skill_manager import SkillManager

    practice = HearingPractice(SkillManager(cfg.snapshot), cfg.snapshot)
    practice.call("start")
    for phrase in practice.PHRASES:
        if not practice.consume("крест " + phrase, {"confidence": 0.99}):
            raise RuntimeError("Проверка слуха не перехватила тестовую команду")
    if practice.snapshot()["passed"] != 3 or practice.snapshot()["active"]:
        raise RuntimeError("Проверка слуха неверно оценила контрольные фразы")
    lock = ProcessLock(config_path.parent / ".kernel.lock")
    if not lock.acquire():
        raise RuntimeError("Для проверки нужна отдельная папка конфигурации")
    lock.close()
    client = KernelClient(config_path).ensure_started(safe=True)
    try:
        if not client.call("ping")["safe_mode"]:
            raise RuntimeError("Ядро запустилось с неправильным режимом")
        if client.call("voice.indicator")["enabled"]:
            raise RuntimeError("Индикатор включился в безопасном режиме без микрофона")
        result = client.call("command.parse", text="Крест, сделай скриншот")
        if result["skill"] != "skills.windows.take_screenshot":
            raise RuntimeError("Команды не загрузились в отдельном ядре")
        for phrase, expected in (
            ("Крест, можешь пожалуйста сделать скриншот", "skills.windows.take_screenshot"),
            ("креси с ключи трек", "skills.media.next_track"),
            ("Крест, выключи микрофон в Дискорде", "skills.discord_voice.mute_microphone"),
            ("ребята сейчас приду крест переключи трек", "skills.media.next_track"),
            ("крест можешь сейчас пожалуйста переключить трек", "skills.media.next_track"),
            ("крест кто написал", "skills.workflows.missed_messages"),
            ("крест отчёт фарма", "skills.warframe.farm_report"),
            ("крест найди последний архив", "skills.assistant_tools.find_local_file"),
            ("крест ответь ему буду через час", "skills.assistant_tools.draft_reply"),
            ("крест подтверди добычу акси количество один", "skills.warframe.confirm_loot"),
            ("крест ээ крест переключи трек", "skills.media.next_track"),
            ("крест заметка для игры проверить реликвии", "skills.personal_tools.game_note"),
            ("крест итоги игровой сессии", "skills.personal_tools.game_report"),
            ("крест покажи историю буфера", "skills.personal_tools.clipboard_history"),
            ("крест статус дежурства", "skills.autonomy.duty_status"),
            ("крест останови дежурство", "skills.autonomy.duty_pause"),
            ("крест продолжи дежурство", "skills.autonomy.duty_resume"),
        ):
            parsed = client.call("command.parse", text=phrase)
            if parsed["skill"] != expected:
                raise RuntimeError("Разбор команды в отдельном ядре: " + phrase)
        if "dota_guide" in cfg.snapshot() or "gid" in client.call("snapshot"):
            raise RuntimeError("Удалённый Dota-гид остался в сборке")
        if not isinstance(client.call("catalog.names"), list):
            raise RuntimeError("Недоступен словарь приложений")
        duty = client.call("autonomy", action="snapshot")
        if duty["jobs"] or duty["activity"]["allowed"]:
            raise RuntimeError("Дежурство не должно запускаться в безопасном режиме")
        task = client.call("autonomy", action="enqueue", kind="profile_backup")
        client.call("autonomy", action="cancel", id=task["id"])
        duty = client.call("autonomy", action="snapshot")
        if duty["jobs"][0]["status"] != "cancelled":
            raise RuntimeError("RPC очереди дежурства не обновляет задачу")
        clips = client.call("personal_tools", section="clipboard", action="list")
        if clips["rows"] or clips["enabled"]:
            raise RuntimeError("История буфера нового профиля должна быть пустой и выключенной")
        sessions = client.call("personal_tools", section="sessions", action="list")
        if sessions["rows"]:
            raise RuntimeError("Игровой журнал нового профиля должен быть пустым")
        import os

        if os.name == "nt" and os.getenv("GITHUB_ACTIONS") == "true":
            # CI has no personal clipboard. Verify 64-bit handles and Unicode
            # from a worker, matching the real GUI/voice execution path.
            from concurrent.futures import ThreadPoolExecutor
            from windows_clipboard import read, write

            def clipboard_roundtrip():
                text = "Крест · проверка буфера № 42 🎮"
                write(text)
                if read()[1] != text:
                    raise RuntimeError("Windows не сохранила Unicode-текст буфера")
                client.call("personal_tools", section="clipboard", action="save")
                rows = client.call("personal_tools", section="clipboard", action="list")["rows"]
                if len(rows) != 1 or rows[0]["text"] != text:
                    raise RuntimeError("Ядро не прочитало буфер из другого процесса")
                client.call(
                    "personal_tools",
                    section="clipboard",
                    action="copy",
                    values={"id": rows[0]["id"]},
                )
                if read()[1] != text:
                    raise RuntimeError("Ядро не восстановило фрагмент в буфер Windows")
                client.call("personal_tools", section="clipboard", action="clear")

            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(clipboard_roundtrip).result(timeout=20)
        # Exercise new modules through the packaged child kernel, without a game or account.
        snapshot = client.call("companion", action="snapshot")
        if snapshot.get("learning") != []:
            raise RuntimeError("Новый профиль получил чужие исправления речи")
        client.call(
            "companion",
            action="draft.create",
            text="создай сценарий Проверка из действий включи не беспокоить затем выключи не беспокоить",
        )
        client.call("companion", action="draft.save")
        if len(cfg.snapshot()["scenarios"].get("Проверка", [])) != 2:
            raise RuntimeError("Голосовой черновик не сохранился в конфигурации ядра")
        if client.call("terminal_jobs", chat_id=123):
            raise RuntimeError("Новый пульт получил постороннюю историю")
        key = uuid.uuid4().hex
        client.call("submit", text="таймер на две минуты и время", request_key=key)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = next((j for j in client.call("jobs") if j["request_key"] == key), None)
            if job and job["status"] == "completed":
                break
            if job and job["status"] == "failed":
                raise RuntimeError(job["result"])
            time.sleep(0.1)
        else:
            raise RuntimeError("Ядро не выполнило цепочку команд")
        if len(client.call("snapshot")["state"]["active_timers"]) != 1:
            raise RuntimeError("Цепочка не создала таймер")
        parsed = client.call("command.parse", text="время и таймер на две минуты")
        if parsed["skill"] != "sequence" or len(parsed.get("steps", [])) != 2:
            raise RuntimeError("Разбор цепочки недоступен в сборке")
        key = uuid.uuid4().hex
        client.call("submit", text="верни как было", request_key=key)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = next((j for j in client.call("jobs") if j["request_key"] == key), None)
            if job and job["status"] == "completed":
                if client.call("snapshot")["state"]["active_timers"]:
                    raise RuntimeError("Отмена не удалила созданный таймер")
                if client.call("overlay")["feedback"].get("stage") != "success":
                    raise RuntimeError("Индикатор не получил результат команды")
                return
            if job and job["status"] == "failed":
                raise RuntimeError(job["result"])
            time.sleep(0.1)
        raise RuntimeError("Ядро не выполнило отмену последнего действия")
    finally:
        client.call("shutdown")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if lock.acquire():
                lock.close()
                break
            time.sleep(0.1)


def check_models(settings):
    """Test compiled model loaders and synthesis, without requiring CI audio hardware."""
    from voice_models import discover_models

    # Never download here: the test must exercise what a recipient gets in the ZIP.
    paths = discover_models(settings)
    if not all(paths.values()):
        raise RuntimeError("Сборка не подготовила обе голосовые модели")
    import config

    config.MODEL_VOSK_PATH, config.MODEL_SILERO_PATH = paths["vosk_path"], paths["silero_path"]
    import voice_engine as engine

    if engine.torch is not None or engine.rec is not None:
        raise RuntimeError("Импорт движка начал загрузку моделей до подключения микрофона")
    started = time.monotonic()
    engine.initialize_recognition(paths["vosk_path"])
    if engine.rec is None or engine.torch is not None:
        raise RuntimeError("Распознавание не запустилось независимо от Silero")
    print(f"Vosk ready before TTS: {time.monotonic() - started:.2f}s", flush=True)
    engine.initialize_tts(paths["silero_path"])
    if engine.rec is None or engine.model_tts is None:
        raise RuntimeError("Сборка не загрузила модели: " + str(engine.metrics))
    engine.rec.AcceptWaveform(bytes(3200))
    import os

    if os.name == "nt":
        import shutil
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="Крест-проверка-") as folder:
            target = Path(folder) / "модель"
            shutil.copytree(paths["vosk_path"], target)
            engine.initialize_recognition(str(target))
            if engine.rec is None:
                raise RuntimeError(
                    "Vosk не открыла путь с русскими буквами: "
                    + engine.metrics.get("model_error", "")
                )
            engine.rec.AcceptWaveform(bytes(3200))
            # Release native file handles before TemporaryDirectory cleanup.
            engine.rec = engine.model_vosk = None
        engine.initialize_recognition(paths["vosk_path"])
    import numpy as np

    samples = engine.synthesize(engine.prepare_text("Крест готова. Громкость 80%. Время 12:05."))
    if samples.size < 240 or not np.isfinite(samples).all() or not np.any(samples):
        raise RuntimeError("Синтез в сборке не вернул корректный звук")
    print("Vosk (включая русский путь) и синтез Silero проверены без микрофона и динамиков.")
    from speech_check import check

    check(engine)


def check_telegram_worker():
    """Exercise the GUI's first import from a fresh worker, without logging in."""
    from concurrent.futures import ThreadPoolExecutor
    import importlib

    def load():
        bridge = importlib.import_module("tg_bridge")
        if not bridge.PYROGRAM_AVAILABLE:
            raise RuntimeError("Pyrogram недоступен для авторизации Telegram")

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="TelegramAuthCheck") as pool:
        pool.submit(load).result(timeout=20)
    print("Telegram: импорт из фонового потока выполнен без подключения к аккаунту.", flush=True)


def check_dependencies():
    """Verify native speech/media libraries without downloads or hardware access."""
    import importlib
    import os

    # Must run before anything imports Pyrogram on the main thread: that would
    # hide the missing-event-loop error that occurs after pressing Get code.
    check_telegram_worker()
    modules = [
        "torch",
        "vosk",
        "sounddevice",
        "numpy",
        "num2words",
        "aiohttp",
        "discord",
        "telebot",
    ]
    if os.name == "nt":
        modules += [
            "winsdk.windows.media.control",
            "winsdk.windows.media.ocr",
            "pycaw.pycaw",
        ]
    for module in modules:
        print("Проверка библиотеки: " + module, flush=True)
        importlib.import_module(module)
    import sounddevice
    from audio_devices import input_candidates

    print(
        "PortAudio: устройств ввода найдено " + str(len(input_candidates(sounddevice))), flush=True
    )
    if os.name == "nt":
        from concurrent.futures import ThreadPoolExecutor
        from autonomy_activity import windows_input, windows_busy

        with ThreadPoolExecutor(max_workers=1) as worker:
            idle = worker.submit(windows_input).result(timeout=10)
            busy = worker.submit(windows_busy, {os.getpid()}).result(timeout=15)
        if idle < 0 or not isinstance(busy, str):
            raise RuntimeError("Проверка AFK не вернула состояние Windows")
        print("AFK: native input, media, audio/capture and fullscreen probes verified.", flush=True)
        from audio_scheduling import recognition_priority, status

        with recognition_priority() as scheduling:
            print("Планировщик распознавания: " + scheduling, flush=True)
        if status() != "standard":
            raise RuntimeError("Приоритет потока не освобождён после проверки")
        from concurrent.futures import ThreadPoolExecutor
        from media_desktop import request

        print("Проверка изолированного UI Automation из фоновых потоков…", flush=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: request("probe", {"title": "Музыка • проверка"}), range(2))
            )
        if any(
            not isinstance(r.get("windows"), int) or r.get("echo") != "Музыка • проверка"
            for r in results
        ):
            raise RuntimeError("Помощник плеера не вернул результат UI Automation")
        from desktop_transport import close

        close()
        print("UI Automation: повторные запросы к помощнику проверены без нажатий.", flush=True)
        check_menu_surface()
    import torch

    if torch.tensor([1.0, 2.0]).sum().item() != 3:
        raise RuntimeError("PyTorch не выполнил операцию на CPU")
    print("Голосовые и сервисные библиотеки загружены; CPU PyTorch проверен.")
    check_extension_process()


def check_extension_process():
    """Run a real trusted demo ZIP in a child EXE/Python process."""
    import tempfile
    from pathlib import Path
    from configuration import ConfigManager
    from skill_manager import SkillManager
    from app_state import AssistantState
    from extension_packages import save_template, inspect_package
    from app_paths import compiled

    if compiled() and __import__("os").name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetConsoleWindow():
            raise RuntimeError("Оконная сборка создала консоль")
    with tempfile.TemporaryDirectory(prefix="krest-package-check-") as folder:
        root = Path(folder)
        config = ConfigManager(root / "config.json")
        manager = SkillManager(config.snapshot, plugin_dir=root / "imported_skills")
        path = root / "demo.zip"
        save_template(path)
        preview = inspect_package(path)
        installed = manager.packages.install(path, preview["sha256"])
        state = AssistantState(root / "state.json")
        state.config_getter = config.snapshot
        result = manager.execute("привет от расширения", state)
        if not result.success or result.text != "Расширение подключено!":
            raise RuntimeError("Пакет не выполнился в отдельном процессе: " + result.text)
        manager.packages.change(installed["id"][8:], "remove")
    print("Пакет ZIP: отдельный процесс, Unicode и результат проверены.", flush=True)


def check_menu_surface():
    """Exercise native Tk layout and wheel bounds in the packaged Windows app."""
    import tkinter as tk
    from tkinter import ttk
    from ui_scroll import ScrollViewport
    from ui_design import apply_style
    from themes import THEMES

    root = tk.Tk()
    try:
        root.geometry("480x320")
        apply_style(root, THEMES["Graphite"])
        pane = ScrollViewport(root, bg=THEMES["Graphite"]["bg"])
        pane.pack(fill="both", expand=True)
        for index in range(20):
            ttk.Button(pane.content, text=f"Кнопка {index}").pack(fill="x", pady=4)
        root.update()
        pane.yview_scroll(137, "units")
        root.update()
        if pane.content.winfo_y() != -137:
            raise RuntimeError("Страница не прокручивается на целое число пикселей")
        pane.yview_moveto(1)
        root.update()
        if pane.yview()[1] != 1:
            raise RuntimeError("Прокрутка вышла за пределы страницы")
        pane.yview_moveto(0)
        root.update()
        if pane.content.winfo_y() != 0:
            raise RuntimeError("Не удалось вернуть страницу в начало")
        from relic_indicator_window import LayeredWindow
        from relic_indicator_visual import animation_frame
        from ctypes import wintypes
        import ctypes

        overlay = LayeredWindow(root)
        try:
            overlay.u.GetForegroundWindow.restype = wintypes.HWND
            before = overlay.u.GetForegroundWindow()
            overlay._position()
            overlay._present(animation_frame("idle", 0))
            root.update()
            if overlay.u.GetForegroundWindow() != before:
                raise RuntimeError("Индикатор забрал фокус")
            flags = 0x80000 | 0x20 | 0x80 | 0x8000000
            if overlay.get_style(overlay.hwnd, -20) & flags != flags:
                raise RuntimeError("Индикатор не настроил прозрачность и пропуск кликов")
            overlay.hide()
            overlay.u.IsWindowVisible.argtypes = [wintypes.HWND]
            overlay.u.IsWindowVisible.restype = ctypes.c_int
            if overlay.u.IsWindowVisible(overlay.hwnd):
                raise RuntimeError("Индикатор не скрылся")
            print(
                "Relic V2: layered alpha, no activation, click-through flags and hide checked.",
                flush=True,
            )
        finally:
            overlay.close()
        print("Меню: шрифт, кнопки и границы прокрутки проверены.", flush=True)
    finally:
        root.destroy()


def check_duty():
    """Exercise persistence, AFK gating, version restoration and a return mid-copy."""
    import os
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from configuration import ConfigManager
    from autonomy import AutonomousDuty
    from autonomy_store import DutyStore
    from autonomy_files import Paused, CHUNK

    with tempfile.TemporaryDirectory(prefix="krest-duty-check-") as temp:
        root = Path(temp)
        config = ConfigManager(root / "config.json")
        options = config.snapshot()
        options["autonomy"].update(profile_backup=False, health_checks=False)
        config.save_config(options)
        permission = [False]
        gate = SimpleNamespace(
            permitted=lambda: permission[0],
            snapshot=lambda: {"allowed": permission[0], "reason": "smoke", "idle": 600},
        )
        rt = SimpleNamespace(
            settings=config.snapshot, config_manager=config, publish_notice=lambda *a, **kw: None
        )
        duty = AutonomousDuty(rt, root, gate=gate)
        source = root / "original.txt"
        source.write_bytes(b"original content")
        os.utime(source, (time.time() - 300, time.time() - 300))
        task = duty.call(
            "enqueue",
            kind="backup",
            values={"source": str(source), "destination": str(root / "versions")},
        )["id"]
        if duty.run_once() or (root / "versions").exists():
            raise RuntimeError("AFK worker touched files while the user was active")
        permission[0] = True
        duty.run_once()
        record = DutyStore(duty.store.path).get(task)
        versions = duty.store.versions(source)
        if record["status"] != "completed" or len(versions) != 1:
            raise RuntimeError("Backup did not persist its verified result: " + record["result"])
        restore = duty.call("enqueue", kind="restore", values={"version_id": versions[0]["id"]})[
            "id"
        ]
        duty.run_once()
        if (
            duty.store.get(restore)["status"] != "completed"
            or source.read_bytes() != b"original content"
        ):
            raise RuntimeError("Version restoration did not preserve the current original")
        big = root / "large.bin"
        big.write_bytes(b"x" * (2 * CHUNK))
        os.utime(big, (time.time() - 300, time.time() - 300))
        target = root / "copy.bin"
        ident = duty.store.enqueue("copy", "copy", {})
        count = [0]

        def guard():
            count[0] += 1
            if count[0] >= 3:
                raise Paused("user returned")

        duty.files.guard = guard
        try:
            duty.files.copy_file(big, target, duty.store.get(ident))
        except Paused:
            pass
        else:
            raise RuntimeError("Copy ignored cancellation")
        if target.exists():
            raise RuntimeError("Partial copy was published as a completed file")
        duty.files.guard = duty.guard
        duty.files.copy_file(big, target, duty.store.get(ident))
        if target.read_bytes() != big.read_bytes():
            raise RuntimeError("Interrupted copy did not resume correctly")
    print("AFK queue, verified backup, restore and interrupted copy passed.", flush=True)
