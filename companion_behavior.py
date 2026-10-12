"""Small deterministic character policies; keep all factual/error text intact."""

from personality import PERSONALITIES

BEHAVIORS = {
    "friendly": {
        "sentences": 3,
        "initiative": True,
        "listen": "Я здесь. Слушаю тебя.",
        "confirm": "Ты имеешь в виду «{command}»?",
    },
    "concise": {
        "sentences": 1,
        "initiative": False,
        "listen": "Слушаю.",
        "confirm": "«{command}»?",
    },
    "lively": {
        "sentences": 3,
        "initiative": True,
        "listen": "На связи! Что делаем?",
        "confirm": "Делаем «{command}», верно?",
    },
    "calm": {
        "sentences": 2,
        "initiative": False,
        "listen": "Я рядом. Говори.",
        "confirm": "Уточню: «{command}»?",
    },
    "tactical": {
        "sentences": 1,
        "initiative": False,
        "listen": "Принимаю команду.",
        "confirm": "Подтверди: «{command}».",
    },
    "ironic": {
        "sentences": 2,
        "initiative": True,
        "listen": "Уши на месте. Слушаю.",
        "confirm": "Проверю догадку: «{command}»?",
    },
    "gothic": {
        "sentences": 2,
        "initiative": False,
        "listen": "Хранительница слушает.",
        "confirm": "Верно ли услышала: «{command}»?",
    },
}


def policy(settings, gaming=False):
    style = settings.get("profile", {}).get("personality", "friendly")
    value = dict(BEHAVIORS.get(style, BEHAVIORS["friendly"]))
    if gaming:
        value.update(sentences=1, initiative=False, listen="Слушаю.")
    return value


def spoken_text(text, settings, gaming=False):
    from speech_tools import spoken_summary

    if not settings.get("app_settings", {}).get("short_voice_answers", True):
        return text
    # Summarize only long speech. The full result always remains in the menu.
    count = policy(settings, gaming)["sentences"]
    import re

    pieces = re.split(r"(?<=[.!?])\s+|\n+", text)
    if len(text) <= 180 or len(pieces) <= count:
        return spoken_summary(text)
    return " ".join(pieces[:count]) + " Полный ответ — в меню."


def description(settings):
    style = settings.get("profile", {}).get("personality", "friendly")
    value = policy(settings)
    return (
        f"{PERSONALITIES.get(style, 'Дружелюбная')}: до {value['sentences']} предложений в длинной озвучке; "
        + (
            "может предлагать повторяющиеся привычки."
            if value["initiative"]
            else "без инициативных предложений привычек."
        )
        + " В игре — краткий ответ."
    )
