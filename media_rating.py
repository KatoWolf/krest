"""Conservative desktop UI Automation adapter for track ratings.

A like is not part of SMTC. Only an unambiguous button in a verified player
window is invoked. Browser tabs and global key combinations are never used.
"""

import time
import re
import psutil
from media_control import MediaError

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
    },
    "unlike": {
        "убрать из любимого",
        "снять лайк",
        "убрать лайк",
        "удалить из любимого",
        "удалить из избранного",
        "remove from your liked songs",
        "unlike",
    },
    "dislike": {"не нравится", "дизлайк", "dislike"},
}


def player_processes(app_id):
    value = re.sub(r"[^a-z0-9]", "", app_id.casefold())
    if "yandexmusic" in value or "yandex.yandexmusic" in value:
        return {"yandexmusic.exe", "yandex music.exe", "яндекс музыка.exe"}
    if "spotify" in value:
        return {"spotify.exe"}
    raise MediaError(
        "Для этого источника нет прямого управления отметками. Поддержаны доступные кнопки настольных Яндекс Музыки и Spotify; браузерные вкладки пока не подключены."
    )


def _label(button):
    return button.window_text().strip().casefold()


def _toggle(button):
    try:
        return button.get_toggle_state()
    except Exception:
        return None


def apply_rating(buttons, action):
    if action not in LABELS:
        raise MediaError("Неизвестная отметка трека.")
    opposite = "unlike" if action == "like" else "like"
    accepted = LABELS[action] | (LABELS[opposite] if action != "dislike" else set())
    matches = [b for b in buttons if _label(b) in accepted and b.is_enabled() and b.is_visible()]
    if len(matches) != 1:
        raise MediaError(
            "Не удалось однозначно найти кнопку текущего трека. Откройте экран плеера; в этой версии приложения кнопка может быть недоступна автоматизации."
        )
    button = matches[0]
    before, toggle = _label(button), _toggle(button)
    desired = 0 if action == "unlike" else 1
    already = (
        toggle == desired
        if toggle is not None
        else (action != "dislike" and before in LABELS[opposite])
    )
    if already:
        return {"success": True, "message": "Нужная отметка уже установлена."}
    if toggle is not None:
        button.toggle()
    else:
        button.invoke()
    for _ in range(8):
        time.sleep(0.15)
        if _toggle(button) == desired or (
            action != "dislike" and _label(button) in LABELS[opposite]
        ):
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


def find_player_window(data):
    names = player_processes(data["app_id"])
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise MediaError("Для кнопок плеера откройте Музыка → Подключить управление музыкой. Крест установит компоненты сама.") from exc
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        if (proc.info["name"] or "").casefold() in names:
            pids.add(proc.info["pid"])
    windows = []
    for window in Desktop(backend="uia").windows():
        if window.process_id() not in pids:
            continue
        # Confirm the track in this window; never blindly click a background app.
        texts = [
            window.window_text(),
            *[x.window_text() for x in window.descendants(control_type="Text")],
        ]
        if data["title"].casefold() in [x.strip().casefold() for x in texts]:
            windows.append(window)
    if len(windows) != 1:
        raise MediaError(
            "Откройте экран текущего трека в настольном плеере. Не удалось подтвердить окно и название трека."
        )
    return windows[0]


def change_rating(data, action):
    window = find_player_window(data)
    from media_control import query

    current = query(data["app_id"])
    if (current["title"], current["artist"]) != (data["title"], data["artist"]):
        raise MediaError("Трек сменился во время проверки. Повторите команду для нового трека.")
    return apply_rating(window.descendants(control_type="Button"), action)
