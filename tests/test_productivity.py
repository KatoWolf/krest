from datetime import datetime
import json
import os
import threading
import time

import pytest

from app_state import AssistantState
from app_runtime import AssistantRuntime
from command_book import CommandBook
from configuration import ConfigManager
from downloads import DownloadSorter


def test_reminder_survives_restart_and_snooze(tmp_path):
    path = tmp_path / "state.json"
    state = AssistantState(path)
    timer = state.add_timer(5, "Проверить духовку")
    state.take_due(timer["end_time"] + 1)
    restored = AssistantState(path)
    assert restored.reminders()[0]["label"] == "Проверить духовку"
    assert not restored.take_due(timer["end_time"] + 2)
    snoozed = restored.snooze(timer["id"])
    restored = AssistantState(path)
    assert not restored.reminders()
    assert restored.active_timers[0]["id"] == snoozed["id"]
    restored.take_due(snoozed["end_time"])
    restored.acknowledge(snoozed["id"])
    assert not AssistantState(path).reminders()


def test_daily_task_skips_missed_days_and_remains_scheduled(tmp_path):
    path = tmp_path / "state.json"
    state = AssistantState(path)
    task = state.add_alarm("09:00", "Зарядка", now=datetime(2026, 10, 1, 8), daily=True)
    now = datetime(2026, 10, 5, 12).timestamp()
    assert len(state.take_due(now)) == 1
    assert not state.take_due(now + 1)
    restored = AssistantState(path)
    assert restored.daily_tasks[0]["end_time"] == datetime(2026, 10, 6, 9).timestamp()
    restored.acknowledge(restored.reminders()[0]["id"])
    assert restored.daily_tasks
    assert restored.cancel("daily_tasks", task["id"]) == 1
    assert not AssistantState(path).daily_tasks


def test_failed_save_does_not_lose_due_reminder(tmp_path, monkeypatch):
    import app_state

    state = AssistantState(tmp_path / "state.json")
    item = state.add_timer(1, "Не потерять")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(app_state, "atomic_write_json", fail)
    with pytest.raises(OSError):
        state.take_due(item["end_time"])
    assert state.active_timers == [item]
    assert not state.reminders()


def test_invalid_state_recovers_valid_records_and_backs_up(tmp_path):
    path = tmp_path / "state.json"
    good = {"id": "good", "end_time": time.time() + 60, "label": "ok"}
    data = {
        "active_timers": [None, {"id": []}, good, good, {"id": "huge", "end_time": 1e100}],
        "active_alarms": None,
    }
    path.write_text(json.dumps(data))
    original = path.read_text()
    state = AssistantState(path)
    assert state.active_timers == [good]
    assert state.load_error
    state.persist()
    assert next(tmp_path.glob("*.broken-*")).read_text() == original


def test_favorites_are_persistent_history_is_session_only(tmp_path):
    path = tmp_path / "favorites.json"
    book = CommandBook(path)
    book.add("время")
    book.add("время")
    book.remember("дата")
    book.remember("таймер на 5 минут")
    assert book.navigate(-1, "черновик") == "таймер на 5 минут"
    assert book.navigate(-1, "") == "дата"
    assert book.navigate(1, "") == "таймер на 5 минут"
    assert book.navigate(1, "") == "черновик"
    restored = CommandBook(path)
    assert restored.favorites == ["время"] and not restored.history
    restored.remove("время")
    assert CommandBook(path).favorites == []


def test_queue_cancel_keeps_current_command_and_rejects_after_close(tmp_path):
    runtime = AssistantRuntime(lambda: {}, data_dir=tmp_path)
    entered, release = threading.Event(), threading.Event()
    executed = []

    def execute(text, source):
        executed.append(text)
        entered.set()
        release.wait(5)

    runtime.execute = execute
    try:
        runtime.submit("first")
        runtime.start()
        assert entered.wait(2)
        runtime.submit("second")
        runtime.submit("third")
        assert runtime.command_status() == ("first", 2)
        assert runtime.clear_pending() == 2
        release.set()
        runtime.requests.join()
        assert executed == ["first"]
    finally:
        release.set()
        runtime.close()
    assert not runtime.submit("after close")


def test_named_daily_commands_aliases_and_suggestions(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    values = config.snapshot()
    values["command_aliases"] = {"Перерыв": "напомни через пять минут выпить воды"}
    config.save_config(values)
    runtime = AssistantRuntime(config.snapshot, data_dir=tmp_path)
    try:
        assert runtime.execute("крест перерыв", source="voice").success
        assert runtime.state.active_timers[0]["label"] == "выпить воды"
        assert runtime.execute("напомни через 1 час 5 минут: проверить духовку").success
        assert runtime.state.active_timers[-1]["total_seconds"] == 3900
        assert runtime.execute("напоминай каждый день в 09:00 сделать зарядку").success
        assert len(runtime.state.daily_tasks) == 1
        assert runtime.execute(
            "крест напоминай каждый день в девять утра сделать зарядку", source="voice"
        ).success
        assert runtime.state.daily_tasks[-1]["time_str"] == "09:00"
        assert runtime.execute("пожалуйста, напомни через полчаса выпить воды").success
        assert runtime.state.active_timers[-1]["total_seconds"] == 1800
        result = runtime.execute("напоминай каждый день в 29:00 ошибка")
        assert not result.success
        assert "время" in runtime.manager.suggest("времмя")
        assert not runtime.execute("времмя").success
    finally:
        runtime.close()


def old_file(path, contents="content"):
    path.write_text(contents)
    os.utime(path, (time.time() - 600,) * 2)
    return path


def test_automatic_sort_waits_for_stability_and_never_overwrites(tmp_path):
    from skills.system_mon import get_category_for_extension

    source = old_file(tmp_path / "report.txt", "new")
    folder = tmp_path / get_category_for_extension(".txt")
    folder.mkdir()
    existing = old_file(folder / "report.txt", "existing")
    incomplete = old_file(tmp_path / "video.crdownload")
    sorter = DownloadSorter()
    now = time.time()
    assert sorter.scan(tmp_path, now=now)["moved_count"] == 0
    assert source.exists()
    assert sorter.scan(tmp_path, now=now + 119)["moved_count"] == 0
    result = sorter.scan(tmp_path, now=now + 121)
    assert result["moved_count"] == 1 and not result["errors"]
    assert not source.exists() and incomplete.exists()
    assert existing.read_text() == "existing"
    assert (folder / "report (1).txt").read_text() == "new"


def test_sort_restarts_stability_after_file_changes(tmp_path):
    source = old_file(tmp_path / "report.txt")
    sorter = DownloadSorter()
    now = time.time()
    sorter.scan(tmp_path, now=now)
    source.write_text("updated download")
    assert sorter.scan(tmp_path, now=now + 121)["moved_count"] == 0
    assert source.exists()
    assert sorter.scan(tmp_path, now=now + 242)["moved_count"] == 1


def test_sort_does_not_follow_category_symlink(tmp_path):
    from skills.system_mon import get_category_for_extension

    root = tmp_path / "downloads"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    source = old_file(root / "report.txt")
    try:
        (root / get_category_for_extension(".txt")).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    result = DownloadSorter().scan(root, automatic=False)
    assert result["errors"] and not result["moved_count"]
    assert source.exists() and not list(outside.iterdir())


def test_sort_preserves_source_when_copy_fails(tmp_path, monkeypatch):
    import downloads

    source = old_file(tmp_path / "report.txt")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(downloads.shutil, "copyfileobj", fail)
    result = DownloadSorter().scan(tmp_path, automatic=False)
    assert result["errors"] and source.read_text() == "content"
    assert list(tmp_path.rglob("*.txt")) == [source]


def test_sort_cancellation_keeps_original(tmp_path, monkeypatch):
    import downloads

    source = old_file(tmp_path / "report.txt")
    stop = threading.Event()
    copy = downloads.shutil.copyfileobj

    def cancel_then_copy(reader, writer, **kwargs):
        stop.set()
        return copy(reader, writer, **kwargs)

    monkeypatch.setattr(downloads.shutil, "copyfileobj", cancel_then_copy)
    result = DownloadSorter().scan(tmp_path, automatic=False, stop=stop)
    assert result["errors"] and source.read_text() == "content"
    assert list(tmp_path.rglob("*.txt")) == [source]


def test_background_sort_uses_saved_folder(tmp_path, monkeypatch):
    import downloads

    called = threading.Event()
    folders = []

    def scan(self, folder, **kwargs):
        folders.append(folder)
        called.set()
        return {"moved_count": 0, "skipped_count": 0, "errors": []}

    monkeypatch.setattr(downloads.DownloadSorter, "scan", scan)
    cfg = ConfigManager(tmp_path / "config.json")
    candidate = cfg.snapshot()
    candidate["app_settings"].update(downloads_auto_sort=True, downloads_path=str(tmp_path))
    cfg.save_config(candidate)
    runtime = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    try:
        runtime.start()
        assert called.wait(2)
        assert folders == [str(tmp_path)]
    finally:
        runtime.close()
