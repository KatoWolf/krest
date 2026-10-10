from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from app_state import AssistantState
from app_runtime import AssistantRuntime, check_wake_word
from configuration import ConfigManager
from skill_manager import SkillManager


@pytest.fixture
def runtime(tmp_path):
    cfg = ConfigManager(tmp_path / "config.json")
    app = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    yield app
    app.close()


def test_imports_are_passive(tmp_path):
    code = """import threading,sys
from skill_manager import SkillManager
before=set(threading.enumerate())
manager=SkillManager()
assert not manager.errors,manager.errors
assert len(manager.catalog())>=90
assert set(threading.enumerate())==before
assert 'voice_engine' not in sys.modules
from skills import gid
assert not gid.service_status()['running']
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "text,func",
    [
        ("запусти секундомер", "start_stopwatch"),
        ("закрой ассистента", "shutdown"),
        ("закрой рабочий стол", "close_virtual_desktop"),
        ("таймер на пять минут", "set_timer"),
        ("запусти таймер на 2 минуты", "set_timer"),
        ("сколько будет 2 километра в метры", "convert_currency_or_units"),
        ("поставь будильник на восемь тридцать", "set_alarm"),
        ("переведи 2 километра в метры", "convert_currency_or_units"),
        ("помощь", "show_help"),
        ("громкость на максимум", "set_volume_max"),
    ],
)
def test_specific_command_wins(runtime, text, func):
    assert runtime.manager.match(text)[0].func.__name__ == func


@pytest.mark.parametrize(
    "text,seconds",
    [
        ("на 2 часа 15 минут", 8100),
        ("на семь минут", 420),
        ("на полчаса", 1800),
        ("на полтора часа", 5400),
        ("на 1 минуту", 60),
        ("на одну минуту тридцать секунд", 90),
        ("на двадцать пять минут", 1500),
        ("на -5 минут", 0),
        ("на 0 секунд", 0),
    ],
)
def test_timer_parsing(text, seconds):
    from skills.timers_alarms import parse_timer_duration

    assert parse_timer_duration(text)[0] == seconds


@pytest.mark.parametrize(
    "text,expected",
    [
        ("8:30", "08:30"),
        ("25:00", None),
        ("12:90", None),
        ("восемь тридцать", "08:30"),
        ("двадцать три пятьдесят девять", "23:59"),
        ("семь вечера", "19:00"),
        ("двенадцать десять", "12:10"),
        ("двадцать три", "23:00"),
        ("12:00 ночи", "00:00"),
    ],
)
def test_alarm_parsing(text, expected):
    from skills.timers_alarms import parse_alarm_time

    assert parse_alarm_time(text) == expected


def test_timer_lifecycle_persistence(runtime, tmp_path):
    result = runtime.execute("таймер на две минуты")
    assert result.success, result.text
    restored = AssistantState(tmp_path / "assistant_state.json")
    assert len(restored.active_timers) == 1
    due = restored.take_due(time.time() + 125)
    assert len(due) == 1
    assert not restored.take_due(time.time() + 125)
    assert not AssistantState(tmp_path / "assistant_state.json").active_timers


def test_alarm_next_day_and_missed_tick(tmp_path):
    state = AssistantState(tmp_path / "state.json")
    item = state.add_alarm("08:30", now=datetime(2026, 10, 10, 9, 0))
    assert datetime.fromtimestamp(item["end_time"]) == datetime(2026, 10, 11, 8, 30)
    assert len(state.take_due(item["end_time"] + 95)) == 1
    assert not state.take_due(item["end_time"] + 96)


def test_timer_survives_status_query(runtime):
    runtime.execute("таймер на одну секунду")
    item = runtime.state.active_timers[0]
    item["end_time"] = time.time() - 5
    runtime.execute("статус таймера")
    assert len(runtime.state.take_due()) == 1


def test_sleep_can_wake_by_voice(runtime):
    assert runtime.execute("усни").success
    assert runtime.state.is_sleeping
    result = runtime.execute("крест время", source="voice")
    assert not result.success
    assert runtime.execute("крест проснись", source="voice").success
    assert not runtime.state.is_sleeping


def test_wake_followup(runtime):
    assert not runtime.execute("время", source="voice").handled
    assert runtime.execute("крест", source="voice").success
    assert runtime.execute("время", source="voice").success
    assert check_wake_word("крест, дата", "крест") == (True, "дата")
    assert not check_wake_word("окрестности", "крест")[0]


def test_reload_keeps_unique_registry(runtime):
    before = {e.id for e in runtime.manager.catalog()}
    runtime.manager.reload()
    assert not runtime.manager.errors
    after = runtime.manager.catalog()
    assert len(after) == len({e.id for e in after})
    assert before == {e.id for e in after}


def test_disabled_module(runtime):
    runtime.manager.settings = lambda: {"skills": {"timers_alarms": False}}
    result = runtime.execute("таймер на пять минут")
    assert not result.success and "Выключен" in result.text
    assert not runtime.state.active_timers


def test_nonwindows_command_is_diagnostic(runtime):
    if os.name == "nt":
        pytest.skip("Only meaningful on a non-Windows host")
    result = runtime.execute("заблокируй пк")
    assert not result.success and "Windows" in result.text


def test_math_and_units(runtime):
    assert "96" in runtime.execute("посчитай 12 * 8").text
    assert "2000" in runtime.execute("переведи 2 километра в метры").text
    assert "это 1 килограмм" in runtime.execute("переведи 1 килограмм в килограмм").text
    from skills.utils import safe_eval_math

    assert safe_eval_math("9 ** 99999999") == ""
    assert safe_eval_math('__import__("os")') == ""


def test_currency_has_no_invented_fallback(monkeypatch, tmp_path):
    from skills import utils

    monkeypatch.setattr(utils, "RATES_CACHE_FILE", str(tmp_path / "rates.json"))

    def fail(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(utils.urllib.request, "urlopen", fail)
    with pytest.raises(RuntimeError, match="курс валют"):
        utils.update_rates_cache_online()


def test_gsi_server_validation_and_restart():
    from skills import gid

    gid.stop_service()
    try:
        port = gid.start_service({"gsi_port": 0, "auto_mode": False})
        assert gid.start_service() == port
        url = f"http://127.0.0.1:{port}/"
        payload = json.dumps(
            {
                "map": {
                    "clock_time": 120,
                    "game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                }
            }
        ).encode()
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=3
        ) as response:
            assert response.status == 200
        assert gid.dota_state.match_time_seconds == 120
        assert not gid.dota_state.is_active
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(url, data=b"[]"), timeout=3)
        assert exc.value.code == 400
    finally:
        gid.stop_service()
    assert not gid.service_status()["running"]


def test_temp_cleaner_keeps_recent_and_symlink(tmp_path):
    from skills.system_mon import clean_directory_files

    old = tmp_path / "old"
    old.write_text("old")
    os.utime(old, (time.time() - 172800,) * 2)
    recent = tmp_path / "recent"
    recent.write_text("recent")
    link = tmp_path / "link"
    try:
        link.symlink_to(recent)
    except OSError:
        pass
    assert clean_directory_files(tmp_path) == 3
    assert not old.exists() and recent.read_text() == "recent"


def test_corrupt_config_is_backed_up_on_explicit_save(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken")
    config = ConfigManager(path)
    assert config.load_error
    assert path.read_text() == "{broken"
    config.save_config()
    assert list(tmp_path.glob("config.json.broken-*"))[0].read_text() == "{broken"


def test_terminal_import_does_not_connect_or_open_desktop(tmp_path):
    code = """import sys
import krest_bot
assert krest_bot.bot.client is None
assert len(krest_bot.bot.handlers)==2
assert 'pyautogui' not in sys.modules
assert krest_bot.TOKEN==''
"""
    env = dict(os.environ, KREST_CONFIG_PATH=str(tmp_path / "missing.json"), KREST_BOT_TOKEN="")
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_command_error_not_reported_as_success(runtime, monkeypatch):
    from skill_manager import skill, SKILL_REGISTRY

    @skill(keywords=["тест ошибки"])
    def broken():
        raise RuntimeError("example failure")

    try:
        result = runtime.execute("тест ошибки")
        assert result.handled and not result.success and "example failure" in result.text
    finally:
        SKILL_REGISTRY[:] = [entry for entry in SKILL_REGISTRY if entry.func is not broken]


def test_scheduler_emits_alarm_without_voice(runtime):
    runtime.state.add_timer(0.02, "проверка")
    runtime.start()
    deadline = time.monotonic() + 3
    found = False
    while time.monotonic() < deadline:
        kind, value = runtime.events.get(timeout=3)
        if kind == "alarm":
            found = True
            assert value["label"] == "проверка"
            break
    assert found
    assert not runtime.state.active_timers


def test_recycle_bin_does_not_select_temp_cleanup(runtime):
    assert runtime.manager.match("очисти корзину")[0].func.__name__ == "empty_recycle_bin"


def test_weather_failure_has_error_status(runtime, monkeypatch):
    from skills import webo

    monkeypatch.setattr(webo, "fetch_weather_info", lambda city: {"success": False})
    assert not runtime.execute("какая погода").success


def test_structured_system_result_is_spoken(runtime, monkeypatch):
    from skills import system_mon

    monkeypatch.setattr(system_mon.psutil, "sensors_battery", lambda: None)
    result = runtime.execute("заряд батареи")
    assert result.success, result.text
    assert "Аккумулятор" in result.text
    events = []
    while not runtime.events.empty():
        events.append(runtime.events.get_nowait())
    assert any(kind == "speech" and "Аккумулятор" in str(value) for kind, value in events)
