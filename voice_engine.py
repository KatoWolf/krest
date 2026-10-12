import os
import sys
import json

torch_import_error = ""
try:
    import torch
except (ImportError, OSError) as exc:
    torch = None
    torch_import_error = str(exc)[:240]
import sounddevice as sd
import vosk

try:
    from num2words import num2words
except ImportError:

    def num2words(value, **kwargs):
        return str(value)


import re
import time
import logging
import importlib
import numpy as np
import hashlib
import threading
from collections import deque
from speech_tools import Endpoint, pcm_levels, verbalize_numbers, AdaptiveNoiseFloor
from speech_stream import AudioFrames, Continuity

# --- Индикатор и конфигурация ---


def _init_indicator_trigger():
    """Динамический автопоиск функции trigger_indicator_pulse во всех модулях."""
    modules_to_try = [
        "guikrest",
        "skills.guikrest",
        "visual_indicator",
        "skills.visual_indicator",
    ]

    for mod_name in modules_to_try:
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "trigger_indicator_pulse"):
                return getattr(mod, "trigger_indicator_pulse")
        except Exception:
            pass

    for mod_name, mod in list(sys.modules.items()):
        if mod and hasattr(mod, "trigger_indicator_pulse"):
            func = getattr(mod, "trigger_indicator_pulse")
            if callable(func) and func.__module__ != __name__:
                return func

    return None


_indicator_func = None


def trigger_indicator_pulse(duration: float = 4.0):
    """Вызов вспышки визуального индикатора при обнаружении команды."""
    global _indicator_func
    if _indicator_func is None:
        _indicator_func = _init_indicator_trigger()

    if _indicator_func:
        try:
            _indicator_func(duration)
        except Exception:
            pass


try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    import config
except ImportError:
    config = None


if torch is not None:
    torch.set_num_threads(
        int(config.reload_config().get("app_settings", {}).get("tts_threads", 2)) if config else 2
    )
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # PyTorch inter-op count cannot be reset after initialization.
    torch.set_grad_enabled(False)
    torch.backends.cudnn.benchmark = False

    if sys.platform == "win32":
        try:
            torch.backends.quantized.engine = "qnnpack"
        except Exception:
            pass

# Глобальное состояние
q = AudioFrames()
_continuity = Continuity()
_recognizer_lock = threading.RLock()
_play_lock = threading.Lock()
_cancel_speech = threading.Event()
_settings_getter = lambda: config.reload_config() if config else {}
metrics = {"rms": 0, "peak": 0, "dropped": 0, "partial": ""}
last_recognition = {}
_endpoint = Endpoint()
_noise = AdaptiveNoiseFloor()
_audio_cfg = {}
_audio_cfg_at = 0
_segments = []
_words = []
_preroll = deque(maxlen=10)  # 300 ms, independent of USB/Bluetooth callback sizes.
_partial_elapsed = 0.0
_partial_stable = 0.0
is_speaking = False
stream = None
last_mic_error_time = 0
_microphone_setting = None
_last_audio_at = 0
_last_backend_refresh = 0
_failed_microphones = {}
_last_nonzero_audio_at = 0
_refresh_audio_requested = False
_quick_phrases = set()

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_wake_word() -> str:
    """Динамическое получение обращения из конфига."""
    if config:
        return str(config.reload_config().get("app_settings", {}).get("wake_word", "крест")).lower()
    return "крест"


def get_active_timeout() -> float:
    """Динамическое получение таймаута из конфига."""
    if config:
        return float(config.reload_config().get("app_settings", {}).get("active_timeout", 6.0))
    return 6.0


KREST_VOLUME = float(getattr(config, "KREST_VOLUME", 1.3)) if config else 1.3

vosk_model_setting = (
    getattr(config, "MODEL_VOSK_PATH", getattr(config, "VOSK_MODEL_PATH", "model"))
    if config
    else "model"
)
vosk_path = (
    vosk_model_setting
    if os.path.isabs(vosk_model_setting)
    else os.path.join(BASE_DIR, vosk_model_setting)
)

silero_model_setting = (
    getattr(
        config,
        "MODEL_SILERO_PATH",
        getattr(config, "SILERO_MODEL_PATH", "model_silero.pt"),
    )
    if config
    else "model_silero.pt"
)
silero_path = (
    silero_model_setting
    if os.path.isabs(silero_model_setting)
    else os.path.join(BASE_DIR, silero_model_setting)
)

SAMPLE_RATE = (
    int(getattr(config, "SAMPLE_RATE", getattr(config, "SAMPLERATE", 16000))) if config else 16000
)

from app_paths import data_dir

TTS_CACHE_DIR = str(data_dir() / "tts_cache")
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

# --- Вспомогательные функции ---


def auto_accentuate_name(name: str = "", pronunciation: str = None) -> str:
    """Автоматическое расставление ударений для имён пользователей."""
    if pronunciation and "+" in pronunciation:
        return pronunciation

    target = pronunciation if pronunciation else name
    if not target:
        return "пользователь"

    en_to_ru_names = {
        "katowolf": "к+ато в+ульф",
        "kato": "к+ато",
        "wolf": "в+ульф",
        "alex": "ал+екс",
        "max": "макс",
        "admin": "адм+ин",
        "user": "юзер",
    }

    clean_low = target.strip().lower()
    if clean_low in en_to_ru_names:
        return en_to_ru_names[clean_low]

    translit_map = {
        "a": "а",
        "b": "б",
        "c": "к",
        "d": "д",
        "e": "е",
        "f": "ф",
        "g": "г",
        "h": "х",
        "i": "и",
        "j": "дж",
        "k": "к",
        "l": "л",
        "m": "м",
        "n": "н",
        "o": "о",
        "p": "п",
        "q": "к",
        "r": "р",
        "s": "с",
        "t": "т",
        "u": "у",
        "v": "в",
        "w": "в",
        "x": "кс",
        "y": "и",
        "z": "з",
    }

    ru_text = ""
    for char in target:
        low = char.lower()
        ru_text += translit_map.get(low, char)

    # Unknown names use Silero's accent model; guessed stress distorted names.
    return ru_text


def clear_audio_queue():
    q.clear()


def manage_disk_cache(max_files=200, max_bytes=64 * 1024 * 1024):
    """Bound both the number and total size of synthesized replies."""
    try:
        files = sorted(
            (
                os.path.join(TTS_CACHE_DIR, f)
                for f in os.listdir(TTS_CACHE_DIR)
                if f.endswith(".npy")
            ),
            key=os.path.getatime,
            reverse=True,
        )
        used = 0
        for index, path in enumerate(files):
            used += os.path.getsize(path)
            if index >= max_files or used > max_bytes:
                os.remove(path)
    except OSError:
        pass


# --- Инициализация нейросетей ---


def initialize_models(models=None):
    global vosk_path, silero_path
    if models:
        vosk_path, silero_path = models["vosk_path"], models["silero_path"]
    initialize_recognition(vosk_path)
    initialize_tts(silero_path)


def initialize_recognition(path):
    global rec, model_vosk, vosk_path
    vosk_path = path
    metrics.pop("model_error", None)
    logging.getLogger(__name__).info(
        "--- [VOICE ENGINE]: Загрузка нейронных связей слуха (Vosk)... ---"
    )
    rec = None
    model_vosk = None
    try:
        if os.path.exists(vosk_path):
            if not os.path.exists(os.path.join(vosk_path, "am")) and not os.path.exists(
                os.path.join(vosk_path, "conf")
            ):
                subdirs = [
                    os.path.join(vosk_path, d)
                    for d in os.listdir(vosk_path)
                    if os.path.isdir(os.path.join(vosk_path, d))
                ]
                if len(subdirs) == 1:
                    vosk_path = subdirs[0]

        from vosk_support import model_problem, native_model_path

        if problem := model_problem(vosk_path):
            raise RuntimeError(problem)
        actual_path = native_model_path(vosk_path)
        model_vosk = vosk.Model(actual_path)
        rec = vosk.KaldiRecognizer(model_vosk, SAMPLE_RATE)
        rec.SetWords(True)
        if hasattr(rec, "SetPartialWords"):
            rec.SetPartialWords(True)
        metrics.update(model_path=vosk_path, model_native_path=actual_path)
    except Exception as e:
        metrics["model_error"] = str(e)
        logging.getLogger(__name__).info(
            f"--- [CRITICAL]: Ошибка загрузки Vosk. Путь: {vosk_path} | Ошибка: {e} ---"
        )


def initialize_tts(path):
    """Replace only the synthesizer; recognition and the microphone stay active."""
    global model_tts, silero_path, device
    silero_path = path
    model_tts = None
    metrics.update(tts_ready=False, tts_error="")
    if torch is None:
        metrics["tts_error"] = "Не загрузился голосовой движок PyTorch: " + torch_import_error
        return False
    if not os.path.isfile(path):
        metrics["tts_error"] = "Модель Silero отсутствует или повреждена."
        return False
    try:
        device = torch.device("cpu")
        candidate = torch.package.PackageImporter(path).load_pickle("tts_models", "model")
        candidate.to(device)
        model_tts = candidate
        # A model is ready only after a real synthesis, not merely deserialization.
        synthesize("Крест готова.")
    except Exception as exc:
        model_tts = None
        metrics["tts_error"] = "Не удалось загрузить Silero: " + str(exc)[:240]
        logging.getLogger(__name__).warning(metrics["tts_error"])
        return False
    metrics.update(tts_ready=True, tts_progress="Голос Silero готов", tts_error="")
    return True


def synthesize(text):
    """The same bounded CPU synthesis path is used by runtime and release checks."""
    if model_tts is None or torch is None:
        raise RuntimeError("Silero ещё не готова")
    # v4 uses xenia; some older drop-in packages offer kseniya instead.
    speakers = getattr(model_tts, "speakers", ())
    speaker = "kseniya" if "xenia" not in speakers and "kseniya" in speakers else "xenia"
    with torch.no_grad():
        samples = model_tts.apply_tts(
            text=text,
            speaker=speaker,
            sample_rate=24000,
            put_accent=True,
            put_yo=True,
        ).numpy()
    return validate_audio(samples)


def validate_audio(samples):
    samples = np.asarray(samples, dtype=np.float32)
    if (
        samples.ndim != 1
        or not 240 <= samples.size <= 24000 * 120
        or not np.isfinite(samples).all()
    ):
        raise ValueError("Silero вернула некорректный звук")
    return samples


initialize_models()

# --- Захват и обработка звука ---


def configure(settings_getter):
    global _settings_getter, _audio_cfg
    _audio_cfg = {}
    _settings_getter = settings_getter
    settings = _settings_getter().get("app_settings", {})
    if torch is not None:
        torch.set_num_threads(int(settings.get("tts_threads", 2)))
    with _recognizer_lock:
        clear_audio_queue()
        _reset_recognizer()


def set_command_dictionary(phrases):
    global _quick_phrases
    _quick_phrases = frozenset(phrases)
    metrics["dictionary_phrases"] = len(_quick_phrases)


def interrupt():
    with _play_lock:
        _cancel_speech.set()
        sd.stop()


def audio_callback(indata, frames, time_info, status):
    global _last_audio_at, _last_nonzero_audio_at
    _last_audio_at = time.monotonic()
    metrics["connected"] = True
    if is_speaking and not _audio_cfg.get("voice_headphones", False):
        return
    data = bytes(indata)
    rms, peak = pcm_levels(data)
    if peak > 0:
        _last_nonzero_audio_at = _last_audio_at
    metrics.update(rms=rms, peak=peak)
    # PortAudio timestamps share a clock with each other, not with monotonic().
    lag = max(0, getattr(time_info, "currentTime", 0) - getattr(time_info, "inputBufferAdcTime", 0))
    metrics["capture_ms"] = round(min(10, lag) * 1000)
    q.put(
        data,
        _last_audio_at - max(0, lag - len(data) / (2 * SAMPLE_RATE)),
        bool(status and getattr(status, "input_overflow", False)),
    )
    metrics["dropped"] = q.dropped


def start_microphone() -> bool:
    """Безопасная инициализация / перезапуск микрофона."""
    global _refresh_audio_requested
    global \
        stream, \
        last_mic_error_time, \
        _microphone_setting, \
        _last_audio_at, \
        _last_backend_refresh, \
        _last_nonzero_audio_at

    # Защита от слишком частых попыток перезапуска при сбое устройства
    if time.time() - last_mic_error_time < 3.0:
        return False

    try:
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            stream = None

        settings = _settings_getter().get("app_settings", {})
        selected = settings.get("microphone", "")
        from audio_devices import open_microphone, refresh_idle_backend

        if (
            _refresh_audio_requested
            or last_mic_error_time
            and time.monotonic() - _last_backend_refresh > 15
        ) and not is_speaking:
            with _play_lock:
                if not is_speaking:
                    refresh_idle_backend(sd)
                    _last_backend_refresh = time.monotonic()
                    _refresh_audio_requested = False
        now = time.monotonic()
        avoid = {key for key, until in _failed_microphones.items() if until > now}
        # Do not retain indices: Windows can renumber them after USB reconnection.
        if avoid:
            stream, info = open_microphone(sd, selected, audio_callback, SAMPLE_RATE, avoid=avoid)
        else:
            stream, info = open_microphone(sd, selected, audio_callback, SAMPLE_RATE)
        _microphone_setting = str(selected)
        _last_audio_at = time.monotonic()
        _last_nonzero_audio_at = _last_audio_at
        _noise.reset(int(settings.get("speech_threshold", 200)))
        metrics.update(
            connected=True,
            device_name=info["name"],
            device_index=info["index"],
            input_rate=info["input_rate"],
            hostapi=info.get("hostapi"),
            host_name=info.get("host_name", ""),
            mic_error="",
            mic_hint="",
        )
        clear_audio_queue()
        _reset_recognizer()
        return True
    except Exception as e:
        last_mic_error_time = time.time()
        logging.getLogger(__name__).info(f"\n--- [MIC ERROR]: Ошибка захвата микрофона: {e} ---")
        metrics.update(connected=False, mic_error=str(e), rms=0, peak=0)
        stream = None
        return False


def request_microphone_refresh():
    global _refresh_audio_requested, last_mic_error_time
    _refresh_audio_requested = True
    last_mic_error_time = 0
    _failed_microphones.clear()


def fix_wake_word(text):
    """Kept for compatibility; wake words are checked once by the runtime."""
    return text


_decoder_ms = 0.0


def _decode(method, *args):
    global _decoder_ms
    started = time.perf_counter()
    try:
        return method(*args)
    finally:
        _decoder_ms += (time.perf_counter() - started) * 1000


def _reset_recognizer():
    global _decoder_ms, _partial_elapsed, _partial_stable
    _decoder_ms = 0.0
    _partial_elapsed = _partial_stable = 0.0
    if rec is not None:
        rec.Reset()
    _endpoint.reset()
    _segments.clear()
    _words.clear()
    _preroll.clear()
    metrics["partial"] = ""


def listen():
    global last_recognition, _audio_cfg, _audio_cfg_at, _partial_elapsed, _partial_stable
    if rec is None:
        return ""
    now = time.monotonic()
    if not _audio_cfg or now - _audio_cfg_at > 0.5:
        _audio_cfg = _settings_getter().get("app_settings", {})
        _audio_cfg_at = now
    cfg = _audio_cfg
    if is_speaking and not cfg.get("voice_headphones", False):
        return ""
    selected = str(cfg.get("microphone", ""))
    try:
        active = stream is not None and stream.active
    except Exception:
        active = False
    stalled = bool(_last_audio_at and now - _last_audio_at > 5)
    if stalled and metrics.get("device_name"):
        _failed_microphones[(metrics["device_name"], metrics.get("hostapi"))] = now + 60
        metrics.update(
            connected=False, mic_hint="Микрофон перестал передавать звук. Подключаю другой вход…"
        )
        # Only mark a stalled stream once; subsequent retries may still fail.
        metrics["device_name"] = ""
    if (
        not active
        or (_microphone_setting is not None and selected != _microphone_setting)
        or stalled
    ):
        if not start_microphone():
            return ""
    metrics["mic_hint"] = (
        "Микрофон подключён, но передаёт тишину. Проверьте кнопку mute на гарнитуре и уровень входа в Windows."
        if _last_nonzero_audio_at and now - _last_nonzero_audio_at > 8
        else ""
    )
    _endpoint.threshold = int(cfg.get("speech_threshold", 200))
    metrics["threshold"] = _endpoint.threshold
    _endpoint.pause = int(cfg.get("speech_pause_ms", 700)) / 1000
    with _recognizer_lock:
        if is_speaking and not cfg.get("voice_headphones", False):
            return ""
        for _ in range(32):
            packets = q.take(1)
            if not packets:
                break
            packet = packets[0]
            data, rms = packet[:2]
            age = max(0, time.monotonic() - packet[2])
            metrics["audio_queue_ms"] = round(age * 1000)
            if age > 1:
                # A busy/game-suspended process must not execute stale speech later.
                _reset_recognizer()
                _continuity.recovering, _continuity.quiet = True, 0.0
                continue
            accept, gap = _continuity.accept(packet[3], rms, _endpoint.threshold, q.seconds)
            if gap:
                _reset_recognizer()
            if not accept:
                continue
            if cfg.get("speech_auto_threshold", False):
                _endpoint.threshold = _noise.feed(rms, speaking=_endpoint.started)
                metrics["threshold"] = _endpoint.threshold
            if not _endpoint.started and rms < _endpoint.threshold:
                _preroll.append(data)
                continue
            if not _endpoint.started:
                for prefix in _preroll:
                    _decode(rec.AcceptWaveform, prefix)
                _preroll.clear()
            finished = _endpoint.feed(rms, len(data) / (2 * SAMPLE_RATE))
            from speech_tools import addressed_speech

            addressed, body = addressed_speech(
                metrics.get("partial", ""), cfg.get("wake_word", "крест")
            )
            quick = (cfg.get("wake_word", "крест") + " " + body) if addressed else body
            # Never execute a partial hypothesis. Only shorten the end-of-speech
            # wait for a complete known transport command; FinalResult still wins.
            if (
                cfg.get("voice_fast_commands", True)
                and quick.casefold() in _quick_phrases
                and _partial_stable >= 0.09
                and _endpoint.silence + 1e-9 >= min(_endpoint.pause, 0.33)
            ):
                finished = True
            if _decode(rec.AcceptWaveform, data):
                result = json.loads(_decode(rec.Result))
                _segments.append(result.get("text", ""))
                _words.extend(result.get("result", []))
            if finished:
                result = json.loads(_decode(rec.FinalResult))
                _segments.append(result.get("text", ""))
                _words.extend(result.get("result", []))
                text = " ".join(v for v in _segments if v).strip()
                confidences = [float(v["conf"]) for v in _words if "conf" in v]
                last_recognition = {
                    "text": text,
                    "confidence": sum(confidences) / len(confidences) if confidences else None,
                    "decoder_ms": round(_decoder_ms),
                    "pause_ms": round(_endpoint.silence * 1000),
                    "words": list(_words),
                    "audio_queue_ms": metrics.get("audio_queue_ms", 0),
                    "capture_ms": metrics.get("capture_ms", 0),
                }
                metrics["recognition_pause_ms"] = round(_endpoint.silence * 1000)
                _reset_recognizer()
                return text
            _partial_elapsed += q.seconds
            _partial_stable += q.seconds
            if _partial_elapsed + 1e-9 >= 0.09:
                _partial_elapsed = 0.0
                partial = " ".join(
                    [*_segments, json.loads(_decode(rec.PartialResult)).get("partial", "")]
                ).strip()
                if partial != metrics.get("partial"):
                    _partial_stable = 0.0
                    metrics["partial"] = partial
    return ""


# --- Синтез речи (TTS) ---


def clean_text_for_tts(text: str) -> str:
    text = re.sub(r"[*_`~\[\]\(\)\{\}]", "", text)
    text = re.sub(r"#(?=\w)", "", text)
    text = re.sub(r"(?<=[\w])\s*\n+\s*", ". ", text)
    text = re.sub(r"[—–]", ", ", text)
    text = re.sub(r" - ", ", ", text)
    text = re.sub(r"[^а-яА-ЯёЁa-zA-Z0-9\s\.,!?\:\;+]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def transliterate_english(text: str) -> str:
    en_words = {
        r"\bwindows\b": "в+индовс",
        r"\blinux\b": "л+инукс",
        r"\bgoogle\b": "г+угл",
        r"\byoutube\b": "ют+уб",
        r"\btwitch\b": "тв+ич",
        r"\btelegram\b": "телегр+ам",
        r"\bdiscord\b": "диск+орд",
        r"\bsteam\b": "ст+им",
        r"\bgame\b": "гейм",
        r"\bplay\b": "плей",
        r"\bcpu\b": "проц+ессор",
        r"\bram\b": "операт+ивная п+амять",
        r"\bgpu\b": "видеок+арта",
        r"\bfps\b": "фэ пэ +эс",
        r"\bpc\b": "пэк+а",
    }
    for pattern, replacement in en_words.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    en_to_ru = {
        "a": "а",
        "b": "б",
        "c": "ц",
        "d": "д",
        "e": "е",
        "f": "ф",
        "g": "г",
        "h": "х",
        "i": "ай",
        "j": "дж",
        "k": "к",
        "l": "л",
        "m": "м",
        "n": "н",
        "o": "о",
        "p": "п",
        "q": "кью",
        "r": "р",
        "s": "с",
        "t": "т",
        "u": "ю",
        "v": "в",
        "w": "в",
        "x": "кс",
        "y": "й",
        "z": "з",
    }
    res = ""
    for char in text:
        if char.lower() in en_to_ru:
            res += en_to_ru[char.lower()] if char.islower() else en_to_ru[char.lower()].upper()
        else:
            res += char
    return res


def apply_cyberpunk_dsp(
    audio_data: np.ndarray, sr: int = 24000, fade_time: float = 0.008
) -> np.ndarray:
    fade_length = int(sr * fade_time)
    if len(audio_data) > fade_length * 2:
        fade_in = np.linspace(0, 1, fade_length, dtype=np.float32)
        fade_out = np.linspace(1, 0, fade_length, dtype=np.float32)
        audio_data[:fade_length] *= fade_in
        audio_data[-fade_length:] *= fade_out

    drive_amount = 1.15
    audio_data = audio_data * drive_amount
    audio_data = audio_data - (np.power(audio_data, 3) / 3.0)
    return audio_data


def prepare_text(text):
    clean_text = clean_text_for_tts(verbalize_numbers(text))

    owner_name = str(_settings_getter().get("owner_name", ""))
    owner_pron = getattr(config, "OWNER_PRONUNCIATION", None)

    formatted_pronunciation = auto_accentuate_name(owner_name, owner_pron)

    dict_replacements = {
        r"\bWolfy\b": "в+ульфи",
        r"\bKrest\b": "кр+ест",
        r"\bcs2\b": "кэ эс дв+а",
        r"\bAFK\b": "а фэ к+а",
        r"\bRust\b": "р+аст",
        r"\bDota\b": "д+ота",
        r"\bотошел\b": "отош+ёл",
        r"\bотключен\b": "отключ+ён",
        r"\bвключен\b": "включ+ён",
        r"\bзавершено\b": "завершен+о",
        r"\bзагружена\b": "загр+ужена",
    }

    if owner_name:
        dict_replacements[rf"\b{re.escape(owner_name)}\b"] = formatted_pronunciation

    extra_pronunciations = getattr(config, "EXTRA_PRONUNCIATIONS", {})
    if isinstance(extra_pronunciations, dict):
        dict_replacements.update(extra_pronunciations)

    for word, pronunciation in _settings_getter().get("pronunciations", {}).items():
        clean_text = re.sub(
            r"(?<!\w)" + re.escape(word) + r"(?!\w)",
            lambda match, value=pronunciation: value,
            clean_text,
            flags=re.I,
        )

    for pattern, replacement in dict_replacements.items():
        clean_text = re.sub(pattern, replacement, clean_text, flags=re.IGNORECASE)

    try:
        clean_text = re.sub(
            r"\d+[.,]\d+", lambda m: num2words(m.group().replace(",", "."), lang="ru"), clean_text
        )
        clean_text = re.sub(r"\d+", lambda m: num2words(int(m.group()), lang="ru"), clean_text)
    except Exception:
        pass

    clean_text = re.sub(r"\bодин(?=\s+минута\b)", "одна", clean_text)
    clean_text = re.sub(r"\bдва(?=\s+минуты\b)", "две", clean_text)
    clean_text = transliterate_english(clean_text)
    clean_text = clean_text.strip()

    if clean_text and clean_text[-1] not in [".", "!", "?"]:
        clean_text += "."

    return clean_text


def cached_synthesis(text):
    # Version the cache when pronunciation/audio processing changes.
    key = "2:xenia:24000:" + str(silero_path) + ":" + str(os.path.getmtime(silero_path)) + text
    path = os.path.join(TTS_CACHE_DIR, hashlib.sha256(key.encode("utf-8")).hexdigest() + ".npy")
    if os.path.isfile(path):
        try:
            if os.path.getsize(path) > 12 * 1024 * 1024:
                raise ValueError("Oversized audio cache")
            samples = validate_audio(np.load(path, allow_pickle=False))
            os.utime(path, None)
            return samples
        except (OSError, ValueError, EOFError):
            pass
    samples = synthesize(text)
    try:
        # A crash cannot publish a partial cache entry.
        with open(path + ".tmp", "wb") as output:
            np.save(output, samples, allow_pickle=False)
        os.replace(path + ".tmp", path)
        manage_disk_cache()
    except OSError:
        pass
    return samples


def speak(text: str, cancelled=lambda: False):
    """Speak sentence-sized pieces, keeping CPU/memory and cancellation bounded."""
    global is_speaking
    if not text or model_tts is None or cancelled():
        return
    from speech_tools import speech_chunks

    _cancel_speech.clear()
    is_speaking = True
    if not _settings_getter().get("app_settings", {}).get("voice_headphones", False):
        with _recognizer_lock:
            clear_audio_queue()
            _reset_recognizer()
    try:
        for part in speech_chunks(prepare_text(text)):
            if cancelled() or _cancel_speech.is_set():
                return
            audio_np = cached_synthesis(part)
            if cancelled() or _cancel_speech.is_set():
                return
            settings = _settings_getter().get("app_settings", {})
            if settings.get("voice_effect", True):
                audio_np = apply_cyberpunk_dsp(audio_np.copy())
            volume = max(0, min(100, settings.get("voice_volume", 80))) / 100
            audio_np = audio_np * volume
            # Scale excessive peaks instead of hard clipping syllables.
            peak = float(np.max(np.abs(audio_np)))
            if peak > 0.98:
                audio_np *= 0.98 / peak
            with _play_lock:
                if cancelled() or _cancel_speech.is_set():
                    return
                sd.play(audio_np, 24000)
            sd.wait()
        metrics["tts_error"] = ""
    except Exception as exc:
        metrics["tts_error"] = "Ошибка озвучки: " + str(exc)[:240]
        logging.getLogger(__name__).warning(metrics["tts_error"])
    finally:
        if not _settings_getter().get("app_settings", {}).get("voice_headphones", False):
            # Keep the speaker tail out of recognition without cutting off headset input.
            time.sleep(0.2)
            with _recognizer_lock:
                _reset_recognizer()
                clear_audio_queue()
        is_speaking = False
