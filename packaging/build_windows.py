"""Compile application sources, smoke-test the distribution, then make an installer."""

from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.0"


def stop_process_tree(process):
    """A timed-out compiler must not leave SCons/MSVC children holding logs open."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # Still attempt to stop the owned parent below.
    if process.poll() is None:
        process.kill()
    process.wait(timeout=30)


def run_checked(command, **kwargs):
    """Keep live build output and a bounded error tail in public CI annotations."""
    from collections import deque
    import threading

    log_path = kwargs.pop("log_path", None)
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    timeout = kwargs.pop("timeout", None)
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    kwargs.pop("check", None)
    tail = deque(maxlen=35)

    def record(status, **extra):
        if log_path:
            Path(str(log_path) + ".status.json").write_text(
                json.dumps(
                    {
                        "status": status,
                        "timeout_seconds": timeout,
                        "started_at": started_at,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        **extra,
                    }
                ),
                encoding="utf-8",
            )

    record("starting")
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
            from contextlib import nullcontext

            with open(log_path, "w", encoding="utf-8") if log_path else nullcontext() as log:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    tail.append(line)
                    if log:
                        log.write(line)
                        log.flush()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            record("running", pid=process.pid)
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            record("timeout", pid=process.pid)
            stop_process_tree(process)
            raise
        finally:
            reader.join(timeout=3)
        record("succeeded" if code == 0 else "failed", returncode=code)
        if code:
            detail = "".join(line for line in tail if not line.startswith("Nuitka-Options:"))[
                -3000:
            ]
            print(
                "::error::" + detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
                flush=True,
            )
            raise subprocess.CalledProcessError(code, command)


def build_preflight(output, command):
    """Persist evidence before a compiler can hang; do not collect environment secrets."""
    import importlib.metadata
    import platform

    (output / "build-environment.json").write_text(
        json.dumps(
            {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "python": sys.version,
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
                "runner_image": os.getenv("ImageVersion"),
                "command": command,
                "packages": sorted(
                    (d.metadata.get("Name", ""), d.version)
                    for d in importlib.metadata.distributions()
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    run_checked(
        [
            sys.executable,
            "-c",
            "import json,torch; print(json.dumps({'torch':torch.__version__,"
            "'cuda':torch.version.cuda,'hip':getattr(torch.version,'hip',None)}),flush=True); "
            "assert torch.version.cuda is None and getattr(torch.version,'hip',None) is None, "
            "'Install the CPU PyTorch wheel before packaging'; "
            "assert hasattr(torch.package,'PackageImporter'); assert hasattr(torch.jit,'load')",
        ],
        cwd=ROOT,
        timeout=120,
        log_path=output / "torch-preflight.log",
    )


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
        "--show-memory",
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


def distribution_manifest(dist):
    """Bind successful runtime verification to every byte shipped by Inno Setup."""
    if not (dist / "Krest.exe").is_file():
        raise RuntimeError("Не найдена standalone-сборка Krest.exe")
    manifest = {}
    for path in sorted(dist.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                manifest[path.relative_to(dist).as_posix()] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return manifest


def require_verified_distribution(dist, receipt):
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Перед установщиком выполните --stage verify") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != VERSION
        or data.get("files") != distribution_manifest(dist)
        or data.get("checks") != ["skills", "kernel", "models"]
    ):
        raise RuntimeError("Сборка изменилась после проверки; повторите --stage verify")


def validate_skill_reports(source, packaged):
    expected = source.get("builtin_skill_ids")
    actual = packaged.get("builtin_skill_ids")
    if (
        source.get("errors")
        or packaged.get("errors")
        or not expected
        or not actual
        or sorted(expected) != sorted(actual)
    ):
        raise RuntimeError("Каталог встроенных навыков сборки не совпадает с исходниками")


def smoke_test(dist):
    executable = dist / "Krest.exe"
    reports = dist.parent / "release-checks"
    reports.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="krest-release-test-") as temp:
        root = Path(temp)
        cfg = root / "config.json"
        source_report = reports / "source-skills.json"
        packaged_report = reports / "packaged-skills.json"
        # Separate empty profiles: no custom skills or local user configuration.
        for label, command, report in (
            ("source-skills", [sys.executable, str(ROOT / "main.py")], source_report),
            ("packaged-skills", [str(executable)], packaged_report),
        ):
            report.unlink(missing_ok=True)
            run_checked(
                command
                + [
                    "--check",
                    "--safe-mode",
                    "--check-output",
                    str(report),
                    "--config",
                    str(root / (label + ".json")),
                ],
                timeout=180,
                cwd=root,
                log_path=reports / (label + ".log"),
            )
        validate_skill_reports(
            json.loads(source_report.read_text(encoding="utf-8")),
            json.loads(packaged_report.read_text(encoding="utf-8")),
        )
        for label, option, timeout in (
            ("kernel", "--release-smoke", 90),
            ("models", "--release-model-check", 900),
        ):
            run_checked(
                [str(executable), option, "--config", str(cfg)],
                timeout=timeout,
                cwd=root,
                log_path=reports / (label + ".log"),
            )


def verify_distribution(dist):
    receipt = dist.parent / "verified-distribution.json"
    receipt.unlink(missing_ok=True)
    # Do not attach previous successful checks to a failed new verification.
    reports = dist.parent / "release-checks"
    if reports.exists():
        shutil.rmtree(reports)
    native = {p.name.lower() for p in dist.rglob("*.dll")}
    required = {"libvosk.dll", "libgcc_s_seh-1.dll", "libstdc++-6.dll", "libwinpthread-1.dll"}
    if required - native:
        raise RuntimeError(
            "В сборку не попали библиотеки Vosk: " + ", ".join(sorted(required - native))
        )
    audit_distribution(dist)
    before = distribution_manifest(dist)
    smoke_test(dist)
    if before != distribution_manifest(dist):
        raise RuntimeError("Файлы сборки изменились во время проверки")
    receipt.write_text(
        json.dumps(
            {
                "version": VERSION,
                "checks": ["skills", "kernel", "models"],
                "files": before,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--skip-installer", action="store_true")
    parser.add_argument("--compile-timeout-minutes", type=int, default=180)
    parser.add_argument("--stage", choices=("all", "compile", "verify", "installer"), default="all")
    args = parser.parse_args()
    if not 1 <= args.compile_timeout_minutes <= 300:
        parser.error("--compile-timeout-minutes must be between 1 and 300")
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
    dist = output / "main.dist"
    if args.stage in ("all", "compile"):
        (output / "verified-distribution.json").unlink(missing_ok=True)
        build_preflight(output, command)
        run_checked(
            command,
            cwd=ROOT,
            log_path=output / "compile.log",
            timeout=args.compile_timeout_minutes * 60,
        )
        print("::notice::Compilation finished.", flush=True)
    if args.stage in ("all", "verify"):
        verify_distribution(dist)
        print(
            "::notice::Packaged kernel and speech models passed their runtime checks.", flush=True
        )
    if args.stage in ("all", "installer") and not args.skip_installer:
        require_verified_distribution(dist, output / "verified-distribution.json")
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
            log_path=output / "installer.log",
        )
    print("Этап " + args.stage + " завершён: " + str(output))


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
