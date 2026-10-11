"""Shared local reply styles and bounded per-contact cooldowns; no message history."""

import re
import time

STYLES = {
    "minimal": "Минимализм",
    "warm": "Тёплая Крест",
    "gothic": "Тёмная эстетика",
    "tactical": "Бортовой помощник",
}
DEFAULT_REPLIES = {
    "enabled": True,
    "style": "warm",
    "cooldown_seconds": 300,
    "delete_seconds": 300,
    "share_activity": False,
}


def urgent(text):
    return bool(re.search(r"\b(срочно|важно|горит|ответь)\b", str(text).casefold()))


def render_reply(owner, status=None, settings=None, attachment=False, important=False, night=False):
    cfg = {**DEFAULT_REPLIES, **(settings or {})}
    status = status or {}
    owner = str(owner or "Владелец")[:60].replace("\n", " ")
    reason = "сейчас недоступен"
    if night:
        reason = "сейчас отдыхает"
    elif cfg["share_activity"] and status.get("is_playing"):
        reason = "сейчас в игре"
    elif cfg["share_activity"] and status.get("activity_type") == "WORK":
        reason = "сосредоточен на работе"
    body = f"{owner} {reason}."
    if attachment:
        body += " Если вопрос важный, напишите его текстом."
    elif important:
        body += " Вы отметили сообщение как срочное."
    else:
        body += " Сообщение осталось в этом чате."
    styles = {
        "minimal": body,
        "warm": "✦ Крест рядом\n" + body + " Спасибо за терпение.",
        "gothic": "✝ КРЕСТ · тихий час\n━━━━━━━━━━━━\n" + body,
        "tactical": "КРЕСТ / КАНАЛ СВЯЗИ\n"
        + ("Приоритет: высокий\n" if important else "Статус: ожидание\n")
        + body,
    }
    return styles.get(cfg["style"], styles["warm"]) + "\n\nАвтоматический ответ Крест."


class ReplyGate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.sent = {}
        self.manual = {}

    def outgoing(self, contact):
        self.manual[str(contact)] = self.clock()

    def allow(self, contact, settings=None, important=False):
        cfg = {**DEFAULT_REPLIES, **(settings or {})}
        if not cfg["enabled"]:
            return False
        now = self.clock()
        key = str(contact)
        for cache in (self.sent, self.manual):
            for old in list(cache):
                if now - cache[old] > 3600:
                    del cache[old]
        if now - self.manual.get(key, -1e9) < 300:
            return False
        delay = max(60, cfg["cooldown_seconds"] // 3) if important else cfg["cooldown_seconds"]
        if now - self.sent.get(key, -1e9) < delay:
            return False
        if len(self.sent) >= 1000:
            return False
        self.sent[key] = now
        return True
