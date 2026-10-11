"""Terminal health observes existing SDK polling, entirely offline."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests


def test_sdk_internal_retry_updates_health_without_extra_requests(monkeypatch):
    import bridge_sdk
    import krest_bot
    import telebot

    events = []
    monkeypatch.setattr(
        bridge_sdk, "emit", lambda kind, text, **kw: events.append((kind, text, kw))
    )
    fetch = Mock(side_effect=[requests.ConnectionError("https://secret-token"), []])
    monkeypatch.setattr(telebot.apihelper, "get_updates", fetch)
    monkeypatch.setattr(telebot.time, "sleep", Mock())
    client = krest_bot.ObservedTeleBot("12345:offline", threaded=False)
    client.health.waiting()

    def poll(**kwargs):
        # Exercise the installed SDK retrieval method and infinity retry loop.
        client._TeleBot__retrieve_updates(timeout=7, long_polling_timeout=11)
        client.stop_polling()

    monkeypatch.setattr(client, "polling", poll)
    client.infinity_polling(logger_level=None)
    assert fetch.call_count == 2
    assert [event[0] for event in events] == ["status", "error", "ready"]
    assert events[0][2]["health"] == "unknown"
    assert "secret-token" not in str(events)
    assert fetch.call_args.kwargs["timeout"] == 7
    assert fetch.call_args.kwargs["long_polling_timeout"] == 11


@pytest.mark.parametrize("code", [401, 409, 429, None])
def test_poll_failure_preserves_exception_and_recovers(monkeypatch, code):
    import bridge_sdk
    import krest_bot
    import telebot

    events = []
    monkeypatch.setattr(bridge_sdk, "emit", lambda *args, **kwargs: events.append((args, kwargs)))
    error = RuntimeError("private-token")
    error.error_code = code
    request = Mock(side_effect=[error, error, []])
    monkeypatch.setattr(telebot.apihelper, "get_updates", request)
    client = krest_bot.ObservedTeleBot("12345:offline", threaded=False)
    for _ in range(2):
        with pytest.raises(RuntimeError) as caught:
            client.get_updates(offset=123, timeout=2, long_polling_timeout=4)
        assert caught.value is error
    assert len(events) == 1  # Repeated identical failures do not flood the log.
    assert client.get_updates(offset=123) == []
    assert [item[0][0] for item in events] == ["error", "ready"]
    assert "private-token" not in str(events)


def test_startup_get_me_does_not_claim_polling_ready(monkeypatch, tmp_path):
    import bridge_sdk
    import krest_bot

    events = []
    client = SimpleNamespace(
        initialize=Mock(),
        get_me=Mock(),
        infinity_polling=Mock(side_effect=[requests.ReadTimeout("secret"), None]),
    )
    monkeypatch.setattr(krest_bot, "bot", client)
    monkeypatch.setattr(krest_bot, "TOKEN", "fake-token")
    monkeypatch.setattr(krest_bot, "MY_ID", 123)
    monkeypatch.setattr(krest_bot, "UPLOADS_DIR", str(tmp_path))
    monkeypatch.setattr(krest_bot.time, "sleep", Mock())
    monkeypatch.setattr(
        bridge_sdk, "emit", lambda kind, text, **kw: events.append((kind, text, kw))
    )
    krest_bot.run_krest_service()
    assert [event[0] for event in events] == ["status", "error"]
    assert "secret" not in str(events)
    client.get_me.assert_called_once()
    assert client.infinity_polling.call_count == 2


def test_bot_proxy_keeps_handlers_and_one_observed_client(monkeypatch):
    import krest_bot

    client = SimpleNamespace(register_message_handler=Mock())
    create = Mock(return_value=client)
    monkeypatch.setattr(krest_bot, "ObservedTeleBot", create)
    proxy = krest_bot.BotProxy()
    handler = Mock()
    proxy.message_handler(content_types=["text"])(handler)
    proxy.initialize("fake-token")
    proxy.initialize("fake-token")
    create.assert_called_once_with("fake-token")
    client.register_message_handler.assert_called_once_with(handler, content_types=["text"])
