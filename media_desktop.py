"""Bounded desktop accessibility requests in reusable, idle-expiring Windows processes.

UI Automation caches apartment-bound COM objects. Keep those out of the GUI,
kernel workers and WinRT media-session process. Each application family has a separate helper with a bounded lifetime.
"""

import json
import os
import sys

from media_control import MediaError

PREFIX = "KREST_DESKTOP_RESULT:"


def request(operation, data=None, action=""):
    if os.name != "nt":
        raise MediaError("Управление кнопками настольных приложений доступно в Windows.")
    from desktop_transport import request as desktop_request

    try:
        response = desktop_request({"operation": operation, "data": data or {}, "action": action})
    except (OSError, TimeoutError) as exc:
        raise MediaError(str(exc)) from exc
    if response.get("error"):
        raise MediaError(response["error"])
    value = response.get("result")
    if not isinstance(value, dict):
        raise MediaError("Приложение не подтвердило результат команды.")
    return value


def run_worker(service=False):
    if service:
        from desktop_transport import serve
        from media_rating import uia_apartment

        with uia_apartment():
            return serve(_handle)
    return _handle(sys.stdin.read(32769))


def _handle(raw):
    operation = ""
    request_id = None
    try:
        if os.name != "nt":
            raise MediaError("Доступ к кнопкам требует Windows.")
        if len(raw) > 32768:
            raise MediaError("Слишком большой запрос к плееру.")
        payload = json.loads(raw)
        request_id = payload.get("request_id")
        operation = payload["operation"]
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise MediaError("Некорректный запрос к приложению.")
        if operation in ("rating", "diagnostics"):
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
        elif operation == "discord_voice":
            from discord_voice import perform

            result = perform(payload.get("action", ""))
        elif operation == "file_context":
            from object_actions import explorer_selection

            result = explorer_selection()
        elif operation == "audio_dispatch":
            from audio_dispatcher import native_snapshot, native_apply

            if payload.get("action") not in {"snapshot", "apply"}:
                raise MediaError("Неизвестное действие звукового диспетчера")
            result = native_snapshot() if payload["action"] == "snapshot" else native_apply(data)
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
        detail = str(exc)
        if operation == "probe" and exc.__cause__:
            detail += " · " + repr(exc.__cause__)
        response = {"error": detail}
        if operation == "probe":
            import traceback

            response["traceback"] = traceback.format_exc()
        code = 1
    response["request_id"] = request_id
    print(PREFIX + json.dumps(response, ensure_ascii=True), flush=True)
    return code
