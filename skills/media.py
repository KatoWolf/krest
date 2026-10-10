"""Music skills use the selected Windows media session and explicit outcomes."""

from contextlib import contextmanager
import re
import webbrowser
from skill_manager import skill
from app_state import get_state
import media_control


def preferred_player():
    return get_state().config_getter().get("media", {}).get("preferred_app", "")


def _action(action, seconds=10):
    try:
        if action in ("like", "unlike", "dislike"):
            return media_control.rating(action, preferred_player())
        return media_control.control(action, preferred_player(), seconds)
    except media_control.MediaError as exc:
        return {"status": "error", "message": str(exc)}


def get_now_playing_data():
    try:
        return media_control.query(preferred_player())
    except media_control.MediaError as exc:
        return {"error": str(exc)}


@skill(
    keywords=["следующий трек", "вперед трек", "следующая песня", "переключи трек", "следующий"],
    description="Следующий трек в выбранном плеере",
    category="Media",
)
def next_track(text=""):
    return _action("next")


@skill(
    keywords=["предыдущий трек", "назад трек", "предыдущая песня", "верни трек", "предыдущий"],
    description="Предыдущий трек в выбранном плеере",
    category="Media",
)
def prev_track(text=""):
    return _action("prev")


@skill(
    keywords=["пауза", "стоп музыка", "поставь на паузу", "останови музыку"],
    description="Поставить музыку на паузу (повторная команда не включает её)",
    category="Media",
)
def pause_music(text=""):
    return _action("pause")


@skill(
    keywords=["продолжи", "продолжи музыку", "плей", "включи музыку", "сними с паузы"],
    description="Продолжить воспроизведение музыки",
    category="Media",
)
def play_music(text=""):
    return _action("play")


@skill(
    keywords=["переключи паузу"],
    description="Переключить воспроизведение / паузу",
    category="Media",
)
def toggle_play_pause(text=""):
    return _action("play_pause")


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
    description="Отметить текущий трек через доступную кнопку настольного плеера",
    category="Media",
)
def like_track(text=""):
    return _action("like")


@skill(
    keywords=["убери лайк", "удали из избранного"],
    description="Убрать отметку «Нравится» у текущего трека",
    category="Media",
)
def unlike_track(text=""):
    return _action("unlike")


@skill(
    keywords=["дизлайк", "поставь дизлайк", "не нравится"],
    description="Нажать доступную кнопку «Не нравится» текущего трека",
    category="Media",
)
def dislike_track(text=""):
    return _action("dislike")


@contextmanager
def endpoint_volume():
    import comtypes
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER

    comtypes.CoInitialize()
    try:
        speakers = AudioUtilities.GetSpeakers()
        if hasattr(speakers, "EndpointVolume"):
            yield speakers.EndpointVolume
        else:
            yield cast(
                speakers.Activate(IAudioEndpointVolume._iid_, comtypes.CLSCTX_ALL, None),
                POINTER(IAudioEndpointVolume),
            )
    finally:
        comtypes.CoUninitialize()


def _volume(action, value=0):
    try:
        with endpoint_volume() as volume:
            if action in ("mute", "unmute"):
                volume.SetMute(action == "mute", None)
                return "Системный звук выключен." if action == "mute" else "Системный звук включён."
            level = value if action == "set" else volume.GetMasterVolumeLevelScalar() * 100 + value
            level = max(0, min(100, level))
            volume.SetMasterVolumeLevelScalar(level / 100, None)
            return f"Системная громкость: {round(level)}%."
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Не удалось изменить системную громкость. Проверьте устройство вывода и requirements-windows.txt. {exc}",
        }


def _number(value):
    from skills.timers_alarms import words_to_number

    value = re.sub(r"\s*(процента|процентов|процент|%)$", "", str(value)).strip()
    return int(value) if value.isdigit() else words_to_number(value)


@skill(
    patterns=[r"(?:поставь |установи )?громкость (?:на )?(?P<level>.+)", r"звук на (?P<level>.+)"],
    examples=["громкость на двадцать пять"],
    description="Установить системную громкость 0–100, цифрами или словами",
    category="Media",
)
def set_exact_volume(text="", level=""):
    val = _number(level)
    if val is None or not 0 <= val <= 100:
        return {"status": "error", "message": "Укажите громкость от нуля до ста процентов."}
    return _volume("set", val)


@skill(
    keywords=[
        "громкость на максимум",
        "максимальная громкость",
        "полная громкость",
        "громкость сто",
    ],
    description="Системная громкость 100%",
    category="Media",
)
def set_volume_max(text=""):
    return _volume("set", 100)


def _relative_volume(step, sign):
    value = _number(step) if step else 10
    if value is None or not 1 <= value <= 100:
        return {"status": "error", "message": "Укажите шаг громкости от одного до ста."}
    return _volume("relative", sign * value)


@skill(
    patterns=[r"(?:громче|прибавь звук|увеличь громкость|сделай громче)(?: на (?P<step>.+))?"],
    keywords=["громче", "прибавь звук", "увеличь громкость", "сделай громче"],
    description="Увеличить системную громкость на заданное число процентов (обычно 10)",
    category="Media",
)
def volume_up(text="", step=""):
    return _relative_volume(step, 1)


@skill(
    patterns=[r"(?:тише|убавь звук|уменьши громкость|сделай тише)(?: на (?P<step>.+))?"],
    keywords=["тише", "убавь звук", "уменьши громкость", "сделай тише"],
    description="Уменьшить системную громкость на заданное число процентов (обычно 10)",
    category="Media",
)
def volume_down(text="", step=""):
    return _relative_volume(step, -1)


@skill(
    keywords=["выключи звук", "без звука", "мут звука", "заглуши звук"],
    description="Выключить системный звук",
    category="Media",
)
def mute_system(text=""):
    return _volume("mute")


@skill(
    keywords=["включи звук", "включи звук обратно"],
    description="Включить системный звук",
    category="Media",
)
def unmute_system(text=""):
    return _volume("unmute")


@skill(
    keywords=["перемотай вперед", "вперед на 10 секунд", "перемотка вперед"],
    description="Перемотать выбранный трек на 10 секунд вперёд",
    category="Media",
)
def seek_forward(text=""):
    return _action("seek", 10)


@skill(
    keywords=["перемотай назад", "назад на 10 секунд", "перемотка назад"],
    description="Перемотать выбранный трек на 10 секунд назад",
    category="Media",
)
def seek_backward(text=""):
    return _action("seek", -10)


def _clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


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
    description="Название, исполнитель и источник выбранной медиасессии",
    category="Media",
)
def tell_current_playing_track(text=""):
    data = get_now_playing_data()
    if data.get("error"):
        return {"status": "error", "message": data["error"]}
    return f"{data['app_name']}: {data['artist']} — {data['title'] or 'Название не передано'}. " + (
        "Играет." if data["is_playing"] else "На паузе или остановлено."
    )


@skill(
    keywords=[
        "сколько идет трек",
        "длительность трека",
        "сколько осталось до конца трека",
        "какая длительность",
    ],
    description="Позиция, длительность и остаток трека",
    category="Media",
)
def tell_track_duration(text=""):
    data = get_now_playing_data()
    if data.get("error") or not data.get("duration_seconds"):
        return {
            "status": "error",
            "message": data.get("error") or "Плеер не передаёт длительность.",
        }
    position, duration = data["position_seconds"], data["duration_seconds"]
    return f"Прошло {_clock(position)} из {_clock(duration)}. Осталось {_clock(max(0, duration - position))}."


def _open(url, name):
    if not webbrowser.open(url):
        return {"status": "error", "message": "Не удалось открыть браузер."}
    return "Открываю " + name + "."


@skill(
    keywords=["открой яндекс музыку", "включи яндекс музыку", "запусти яндекс музыку"],
    description="Открыть Яндекс Музыку в браузере",
    category="Media",
)
def open_yandex_music(text=""):
    return _open("https://music.yandex.ru", "Яндекс Музыку")


@skill(
    keywords=["открой спотифай", "включи спотифай", "запусти спотифай", "open spotify"],
    description="Открыть Spotify в браузере",
    category="Media",
)
def open_spotify(text=""):
    return _open("https://open.spotify.com", "Spotify")


@skill(
    keywords=["открой ютуб музыку", "включи ютуб музыку", "youtube music"],
    description="Открыть YouTube Music в браузере",
    category="Media",
)
def open_youtube_music(text=""):
    return _open("https://music.youtube.com", "YouTube Music")


@skill(
    keywords=["открой вк музыку", "включи вк музыку", "запусти вк музыку"],
    description="Открыть VK Музыку в браузере",
    category="Media",
)
def open_vk_music(text=""):
    return _open("https://vk.com/music", "VK Музыку")
