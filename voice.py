"""Lightweight speech interface shared by text commands, GUI and skills.

Importing this module never loads a model, opens an audio device or creates a window.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import importlib.util
import importlib
import logging
import queue
import threading
import time

logger = logging.getLogger(__name__)
_capture = ContextVar("speech_capture", default=None)
_defer = ContextVar("speech_defer", default=False)
_engine = None
_model_signature = None
_output = None
_settings = lambda: {}
_state = None
_speech_lock = threading.RLock()
_engine_lock = threading.RLock()
_audio_queue = queue.Queue(maxsize=50)
_audio_thread = None
_generation = 0
_remote = None
_vosk_repairs = {}
_recovered_vosk = ""
_vosk_recovery_source = ""


def set_remote(remote):
    global _remote
    _remote = remote


def configure(output=None, settings=None, state=None):
    global _output, _settings, _state
    _output, _state = output, state
    if settings is not None:
        _settings = settings


@contextmanager
def capture():
    messages = []
    token = _capture.set(messages)
    try:
        yield messages
    finally:
        _capture.reset(token)


@contextmanager
def defer_output():
    """Capture per-step replies, then announce one ordered sequence summary."""
    token = _defer.set(True)
    try:
        yield
    finally:
        _defer.reset(token)


def _playback():
    while True:
        generation, text = _audio_queue.get()
        try:
            with _speech_lock:
                if (
                    _engine is not None
                    and generation == _generation
                    and not getattr(_state, "is_muted", False)
                    and _settings().get("app_settings", {}).get("voice_enabled", True)
                ):
                    from contextlib import nullcontext

                    focus = getattr(getattr(_state, "runtime", None), "audio_focus", None)
                    with focus.speech() if focus is not None else nullcontext():
                        _engine.speak(text, cancelled=lambda: generation != _generation)
        except Exception:
            logger.exception("Ошибка воспроизведения")
        finally:
            _audio_queue.task_done()


def speak(text):
    global _audio_thread
    if _remote is not None:
        return _remote.client.call("voice.speak", text=str(text))
    text = str(text).strip()
    if not text:
        return
    messages = _capture.get()
    if messages is not None:
        messages.append(text)
    if _defer.get():
        return
    if _output:
        _output(text)
    else:
        logger.info("%s", text)
    if _engine is not None and _settings().get("app_settings", {}).get("voice_enabled", True):
        if not getattr(_state, "is_muted", False):
            with _engine_lock:
                if _audio_thread is None or not _audio_thread.is_alive():
                    _audio_thread = threading.Thread(
                        target=_playback, daemon=True, name="krest-speech"
                    )
                    _audio_thread.start()
            try:
                from companion_behavior import spoken_text

                runtime = getattr(_state, "runtime", None)
                gaming = bool(getattr(getattr(runtime, "performance", None), "game", ""))
                spoken = spoken_text(text, _settings(), gaming)
                _audio_queue.put_nowait((_generation, spoken))
            except queue.Full:
                logger.warning("Очередь озвучки заполнена; текст доступен в меню")


def local_models_ready(settings):
    """Only inspect paths: this does not import models or open a microphone."""
    from voice_models import discover_models

    return bool(discover_models(settings)["vosk_path"])


def enable():
    global _engine, _model_signature, _recovered_vosk, _vosk_recovery_source
    from voice_models import discover_models

    with _engine_lock:
        if _engine is not None:
            return
        models = discover_models(_settings())
        if _recovered_vosk and models["vosk_path"] == _vosk_recovery_source:
            from voice_models import is_vosk
            from pathlib import Path

            if is_vosk(Path(_recovered_vosk)):
                models["vosk_path"] = _recovered_vosk
        signature = tuple(models.values())
        missing = [
            name
            for name in ("vosk", "sounddevice", "numpy")
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            from app_paths import compiled

            action = (
                "Распакуйте свежий ZIP Крест целиком в новую папку"
                if compiled()
                else "Откройте start.bat для подготовки голоса"
            )
            raise RuntimeError(action + ". Не найдены: " + ", ".join(missing))
        if not models["vosk_path"]:
            raise RuntimeError(
                "Не найдена модель распознавания. Положите распакованную папку Vosk в model "
                "рядом с программой. Внутри нужны am/final.mdl и conf/model.conf. Крест повторит подключение автоматически."
            )
        import config

        from app_paths import compiled

        if not compiled():
            importlib.reload(config)
        # Pass paths explicitly; importing the engine never loads Silero or PyTorch.
        config.MODEL_VOSK_PATH = models["vosk_path"]
        config.MODEL_SILERO_PATH = models["silero_path"]
        was_loaded = "voice_engine" in __import__("sys").modules
        engine = importlib.import_module("voice_engine")
        if signature != _model_signature or engine.rec is None:
            engine.initialize_recognition(models["vosk_path"])
        if getattr(engine, "silero_path", models["silero_path"]) != models["silero_path"]:
            # A changed voice model is reloaded by the TTS worker, never by capture.
            engine.model_tts = None
        _model_signature = signature
        engine.configure(_settings)
        from audio_scheduling import status

        engine.metrics["scheduler"] = status()
        if was_loaded:
            engine.request_microphone_refresh()
        if engine.rec is None:
            failed = models["vosk_path"]
            if time.monotonic() >= _vosk_repairs.get(failed, 0) and _settings().get(
                "app_settings", {}
            ).get("voice_auto_models", True):
                from model_setup import prepare

                # Retry after a temporary network failure, without downloading on each tick.
                _vosk_repairs[failed] = time.monotonic() + 600
                repaired = prepare(
                    _settings(),
                    lambda text: _output(text) if _output else logger.info("%s", text),
                    components=("vosk_path",),
                    rejected_vosk=(failed,),
                )
                if repaired["vosk_path"]:
                    engine.initialize_recognition(repaired["vosk_path"])
                    if engine.rec is not None:
                        _vosk_recovery_source = failed
                        _recovered_vosk = models["vosk_path"] = repaired["vosk_path"]
                        _model_signature = tuple(models.values())
            if engine.rec is None:
                raise RuntimeError(
                    "Vosk не загрузилась: "
                    + engine.metrics.get("model_error", "проверьте целостность файлов")
                    + ". Папка: "
                    + failed
                    + ". Распакуйте ZIP целиком. Если путь содержит русские буквы, попробуйте C:\\Krest."
                )
        engine.metrics.update(models=models, tts_ready=engine.model_tts is not None)
        try:
            active = engine.stream is not None and engine.stream.active
        except Exception:
            active = False
        if not active:
            engine.start_microphone()
        _engine = engine


def tts_progress(text):
    engine = _engine
    if engine is not None:
        engine.metrics["tts_progress"] = text


def refresh_tts(models, cancelled=lambda: False):
    """Attach a downloaded voice without reinitializing recognition or audio input."""
    global _model_signature
    with _engine_lock, _speech_lock:
        engine = _engine
        if engine is None or cancelled():
            return False
        if engine.model_tts is None:
            engine.initialize_tts(models["silero_path"])
        engine.metrics["models"] = dict(models)
        _model_signature = tuple(models.values())
        return engine.model_tts is not None


def disable():
    global _engine
    stop_speaking()
    with _engine_lock, _speech_lock:
        engine, _engine = _engine, None
        while True:
            try:
                _audio_queue.get_nowait()
                _audio_queue.task_done()
            except queue.Empty:
                break
        if engine is not None:
            try:
                engine.sd.stop()
            except Exception:
                pass
            if engine.stream is not None:
                try:
                    engine.stream.stop()
                except Exception:
                    pass
                try:
                    engine.stream.close()
                except Exception:
                    pass
                engine.stream = None


def listen():
    engine = _engine
    return engine.listen() if engine is not None else ""


def wait_for_audio(timeout=0.03):
    engine = _engine
    if engine is not None:
        engine.wait_for_audio(timeout)
    else:
        time.sleep(timeout)


def set_command_dictionary(phrases):
    if _engine is not None:
        _engine.set_command_dictionary(phrases)


def refresh_output():
    engine = _engine
    if engine is not None:
        engine.refresh_output()


def is_enabled():
    if _remote is not None:
        return _remote.cached["voice"]["enabled"]
    return _engine is not None


def stop_speaking():
    global _generation
    if _remote is not None:
        return _remote.client.call("voice.interrupt")
    _generation += 1
    engine = _engine
    if engine is not None:
        engine.interrupt()
    while True:
        try:
            _audio_queue.get_nowait()
            _audio_queue.task_done()
        except queue.Empty:
            break


def telemetry():
    if _remote is not None:
        return _remote.cached["voice"]["telemetry"]
    engine = _engine
    if engine is None:
        return {}
    import time

    last_audio = getattr(engine, "_last_audio_at", 0)
    return dict(
        engine.metrics,
        speaking=bool(engine.is_speaking),
        audio_age_seconds=round(time.monotonic() - last_audio, 2) if last_audio else None,
    )


def recognition():
    engine = _engine
    return dict(engine.last_recognition) if engine else {}
