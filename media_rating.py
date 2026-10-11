"""Track ratings through the verified desktop player's accessibility tree.

SMTC handles transport controls; it has no rating API. Ratings require a named
button in the current-track panel. Never guess a click position or send hotkeys.
"""

from contextlib import contextmanager, ExitStack
import os
import re
import time

import psutil
from media_control import MediaError
from desktop_access import expose_window

LABELS = {
    "like": {
        "мне нравится",
        "лайк",
        "поставить лайк",
        "нравится",
        "like",
        "добавить в любимое",
        "добавить в избранное",
        "save to your liked songs",
        "добавить в коллекцию",
        "добавить трек в коллекцию",
        "добавить в любимые треки",
        "добавить трек в избранное",
        "добавить трек в любимое",
        "добавить в мою музыку",
    },
    "unlike": {
        "убрать из любимого",
        "снять лайк",
        "убрать лайк",
        "удалить из любимого",
        "удалить из избранного",
        "remove from your liked songs",
        "unlike",
        "удалить из коллекции",
        "удалить трек из коллекции",
        "убрать из коллекции",
        "удалить из любимых треков",
        "удалить трек из избранного",
        "больше не нравится",
    },
    "dislike": {
        "не нравится",
        "дизлайк",
        "dislike",
        "не рекомендовать этот трек",
        "не рекомендовать",
    },
}
# Recommendation cards can contain Play + Like too. A transport panel must
# contain track navigation, or the active Pause button, to distinguish it.
PLAYER_ANCHORS = {
    "следующий трек",
    "предыдущий трек",
    "следующий",
    "предыдущий",
    "next track",
    "previous track",
    "next",
    "previous",
    "пауза",
    "pause",
    "приостановить",
    "приостановить воспроизведение",
}


def normalize_label(value):
    value = " ".join(value.casefold().replace("ё", "е").split()).strip()
    # Accessible tooltips can include keyboard hints; don't strip arbitrary track names.
    return re.sub(r"\s*\((?:пробел|space|[a-zа-я]|(?:ctrl|alt|shift)\s*\+[^)]+)\)$", "", value)


def player_processes(app_id):
    value = re.sub(r"[^a-zа-я0-9]", "", app_id.casefold())
    if any(token in value for token in ("yandexmusic", "yandexdesktopmusic", "яндексмузыка")):
        return {"yandexmusic.exe", "yandex music.exe", "яндекс музыка.exe"}
    if "spotify" in value:
        return {"spotify.exe"}
    raise MediaError(
        "У этого источника нет адаптера отметок. Поддерживаются доступные кнопки настольных Яндекс Музыки и Spotify."
    )


@contextmanager
def uia_apartment():
    # The GUI and the kernel call this adapter from worker threads, not the import thread.
    if os.name != "nt":
        yield
        return
    # This context only runs in the isolated helper's main thread.
    import sys
    import types
    from importlib.machinery import ModuleSpec
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="krest-uia-", ignore_cleanup_errors=True) as cache:
        try:
            import comtypes

            # Register a writable, process-private namespace BEFORE importing
            # client: its own cache creation uses a racy exists()/makedirs() pair.
            if "comtypes.client" not in sys.modules:
                generated = types.ModuleType("comtypes.gen")
                generated.__path__ = [cache]
                generated.__package__ = "comtypes.gen"
                generated.__spec__ = ModuleSpec("comtypes.gen", loader=None, is_package=True)
                sys.modules["comtypes.gen"] = generated
                comtypes.gen = generated
            import comtypes.client

            comtypes.gen.__path__ = [cache]
            comtypes.client.gen_dir = None
            import pywinauto  # noqa: F401
        except ImportError as exc:
            raise MediaError(
                "Откройте Музыка → Подключить управление музыкой: нужны компоненты доступа к кнопкам."
            ) from exc
        owned = False
        try:
            comtypes.CoInitializeEx()
            owned = True
        except OSError as exc:
            if (getattr(exc, "winerror", 0) or 0) & 0xFFFFFFFF != 0x80010106:
                raise
            raise MediaError(
                "Доступ к кнопкам занят другим режимом Windows. Полностью перезапустите Крест, включая ядро в трее."
            ) from exc
        try:
            yield
        finally:
            if owned:
                comtypes.CoUninitialize()


def _label(button):
    try:
        return normalize_label(button.window_text())
    except Exception:
        return ""


def _toggle(button):
    try:
        value = button.get_toggle_state()
        return value if value in (0, 1) else None
    except Exception:
        return None


def _available(button):
    try:
        return button.is_enabled() and button.is_visible()
    except Exception:
        return False


def _rating_button(buttons, action):
    opposite = "unlike" if action == "like" else "like"
    accepted = LABELS[action] | (LABELS[opposite] if action != "dislike" else set())
    matches = [b for b in buttons if _label(b) in accepted and _available(b)]
    if len(matches) != 1:
        raise MediaError(
            "Не удалось однозначно найти кнопку текущего трека. Разверните Яндекс Музыку с нижней панелью плеера и повторите команду. Диагностика доступна в меню «Музыка»."
        )
    return matches[0]


def apply_rating(buttons, action, refresh=None, verify=None):
    if action not in LABELS:
        raise MediaError("Неизвестная отметка трека.")
    button = _rating_button(buttons, action)
    opposite = "unlike" if action == "like" else "like"
    desired = 0 if action == "unlike" else 1

    def is_desired(candidate):
        state = _toggle(candidate)
        return (
            state == desired
            if state is not None
            else (action != "dislike" and _label(candidate) in LABELS[opposite])
        )

    if verify:
        verify()
    if is_desired(button):
        return {"success": True, "message": "Нужная отметка уже установлена."}
    try:
        if _toggle(button) is not None:
            button.toggle()
        else:
            button.invoke()
    except Exception as exc:
        # Do not retry a click: an unconfirmed first invocation might have succeeded.
        raise MediaError(
            "Плеер не принял нажатие кнопки. Откройте его окно и повторите команду."
        ) from exc
    for _ in range(10):
        time.sleep(0.15)
        if refresh:
            try:
                # Electron can replace the button node after a React re-render.
                button = _rating_button(refresh(), action)
            except MediaError:
                continue
        if is_desired(button):
            if verify:
                verify()
            return {
                "success": True,
                "message": {
                    "like": "Добавила трек в любимые.",
                    "unlike": "Убрала отметку «Нравится».",
                    "dislike": "Поставила отметку «Не нравится».",
                }[action],
            }
    raise MediaError(
        "Кнопка получила команду, но плеер не подтвердил изменение отметки. Проверьте её в плеере."
    )


def _enable_accessibility(handle):
    """Ask Chromium for its native accessibility object without moving focus."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    send = ctypes.windll.user32.SendMessageTimeoutW
    send.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    send(handle, 0x003D, 0, -4, 0x0002, 400, ctypes.byref(result))  # WM_GETOBJECT, OBJID_CLIENT


def _elements(window):
    return window.descendants()


def _has_track(elements, data):
    labels = {_label(e) for e in elements}
    title, artist = normalize_label(data["title"]), normalize_label(data.get("artist", ""))
    if not title:
        return False
    # Text nodes may be links or groups, not just UIA Text controls.
    title_found = title in labels or any(
        label in (f"{title} — {artist}", f"{artist} — {title}", f"{title}, {artist}")
        for label in labels
        if artist
    )
    return title_found


def find_player_window(data, stack=None):
    names = player_processes(data["app_id"])
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise MediaError(
            "Откройте Музыка → Подключить управление музыкой: Крест установит компоненты сама."
        ) from exc
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        if (proc.info["name"] or "").casefold() in names:
            pids.add(proc.info["pid"])
    # Native discovery also works when Electron hasn't exposed its UIA tree yet.
    handles = []
    for window in Desktop(backend="win32").windows(visible_only=False, enabled_only=False):
        try:
            if (
                window.process_id() in pids
                and window.class_name().startswith("Chrome_WidgetWin_")
                and len(handles) < 8
            ):
                if stack is not None:
                    stack.enter_context(expose_window(window))
                handles.append(window.handle)
        except Exception:
            continue
    windows = []
    desktop = Desktop(backend="uia")
    for handle in handles[:8]:
        try:
            _enable_accessibility(handle)
            window = desktop.window(handle=handle).wrapper_object()
            for attempt in range(8):
                if _has_track(_elements(window), data):
                    windows.append(window)
                    break
                if attempt < 7:
                    time.sleep(0.2)
        except Exception:
            continue
    if len(windows) != 1:
        raise MediaError(
            "Не удалось прочитать текущий трек в окне плеера. Разверните Яндекс Музыку, оставьте нижнюю панель видимой и повторите. Если не поможет, откройте «Музыка → Диагностика плеера»."
        )
    return windows[0]


def track_buttons(window, data):
    """Prefer the smallest ancestor that contains track text AND transport controls."""
    elements = _elements(window)
    title = normalize_label(data["title"])
    anchors = [e for e in elements if _label(e) == title]
    candidates = []
    for anchor in anchors[:12]:
        parent = anchor
        for _ in range(7):
            try:
                parent = parent.parent()
                if parent is None or parent == window:
                    break
                buttons = parent.descendants(control_type="Button")
                labels = {_label(b) for b in buttons if _available(b)}
                if labels & PLAYER_ANCHORS and labels & set().union(*LABELS.values()):
                    candidates.append((len(buttons), buttons))
                    break
            except Exception:
                break
    if candidates:
        candidates.sort(key=lambda row: row[0])
        smallest = candidates[0][0]
        # Merge equivalent-size panels; ambiguity must reach _rating_button.
        result, seen = [], set()
        for size, buttons in candidates:
            if size != smallest:
                break
            for button in buttons:
                try:
                    key = tuple(button.element_info.runtime_id)
                except Exception:
                    key = id(button)
                if key not in seen:
                    seen.add(key)
                    result.append(button)
        return result
    # Flat accessibility trees have no named player container. Use the actual
    # transport row to filter named controls, never to synthesize click positions.
    buttons = window.descendants(control_type="Button")
    transports = [b for b in buttons if _label(b) in PLAYER_ANCHORS and _available(b)]
    result = []
    for button in buttons:
        if _label(button) not in set().union(*LABELS.values()) or not _available(button):
            continue
        try:
            rect = button.rectangle()
            cy = (rect.top + rect.bottom) / 2
            for transport in transports:
                tr = transport.rectangle()
                row_y = (tr.top + tr.bottom) / 2
                tolerance = max(32, tr.bottom - tr.top)
                if abs(cy - row_y) > tolerance:
                    continue
                if any(
                    abs((anchor.rectangle().top + anchor.rectangle().bottom) / 2 - row_y)
                    <= tolerance
                    for anchor in anchors
                ):
                    result.append(button)
                    break
        except Exception:
            continue
    return result


def perform_rating(data, action):
    with uia_apartment(), ExitStack() as stack:
        window = find_player_window(data, stack)
        refresh = lambda: track_buttons(window, data)
        # Recheck the panel with the expected title before and after invocation.
        return apply_rating(
            refresh(), action, refresh=refresh, verify=lambda: _rating_button(refresh(), action)
        )


def change_rating(data, action):
    from media_control import query
    from media_desktop import request

    def verify():
        current = query(data["app_id"])
        if (current["title"], current["artist"]) != (data["title"], data["artist"]):
            raise MediaError(
                "Трек сменился во время проверки. Проверьте отметку и повторите для нового трека."
            )

    verify()
    result = request("rating", data, action)
    verify()
    return result


def inspect_player(data):
    with uia_apartment(), ExitStack() as stack:
        window = find_player_window(data, stack)
        buttons = track_buttons(window, data)
        candidates = [b for b in buttons if _label(b) in set().union(*LABELS.values())]
        lines = [f"Кнопок отметки найдено: {len(candidates)}"]
        for button in candidates:
            lines.append(
                f"{button.window_text()} · доступна: {button.is_enabled()} · видна: {button.is_visible()} · состояние: {_toggle(button)}"
            )
        if not candidates:
            lines.append(
                "Названия кнопок панели: " + "; ".join(b.window_text() for b in buttons[:40])
            )
        return lines
