import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from auto_replies import ReplyGate, render_reply, STYLES
from configuration import ConfigManager
from game_routes import RouteController, validate_route
from storage import atomic_write_json


def test_styles_do_not_leak_activity_or_promise_delivery():
    for style in STYLES:
        text = render_reply(
            "Тенно",
            {"is_playing": True, "full_message": "PRIVATE", "game": "Secret"},
            {"style": style},
        )
        assert "Тенно" in text and "Автоматический ответ" in text
        assert "PRIVATE" not in text and "Secret" not in text and "в игре" not in text
    assert "в игре" in render_reply("Тенно", {"is_playing": True}, {"share_activity": True})


def test_urgent_replies_still_have_cooldown_and_manual_activity_wins():
    now = [1000]
    gate = ReplyGate(lambda: now[0])
    assert gate.allow("user", important=True)
    assert not gate.allow("user", important=True)
    now[0] += 101
    assert gate.allow("user", important=True)
    gate.outgoing("user")
    now[0] += 200
    assert not gate.allow("user", important=True)
    now[0] += 101
    assert gate.allow("user", important=True)
    assert not gate.allow("other", {"enabled": False})


def test_new_settings_validate_and_account_key_is_secret(tmp_path):
    from configuration import validate
    from secret_store import SECRET_KEYS
    from integrations import redact

    cfg = ConfigManager(tmp_path / "config.json").snapshot()
    assert cfg["dota_guide"]["auto_setup"] and cfg["dota_guide"]["enabled"]
    assert not cfg["discord_account"]["enabled"] and not cfg["warframe"]["route_enabled"]
    cfg["api_keys"]["discord_user_token"] = "private-test-token"
    assert "discord_user_token" in SECRET_KEYS
    assert "private-test-token" not in redact("failed private-test-token", cfg)
    cfg["auto_replies"]["cooldown_seconds"] = "10"
    with pytest.raises(ValueError):
        validate(cfg)


def test_dota_provisions_secondary_library_idempotently_and_preserves_old_file(
    tmp_path, monkeypatch
):
    from skills import gid, webo

    monkeypatch.setattr(webo, "get_steam_install_path", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root, second = tmp_path / "Steam", tmp_path / "Other Steam"
    (root / "steamapps").mkdir(parents=True)
    (second / "steamapps/common/dota 2 beta/game/dota").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(
        '"path" "' + str(second).replace("\\", "\\\\") + '"'
    )
    (second / "steamapps/appmanifest_570.acf").write_text(
        '"appid" "570" "name" "Dota 2" "StateFlags" "4" "installdir" "dota 2 beta"'
    )
    monkeypatch.setattr(gid, "dota_state", gid.DotaState())
    monkeypatch.setattr(gid, "GSI_PORT", 3210)
    assert gid.auto_setup_gsi_config(str(root))
    path = Path(gid.dota_state.config_file)
    assert "127.0.0.1:3210" in path.read_text()
    first = path.stat().st_mtime_ns
    assert gid.auto_setup_gsi_config(str(root)) and path.stat().st_mtime_ns == first
    path.write_text("old owned configuration")
    assert gid.auto_setup_gsi_config(str(root))
    assert path.with_suffix(".cfg.bak").read_text() == "old owned configuration"


def test_dota_rejects_escaped_manifest_directory(tmp_path, monkeypatch):
    from skills import gid
    import steam_service

    monkeypatch.setattr(
        steam_service,
        "local_games",
        lambda _: [
            {
                "appid": "570",
                "installed": True,
                "library": str(tmp_path),
                "install_dir": "../../outside",
            }
        ],
    )
    monkeypatch.setattr(gid, "dota_state", gid.DotaState())
    assert not gid.auto_setup_gsi_config(str(tmp_path))
    assert "Некорректная" in gid.dota_state.setup_message


def test_dota_hero_briefing_expires_and_pregame_resets_match(monkeypatch):
    from skills import gid

    monkeypatch.setattr(gid, "dota_state", gid.DotaState(auto_mode=True))
    gid.apply_gsi(
        {
            "map": {"clock_time": 120, "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
            "hero": {"health_percent": 24, "mana_percent": 61},
            "player": {"kills": 2, "deaths": 1, "assists": 3, "gold": 777},
        }
    )
    assert "24" in gid.match_briefing() and "777" in gid.match_briefing()
    gid.dota_state.last_gsi_update -= 30
    assert "Свежих данных" in gid.match_briefing()
    gid.apply_gsi({"map": {"clock_time": -30, "game_state": "DOTA_GAMERULES_STATE_PRE_GAME"}})
    assert not gid.dota_state.match_started


class FakeInput:
    def __init__(self):
        self.events = []
        self.active = True
        self.position = (100, 100)
        self.abort = False

    def foreground(self):
        if not self.active:
            raise ValueError("not foreground")

    def pressed(self):
        return {"w"}

    def panic(self):
        return self.abort

    def cursor(self):
        return self.position

    def send(self, key, down):
        self.events.append((key, down))


def test_route_confirmation_expiry_focus_and_release(tmp_path):
    port = FakeInput()
    ctl = RouteController(tmp_path, lambda: True, lambda: port)
    atomic_write_json(ctl.path, {"version": 1, "duration": 0.5, "events": [[0, ["w"]]]})
    with pytest.raises(ValueError):
        ctl.confirm()
    assert "двадцати" in ctl.request()
    ctl.confirm_until = 0
    with pytest.raises(ValueError):
        ctl.confirm()
    ctl.request()
    ctl.confirm()
    for _ in range(100):
        if port.events:
            break
        time.sleep(0.002)
    assert port.events == [("w", True)]
    port.active = False
    ctl.thread.join(1)
    assert port.events[-1] == ("w", False)
    assert ctl.mode == "idle" and "foreground" in ctl.message
    ctl.close()


def test_route_mouse_takeover_releases_keys_and_does_not_repeat(tmp_path):
    port = FakeInput()
    ctl = RouteController(tmp_path, lambda: True, lambda: port)
    route = {"version": 1, "duration": 1, "events": [[0, ["shift", "w"]]]}
    atomic_write_json(ctl.path, route)
    ctl.request()
    ctl.confirm()
    for _ in range(100):
        if len(port.events) == 2:
            break
        time.sleep(0.002)
    port.position = (140, 100)
    ctl.thread.join(1)
    assert len(port.events) == 4 and not ctl.thread.is_alive()
    assert "движение мыши" in ctl.message
    assert not ctl.snapshot()["confirmation_pending"]
    ctl.close()


def test_route_rejects_nonmovement_keys_and_unbounded_data():
    for value in (
        {"version": 1, "duration": 999, "events": [[0, ["w"]]]},
        {"version": 1, "duration": 1, "events": [[0, ["enter"]]]},
        {"version": 1, "duration": 1, "events": [[0.5, []], [0.2, ["w"]]]},
    ):
        with pytest.raises(ValueError):
            validate_route(value)


def test_account_ignores_guilds_and_disabled_replies_and_uses_styles(tmp_path):
    from discord_account import AccountSession

    cfg = ConfigManager(tmp_path / "config.json")
    cfg.config["owner_name"] = "Тенно"
    cfg.config["discord_account"].update(enabled=True, auto_reply=True)
    cfg.config["auto_replies"].update(style="gothic", delete_seconds=0)
    account = AccountSession(cfg)
    account.config = cfg.snapshot
    account.user_id = "1"
    calls = []

    async def request(*args, **kwargs):
        calls.append((args, kwargs))
        return {"id": "42", "type": 1} if args[1] == "GET" else {"id": "999"}

    account.request = request
    data = {"author": {"id": "2", "username": "friend"}, "channel_id": "42", "content": "Срочно"}

    async def exercise():
        await account.message(None, {**data, "guild_id": "server"})
        assert not calls
        await account.message(None, data)
        await account.message(None, data)
        assert len(calls) == 2
        assert "КРЕСТ · тихий час" in calls[1][1]["json"]["content"]
        assert calls[1][1]["json"]["allowed_mentions"] == {"parse": []}
        cfg.config["discord_account"]["auto_reply"] = False
        await account.message(None, {**data, "channel_id": "43"})
        assert len(calls) == 2

    asyncio.run(exercise())


def test_warframe_briefing_is_bounded_offline_and_can_be_closed(tmp_path):
    from warframe_service import WarframeService

    cfg = ConfigManager(tmp_path / "config.json")
    rt = SimpleNamespace(settings=cfg.snapshot)
    service = WarframeService(rt, tmp_path)

    def offline(*a, **kw):
        raise RuntimeError("offline")

    service.world = offline
    try:
        service.action("add_goal", name="Собрать Некроса")
        result = service.action("show_briefing")
        panel = service.status()["panel"]
        assert result["stale"] and panel["stale"]
        assert len(panel["lines"]) <= 6 and any("Некроса" in line for line in panel["lines"])
        assert 80 < panel["until"] - time.time() <= 90
        service.action("hide_panel")
        assert not service.status()["panel"]
    finally:
        service.close()


def test_voice_emergency_release_does_not_wait_for_busy_command(tmp_path):
    from app_runtime import AssistantRuntime

    runtime = AssistantRuntime(lambda: {}, data_dir=tmp_path)
    calls = []
    runtime.warframe.routes.cancel = lambda: calls.append("release")
    try:
        runtime._execute_lock.acquire()
        assert runtime.submit("Крест, верни управление", source="voice")
        assert calls == ["release"]
    finally:
        runtime._execute_lock.release()
        runtime.close()


def test_record_saves_only_allowed_movement_and_cancel_joins(tmp_path):
    port = FakeInput()
    ctl = RouteController(tmp_path, lambda: True, lambda: port)
    try:
        ctl.record()
        time.sleep(0.52)
        ctl.cancel()
        from storage import read_json

        value = validate_route(read_json(ctl.path))
        assert value["events"][0][1] == ["w"]
        assert not ctl.thread.is_alive() and not port.events
    finally:
        ctl.close()
