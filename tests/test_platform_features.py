from datetime import datetime, timedelta
import copy
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import zipfile

import pytest

from app_runtime import AssistantRuntime
from app_state import AssistantState
from configuration import ConfigManager
from platform_store import MemoryStore


def test_memory_commands_survive_restart_and_import_is_atomic(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    runtime = AssistantRuntime(config.snapshot, data_dir=tmp_path)
    try:
        assert runtime.execute("запомни любимый цвет: фиолетовый").success
        assert runtime.execute("что ты помнишь про любимый цвет").text == "фиолетовый"
        from assistant_memory import export_memory, import_memory

        path = tmp_path / "memory.json"
        export_memory(runtime.memory, path)
        assert runtime.execute("забудь заметку любимый цвет").success
        assert import_memory(runtime.memory, path) == 1
        assert runtime.memory.recall("любимый цвет") == "фиолетовый"
        data = json.loads(path.read_text())
        data["memory"].insert(0, {"key": "first", "value": "should not import", "category": "note"})
        data["memory"].append({"key": "", "value": "invalid", "category": "note"})
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            import_memory(runtime.memory, path)
        assert runtime.memory.recall("first") is None
    finally:
        runtime.close()


def test_repeated_commands_produce_proposal_without_execution():
    from assistant_memory import routine_suggestions

    jobs = [
        {
            "command": "время",
            "source": "text",
            "status": "completed",
            "created": datetime(2026, 10, day, 9).timestamp(),
        }
        for day in (1, 2, 3)
    ]
    assert routine_suggestions(jobs) == [{"command": "время", "days": 3, "time": "09:00"}]
    jobs[2]["created"] = jobs[1]["created"]
    assert routine_suggestions(jobs) == []


def test_move_one_daily_occurrence_keeps_regular_schedule(tmp_path):
    state = AssistantState(tmp_path / "state.json")
    task = state.add_alarm("09:00", "Зарядка", daily=True)
    shifted = (datetime.now() + timedelta(days=1)).replace(
        hour=11, minute=0, second=0, microsecond=0
    )
    state.move_occurrence(task["id"], shifted.timestamp())
    assert state.take_due(shifted.timestamp())[0]["label"] == "Зарядка"
    assert state.daily_tasks[0]["time_str"] == "09:00"
    assert datetime.fromtimestamp(state.daily_tasks[0]["end_time"]).hour == 9
    before = state.daily_tasks[0]["end_time"]
    state.skip_occurrence(task["id"])
    assert state.daily_tasks[0]["end_time"] == before + 86400
    assert AssistantState(state.filepath).daily_tasks == state.daily_tasks


@pytest.mark.parametrize(
    ("phrase", "canonical"),
    [
        ("поставь на пять минут таймер", "таймер на пять минут"),
        (
            "через пять минут напомни не выключать духовку",
            "напомни через пять минут: не выключать духовку",
        ),
        ("громкость музыки поставь на 30 процентов", "громкость на 30"),
    ],
)
def test_deterministic_slot_normalization(phrase, canonical):
    from speech_tools import normalize_command

    assert normalize_command(phrase) == canonical
    assert normalize_command("не " + phrase) == "не " + phrase


def test_event_rules_are_opt_in_debounced_and_traceable(tmp_path):
    from automation import EventAutomation

    called = []
    cfg = {
        "event_rules": [
            {
                "id": "wake-up",
                "event": "wake",
                "enabled": False,
                "scenario": "Работа",
                "cooldown": 60,
            }
        ]
    }
    runtime = SimpleNamespace(
        settings=lambda: cfg,
        memory=MemoryStore(tmp_path / "memory.db"),
        scenarios=SimpleNamespace(start=called.append),
    )
    engine = EventAutomation(runtime)
    engine.emit("wake", now=100)
    assert not called
    cfg["event_rules"][0]["enabled"] = True
    engine.emit("wake", now=100)
    engine.emit("wake", now=101)
    engine.emit("wake", now=160)
    assert called == ["Работа", "Работа"]
    assert [r["type"] for r in runtime.memory.events()].count("rule_started") == 2


def test_scenario_conditions_timeout_and_temporary_restore(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    cfg = config.snapshot()
    cfg["scenarios"]["Проверка"] = [
        {"action": "notifications", "value": "silent", "temporary": True},
        {
            "action": "command",
            "value": "таймер на пять минут",
            "condition": {"kind": "file", "value": str(tmp_path / "absent")},
        },
        {"action": "wait_process", "value": "nonexistent-krest-test-process", "timeout": 1},
    ]
    config.save_config(cfg)
    runtime = AssistantRuntime(config.snapshot, data_dir=tmp_path)
    try:
        runtime.scenarios.start("Проверка")
        runtime.scenarios.thread.join(4)
        assert not runtime.scenarios.thread.is_alive()
        assert "Истекло время ожидания" in runtime.scenarios.status
        assert runtime.scenarios.failed == ("Проверка", 2)
        assert runtime.notification_mode is None
        assert runtime.state.active_timers == []
        assert json.loads(runtime.memory.events()[-1]["payload"])["outcome"] == "failed"
    finally:
        runtime.close()


def test_file_rules_exclusions_duplicates_and_cross_process_lock(tmp_path):
    from downloads import DownloadSorter
    from file_tools import find_duplicates
    from process_lock import ProcessLock

    for name in ("test.pdf", "other.pdf", "keep.pdf"):
        path = tmp_path / name
        path.write_bytes(b"same document")
        os.utime(path, (time.time() - 200, time.time() - 200))
    policy = {"exclusions": ["keep.*"], "rules": [{"extensions": [".pdf"], "category": "Книги"}]}
    lock = ProcessLock(tmp_path / ".krest-sort.lock")
    assert lock.acquire()
    with pytest.raises(ValueError):
        DownloadSorter(policy).scan(tmp_path, automatic=False)
    lock.close()
    result = DownloadSorter(policy).scan(tmp_path, automatic=False)
    assert result["moved_count"] == 2
    assert (tmp_path / "keep.pdf").exists()
    assert (tmp_path / "Книги/test.pdf").exists()
    assert len(find_duplicates(tmp_path)["groups"][0]["paths"]) == 3
    assert find_duplicates(tmp_path, budget_bytes=1)["limited"]


def test_backups_strip_secrets_check_integrity_and_reject_traversal(tmp_path):
    from backup_service import create_backup, inspect_backup, restore_backup

    config = ConfigManager(tmp_path / "config.json")
    cfg = config.snapshot()
    cfg["api_keys"]["discord_token"] = "SENSITIVE"
    cfg["owner_name"] = "Анна"
    config.save_config(cfg)
    path = tmp_path / "backup.zip"
    create_backup(config, path)
    files = inspect_backup(path)
    assert b"SENSITIVE" not in files["config.json"]
    assert json.loads(files["config.json"])["owner_name"] == "Анна"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../outside.txt", "bad")
    with pytest.raises(ValueError):
        inspect_backup(bad)
    from process_lock import ProcessLock

    lock = ProcessLock(tmp_path / ".kernel.lock")
    assert lock.acquire()
    with pytest.raises(ValueError):
        restore_backup(path, config)
    lock.close()
    cfg["owner_name"] = "Новое имя"
    config.save_config(cfg)
    rollback = restore_backup(path, config)
    assert Path(rollback).exists()
    assert ConfigManager(config.filepath).snapshot()["owner_name"] == "Анна"


def test_remote_exact_allowlist_and_locked_session():
    from remote_policy import authorize

    cfg = {
        "remote_control": {
            "enabled": True,
            "allowed_commands": ["время"],
            "block_when_locked": True,
        }
    }
    assert authorize("Время", cfg, locked=False)[0]
    assert not authorize("время; cmd whoami", cfg, locked=False)[0]
    assert not authorize("время", cfg, locked=True)[0]
    assert not authorize("время", {}, locked=False)[0]


def test_plugin_manifest_rejects_unknown_version_and_permissions(tmp_path):
    from plugin_manifest import inspect_plugin

    script = tmp_path / "bot.py"
    script.write_text('print("ready")')
    assert inspect_plugin(script)["manifest"] is False
    script.with_suffix(".krest.json").write_text(
        json.dumps({"api_version": 2, "name": "x", "permissions": []})
    )
    with pytest.raises(ValueError):
        inspect_plugin(script)
    script.with_suffix(".krest.json").write_text(
        json.dumps({"api_version": 1, "name": "x", "permissions": ["shell"]})
    )
    with pytest.raises(ValueError):
        inspect_plugin(script)
    script.with_suffix(".krest.json").write_text(
        json.dumps({"api_version": 1, "name": "x", "permissions": ["notifications"]})
    )
    assert inspect_plugin(script)["permissions"] == ["notifications"]


def test_support_export_omits_private_data(tmp_path):
    from support_report import build_report

    cfg = ConfigManager(tmp_path / "config.json")
    runtime = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    try:
        runtime.memory.remember("secret", "private note")
        runtime.execute("запомни secret: private note")
        report = json.dumps(build_report(runtime, cfg))
        assert "private note" not in report and "запомни" not in report
    finally:
        runtime.close()


def test_source_update_fast_forwards_and_rolls_back_without_touching_user_data(tmp_path):
    import subprocess
    from source_update import SourceUpdater

    def git(root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.email", "test@example.invalid")
    git(upstream, "config", "user.name", "Test")
    (upstream / "source.py").write_text("version = 1")
    git(upstream, "add", "source.py")
    git(upstream, "commit", "-m", "initial")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(upstream), str(checkout)], check=True, capture_output=True)
    old = git(checkout, "rev-parse", "HEAD")
    (upstream / "source.py").write_text("version = 2")
    git(upstream, "commit", "-am", "update")
    data = tmp_path / "data"
    data.mkdir()
    cfg = ConfigManager(data / "config.json")
    cfg.save_config()
    updater = SourceUpdater(checkout, cfg)
    assert updater.check()["available"]
    updater.apply()
    assert (checkout / "source.py").read_text() == "version = 2"
    updater.rollback()
    assert git(checkout, "rev-parse", "HEAD") == old
    assert (checkout / "source.py").read_text() == "version = 1"
    assert cfg.filepath.exists()
    (checkout / "source.py").write_text("local changes")
    with pytest.raises(ValueError, match="локальные изменения"):
        updater.apply()


def test_memory_event_log_retains_recent_entries(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    with store.connect() as db:
        db.executemany(
            "INSERT INTO events(created,type,payload) VALUES (?,?,?)",
            [(i, "test", "{}") for i in range(2010)],
        )
    store.event("last", {})
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2000


def test_cancellation_of_running_cooperative_action_is_durable(tmp_path, monkeypatch):
    from skill_manager import CommandResult

    config = ConfigManager(tmp_path / "config.json")
    runtime = AssistantRuntime(config.snapshot, data_dir=tmp_path)
    started = threading.Event()

    def execute(*args):
        started.set()
        while not runtime.command_stop.is_set():
            time.sleep(0.01)
        return CommandResult(True, False, "Остановлено безопасно")

    monkeypatch.setattr(runtime, "_execute", execute)
    try:
        runtime.start()
        runtime.submit("test cancellation", request_key="cancel")
        assert started.wait(3)
        job = runtime.jobs.list()[0]
        assert "запрошена" in runtime.cancel_job(job["id"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and runtime.jobs.get(job["id"])["status"] == "running":
            time.sleep(0.01)
        assert runtime.jobs.get(job["id"])["status"] == "cancelled"
    finally:
        runtime.close()


def test_timeout_token_does_not_kill_a_thread():
    from execution import ExecutionToken

    token = ExecutionToken(threading.Event(), timeout=0)
    assert token.is_set()
    assert "время" in token.reason


def test_restore_rolls_back_already_replaced_files_after_io_failure(tmp_path, monkeypatch):
    import backup_service

    config = ConfigManager(tmp_path / "config.json")
    config.save_config(config.snapshot())
    state = tmp_path / "assistant_state.json"
    state.write_text('{"marker":"before backup"}')
    backup = tmp_path / "restore.zip"
    backup_service.create_backup(config, backup)
    current = config.snapshot()
    current["owner_name"] = "Текущее имя"
    config.save_config(current)
    state.write_text('{"marker":"current state"}')
    original = {p: p.read_bytes() for p in (config.filepath, state)}
    replace = backup_service.os.replace
    count = 0

    def fail_second(source, target):
        nonlocal count
        if str(source).endswith(".restoring"):
            count += 1
            if count == 2:
                raise OSError("simulated full disk")
        return replace(source, target)

    monkeypatch.setattr(backup_service.os, "replace", fail_second)
    with pytest.raises(OSError, match="simulated full disk"):
        backup_service.restore_backup(backup, config)
    assert count == 2
    assert {p: p.read_bytes() for p in original} == original
    assert not list(tmp_path.glob("*.restoring"))


def test_windows_output_enumeration_accepts_pycaw_enum(monkeypatch):
    from enum import Enum
    import sys
    import windows_audio

    class State(Enum):
        Active = 1
        Disabled = 2

    devices = [
        SimpleNamespace(id="{0.0.0.1}", FriendlyName="Headphones", state=State.Active),
        SimpleNamespace(id="{0.0.0.2}", FriendlyName="Speakers", state=1),
        SimpleNamespace(id="{0.0.1.1}", FriendlyName="Microphone", state=State.Active),
        SimpleNamespace(id="{0.0.0.3}", FriendlyName="Disabled", state=State.Disabled),
    ]
    monkeypatch.setitem(
        sys.modules,
        "pycaw.pycaw",
        SimpleNamespace(AudioUtilities=SimpleNamespace(GetAllDevices=lambda: devices)),
    )
    monkeypatch.setattr(windows_audio, "os", SimpleNamespace(name="nt"))
    assert [row["name"] for row in windows_audio.output_devices.__wrapped__()] == [
        "Headphones",
        "Speakers",
    ]


def test_power_actions_check_windows_result_without_forced_termination(monkeypatch):
    import subprocess
    from skills import windows

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 5
        assert "/f" not in args
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert "30 секунд" in windows.shutdown_windows()
    assert "30 секунд" in windows.restart_windows()
    assert "отменены" in windows.cancel_shutdown_windows()
    assert calls == [
        ["shutdown.exe", "/s", "/t", "30"],
        ["shutdown.exe", "/r", "/t", "30"],
        ["shutdown.exe", "/a"],
    ]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1, stderr="Access denied")
    )
    with pytest.raises(RuntimeError, match="не запланировала"):
        windows.shutdown_windows()


def test_telegram_owner_allowlist_and_actual_kernel_result(tmp_path, monkeypatch):
    import configuration
    import kernel_client
    import krest_bot

    config = ConfigManager(tmp_path / "config.json")
    cfg = config.snapshot()
    cfg["remote_control"].update(enabled=True, block_when_locked=False, allowed_commands=["время"])
    cfg["api_keys"]["telegram_owner_id"] = "123"
    config.save_config(cfg)
    monkeypatch.setattr(configuration, "ConfigManager", lambda: config)
    monkeypatch.setattr(krest_bot, "MY_ID", 123)
    replies = []
    monkeypatch.setattr(
        krest_bot.bot,
        "client",
        SimpleNamespace(reply_to=lambda message, text, **kwargs: replies.append(text)),
    )
    calls = []

    class Client:
        def __init__(self, path):
            assert path == config.filepath

        def call(self, method, **args):
            calls.append((method, args))
            return (
                True
                if method == "remote_submit"
                else {"status": "completed", "result": "Сейчас 15:42."}
            )

    monkeypatch.setattr(kernel_client, "KernelClient", Client)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=123, type="private"),
        from_user=SimpleNamespace(id=123),
        text="время",
        message_id=42,
    )
    krest_bot.terminal_handler(message)
    assert replies == ["Сейчас 15:42."]
    assert calls[0] == ("remote_submit", {"original": "время", "request_key": "telegram:123:42"})
    message.text = "выключи компьютер"
    krest_bot.terminal_handler(message)
    assert "разрешённом списке" in replies[-1] and len(calls) == 2
    message.text = "/start"
    krest_bot.terminal_handler(message)
    assert "время" in replies[-1]
    before = len(replies)
    message.from_user.id = 999
    krest_bot.terminal_handler(message)
    assert len(replies) == before
