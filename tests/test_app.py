import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from commands import execute
from storage import atomic_write_json
from diagnostics import validate_module
from welcome_gui import ConfigManager, IntegrationManager


def test_commands():
    assert "Сейчас" in execute("крест время")
    assert "Сегодня" in execute("дата")
    assert "Python" in execute("статус")
    assert "Доступно" in execute("помощь")
    assert "Неизвестная" in execute("неизвестно")


def test_config_round_trip_preserves_unknown_fields(tmp_path):
    path = tmp_path / "config.json"
    atomic_write_json(
        path,
        {
            "owner_name": "Тест",
            "api_keys": {"krest_bot_token": "example"},
            "custom": 42,
        },
    )
    manager = ConfigManager(str(path))
    assert manager.config["api_keys"]["krest_bot_token"] == "example"
    assert "discord_token" in manager.config["api_keys"]
    manager.save_config()
    assert json.loads(path.read_text())["custom"] == 42


def test_atomic_write_with_concurrent_reader(tmp_path):
    path = tmp_path / "config.json"
    atomic_write_json(path, {"n": 0})

    def writer():
        for n in range(100):
            atomic_write_json(path, {"n": n})

    thread = threading.Thread(target=writer)
    thread.start()
    while thread.is_alive():
        assert isinstance(json.loads(path.read_text())["n"], int)
    thread.join()
    assert json.loads(path.read_text())["n"] == 99


def test_missing_and_invalid_credentials(monkeypatch):
    monkeypatch.delenv("KREST_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(ValueError, match="krest_bot_token"):
        validate_module("krest_bot.py", {})
    with pytest.raises(ValueError, match="целым"):
        validate_module(
            "krest_bot.py",
            {"api_keys": {"krest_bot_token": "example", "telegram_owner_id": "abc"}},
        )
    with pytest.raises(ValueError, match="discord_token"):
        validate_module("ds_session.py", {})


def test_startup_does_not_import_voice():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            'import main,sys; assert "voice_engine" not in sys.modules; assert "torch" not in sys.modules',
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_process_lifecycle(tmp_path, monkeypatch):
    import welcome_gui
    import diagnostics

    monkeypatch.setattr(welcome_gui, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics, "validate_module", lambda *args: None)
    (tmp_path / "service.py").write_text(
        'import time\nprint("ready", flush=True)\ntime.sleep(60)\n'
    )
    manager = IntegrationManager(
        SimpleNamespace(config_mgr=ConfigManager(tmp_path / "config.json")),
        base_dir=tmp_path,
    )
    try:
        assert manager.start("service.py") == "Запущен"
        pid = manager.processes["service.py"].pid
        assert manager.start("service.py") == "Уже запущен"
        assert manager.processes["service.py"].pid == pid
        assert manager.stop("service.py") == "Остановлен"
        assert manager._module_status("service.py") == "Не запущен"
        assert not manager.log_handles
    finally:
        manager.stop_all()


def test_failed_child_is_reported(tmp_path, monkeypatch):
    import welcome_gui
    import diagnostics

    monkeypatch.setattr(welcome_gui, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(diagnostics, "validate_module", lambda *args: None)
    (tmp_path / "bad.py").write_text('raise RuntimeError("broken")\n')
    manager = IntegrationManager(
        SimpleNamespace(config_mgr=ConfigManager(tmp_path / "config.json")),
        base_dir=tmp_path,
    )
    manager.start("bad.py")
    manager.processes["bad.py"].wait(timeout=5)
    manager._readers["bad.py"].join(timeout=2)
    manager.refresh()
    assert manager._module_status("bad.py") == "Ошибка запуска"
    assert "broken" in (tmp_path / "logs/bad.py.log").read_text()
    assert not manager.log_handles


def test_cross_process_notifications(tmp_path):
    from notifications import NotificationQueue
    import queue

    path = tmp_path / "notifications.sqlite3"
    consumer = NotificationQueue(path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            'from notifications import NotificationQueue; import sys; NotificationQueue(sys.argv[1]).put("Discord")',
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert consumer.get_nowait() == "Discord"
    with pytest.raises(queue.Empty):
        consumer.get_nowait()


def test_notifications_expire(tmp_path):
    from notifications import NotificationQueue
    import queue
    import sqlite3

    consumer = NotificationQueue(tmp_path / "notifications.sqlite3")
    with sqlite3.connect(consumer.path) as connection:
        connection.execute('INSERT INTO notifications(created,text) VALUES (0,"old")')
    with pytest.raises(queue.Empty):
        consumer.get_nowait()
