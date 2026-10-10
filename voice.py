"""Lightweight speech interface shared by text commands, GUI and skills.

Importing this module never loads a model, opens an audio device or creates a window.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import importlib.util
import importlib
import logging
import queue
import threading

logger = logging.getLogger(__name__)
_capture = ContextVar("speech_capture", default=None)
_engine = None
_model_signature = None
_output = None
_settings = lambda: {}
_state = None
_speech_lock = threading.RLock()
_engine_lock = threading.RLock()
_audio_queue = queue.Queue(maxsize=50)
_audio_thread = None


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


def _playback():
    while True:
        text = _audio_queue.get()
        try:
            with _speech_lock:
                if (
                    _engine is not None
                    and not getattr(_state, "is_muted", False)
                    and _settings().get("app_settings", {}).get("voice_enabled", True)
                ):
                    _engine.speak(text)
        except Exception:
            logger.exception("Ошибка воспроизведения")
        finally:
            _audio_queue.task_done()


def speak(text):
    global _audio_thread
    text = str(text).strip()
    if not text:
        return
    messages = _capture.get()
    if messages is not None:
        messages.append(text)
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
                _audio_queue.put_nowait(text)
            except queue.Full:
                logger.warning("Очередь озвучки заполнена; текст доступен в меню")


def enable():
    global _engine, _model_signature
    with _engine_lock:
        if _engine is not None:
            return
        cfg = _settings().get("app_settings", {})
        signature = (
            cfg.get("vosk_path", "model"),
            cfg.get("silero_path", "model_silero.pt"),
        )
        missing = [
            name
            for name in ("torch", "vosk", "sounddevice", "numpy", "num2words")
            if importlib.util.find_spec(name) is None
        ]
        if missing:
            raise RuntimeError(
                "Установите requirements-voice.txt. Не найдены: " + ", ".join(missing)
            )
        base = Path(__file__).resolve().parent
        if not (base / signature[0]).is_dir() or not (base / signature[1]).is_file():
            raise RuntimeError("Выберите существующие модели Vosk и Silero на странице настроек.")
        import config

        importlib.reload(config)
        engine = importlib.import_module("voice_engine")
        if _model_signature is not None and signature != _model_signature:
            engine = importlib.reload(engine)
        _model_signature = signature
        if engine.rec is None or engine.model_tts is None:
            raise RuntimeError("Не загружены модели Vosk/Silero. Проверьте пути в настройках.")
        if engine.stream is None or not engine.stream.active:
            if not engine.start_microphone():
                raise RuntimeError("Микрофон недоступен. Проверьте выбранное устройство.")
        _engine = engine


def disable():
    global _engine
    with _engine_lock, _speech_lock:
        engine, _engine = _engine, None
        while True:
            try:
                _audio_queue.get_nowait()
                _audio_queue.task_done()
            except queue.Empty:
                break
        if engine is not None:
            engine.sd.stop()
            if engine.stream is not None:
                engine.stream.stop()
                engine.stream.close()
                engine.stream = None


def listen():
    engine = _engine
    return engine.listen() if engine is not None else ""


def is_enabled():
    return _engine is not None
