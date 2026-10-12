"""Server bot and its menu bridge. Personal-account access is a separate service."""

import asyncio
from pathlib import Path
import threading

import discord

from auto_replies import ReplyGate, render_reply, urgent
from bridge_sdk import commands, emit
from configuration import ConfigManager
from storage import read_json

CAPABILITIES = ["refresh", "channels", "send_message"]


class KrestDS(discord.Client):
    def __init__(self, notifications_queue=None, manager=None, **kwargs):
        super().__init__(**kwargs)
        self.manager = manager or ConfigManager()
        self.notifications_queue = notifications_queue
        self.gate = ReplyGate()
        self.tasks = set()
        self.presence_task = None
        self.last_presence = None

    async def setup_hook(self):
        loop = asyncio.get_running_loop()

        def control():
            for request in commands():
                if self.is_closed() or loop.is_closed():
                    return
                future = asyncio.run_coroutine_threadsafe(self.control(request), loop)
                try:
                    future.result(timeout=40)
                except TimeoutError:
                    future.cancel()
                    emit(
                        "error", "Discord: запрос меню не завершён; проверьте журнал перед повтором"
                    )
                except Exception:
                    emit("error", "Discord: запрос меню не выполнен")

        threading.Thread(target=control, name="DiscordBotControl", daemon=True).start()

    async def on_ready(self):
        emit("ready", "Серверный Discord-бот подключён", capabilities=CAPABILITIES)
        self.overview()
        if self.presence_task is None or self.presence_task.done():
            self.presence_task = asyncio.create_task(self.status_rotator())

    async def on_disconnect(self):
        emit("disconnected", "Discord: восстанавливаю соединение серверного бота")

    async def on_resumed(self):
        await self.on_ready()

    def overview(self):
        emit(
            "data",
            data={
                "overview": {
                    "name": str(self.user),
                    "id": str(self.user.id),
                    "guild_count": len(self.guilds),
                    "guilds": [
                        {"id": str(g.id), "name": g.name[:48], "members": g.member_count}
                        for g in self.guilds[:50]
                    ],
                    "latency_ms": round(self.latency * 1000)
                    if self.latency == self.latency
                    else None,
                }
            },
        )

    def channels(self, guild_id):
        guild = self.get_guild(int(guild_id)) if str(guild_id).isdigit() else None
        if guild is None:
            raise ValueError("Бот не подключён к выбранному серверу. Обновите список.")
        if guild.me is None:
            raise ValueError("Discord ещё загружает права бота. Повторите через несколько секунд.")
        result = []
        for channel in guild.text_channels:
            permissions = channel.permissions_for(guild.me)
            if permissions.view_channel:
                result.append(
                    {
                        "id": str(channel.id),
                        "name": channel.name[:40],
                        "send": permissions.send_messages,
                    }
                )
        emit(
            "data",
            data={
                "channels": {"guild_id": str(guild.id), "count": len(result), "items": result[:60]}
            },
        )

    async def control(self, request):
        command = request.get("command")
        if command not in CAPABILITIES:
            return
        request_id = str(request.get("request_id", ""))[:80]
        try:
            if not self.is_ready():
                raise ValueError("Бот ещё подключается к Discord. Дождитесь статуса «Подключён».")
            if command == "refresh":
                self.overview()
                emit("status", "Список серверов обновлён")
            elif command == "channels":
                self.channels(request.get("guild_id", ""))
            else:
                text = request.get("text", "")
                if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                    raise ValueError("Сообщение должно содержать от 1 до 2000 символов.")
                ident = str(request.get("channel_id", ""))
                channel = self.get_channel(int(ident)) if ident.isdigit() else None
                if not isinstance(channel, discord.TextChannel):
                    raise ValueError("Выберите текстовый канал сервера из списка.")
                if str(channel.guild.id) != str(request.get("guild_id", "")):
                    raise ValueError(
                        "Выбранный канал относится к другому серверу. Обновите список."
                    )
                if channel.guild.me is None:
                    raise ValueError("Права бота ещё не загружены.")
                permissions = channel.permissions_for(channel.guild.me)
                if not permissions.view_channel or not permissions.send_messages:
                    raise ValueError("У бота нет прав просмотра или отправки в этом канале.")
                await asyncio.wait_for(
                    channel.send(text.strip(), allowed_mentions=discord.AllowedMentions.none()), 25
                )
                emit(
                    "data",
                    data={
                        "action": {
                            "id": request_id,
                            "ok": True,
                            "text": "Сообщение отправлено в #" + channel.name,
                        }
                    },
                )
        except (ValueError, discord.Forbidden) as exc:
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Discord запретил действие. Проверьте права бота."
            )
            emit("data", data={"action": {"id": request_id, "ok": False, "text": message}})
            if not request_id:
                emit("error", message)
        except (discord.HTTPException, OSError, TimeoutError):
            emit(
                "data",
                data={
                    "action": {
                        "id": request_id,
                        "ok": False,
                        "text": "Discord не подтвердил выполнение. Проверьте канал перед повторной отправкой.",
                    }
                },
            )

    def local_status(self):
        try:
            return read_json(Path(self.manager.filepath).parent / "current_status.json")
        except (OSError, ValueError):
            return {}

    async def status_rotator(self):
        while not self.is_closed():
            cfg = self.manager.snapshot()["discord_bot"]
            status = (
                "dnd"
                if cfg["follow_activity"] and self.local_status().get("is_playing")
                else cfg["status"]
            )
            current = (status, cfg["activity_text"])
            if self.is_ready() and current != self.last_presence:
                try:
                    await self.change_presence(
                        activity=discord.Game(cfg["activity_text"]),
                        status=getattr(discord.Status, status),
                    )
                    self.last_presence = current
                except (discord.DiscordException, OSError):
                    emit("status", "Discord: обновление активности будет повторено")
            await asyncio.sleep(20)

    async def auto_delete_message(self, message, delay):
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except discord.DiscordException:
            pass

    async def on_message(self, message):
        if (
            message.author == self.user
            or message.author.bot
            or not isinstance(message.channel, discord.DMChannel)
        ):
            return
        cfg = self.manager.snapshot()
        if not self.gate.incoming(message.channel.id, message.id):
            return
        important = urgent(message.content)
        emit(
            "notification",
            "Discord Bot: новое сообщение от " + message.author.display_name[:80],
            priority="high" if important else "normal",
            notice_kind="message",
        )
        if not cfg["discord_bot"]["auto_reply"] or not self.gate.allow(
            message.channel.id, cfg["auto_replies"], important
        ):
            return
        text = render_reply(
            cfg["owner_name"],
            self.local_status(),
            cfg["auto_replies"],
            bool(message.attachments),
            important,
            personality=cfg["profile"]["personality"],
        )
        try:
            sent = await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            self.gate.failed(message.channel.id)
            emit("error", "Discord: автоответ не отправлен")
            return
        delay = cfg["auto_replies"]["delete_seconds"]
        self.gate.delivered(message.channel.id)
        if delay:
            task = asyncio.create_task(self.auto_delete_message(sent, delay))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def close(self):
        tasks = list(self.tasks) + ([self.presence_task] if self.presence_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


def start_ds_bridge(notifications_queue=None):
    import os

    manager = ConfigManager()
    token = (os.getenv("DISCORD_TOKEN") or manager.snapshot()["api_keys"]["discord_token"]).strip()
    if not token:
        raise RuntimeError("Укажите Bot Token на вкладке «Discord · серверный бот»")
    intents = discord.Intents.none()
    intents.guilds = True
    intents.dm_messages = True
    client = KrestDS(notifications_queue=notifications_queue, manager=manager, intents=intents)
    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure as exc:
        emit(
            "error",
            "Discord отклонил Bot Token. Токен личного аккаунта вводится в «Discord · мой аккаунт».",
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    from licensing import require_premium

    require_premium(ConfigManager().snapshot())
    start_ds_bridge()
