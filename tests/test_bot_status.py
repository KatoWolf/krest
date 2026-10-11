"""Offline status and reconnect tests: no external bot is started."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from integrations import IntegrationManager
from service_pages import ServicePages, service_presentation
from bridge_sdk import PREFIX


def manager_for(tmp_path):
    cfg = SimpleNamespace(snapshot=lambda: {}, filepath=tmp_path / "config.json")
    return IntegrationManager(SimpleNamespace(config_mgr=cfg), base_dir=tmp_path)


def test_starting_is_not_claimed_connected():
    view = service_presentation({"running": True, "health": "starting", "uptime": 45})
    assert view["title"] == "Ожидание готовности"
    assert "30 секунд" in view["message"]
    assert not view["buttons"]["start"]
    assert view["buttons"]["stop"]


def test_ready_and_failed_reconnect_states():
    assert service_presentation({"health": "ready", "running": True})["title"] == "Подключён"
    view = service_presentation({"health": "error", "error": "Нет доступа"})
    assert "Нет доступа" in view["message"]
    assert view["buttons"]["restart"]
    assert not view["buttons"]["stop"]


@pytest.mark.parametrize("kwargs", [{"busy": True}, {"premium": False}, {"installed": False}])
def test_unavailable_service_cannot_connect(kwargs):
    view = service_presentation({"running": False}, **kwargs)
    assert not view["buttons"]["start"]
    assert not view["buttons"]["restart"]


def test_pending_recovery_can_be_cancelled():
    view = service_presentation({"restart_in": 5, "error": "Сбой"})
    assert view["buttons"]["stop"]
    assert "через 5 с" in view["message"]


def test_manager_does_not_queue_second_reconnect(tmp_path):
    manager = manager_for(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def reconnect(filename):
        calls.append(filename)
        entered.set()
        assert release.wait(3)
        return "Запущен"

    manager.restart = reconnect
    worker = threading.Thread(target=manager.perform, args=("bot.py", "restart"))
    worker.start()
    try:
        assert entered.wait(2)
        assert manager.details("bot.py")["operation"] == "restart"
        assert manager.perform("bot.py", "restart") == "Операция уже выполняется"
    finally:
        release.set()
        worker.join(3)
    assert calls == ["bot.py"]
    assert manager.details("bot.py")["operation"] is None


def test_error_is_redacted_and_operation_released(tmp_path):
    manager = manager_for(tmp_path)
    manager.app.config_mgr.snapshot = lambda: {"api_keys": {"discord_token": "secret-token"}}
    manager.restart = Mock(side_effect=RuntimeError("Rejected secret-token"))
    with pytest.raises(RuntimeError):
        manager.perform("bot.py", "restart")
    assert manager.details("bot.py")["error"] == "Rejected [скрыто]"
    assert manager.details("bot.py")["operation"] is None


def test_exit_does_not_overwrite_actual_reason(tmp_path):
    manager = manager_for(tmp_path)
    manager.processes["bot.py"] = SimpleNamespace(poll=lambda: 1, stdin=None)
    manager._event(
        "bot.py", PREFIX + json.dumps({"type": "error", "text": "Авторизация отклонена"}), {}
    )
    manager.refresh()
    assert manager.details("bot.py")["error"] == "Авторизация отклонена"


def test_ui_double_click_and_failure_release():
    callbacks, operations = [], []
    app = SimpleNamespace(
        busy_services=set(),
        config_mgr=SimpleNamespace(is_premium=lambda: True),
        save_all_settings=lambda **kw: True,
        _update_service_views=Mock(),
        _background=operations.append,
        post_ui=callbacks.append,
        integration=SimpleNamespace(perform=Mock(side_effect=RuntimeError("offline"))),
        _append_log=Mock(),
    )
    ServicePages._service_action(app, "bot.py", "restart")
    ServicePages._service_action(app, "bot.py", "restart")
    assert len(operations) == 1
    assert app.busy_services == {"bot.py"}
    with pytest.raises(RuntimeError):
        operations[0]()
    for callback in callbacks:
        callback()
    assert not app.busy_services
    assert app._update_service_views.call_count == 2


@pytest.mark.skipif(not __import__("os").getenv("DISPLAY"), reason="GUI requires X display")
def test_real_tk_bot_status_busy_error_ready(premium_config, monkeypatch):
    import logging
    from welcome_gui import ControlCenterApp, indicator_controller

    settings = premium_config.snapshot()
    settings["profile"]["onboarding_completed"] = True
    settings["owner_name"] = "Проверка"
    premium_config.save_config(settings)
    ui = ControlCenterApp(config_path=premium_config.filepath)
    errors = []
    ui.report_callback_exception = lambda *args: errors.append(str(args))
    filename = "krest_bot.py"
    detail = {"running": False, "health": "error", "error": "Токен отклонён", "installed": True}
    monkeypatch.setattr(ui.integration, "details", lambda name: dict(detail))
    monkeypatch.setattr(ui.integration, "refresh", lambda: {})
    monkeypatch.setattr(ui, "save_all_settings", lambda **kw: True)
    operations = []
    monkeypatch.setattr(ui, "_background", operations.append)
    try:
        ui.switch_page("bot_status")
        ui.update()
        label, description, buttons = ui.bot_status_views[filename]
        assert "Ошибка" in label.cget("text")
        assert "Токен отклонён" in description.cget("text")
        assert str(buttons["restart"].cget("state")) == "normal"
        screenshot = __import__("os").getenv("KREST_STATUS_SCREENSHOT")
        if screenshot:
            from PIL import ImageGrab

            ui.update_idletasks()
            ImageGrab.grab(
                bbox=(
                    ui.winfo_rootx(),
                    ui.winfo_rooty(),
                    ui.winfo_rootx() + ui.winfo_width(),
                    ui.winfo_rooty() + ui.winfo_height(),
                )
            ).save(screenshot)
        buttons["restart"].invoke()
        buttons["restart"].invoke()
        assert len(operations) == 1
        assert str(buttons["restart"].cget("state")) == "disabled"
        assert "Выполняется" in label.cget("text")
        ui.busy_services.clear()
        detail.update(running=True, health="ready", error="", pid=123, uptime=8)
        ui._update_service_views(refresh=False)
        ui.update()
        assert "Подключён" in label.cget("text")
        assert str(buttons["stop"].cget("state")) == "normal"
        assert ui.page_bodies["bot_status"].winfo_reqwidth() <= ui.winfo_width()
        ui.apply_theme("Emerald Cyber")
        ui.update()
        assert not errors
    finally:
        ui.runtime.close()
        ui.integration.stop_all()
        indicator_controller.stop()
        logging.getLogger().removeHandler(ui._log_handler)
        ui.destroy()


def test_real_child_kernel_status_and_safe_reconnect(tmp_path):
    from configuration import ConfigManager
    from kernel_client import KernelClient, RemoteRuntime

    cfg = ConfigManager(tmp_path / "config.json")
    client = KernelClient(cfg.filepath).ensure_started(safe=True)
    remote = None
    try:
        remote = RemoteRuntime(client, cfg.snapshot)
        service = remote.integrations
        assert service.details("krest_bot.py")["installed"]
        assert service.exists("krest_bot.py")
        assert service.perform("krest_bot.py", "stop") == "Не запущен"
        with pytest.raises(RuntimeError, match="Безопасный режим"):
            service.perform("krest_bot.py", "restart")
        assert "Безопасный режим" in service.details("krest_bot.py")["error"]
        assert not service.details("krest_bot.py")["running"]
        # Clearing a failed attempt updates both local presentation and the kernel snapshot.
        service.perform("krest_bot.py", "stop")
        assert not service.details("krest_bot.py")["error"]
    finally:
        if remote:
            remote.detach()
        client.call("shutdown")


def test_proxy_error_clears_only_on_fresh_confirmed_recovery():
    from types import SimpleNamespace
    from kernel_client import ServiceProxy

    name = "krest_bot.py"
    ready = {"running": True, "health": "ready", "error": ""}
    remote = SimpleNamespace(cached={"modules": {}, "services": {name: ready}}, settings=lambda: {})
    proxy = ServiceProxy(remote)
    def fail(filename):
        raise RuntimeError("restart failed")
    proxy.restart = fail
    with pytest.raises(RuntimeError):
        proxy.perform(name, "restart")
    assert proxy.details(name)["health"] == "error"  # old snapshot must not erase failure
    remote.cached = {"modules": {}, "services": {name: {**ready, "health": "unknown"}}}
    assert proxy.details(name)["error"] == "restart failed"
    remote.cached = {"modules": {}, "services": {name: {**ready, "error": "server failure"}}}
    assert proxy.details(name)["health"] == "error"
    remote.cached = {"modules": {}, "services": {name: ready}}
    assert proxy.details(name)["health"] == "ready"
    assert not proxy.details(name)["error"]
