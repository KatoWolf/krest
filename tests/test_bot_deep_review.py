"""Offline regressions for bot stop, ownership and deferred refresh lifecycles."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_terminal_normal_polling_stop_does_not_restart(monkeypatch, tmp_path):
    import krest_bot

    client = SimpleNamespace(initialize=Mock(), get_me=Mock(), infinity_polling=Mock())
    monkeypatch.setattr(krest_bot, "bot", client)
    monkeypatch.setattr(krest_bot, "TOKEN", "dummy")
    monkeypatch.setattr(krest_bot, "MY_ID", 123)
    monkeypatch.setattr(krest_bot, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(
        krest_bot.time, "sleep", Mock(side_effect=AssertionError("unexpected restart"))
    )
    krest_bot.run_krest_service()
    client.infinity_polling.assert_called_once()


def test_discord_normal_stop_does_not_restart(monkeypatch):
    import ds_session

    client = SimpleNamespace(run=Mock())
    monkeypatch.setattr(ds_session, "KrestDS", lambda **kwargs: client)
    monkeypatch.setattr(ds_session.config, "DS_USER_TOKEN", "dummy")
    monkeypatch.setattr(
        ds_session.time, "sleep", Mock(side_effect=AssertionError("unexpected restart"))
    )
    ds_session.start_ds_bridge()
    client.run.assert_called_once()


def test_steam_delivery_failure_survives_api_recovery_until_delivery_or_opt_out(monkeypatch):
    import steam_bot

    events = []
    monkeypatch.delenv("KREST_BOT_TOKEN", raising=False)
    monkeypatch.setattr(steam_bot, "emit", lambda kind, text, **kw: events.append(kind))
    cfg = {"steam": {"telegram_notifications": True}, "api_keys": {}}
    health = steam_bot.SteamHealthReporter()
    data = {"games": [], "errors": {}}
    health.notify("event", cfg)  # Missing credentials: no HTTP request can occur.
    assert health.delivery_error
    health.report(data, cfg, remote=True)
    assert events[-1] == "error"
    health.report(data, cfg, remote=False)
    assert events[-1] == "error"
    monkeypatch.setattr(steam_bot, "deliver_notification", lambda *args: "")
    health.notify("next event", cfg)
    health.report(data, cfg, remote=True)
    assert events[-1] == "ready" and not health.delivery_error
    health.delivery_error = "another delivery failure"
    cfg["steam"]["telegram_notifications"] = False
    health.report(data, cfg, remote=True)
    assert events[-1] == "ready" and not health.delivery_error


def test_refresh_cooldown_preserves_and_coalesces_pending_request():
    from steam_bot import RefreshSchedule

    schedule = RefreshSchedule()
    assert schedule.due(100, requested=True)
    schedule.next_remote = 1000
    assert not schedule.due(110, requested=True)
    assert schedule.pending
    assert not schedule.due(120, requested=True)
    assert schedule.wait_seconds(145, 30) == 15
    assert not schedule.due(159)
    assert schedule.due(160)
    assert not schedule.pending
    assert not schedule.due(161)
    assert schedule.wait_seconds(161, 30) == 30


@pytest.fixture
def terminal_config(tmp_path, monkeypatch):
    import configuration
    import krest_bot

    config = configuration.ConfigManager(tmp_path / "config.json")
    cfg = config.snapshot()
    cfg["api_keys"]["telegram_owner_id"] = "123"
    cfg["remote_control"]["allow_uploads"] = False
    config.save_config(cfg)
    monkeypatch.setattr(configuration, "ConfigManager", lambda: config)
    monkeypatch.setattr(krest_bot, "MY_ID", 123)  # Stale import-time identity remains unchanged.
    reply = Mock()
    monkeypatch.setattr(krest_bot.bot, "client", SimpleNamespace(reply_to=reply))
    return config, reply


def message(owner):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=owner),
        chat=SimpleNamespace(id=owner, type="private"),
        text="/start",
    )


@pytest.mark.parametrize("handler", ["terminal_handler", "handle_remote_upload"])
def test_saved_owner_change_immediately_revokes_old_owner(terminal_config, handler):
    import krest_bot

    cfg, reply = terminal_config
    receive = getattr(krest_bot, handler)
    receive(message(123))
    assert reply.call_count == 1
    values = cfg.snapshot()
    values["api_keys"]["telegram_owner_id"] = "456"
    cfg.save_config(values)
    receive(message(123))
    assert reply.call_count == 1
    receive(message(456))
    assert reply.call_count == 2
    values["api_keys"]["telegram_owner_id"] = ""
    cfg.save_config(values)
    receive(message(456))
    assert reply.call_count == 2


@pytest.mark.parametrize("owner", ["", "0", "-1", "bad", None, True])
def test_owner_identity_fails_closed(owner):
    import krest_bot

    assert not krest_bot.is_current_owner(message(123), {"api_keys": {"telegram_owner_id": owner}})


def test_owner_requires_exact_private_sender_chat_and_no_static_decorator():
    import krest_bot

    cfg = {"api_keys": {"telegram_owner_id": "123"}}
    item = message(123)
    item.chat.type = "group"
    assert not krest_bot.is_current_owner(item, cfg)
    item.chat.type = "private"
    item.chat.id = 456
    assert not krest_bot.is_current_owner(item, cfg)
    item.chat.id = 123
    item.from_user = None
    assert not krest_bot.is_current_owner(item, cfg)
    for callback, filters in krest_bot.bot.handlers:
        if callback in (krest_bot.terminal_handler, krest_bot.handle_remote_upload):
            assert "func" not in filters


def test_forged_owner_id_text_from_foreign_sender_is_ignored(terminal_config):
    import krest_bot

    _, reply = terminal_config
    item = message(999)
    item.text = '{"owner_id":123,"command":"статус","from_user":{"id":123}}'
    item.owner_id = 123
    item.telegram_owner_id = 123
    krest_bot.terminal_handler(item)
    krest_bot.handle_remote_upload(item)
    reply.assert_not_called()


def test_authorized_oversized_command_is_rejected_before_casefold(terminal_config, monkeypatch):
    import kernel_client
    import krest_bot

    _, reply = terminal_config
    client = Mock(side_effect=AssertionError("IPC must not be constructed"))
    monkeypatch.setattr(kernel_client, "KernelClient", client)

    class Oversized(str):
        def casefold(self):
            raise AssertionError("oversized input must not be normalized")

        def strip(self, *args):
            raise AssertionError("oversized input must not be copied")

    item = message(123)
    item.text = Oversized("x" * 100000)
    krest_bot.terminal_handler(item)
    reply.assert_called_once()
    assert len(reply.call_args.args[1]) < 100
    client.assert_not_called()


@pytest.mark.parametrize(
    "command",
    ["выключи компьютер", "удали все файлы", "запусти powershell", "перезагрузи компьютер"],
)
def test_dangerous_commands_default_denied_without_ipc(terminal_config, monkeypatch, command):
    import kernel_client
    import krest_bot

    cfg, reply = terminal_config
    values = cfg.snapshot()
    values["remote_control"]["enabled"] = True
    values["remote_control"]["block_when_locked"] = False
    cfg.save_config(values)
    client = Mock(side_effect=AssertionError("IPC must not be constructed"))
    monkeypatch.setattr(kernel_client, "KernelClient", client)
    item = message(123)
    item.text = command
    krest_bot.terminal_handler(item)
    assert "разрешённом списке" in reply.call_args.args[1]
    client.assert_not_called()


def test_huge_owner_digits_fail_closed_without_integer_conversion():
    import krest_bot

    assert not krest_bot.is_current_owner(
        message(123), {"api_keys": {"telegram_owner_id": "1" * 100000}}
    )


def test_terminal_read_timeout_backoff_then_clean_stop(monkeypatch, tmp_path):
    import requests
    import krest_bot

    client = SimpleNamespace(
        initialize=Mock(),
        get_me=Mock(),
        infinity_polling=Mock(side_effect=[requests.ReadTimeout("secret"), None]),
    )
    pause = Mock()
    monkeypatch.setattr(krest_bot, "bot", client)
    monkeypatch.setattr(krest_bot, "TOKEN", "dummy")
    monkeypatch.setattr(krest_bot, "MY_ID", 123)
    monkeypatch.setattr(krest_bot, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(krest_bot.time, "sleep", pause)
    krest_bot.run_krest_service()
    assert client.infinity_polling.call_count == 2
    pause.assert_called_once_with(3)


def test_discord_exception_backoff_then_clean_stop(monkeypatch):
    import ds_session

    client = SimpleNamespace(run=Mock(side_effect=[OSError("offline"), None]))
    pause = Mock()
    monkeypatch.setattr(ds_session, "KrestDS", lambda **kwargs: client)
    monkeypatch.setattr(ds_session.config, "DS_USER_TOKEN", "dummy")
    monkeypatch.setattr(ds_session.time, "sleep", pause)
    ds_session.start_ds_bridge()
    assert client.run.call_count == 2
    pause.assert_called_once_with(10)
