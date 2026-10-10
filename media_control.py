"""Direct Windows media-session control. No global keyboard shortcuts."""

import asyncio
import importlib
import re
from datetime import timedelta


class MediaError(RuntimeError):
    pass


def app_name(app_id):
    for token, name in [
        ("yandexmusic", "Яндекс Музыка"),
        ("yandex.yandexmusic", "Яндекс Музыка"),
        ("spotify", "Spotify"),
        ("chrome", "Chrome"),
        ("msedge", "Edge"),
        ("firefox", "Firefox"),
        ("aimp", "AIMP"),
        ("vlc", "VLC"),
    ]:
        if re.sub(r"[^a-z0-9]", "", token) in re.sub(r"[^a-z0-9]", "", app_id.lower()):
            return name
    return app_id or "Плеер"


def _manager_type():
    try:
        return importlib.import_module(
            "winsdk.media.control"
        ).GlobalSystemMediaTransportControlsSessionManager
    except ImportError as exc:
        raise MediaError(
            "Для прямого управления музыкой установите requirements-windows.txt на Windows."
        ) from exc


def choose_session(sessions, current, preferred=""):
    if preferred:
        matches = [s for s in sessions if s.source_app_user_model_id == preferred]
        if len(matches) == 1:
            return matches[0]
        raise MediaError(
            "Выбранный плеер недоступен или открыл несколько сессий. Выберите источник во вкладке «Музыка»."
        )
    playing = [s for s in sessions if s.get_playback_info().playback_status == 4]
    if len(playing) == 1:
        return playing[0]
    candidates = playing or sessions
    if current:
        same = [
            s for s in candidates if s.source_app_user_model_id == current.source_app_user_model_id
        ]
        if len(same) == 1:
            return same[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise MediaError("Нет медиасессий. Запустите трек в плеере.")
    raise MediaError("Найдено несколько плееров. Выберите источник во вкладке «Музыка».")


async def _sessions():
    manager = await _manager_type().request_async()
    return list(manager.get_sessions()), manager.get_current_session()


async def _metadata(session):
    info = await session.try_get_media_properties_async()
    timeline = session.get_timeline_properties()
    playback = session.get_playback_info()
    position = timeline.position.total_seconds()
    start = timeline.start_time.total_seconds()
    end = timeline.end_time.total_seconds()
    return {
        "capabilities": {
            name: bool(getattr(playback.controls, flag, False))
            for name, flag in [
                ("Играть", "is_play_enabled"),
                ("Пауза", "is_pause_enabled"),
                ("Следующий", "is_next_enabled"),
                ("Предыдущий", "is_previous_enabled"),
                ("Перемотка", "is_playback_position_enabled"),
            ]
        },
        "app_id": session.source_app_user_model_id,
        "app_name": app_name(session.source_app_user_model_id),
        "title": info.title or "",
        "artist": info.artist or "",
        "album": info.album_title or "",
        "is_playing": playback.playback_status == 4,
        "position_seconds": max(0, position - start),
        "duration_seconds": max(0, end - start),
    }


async def _query(preferred="", all_sessions=False):
    sessions, current = await _sessions()
    if all_sessions:
        rows = []
        for session in sessions:
            try:
                rows.append(await _metadata(session))
            except Exception:
                continue  # One closing session must not hide the other players.
        return rows
    return await _metadata(choose_session(sessions, current, preferred))


async def _control(action, preferred="", seconds=10):
    sessions, current = await _sessions()
    session = choose_session(sessions, current, preferred)
    playback = session.get_playback_info()
    methods = {
        "play": ("is_play_enabled", "try_play_async"),
        "pause": ("is_pause_enabled", "try_pause_async"),
        "next": ("is_next_enabled", "try_skip_next_async"),
        "prev": ("is_previous_enabled", "try_skip_previous_async"),
        "play_pause": ("is_play_pause_toggle_enabled", "try_toggle_play_pause_async"),
    }
    if (action == "pause" and playback.playback_status == 5) or (
        action == "play" and playback.playback_status == 4
    ):
        return {
            "success": True,
            "message": "Уже на паузе." if action == "pause" else "Музыка уже играет.",
        }
    if action == "seek":
        if not getattr(playback.controls, "is_playback_position_enabled", False):
            raise MediaError("Этот плеер не разрешает перемотку через Windows.")
        timeline = session.get_timeline_properties()
        target = max(
            timeline.start_time,
            min(timeline.end_time, timeline.position + timedelta(seconds=seconds)),
        )
        if timeline.end_time <= timeline.start_time:
            raise MediaError("Плеер не сообщил длительность трека.")
        ok = await session.try_change_playback_position_async(
            int(target.total_seconds() * 10_000_000)
        )
    elif action in methods:
        flag, method = methods[action]
        if not getattr(playback.controls, flag, False):
            raise MediaError("Выбранный плеер не поддерживает это действие через Windows.")
        ok = await getattr(session, method)()
    else:
        raise MediaError("Неизвестное действие плеера.")
    if not ok:
        raise MediaError("Плеер отклонил команду. Проверьте его состояние.")
    return {
        "success": True,
        "message": {
            "play": "Продолжила воспроизведение.",
            "pause": "Поставила на паузу.",
            "next": "Включила следующий трек.",
            "prev": "Вернула предыдущий трек.",
            "seek": "Перемотала трек.",
            "play_pause": "Переключила воспроизведение.",
        }[action],
    }


def run(coroutine):
    async def bounded():
        return await asyncio.wait_for(coroutine, timeout=8)

    try:
        return asyncio.run(bounded())
    except TimeoutError as exc:
        raise MediaError("Плеер не ответил за восемь секунд.") from exc
    except MediaError:
        raise
    except Exception as exc:
        raise MediaError(f"Ошибка медиасессии: {exc}") from exc


def query(preferred="", all_sessions=False):
    return run(_query(preferred, all_sessions))


def control(action, preferred="", seconds=10):
    return run(_control(action, preferred, seconds))


def rating(action, preferred=""):
    from media_rating import change_rating

    data = query(preferred)
    if not data["title"]:
        raise MediaError("Плеер не сообщил название трека: отметку не меняю.")
    return change_rating(data, action)


def diagnostics(preferred=""):
    data = query(preferred)
    lines = [
        f"Источник: {data['app_name']} ({data['app_id']})",
        f"Трек: {data['artist']} — {data['title']}",
    ]
    lines.extend(
        name + ": " + ("доступно" if enabled else "плеер не разрешает")
        for name, enabled in data["capabilities"].items()
    )
    try:
        from media_rating import find_player_window, _label, _toggle, LABELS

        window = find_player_window(data)
        buttons = window.descendants(control_type="Button")
        candidates = [b for b in buttons if _label(b) in set().union(*LABELS.values())]
        lines.append(f"Кнопок отметки найдено: {len(candidates)}")
        for button in candidates:
            lines.append(
                f"{button.window_text()} · доступна: {button.is_enabled()} · видна: {button.is_visible()} · состояние: {_toggle(button)}"
            )
        if not candidates:
            lines.append(
                "Кнопка отметки не распознана. Доступные названия: "
                + "; ".join(b.window_text() for b in buttons[:40])
            )
    except Exception as exc:
        lines.append("Лайк: " + str(exc))
    return "\n".join(lines)
