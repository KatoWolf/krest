"""Explicit local notification actions; message drafts require a separate confirmation."""

import threading
import time
import uuid

SERVICES = {"tg_bridge.py": "Telegram", "discord_account.py": "Discord"}


class ConversationActions:
    def __init__(self, runtime):
        self.runtime = runtime
        self.lock = threading.RLock()
        self.draft = None

    def target(self, ident=None):
        rows = [
            r
            for r in self.runtime.center.list(unread=True)
            if r["source"] in SERVICES and r.get("target", {}).get("chat")
        ]
        if ident is not None:
            rows = [
                r
                for r in self.runtime.center.list()
                if r["id"] == int(ident)
                and r["source"] in SERVICES
                and r.get("target", {}).get("chat")
            ]
        else:
            rows = [r for r in rows if time.time() - r["updated"] < 120]
            contacts = {(r["source"], r["target"]["chat"]) for r in rows}
            if len(contacts) > 1:
                raise ValueError(
                    "Написали несколько человек. Выберите нужное уведомление в меню, чтобы не перепутать адресата."
                )
        if not rows:
            raise ValueError(
                "Выберите свежее сообщение Telegram или личного Discord в уведомлениях."
            )
        return rows[0]

    def call(self, action, ident=None, text=""):
        with self.lock:
            if action == "cancel":
                self.draft = None
                return "Черновик ответа удалён."
            if action == "confirm":
                draft, self.draft = self.draft, None
                if not draft or time.monotonic() > draft["expires"]:
                    raise ValueError(
                        "Сначала подготовьте ответ. Подтверждение действует две минуты."
                    )
                self.runtime.integrations.send(
                    draft["source"],
                    "reply",
                    chat=draft["chat"],
                    text=draft["text"],
                    request_id=draft["id"],
                )
                return (
                    "Ответ передан в "
                    + SERVICES[draft["source"]]
                    + ". Подтверждение доставки появится в уведомлениях."
                )
            row = self.target(ident)
            target = row["target"]
            if action == "draft":
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                    raise ValueError("Ответ должен содержать от 1 до 2000 символов.")
                self.draft = dict(
                    id=uuid.uuid4().hex,
                    source=row["source"],
                    chat=target["chat"],
                    text=text.strip(),
                    expires=time.monotonic() + 120,
                )
                return f"Ответ для {target['sender']} в {SERVICES[row['source']]}: «{text.strip()}». Скажите «Крест, подтверждаю отправку» или «Крест, отмени ответ»."
            if action == "mute":
                self.runtime.center.mute(row["source"], target["chat"], time.time() + 3600)
                return f"Уведомления и автоответы для {target['sender']} приглушены на час."
            if action == "after_game":
                if not self.runtime.performance.game:
                    raise ValueError(
                        "Игра сейчас не обнаружена. Можно поставить обычное напоминание."
                    )
                self.runtime.center.after_game(row["id"])
                return "Напомню об этом сообщении после выхода из игры."
            raise ValueError("Неизвестное действие с уведомлением")
