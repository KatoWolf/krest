"""Shared local reply styles and bounded per-contact cooldowns; no message history."""

import re
import time
from reply_bank import REPLY_BANK

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
    "personality": "profile",
    "variant": 0,
    "urgent_only": False,
    "paused_until": 0,
}


def urgent(text):
    return bool(re.search(r"\b(срочно|важно|горит|ответь)\b", str(text).casefold()))


def render_reply(
    owner,
    status=None,
    settings=None,
    attachment=False,
    important=False,
    night=False,
    *,
    personality="friendly",
):
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
    selected = cfg["personality"] if cfg["personality"] != "profile" else personality
    bank = REPLY_BANK.get(selected, REPLY_BANK["friendly"])
    try:
        variant = max(0, min(5, int(cfg["variant"])))
    except (ValueError, TypeError):
        variant = 0
    body = bank[variant].format(owner=owner, reason=reason)
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
        self.automatic = {}
        self.automatic_ids = {}
        self.received = {}
        self.blocked_until = 0

    def incoming(self, contact, ident):
        """Deduplicate gateway retries independently of auto-reply cooldowns."""
        if not ident:
            return True
        now = self.clock()
        self.received = {k: v for k, v in self.received.items() if now - v < 900}
        key = (str(contact), str(ident))
        if key in self.received:
            return False
        if len(self.received) >= 2000:
            self.received.pop(next(iter(self.received)))
        self.received[key] = now
        return True

    def sending(self, contact, text):
        # Register before awaiting HTTP: the outgoing gateway update may arrive
        # before the send call returns its message ID.
        now = self.clock()
        self.automatic = {k: v for k, v in self.automatic.items() if now - v[1] < 60}
        if len(self.automatic) >= 1000:
            self.automatic.pop(next(iter(self.automatic)))
        self.automatic[str(contact)] = (str(text), now)

    def own_echo(self, contact, text, ident=None):
        if ident and (str(contact), str(ident)) in self.automatic_ids:
            return True
        row = self.automatic.get(str(contact))
        return bool(row and row[0] == str(text) and self.clock() - row[1] < 60)

    def delivered(self, contact, ident=None):
        self.sent[str(contact)] = self.clock()
        if ident:
            if len(self.automatic_ids) >= 1000:
                self.automatic_ids.pop(next(iter(self.automatic_ids)))
            self.automatic_ids[(str(contact), str(ident))] = self.clock()
            self.automatic.pop(str(contact), None)

    def outgoing(self, contact):
        if len(self.manual) >= 1000:
            self.manual.pop(next(iter(self.manual)))
        self.manual[str(contact)] = self.clock()

    def failed(self, contact):
        """A failed send must not count as a delivered auto-reply."""
        self.sent.pop(str(contact), None)
        self.automatic.pop(str(contact), None)

    def allow(self, contact, settings=None, important=False):
        cfg = {**DEFAULT_REPLIES, **(settings or {})}
        if not cfg["enabled"]:
            return False
        if time.time() < cfg["paused_until"] or cfg["urgent_only"] and not important:
            return False
        now = self.clock()
        key = str(contact)
        if now < self.blocked_until:
            return False
        for cache, retention in (
            (self.sent, max(3600, cfg["cooldown_seconds"])),
            (self.manual, 3600),
        ):
            for old in list(cache):
                if now - cache[old] >= retention:
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
