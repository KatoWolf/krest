"""Bot regressions with in-memory clients; never connect to external accounts."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from steam_service import SteamClient, SteamError


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return Response(next(self.responses))


def test_offers_paginates_and_deduplicates():
    session = Session(
        [
            {"response": {"trade_offers_received": [{"tradeofferid": "12"}], "next_cursor": 7}},
            {
                "response": {
                    "trade_offers_received": [{"tradeofferid": "12"}, {"tradeofferid": "13"}],
                    "trade_offers_sent": [{"tradeofferid": "14"}],
                }
            },
        ]
    )
    offers = SteamClient("key", "76561198000000000", session).offers()
    assert [(offer["id"], offer["direction"]) for offer in offers] == [
        ("12", "in"),
        ("13", "in"),
        ("14", "out"),
    ]
    assert session.calls[1]["params"]["cursor"] == "7"


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"trade_offers_received": None},
        {"trade_offers_sent": [None]},
        {"trade_offers_received": [{"tradeofferid": "bad"}]},
    ],
)
def test_offers_rejects_malformed_data(response):
    with pytest.raises(SteamError):
        SteamClient("key", "76561198000000000", Session([{"response": response}])).offers()


def test_offers_repeated_cursor_rejects_partial_result():
    page = {"response": {"next_cursor": 5}}
    with pytest.raises(SteamError, match="страницу"):
        SteamClient("key", "76561198000000000", Session([page, page])).offers()


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    import krest_bot

    monkeypatch.setattr(krest_bot, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(krest_bot, "STEAM_ID", "76561198000000000")
    monkeypatch.setattr(krest_bot, "STEAM_API_KEY", "test-key")
    return krest_bot


def test_terminal_cache_survives_corruption_and_does_not_cross_accounts(terminal, monkeypatch):
    from pathlib import Path

    Path(terminal.CACHE_FILE).write_text("broken")
    terminal.save_cached_data("one", "first")
    terminal.save_cached_data("two", "second")
    assert terminal.get_cached_data("one", 900) == "first"
    assert terminal.get_cached_data("two", 900) == "second"
    monkeypatch.setattr(terminal, "STEAM_ID", "76561198000000001")
    assert terminal.get_cached_data("one", 900) is None


def test_terminal_inventory_uses_confirmed_dates_and_quantities(terminal, monkeypatch):
    items = [{"name": "Item", "tradable": False, "amount": 3, "unlock_at": None}]
    monkeypatch.setattr(
        terminal, "SteamClient", lambda *args: SimpleNamespace(inventory=lambda *args: items)
    )
    text = terminal.check_cs2_inventory()
    assert "Всего: 3" in text and "Steam не указал дату" in text


def test_terminal_failure_is_not_empty_inventory(terminal, monkeypatch):
    def fail(*args):
        raise SteamError("недоступен")

    monkeypatch.setattr(terminal, "SteamClient", fail)
    assert "недоступен" in terminal.check_cs2_inventory()
    terminal.save_cached_data("cs2_inv", "old items")
    text = terminal.check_cs2_inventory(force_update=True)
    assert "old items" in text and "Старые данные" in text


def test_terminal_offers_includes_sent(terminal, monkeypatch):
    monkeypatch.setattr(
        terminal,
        "SteamClient",
        lambda *args: SimpleNamespace(offers=lambda: [{"direction": "in"}, {"direction": "out"}]),
    )
    assert "входящих 1, исходящих 1" in terminal.check_active_trades()


@pytest.mark.parametrize("payload", ["[]", "null", "broken", '{"activity_type":"WORK"}'])
def test_telegram_status_is_always_mapping(tmp_path, monkeypatch, payload):
    import tg_bridge

    path = tmp_path / "status.json"
    path.write_text(payload)
    monkeypatch.setattr(tg_bridge, "STATUS_FILE", str(path))
    assert isinstance(asyncio.run(tg_bridge.get_local_status()), dict)


def test_telegram_urgent_repeat_is_throttled(monkeypatch):
    import tg_bridge

    monkeypatch.setattr(tg_bridge, "spam_control", {1: 100.0})
    monkeypatch.setattr(tg_bridge, "last_outgoing", {})
    monkeypatch.setattr(tg_bridge.time, "monotonic", lambda: 110.0)
    monkeypatch.setattr(tg_bridge, "get_owner_name", lambda: "Owner")
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1), text="срочно"
    )
    client = SimpleNamespace(send_chat_action=AsyncMock())
    asyncio.run(tg_bridge.auto_reply(client, message))
    client.send_chat_action.assert_not_called()


def test_discord_status_and_background_shutdown(tmp_path, monkeypatch):
    import discord
    import ds_session

    path = tmp_path / "status.json"
    path.write_text("null")
    monkeypatch.setattr(ds_session, "STATUS_FILE", str(path))
    monkeypatch.setattr(discord.Client, "close", AsyncMock())

    async def exercise():
        client = ds_session.KrestDS(intents=discord.Intents.none())
        assert await client.get_local_status() == {}
        task = client._spawn(asyncio.sleep(100))
        await client.close()
        assert task.cancelled() and not client._background_tasks

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "method, payload",
    [
        ("owned_games", {"response": None}),
        ("owned_games", {"response": {"game_count": 1, "games": [None]}}),
        ("bans", {"players": [None]}),
        ("inventory", {"success": 1, "assets": [None]}),
        ("inventory", {"success": 1, "descriptions": [None]}),
    ],
)
def test_malformed_steam_records_are_recoverable_errors(method, payload):
    client = SteamClient("key", "76561198000000000", Session([payload]))
    with pytest.raises(SteamError):
        getattr(client, method)(*(("730", "2") if method == "inventory" else ()))


def test_invalid_inventory_context_never_makes_request():
    session = Session([])
    with pytest.raises(SteamError):
        SteamClient("key", "76561198000000000", session).inventory("730", "../other")
    assert not session.calls


def _daytime():
    return SimpleNamespace(now=lambda: SimpleNamespace(hour=12))


def test_discord_channel_reservation_race_length_and_outgoing_reset(monkeypatch, capsys):
    import discord
    import ds_session
    from contextlib import asynccontextmanager

    class Channel:
        id = 999
        send = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))

        @asynccontextmanager
        async def typing(self):
            yield

    monkeypatch.setattr(discord, "DMChannel", Channel)
    monkeypatch.setattr(ds_session.datetime, "datetime", _daytime())
    monkeypatch.setattr(ds_session.config, "get_owner_name", lambda: "Owner")
    original_sleep = asyncio.sleep
    monkeypatch.setattr(ds_session.asyncio, "sleep", lambda _: original_sleep(0))
    message = SimpleNamespace(
        author=SimpleNamespace(id=1, bot=False, name="Sender"),
        channel=Channel(),
        content="срочно",
        attachments=[],
    )

    async def exercise():
        client = ds_session.KrestDS(intents=discord.Intents.none())
        client._connection.user = SimpleNamespace(id=2)
        client.get_local_status = AsyncMock(
            return_value={"activity_type": "WORK", "full_message": "😀" * 10000}
        )
        client.auto_delete_message = AsyncMock()
        await asyncio.gather(client.on_message(message), client.on_message(message))
        assert message.channel.send.await_count == 1
        text = message.channel.send.await_args.args[0]
        assert len(text.encode("utf-16-le")) // 2 <= 2000 and "Попробую удалить" in text
        assert 999 in client.spam_control and 1 not in client.spam_control
        await client.on_message(SimpleNamespace(author=client.user, channel=message.channel))
        assert 999 not in client.spam_control
        # An outgoing message arriving during the typing delay cancels the reply.
        client.last_outgoing.clear()
        message.channel.send.reset_mock()

        async def answer_during_delay(_):
            await client.on_message(SimpleNamespace(author=client.user, channel=message.channel))

        monkeypatch.setattr(ds_session.asyncio, "sleep", answer_during_delay)
        await client.on_message(message)
        message.channel.send.assert_not_called()
        # Exceptions from libraries must not expose raw request URLs/tokens.
        client.last_outgoing.clear()
        client.spam_control.clear()
        client.get_local_status = AsyncMock(side_effect=RuntimeError("token-super-secret"))
        await client.on_message(message)

    asyncio.run(exercise())
    assert "token-super-secret" not in capsys.readouterr().out


def test_telegram_reservation_race_length_and_safe_errors(monkeypatch, caplog):
    import tg_bridge

    monkeypatch.setattr(tg_bridge, "spam_control", {})
    monkeypatch.setattr(tg_bridge, "last_outgoing", {})
    monkeypatch.setattr(tg_bridge, "get_owner_name", lambda: "Owner")
    monkeypatch.setattr(tg_bridge.datetime, "datetime", _daytime())
    monkeypatch.setattr(
        tg_bridge,
        "get_local_status",
        AsyncMock(return_value={"activity_type": "WORK", "full_message": "😀" * 10000}),
    )
    monkeypatch.setattr(tg_bridge, "auto_delete_message", AsyncMock())
    original_sleep = asyncio.sleep
    monkeypatch.setattr(tg_bridge.asyncio, "sleep", lambda _: original_sleep(0))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, first_name="Sender"),
        chat=SimpleNamespace(id=999),
        text="срочно",
        voice=None,
        video_note=None,
        reply=AsyncMock(),
    )
    client = SimpleNamespace(send_chat_action=AsyncMock())

    async def exercise():
        await asyncio.gather(
            tg_bridge.auto_reply(client, message), tg_bridge.auto_reply(client, message)
        )
        assert message.reply.await_count == 1
        text = message.reply.await_args.args[0]
        assert len(text.encode("utf-16-le")) // 2 <= 4096 and "Попробую удалить" in text
        assert 999 in tg_bridge.spam_control and 1 not in tg_bridge.spam_control
        await tg_bridge.track_outgoing(client, message)
        assert 999 not in tg_bridge.spam_control
        tg_bridge.last_outgoing.clear()
        message.reply.reset_mock()

        async def answer_during_delay(_):
            await tg_bridge.track_outgoing(client, message)

        monkeypatch.setattr(tg_bridge.asyncio, "sleep", answer_during_delay)
        await tg_bridge.auto_reply(client, message)
        message.reply.assert_not_called()
        tg_bridge.last_outgoing.clear()
        tg_bridge.spam_control.clear()
        client.send_chat_action.side_effect = RuntimeError("token-super-secret")
        await tg_bridge.auto_reply(client, message)

    asyncio.run(exercise())
    assert "token-super-secret" not in caplog.text
