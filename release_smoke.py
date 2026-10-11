"""Release-time smoke check: own child kernel, authenticated RPC, command and shutdown."""

import time
import uuid


def check_kernel(config_path):
    from kernel_client import KernelClient
    from process_lock import ProcessLock
    from configuration import ConfigManager

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
    from model_setup import prepare

    paths = prepare(settings, progress=print)
    if not all(paths.values()):
        raise RuntimeError("Сборка не подготовила обе голосовые модели")
    import config

    config.MODEL_VOSK_PATH, config.MODEL_SILERO_PATH = paths["vosk_path"], paths["silero_path"]
    import voice_engine as engine

    if engine.rec is None or engine.model_tts is None:
        raise RuntimeError("Сборка не загрузила модели: " + str(engine.metrics))
    engine.rec.AcceptWaveform(bytes(3200))
    import torch
    import numpy as np

    with torch.no_grad():
        try:
            audio = engine.model_tts.apply_tts(
                text="Проверка голоса Крест.", speaker="xenia", sample_rate=24000
            )
        except Exception:
            audio = engine.model_tts.apply_tts(
                text="Проверка голоса Крест.", speaker="kseniya", sample_rate=24000
            )
    samples = audio.numpy()
    if samples.size < 240 or not np.isfinite(samples).all() or not np.any(samples):
        raise RuntimeError("Синтез в сборке не вернул корректный звук")
    print("Vosk и синтез Silero проверены без микрофона и динамиков.")


def check_dependencies():
    """Verify native speech/media libraries without downloads or hardware access."""
    import importlib
    import os

    modules = [
        "torch",
        "vosk",
        "sounddevice",
        "numpy",
        "num2words",
        "aiohttp",
        "discord",
        "telebot",
        "pyrogram",
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
        print("UI Automation: оба помощника завершились без нажатий.", flush=True)
    import torch

    if torch.tensor([1.0, 2.0]).sum().item() != 3:
        raise RuntimeError("PyTorch не выполнил операцию на CPU")
    print("Голосовые и сервисные библиотеки загружены; CPU PyTorch проверен.")
