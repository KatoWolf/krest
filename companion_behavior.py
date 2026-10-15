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

    if settings.get("profile", {}).get("personality") == "concise":
        return concise_speech(text)
    if not settings.get("app_settings", {}).get("short_voice_answers", True):
        return text
    # Summarize only long speech. The full result always remains in the menu.
    count = policy(settings, gaming)["sentences"]
    import re

    pieces = re.split(r"(?<=[.!?])\s+|\n+", text)
    if len(text) <= 180 or len(pieces) <= count:
        return spoken_summary(text)
    return " ".join(pieces[:count]) + " Полный ответ — в меню."


def concise_speech(text):
    """One complete ordinary answer; keep error/confirmation details available aloud."""
    import re

    # These texts can contain the recipient, destructive operation, or recovery
    # instruction. A short personality must not hide them behind a generic OK.
    if "?" in text or re.search(
        r"\b(?:ошибк\w*|подтверд\w*|подтверждени\w*|не удалось|не выполн\w*|не подтвержден\w*)\b",
        text,
        re.I,
    ):
        return text
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text.strip())
    first = re.split(r"(?<=[.!?])\s+|\n+", text, maxsplit=1)[0]
    # Do not cut names, numbers, paths or requested facts in half. The short
    # interaction decks and factual action results provide the concise wording.
    return first


def description(settings):
    style = settings.get("profile", {}).get("personality", "friendly")
    if style == "concise":
        return (
            "Краткая: одно предложение по делу, без вступлений и повторов. "
            "Обычные подтверждения — 1–3 слова. Полные результаты остаются в меню; "
            "ошибки и запросы подтверждения озвучиваются с нужными деталями."
        )
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
