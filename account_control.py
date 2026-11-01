"""Bounded stdin control for account services, on their own asyncio loop."""

import asyncio
import sqlite3
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
            result, claimed = self._send(request, command, ident)
            self._receipt(result, request if claimed else None)
            if command == "reply":
                emit(
                    "notification",
                    "Ваш ответ доставлен" if result["ok"] else result["text"],
                    priority="normal" if result["ok"] else "high",
                )

    def _send(self, request, command, ident):
        loop = self.loop
        handler = self.organizer_handler if command == "organizer_send" else self.handler
        claimed = False
        try:
            text, chat = request.get("text"), str(request.get("chat", ""))
            if (
                not chat.isdigit()
                or not isinstance(text, str)
                or not 1 <= len(text.strip()) <= 2000
            ):
                raise ValueError("Некорректный адресат или текст")
            if command == "organizer_send":
                from organizer_bridge import saved_receipt

                previous = saved_receipt(self.folder, self.service, request)
                if previous:
                    return previous, False
            if loop is None or handler is None or loop.is_closed():
                raise ValueError("Аккаунт сейчас не подключён")
            if command == "organizer_send":
                from organizer_bridge import claim

                claim(self.folder, self.service, request)
                claimed = True
            future = asyncio.run_coroutine_threadsafe(handler(chat, text.strip()), loop)
            try:
                delivered = future.result(timeout=30)
            except TimeoutError:
                future.cancel()
                raise TimeoutError("Отправка не подтверждена") from None
            if command == "organizer_send" and not delivered:
                raise TimeoutError("Мессенджер не вернул номер сообщения")
            return {
                "id": ident,
                "ok": True,
                "text": "Мессенджер принял сообщение",
                "message_id": str(delivered or "")[:80],
            }, claimed
        except Exception as exc:
            from organizer_bridge import delivery_error

            message, uncertain = delivery_error(exc)
            return {"id": ident, "ok": False, "text": message, "uncertain": uncertain}, claimed

    def _receipt(self, result, request=None):
        if request is not None:
            from organizer_bridge import record_receipt

            try:
                record_receipt(self.folder, self.service, request, result)
            except (OSError, sqlite3.Error, ValueError):
                emit("status", "Не удалось сохранить результат отправки; передаю его напрямую ядру")
        emit("data", data={"action": result})
