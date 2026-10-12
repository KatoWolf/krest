"""Control the local Discord microphone through its own accessible user panel."""

from contextlib import ExitStack
import time

import psutil

from media_control import MediaError
from media_rating import _enable_accessibility, _label, _toggle, uia_apartment
from desktop_access import expose_window as _expose_window

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
SETTINGS_LABELS |= {"настройки", "settings", "открыть настройки пользователя"}
MUTE_LABELS |= {"отключить микрофон", "отключить свой микрофон", "mute audio"}
UNMUTE_LABELS |= {"включить свой микрофон", "unmute audio"}
DEAFEN_LABELS |= {"заглушить звук", "отключить наушники", "включить наушники"}


def _discord_label(control):
    import re

    label = _label(control)
    # New Electron builds expose shortcuts after a comma or on a separate line.
    label = re.sub(r"\s*[,·—:-]?\s*(?:ctrl|control|alt|shift)\s*\+.*$", "", label)
    return label.strip(" ,.:;—-()")


def _controls(container):
    try:
        # Native UIA conditions avoid marshaling every chat message/text node.
        controls = container.descendants(control_type="Button") + container.descendants(
            control_type="CheckBox"
        )
        if not any(_discord_label(c) in MUTE_LABELS | UNMUTE_LABELS for c in controls):
            for kind in ("Custom", "Group"):
                controls.extend(
                    c
                    for c in container.descendants(control_type=kind)
                    if _discord_label(c)
                    in MUTE_LABELS | UNMUTE_LABELS | DEAFEN_LABELS | SETTINGS_LABELS
                )
        return controls
    except TypeError:
        pass
    return [
        control
        for control in container.descendants()
        if control.element_info.control_type in ("Button", "CheckBox", "ToggleButton")
        or (
            control.element_info.control_type in ("Custom", "Group")
            and _discord_label(control)
            in MUTE_LABELS | UNMUTE_LABELS | DEAFEN_LABELS | SETTINGS_LABELS
        )
    ]


def microphone_button(window):
    """Require the user's microphone beside Deafen and User Settings controls.

    Discord also has per-user mute menu items and channel notification buttons.
    Those are not the microphone. Never substitute a global toggle hotkey.
    """
    candidates = []
    controls_in_window = _controls(window)

    # Discord's redesigned user bar can split controls between DOM containers.
    # UIA rectangles identify the bar; invocation still targets the named button.
    def beside(first, second):
        try:
            a, b = first.rectangle(), second.rectangle()
            if min(a.right - a.left, a.bottom - a.top, b.right - b.left, b.bottom - b.top) <= 0:
                return False
            return (
                abs((a.top + a.bottom) - (b.top + b.bottom)) / 2 <= 24
                and abs((a.left + a.right) - (b.left + b.right)) / 2 <= 180
            )
        except Exception:
            return False

    for button in controls_in_window:
        if _discord_label(button) not in MUTE_LABELS | UNMUTE_LABELS or not button.is_enabled():
            continue
        neighbours = {_discord_label(c) for c in controls_in_window if beside(button, c)}
        if neighbours & DEAFEN_LABELS and neighbours & SETTINGS_LABELS:
            candidates.append(button)
            continue
        parent = button
        for _ in range(8):
            parent = parent.parent()
            if parent is None:
                break
            controls = _controls(parent)
            # The root may also contain another user's context menu. A flat
            # root is acceptable only when this is its sole microphone control.
            if (
                parent == window
                and sum(_discord_label(c) in MUTE_LABELS | UNMUTE_LABELS for c in controls) != 1
            ):
                break
            if len(controls) > 80:
                break
            labels = {_discord_label(control) for control in controls}
            if labels & DEAFEN_LABELS and labels & SETTINGS_LABELS:
                candidates.append(button)
                break
            if parent == window:
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
    label = _discord_label(button)
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
            "Discord не подтвердил нажатие. Скажите «Крест, я в муте» перед повтором."
        ) from exc
    for attempt in range(10):
        if attempt:
            time.sleep(0.12)
        try:
            # React can replace the original control after a state change.
            current = is_muted(button)
            if current != desired:
                button = microphone_button(window)
                current = is_muted(button)
        except Exception:
            try:
                button = microphone_button(window)
                current = is_muted(button)
            except Exception:
                continue
        if current == desired:
            return _result(
                current,
                "Выключила микрофон в Discord." if current else "Включила микрофон в Discord.",
            )
    raise MediaError(
        "Команда отправлена, но Discord не подтвердил состояние. Скажите «Крест, я в муте»."
    )


def _result(muted, message):
    return {"success": True, "muted": muted, "message": message}


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
        exposed = 0
        hints = set()
        for window in native.windows(visible_only=False, enabled_only=False):
            try:
                if window.process_id() not in pids or not window.class_name().startswith(
                    "Chrome_WidgetWin_"
                ):
                    continue
                stack.enter_context(_expose_window(window))
                exposed += 1
                _enable_accessibility(window.handle)
                candidate = desktop.window(handle=window.handle).wrapper_object()
                for attempt in range(8):
                    try:
                        microphone_button(candidate)
                        windows.append(candidate)
                        break
                    except MediaError:
                        if attempt == 7:
                            hints.update(
                                _discord_label(c)
                                for c in _controls(candidate)
                                if any(
                                    token in _discord_label(c)
                                    for token in (
                                        "микро",
                                        "mute",
                                        "звук",
                                        "настройк",
                                        "settings",
                                        "deafen",
                                    )
                                )
                            )
                        if attempt < 7:
                            time.sleep(0.2)
            except Exception:
                continue
        if len(windows) != 1:
            raise MediaError(
                "Discord запущен, но не предоставил доступ к вашей кнопке микрофона. "
                "Проверьте вход в аккаунт и одинаковые права запуска Discord и Крест. "
                "Если открыты несколько клиентов Discord, оставьте один."
                + (
                    " Окна Discord не найдены."
                    if not exposed
                    else " Панель: "
                    + ("; ".join(sorted(hints)[:8]) or "кнопки не доступны через Windows")
                )
            )
        return apply_microphone(windows[0], action)
