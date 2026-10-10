"""Compile application sources, smoke-test the distribution, then make an installer."""

from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.0"


def run_checked(command, **kwargs):
    """Keep live build output and a bounded error tail in public CI annotations."""
    from collections import deque
    import threading

    timeout = kwargs.pop("timeout", None)
    kwargs.pop("check", None)
    tail = deque(maxlen=35)
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    ) as process:

        def read():
            for line in process.stdout:
                print(line, end="", flush=True)
                tail.append(line)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            raise
        finally:
            reader.join(timeout=3)
        if code:
            detail = "".join(line for line in tail if not line.startswith("Nuitka-Options:"))[
                -3000:
            ]
            print(
                "::error::" + detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
                flush=True,
            )
            raise subprocess.CalledProcessError(code, command)


def compiler_command(output):
    # Only explicit public resources enter the release; no configs, sessions or signing keys.
    modules = [
        p.stem for p in ROOT.glob("*.py") if p.stem not in {"main", "setup_app", "dependency_setup"}
    ]
    return [
        sys.executable,
        "-m",
        "nuitka",
        "--mode=standalone",
        "--assume-yes-for-downloads",
        "--msvc=latest",
        "--jobs=2",
        "--windows-console-mode=attach",
        "--enable-plugin=tk-inter",
        "--module-parameter=torch-disable-jit=no",
        "--include-package=skills",
        "--include-package=vosk",
        "--include-package=winsdk",
        "--include-package=pywinauto",
        "--include-package=pycaw",
        "--include-package=comtypes",
        "--include-package=telebot",
        "--include-package=discord",
        "--include-package=pyrogram",
        "--include-module=pystray._win32",
        "--include-module=pyautogui._pyautogui_win",
        *["--include-module=" + name for name in sorted(modules)],
        "--include-data-dir=" + str(ROOT / "assets") + "=assets",
        "--include-data-files=" + str(ROOT / "license_public_key.pem") + "=license_public_key.pem",
        "--include-data-files=" + str(ROOT / "packaging" / "NOTICE.txt") + "=NOTICE.txt",
        "--windows-icon-from-ico=" + str(ROOT / "assets" / "krest.ico"),
        "--company-name=Krest",
        "--product-name=Крест",
        "--file-version=" + VERSION,
        "--product-version=" + VERSION,
        "--output-filename=Krest.exe",
        "--output-dir=" + str(output),
        "--report=" + str(output / "compilation-report.xml"),
        str(ROOT / "main.py"),
    ]


def audit_distribution(dist):
    forbidden_names = {
        "config.json",
        "kernel_endpoint.json",
        "assistant_state.json",
        "favorites.json",
        "tg_auth_temp.json",
    }
    own_sources = {p.name for p in ROOT.glob("*.py")} | {
        p.name for p in (ROOT / "skills").glob("*.py")
    }
    for path in dist.rglob("*"):
        if not path.is_file():
            continue
        if path.name in forbidden_names or path.name.endswith(
            (".private.pem", ".session", ".sqlite3")
        ):
            raise RuntimeError("Личные данные в сборке: " + str(path))
        # Third-party libraries may need their own source/data at runtime; our modules must be compiled.
        if (
            path.suffix == ".py"
            and (path.parent == dist or path.parent == dist / "skills")
            and path.name in own_sources
        ):
            raise RuntimeError("Исходник Крест в сборке: " + str(path))


def smoke_test(dist):
    executable = dist / "Krest.exe"
    with tempfile.TemporaryDirectory(prefix="krest-release-test-") as temp:
        root = Path(temp)
        cfg = root / "config.json"
        report = root / "skills.json"
        run_checked(
            [str(executable), "--check", "--check-output", str(report), "--config", str(cfg)],
            check=True,
            timeout=180,
            cwd=root,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        if data["errors"] or data["skills"] < 150:
            raise RuntimeError("Встроенные навыки не загрузились: " + str(data))
        # Exercise the real child-process launcher with the installed executable, not python main.py.
        run_checked(
            [str(executable), "--release-smoke", "--config", str(cfg)],
            check=True,
            timeout=90,
            cwd=root,
        )
        run_checked(
            [str(executable), "--release-model-check", "--config", str(cfg)],
            check=True,
            timeout=900,
            cwd=root,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--skip-installer", action="store_true")
    args = parser.parse_args()
    output = ROOT / "build" / "windows"
    command = compiler_command(output)
    if args.plan:
        print(subprocess.list2cmdline(command))
        return
    if os.name != "nt" or sys.version_info[:2] not in ((3, 11), (3, 12)):
        raise SystemExit("Сборка требует 64-битную Windows и Python 3.11 или 3.12")
    import struct

    if struct.calcsize("P") != 8:
        raise SystemExit("Требуется 64-битный Python")
    output.mkdir(parents=True, exist_ok=True)
    run_checked(command, cwd=ROOT, check=True)
    print(
        "::notice::Compilation finished; auditing native libraries and packaged files.", flush=True
    )
    dist = output / "main.dist"
    native = {p.name.lower() for p in dist.rglob("*.dll")}
    required = {"libvosk.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"}
    if required - native:
        raise RuntimeError(
            "В сборку не попали библиотеки Vosk: " + ", ".join(sorted(required - native))
        )
    audit_distribution(dist)
    print(
        "::notice::Distribution audit passed; checking the packaged kernel and speech models.",
        flush=True,
    )
    smoke_test(dist)
    print("::notice::Packaged kernel and speech models passed their runtime checks.", flush=True)
    if not args.skip_installer:
        iscc = shutil.which("ISCC.exe") or str(
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Inno Setup 6"
            / "ISCC.exe"
        )
        run_checked(
            [
                iscc,
                "/DAppVersion=" + VERSION,
                "/DSourceDir=" + str(dist),
                "/O" + str(ROOT / "dist"),
                str(ROOT / "packaging" / "krest.iss"),
            ],
            check=True,
        )
    print("Сборка и проверка завершены: " + str(output))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        print(
            "::error::"
            + message[-2500:].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
            flush=True,
        )
        raise
