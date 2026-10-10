"""Windows per-user autostart; applied only from the settings page."""

from pathlib import Path
import os
import subprocess
import sys

VALUE_NAME = "KrestAssistant"
KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command(executable=sys.executable):
    from app_paths import compiled

    if compiled():
        return subprocess.list2cmdline([sys.executable])
    executable = Path(executable)
    windowless = executable.with_name("pythonw.exe")
    if windowless.exists():
        executable = windowless
    return subprocess.list2cmdline(
        [str(executable), str(Path(__file__).resolve().parent / "main.py")]
    )


def set_enabled(enabled):
    if os.name != "nt":
        if enabled:
            raise RuntimeError("Автозапуск поддерживается только в Windows")
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
