"""Offline characters with separate, non-repeating dialogue decks."""

import random
from personality_bank import INTERACTIONS, FACTS, CONCISE_FACTS

PERSONALITIES = {
    "friendly": "Дружелюбная",
    "concise": "Краткая",
    "lively": "Живая",
    "calm": "Спокойная",
    "tactical": "Штурман",
    "ironic": "Ироничная",
    "gothic": "Хранительница",
}
CHARACTERS = {
    "friendly": "Тёплая и внимательная. Объясняет действия простыми словами.",
    "concise": "Ответы по делу: короткое подтверждение, без вступлений и лишних вопросов.",
    "lively": "Энергичная и любопытная. Предлагает занятия и поддерживает разговор.",
    "calm": "Спокойная и терпеливая. Помогает разбить большое дело на маленькие шаги.",
    "tactical": "Собранный второй пилот. Цели, приоритеты, состояние системы.",
    "ironic": "Сухой юмор и добрые подколы. Без оскорблений и лишней болтовни.",
    "gothic": "Сдержанная хранительница с мрачной образностью и заботливым характером.",
}

STYLE_REPLIES = {style: {**bank, "fact": FACTS} for style, bank in INTERACTIONS.items()}
STYLE_REPLIES["concise"]["fact"] = CONCISE_FACTS
REPLIES = STYLE_REPLIES["friendly"]


def reply(kind, state):
    """Shuffle each style/topic deck; exhaust it before repeating any phrase."""
    getter = getattr(state, "config_getter", lambda: {})
    style = getter().get("profile", {}).get("personality", "friendly")
    if style not in PERSONALITIES:
        style = "friendly"
    pool = STYLE_REPLIES[style].get(kind, REPLIES.get(kind, REPLIES["conversation"]))
    if not hasattr(state, "_reply_decks"):
        state._reply_decks = {}
        state._last_reply = {}
    key = (style, kind)
    deck = state._reply_decks.get(key, [])
    if not deck:
        deck = list(dict.fromkeys(pool))
        random.shuffle(deck)
        if len(deck) > 1 and deck[-1] == state._last_reply.get(key):
            deck[0], deck[-1] = deck[-1], deck[0]
        state._reply_decks[key] = deck
    phrase = deck.pop()
    state._last_reply[key] = phrase
    name = getattr(state, "owner_name", "").strip()
    return phrase.format(name=", " + name if name else "")
