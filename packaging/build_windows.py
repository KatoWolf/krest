"""One-click PyInstaller release with an optional, explicit PyArmor protection step."""

from pathlib import Path
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.17.1"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_support import BuildSession


def source_smoke():
    """Also expose setup/cleanup failures outside the checked child process."""
    try:
        with tempfile.TemporaryDirectory(prefix="krest-source-check-") as folder:
            run_checked(
                [
                    sys.executable,
                    "main.py",
                    "--release-smoke",
                    "--config",
                    str(Path(folder) / "config.json"),
                ],
                timeout=120,
            )
    except Exception:
        import traceback

        detail = traceback.format_exc()[-3000:]
        print(
            "::error::" + detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
            flush=True,
        )
        raise


def run_checked(command, **kwargs):
    """Stream progress and retain a bounded failure summary for CI."""
    from collections import deque
    import threading

    timeout = kwargs.pop("timeout", None)
    kwargs.pop("check", None)
    if os.name != "nt":
        kwargs.setdefault("start_new_session", True)
    tail = deque(maxlen=25)
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
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # Release checks start their own kernel. Do not leave that child
            # holding DLLs/model files after a cancelled or timed-out check.
            if os.name == "nt" and process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            elif os.name != "nt":
                import signal

                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=3)
            detail = "Command interrupted or timed out. Last output: " + "".join(tail)[-2500:]
            print(
                "::error::" + detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
                flush=True,
            )
            raise
        finally:
            reader.join(timeout=3)
        if code:
            detail = "".join(tail)[-2500:]
            print(
                "::error::" + detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
                flush=True,
            )
            raise subprocess.CalledProcessError(code, command)


def application_sources():
    return sorted(ROOT.glob("*.py")) + sorted((ROOT / "skills").glob("*.py"))


def package_command(output, *, protect=False, spec=None):
    spec = Path(spec or ROOT / "packaging" / "krest.spec")
    if protect:
        # SpecRepacker analyzes original imports, then replaces only our modules with
        # PyArmor output. Third-party Torch stays prebuilt; no C++ toolchain is involved.
        return [
            sys.executable,
            "-m",
            "pyarmor.cli",
            "gen",
            "--pack",
            str(spec),
            "-O",
            str(output),
            "-r",
            str(ROOT / "main.py"),
            *[str(p) for p in sorted(ROOT.glob("*.py")) if p != ROOT / "main.py"],
            str(ROOT / "skills"),
        ]
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(output),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        str(spec),
    ]


def preflight(output, stage):
    """Fail before expensive packaging when a recipient copy still holds files."""
    import psutil

    targets = [os.path.normcase(str(Path(p).resolve())) + os.sep for p in (output, stage)]
    for process in psutil.process_iter(["exe"]):
        try:
            executable = os.path.normcase(str(process.info.get("exe") or ""))
            if executable and any(executable.startswith(p) for p in targets):
                raise RuntimeError(
                    "Крест запущена из папки сборки (PID " + str(process.pid) + "). "
                    "Завершите её через трей и повторите build-exe.bat."
                )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if shutil.disk_usage(ROOT).free < 2 * 1024**3:
        raise RuntimeError("Для сборки освободите хотя бы 2 ГБ на диске проекта.")


def publish_distribution(dist, output, *, protect=False, with_models=True):
    """Prepare and verify the ZIP before replacing either published artifact."""
    dist, output = Path(dist), Path(output)
    output.mkdir(exist_ok=True)
    name = (
        "Krest-Windows-"
        + VERSION
        + ("-protected" if protect else "")
        + ("" if with_models else "-online-setup")
        + ".zip"
    )
    archive, target = output / name, output / "Krest"
    suffix = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    temporary = output / (name + "." + suffix + ".partial")
    previous = output / ("Krest-previous-" + suffix)
    backup = output / (name + ".previous-" + suffix)
    moved_old = moved_zip = moved_new = False
    try:
        print("Создаю ZIP и проверяю целостность файлов…", flush=True)
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as result:
            for path in sorted(dist.rglob("*")):
                if path.is_file():
                    result.write(path, Path("Krest") / path.relative_to(dist))
        with zipfile.ZipFile(temporary) as result:
            bad = result.testzip()
            if bad:
                raise RuntimeError("Повреждён файл в новом ZIP: " + bad)
            if "Krest/Krest.exe" not in result.namelist():
                raise RuntimeError("В архиве отсутствует Krest.exe")
        if target.exists():
            target.rename(previous)
            moved_old = True
        if archive.exists():
            archive.rename(backup)
            moved_zip = True
        dist.rename(target)
        moved_new = True
        temporary.rename(archive)
    except BaseException as exc:
        # Roll back only our own completed moves, in reverse order. Never delete
        # an earlier working release or a profile stored beside its executable.
        try:
            if moved_new:
                target.rename(dist)
            if moved_zip:
                backup.rename(archive)
            if moved_old:
                previous.rename(target)
        except OSError as rollback:
            raise RuntimeError(
                "Не удалось вернуть прежнюю сборку автоматически. Она сохранена в "
                + str(previous)
                + "; архив: "
                + str(backup)
            ) from rollback
        if isinstance(exc, OSError):
            raise RuntimeError(
                "Не удалось записать новую сборку. Прежние файлы сохранены. "
                "Закройте Крест из dist через трей, проверьте свободное место "
                "и доступ к папке. Причина: " + str(exc)
            ) from exc
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            print("Временный ZIP не удалось удалить:", temporary)
    if moved_zip:
        try:
            backup.unlink()
        except OSError:
            print("Предыдущий ZIP сохранён:", backup)
    if moved_old:
        print("Предыдущая сборка сохранена:", previous)
    return target, archive


def audit_distribution(dist):
    forbidden = {
        "config.json",
        "kernel_endpoint.json",
        "assistant_state.json",
        "favorites.json",
        "tg_auth_temp.json",
        "warframe_state.json",
        "warframe_route.json",
        "warframe_visual_memory.json",
    }
    own_sources = {p.name for p in ROOT.glob("*.py")}
    for path in Path(dist).rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(dist)
        if path.name in forbidden or path.name.endswith((".private.pem", ".session", ".sqlite3")):
            raise RuntimeError("Личные данные в сборке: " + str(relative))
        if any(
            part in {".git", ".github", "tests", "docs", ".venv", ".build-venv", "__pycache__"}
            for part in relative.parts
        ):
            raise RuntimeError("Служебный файл в сборке: " + str(relative))
        if path.name == "bot_bridge_adapter.py" and path.parent == Path(dist) / "_internal":
            # Deliberately exportable adapter, not an application source leak.
            if path.read_bytes() != (ROOT / "bot_bridge_adapter.py").read_bytes():
                raise RuntimeError("Экспортируемый адаптер отличается от исходного")
            continue
        # PyTorch can legitimately need its own .py data for TorchScript introspection.
        if path.suffix == ".py" and (
            relative.parts[0] == "skills"
            or relative.parts[:2] == ("_internal", "skills")
            or path.name in own_sources
            and path.parent in (Path(dist), Path(dist) / "_internal")
        ):
            raise RuntimeError("Открытый исходник Крест в сборке: " + str(relative))


def smoke_test(dist, *, with_models=True):
    executable = Path(dist) / "Krest.exe"
    with tempfile.TemporaryDirectory(prefix="krest-release-check-") as temp:
        root = Path(temp)
        cfg = root / "config.json"
        report = root / "skills.json"
        run_checked(
            [str(executable), "--check", "--check-output", str(report), "--config", str(cfg)],
            cwd=root,
            timeout=180,
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        if data["errors"] or data["skills"] < 168:
            raise RuntimeError("Навыки не загрузились: " + str(data))
        run_checked(
            [str(executable), "--release-smoke", "--config", str(cfg)], cwd=root, timeout=120
        )
        run_checked(
            [str(executable), "--release-import-check", "--config", str(cfg)], cwd=root, timeout=180
        )
        if with_models:
            run_checked(
                [str(executable), "--release-model-check", "--config", str(cfg)],
                cwd=root,
                timeout=240,
            )


def prepare_models():
    # Resolve/download before packaging, so network errors fail early.
    sys.path.insert(0, str(ROOT))
    from model_setup import prepare
    from configuration import ConfigManager

    found = prepare(
        ConfigManager().snapshot(),
        progress=print,
        force=True,
        destination=ROOT / "build" / "speech-models",
    )
    if not all(found.values()):
        raise RuntimeError("Для готового ZIP необходимы обе модели: Vosk и Silero")
    return found


def copy_models(dist, found=None):
    # Copy discovered models only, never the whole user's model/project directory.
    found = prepare_models() if found is None else found
    folder = Path(dist) / "model"
    for key, filename in (("vosk_path", "vosk"), ("silero_path", "model_silero.pt")):
        folder.mkdir(exist_ok=True)
        source = Path(found[key])
        if source.is_dir():
            if any(p.is_symlink() for p in source.rglob("*")):
                raise ValueError("Модель содержит ссылки; проверьте папку перед упаковкой")
            shutil.copytree(source, folder / filename)
        else:
            shutil.copy2(source, folder / filename)


def check_build_runtime():
    """Catch missing DLLs and incompatible installations before a long EXE build."""
    with tempfile.TemporaryDirectory(prefix="krest-builder-check-") as folder:
        run_checked(
            [
                sys.executable,
                str(ROOT / "main.py"),
                "--release-import-check",
                "--config",
                str(Path(folder) / "config.json"),
            ],
            cwd=ROOT,
            timeout=180,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", action="store_true")
    parser.add_argument(
        "--protect",
        action="store_true",
        help="Обфускация PyArmor; ошибка не вызывает незащищённую пересборку",
    )
    parser.add_argument("--license", help="Лицензия PyArmor для регистрации в авторской среде")
    models = parser.add_mutually_exclusive_group()
    models.add_argument(
        "--with-models",
        dest="with_models",
        action="store_true",
        default=True,
        help="Включить Vosk и Silero в ZIP (по умолчанию); скачать отсутствующие модели",
    )
    models.add_argument(
        "--without-models",
        dest="with_models",
        action="store_false",
        help="Облегчённый ZIP: модели потребуется скачать при первом запуске",
    )
    args = parser.parse_args()
    output = ROOT / "dist"
    stage = ROOT / "build" / "exe-stage"
    command = package_command(stage, protect=args.protect)
    if args.plan:
        print(subprocess.list2cmdline(command))
        return
    if os.name != "nt" or sys.version_info[:2] not in ((3, 11), (3, 12)) or sys.maxsize <= 2**32:
        raise SystemExit("Сборка требует Windows и Python 3.11/3.12 x64")
    if args.license and not args.protect:
        raise SystemExit("Лицензия используется только с --protect")
    preflight(output / "Krest", stage / "Krest")
    print("Проверяю библиотеки до упаковки…", flush=True)
    check_build_runtime()
    found_models = prepare_models() if args.with_models else None
    env = dict(os.environ, KREST_BUILD_ROOT=str(ROOT), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if args.protect:
        if importlib.util.find_spec("pyarmor") is None:
            run_checked(
                [sys.executable, "-m", "pip", "install", "pyarmor==9.2.7"], cwd=ROOT, env=env
            )
        if args.license:
            run_checked(
                [
                    sys.executable,
                    "-m",
                    "pyarmor.cli",
                    "reg",
                    str(Path(args.license).resolve(strict=True)),
                ],
                cwd=ROOT,
                env=env,
            )
    if stage.exists():
        shutil.rmtree(stage)
    stage.parent.mkdir(parents=True, exist_ok=True)
    # Work on a disposable spec: PyArmor creates a .patched.spec next to it.
    spec = stage.parent / "krest.spec"
    shutil.copy2(ROOT / "packaging/krest.spec", spec)
    command = package_command(stage, protect=args.protect, spec=spec)
    print("[1/4] Упаковываю программу…", flush=True)
    run_checked(command, cwd=ROOT, env=env)
    dist = stage / "Krest"
    print("[2/4] Подготавливаю модели голоса…", flush=True)
    if args.with_models:
        copy_models(dist, found_models)
    audit_distribution(dist)
    print("[3/4] Проверяю готовую программу, ядро, библиотеки и голос…", flush=True)
    smoke_test(dist, with_models=args.with_models)
    (dist / "build-info.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "python": sys.version.split()[0],
                "with_models": args.with_models,
                "protected": args.protect,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("[4/4] Выпускаю папку программы и ZIP…", flush=True)
    target, archive = publish_distribution(
        dist, output, protect=args.protect, with_models=args.with_models
    )
    print("::notice::EXE, skills, child kernel and native runtime imports passed.")
    if args.with_models:
        print(
            "::notice::Bundled Vosk and real Silero synthesis passed from a clean profile, offline."
        )
    print("Готово: " + str(target / "Krest.exe"))
    print("Архив для передачи: " + str(archive))
    if args.protect:
        print("Код обработан PyArmor; абсолютная защита от анализа не гарантируется.")
    else:
        print("Обычная упаковка: исходники не лежат открыто, но байткод можно извлечь.")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        with BuildSession(ROOT):
            main()
    except (Exception, KeyboardInterrupt) as exc:
        message = str(exc)[-2000:]
        print(
            "::error::" + message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
            flush=True,
        )
        raise SystemExit(1)
