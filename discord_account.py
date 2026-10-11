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
            if response.status in (401, 403):
                raise PermissionError(
                    "Discord отклонил доступ аккаунта; проверьте сессию в приложении Discord"
                )
            if response.status == 429:
                value = await response.json()
                await asyncio.sleep(min(60, max(1, float(value.get("retry_after", 5)))))
                raise RuntimeError("Discord ограничил частоту запросов; запрос пропущен")
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

    async def message(self, session, data):
        author = data.get("author") or {}
        channel = str(data.get("channel_id", ""))
        if data.get("guild_id") or not channel.isdigit():
            return
        if str(author.get("id")) == self.user_id:
            # Own replies are deduplicated, genuine outgoing messages pause automation.
            if str(data.get("id")) not in self.own_messages:
                self.gate.outgoing(channel)
            return
        if author.get("bot") or not author.get("id"):
            return
        cfg = self.config()
        opts = cfg["discord_account"]
        if not opts["enabled"] or not opts["auto_reply"]:
            return
        important = urgent(data.get("content", ""))
        if not self.gate.allow(channel, cfg["auto_replies"], important):
            return
        if channel not in self.dm_channels:
            info = await self.request(session, "GET", "/channels/" + channel)
            if info.get("type") != 1:
                return  # Group DMs must never receive personal auto-replies.
            if len(self.dm_channels) >= 1000:
                self.dm_channels.clear()
            self.dm_channels.add(channel)
        status = self.local_status()
        text = render_reply(
            cfg["owner_name"], status, cfg["auto_replies"], bool(data.get("attachments")), important
        )
        sent = await self.request(
            session,
            "POST",
            f"/channels/{channel}/messages",
            json={"content": text, "allowed_mentions": {"parse": []}},
        )
        ident = str(sent.get("id", ""))
        self.own_messages.add(ident)
        if len(self.own_messages) > 1000:
            self.own_messages = {ident}
        delay = cfg["auto_replies"]["delete_seconds"]
        if delay:
            task = asyncio.create_task(self.delete_later(session, channel, ident, delay))
            self.deletions.add(task)
            task.add_done_callback(self.deletions.discard)
        emit(
            "notification",
            "Discord: сообщение от "
            + str(author.get("global_name") or author.get("username", "контакта"))[:80],
            priority="high" if important else "normal",
        )

    def local_status(self):
        try:
            return read_json(Path(self.manager.filepath).parent / "current_status.json")
        except (OSError, ValueError):
            return {}

    async def status_loop(self, session, ws):
        while True:
            cfg = self.config()["discord_account"]
            if not cfg["enabled"]:
                await ws.close()
                return
            if cfg["update_status"]:
                text = cfg["status_text"]
                if cfg["follow_activity"]:
                    state = self.local_status()
                    text = "В игре · Крест рядом" if state.get("is_playing") else cfg["status_text"]
                if text != self.last_status and time.monotonic() - self.status_updated > 300:
                    await self.request(
                        session,
                        "PATCH",
                        "/users/@me/settings",
                        json={"custom_status": {"text": text}},
                    )
                    self.last_status = text
                    self.status_updated = time.monotonic()
            await asyncio.sleep(30)

    async def consume_messages(self, session, incoming):
        while True:
            data = await incoming.get()
            try:
                await self.message(session, data)
            except PermissionError:
                raise
            except Exception:
                emit("error", "Discord: автоответ не отправлен; проверьте соединение и лимиты")
            finally:
                incoming.task_done()

    async def run(self):
        import aiohttp

        cfg = self.config()
        token = cfg["api_keys"]["discord_user_token"].strip()
        if not token:
            raise ValueError("Введите токен личной сессии в меню «Мой Discord»")
        async with aiohttp.ClientSession(
            headers={"Authorization": token}, timeout=aiohttp.ClientTimeout(total=25)
        ) as session:
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
                        hello = await ws.receive_json(timeout=20)
                        if hello.get("op") != 10:
                            raise RuntimeError("Discord не прислал приветствие шлюза")
                        self.acknowledged = True
                        self.sequence = None
                        tasks.append(
                            asyncio.create_task(
                                self.heartbeat(ws, max(1, hello["d"]["heartbeat_interval"] / 1000))
                            )
                        )
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
                                emit("ready", "Личная сессия Discord подключена")
                                tasks.append(asyncio.create_task(self.status_loop(session, ws)))
                                tasks.append(
                                    asyncio.create_task(self.consume_messages(session, incoming))
                                )
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
                        if ws.close_code in (4004, 4014):
                            raise PermissionError("Discord отклонил авторизацию личной сессии")
                except PermissionError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError):
                    emit("status", "Переподключаю личную сессию Discord")
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
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
            "error",
            str(exc)
            if isinstance(exc, (ValueError, PermissionError))
            else "Личная сессия остановлена. Проверьте доступ Discord и настройки.",
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
