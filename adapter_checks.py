"""Explicit capability checks, with executable changes tracked between checks."""

import json
from pathlib import Path
import time


def check(kind, memory, preferred=""):
    if kind not in ("music", "discord"):
        raise ValueError("Неизвестный адаптер")
    import psutil

    needles = (
        ("discord",)
        if kind == "discord"
        else ("yandexmusic", "яндекс", "spotify", "aimp", "vlc", "chrome", "msedge")
    )
    executables = {}
    for process in psutil.process_iter(["name", "exe"]):
        if not any(v in (process.info.get("name") or "").casefold() for v in needles):
            continue
        try:
            path = Path(process.info["exe"])
            info = path.stat()
            executables[str(path)] = [info.st_size, info.st_mtime_ns]
        except (OSError, TypeError):
            continue
    key = "адаптер " + kind
    try:
        previous = json.loads(memory.recall(key, "{}"))
    except (ValueError, TypeError):
        previous = {}
    changed = bool(
        isinstance(previous, dict)
        and previous.get("executables")
        and previous["executables"] != executables
    )
    try:
        if kind == "music":
            from media_control import diagnostics

            text = diagnostics(preferred)
        else:
            from media_desktop import request

            result = request("discord_voice", action="status")
            text = str(result.get("message", result.get("text", result)))
        okay = True
    except Exception as exc:
        text, okay = str(exc), False
    data = {
        "executables": dict(list(executables.items())[:6]),
        "time": time.time(),
        "reachable": okay,
        "report": text[:1200],
    }
    encoded = json.dumps(data, ensure_ascii=False)
    if len(encoded) > 4000:
        data["executables"] = {}
    memory.remember(key, json.dumps(data, ensure_ascii=False), "application")
    return (
        ("Файл или состав приложений изменился с прошлой проверки.\n" if changed else "")
        + text
        + "\n\nПроверка выполнена без команд воспроизведения и переключения микрофона. Состояние сохранено в памяти Крест."
    )
