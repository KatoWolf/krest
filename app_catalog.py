"""Discover launchable Windows apps; aliases resolve to explicit catalog entries."""

import json
import os
from pathlib import Path
import subprocess


def scan_apps():
    if os.name != "nt":
        return []
    result = {}
    roots = [
        Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.lnk"):
            if path.is_symlink() or not path.is_file():
                continue
            name = path.stem
            result[name.casefold()] = {
                "name": name,
                "kind": "shortcut",
                "target": str(path.resolve()),
            }
            if len(result) >= 2000:
                break
    command = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8-sig",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        entries = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else []
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries[:2000]:
            if (
                isinstance(entry.get("Name"), str)
                and isinstance(entry.get("AppID"), str)
                and "!" in entry["AppID"]
            ):
                result.setdefault(
                    entry["Name"].casefold(),
                    {"name": entry["Name"], "kind": "app_id", "target": entry["AppID"]},
                )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return sorted(result.values(), key=lambda v: v["name"].casefold())


def launch(name, settings):
    aliases = settings.get("application_aliases", {})
    query = aliases.get(name.casefold().strip(), name).casefold().strip()
    rows = settings.get("applications", [])
    found = [v for v in rows if v["name"].casefold() == query]
    if len(found) != 1:
        raise ValueError(
            "Программа не найдена однозначно. Обновите каталог программ или добавьте короткое имя в меню."
        )
    item = found[0]
    if os.name != "nt":
        raise ValueError("Запуск каталога программ доступен в Windows")
    if item["kind"] == "app_id":
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + item["target"]], shell=False)
    else:
        path = Path(item["target"])
        if not path.is_file() or path.suffix.casefold() not in (".lnk", ".exe"):
            raise ValueError("Программа была удалена или путь изменился")
        os.startfile(str(path))
    message = "Запрос на запуск передан Windows: " + item["name"]
    from media_control import app_name

    context = {}
    player = app_name(item["name"])
    if player in {"Яндекс Музыка", "Spotify", "AIMP", "VLC"}:
        context = {"scope": "music", "name": player}
    elif item["kind"] == "exe":
        context = {"scope": "volume", "process": Path(item["target"]).name}
    if item["kind"] == "exe":
        return {
            "success": True,
            "message": message,
            "verification": "pending",
            "check": {"kind": "process", "path": item["target"]},
            "voice_context": context,
        }
    return {"success": True, "message": message, "voice_context": context}


def validate_apps(data):
    rows = data.get("applications", [])
    if not isinstance(rows, list) or len(rows) > 4000:
        raise ValueError("Некорректный каталог программ")
    for item in rows:
        if (
            not isinstance(item, dict)
            or item.get("kind") not in ("shortcut", "app_id", "exe")
            or any(
                not isinstance(item.get(k), str) or not 1 <= len(item[k]) <= 2000
                for k in ("name", "target")
            )
        ):
            raise ValueError("Некорректная запись программы")
        if item["kind"] == "app_id" and (
            not __import__("re").fullmatch(r"[\w.!-]+", item["target"]) or "!" not in item["target"]
        ):
            raise ValueError("Некорректный идентификатор приложения Windows")
    aliases = data.get("application_aliases", {})
    if (
        not isinstance(aliases, dict)
        or len(aliases) > 200
        or any(
            not isinstance(v, str) or not 1 <= len(k) <= 100 or not 1 <= len(v) <= 200
            for k, v in aliases.items()
        )
    ):
        raise ValueError("Некорректные короткие имена программ")
