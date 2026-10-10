import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
import licensing
from configuration import ConfigManager
from integrations import IntegrationManager


def test_signed_license_and_expiration(issuer):
    from tools.license_admin import issue

    code = issue(issuer[0], days=1)
    cfg = {"license": {"code": code}}
    assert licensing.status(cfg)["premium"]
    assert not licensing.status(cfg, now=time.time() + 86401)["premium"]
    assert not licensing.status(cfg, now=0)["premium"]
    assert not licensing.status({"license": {"is_premium": True}})["premium"]


def test_tampering_and_foreign_issuer_rejected(issuer, tmp_path):
    from tools.license_admin import generate, issue

    prefix, body, signature = issuer[2].split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["id"] = "forged"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    assert not licensing.status({"license": {"code": ".".join([prefix, forged, signature])}})[
        "premium"
    ]
    private, public = tmp_path / "foreign.private.pem", tmp_path / "foreign.pem"
    generate(private, public)
    assert not licensing.status({"license": {"code": issue(private)}})["premium"]


def test_ordinary_edition_cannot_start_or_send_to_bots(tmp_path, monkeypatch):
    cfg = ConfigManager(tmp_path / "config.json")
    manager = IntegrationManager(SimpleNamespace(config_mgr=cfg), base_dir=tmp_path)
    spawn = MagicMock()
    monkeypatch.setattr("integrations.subprocess.Popen", spawn)
    with pytest.raises(PermissionError):
        manager.start("service.py")
    with pytest.raises(PermissionError):
        manager.send("service.py", "refresh")
    spawn.assert_not_called()
    manager.stop_all()


def test_deactivation_stops_owned_services(premium_config, tmp_path, monkeypatch):
    manager = IntegrationManager(SimpleNamespace(config_mgr=premium_config), base_dir=tmp_path)
    manager.processes = {"service.py": object()}
    manager.stop = MagicMock()
    manager.enforce_license()
    manager.stop.assert_not_called()
    cfg = premium_config.snapshot()
    cfg["license"]["code"] = ""
    premium_config.save_config(cfg)
    manager.enforce_license()
    manager.stop.assert_called_once_with("service.py")


def test_new_profile_blank_and_legacy_default_migration(tmp_path):
    path = tmp_path / "config.json"
    cfg = ConfigManager(path)
    assert cfg.snapshot()["owner_name"] == ""
    assert not cfg.snapshot()["profile"]["onboarding_completed"]
    path.write_text(json.dumps({"owner_name": "Игорь", "custom": 42}), encoding="utf-8")
    cfg = ConfigManager(path)
    assert cfg.snapshot()["owner_name"] == "" and cfg.snapshot()["custom"] == 42
    data = cfg.snapshot()
    data["owner_name"] = "Мария"
    data["profile"]["onboarding_completed"] = True
    cfg.save_config(data)
    assert ConfigManager(path).snapshot()["owner_name"] == "Мария"


def test_response_personality_and_quiet_mode_are_local(tmp_path):
    from app_runtime import AssistantRuntime

    cfg = ConfigManager(tmp_path / "config.json")
    data = cfg.snapshot()
    data["owner_name"] = "Саша"
    cfg.save_config(data)
    app = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    try:
        one = app.execute("привет")
        two = app.execute("привет")
        assert one.success and "Саша" in one.text and one.text != two.text
        assert "Саша" in app.execute("как меня зовут").text
        app.execute("тихий режим")
        app.execute("тихий режим")
        assert app.state.is_muted
        app.execute("верни голос")
        assert not app.state.is_muted
        assert "Ежедневных дел" in app.execute("план на сегодня").text
        assert "Запускаю рабочее окружение" not in app.execute("доброе утро").text
    finally:
        app.close()


def test_all_catalog_phrases_dispatch_to_their_own_skill():
    from skill_manager import SkillManager

    manager = SkillManager()
    assert not manager.errors
    for entry in manager.catalog():
        for phrase in [*entry.keywords, *entry.examples]:
            matched = manager.match(phrase)
            assert matched and matched[0].id == entry.id, (phrase, entry.id, matched)


def test_embedded_actions_in_queries_do_not_execute():
    from skill_manager import SkillManager

    manager = SkillManager()
    assert manager.match("найди в интернете как поставить музыку на паузу")[0].module == "webo"
    assert manager.match("не поставь лайк") is None
    assert manager.match("запусти таймер на 2 минуты")[0].func.__name__ == "set_timer"


def test_frozen_process_inspection_is_read_only(monkeypatch):
    from skills import windows

    process = MagicMock(pid=123)
    process.name.return_value = "example.exe"
    monkeypatch.setattr(windows, "find_hung_processes", lambda: [process])
    assert "example.exe" in windows.report_frozen_processes()
    process.kill.assert_not_called()
    assert "example.exe" in windows.kill_frozen_processes()
    process.kill.assert_called_once()


def test_windows_shortcuts_use_explicit_minimize_and_os_notifications(monkeypatch):
    from skills import windows

    press = MagicMock()
    monkeypatch.setattr(windows, "press_key_combination", press)
    windows.minimize_all_windows()
    press.assert_called_with([windows.VK_LWIN, windows.VK_M])
    monkeypatch.setattr(
        windows.sys, "getwindowsversion", lambda: SimpleNamespace(build=22621), raising=False
    )
    windows.open_action_center()
    press.assert_called_with([windows.VK_LWIN, windows.VK_N])


def test_structured_failure_is_not_hidden_by_a_spoken_prelude(tmp_path, monkeypatch):
    from skill_manager import SkillManager, SkillEntry
    from app_state import AssistantState
    import voice

    def failure():
        voice.speak("Проверяю приложение.")
        return {"status": "error", "message": "Приложение отклонило команду."}

    manager = SkillManager()
    monkeypatch.setattr(manager, "match", lambda text: (SkillEntry(failure), {}))
    result = manager.execute("test", AssistantState(tmp_path / "state.json"))
    assert not result.success
    assert "отклонило" in result.text
