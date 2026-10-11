"""Ordinary Discord bot. Personal-account sessions live in discord_account.py."""

import asyncio
from pathlib import Path

import discord

from auto_replies import ReplyGate, render_reply, urgent
from bridge_sdk import emit
from configuration import ConfigManager
from storage import read_json


class KrestDS(discord.Client):
    def __init__(self, notifications_queue=None, manager=None, **kwargs):
        super().__init__(**kwargs)
        self.manager = manager or ConfigManager()
        self.notifications_queue = notifications_queue
        self.gate = ReplyGate()
        self.tasks = set()
        self.presence_task = None

    async def on_ready(self):
        emit("ready", "Discord: обычный бот подключён")
        if self.presence_task is None or self.presence_task.done():
            self.presence_task = asyncio.create_task(self.status_rotator())

    async def on_disconnect(self):
        emit("status", "Discord: восстанавливаю соединение бота")

    def local_status(self):
        try:
            return read_json(Path(self.manager.filepath).parent / "current_status.json")
        except (OSError, ValueError):
            return {}

    async def status_rotator(self):
        while not self.is_closed():
            status = self.local_status()
            try:
                await self.change_presence(
                    activity=discord.Game("Крест · помощник рядом"),
                    status=discord.Status.dnd
                    if status.get("is_playing")
                    else discord.Status.online,
                )
            except discord.DiscordException:
                emit("status", "Discord: не удалось обновить активность бота")
            await asyncio.sleep(300)

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
        important = urgent(message.content)
        if not self.gate.allow(message.channel.id, cfg["auto_replies"], important):
            return
        text = render_reply(
            cfg["owner_name"],
            self.local_status(),
            cfg["auto_replies"],
            bool(message.attachments),
            important,
        )
        try:
            sent = await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            emit("error", "Discord: автоответ не отправлен")
            return
        delay = cfg["auto_replies"]["delete_seconds"]
        if delay:
            task = asyncio.create_task(self.auto_delete_message(sent, delay))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        emit(
            "notification",
            "Discord Bot: сообщение от " + message.author.display_name[:80],
            priority="high" if important else "normal",
        )

    async def close(self):
        tasks = list(self.tasks) + ([self.presence_task] if self.presence_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()


def start_ds_bridge(notifications_queue=None):
    import os

    manager = ConfigManager()
    token = os.getenv("DISCORD_TOKEN") or manager.snapshot()["api_keys"]["discord_token"]
    if not token:
        raise RuntimeError("Укажите Bot Token на вкладке Discord Bot")
    intents = discord.Intents.none()
    intents.dm_messages = True
    client = KrestDS(notifications_queue=notifications_queue, manager=manager, intents=intents)
    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure as exc:
        raise RuntimeError(
            "Discord отклонил Bot Token; токен личного аккаунта вводится на отдельной вкладке"
        ) from exc


if __name__ == "__main__":
    from licensing import require_premium

    require_premium(ConfigManager().snapshot())
    start_ds_bridge()
