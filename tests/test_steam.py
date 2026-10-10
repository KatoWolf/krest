import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from configuration import ConfigManager
from steam_service import (
    SteamClient,
    SteamError,
    SteamMonitor,
    local_games,
    unlock_time,
    inventory_summary,
)


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError("status")


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def client(*responses):
    return SteamClient("fake-key", "76561198000000000", Session(*responses))


def asset(ident, klass="class"):
    return {"assetid": ident, "classid": klass, "instanceid": "0", "amount": "1"}


def description(tradable=0):
    return {
        "classid": "class",
        "instanceid": "0",
        "tradable": tradable,
        "name": "CS2 item",
        "market_tradable_restriction": 7,
    }


def test_inventory_pagination_and_unknown_dates():
    api = client(
        Response(
            {
                "success": 1,
                "assets": [asset("1")],
                "descriptions": [description()],
                "more_items": 1,
                "last_assetid": "1",
                "total_inventory_count": 2,
            }
        ),
        Response(
            {
                "success": 1,
                "assets": [asset("2", "missing")],
                "more_items": 0,
                "total_inventory_count": 2,
            }
        ),
    )
    result = api.inventory("730", "2")
    assert len(result) == 2
    assert api.session.calls[1][1]["params"]["start_assetid"] == "1"
    assert result[0]["tradable"] is False and result[0]["unlock_at"] is None
    assert result[1]["tradable"] is None
    assert inventory_summary(result) == {"total": 2, "locked": 1, "tradable": 0, "unknown": 1}


def test_explicit_steam_unlock_time_is_utc():
    value = {"owner_descriptions": [{"value": "Tradable After Oct 17, 2026 (7:00:00) GMT"}]}
    assert unlock_time(value) == datetime(2026, 10, 17, 7, tzinfo=timezone.utc).timestamp()
    assert unlock_time({"market_tradable_restriction": 7}) is None


def test_private_library_and_rate_limit_are_errors():
    with pytest.raises(SteamError, match="закрыта"):
        client(Response({"response": {}})).owned_games()
    with pytest.raises(SteamError, match="частоту"):
        client(Response({}, 429)).inventory("730", "2")
    with pytest.raises(SteamError, match="не полностью"):
        client(Response({"success": 1, "assets": [], "total_inventory_count": 3})).inventory(
            "730", "2"
        )


def test_library_discovers_additional_drive_and_appids(tmp_path, monkeypatch):
    from skills import webo

    monkeypatch.setattr(webo, "get_steam_install_path", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root, second = tmp_path / "Steam", tmp_path / "Other Library"
    for folder in (root, second):
        (folder / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders" { "1" { "path" "' + str(second) + '" } }'
    )
    (second / "steamapps/appmanifest_730.acf").write_text(
        '"AppState" { "appid" "730" "name" "Counter-Strike 2" "StateFlags" "4" }'
    )
    assert local_games(str(root)) == [
        {"appid": "730", "name": "Counter-Strike 2", "installed": True, "library": str(second)}
    ]


class FakeClient:
    items = [
        {
            "assetid": "1",
            "name": "Item",
            "amount": 1,
            "tradable": False,
            "unlock_at": 1,
            "appid": "730",
            "contextid": "2",
        }
    ]
    failure = False

    def __init__(self, *args):
        pass

    def owned_games(self):
        return [{"appid": "730", "name": "Counter-Strike 2"}]

    def bans(self):
        return {"economy_ban": "none"}

    def offers(self):
        return []

    def inventory(self, *args):
        if self.failure:
            raise SteamError("offline")
        return copy.deepcopy(self.items)


def test_monitor_only_notifies_confirmed_unlock_once_and_preserves_failures(tmp_path, monkeypatch):
    import steam_service

    monkeypatch.setattr(steam_service, "local_games", lambda *args: [])
    monkeypatch.setattr(FakeClient, "items", copy.deepcopy(FakeClient.items))
    monkeypatch.setattr(FakeClient, "failure", False)
    cfg = ConfigManager(tmp_path / "config.json").snapshot()
    cfg["api_keys"]["steam_id"] = "76561198000000000"
    cfg["steam"]["inventories"] = "730:2"
    notifications = []
    path = tmp_path / "steam.json"
    monitor = SteamMonitor(path, notifications.append, FakeClient)
    monitor.refresh(cfg)
    assert notifications == []  # Past deadline alone never implies unlocked.
    FakeClient.failure = True
    failed = monitor.refresh(cfg)
    assert failed["inventories"]["730:2"]["summary"]["locked"] == 1
    assert failed["errors"]["730:2"] == "offline"
    FakeClient.failure = False
    FakeClient.items[0]["tradable"] = True
    monitor.refresh(cfg)
    assert len(notifications) == 1 and "обмен стал доступен" in notifications[0]
    SteamMonitor(path, notifications.append, FakeClient).refresh(cfg)
    assert len(notifications) == 1
    assert len(json.loads(path.read_text())["notices"]) == 1


def test_missing_item_is_not_reported_as_unlocked(tmp_path, monkeypatch):
    import steam_service

    monkeypatch.setattr(steam_service, "local_games", lambda *args: [])
    monkeypatch.setattr(FakeClient, "items", copy.deepcopy(FakeClient.items))
    cfg = ConfigManager(tmp_path / "config.json").snapshot()
    cfg["api_keys"]["steam_id"] = "76561198000000000"
    notifications = []
    monitor = SteamMonitor(tmp_path / "steam.json", notifications.append, FakeClient)
    monitor.refresh(cfg)
    FakeClient.items = []
    monitor.refresh(cfg)
    assert not notifications


def test_presence_payload_uses_activity_without_user_token():
    from discord_presence import presence_payload

    cfg = {"discord_presence": {"details": "Крест", "state": "Работа", "follow_activity": True}}
    assert presence_payload(cfg, {"game": "Dota 2", "full_message": "В игре"}) == {
        "details": "Dota 2",
        "state": "В игре",
    }


def test_real_custom_script_handshake_notifications_and_refresh(tmp_path, premium_config):
    from integrations import IntegrationManager
    from notifications import NotificationQueue
    from queue import Empty

    cfg = premium_config
    values = cfg.snapshot()
    values["services"] = {
        "custom-test.py": {
            "title": "Test",
            "path": str(Path("examples/bridge_example.py").resolve()),
            "token": "test-secret",
            "autostart": True,
        }
    }
    cfg.save_config(values)
    notifications = NotificationQueue(tmp_path / "queue.sqlite3")
    app = SimpleNamespace(config_mgr=cfg, runtime=SimpleNamespace(notifications=notifications))
    manager = IntegrationManager(app, base_dir=tmp_path)
    # The example imports the SDK through PYTHONPATH, just like a third-party script.
    import os

    old = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(Path.cwd())
    try:
        manager.autostart()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if manager.details("custom-test.py")["bridge"].get("capabilities") == ["refresh"]:
                break
            time.sleep(0.02)
        manager.send("custom-test.py", "refresh")
        message = None
        while time.monotonic() < deadline:
            try:
                message = notifications.get_nowait()
                break
            except Empty:
                time.sleep(0.02)
        assert message and "уведомление доставлено" in message
        assert "test-secret" not in manager.log_tail("custom-test.py")
    finally:
        manager.stop_all()
        if old is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old
    assert not manager.processes


def test_steam_service_process_builds_local_catalog_without_credentials(
    tmp_path, premium_config, issuer, monkeypatch
):
    from integrations import IntegrationManager

    folder = tmp_path / "Steam/steamapps"
    folder.mkdir(parents=True)
    (folder / "appmanifest_730.acf").write_text('"appid" "730" "name" "CS2" "StateFlags" "4"')
    cfg = premium_config
    values = cfg.snapshot()
    values["steam"]["install_path"] = str(folder.parent)
    cfg.save_config(values)
    # A real child process receives a distribution with this test issuer's public key.
    import shutil

    shutil.copy("steam_bot.py", tmp_path / "steam_bot.py")
    shutil.copy("licensing.py", tmp_path / "licensing.py")
    shutil.copy(issuer[1], tmp_path / "license_public_key.pem")
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    manager = IntegrationManager(SimpleNamespace(config_mgr=cfg), base_dir=tmp_path)
    try:
        manager.start("steam_bot.py")
        deadline = time.monotonic() + 5
        path = tmp_path / "steam_state.json"
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert path.exists(), manager.log_tail("steam_bot.py")
        assert any(
            g["appid"] == "730" and g["installed"] for g in json.loads(path.read_text())["games"]
        )
        manager.send("steam_bot.py", "refresh")
    finally:
        manager.stop_all()


def test_telegram_delivery_is_opt_in_and_targets_owner(monkeypatch):
    import requests
    import steam_bot

    events, sends = [], []
    monkeypatch.delenv("KREST_BOT_TOKEN", raising=False)
    monkeypatch.setattr(steam_bot, "emit", lambda kind, text: events.append((kind, text)))

    def send(url, **kwargs):
        sends.append((url, kwargs))
        return Response({"ok": True})

    monkeypatch.setattr(requests, "post", send)
    cfg = {
        "steam": {"telegram_notifications": False},
        "api_keys": {"krest_bot_token": "fake-token", "telegram_owner_id": "123"},
    }
    steam_bot.deliver_notification("event", cfg)
    assert events == [("notification", "event")] and not sends
    cfg["steam"]["telegram_notifications"] = True
    steam_bot.deliver_notification("event", cfg)
    assert sends[0][1]["json"] == {"chat_id": "123", "text": "event"}


def test_no_notification_or_cache_advance_when_save_fails(tmp_path, monkeypatch):
    import steam_service

    monkeypatch.setattr(steam_service, "local_games", lambda *args: [])
    monkeypatch.setattr(FakeClient, "items", copy.deepcopy(FakeClient.items))
    cfg = ConfigManager(tmp_path / "config.json").snapshot()
    cfg["api_keys"]["steam_id"] = "76561198000000000"
    notices = []
    monitor = SteamMonitor(tmp_path / "steam.json", notices.append, FakeClient)
    monitor.refresh(cfg)
    saved = copy.deepcopy(monitor.cache)
    FakeClient.items[0]["tradable"] = True

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(steam_service, "atomic_write_json", fail)
    with pytest.raises(OSError):
        monitor.refresh(cfg)
    assert monitor.cache == saved and not notices
