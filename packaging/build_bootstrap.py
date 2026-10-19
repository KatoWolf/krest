"""Repair the private build environment and run the same release path as CI."""

from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time

from build_support import BuildSession
from build_windows import run_checked

ROOT = Path(__file__).resolve().parents[1]


def environment_valid(python):
    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import sys, struct, tkinter, pip; "
                "assert sys.version_info[:2] in ((3,11),(3,12)); "
                "assert struct.calcsize('P') == 8; assert sys.prefix != sys.base_prefix",
            ],
            capture_output=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prepare_environment(root, base_python, env):
    folder = Path(root) / ".build-venv"
    python = folder / "Scripts" / "python.exe"
    if environment_valid(python):
        return python
    previous = None
    if folder.exists():
        previous = folder.with_name(".build-venv-broken-" + str(time.time_ns()))
        try:
            folder.rename(previous)
        except OSError as exc:
            raise RuntimeError(
                "Окружение сборщика повреждено и занято другим процессом. "
                "Закройте другие сборщики и повторите запуск."
            ) from exc
        print("Старое окружение сохранено:", previous)
    print("Создаю отдельное окружение сборки…", flush=True)
    run_checked([str(base_python), "-m", "venv", str(folder)], env=env, timeout=180)
    if not environment_valid(python):
        raise RuntimeError("Python не смог создать рабочее окружение с pip и tkinter.")
    return python


def dependency_signature(root, python):
    """Only skip pip when every requirements file and the interpreter are unchanged."""
    digest = hashlib.sha256()
    for path in sorted(Path(root).glob("requirements*.txt")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    metadata = subprocess.check_output(
        [str(python), "-c", "import sys; print(sys.version); print(sys.base_prefix)"],
        timeout=20,
    )
    digest.update(metadata)
    return digest.hexdigest()


def install_dependencies(root, python, env):
    stamp = Path(root) / ".build-venv" / "krest-dependencies.json"
    signature = dependency_signature(root, python)
    try:
        saved = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    # Metadata avoids importing Torch on every incremental build. pip check also
    # detects manually removed/broken requirements despite an unchanged stamp.
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m, json; "
            "print(json.dumps({d.metadata['Name'].lower().replace('_','-'): d.version "
            "for d in m.distributions()}))",
        ],
        capture_output=True,
        timeout=20,
        env=env,
    )
    installed = json.loads(probe.stdout) if probe.returncode == 0 else {}
    cpu_torch = "+cpu" in installed.get("torch", "")
    healthy = (
        cpu_torch
        and subprocess.run(
            [str(python), "-m", "pip", "check"], capture_output=True, timeout=60, env=env
        ).returncode
        == 0
    )
    if saved.get("signature") == signature and saved.get("installed") == installed and healthy:
        print("Зависимости сборщика уже готовы; повторная загрузка не нужна.", flush=True)
        return
    # Invalidate before attempting an upgrade: an interrupted install is never cached.
    stamp.unlink(missing_ok=True)
    print("Подготавливаю библиотеки сборщика и CPU-версию голоса…", flush=True)
    pip = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if "torch" in installed and not cpu_torch:
        # A CUDA wheel also satisfies torch>=2.2; pip would otherwise keep it.
        run_checked([str(python), "-m", "pip", "uninstall", "-y", "torch"], env=env)
    run_checked(
        [*pip, "torch>=2.2,<3", "--index-url", "https://download.pytorch.org/whl/cpu"],
        cwd=root,
        env=env,
    )
    run_checked(
        [
            *pip,
            "-r",
            "requirements-windows.txt",
            "-r",
            "requirements-voice.txt",
            "-r",
            "requirements-build.txt",
        ],
        cwd=root,
        env=env,
    )
    run_checked([str(python), "-m", "pip", "check"], env=env, timeout=60)
    installed = json.loads(subprocess.check_output(probe.args, timeout=20, env=env))
    if "+cpu" not in installed.get("torch", ""):
        raise RuntimeError("Не удалось установить CPU-версию PyTorch для сборки.")
    stamp.write_text(json.dumps({"signature": signature, "installed": installed}), encoding="utf-8")


def main():
    # Renaming a broken cache must not move the interpreter running this script.
    # find_python may legitimately have selected an otherwise working build venv.
    base = Path(getattr(sys, "_base_executable", sys.executable))
    if sys.prefix != sys.base_prefix:
        if Path(sys.prefix).resolve() == (ROOT / ".build-venv").resolve():
            raise RuntimeError(
                "Запустите build-exe.bat: он освободит окружение сборки для ремонта."
            )
        return subprocess.call([str(base), str(Path(__file__).resolve()), *sys.argv[1:]])
    if os.name != "nt" or sys.version_info[:2] not in ((3, 11), (3, 12)):
        raise RuntimeError("Для сборки нужен Windows и Python 3.11/3.12 x64.")
    with BuildSession(ROOT) as session:
        env = session.environment()
        python = prepare_environment(ROOT, base, env)
        install_dependencies(ROOT, python, env)
        run_checked(
            [str(python), str(ROOT / "packaging" / "build_windows.py"), *sys.argv[1:]],
            cwd=ROOT,
            env=env,
        )
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print("Сборка не завершена:", exc, flush=True)
        raise SystemExit(1)
