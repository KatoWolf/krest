"""Optional Discord personal session, isolated from the ordinary discord.py bot.

Uses the account gateway/HTTP endpoints, which are unofficial for automation.
No token extraction, mass messaging, guild reads, or history download.
"""

import asyncio
import json
import time
from pathlib import Path

from auto_replies import ReplyGate, render_reply, urgent
from bridge_sdk import emit
from storage import read_json

API = "https://discord.com/api/v9"
GATEWAY = "wss://gateway.discord.gg/?v=9&encoding=json"


class RateLimited(RuntimeError):
    def __init__(self, seconds):
        self.seconds = min(3600, max(1, float(seconds)))
        super().__init__(f"Discord ограничил запросы на {self.seconds:g} сек.")


class AccountSession:
    def __init__(self, manager):
        self.manager = manager
        self.gate = ReplyGate()
        self.user_id = None
        self.sequence = None
        self.acknowledged = True
        self.own_messages = set()
        self.dm_channels = set()
        self.status_updated = 0
        self.last_status = None
        self.deletions = set()

    def config(self):
        # Reload saved preferences without restarting the session.
        return self.manager.snapshot()

    async def request(self, session, method, route, **kwargs):
        async with session.request(method, API + route, **kwargs) as response:
            if response.status == 401:
                raise PermissionError(
                    "Discord отклонил доступ аккаунта; проверьте сессию в приложении Discord"
                )
            if response.status == 429:
                value = await response.json()
                raise RateLimited(value.get("retry_after", 5))
            if response.status == 403:
                raise ValueError("Discord запретил действие; проверьте доступ к этому чату")
            if response.status >= 400:
                raise RuntimeError("Discord HTTP " + str(response.status))
            return await response.json() if response.status != 204 else {}

    async def heartbeat(self, ws, interval):
        while not ws.closed:
            await asyncio.sleep(interval)
            if not self.acknowledged:
                await ws.close()
                return
            self.acknowledged = False
            await ws.send_json({"op": 1, "d": self.sequence})

    async def delete_later(self, session, channel, message, seconds):
        await asyncio.sleep(seconds)
        try:
            await self.request(session, "DELETE", f"/channels/{channel}/messages/{message}")
        except Exception:
            emit("status", "Автоответ отправлен; удалить его автоматически не удалось")

    async def message(self, session, data, deferred=False):
        author = data.get("author") or {}
        channel = str(data.get("channel_id", ""))
        if data.get("guild_id") or not channel.isdigit():
            return
        if str(author.get("id")) == self.user_id:
            # Own replies are deduplicated, genuine outgoing messages pause automation.
            if str(data.get("id")) not in self.own_messages and not self.gate.own_echo(
                channel, data.get("content", "")
            ):
                self.gate.outgoing(channel)
            return
        if author.get("bot") or not author.get("id"):
            return
        cfg = self.config()
        opts = cfg["discord_account"]
        if not opts["enabled"]:
            return
        important = urgent(data.get("content", ""))
        if channel not in self.dm_channels:
            info = await self.request(session, "GET", "/channels/" + channel)
            if info.get("type") != 1:
                return  # Group DMs must never receive personal auto-replies.
            if len(self.dm_channels) >= 1000:
                self.dm_channels.clear()
            self.dm_channels.add(channel)
        if not deferred and not self.gate.incoming(channel, data.get("id")):
            return
        if not deferred:
            emit(
                "notification",
                "Discord: новое сообщение от "
                + str(author.get("global_name") or author.get("username", "контакта"))[:80],
                priority="high" if important else "normal",
                notice_kind="message",
                target={
                    "chat": channel,
                    "sender": str(author.get("global_name") or author.get("username", "контакта"))[
                        :80
                    ],
                    "message": str(data.get("id", "")),
                },
            )
        from bot_activity import reply_options, chat_muted

        if chat_muted(Path(self.manager.filepath).parent, "discord_account.py", channel):
            self.gate.pending.pop(channel, None)
            return
        reply_cfg = reply_options(cfg, "discord_account")
        if not opts["auto_reply"]:
            return
        if not self.gate.allow(channel, reply_cfg, important):
            self.gate.postpone(channel, data, reply_cfg, important)
            return
        self.gate.pending.pop(channel, None)
        status = self.local_status()
        text = render_reply(
            cfg["owner_name"],
            status,
            self.gate.reply_settings(
                channel, reply_cfg, cfg["profile"]["personality"], status.get("is_playing", False)
            ),
            bool(data.get("attachments")),
            important,
            personality=cfg["profile"]["personality"],
        )
        try:
            self.gate.sending(channel, text)
            sent = await self.request(
                session,
                "POST",
                f"/channels/{channel}/messages",
                json={"content": text, "allowed_mentions": {"parse": []}},
            )
        except RateLimited as exc:
            self.gate.failed(channel)
            self.gate.blocked_until = time.monotonic() + exc.seconds
            emit("status", str(exc))
            return
        except Exception:
            self.gate.failed(channel)
            raise
        ident = str(sent.get("id", ""))
        self.gate.delivered(channel, ident)
        self.gate.advance(
            channel, reply_cfg, cfg["profile"]["personality"], status.get("is_playing", False)
        )
        self.gate.last_reason = "Автоответ доставлен"
        self.own_messages.add(ident)
        if len(self.own_messages) > 1000:
            self.own_messages = {ident}
        delay = cfg["auto_replies"]["delete_seconds"]
        if delay:
            task = asyncio.create_task(self.delete_later(session, channel, ident, delay))
            self.deletions.add(task)
            task.add_done_callback(self.deletions.discard)

    def local_status(self):
        from bot_activity import local_activity

        return local_activity(Path(self.manager.filepath).parent)

    async def reply_worker(self, session):
        while True:
            for _, data, _ in self.gate.due():
                try:
                    await self.message(session, data, deferred=True)
                except PermissionError:
                    raise
                except Exception:
                    emit(
                        "status",
                        "Отложенный автоответ не подтверждён; следующий запрос будет обработан",
                    )
            await asyncio.sleep(1)

    async def status_loop(self, session, ws):
        while True:
            cfg = self.config()["discord_account"]
            if not cfg["enabled"]:
                await ws.close()
                return
            if cfg["update_status"]:
                from bot_activity import status_text

                text = status_text(self.config(), "discord_account", self.local_status())
                if self.last_status is None or (
                    text != self.last_status and time.monotonic() - self.status_updated >= 300
                ):
                    try:
                        await self.request(
                            session,
                            "PATCH",
                            "/users/@me/settings",
                            json={"custom_status": {"text": text}},
                        )
                    except RateLimited as exc:
                        emit("status", str(exc))
                        await asyncio.sleep(exc.seconds)
                        continue
                    except PermissionError:
                        raise
                    except (OSError, ValueError, RuntimeError):
                        emit(
                            "status", "Discord: статус не обновлён; следующая попытка через 30 сек."
                        )
                        await asyncio.sleep(30)
                        continue
                    self.last_status = text
                    self.status_updated = time.monotonic()
            emit(
                "health",
                self.gate.last_reason,
                connected=not ws.closed,
                pending=len(self.gate.pending),
                replies=len(self.gate.sent),
            )
            await asyncio.sleep(30)

    async def consume_messages(self, session, incoming):
        while True:
            data = await incoming.get()
            try:
                await self.message(session, data)
            except PermissionError:
                raise
            except RateLimited as exc:
                emit("status", str(exc))
                await asyncio.sleep(exc.seconds)
            except Exception:
                emit("error", "Discord: автоответ не отправлен; проверьте соединение и лимиты")
            finally:
                incoming.task_done()

    async def run(self):
        import aiohttp
        from account_control import AccountControl

        control = AccountControl()

        cfg = self.config()
        token = cfg["api_keys"]["discord_user_token"].strip()
        if not token:
            raise ValueError("Введите токен личной сессии в меню «Мой Discord»")
        async with aiohttp.ClientSession(
            headers={"Authorization": token}, timeout=aiohttp.ClientTimeout(total=25)
        ) as session:

            async def manual_reply(chat, text):
                if str(chat) not in self.dm_channels or str(chat) not in {
                    key[0] for key in self.gate.received
                }:
                    raise ValueError("Нет свежего входящего от этого личного чата")
                self.gate.outgoing(chat)
                await self.request(
                    session,
                    "POST",
                    f"/channels/{chat}/messages",
                    json={"content": text, "allowed_mentions": {"parse": []}},
                )

            identity = await self.request(session, "GET", "/users/@me")
            if identity.get("bot"):
                raise ValueError("Это токен бота. Используйте вкладку «Discord Bot»")
            self.user_id = str(identity["id"])
            for attempt in range(5):
                if not self.config()["discord_account"]["enabled"]:
                    return
                tasks = []
                incoming = asyncio.Queue(maxsize=32)
                try:
                    async with session.ws_connect(GATEWAY, max_msg_size=4 * 1024 * 1024) as ws:

                        def task_finished(task):
                            # A failed consumer/status task must wake the gateway
                            # even when no more messages arrive from Discord.
                            if not task.cancelled() and task.exception() is not None:
                                asyncio.create_task(ws.close())

                        def watch(coro):
                            task = asyncio.create_task(coro)
                            task.add_done_callback(task_finished)
                            tasks.append(task)

                        hello = await ws.receive_json(timeout=20)
                        if hello.get("op") != 10:
                            raise RuntimeError("Discord не прислал приветствие шлюза")
                        self.acknowledged = True
                        self.sequence = None
                        watch(self.heartbeat(ws, max(1, hello["d"]["heartbeat_interval"] / 1000)))
                        await ws.send_json(
                            {
                                "op": 2,
                                "d": {
                                    "token": token,
                                    "properties": {
                                        "os": "Windows",
                                        "browser": "Krest",
                                        "device": "Krest",
                                    },
                                    "compress": False,
                                },
                            }
                        )
                        async for message in ws:
                            if message.type != aiohttp.WSMsgType.TEXT:
                                break
                            event = json.loads(message.data)
                            if event.get("s") is not None:
                                self.sequence = event["s"]
                            op = event.get("op")
                            if op == 11:
                                self.acknowledged = True
                                emit(
                                    "health",
                                    self.gate.last_reason,
                                    connected=True,
                                    pending=len(self.gate.pending),
                                    replies=len(self.gate.sent),
                                )
                            elif op == 1:
                                await ws.send_json({"op": 1, "d": self.sequence})
                            elif op in (7, 9):
                                break
                            elif event.get("t") == "READY":
                                self.dm_channels = {
                                    str(c["id"])
                                    for c in (event.get("d") or {}).get("private_channels", [])
                                    if isinstance(c, dict) and c.get("type") == 1 and c.get("id")
                                }
                                control.attach(manual_reply)
                                emit(
                                    "ready",
                                    "Личный аккаунт Discord подключён: "
                                    + str(identity.get("username", ""))[:80],
                                    capabilities=["reply"],
                                )
                                watch(self.status_loop(session, ws))
                                watch(self.consume_messages(session, incoming))
                                watch(self.reply_worker(session))
                            elif event.get("t") == "CHANNEL_CREATE":
                                channel = event.get("d") or {}
                                if channel.get("type") == 1 and channel.get("id"):
                                    self.dm_channels.add(str(channel["id"]))
                            elif event.get("t") == "CHANNEL_DELETE":
                                self.dm_channels.discard(str((event.get("d") or {}).get("id")))
                            elif event.get("t") == "MESSAGE_CREATE":
                                data = event.get("d") or {}
                                if not data.get("guild_id"):
                                    try:
                                        incoming.put_nowait(data)
                                    except asyncio.QueueFull:
                                        emit(
                                            "status",
                                            "Очередь личных сообщений заполнена; часть автоответов пропущена",
                                        )
                            for task in tasks:
                                if task.done() and not task.cancelled() and task.exception():
                                    raise task.exception()
                        for task in tasks:
                            if task.done() and not task.cancelled() and task.exception():
                                raise task.exception()
                        if ws.close_code in (4004, 4014):
                            raise PermissionError("Discord отклонил авторизацию личной сессии")
                except PermissionError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError):
                    emit("disconnected", "Переподключаю личную сессию Discord")
                finally:
                    control.detach()
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                emit("disconnected", "Соединение личного аккаунта закрыто; ожидаю переподключения")
                if not self.config()["discord_account"]["enabled"]:
                    return
                await asyncio.sleep(min(60, 10 * (attempt + 1)))
            raise RuntimeError(
                "Discord: соединение нестабильно. Сессия остановлена после пяти попыток"
            )


def main():
    from configuration import ConfigManager
    from licensing import require_premium

    manager = ConfigManager()
    require_premium(manager.snapshot())
    if not manager.snapshot()["discord_account"]["enabled"]:
        raise ValueError("Включите личную сессию в меню «Мой Discord»")
    try:
        asyncio.run(AccountSession(manager).run())
    except Exception as exc:
        # Never print token-bearing HTTP objects or tracebacks.
        emit(
            "auth_required" if isinstance(exc, PermissionError) else "error",
            str(exc)
            if isinstance(exc, (ValueError, PermissionError))
            else "Личная сессия остановлена. Проверьте доступ Discord и настройки.",
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
