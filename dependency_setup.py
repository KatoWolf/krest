"""Standard-library setup backend. Installs only the project's fixed manifests."""

import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent
PROFILES = {
    "all": ("requirements-voice.txt", "requirements-windows.txt"),
    "music": ("requirements-media.txt",),
}
MODULES = {
    "pillow": "PIL",
    "discord-py": "discord",
    "pytelegrambotapi": "telebot",
    "pyautogui": "pyautogui",
    "pyrogram": "pyrogram",
}


def requirements(profile):
    from pip._vendor.packaging.requirements import Requirement

    seen = set()

    def read(name):
        if name in seen:
            return []
        seen.add(name)
        rows = []
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r "):
                rows.extend(read(line[3:].strip()))
            else:
                req = Requirement(line)
                if req.marker is None or req.marker.evaluate():
                    rows.append(req)
        return rows

    return [req for path in PROFILES[profile] for req in read(path)]


def probe(profile):
    missing, broken = [], []
    for req in requirements(profile):
        key = req.name.lower().replace(".", "-").replace("_", "-")
        module = MODULES.get(key, req.name.lower().replace("-", "_"))
        try:
            version = importlib.metadata.version(req.name)
            if version not in req.specifier:
                missing.append(str(req))
            elif importlib.util.find_spec(module) is None:
                broken.append(str(req))
        except importlib.metadata.PackageNotFoundError:
            missing.append(str(req))
    if os.name == "nt" and not any("winsdk" in v for v in missing + broken):
        try:
            from winsdk.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager,
            )

            assert GlobalSystemMediaTransportControlsSessionManager
        except (ImportError, OSError, RuntimeError):
            broken.append("winsdk>=1.0.0b10")
    return {"missing": missing, "broken": broken}


def console_python(path):
    path = Path(path)
    return path.with_name("python.exe") if path.name.lower() == "pythonw.exe" else path


def choose_environment(root=ROOT):
    suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    candidates = [Path(root) / name / suffix for name in (".venv", ".krest-runtime")]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            result = subprocess.run(
                [
                    str(path),
                    "-c",
                    "import sys,struct,tkinter; assert sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P')==8",
                ],
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return path
        except (OSError, subprocess.TimeoutExpired):
            pass
    for path in candidates:
        if not path.is_file():
            return path
    raise RuntimeError(
        "Обе локальные среды Python повреждены или несовместимы. Их файлы сохранены. Нужна проверка папок .venv и .krest-runtime."
    )


def inspect(executable, profile):
    result = subprocess.run(
        [str(console_python(executable)), str(ROOT / "dependency_setup.py"), "--probe", profile],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("Не удалось проверить компоненты. " + result.stderr[-700:])
    return json.loads(result.stdout)


def install_commands(executable, profile, report):
    python = str(console_python(executable))
    common = [
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--progress-bar",
        "off",
        "--timeout",
        "30",
        "--retries",
        "2",
    ]
    commands = []
    if profile == "all" and any(
        v.startswith("torch") for v in report["missing"] + report["broken"]
    ):
        commands.append(
            common
            + (
                ["--force-reinstall"]
                if any(v.startswith("torch") for v in report["broken"])
                else []
            )
            + ["--index-url", "https://download.pytorch.org/whl/cpu", "torch>=2.2,<3"]
        )
    broken = [v for v in report["broken"] if profile != "all" or not v.startswith("torch")]
    if broken:
        commands.append(
            common
            + [
                "--index-url",
                "https://pypi.org/simple",
                "--force-reinstall",
                "--no-deps",
                *broken,
            ]
        )
    if report["missing"] or report["broken"]:
        commands.append(
            common
            + [
                "--index-url",
                "https://pypi.org/simple",
                *[v for name in PROFILES[profile] for v in ("-r", str(ROOT / name))],
            ]
        )
    return commands


def run_command(command, emit, cancel):
    if cancel.is_set():
        raise RuntimeError("Установка прервана")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    with subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as proc:
        tail = []

        def read():
            for line in proc.stdout:
                tail.append(line.rstrip())
                del tail[:-30]
                emit(line.rstrip())

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        while True:
            if cancel.is_set():
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise RuntimeError("Установка прервана. Её можно повторить этой же кнопкой.")
            try:
                code = proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=2)
        if code:
            raise RuntimeError(
                "Не удалось установить компоненты. Проверьте подключение к интернету и повторите.\n"
                + "\n".join(tail[-8:])
            )


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--probe" or sys.argv[2] not in PROFILES:
        raise SystemExit(2)
    print(json.dumps(probe(sys.argv[2]), ensure_ascii=True))
