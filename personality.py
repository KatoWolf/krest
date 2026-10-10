"""Small offline response bank; no conversational model or network calls."""

import random

REPLIES = {
    "greeting": [
        "На связи{name}. Что сделаем?",
        "Привет{name}. Готова помочь.",
        "Слушаю{name}. С чего начнём?",
    ],
    "thanks": ["Пожалуйста{name}.", "Рада помочь{name}.", "Обращайтесь{name}."],
    "identity": [
        "Я Крест — ваш локальный помощник. Управляю компьютером, музыкой и напоминаниями через команды.",
        "Я Крест. Собираю повседневные действия в одном месте: от музыки до ваших задач.",
    ],
    "mood": [
        "На связи и готова к работе{name}.",
        "Всё в порядке{name}. Можно заняться делами или включить музыку.",
    ],
    "support": [
        "Можно начать с одного небольшого дела{name}. Хотите — поставьте напоминание о перерыве.",
        "Не обязательно решать всё сразу{name}. Выберите одну задачу, а я помогу с напоминанием.",
    ],
    "joke": [
        "Почему компьютер не опаздывает? У него всё по тактам.",
        "Мой любимый вид уборки — когда после неё файлы всё ещё находятся.",
        "Лучшая команда дня — сохранить. Особенно перед экспериментами.",
    ],
}


def reply(kind, state):
    style = state.config_getter().get("profile", {}).get("personality", "friendly")
    pool = REPLIES[kind]
    if style == "concise":
        pool = pool[:1]
    previous = getattr(state, "_last_reply", {}).get(kind)
    choices = [p for p in pool if p != previous] or pool
    phrase = random.choice(choices)
    if not hasattr(state, "_last_reply"):
        state._last_reply = {}
    state._last_reply[kind] = phrase
    name = state.owner_name.strip()
    result = phrase.format(name=", " + name if name else "")
    if style == "lively" and kind == "greeting":
        result += " Музыка, дела или немного порядка на компьютере?"
    return result
