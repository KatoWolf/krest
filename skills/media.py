import os
import sys
import re
import time
import ctypes
import asyncio
import logging
import importlib
import webbrowser
from ctypes import cast, POINTER
import voice


try:
    from skill_manager import skill  # type: ignore # pyright: ignore
except ImportError:
    from ..skill_manager import skill  # type: ignore # pyright: ignore

logger = logging.getLogger("SkillMedia")

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt Key
VK_LEFT = 0x25  # Left Arrow
VK_UP = 0x26  # Up Arrow
VK_RIGHT = 0x27  # Right Arrow
VK_DOWN = 0x28  # Down Arrow
VK_B = 0x42  # B Key (Spotify like: Alt + Shift + B)
VK_L = 0x4C  # L Key (VK Music like)
VK_S = 0x53  # S Key (Shuffle)
VK_R = 0x52  # R Key (Repeat)

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF

KEYEVENTF_KEYUP = 0x0002

HAS_WINSDK = False
MediaManager = None

try:
    winsdk_module = importlib.import_module("winsdk.media.control")
    MediaManager = winsdk_module.GlobalSystemMediaTransportControlsSessionManager
    HAS_WINSDK = True
except ImportError:
    logger.warning(
        "winsdk module is not installed. System media session control will use keyboard fallbacks."
    )


class IAudioEndpointVolume(ctypes.c_void_p):
    pass


def set_master_volume_level_scalar(level_percent: float) -> bool:
    """
    Sets system master volume using Windows CoreAudio API via ctypes.
    level_percent: float from 0.0 to 1.0
    """
    try:
        # Ole32 initialization
        ctypes.windll.ole32.CoInitialize(None)

        # CLSID & IID for MMDeviceEnumerator and IAudioEndpointVolume
        from comtypes import CLSCTX_ALL  # type: ignore
        from pycaw.pycaw import (
            AudioUtilities,
            IAudioEndpointVolume as PycawIAudioEndpointVolume,
        )  # type: ignore

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(PycawIAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(PycawIAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, level_percent)), None)
        return True
    except Exception:
        # Fallback if pycaw is not installed: approximate using VK key pulses
        return False


def get_master_volume_level_scalar() -> float:
    """Retrieves current master volume percentage scalar (0.0 to 1.0)."""
    try:
        from comtypes import CLSCTX_ALL  # type: ignore
        from pycaw.pycaw import (
            AudioUtilities,
            IAudioEndpointVolume as PycawIAudioEndpointVolume,
        )  # type: ignore

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(PycawIAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(PycawIAudioEndpointVolume))
        return volume.GetMasterVolumeLevelScalar()
    except Exception:
        return -1.0


def press_vk_code(vk_code: int):
    """Sends a single virtual key press and release event to Windows."""
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


def press_key_combination(keys: list):
    """Sends a combination of keys pressed simultaneously and released in reverse order."""
    for key in keys:
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
    time.sleep(0.03)
    for key in reversed(keys):
        ctypes.windll.user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)


APP_NAMES_MAP = {
    "yandex.yandexmusic": "Яндекс Музыке",
    "yandexmusic": "Яндекс Музыке",
    "spotify": "Spotify",
    "chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "browser": "Яндекс Браузере",
    "yandexbrowser": "Яндекс Браузере",
    "firefox": "Mozilla Firefox",
    "opera": "Opera",
    "vk": "VK Музыке",
    "aimp": "AIMP",
    "vlc": "VLC",
    "foobar2000": "foobar2000",
    "applemusic": "Apple Music",
    "itunes": "iTunes",
    "youtubemusic": "YouTube Music",
}


def resolve_app_name(app_id: str) -> str:
    """Translates raw Windows AppUserModelId into human-friendly app name."""
    if not app_id:
        return "медиаплеере"

    app_id_lower = app_id.lower()
    for key, name in APP_NAMES_MAP.items():
        if key in app_id_lower:
            return name

    clean_id = app_id.split("!")[0].split("\\")[-1].replace(".exe", "")
    return clean_id.capitalize() if clean_id else "медиаплеере"


async def _async_get_media_properties():
    """Queries Windows System Media Transport Controls for current track, artist, timeline, and player source."""
    if not HAS_WINSDK:
        return None

    try:
        sessions = await MediaManager.request_async()
        current_session = sessions.get_current_session()
        if current_session:
            app_id = current_session.source_app_user_model_id
            info = await current_session.try_get_media_properties_async()
            playback = current_session.get_playback_info()
            timeline = current_session.get_timeline_properties()

            is_playing = False
            if playback:
                # PlaybackStatus: 4 == Playing, 5 == Paused
                is_playing = playback.playback_status == 4

            pos_str = ""
            dur_str = ""
            if timeline:
                try:
                    pos_sec = int(timeline.position.total_seconds())
                    dur_sec = int(timeline.end_time.total_seconds())
                    if dur_sec > 0:
                        pos_str = f"{pos_sec // 60}:{pos_sec % 60:02d}"
                        dur_str = f"{dur_sec // 60}:{dur_sec % 60:02d}"
                except Exception:
                    pass

            if info:
                return {
                    "title": info.title,
                    "artist": info.artist,
                    "album": info.album_title,
                    "app_id": app_id,
                    "app_name": resolve_app_name(app_id),
                    "is_playing": is_playing,
                    "position": pos_str,
                    "duration": dur_str,
                    "session": current_session,
                }
    except Exception as e:
        logger.error(f"Failed to query Windows Media Session: {e}")

    return None


def get_now_playing_data():
    """Synchronously retrieves structured media metadata from current active session."""
    if not HAS_WINSDK:
        return {"error": "winsdk_not_installed"}

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        data = loop.run_until_complete(_async_get_media_properties())
        loop.close()

        if data:
            data["error"] = None
            return data
        return {"error": "no_active_media"}
    except Exception as e:
        logger.error(f"Error reading media properties: {e}")
        return {"error": str(e)}


async def _async_control_session(action: str):
    """Executes direct WinRT session control commands (play, pause, next, prev)."""
    if not HAS_WINSDK:
        return False
    try:
        sessions = await MediaManager.request_async()
        session = sessions.get_current_session()
        if session:
            if action == "next":
                return await session.try_skip_next_async()
            elif action == "prev":
                return await session.try_skip_previous_async()
            elif action == "play_pause":
                return await session.try_toggle_play_pause_async()
            elif action == "play":
                return await session.try_play_async()
            elif action == "pause":
                return await session.try_pause_async()
    except Exception as e:
        logger.error(f"Failed to execute WinRT session action {action}: {e}")
    return False


def control_media_session(action: str):
    """Wrapper to run async WinRT media control command."""
    if not HAS_WINSDK:
        return False
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(_async_control_session(action))
        loop.close()
        return success
    except Exception:
        return False


@skill(
    keywords=[
        "следующий трек",
        "вперед трек",
        "следующая песня",
        "переключи трек",
        "следующий",
    ],
    description="Переключить воспроизведение на следующий трек",
    category="Media",
)
def next_track(text: str = ""):
    if not control_media_session("next"):
        press_vk_code(VK_MEDIA_NEXT_TRACK)
    voice.speak("Включила следующий трек.")


@skill(
    keywords=[
        "предыдущий трек",
        "назад трек",
        "предыдущая песня",
        "верни трек",
        "предыдущий",
    ],
    description="Переключить воспроизведение на предыдущий трек",
    category="Media",
)
def prev_track(text: str = ""):
    if not control_media_session("prev"):
        press_vk_code(VK_MEDIA_PREV_TRACK)
    voice.speak("Вернула предыдущий трек.")


@skill(
    keywords=[
        "пауза",
        "стоп музыка",
        "продолжи",
        "плей",
        "поставь на паузу",
        "включи музыку",
        "останови музыку",
        "сними с паузы",
    ],
    description="Переключить паузу / воспроизведение",
    category="Media",
)
def toggle_play_pause(text: str = ""):
    if not control_media_session("play_pause"):
        press_vk_code(VK_MEDIA_PLAY_PAUSE)
    voice.speak("Готово.")


@skill(
    keywords=[
        "лайк",
        "поставь лайк",
        "оцени трек",
        "лайкни трек",
        "сохрани трек",
        "добавь в избранное",
        "мне нравится",
    ],
    description="Умный лайк: адаптивно подстраивает сочетание клавиш под активный плеер",
    category="Media",
)
def like_track(text: str = ""):
    data = get_now_playing_data()
    app_id = (data.get("app_id") or "").lower()

    if "spotify" in app_id:
        # Spotify Desktop: Alt + Shift + B
        press_key_combination([VK_MENU, VK_SHIFT, VK_B])
        voice.speak("Отправила отметку «Нравится» в Spotify.")
    elif "yandex" in app_id or "яндекс" in data.get("app_name", "").lower():
        # Yandex Music Desktop / Extension: Shift + Alt + Right Arrow
        press_key_combination([VK_SHIFT, VK_MENU, VK_RIGHT])
        voice.speak("Добавила трек в любимые Яндекс Музыки.")
    elif "vk" in app_id:
        # VK Music extension or app: Alt + L
        press_key_combination([VK_MENU, VK_L])
        voice.speak("Сохранила трек в VK Музыке.")
    else:
        # General default fallback for Yandex / Web extensions
        press_key_combination([VK_SHIFT, VK_MENU, VK_RIGHT])
        voice.speak("Отправила команду лайка.")


@skill(
    keywords=[
        "дизлайк",
        "поставь дизлайк",
        "не нравится",
        "удали из избранного",
        "пропусти и заблокируй",
    ],
    description="Умный дизлайк: блокирует трек или убирает его из понравившихся",
    category="Media",
)
def dislike_track(text: str = ""):
    data = get_now_playing_data()
    app_id = (data.get("app_id") or "").lower()

    if "yandex" in app_id or "яндекс" in data.get("app_name", "").lower():
        # Yandex Music Desktop / Extension Dislike: Shift + Alt + Left Arrow
        press_key_combination([VK_SHIFT, VK_MENU, VK_LEFT])
        voice.speak("Поставила дизлайк в Яндекс Музыке.")
    else:
        press_key_combination([VK_SHIFT, VK_MENU, VK_LEFT])
        voice.speak("Отправила отметку дизлайка.")


def extract_number_from_text(text: str) -> int:
    """Extracts the first numeric integer value found in text string."""
    numbers = re.findall(r"\d+", text)
    if numbers:
        return int(numbers[0])
    return -1


@skill(
    patterns=[
        r"громкость (на )?(?P<level>\d+)( процента| процентов|%)?",
        r"поставь громкость (на )?(?P<level>\d+)",
        r"установи громкость (на )?(?P<level>\d+)",
        r"звук на (?P<level>\d+)",
    ],
    keywords=["установи громкость", "поставь громкость", "громкость на"],
    description="Установить точный процент системной громкости (от 0 до 100)",
    category="Media",
)
def set_exact_volume(text: str = "", level: str = ""):
    val = extract_number_from_text(text if not level else level)
    if val < 0 or val > 100:
        voice.speak("Укажите уровень громкости от нуля до ста процентов.")
        return

    scalar = val / 100.0
    if set_master_volume_level_scalar(scalar):
        voice.speak(f"Установила громкость на {val} процентов.")
    else:
        # Fallback using standard volume step keys
        current_scalar = get_master_volume_level_scalar()
        target_steps = val // 2
        for _ in range(50):
            press_vk_code(VK_VOLUME_DOWN)
        for _ in range(target_steps):
            press_vk_code(VK_VOLUME_UP)
        voice.speak(f"Установила громкость примерно на {val} процентов.")


@skill(
    keywords=[
        "громкость на максимум",
        "максимальная громкость",
        "полная громкость",
        "громкость сто",
    ],
    description="Установить громкость на 100%",
    category="Media",
)
def set_volume_max(text: str = ""):
    if set_master_volume_level_scalar(1.0):
        voice.speak("Установила максимальную громкость.")
    else:
        for _ in range(50):
            press_vk_code(VK_VOLUME_UP)
        voice.speak("Максимальная громкость.")


@skill(
    patterns=[
        r"громче( на (?P<step>\d+))?",
        r"прибавь звук( на (?P<step>\d+))?",
        r"увеличь громкость( на (?P<step>\d+))?",
        r"сделай громче( на (?P<step>\d+))?",
    ],
    keywords=["громче", "прибавь звук", "увеличь громкость", "сделай громче"],
    description="Увеличить системную громкость",
    category="Media",
)
def volume_up(text: str = "", step: str = ""):
    val = extract_number_from_text(text)
    steps = max(1, min(25, val // 2 if val > 0 else 5))
    for _ in range(steps):
        press_vk_code(VK_VOLUME_UP)
    voice.speak("Сделала громче.")


@skill(
    patterns=[
        r"тише( на (?P<step>\d+))?",
        r"убавь звук( на (?P<step>\d+))?",
        r"уменьши громкость( на (?P<step>\d+))?",
        r"сделай тише( на (?P<step>\d+))?",
    ],
    keywords=["тише", "убавь звук", "уменьши громкость", "сделай тише"],
    description="Уменьшить системную громкость",
    category="Media",
)
def volume_down(text: str = "", step: str = ""):
    val = extract_number_from_text(text)
    steps = max(1, min(25, val // 2 if val > 0 else 5))
    for _ in range(steps):
        press_vk_code(VK_VOLUME_DOWN)
    voice.speak("Сделала тише.")


@skill(
    keywords=[
        "выключи звук",
        "включи звук",
        "без звука",
        "мут звука",
        "заглуши звук",
        "включи звук обратно",
    ],
    description="Быстрое включение/выключение полного звука ПК (Mute Toggle)",
    category="Media",
)
def toggle_system_mute(text: str = ""):
    press_vk_code(VK_VOLUME_MUTE)
    voice.speak("Переключила режим звука.")


@skill(
    keywords=["перемотай вперед", "вперед на 10 секунд", "перемотка вперед"],
    description="Перемотать текущее воспроизведение немного вперед (Right Arrow)",
    category="Media",
)
def seek_forward(text: str = ""):
    press_key_combination([VK_RIGHT])
    voice.speak("Перемотала вперед.")


@skill(
    keywords=["перемотай назад", "назад на 10 секунд", "перемотка назад"],
    description="Перемотать текущее воспроизведение немного назад (Left Arrow)",
    category="Media",
)
def seek_backward(text: str = ""):
    press_key_combination([VK_LEFT])
    voice.speak("Перемотала назад.")


@skill(
    keywords=[
        "что играет",
        "какая песня играет",
        "название трека",
        "какой трек играет",
        "кто поет",
        "где играет музыка",
        "что за трек",
    ],
    description="Определение названия, исполнителя и источника воспроизведения",
    category="Media",
)
def tell_current_playing_track(text: str = ""):
    data = get_now_playing_data()

    if data.get("error") == "winsdk_not_installed":
        voice.speak("Для подробного определения треков установите пакет winsdk.")
        return

    if data.get("error") or not data.get("title"):
        voice.speak("Не удалось определить текущий трек или сейчас ничего не играет.")
        return

    title = data["title"]
    artist = data.get("artist")
    app_name = data.get("app_name", "плеере")

    if artist:
        msg = f"Сейчас в {app_name} играет {artist} — {title}."
    else:
        msg = f"Сейчас в {app_name} играет {title}."

    if data.get("position") and data.get("duration"):
        msg += f" Прошло {data['position']} из {data['duration']}."

    voice.speak(msg)


@skill(
    keywords=[
        "сколько идет трек",
        "длительность трека",
        "сколько осталось до конца трека",
        "какая длительность",
    ],
    description="Узнать текущее время воспроизведения и общую длительность трека",
    category="Media",
)
def tell_track_duration(text: str = ""):
    data = get_now_playing_data()
    pos = data.get("position")
    dur = data.get("duration")

    if pos and dur:
        voice.speak(f"Текущая позиция: {pos}, общая длительность: {dur}.")
    else:
        voice.speak("Не удалось определить длительность активного трека.")


@skill(
    keywords=["открой яндекс музыку", "включи яндекс музыку", "запусти яндекс музыку"],
    description="Открыть Яндекс Музыку в браузере или приложении",
    category="Media",
)
def open_yandex_music(text: str = ""):
    webbrowser.open("https://music.yandex.ru")
    voice.speak("Открываю Яндекс Музыку.")


@skill(
    keywords=["открой спотифай", "включи спотифай", "запусти спотифай", "open spotify"],
    description="Открыть веб-версию или приложение Spotify",
    category="Media",
)
def open_spotify(text: str = ""):
    webbrowser.open("https://open.spotify.com")
    voice.speak("Открываю Spotify.")


@skill(
    keywords=["открой ютуб музыку", "включи ютуб музыку", "youtube music"],
    description="Открыть веб-версию YouTube Music",
    category="Media",
)
def open_youtube_music(text: str = ""):
    webbrowser.open("https://music.youtube.com")
    voice.speak("Открываю YouTube Music.")


@skill(
    keywords=["открой вк музыку", "включи вк музыку", "запусти вк музыку"],
    description="Открыть раздел музыки ВКонтакте",
    category="Media",
)
def open_vk_music(text: str = ""):
    webbrowser.open("https://vk.com/music")
    voice.speak("Открываю VK Музыку.")
