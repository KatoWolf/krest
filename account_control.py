"""Bounded stdin control for account services, on their own asyncio loop."""

import asyncio
import threading
from bridge_sdk import commands, emit


class AccountControl:
    def __init__(self, service="", folder=None):
        self.loop = None
        self.handler = None
        self.organizer_handler = None
        self.contacts_handler = None
        self.service, self.folder = service, folder
        self.seen = set()
        self.thread = threading.Thread(target=self._read, name="AccountControl", daemon=True)
        self.thread.start()

    def attach(self, handler, organizer_handler=None, contacts_handler=None):
        self.loop = asyncio.get_running_loop()
        self.handler = handler
        self.organizer_handler = organizer_handler
        self.contacts_handler = contacts_handler

    def detach(self):
        self.loop = self.handler = None
        self.organizer_handler = None
        self.contacts_handler = None

    def _read(self):
        for request in commands():
            ident = str(request.get("request_id", ""))[:80]
            command = request.get("command")
            if (
                command not in {"reply", "organizer_send", "organizer_contacts"}
                or not ident
                or ident in self.seen
            ):
                continue
            if command == "reply" and len(self.seen) >= 1000:
                # Refuse replay rather than forget message IDs inside a live process.
                emit("status", "Достигнут лимит команд отправки; перезапустите сервис")
                continue
            if command == "reply":
                self.seen.add(ident)
            if command == "organizer_contacts":
                try:
                    if self.loop is None or self.contacts_handler is None:
                        raise ValueError("Юзербот ещё не подключён")
                    future = asyncio.run_coroutine_threadsafe(self.contacts_handler(), self.loop)
                    try:
                        contacts = future.result(timeout=30)
                    except TimeoutError:
                        future.cancel()
                        raise ValueError(
                            "Загрузка контактов не завершилась; повторите позже"
                        ) from None
                    # Small chunks fit the bridge's 8 KB framing even with Unicode.
                    for i in range(0, max(1, len(contacts)), 10):
                        emit(
                            "data",
                            data={
                                "organizer_contacts": {
                                    "id": ident,
                                    "rows": contacts[i : i + 10],
                                    "done": i + 10 >= len(contacts),
                                }
                            },
                        )
                except Exception:
                    emit(
                        "data",
                        data={
                            "organizer_contacts": {
                                "id": ident,
                                "rows": [],
                                "done": True,
                                "error": "Не удалось получить контакты; проверьте подключение юзербота",
                            }
                        },
                    )
                continue
            loop, handler = (
                self.loop,
                self.organizer_handler if command == "organizer_send" else self.handler,
            )
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
                if command == "organizer_send":
                    from organizer_bridge import claim

                    claim(self.folder, self.service, request)
                future = asyncio.run_coroutine_threadsafe(handler(chat, text.strip()), loop)
                try:
                    delivered = future.result(timeout=30)
                except TimeoutError:
                    future.cancel()
                    raise ValueError(
                        "Отправка не подтверждена. Проверьте чат перед повтором."
                    ) from None
                emit(
                    "data",
                    data={
                        "action": {
                            "id": ident,
                            "ok": True,
                            "text": "Ответ доставлен",
                            "message_id": str(delivered or "")[:80],
                        }
                    },
                )
                if command == "reply":
                    emit("notification", "Ваш ответ доставлен", priority="normal")
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Отправка не подтверждена. Проверьте соединение и чат."
                )
                emit("data", data={"action": {"id": ident, "ok": False, "text": message}})
                if command == "reply":
                    emit("notification", message, priority="high")
