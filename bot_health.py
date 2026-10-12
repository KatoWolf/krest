"""Bounded, read-only project diagnostics; never import or execute the bot."""

import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def inspect_project(path):
    script = Path(path)
    manifest = script.with_suffix(".krest.json")
    if manifest.is_file() and manifest.stat().st_size < 16000:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata.get("generated_by") == "krest_bot_connector_v1" and metadata.get("entrypoint"):
            entry = metadata["entrypoint"]
            if not isinstance(entry, str) or Path(entry).name != entry:
                raise ValueError("Некорректный файл запуска в описании проекта")
            target = (script.parent / entry).resolve()
            if target.parent != script.parent.resolve():
                raise ValueError("Файл запуска должен находиться в папке проекта")
            script = target
    if not script.is_file():
        from app_paths import compiled

        return (
            "Встроенный модуль находится внутри EXE."
            if compiled()
            else "Файл запуска не найден: " + str(script)
        )
    if script.stat().st_size > 2_000_000:
        raise ValueError("Файл запуска слишком большой для быстрой проверки")
    tree = ast.parse(script.read_text(encoding="utf-8-sig"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(v.name.split(".")[0] for v in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module.split(".")[0])
    modules = sorted(
        m
        for m in modules
        if not (script.parent / (m + ".py")).exists() and not (script.parent / m).is_dir()
    )[:80]
    python = next(
        (
            p
            for name in (".venv", "venv")
            for suffix in ("Scripts/python.exe", "bin/python")
            if (p := script.parent / name / suffix).is_file()
        ),
        None,
    )
    from app_paths import compiled

    if python is None and compiled():
        return "Синтаксис файла корректен. Среда Python проекта не найдена; используются встроенные библиотеки Крест. Дополнительные зависимости проекта проверяются в его .venv."
    code = "import importlib.util,json,sys; print(json.dumps([n for n in json.loads(sys.argv[1]) if importlib.util.find_spec(n) is None]))"
    result = subprocess.run(
        [str(python or sys.executable), "-I", "-c", code, json.dumps(modules)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=8,
        cwd=tempfile.gettempdir(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        return "Файл прочитан, но Python проекта не прошёл проверку. Проверьте его .venv."
    missing = json.loads(result.stdout)
    return (
        "Файл запуска: "
        + script.name
        + "\nСинтаксис: корректен\nPython: "
        + str(python or sys.executable)
        + "\n"
        + (
            "Не найдены модули: " + ", ".join(missing)
            if missing
            else "Импортируемые модули доступны"
        )
        + "\nБот не запускался; соединение и токен этой проверкой не проверяются."
    )
