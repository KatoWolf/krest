"""Free local desktop controls, independent of Discord bot/account sessions."""

from media_control import MediaError
from media_desktop import request
from skill_manager import skill

DISCORD = r"(?:дискорд(?:е)?|discord|дс)"


def _microphone(action):
    try:
        return request("discord_voice", action=action)
    except MediaError as exc:
        return {"status": "error", "message": str(exc)}


@skill(
    keywords=[
        "выключи микрофон в дискорде",
        "замуть меня в дискорде",
        "мут в дискорде",
        "мут дискорд",
    ],
    patterns=[
        rf"(?:выключи|отключи|заглуши) (?:мой )?(?:микрофон|микро|мик) в {DISCORD}",
        rf"(?:замуть|замьюти|замути)(?: меня| микрофон| микро| мик)? в {DISCORD}",
    ],
    description="Выключить свой микрофон в Discord, сохранив голосовое управление Крест",
    category="Discord · микрофон",
    examples=["выключи микрофон в дискорде", "замуть меня в дискорде"],
)
def mute_microphone(text=""):
    return _microphone("mute")


@skill(
    keywords=[
        "включи микрофон в дискорде",
        "размуть меня в дискорде",
        "сними мут в дискорде",
        "убери мут в дискорде",
        "анмут дискорд",
    ],
    patterns=[
        rf"(?:включи|подключи) (?:мой )?(?:микрофон|микро|мик) в {DISCORD}",
        rf"(?:размуть|размьюти|размути)(?: меня| микрофон| микро| мик)? в {DISCORD}",
    ],
    description="Включить свой микрофон в Discord, не переключая уже включённый",
    category="Discord · микрофон",
    examples=["включи микрофон в дискорде", "размуть меня в дискорде"],
)
def unmute_microphone(text=""):
    return _microphone("unmute")


@skill(
    keywords=["проверь микрофон в дискорде", "я в муте в дискорде", "я замьючен в дискорде"],
    patterns=[
        rf"(?:проверь|статус|состояние) (?:микрофон|микрофона|микро) в {DISCORD}",
        rf"микрофон в {DISCORD} (?:включ[её]н|выключен)",
    ],
    description="Проверить, включён ли свой микрофон в Discord, без переключения",
    category="Discord · микрофон",
    examples=["проверь микрофон в дискорде"],
)
def microphone_status(text=""):
    return _microphone("status")
