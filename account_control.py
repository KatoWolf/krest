"""Bounded stdin control for account services, on their own asyncio loop."""

import asyncio
import threading
from bridge_sdk import commands, emit


class AccountControl:
    def __init__(self):
        self.loop = None
        self.handler = None
        self.seen = set()
        self.thread = threading.Thread(target=self._read, name="AccountControl", daemon=True)
        self.thread.start()

    def attach(self, handler):
        self.loop = asyncio.get_running_loop()
        self.handler = handler

    def detach(self):
        self.loop = self.handler = None

    def _read(self):
        for request in commands():
            ident = str(request.get("request_id", ""))[:80]
            if request.get("command") != "reply" or not ident or ident in self.seen:
                continue
            if len(self.seen) >= 1000:
                # Refuse replay rather than forget message IDs inside a live process.
                emit("status", "Достигнут лимит команд отправки; перезапустите сервис")
                continue
            self.seen.add(ident)
            loop, handler = self.loop, self.handler
            try:
                if loop is None or handler is None or loop.is_closed():
                    raise ValueError("Аккаунт сейчас не подключён")
                text, chat = request.get("text"), str(request.get("chat", ""))
                if (
                    not chat.isdigit()
                    or not isinstance(text, str)
                    or not 1 <= len(text.strip()) <= 2000
                ):
                    raise ValueError("Некорректный адресат или текст")
                future = asyncio.run_coroutine_threadsafe(handler(chat, text.strip()), loop)
                try:
                    future.result(timeout=30)
                except TimeoutError:
                    future.cancel()
                    raise ValueError(
                        "Отправка не подтверждена. Проверьте чат перед повтором."
                    ) from None
                emit("data", data={"action": {"id": ident, "ok": True, "text": "Ответ доставлен"}})
                emit("notification", "Ваш ответ доставлен", priority="normal")
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Отправка не подтверждена. Проверьте соединение и чат."
                )
                emit("data", data={"action": {"id": ident, "ok": False, "text": message}})
                emit("notification", message, priority="high")
