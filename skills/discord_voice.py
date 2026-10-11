"""Free local desktop controls, independent of Discord bot/account sessions."""

from media_control import MediaError
from media_desktop import request
from skill_manager import skill

# Small Vosk models sometimes split the app name (reported: «в дс горде»).
# Keep these alternatives inside full microphone commands, never free text.
DISCORD = r"(?:дискорд(?:е)?|дис корд(?:е)?|discord|дс(?: горд(?:е)?)?)"
MICROPHONE = r"(?:мой )?(?:микрофон|микро|мик)"
DESTINATION = rf"(?: в {DISCORD})?"


def _microphone(action):
    try:
        return request("discord_voice", action=action)
    except MediaError as exc:
        return {"status": "error", "message": str(exc)}


@skill(
    keywords=[
        "мут",
        "выключи микро",
        "замуть меня",
        "выключи микрофон в дискорде",
        "замуть меня в дискорде",
        "мут в дискорде",
        "мут дискорд",
    ],
    patterns=[
        rf"(?:выключи|отключи|заглуши) {MICROPHONE}{DESTINATION}",
        rf"{MICROPHONE} (?:выключи|отключи|выкл){DESTINATION}",
        rf"(?:замуть|замьюти|замути)(?: меня| микрофон| микро| мик)?{DESTINATION}",
        rf"мут{DESTINATION}",
    ],
    description="Выключить свой микрофон в Discord, сохранив голосовое управление Крест",
    category="Discord · микрофон",
    examples=["мут", "выключи микро", "замуть меня", "выключи микрофон в дискорде"],
)
def mute_microphone(text=""):
    return _microphone("mute")


@skill(
    keywords=[
        "анмут",
        "размут",
        "включи микро",
        "размуть меня",
        "включи микрофон в дискорде",
        "размуть меня в дискорде",
        "сними мут в дискорде",
        "убери мут в дискорде",
        "анмут дискорд",
    ],
    patterns=[
        rf"(?:включи|подключи) {MICROPHONE}{DESTINATION}",
        rf"{MICROPHONE} (?:включи|вкл){DESTINATION}",
        rf"(?:размуть|размьюти|размути)(?: меня| микрофон| микро| мик)?{DESTINATION}",
        rf"(?:анмут|размут|сними мут|убери мут){DESTINATION}",
    ],
    description="Включить свой микрофон в Discord, не переключая уже включённый",
    category="Discord · микрофон",
    examples=["анмут", "включи микро", "размуть меня", "включи микрофон в дискорде"],
)
def unmute_microphone(text=""):
    return _microphone("unmute")


@skill(
    keywords=[
        "я в муте",
        "проверь микрофон",
        "проверь микрофон в дискорде",
        "я в муте в дискорде",
        "я замьючен в дискорде",
    ],
    patterns=[
        rf"(?:проверь|статус|состояние) (?:микрофон|микрофона|микро){DESTINATION}",
        rf"я (?:в муте|замьючен){DESTINATION}",
        rf"микрофон в {DISCORD} (?:включ[её]н|выключен)",
    ],
    description="Проверить, включён ли свой микрофон в Discord, без переключения",
    category="Discord · микрофон",
    examples=["я в муте", "проверь микрофон", "проверь микрофон в дискорде"],
)
def microphone_status(text=""):
    return _microphone("status")
