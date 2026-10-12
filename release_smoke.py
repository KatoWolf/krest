"""Release-time smoke check: own child kernel, authenticated RPC, command and shutdown."""

import time
import uuid


def check_kernel(config_path):
    from kernel_client import KernelClient
    from process_lock import ProcessLock
    from configuration import ConfigManager

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
    lock = ProcessLock(config_path.parent / ".kernel.lock")
    if not lock.acquire():
        raise RuntimeError("Для проверки нужна отдельная папка конфигурации")
    lock.close()
    client = KernelClient(config_path).ensure_started(safe=True)
    try:
        if not client.call("ping")["safe_mode"]:
            raise RuntimeError("Ядро запустилось с неправильным режимом")
        result = client.call("command.parse", text="Крест, сделай скриншот")
        if result["skill"] != "skills.windows.take_screenshot":
            raise RuntimeError("Команды не загрузились в отдельном ядре")
        for phrase, expected in (
            ("Крест, можешь пожалуйста сделать скриншот", "skills.windows.take_screenshot"),
            ("креси с ключи трек", "skills.media.next_track"),
            ("Крест, выключи микрофон в Дискорде", "skills.discord_voice.mute_microphone"),
        ):
            parsed = client.call("command.parse", text=phrase)
            if parsed["skill"] != expected:
                raise RuntimeError("Разбор команды в отдельном ядре: " + phrase)
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
        client.call("submit", text="который час", request_key=key)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = next((j for j in client.call("jobs") if j["request_key"] == key), None)
            if job and job["status"] == "completed":
                return
            if job and job["status"] == "failed":
                raise RuntimeError(job["result"])
            time.sleep(0.1)
        raise RuntimeError("Ядро не выполнило команду времени")
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
    import torch

    if torch.tensor([1.0, 2.0]).sum().item() != 3:
        raise RuntimeError("PyTorch не выполнил операцию на CPU")
    print("Голосовые и сервисные библиотеки загружены; CPU PyTorch проверен.")
