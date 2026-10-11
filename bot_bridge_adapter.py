"""Exportable discord.py bridge. No credentials are stored in this file."""

import asyncio
import json
import os
import sys
import threading


def emit(kind, text="", **values):
    print(
        "KREST_EVENT " + json.dumps({"type": kind, "text": text, **values}, ensure_ascii=False),
        flush=True,
    )


def overview(client):
    emit(
        "data",
        data={
            "overview": {
                "name": str(client.user),
                "id": str(client.user.id),
                "guild_count": len(client.guilds),
                "guilds": [
                    {"id": str(g.id), "name": g.name[:48], "members": g.member_count}
                    for g in client.guilds[:50]
                ],
            }
        },
    )


async def control(client, request):
    import discord

    ident = str(request.get("request_id", ""))[:80]
    try:
        if not client.is_ready():
            raise ValueError("Бот ещё подключается")
        action = request.get("command")
        if action == "refresh":
            overview(client)
        elif action == "channels":
            guild = client.get_guild(int(request.get("guild_id", "0")))
            if guild is None or guild.me is None:
                raise ValueError("Сервер недоступен")
            items = []
            for channel in guild.text_channels:
                access = channel.permissions_for(guild.me)
                if access.view_channel:
                    items.append(
                        {
                            "id": str(channel.id),
                            "name": channel.name[:40],
                            "send": access.send_messages,
                        }
                    )
            emit(
                "data",
                data={
                    "channels": {
                        "guild_id": str(guild.id),
                        "count": len(items),
                        "items": items[:60],
                    }
                },
            )
        elif action == "send_message":
            channel = client.get_channel(int(request.get("channel_id", "0")))
            text = request.get("text", "")
            if not isinstance(channel, discord.TextChannel) or str(channel.guild.id) != str(
                request.get("guild_id")
            ):
                raise ValueError("Выберите текстовый канал нужного сервера")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise ValueError("Сообщение: от 1 до 2000 символов")
            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.view_channel or not permissions.send_messages:
                raise ValueError("Нет прав отправлять в этот канал")
            await asyncio.wait_for(
                channel.send(text.strip(), allowed_mentions=discord.AllowedMentions.none()), 25
            )
            emit(
                "data",
                data={"action": {"id": ident, "ok": True, "text": "Отправлено в #" + channel.name}},
            )
    except Exception as exc:
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "Discord не подтвердил действие. Проверьте канал перед повтором."
        )
        emit("data", data={"action": {"id": ident, "ok": False, "text": message}})


def install():
    import discord

    if getattr(discord.Client, "_krest_bridge_installed", False):
        return
    original_start, original_dispatch = discord.Client.start, discord.Client.dispatch

    async def start(client, token, *args, **kwargs):
        loop = asyncio.get_running_loop()

        def reader():
            for line in sys.stdin:
                if len(line) > 8192 or loop.is_closed():
                    continue
                future = None
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        continue
                    future = asyncio.run_coroutine_threadsafe(control(client, request), loop)
                    future.result(timeout=35)
                except Exception:
                    if future is not None:
                        future.cancel()
                    emit("error", "Команда моста не завершена; проверьте состояние перед повтором")

        if not getattr(client, "_krest_reader", False):
            client._krest_reader = True
            threading.Thread(target=reader, daemon=True, name="krest-discord-bridge").start()
        token = os.environ.get("KREST_SERVICE_TOKEN") or os.environ.get("DISCORD_TOKEN") or token
        return await original_start(client, token, *args, **kwargs)

    def dispatch(client, event, *args, **kwargs):
        original_dispatch(client, event, *args, **kwargs)
        if event in ("ready", "resumed"):
            emit(
                "ready",
                "Папка Discord-бота подключена",
                capabilities=["refresh", "channels", "send_message"],
            )
            overview(client)
        elif event == "disconnect":
            emit("disconnected", "Discord переподключается")

    discord.Client.start, discord.Client.dispatch = start, dispatch
    discord.Client._krest_bridge_installed = True
