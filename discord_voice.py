"""Control the local Discord microphone through its own accessible user panel."""

from contextlib import ExitStack, contextmanager
import time

import psutil

from media_control import MediaError
from media_rating import _enable_accessibility, _label, _toggle, uia_apartment

PROCESS_NAMES = {"discord.exe", "discordptb.exe", "discordcanary.exe"}
MUTE_LABELS = {
    "mute",
    "mute microphone",
    "выключить микрофон",
    "отключить микрофон",
    "заглушить микрофон",
}
UNMUTE_LABELS = {
    "unmute",
    "unmute microphone",
    "включить микрофон",
    "снять заглушение микрофона",
}
DEAFEN_LABELS = {"deafen", "undeafen", "отключить звук", "включить звук"}
SETTINGS_LABELS = {"user settings", "настройки пользователя"}


def _controls(container):
    return [
        control
        for control in container.descendants()
        if control.element_info.control_type in ("Button", "CheckBox")
    ]


def microphone_button(window):
    """Require the user's microphone beside Deafen and User Settings controls.

    Discord also has per-user mute menu items and channel notification buttons.
    Those are not the microphone. Never substitute a global toggle hotkey.
    """
    candidates = []
    for button in _controls(window):
        if _label(button) not in MUTE_LABELS | UNMUTE_LABELS or not button.is_enabled():
            continue
        parent = button
        for _ in range(4):
            parent = parent.parent()
            if parent is None or parent == window:
                break
            controls = _controls(parent)
            if len(controls) > 25:
                break
            labels = {_label(control) for control in controls}
            if labels & DEAFEN_LABELS and labels & SETTINGS_LABELS:
                candidates.append(button)
                break
    if len(candidates) != 1:
        raise MediaError(
            "Не удалось однозначно найти ваш микрофон в Discord. Откройте главное окно "
            "Discord с кнопками микрофона, наушников и настроек, затем повторите команду."
        )
    return candidates[0]


def is_muted(button):
    # aria-checked is authoritative when Electron exposes a UIA TogglePattern.
    state = _toggle(button)
    if state is not None:
        return bool(state)
    label = _label(button)
    if label in UNMUTE_LABELS:
        return True
    if label in MUTE_LABELS:
        return False
    raise MediaError("Discord не сообщил состояние микрофона. Повторите проверку.")


def apply_microphone(window, action):
    if action not in ("mute", "unmute", "status"):
        raise MediaError("Неизвестная команда микрофона Discord.")
    button = microphone_button(window)
    current = is_muted(button)
    if action == "status":
        return _result(current, "Микрофон в Discord " + ("выключен." if current else "включён."))
    desired = action == "mute"
    if current == desired:
        return _result(
            current, "Микрофон в Discord уже " + ("выключен." if current else "включён.")
        )
    try:
        if _toggle(button) is not None:
            button.toggle()
        else:
            button.invoke()
    except Exception as exc:
        raise MediaError(
            "Discord не подтвердил нажатие. Скажите «проверь микрофон в Дискорде» перед повтором."
        ) from exc
    for _ in range(10):
        time.sleep(0.15)
        try:
            # React can replace the original control after a state change.
            current = is_muted(microphone_button(window))
        except Exception:
            continue
        if current == desired:
            return _result(
                current,
                "Выключила микрофон в Discord." if current else "Включила микрофон в Discord.",
            )
    raise MediaError(
        "Команда отправлена, но Discord не подтвердил состояние. "
        "Скажите «проверь микрофон в Дискорде»."
    )


def _result(muted, message):
    return {"success": True, "muted": muted, "message": message}


@contextmanager
def _expose_window(window):
    """Temporarily expose a minimized/tray window without activating it."""
    import win32con
    import win32gui

    handle = window.handle
    hidden = not win32gui.IsWindowVisible(handle)
    minimized = bool(win32gui.IsIconic(handle))
    try:
        if hidden or minimized:
            win32gui.ShowWindow(handle, win32con.SW_SHOWNOACTIVATE)
        yield
    finally:
        if win32gui.IsWindow(handle):
            if minimized:
                win32gui.ShowWindow(handle, win32con.SW_SHOWMINNOACTIVE)
            if hidden:
                win32gui.ShowWindow(handle, win32con.SW_HIDE)


def perform(action):
    """Run only on the main thread of the bounded desktop helper process."""
    if action not in ("mute", "unmute", "status"):
        raise MediaError("Неизвестная команда микрофона Discord.")
    pids = {
        process.info["pid"]
        for process in psutil.process_iter(["pid", "name"])
        if (process.info["name"] or "").casefold() in PROCESS_NAMES
    }
    if not pids:
        raise MediaError("Discord не запущен. Откройте настольный Discord и войдите в аккаунт.")
    with uia_apartment(), ExitStack() as stack:
        from pywinauto import Desktop

        native = Desktop(backend="win32")
        desktop = Desktop(backend="uia")
        windows = []
        for window in native.windows(visible_only=False):
            try:
                if (
                    window.process_id() not in pids
                    or window.class_name() != "Chrome_WidgetWin_1"
                    or not window.window_text()
                ):
                    continue
                stack.enter_context(_expose_window(window))
                _enable_accessibility(window.handle)
                candidate = desktop.window(handle=window.handle).wrapper_object()
                for attempt in range(3):
                    try:
                        microphone_button(candidate)
                        windows.append(candidate)
                        break
                    except MediaError:
                        if attempt < 2:
                            time.sleep(0.2)
            except Exception:
                continue
        if len(windows) != 1:
            raise MediaError(
                "Не найдено единственное окно Discord с вашим микрофоном. "
                "Откройте главное окно Discord; если запущены несколько клиентов, оставьте один."
            )
        return apply_microphone(windows[0], action)
