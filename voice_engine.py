import os
import sys
import json
import queue
import torch
import sounddevice as sd
import vosk
from num2words import num2words
import re
import time
import logging
import importlib
import numpy as np
import hashlib

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


try:
    from fuzzywuzzy import fuzz

    FUZZY_ENABLED = True
except ImportError:
    FUZZY_ENABLED = False

torch.set_num_threads(4)
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
q = queue.Queue()
is_speaking = False
stream = None
last_mic_error_time = 0

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

TTS_CACHE_DIR = os.path.join(BASE_DIR, "tts_cache")
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

    if "+" in ru_text:
        return ru_text

    vowels = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
    words = ru_text.split()
    accented_words = []

    for word in words:
        vowel_indices = [i for i, c in enumerate(word) if c in vowels]
        if not vowel_indices:
            accented_words.append(word)
        elif len(vowel_indices) == 1:
            idx = vowel_indices[0]
            accented_words.append(word[: idx + 1] + "+" + word[idx + 1 :])
        else:
            idx = vowel_indices[-2] if len(vowel_indices) >= 2 else vowel_indices[0]
            accented_words.append(word[: idx + 1] + "+" + word[idx + 1 :])

    return " ".join(accented_words)


def clear_audio_queue():
    """Полная очистка очереди аудиоданных."""
    with q.mutex:
        q.queue.clear()


def manage_disk_cache(max_files=200):
    """Очистка старого кэша озвучки."""
    try:
        files = [
            os.path.join(TTS_CACHE_DIR, f) for f in os.listdir(TTS_CACHE_DIR) if f.endswith(".npy")
        ]
        if len(files) > max_files:
            files.sort(key=os.path.getatime)
            for f in files[:-max_files]:
                try:
                    os.remove(f)
                except Exception:
                    pass
    except Exception:
        pass


# --- Инициализация нейросетей ---

print("--- [VOICE ENGINE]: Загрузка нейронных связей слуха (Vosk)... ---")
rec = None
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

    model_vosk = vosk.Model(vosk_path)
    rec = vosk.KaldiRecognizer(model_vosk, SAMPLE_RATE)
    rec.SetWords(False)
except Exception as e:
    print(f"--- [CRITICAL]: Ошибка загрузки Vosk. Путь: {vosk_path} | Ошибка: {e} ---")

print("--- [VOICE ENGINE]: Инициализация вокального аппарата V4... ---")
device = torch.device("cpu")
model_tts = None

if os.path.exists(silero_path):
    try:
        model_tts = torch.package.PackageImporter(silero_path).load_pickle("tts_models", "model")
        model_tts.to(device)
    except Exception:
        model_tts = None

if model_tts is None:
    print("--- [VOICE]: Добавьте доверенную модель Silero в model_silero.pt ---")

if model_tts is not None:
    print("--- [VOICE ENGINE]: Аппаратный прогрев нейросети (Pre-warming)... ---")
    try:
        model_tts.apply_tts(text=".", speaker="xenia", sample_rate=24000)
    except Exception:
        pass
    print("✅ [VOICE ENGINE]: Системы готовы к мгновенному ответу!")

# --- Захват и обработка звука ---


def audio_callback(indata, frames, time_info, status):
    """Callback-функция sounddevice для сбора аудио с микрофона."""
    global is_speaking

    if is_speaking:
        return

    if q.qsize() > 15:
        try:
            q.get_nowait()
        except Exception:
            pass

    q.put(bytes(indata))


def start_microphone() -> bool:
    """Безопасная инициализация / перезапуск микрофона."""
    global stream, last_mic_error_time

    # Защита от слишком частых попыток перезапуска при сбое устройства
    if time.time() - last_mic_error_time < 1.0:
        return False

    try:
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            stream = None

        settings = config.reload_config().get("app_settings", {})
        selected = settings.get("microphone", "")
        stream = sd.RawInputStream(
            device=int(selected) if str(selected).isdigit() else (selected or None),
            samplerate=SAMPLE_RATE,
            blocksize=4000,
            dtype="int16",
            channels=1,
            callback=audio_callback,
        )
        stream.start()
        return True
    except Exception as e:
        last_mic_error_time = time.time()
        print(f"\n--- [MIC ERROR]: Ошибка захвата микрофона: {e} ---")
        stream = None
        return False


def fix_wake_word(text: str) -> str:
    """Коррекция фонетических ошибок при произношении WAKE_WORD."""
    if not text:
        return text

    wake_word = get_wake_word()
    phonetic_errors = [
        "трест",
        "арест",
        "квест",
        "крем",
        "хлест",
        "пресс",
        "врез",
        "крас",
        "крэст",
        "кресло",
        "рез",
        "тест",
        "блеск",
        "крес",
        "кресс",
        "лес",
        "жест",
        "перст",
        "крепость",
        "крекер",
        "треск",
        "вмест",
        "крест",
        "резь",
        "вместе",
        "вест",
        "шерсть",
        "шест",
        "кредо",
        "грех",
        "кренит",
        "лесть",
        "весть",
        "месть",
        "рест",
    ]
    words = text.split()
    first_word = words[0]

    if first_word in phonetic_errors:
        words[0] = wake_word
        return " ".join(words)

    if FUZZY_ENABLED:
        if fuzz.ratio(first_word, wake_word) >= 75:
            words[0] = wake_word
            return " ".join(words)

    return text


def listen() -> str:
    """Главный цикл прослушивания входящего потока."""
    global stream, rec

    if rec is None:
        time.sleep(0.1)
        return ""

    # Автоматическое пересоздание упавшего/отключенного потока
    if stream is None or not stream.active:
        if not start_microphone():
            time.sleep(0.5)
            return ""

    while not q.empty():
        try:
            data = q.get_nowait()
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "").strip()
                if text:
                    text = text.lower()
                    text = fix_wake_word(text)

                    replacements = {
                        "я африка": "я афк",
                        "я африку": "я афк",
                        "крест я африка": f"{get_wake_word()} я афк",
                        "африка режим": "афк режим",
                        "африка": "афк",
                        "я а ф к": "я афк",
                        "я отелло": "я отошел",
                        "я ушел": "я отошел",
                        "протокол ходинг": "протокол кодинг",
                        "режим программиста": "протокол кодинг",
                        "забудь дискорд": "замуть дискорд",
                        "замутить дискорд": "замуть дискорд",
                        "верни дис": "верни дискорд",
                        "закрой дис": "закрой дискорд",
                        "убей дис": "закрой дискорд",
                        "моя ванна": "моя волна",
                        "мою ванну": "мою волну",
                        "тише музыку": "тише",
                        "громче музыку": "громче",
                        "сделай тише": "тише",
                        "заткнись": "без звука",
                        "на паузу": "пауза",
                        "продолжить": "продолжи",
                        "запусти раз": "запусти раст",
                        "открой раз": "запусти раст",
                        "запусти дату": "запусти доту",
                        "запусти dota": "запусти доту",
                        "запусти cs": "запусти кс",
                        "закрой steam": "закрой стим",
                        "закрой с тем": "закрой стим",
                        "блок нот": "блокнот",
                        "нужницы": "ножницы",
                        "открой об дату": "открой аппдату",
                        "об дату": "аппдату",
                        "локал ап дату": "локал аппдату",
                        "убей зависещее": "убей зависшее",
                        "альт таб": "сверни окно",
                        "сверни всё": "рабочий стол",
                        "орел или решка": "орёл или решка",
                        "засеки время": "таймер",
                    }

                    custom_replacements = (
                        getattr(config, "COMMAND_REPLACEMENTS", {}) if config else {}
                    )
                    if isinstance(custom_replacements, dict):
                        replacements.update(custom_replacements)

                    for wrong in sorted(replacements.keys(), key=len, reverse=True):
                        if wrong in text:
                            pattern = r"\b" + re.escape(wrong) + r"\b"
                            text = re.sub(pattern, replacements[wrong], text)

                    print(f"\r[Распознано]: {text:<40}")
                    return text
            else:
                partial = json.loads(rec.PartialResult())
                part_text = partial.get("partial", "").strip()
                if part_text:
                    print(f"\r[Слушаю...]: {part_text:<40}", end="", flush=True)
        except queue.Empty:
            break
        except Exception as e:
            print(f"\n--- [LISTEN ERROR]: {e} ---")
            break

    return ""


# --- Синтез речи (TTS) ---


def inject_human_breaths(text: str) -> str:
    words = text.split()
    if len(words) > 8:
        if not any(p in words[4] for p in [",", ".", "!", "?", "-", ":"]):
            words[4] += ","
    return " ".join(words)


def clean_text_for_tts(text: str) -> str:
    text = re.sub(r"[*_`~\[\]\(\)\{\}]", "", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r" - ", ", ", text)
    text = re.sub(r"[^а-яА-ЯёЁa-zA-Z0-9\s\.,!?\:\;+]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return inject_human_breaths(text)


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
        r"\bcpu\b": "ц э пэ +у",
        r"\bram\b": "р+ам",
        r"\bgpu\b": "вид+юха",
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
    audio_data: np.ndarray, sr: int = 24000, fade_time: float = 0.03
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


def speak(text: str):
    """Синтез речи и воспроизведение ответа."""
    global is_speaking, model_tts, rec
    if not text or model_tts is None:
        return

    is_speaking = True
    clear_audio_queue()

    clean_text = clean_text_for_tts(text)

    owner_name = str(getattr(config, "OWNER_NAME", "KatoWolf"))
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

    for pattern, replacement in dict_replacements.items():
        clean_text = re.sub(pattern, replacement, clean_text, flags=re.IGNORECASE)

    try:
        clean_text = re.sub(r"\d+", lambda m: num2words(int(m.group()), lang="ru"), clean_text)
    except Exception:
        pass

    clean_text = transliterate_english(clean_text)
    clean_text = clean_text.strip()

    if clean_text and clean_text[-1] not in [".", "!", "?"]:
        clean_text += "."

    try:
        hash_name = hashlib.md5(clean_text.encode("utf-8")).hexdigest()
        cache_file_path = os.path.join(TTS_CACHE_DIR, f"{hash_name}.npy")

        audio_np = None

        if os.path.exists(cache_file_path):
            try:
                with torch.no_grad():
                    audio_np = np.load(cache_file_path)
                os.utime(cache_file_path, None)
            except Exception:
                audio_np = None

        if audio_np is None:
            with torch.no_grad():
                try:
                    audio_tensor = model_tts.apply_tts(
                        text=clean_text,
                        speaker="xenia",
                        sample_rate=24000,
                        put_accent=True,
                        put_yo=True,
                    )
                except Exception:
                    audio_tensor = model_tts.apply_tts(
                        text=clean_text,
                        speaker="kseniya",
                        sample_rate=24000,
                        put_accent=True,
                        put_yo=True,
                    )
                audio_np = audio_tensor.numpy()

            try:
                np.save(cache_file_path, audio_np)
                manage_disk_cache(max_files=200)
            except Exception:
                pass

        audio_np = apply_cyberpunk_dsp(audio_np, sr=24000, fade_time=0.03)

        volume = config.reload_config().get("app_settings", {}).get("voice_volume", 80) / 100
        if volume != 1.0:
            audio_np = audio_np * volume
            audio_np = np.clip(audio_np, -1.0, 1.0)

        sd.play(audio_np, 24000)
        sd.wait()

    except Exception as e:
        print(f"\n--- [Ошибка Neural TTS]: {e} ---")
    finally:
        # Пауза перед включением микрофона, чтобы колонки не перехватили хвост речи
        time.sleep(0.2)
        if rec is not None:
            rec.Reset()
        clear_audio_queue()
        is_speaking = False

        print("\n--- [Сенсоры Активны]: Ожидаю голосовых команд... ---" + " " * 15)
