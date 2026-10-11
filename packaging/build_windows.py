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

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.3"


def run_checked(command, **kwargs):
    """Stream progress and retain a bounded failure summary for CI."""
    from collections import deque
    import threading

    timeout = kwargs.pop("timeout", None)
    kwargs.pop("check", None)
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
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            reader.join(timeout=3)
            detail = f"Command timed out after {timeout}s. Last output: " + "".join(tail)[-2500:]
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


def copy_models(dist):
    # Copy discovered models only, never the whole user's model/project directory.
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
    run_checked(command, cwd=ROOT, env=env)
    dist = stage / "Krest"
    if args.with_models:
        copy_models(dist)
    audit_distribution(dist)
    smoke_test(dist, with_models=args.with_models)
    output.mkdir(exist_ok=True)
    target = output / "Krest"
    if target.exists():
        import time

        previous = output / (
            "Krest-previous-" + time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns() % 1000000)
        )
        target.rename(previous)
        print("Предыдущая сборка сохранена: " + str(previous))
    shutil.move(str(dist), str(target))
    archive = output / (
        "Krest-Windows-"
        + VERSION
        + ("-protected" if args.protect else "")
        + ("" if args.with_models else "-online-setup")
        + ".zip"
    )
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as result:
        for path in target.rglob("*"):
            if path.is_file():
                result.write(path, path.relative_to(output))
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
        main()
    except Exception as exc:
        message = str(exc)[-2000:]
        print(
            "::error::" + message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"),
            flush=True,
        )
        raise
