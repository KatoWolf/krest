import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from configuration import ConfigManager
from kernel_client import KernelClient, RemoteRuntime
from platform_store import JobStore, MemoryStore


def eventually(call, predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = call()
            if predicate(value):
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    pytest.fail("Condition not reached before deadline")


def test_jobs_recover_without_repeating_and_deduplicate(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    ident, fresh = store.create("timer", "text", "same-request")
    assert fresh
    assert store.create("timer", "text", "same-request") == (ident, False)
    assert store.transition(ident, "running")
    recovered = JobStore(store.path)
    assert recovered.get(ident)["status"] == "interrupted"
    assert not recovered.transition(ident, "running")
    assert recovered.get(ident)["attempts"] == 1


def test_memory_persists_and_rejects_oversized_event(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.remember("Рабочий плеер", "Яндекс Музыка")
    assert MemoryStore(store.path).recall("рабочий плеер") == "Яндекс Музыка"
    with pytest.raises(ValueError):
        store.event("x", "я" * 16001)
    assert store.events() == []


def test_kernel_survives_desktop_disconnect_and_single_instance(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    cfg = config.snapshot()
    cfg["profile"]["onboarding_completed"] = True
    cfg["owner_name"] = "Тест"
    config.save_config(cfg)
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, KREST_CONFIG_PATH=str(config.filepath))
    args = [sys.executable, str(root / "main.py"), "--kernel", "--config", str(config.filepath)]
    process = subprocess.Popen(
        args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    client = KernelClient(config.filepath)
    try:
        status = eventually(lambda: client.call("ping"), bool)
        second = subprocess.run(args, env=env, capture_output=True, timeout=10)
        assert second.returncode == 0, second.stderr
        assert client.call("ping")["pid"] == status["pid"]
        remote = RemoteRuntime(client, config.snapshot)
        remote.state.add_timer(120, "не зависит от окна")
        remote.detach()
        assert len(client.call("snapshot")["state"]["active_timers"]) == 1
        assert client.call("submit", text="таймер на 5 минут", request_key="same")
        assert client.call("submit", text="таймер на 5 минут", request_key="same")
        jobs = eventually(
            lambda: client.call("jobs"),
            lambda rows: bool(rows) and rows[0]["status"] in JobStore.TERMINAL,
        )
        assert len(jobs) == 1
        assert jobs[0]["status"] == "completed", jobs
        assert len(client.call("snapshot")["state"]["active_timers"]) == 2
        with pytest.raises(RuntimeError):
            client.call("__getattribute__", value="runtime")
        endpoint = json.loads((tmp_path / "kernel_endpoint.json").read_text())
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", endpoint["port"])
        conn.request("POST", "/rpc", "{}", {"Authorization": "Bearer incorrect"})
        assert conn.getresponse().status == 403
        conn.close()
    finally:
        try:
            client.call("shutdown")
        except Exception:
            process.terminate()
        output, error = process.communicate(timeout=15)
        assert process.returncode == 0, output + error


def test_missing_vault_secret_preserves_settings(tmp_path, monkeypatch):
    from secret_store import PREFIX

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"owner_name": "Анна", "api_keys": {"discord_token": PREFIX + "missing"}})
    )
    cfg = ConfigManager(path)
    assert cfg.snapshot()["owner_name"] == "Анна"
    assert cfg.snapshot()["api_keys"]["discord_token"] == ""
    assert cfg.secret_errors


def test_check_is_read_only_and_cli_command_uses_existing_kernel(tmp_path):
    config = ConfigManager(tmp_path / "config.json")
    cfg = config.snapshot()
    cfg["profile"]["onboarding_completed"] = True
    cfg["owner_name"] = "Тест"
    config.save_config(cfg)
    root = Path(__file__).resolve().parents[1]
    client = KernelClient(config.filepath).ensure_started()
    args = [sys.executable, str(root / "main.py"), "--config", str(config.filepath)]
    try:
        assert (
            Path(client.call("environment")["executable"]).resolve()
            == Path(sys.executable).resolve()
        )
        result = subprocess.run([*args, "--check"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        result = subprocess.run(
            [*args, "--command", "таймер на пять минут"], capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(client.call("snapshot")["state"]["active_timers"]) == 1
        assert client.call("jobs")[0]["status"] == "completed"
    finally:
        client.call("shutdown")
        eventually(lambda: (tmp_path / "kernel_endpoint.json").exists(), lambda exists: not exists)


def test_killed_kernel_recovers_timer_and_marks_scenario_interrupted(tmp_path):
    import psutil

    config = ConfigManager(tmp_path / "config.json")
    cfg = config.snapshot()
    cfg["owner_name"] = "Тест"
    cfg["profile"]["onboarding_completed"] = True
    cfg["scenarios"]["Длинный"] = [{"action": "wait", "value": "60"}]
    config.save_config(cfg)
    client = KernelClient(config.filepath).ensure_started()
    first = client.call("ping")["pid"]
    try:
        client.call("state.add_timer", args=[300, "сохранён"])
        client.call("scenario.start", name="Длинный")
        eventually(
            lambda: client.call("jobs"), lambda jobs: bool(jobs) and jobs[0]["status"] == "running"
        )
        process = psutil.Process(first)
        process.kill()
        process.wait(timeout=10)
        client.ensure_started()
        assert client.call("ping")["pid"] != first
        assert len(client.call("snapshot")["state"]["active_timers"]) == 1
        assert client.call("jobs")[0]["status"] == "interrupted"
        assert client.call("snapshot")["scenario"] == "Сценарии готовы"
    finally:
        client.call("shutdown")
