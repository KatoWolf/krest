from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app_runtime import AssistantRuntime
from app_state import AssistantState
from command_context import CommandContext
from configuration import ConfigManager
from downloads import DownloadSorter
from notification_center import NotificationCenter, should_speak
from scenarios import ScenarioRunner
from sort_history import SortHistory


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import voice

    monkeypatch.setattr(voice, "speak", lambda *a, **kw: None)
    cfg = ConfigManager(tmp_path / "config.json")
    app = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    yield app
    app.close()


def test_context_extends_and_cancels_only_recent_timer(runtime):
    first = runtime.state.add_timer(60, "Чай")
    second = runtime.state.add_timer(120, "Перерыв")
    assert runtime.execute("добавь ещё пять").success
    timers = runtime.state.active_timers
    assert timers[0]["end_time"] == first["end_time"]
    assert timers[1]["end_time"] == second["end_time"] + 300
    assert runtime.execute("отмени его").success
    assert [v["id"] for v in timers] == [first["id"]]


def test_context_ambiguous_expired_and_unrelated_commands(runtime):
    runtime.state.add_timer(60, "Чай")
    runtime.state.add_timer(120, "Перерыв")
    runtime.state.last_target = (runtime.state.active_timers[-1]["id"], time.monotonic() - 121)
    result = runtime.execute("отмени его")
    assert not result.success and "Уточните" in result.text
    assert len(runtime.state.active_timers) == 2
    assert runtime.execute("1").success
    assert runtime.state.active_timers[0]["label"] == "Перерыв"
    runtime.state.add_timer(60, "Чай")
    runtime.state.last_target = None
    runtime.execute("отмени его")
    runtime.execute("время")
    assert not runtime.execute("1").success
    assert len(runtime.state.active_timers) == 2


def test_context_does_not_capture_new_reminders_or_claim_missing_success(runtime):
    assert not runtime.execute("отмени его").success
    assert runtime.execute("напомни через пять минут выпить воды").success
    assert runtime.state.active_timers[0]["label"] == "выпить воды"
    assert runtime.context.handle("напоминай по будням в 09:00 зарядка") is None


def test_weekday_schedule_snooze_and_completion_survive_restart(tmp_path):
    path = tmp_path / "state.json"
    state = AssistantState(path)
    task = state.add_alarm(
        "09:00", "Зарядка", now=datetime(2026, 10, 9, 10), daily=True, weekdays=[0, 2, 4]
    )
    assert datetime.fromtimestamp(task["end_time"]) == datetime(2026, 10, 12, 9)
    state.take_due(datetime(2026, 10, 16, 10).timestamp())
    assert datetime.fromtimestamp(state.daily_tasks[0]["end_time"]) == datetime(2026, 10, 19, 9)
    context = CommandContext(state)
    assert context.handle("отложи на пять минут").success
    snoozed = state.active_timers[0]
    assert snoozed["task_id"] == task["id"]
    state.take_due(snoozed["end_time"])
    assert context.handle("выполнено").success
    restored = AssistantState(path)
    assert restored.completed_tasks[0]["task_id"] == task["id"]
    assert not restored.reminders()
    assert len(restored.daily_tasks) == 1


def test_weekday_voice_command_and_empty_selection(runtime):
    assert runtime.execute("напоминай по выходным в 09:00 зарядка").success
    assert runtime.state.daily_tasks[0]["weekdays"] == [5, 6]
    with pytest.raises(ValueError, match="день недели"):
        runtime.state.add_alarm("09:00", daily=True, weekdays=[])


def test_corrupt_completed_history_is_skipped(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"completed_tasks": [{"id": "a", "end_time": time.time()}]}))
    state = AssistantState(path)
    assert not state.completed_tasks and state.load_error


def test_daily_briefing_excludes_other_days_and_counts_completions(runtime):
    from datetime import timedelta

    today = datetime.now()
    runtime.state.daily_tasks = [
        {"id": "a", "label": "Сегодня", "end_time": today.timestamp()},
        {"id": "b", "label": "Завтра", "end_time": (today + timedelta(days=1)).timestamp()},
    ]
    runtime.state.completed_tasks = [
        {
            "id": "c",
            "label": "Готово",
            "end_time": today.timestamp(),
            "completed_at": today.timestamp(),
        }
    ]
    result = runtime.execute("план на сегодня")
    assert result.success and "Сегодня" in result.text and "Завтра" not in result.text
    assert "Выполнено сегодня: 1" in result.text


def test_scenario_stops_after_error_and_preserves_explicit_order():
    steps = [{"action": "command", "value": v} for v in ("first", "fails", "never")]
    rt = SimpleNamespace(
        settings=lambda: {"scenarios": {"Тест": steps}},
        stop_event=threading.Event(),
        is_scenario_command=lambda value: False,
        execute=Mock(
            side_effect=[
                SimpleNamespace(success=True),
                SimpleNamespace(success=False, text="Ошибка"),
            ]
        ),
        publish_notice=Mock(),
    )
    runner = ScenarioRunner(rt)
    runner.start("тест")
    runner.thread.join(2)
    assert not runner.thread.is_alive()
    assert [c.args[0] for c in rt.execute.call_args_list] == ["first", "fails"]
    assert "Ошибка" in runner.status
    rt.publish_notice.assert_called_once()


def test_scenario_wait_is_cancellable_and_does_not_execute_next():
    entered = threading.Event()
    rt = SimpleNamespace(
        settings=lambda: {
            "scenarios": {
                "Тест": [
                    {"action": "profile", "value": "game"},
                    {"action": "wait", "value": "60"},
                    {"action": "command", "value": "never"},
                ]
            }
        },
        stop_event=threading.Event(),
        performance=SimpleNamespace(set_mode=lambda mode: entered.set()),
        execute=Mock(),
        publish_notice=Mock(),
    )
    runner = ScenarioRunner(rt)
    runner.start("Тест")
    assert entered.wait(1)
    runner.cancel()
    runner.thread.join(1)
    assert not runner.thread.is_alive()
    rt.execute.assert_not_called()
    assert "остановлен" in runner.status


def test_scenario_recursion_including_wake_word_is_blocked(runtime):
    assert runtime.is_scenario_command("крест запусти сценарий Игра")
    cfg = runtime.settings()
    cfg["command_aliases"] = {"го": "запусти сценарий Игра"}
    runtime.settings = lambda: cfg
    assert runtime.is_scenario_command("крест го")
    runtime.scenarios._run("Тест", [{"action": "command", "value": "крест го"}])
    assert "не могут" in runtime.scenarios.status


def test_deleted_scenario_stays_deleted(tmp_path):
    path = tmp_path / "config.json"
    cfg = ConfigManager(path)
    draft = cfg.snapshot()
    draft["scenarios"] = {}
    cfg.save_config(draft)
    assert ConfigManager(path).snapshot()["scenarios"] == {}


def ready_file(tmp_path, name="photo.jpg", content=b"original"):
    file = tmp_path / name
    file.write_bytes(content)
    old = time.time() - 1000
    os.utime(file, (old, old))
    return file


def test_sort_preview_has_no_side_effects(tmp_path):
    file = ready_file(tmp_path)
    result = DownloadSorter().scan(tmp_path, automatic=False, preview=True)
    assert result["preview"][0]["source"] == str(file)
    assert list(tmp_path.iterdir()) == [file]


def test_sort_undo_restores_content_and_auto_does_not_resort(tmp_path):
    file = ready_file(tmp_path)
    sorter = DownloadSorter()
    assert sorter.scan(tmp_path, automatic=False)["moved_count"] == 1
    journal = SortHistory(tmp_path)
    target = Path(journal.list()[0]["target"])
    assert not file.exists() and target.exists()
    assert journal.undo_last() == {"restored": 1, "errors": []}
    assert file.read_bytes() == b"original" and not target.exists()
    assert not sorter.scan(tmp_path, now=time.time() + 1000)["moved_count"]
    assert not sorter.scan(tmp_path, now=time.time() + 1200)["moved_count"]
    assert journal.list()[0]["undone"] == 1


@pytest.mark.parametrize("reason", ["changed", "occupied"])
def test_sort_undo_skips_modified_files_and_occupied_names(tmp_path, reason):
    source = ready_file(tmp_path)
    DownloadSorter().scan(tmp_path, automatic=False)
    journal = SortHistory(tmp_path)
    target = Path(journal.list()[0]["target"])
    if reason == "changed":
        info = target.stat()
        target.write_bytes(b"modified")  # Same length and mtime must still fail the hash check.
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    else:
        source.write_bytes(b"new file")
    result = journal.undo_last()
    assert result["restored"] == 0 and result["errors"]
    assert target.read_bytes() == (b"modified" if reason == "changed" else b"original")
    if reason == "occupied":
        assert source.read_bytes() == b"new file"


def test_failed_sort_does_not_leave_a_phantom_undo_batch(tmp_path, monkeypatch):
    source = ready_file(tmp_path)
    unlink = Path.unlink

    def fail_original(path, *args, **kwargs):
        if path == source:
            raise PermissionError("locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_original)
    result = DownloadSorter().scan(tmp_path, automatic=False)
    assert result["errors"] and not result["moved_count"]
    assert source.read_bytes() == b"original"
    assert SortHistory(tmp_path).list() == []


def test_inbox_groups_persists_and_marks_read(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    center = NotificationCenter(path)
    ident, fresh = center.add("Событие", "Steam", "high", now=100)
    assert fresh
    assert center.add("Событие", "Steam", "high", now=120) == (ident, False)
    restored = NotificationCenter(path)
    assert restored.list()[0]["count"] == 2
    restored.mark_read(ident)
    assert not restored.list(unread=True)
    assert restored.add("Событие", "Steam", "high", now=121)[1]
    assert restored.add("Событие", "Steam", "high", now=200)[1]


@pytest.mark.parametrize(
    "priority,mode,game,expected",
    [
        ("normal", "all", False, True),
        ("normal", "all", True, False),
        ("high", "all", True, True),
        ("high", "silent", False, False),
        ("low", "important", False, False),
    ],
)
def test_notification_voice_policy(priority, mode, game, expected):
    assert should_speak(priority, mode, game) is expected


def test_runtime_notices_are_saved_when_muted_and_repeats_are_not_spoken(runtime, monkeypatch):
    import voice

    speaker = Mock()
    monkeypatch.setattr(voice, "speak", speaker)
    runtime.notification_mode = "silent"
    runtime.publish_notice("Время вышло", "Задачи", "high")
    speaker.assert_not_called()
    runtime.notification_mode = "all"
    runtime.publish_notice("Новость", "Steam")
    runtime.publish_notice("Новость", "Steam")
    speaker.assert_called_once_with("Новость")
    assert len(runtime.center.list()) == 2


def test_performance_auto_detects_game_and_samples_at_bounded_rate(tmp_path, monkeypatch):
    import performance

    clock = [100.0]
    monkeypatch.setattr(performance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        performance.psutil,
        "process_iter",
        Mock(return_value=[SimpleNamespace(info={"name": "CS2.exe"})]),
    )
    process = Mock(pid=1)
    process.children.return_value = []
    process.cpu_percent.return_value = 80
    process.memory_info.return_value = SimpleNamespace(rss=100 * 1024**2)
    monkeypatch.setattr(performance.psutil, "Process", lambda: process)
    monkeypatch.setattr(performance.psutil, "cpu_count", lambda: 4)
    policy = performance.PerformancePolicy(lambda: {}, tmp_path / "activity_profile.json")
    policy.tick()
    assert policy.active == "game" and policy.defer_files
    assert policy.metrics == {"cpu": 20, "memory_mb": 100, "processes": 1}
    assert performance.shared_multiplier(tmp_path) == 3
    clock[0] += 1
    policy.tick()
    process.cpu_percent.assert_called_once()
    policy.set_mode("balanced")
    assert not policy.defer_files and policy.poll_multiplier == 1
    assert performance.shared_multiplier(tmp_path) == 1


def test_stale_shared_profile_does_not_throttle_bot(tmp_path):
    from performance import shared_multiplier

    (tmp_path / "activity_profile.json").write_text(
        json.dumps({"mode": "game", "updated": time.time() - 61})
    )
    assert shared_multiplier(tmp_path) == 1
