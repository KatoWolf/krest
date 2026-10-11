"""Release gates: reject stale/unverified bytes and incomplete skill catalogs."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def builder():
    path = Path(__file__).parents[1] / "packaging" / "build_windows.py"
    spec = importlib.util.spec_from_file_location("integrity_builder", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dist(tmp_path):
    folder = tmp_path / "main.dist"
    folder.mkdir()
    (folder / "Krest.exe").write_bytes(b"compiled executable")
    for name in ("libvosk.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"):
        (folder / name).write_bytes(b"native")
    return folder


def test_installer_requires_successful_verification(builder, dist, monkeypatch):
    receipt = dist.parent / "verified-distribution.json"
    with pytest.raises(RuntimeError, match="stage verify"):
        builder.require_verified_distribution(dist, receipt)
    monkeypatch.setattr(builder, "smoke_test", lambda dist: None)
    builder.verify_distribution(dist)
    builder.require_verified_distribution(dist, receipt)
    (dist / "libvosk.dll").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="изменилась"):
        builder.require_verified_distribution(dist, receipt)


@pytest.mark.parametrize("change", ["add", "remove"])
def test_manifest_checks_added_and_removed_resources(builder, dist, monkeypatch, change):
    monkeypatch.setattr(builder, "smoke_test", lambda dist: None)
    builder.verify_distribution(dist)
    if change == "add":
        (dist / "extra.dll").write_bytes(b"extra")
    else:
        (dist / "libvosk.dll").unlink()
    with pytest.raises(RuntimeError):
        builder.require_verified_distribution(dist, dist.parent / "verified-distribution.json")


def test_failed_reverification_invalidates_previous_receipt(builder, dist, monkeypatch):
    monkeypatch.setattr(builder, "smoke_test", lambda dist: None)
    builder.verify_distribution(dist)

    def fail(dist):
        raise RuntimeError("runtime failed")

    monkeypatch.setattr(builder, "smoke_test", fail)
    with pytest.raises(RuntimeError, match="runtime failed"):
        builder.verify_distribution(dist)
    assert not (dist.parent / "verified-distribution.json").exists()


def test_smoke_cannot_modify_verified_distribution(builder, dist, monkeypatch):
    monkeypatch.setattr(builder, "smoke_test", lambda dist: (dist / "unexpected").touch())
    with pytest.raises(RuntimeError, match="во время проверки"):
        builder.verify_distribution(dist)
    assert not (dist.parent / "verified-distribution.json").exists()


def test_exact_skills_not_minimum_count(builder):
    source = {"errors": {}, "builtin_skill_ids": ["skills.basic.time", "skills.windows.shot"]}
    builder.validate_skill_reports(source, source)
    for actual in ([], ["skills.basic.time"], ["skills.basic.time", "skills.windows.other"]):
        with pytest.raises(RuntimeError, match="Каталог"):
            builder.validate_skill_reports(source, {"errors": {}, "builtin_skill_ids": actual})
    with pytest.raises(RuntimeError):
        builder.validate_skill_reports(source, {"errors": {}, "skills": 999})


def test_failed_command_keeps_diagnostic_log(builder, tmp_path):
    log = tmp_path / "checks" / "kernel.log"
    with pytest.raises(subprocess.CalledProcessError):
        builder.run_checked(
            [sys.executable, "-c", "print('runtime evidence'); raise SystemExit(3)"], log_path=log
        )
    assert "runtime evidence" in log.read_text(encoding="utf-8")


def test_check_reports_builtin_identities(tmp_path):
    report = tmp_path / "skills.json"
    result = subprocess.run(
        [
            sys.executable,
            "main.py",
            "--check",
            "--check-output",
            str(report),
            "--config",
            str(tmp_path / "config.json"),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    assert not data["errors"]
    assert len(data["builtin_skill_ids"]) == data["skills"]
    assert "skills.windows.take_screenshot" in data["builtin_skill_ids"]


def test_kernel_shutdown_timeout_is_a_failed_release_check(tmp_path, monkeypatch):
    import kernel_client
    import process_lock
    import release_smoke

    class Lock:
        acquisitions = 0

        def __init__(self, path):
            pass

        def acquire(self):
            self.acquisitions += 1
            return self.acquisitions == 1

        def close(self):
            pass

    class Client:
        key = None

        def __init__(self, path):
            pass

        def ensure_started(self, safe):
            return self

        def call(self, method, **kwargs):
            if method == "ping":
                return {"safe_mode": True}
            if method == "command.parse":
                return {"skill": "skills.windows.take_screenshot"}
            if method == "submit":
                self.key = kwargs["request_key"]
            if method == "jobs":
                return [{"request_key": self.key, "status": "completed"}]

    ticks = iter(range(100))
    monkeypatch.setattr(process_lock, "ProcessLock", Lock)
    monkeypatch.setattr(kernel_client, "KernelClient", Client)
    monkeypatch.setattr(release_smoke.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(release_smoke.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="не завершилось"):
        release_smoke.check_kernel(tmp_path / "config.json")
