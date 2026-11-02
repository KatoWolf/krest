"""Offline checks for the Telegram update path and native packaged encryption."""


def check_telegram_efficiency():
    import asyncio
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from unittest.mock import AsyncMock, patch

    import tg_bridge as tg
    import tgcrypto
    from configuration import ConfigManager
    from pyrogram.crypto import aes

    # NIST AES-256 vector: IGE with zero IV reduces to ECB for its first block.
    key = bytes.fromhex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    expected = bytes.fromhex("f3eed1bdb5d2a03c064b5a7e3db181f8")
    with patch.object(tgcrypto, "ige256_encrypt", wraps=tgcrypto.ige256_encrypt) as native:
        if aes.ige256_encrypt(plain, key, bytes(32)) != expected or native.call_count != 1:
            raise RuntimeError("Telegram не использует корректное нативное шифрование")
    if aes.ige256_decrypt(expected, key, bytes(32)) != plain:
        raise RuntimeError("Telegram не расшифровывает проверочный блок")

    with TemporaryDirectory(prefix="krest-tg-cache-") as folder:
        path = Path(folder) / "config.json"
        config = ConfigManager(path)
        config.save_config()
        with (
            patch.object(tg, "CONFIG_FILE", str(path)),
            patch.object(tg, "_config_manager", None),
            patch("configuration.ConfigManager", wraps=ConfigManager) as factory,
        ):
            for _ in range(30):
                tg.load_app_config()
            if factory.call_count != 1:
                raise RuntimeError("Telegram заново открывает настройки при каждом обращении")
            draft = config.snapshot()
            draft["telegram_user"]["auto_reply"] = not draft["telegram_user"]["auto_reply"]
            config.save_config(draft)
            if (
                tg.load_app_config()["telegram_user"]["auto_reply"]
                != draft["telegram_user"]["auto_reply"]
            ):
                raise RuntimeError("Telegram не увидел сохранённые настройки")

    async def updates():
        raw = tg.raw.types
        client = tg.Client("offline-check", api_id=1, api_hash="0" * 32, in_memory=True, workers=2)
        users = {
            1: raw.User(id=1, is_self=True, first_name="Owner", restriction_reason=[]),
            2: raw.User(id=2, first_name="Contact", restriction_reason=[]),
            3: raw.User(id=3, bot=True, first_name="Bot", restriction_reason=[]),
        }

        def packet(peer, sender=2, outgoing=False, ident=1):
            return raw.UpdateNewMessage(
                message=raw.Message(
                    id=ident,
                    peer_id=peer,
                    from_id=raw.PeerUser(user_id=sender),
                    date=1,
                    message="Проверка",
                    entities=[],
                    out=outgoing,
                    reply_to=raw.MessageReplyHeader(reply_to_msg_id=100),
                ),
                pts=ident,
                pts_count=1,
            )

        received, sent = AsyncMock(), AsyncMock()
        with (
            patch.object(tg, "auto_reply", received),
            patch.object(tg, "track_outgoing", sent),
            patch.object(client, "invoke", AsyncMock(side_effect=AssertionError("Сетевой запрос"))),
            patch.object(
                client, "get_messages", AsyncMock(side_effect=AssertionError("История цитат"))
            ),
            patch.object(tg.types.Message, "_parse", wraps=tg.types.Message._parse) as parse,
        ):
            tg.configure_private_updates(client)
            await asyncio.sleep(0)  # Let Pyrogram register the raw handler.
            await client.dispatcher.start()
            try:
                for _ in range(200):
                    client.dispatcher.updates_queue.put_nowait(
                        (packet(raw.PeerChannel(channel_id=9)), users, {})
                    )
                client.dispatcher.updates_queue.put_nowait(
                    (packet(raw.PeerUser(user_id=3), sender=3), users, {})
                )
                client.dispatcher.updates_queue.put_nowait(
                    (packet(raw.PeerUser(user_id=2)), users, {})
                )
                client.dispatcher.updates_queue.put_nowait(
                    (packet(raw.PeerUser(user_id=2), sender=1, outgoing=True, ident=2), users, {})
                )
            finally:
                await client.dispatcher.stop()  # FIFO sentinels drain the whole update burst.
                client.executor.shutdown(wait=True)
            if received.await_count != 1 or sent.await_count != 1 or parse.call_count != 2:
                raise RuntimeError("Личные сообщения потеряны или разбираются лишние обновления")
            message = received.await_args.args[1]
            if (
                message.chat.id != 2
                or message.text != "Проверка"
                or message.reply_to_message is not None
            ):
                raise RuntimeError("Изменилось содержимое личного сообщения")

    asyncio.run(asyncio.wait_for(updates(), timeout=10))
    print(
        "Telegram: native AES, cached settings, personal incoming/outgoing update burst verified.",
        flush=True,
    )
