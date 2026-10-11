"""Bounded desktop accessibility requests in a short-lived Windows process.

UI Automation caches apartment-bound COM objects. Keep those out of the GUI,
kernel workers and WinRT media-session process. No persistent helper is needed.
"""

import json
import os
import subprocess
import sys

from media_control import MediaError

PREFIX = "KREST_DESKTOP_RESULT:"


def request(operation, data=None, action=""):
    if os.name != "nt":
        raise MediaError("Кнопки настольных плееров доступны в Windows.")
    from app_paths import app_command

    # Frozen Windows can decode stdin using the legacy console codepage.
    # Keep the wire protocol ASCII; JSON restores Cyrillic labels exactly.
    payload = json.dumps(
        {"operation": operation, "data": data or {}, "action": action}, ensure_ascii=False
    )
    try:
        result = subprocess.run(
            app_command("--desktop-worker"),
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            "Плеер не ответил за 20 секунд. Помощник доступа остановлен. Проверьте отметку в плеере перед повторной командой."
        ) from exc
    except OSError as exc:
        raise MediaError("Не удалось открыть помощник доступа к плееру: " + str(exc)) from exc
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(PREFIX):
            try:
                response = json.loads(line[len(PREFIX) :])
                if response.get("error"):
                    raise MediaError(response["error"])
                value = response["result"]
                if result.returncode or not isinstance(value, dict):
                    break
                return value
            except (ValueError, TypeError, KeyError):
                break
    raise MediaError(
        "Помощник доступа к плееру завершился без результата. Перезапустите Крест и откройте диагностику музыки."
    )


def run_worker():
    try:
        if os.name != "nt":
            raise MediaError("Доступ к кнопкам требует Windows.")
        raw = sys.stdin.read(32769)
        if len(raw) > 32768:
            raise MediaError("Слишком большой запрос к плееру.")
        payload = json.loads(raw)
        operation = payload["operation"]
        data = payload.get("data", {})
        if operation != "probe":
            if not isinstance(data, dict) or any(
                not isinstance(data.get(k), str) or len(data[k]) > 4096
                for k in ("title", "artist", "app_id")
            ):
                raise MediaError("Некорректные сведения о текущем треке.")
        from media_rating import inspect_player, perform_rating, uia_apartment

        if operation == "rating":
            result = perform_rating(data, payload["action"])
        elif operation == "diagnostics":
            result = {"lines": inspect_player(data)}
        elif operation == "probe":
            with uia_apartment():
                from pywinauto import Desktop

                result = {
                    "windows": len(Desktop(backend="uia").windows()),
                    "echo": data.get("title", ""),
                }
        else:
            raise MediaError("Неизвестное действие помощника плеера.")
        response = {"result": result}
        code = 0
    except Exception as exc:
        response = {"error": str(exc)}
        code = 1
    print(PREFIX + json.dumps(response, ensure_ascii=True), flush=True)
    return code
