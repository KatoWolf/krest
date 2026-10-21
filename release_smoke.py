"""Release-time smoke check: own child kernel, authenticated RPC, command and shutdown."""

import time
import uuid


def check_kernel(config_path):
    from kernel_client import KernelClient
    from process_lock import ProcessLock
    from configuration import ConfigManager

    check_duty()
    check_idle_protocol()
    check_idle_storage()
    check_runtime_efficiency()
    check_capture_efficiency()
    check_game_detection()
    check_expansion()

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

    with TemporaryDirectory(prefix="krest-idle-upgrade-") as folder:
        from storage import atomic_write_json

        path = Path(folder) / "config.json"
        atomic_write_json(path, {"autonomy": {"idle_minutes": 5}})
        upgraded = ConfigManager(path)
        data = upgraded.snapshot()
        if data["autonomy"]["idle_minutes"] != 20:
            raise RuntimeError("Старые стандартные пять минут не обновились")
        data["autonomy"]["idle_minutes"] = 15
        upgraded.save_config(data)
        if ConfigManager(path).snapshot()["autonomy"]["idle_minutes"] != 15:
            raise RuntimeError("Обновление заменило выбранный интервал дежурства")

    if config_path.exists():
        raise RuntimeError("Проверка сборки требует новый временный профиль")
    cfg = ConfigManager(config_path)
    draft = cfg.snapshot()
    if draft["autonomy"]["idle_minutes"] != 20:
        raise RuntimeError("Новый профиль запускает дежурство раньше 20 минут")
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

    manager = SkillManager(cfg.snapshot)
    from speech_check import check_turns

    check_turns(manager)
    practice = HearingPractice(manager, cfg.snapshot)
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
    import psutil

    process = psutil.Process(client.call("ping")["pid"])
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
        # Releasing the kernel lock precedes process exit. On Windows stderr's
        # startup.log handle stays open until then and makes temp cleanup fail.
        try:
            process.wait(timeout=15)
        except psutil.TimeoutExpired as exc:
            raise RuntimeError("Проверочное ядро не завершилось после shutdown") from exc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if lock.acquire():
                lock.close()
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Проверочное ядро не освободило блокировку профиля")


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
    check_idle_audio(engine)
    check_audio_conversion()


def check_runtime_efficiency():
    """Check idle work and cache invalidation without native devices/accounts."""
    import copy
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from configuration import ConfigManager
    from skill_manager import SkillManager
    from speech_dictionary import CommandDictionary
    from command_catalog import rows
    from audio_focus import AudioFocus

    with TemporaryDirectory(prefix="krest-efficiency-") as folder:
        cfg = ConfigManager(Path(folder) / "config.json")
        draft = cfg.snapshot()
        draft["applications"] = [{"name": "Проверка", "kind": "exe", "target": "check.exe"}]
        cfg.save_config(draft)
        independent = cfg.snapshot()
        independent["applications"][0]["name"] = "Несохранённое изменение"
        if cfg.snapshot() != draft:
            raise RuntimeError("Редактирование снимка изменило сохранённые настройки")
        calls = 0

        def settings():
            nonlocal calls
            calls += 1
            return cfg.snapshot()

        manager = SkillManager(settings)
        calls = 0
        if not rows(manager, draft) or calls:
            raise RuntimeError("Каталог повторно читает настройки для каждой команды")
        dictionary = CommandDictionary()
        first = dictionary.refresh(manager, draft)
        if not first or dictionary.refresh(manager, draft) is not None:
            raise RuntimeError("Неизменившийся голосовой словарь пересоздаётся")
        draft["command_aliases"]["музыка дальше"] = "переключи трек"
        if "крест музыка дальше" not in dictionary.refresh(manager, draft):
            raise RuntimeError("Новая фраза не обновила голосовой словарь")
        draft["command_aliases"].clear()
        if "крест музыка дальше" in dictionary.refresh(manager, draft):
            raise RuntimeError("Удалённая фраза осталась в голосовом словаре")
        mixer_cfg = {
            "audio_focus": {"enabled": True, "applications": "music.exe", "game_percent": 50}
        }
        sessions = [{"id": "one", "name": "music.exe", "volume": 0.8}]
        reads = 0

        def read():
            nonlocal reads
            reads += 1
            return copy.deepcopy(sessions)

        def write(ident, value):
            sessions[0]["volume"] = value

        focus = AudioFocus(lambda: mixer_cfg, Path(folder) / "audio.json", (read, write))
        for _ in range(10):
            focus.tick(force=True)
        if reads or focus.path.exists():
            raise RuntimeError("Микшер опрашивает аудиосессии или пишет журнал в покое")
        focus.tick(gaming=True, force=True)
        if sessions[0]["volume"] != 0.4:
            raise RuntimeError("Микшер не приглушил музыку после простоя")
        focus.tick(force=True)
        if sessions[0]["volume"] != 0.8:
            raise RuntimeError("Микшер не восстановил громкость")
    print(
        "Проверены изоляция настроек, обновление словаря и работа микшера после простоя.",
        flush=True,
    )


def check_audio_conversion():
    """Native fallback for Python without audioop, including callback boundaries."""
    import numpy as np
    from audio_devices import PCMConverter

    for rate in (44100, 48000):
        converter = PCMConverter(rate, channels=2)
        converter.audioop, converter.numpy = None, np
        # An antiphase stereo microphone must not disappear when mixed to mono.
        raw = np.tile(np.array([1200, -1200], dtype="<i2"), rate).tobytes()
        data = b"".join(converter.convert(raw[i : i + 772]) for i in range(0, len(raw), 772))
        values = np.frombuffer(data, dtype="<i2")
        if abs(len(values) - 16000) > 1 or not np.all(np.abs(values) == 1200):
            raise RuntimeError("Преобразование микрофона теряет звук или время между блоками")
    print("Преобразование 44,1/48 кГц: длительность и голос в противофазе сохранены.", flush=True)


def check_capture_efficiency():
    """Measured callbacks reuse their RMS; split callbacks still measure each frame."""
    from speech_stream import AudioFrames
    from speech_tools import pcm_levels

    calls = 0

    def meter(frame):
        nonlocal calls
        calls += 1
        return pcm_levels(frame)

    queue = AudioFrames(levels=meter)
    frame = b"\xe8\x03" * 480  # 30 ms, amplitude 1000
    queue.put(frame, 1.0, rms=1000)
    packet = queue.take()[0]
    if calls or packet[:3] != (frame, 1000, 1.0):
        raise RuntimeError("Голос повторно обрабатывает уже измеренный звуковой блок")
    queue.put(frame[:400], 2.0, rms=1000)
    queue.put(frame[400:], 2.0175, rms=1000)
    if calls != 1 or queue.take()[0][:2] != (frame, 1000):
        raise RuntimeError("Дробление блока микрофона изменило уровень звука")
    queue.put(frame, 3.0, overflow=True, rms=1000)
    if queue.generation != 1 or queue.take()[0][3] != 1:
        raise RuntimeError("Обработка микрофона потеряла отметку пропуска звука")


def check_game_detection():
    """Known games need no process-path queries; Steam still recognizes unknown names."""
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from performance import PerformancePolicy

    with TemporaryDirectory(prefix="krest-game-check-") as folder:
        root = Path(folder)
        policy = PerformancePolicy(lambda: {}, root / "activity.json")
        policy.game_folders[str(root).casefold()] = "Тестовая игра"
        known = SimpleNamespace(info={"name": "cs2.exe"}, exe=Mock())
        other = SimpleNamespace(
            info={"name": "game.exe"}, exe=Mock(return_value=str(root / "bin/game.exe"))
        )
        helper = SimpleNamespace(info={"name": "steam.exe"}, exe=Mock())
        with patch("performance.psutil.process_iter", return_value=[helper, other, known]):
            if policy._detect_game({"cs2.exe"}) != ("cs2.exe", "cs2.exe"):
                raise RuntimeError("Известная игра не обнаружена")
            if any(proc.exe.called for proc in (helper, other, known)):
                raise RuntimeError("Известная игра вызывает лишнее чтение путей процессов")
        with patch("performance.psutil.process_iter", return_value=[helper, other]):
            if policy._detect_game(set()) != ("Тестовая игра", "game.exe"):
                raise RuntimeError("Игра из библиотеки Steam не обнаружена по папке")
            if helper.exe.called or other.exe.call_count != 1:
                raise RuntimeError("Проверка Steam читает пути фоновых процессов")


def check_idle_protocol():
    """The idle indicator must block, wake on changes, and refresh rotated tokens."""
    import tempfile
    import threading
    from pathlib import Path
    from command_feedback import CommandFeedback
    from kernel_client import KernelClient
    from secret_store import protect_bootstrap
    from storage import atomic_write_json

    feedback = CommandFeedback()
    ready, completed = threading.Event(), threading.Event()
    result = []

    def waiter():
        ready.set()
        result.append(feedback.wait(0, timeout=1))
        completed.set()

    worker = threading.Thread(target=waiter)
    worker.start()
    try:
        if not ready.wait(2) or completed.wait(0.05):
            raise RuntimeError("Индикатор опрашивает состояние без ожидания события")
        turn = feedback.begin()
        if not completed.wait(2) or result[0][1].get("turn") != turn:
            raise RuntimeError("Обращение не разбудило индикатор")
    finally:
        worker.join(timeout=2)
    with tempfile.TemporaryDirectory(prefix="krest-ipc-cache-") as folder:
        path = Path(folder) / "kernel_endpoint.json"
        client = KernelClient(Path(folder) / "config.json")
        for token, port in (("test-one", 12345), ("test-two", 12346)):
            atomic_write_json(
                path, {"protocol": 1, "port": port, "token": protect_bootstrap(token)}
            )
            for _ in range(3):
                if client._connection_info() != (port, token):
                    raise RuntimeError(
                        "Клиент сохранил старые данные подключения после перезапуска"
                    )
    print("Ожидание индикатора и смена локального ключа проверены.", flush=True)


def check_idle_storage():
    """Empty notification polling must not acquire a database write lock."""
    import queue
    import tempfile
    from contextlib import contextmanager
    from pathlib import Path
    from notifications import NotificationQueue
    from notification_center import NotificationCenter

    with tempfile.TemporaryDirectory(prefix="krest-idle-storage-") as folder:
        root = Path(folder)
        delivery = NotificationQueue(root / "delivery.sqlite3")
        inbox = NotificationCenter(root / "inbox.sqlite3")
        statements = []

        @contextmanager
        def traced(connect):
            with connect() as db:
                db.set_trace_callback(statements.append)
                yield db

        delivery_connect, inbox_connect = delivery._connect, inbox.connect
        delivery._connect = lambda: traced(delivery_connect)
        inbox.connect = lambda: traced(inbox_connect)
        for _ in range(5):
            try:
                delivery.get_nowait()
            except queue.Empty:
                pass
            else:
                raise RuntimeError("Пустая очередь вернула сообщение")
            if inbox.take_after_game():
                raise RuntimeError("Пустой список отложенных уведомлений вернул сообщение")
        if any(s.startswith(("BEGIN", "DELETE", "UPDATE", "INSERT")) for s in statements):
            raise RuntimeError("Пустая очередь уведомлений открывает запись в базу")
        delivery.put("Проверка доставки")
        if delivery.get_nowait() != "Проверка доставки":
            raise RuntimeError("Уведомление потеряно после пустого ожидания")
        ident, _ = inbox.add("Проверка после игры")
        inbox.after_game(ident)
        if [r["id"] for r in inbox.take_after_game()] != [ident] or inbox.take_after_game():
            raise RuntimeError("Уведомление после игры потеряно или доставлено повторно")
        if inbox.unread_count() != 1:
            raise RuntimeError("Счётчик входящих не обновился после доставки")
        inbox.mark_read(ident)
        if inbox.unread_count():
            raise RuntimeError("Прочитанное сообщение осталось в счётчике")
    print("Пустые очереди без записи и доставка новых уведомлений проверены.", flush=True)


def check_idle_audio(engine):
    """Real loaded models, simulated silent microphone, no device or action."""
    from types import SimpleNamespace

    original = engine.stream
    engine.stream = SimpleNamespace(active=True)
    engine.clear_audio_queue()
    engine._reset_recognizer()
    engine._noise.reset()
    try:
        # A failed/reconnecting microphone must not keep the audio event set.
        engine.q.put(bytes(960), time.monotonic())
        engine.stream = None
        original_attempt = engine.last_mic_error_time
        engine.last_mic_error_time = time.time()
        try:
            engine.listen()
            if engine.q.ready.is_set():
                raise RuntimeError("Сбой микрофона оставил активное ожидание звука")
        finally:
            engine.last_mic_error_time = original_attempt
        engine.stream = SimpleNamespace(active=True)
        started, cpu = time.monotonic(), time.process_time()
        for _ in range(100):
            engine.audio_callback(bytes(960), 480, None, None)
            if engine.listen():
                raise RuntimeError("Тишина распознана как команда")
            engine.wait_for_audio(0.03)
        duration = time.monotonic() - started
        used = time.process_time() - cpu
        print(f"Loaded voice idle: {used:.3f}s CPU / {duration:.2f}s wall (one core).", flush=True)
        if used > max(0.75, duration * 0.25):
            raise RuntimeError("Голос в тишине использует больше 25% одного ядра")
    finally:
        engine.stream = original
        engine.clear_audio_queue()
        engine._reset_recognizer()


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
        from types import SimpleNamespace
        from welcome_gui import ControlCenterApp

        tree = ttk.Treeview(root, columns=("kind", "label", "remaining"))
        rows = {"active_timers": [{"id": "sample", "label": "Тест", "end_time": time.time() + 60}]}
        app = SimpleNamespace(
            _built_pages={"timers"},
            _timer_rows=None,
            timer_tree=tree,
            runtime=SimpleNamespace(state=SimpleNamespace(snapshot=lambda: rows)),
        )
        ControlCenterApp._update_timers(app)
        tree.selection_set("active_timers:sample")
        tree.focus("active_timers:sample")
        rows["active_timers"][0]["end_time"] += 60
        ControlCenterApp._update_timers(app)
        if tree.selection() != ("active_timers:sample",) or tree.focus() != "active_timers:sample":
            raise RuntimeError("Обновление таймера пересоздало выбранную строку")
        tree.destroy()
        history = tk.Text(pane.content)
        history.pack()
        history_state = SimpleNamespace(
            history=[],
            config_mgr=SimpleNamespace(snapshot=lambda: {}),
            current_page="actions",
            state=lambda: "withdrawn",
            event_text=history,
        )
        history_state._render_history = lambda: ControlCenterApp._render_history(history_state)
        for index in range(30):
            ControlCenterApp._append_log(history_state, f"Сообщение {index}")
        ControlCenterApp._flush_history(history_state)
        if history.get("1.0", "end").strip():
            raise RuntimeError("Скрытое меню перерисовывает журнал событий")
        history_state.state = lambda: "normal"
        root.update()
        ControlCenterApp._flush_history(history_state)
        if history_state._history_dirty or "Сообщение 29" not in history.get("1.0", "end"):
            raise RuntimeError("Меню не показало накопившиеся события после открытия")
        del history_state._render_history
        history.destroy()
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
        options["autonomy"].update(profile_backup=False, health_checks=False, backup_audit=False)
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
        verify = duty.call(
            "enqueue", kind="verify_version", values={"version_id": versions[0]["id"]}
        )["id"]
        duty.run_once()
        if (
            duty.store.get(verify)["status"] != "completed"
            or source.read_bytes() != b"original content"
        ):
            raise RuntimeError("Backup rehearsal failed or changed the original")
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


def check_expansion():
    """Portable contracts shipped with the release, using disposable state only."""
    import copy
    import os
    import sqlite3
    import subprocess
    import sys
    from contextlib import closing
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace
    import threading

    from configuration import ConfigManager, DEFAULT_CONFIG
    from voice_context import VoiceContext
    from backup_service import create_backup, rehearse_backup
    from component_health import ComponentHealth
    from process_lease import DownloadLease

    settings = copy.deepcopy(DEFAULT_CONFIG)
    context = VoiceContext(lambda: settings)
    context.remember(
        "skills.media.next_track",
        "следующий трек",
        {"voice_context": {"scope": "music", "player": "YandexMusic.exe"}},
    )
    command = context.resolve("сделай потише")
    if (
        not command
        or not context.valid(command)
        or command["target"]["player"] != "YandexMusic.exe"
    ):
        raise RuntimeError("Dialogue lost the selected player")
    for phrase in ("помоги убить врага", "не переключи трек", "выключи компьютер", "ещё немного"):
        if context.resolve(phrase):
            raise RuntimeError("Dialogue inferred an unsupported action: " + phrase)
    context.remember("skills.media.volume_down", "тише", previous=command["target"])
    if context.resolve("ещё немного")["command"] != "тише на пять" or context.valid(command):
        raise RuntimeError("Dialogue replayed a stale target or lost volume direction")
    settings["owner_name"] = "Another profile"
    if context.resolve("сделай потише"):
        raise RuntimeError("Dialogue leaked across profiles")
    old = context.generation
    context.clear()
    context.remember("skills.discord_voice.mute_microphone", "мут")
    context.remember(
        "skills.media.next_track",
        "следующий трек",
        {"voice_context": {"scope": "music", "player": "late.exe"}},
        revision=old,
    )
    if context.resolve("включи обратно")["command"] != "включи микро":
        raise RuntimeError("A slow old command replaced the newer dialogue target")
    with TemporaryDirectory(prefix="krest-rehearsal-") as folder:
        root = Path(folder)
        config = ConfigManager(root / "config.json")
        config.save_config(config.snapshot())
        original = config.filepath.read_bytes()
        with closing(sqlite3.connect(root / "memory.sqlite3")) as db, db:
            db.execute("CREATE TABLE example(value TEXT)")
            db.execute("INSERT INTO example VALUES ('kept')")
        backup = root / "backup.zip"
        create_backup(config, backup)
        rehearse_backup(backup, temporary_root=root)
        if config.filepath.read_bytes() != original or list(root.glob("krest-restore-check-*")):
            raise RuntimeError("Backup rehearsal changed the profile or left staged files")
        rt = SimpleNamespace(
            settings=config.snapshot,
            stop_event=threading.Event(),
            integrations=SimpleNamespace(
                MODULES={"demo": ("demo.py", "Demo")},
                refresh=lambda: None,
                details=lambda _: {"running": True, "bridge": {}, "restarts": 0},
            ),
        )
        health = ComponentHealth(rt, root)
        health.check(("bots",), repair=False)
        if health.snapshot()["components"]["bot:demo.py"]["state"] != "unknown":
            raise RuntimeError("Running bot was mistaken for a confirmed connection")
    if os.name == "nt" and not getattr(sys, "frozen", False):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            lease = DownloadLease(process)
            lease.close()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    print(
        "Voice context, backup rehearsal, connection evidence and download lifetime passed.",
        flush=True,
    )
