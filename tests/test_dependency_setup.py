import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

import dependency_setup as setup


def test_install_uses_exact_interpreter_fixed_manifests_and_cpu_torch():
    python = Path("a path") / "python.exe"
    commands = setup.install_commands(python, "all", {"missing": ["torch>=2.2,<3"], "broken": []})
    assert len(commands) == 2
    assert commands[0][0] == str(python)
    assert "https://download.pytorch.org/whl/cpu" in commands[0]
    assert commands[1][-4:] == [
        "-r",
        str(setup.ROOT / "requirements-voice.txt"),
        "-r",
        str(setup.ROOT / "requirements-windows.txt"),
    ]
    assert setup.install_commands(python, "all", {"missing": [], "broken": []}) == []
    repair = setup.install_commands(python, "all", {"missing": [], "broken": ["torch>=2.2,<3"]})
    assert "--force-reinstall" in repair[0]
    assert len(repair) == 2  # Never replace the repaired CPU wheel from another index.


def test_music_setup_does_not_install_voice_or_bots():
    commands = setup.install_commands(
        sys.executable, "music", {"missing": ["winsdk"], "broken": []}
    )
    assert len(commands) == 1
    assert commands[0][-2:] == ["-r", str(setup.ROOT / "requirements-media.txt")]


def test_invalid_profile_is_rejected_and_manifests_parse():
    with pytest.raises(KeyError):
        setup.requirements("../untrusted.txt")
    assert any(req.name == "torch" for req in setup.requirements("all"))
    if os.name != "nt":
        assert setup.requirements("music") == []


def test_cancel_does_not_start_a_process(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(RuntimeError, match="прервана"):
        setup.run_command(["anything"], lambda line: None, cancel)


def test_install_process_output_and_failure_are_reported():
    lines = []
    with pytest.raises(RuntimeError, match="connection-test"):
        setup.run_command(
            [sys.executable, "-c", "print('connection-test'); raise SystemExit(1)"],
            lines.append,
            threading.Event(),
        )
    assert lines == ["connection-test"]


def test_broken_environment_is_preserved_and_another_path_is_chosen(tmp_path, monkeypatch):
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    old = tmp_path / ".venv" / suffix
    old.parent.mkdir(parents=True)
    old.write_text("old interpreter fixture")
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert setup.choose_environment(tmp_path) == tmp_path / ".krest-runtime" / suffix
    assert old.read_text() == "old interpreter fixture"
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    assert setup.choose_environment(tmp_path) == old


def test_probe_runs_as_target_python_and_reports_broken_environment(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"missing":[],"broken":[]}', stderr="")

    monkeypatch.setattr(setup.subprocess, "run", run)
    assert setup.inspect(Path("other-env") / "python.exe", "music")["missing"] == []
    assert calls[0][0][0] == str(Path("other-env") / "python.exe")
    assert calls[0][0][-2:] == ["--probe", "music"]
    assert "shell" not in calls[0][1]


def test_windows_media_import_uses_the_actual_winsdk_namespace(monkeypatch):
    import media_control

    expected = object()

    def imported(name):
        assert name == "winsdk.windows.media.control"
        return SimpleNamespace(GlobalSystemMediaTransportControlsSessionManager=expected)

    monkeypatch.setattr(media_control.importlib, "import_module", imported)
    assert media_control._manager_type() is expected


@pytest.mark.skipif(os.name != "nt", reason="Native Windows media bridge")
def test_installed_windows_media_module_loads():
    from media_control import _manager_type

    assert callable(_manager_type().request_async)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell parser")
def test_powershell_bootstrap_has_valid_syntax():
    path = str(setup.ROOT / "start.ps1").replace("'", "''")
    script = (
        "$tokens=$null; $errors=$null; [System.Management.Automation.Language.Parser]::ParseFile('"
        + path
        + "', [ref]$tokens, [ref]$errors) | Out-Null; if ($errors.Count) { $errors | Out-String | Write-Output; exit 1 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
