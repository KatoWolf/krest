import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
import media_control as media
from media_rating import apply_rating, player_processes


def session(name="YandexMusic.exe", playing=4, position=50):
    obj = SimpleNamespace(source_app_user_model_id=name)
    obj.get_playback_info = lambda: SimpleNamespace(
        playback_status=playing,
        controls=SimpleNamespace(
            is_play_enabled=True,
            is_pause_enabled=True,
            is_next_enabled=True,
            is_previous_enabled=True,
            is_playback_position_enabled=True,
        ),
    )
    obj.get_timeline_properties = lambda: SimpleNamespace(
        start_time=timedelta(), end_time=timedelta(seconds=60), position=timedelta(seconds=position)
    )
    for method in (
        "try_play_async",
        "try_pause_async",
        "try_skip_next_async",
        "try_change_playback_position_async",
    ):
        setattr(obj, method, AsyncMock(return_value=True))
    obj.try_get_media_properties_async = AsyncMock(
        return_value=SimpleNamespace(title="Track", artist="Artist", album_title="Album")
    )
    return obj


def test_auto_selection_prefers_playing_over_paused_current():
    music, browser = session(), session("Chrome", 5)
    assert media.choose_session([browser, music], browser) is music
    assert media.choose_session([browser, music], music, "Chrome") is browser
    with pytest.raises(media.MediaError, match="Выбранный"):
        media.choose_session([music], music, "absent")


def test_ambiguous_players_require_selection():
    with pytest.raises(media.MediaError, match="несколько"):
        media.choose_session([session("A"), session("B")], None)
    with pytest.raises(media.MediaError, match="Нет медиасессий"):
        media.choose_session([], None)


def test_pause_is_idempotent_and_seek_is_direct_and_clamped(monkeypatch):
    paused = session(playing=5)
    monkeypatch.setattr(media, "_sessions", AsyncMock(return_value=([paused], paused)))
    assert media.control("pause")["success"]
    paused.try_pause_async.assert_not_awaited()
    media.control("play")
    paused.try_play_async.assert_awaited_once()
    media.control("seek", seconds=100)
    paused.try_change_playback_position_async.assert_awaited_once_with(600_000_000)
    media.control("seek", seconds=-100)
    assert paused.try_change_playback_position_async.call_args.args == (0,)


def test_rejected_action_does_not_fall_back_to_another_player(monkeypatch):
    selected = session()
    selected.try_pause_async.return_value = False
    other = session("Spotify")
    monkeypatch.setattr(media, "_sessions", AsyncMock(return_value=([selected, other], selected)))
    with pytest.raises(media.MediaError, match="отклонил"):
        media.control("pause")
    other.try_pause_async.assert_not_called()


def test_unsupported_controls_are_not_invoked(monkeypatch):
    selected = session()
    selected.get_playback_info = lambda: SimpleNamespace(
        playback_status=4, controls=SimpleNamespace(is_pause_enabled=False)
    )
    monkeypatch.setattr(media, "_sessions", AsyncMock(return_value=([selected], selected)))
    with pytest.raises(media.MediaError, match="не поддерживает"):
        media.control("pause")
    selected.try_pause_async.assert_not_called()


def test_metadata_records_paused_state(monkeypatch):
    selected = session(playing=5)
    monkeypatch.setattr(media, "_sessions", AsyncMock(return_value=([selected], selected)))
    data = media.query()
    assert data["title"] == "Track" and not data["is_playing"]
    assert data["duration_seconds"] == 60


class Button:
    def __init__(self, state=0):
        self.state = state
        self.calls = 0

    def window_text(self):
        return "Мне нравится"

    def is_enabled(self):
        return True

    def is_visible(self):
        return True

    def get_toggle_state(self):
        return self.state

    def toggle(self):
        self.calls += 1
        self.state = 1 - self.state


def test_diagnostics_reports_capabilities_without_pressing_like(monkeypatch):
    selected = session()
    monkeypatch.setattr(media, "_sessions", AsyncMock(return_value=([selected], selected)))
    button = Button()
    window = SimpleNamespace(descendants=lambda **kw: [button])
    monkeypatch.setattr("media_rating.find_player_window", lambda data: window)
    report = media.diagnostics()
    assert "Яндекс" in report and "Кнопок отметки найдено: 1" in report
    assert "доступно" in report
    assert button.calls == 0
    selected.try_play_async.assert_not_awaited()
    selected.try_pause_async.assert_not_awaited()


def test_rating_is_confirmed_and_idempotent(monkeypatch):
    monkeypatch.setattr("media_rating.time.sleep", lambda _: None)
    button = Button()
    assert apply_rating([button], "like")["success"]
    assert apply_rating([button], "like")["success"]
    assert button.calls == 1
    assert apply_rating([button], "unlike")["success"]
    assert button.calls == 2 and button.state == 0


def test_ambiguous_rating_never_clicks_and_browser_is_unsupported():
    buttons = [Button(), Button()]
    with pytest.raises(media.MediaError, match="однозначно"):
        apply_rating(buttons, "like")
    assert not any(b.calls for b in buttons)
    with pytest.raises(media.MediaError, match="источника"):
        player_processes("Chrome")
    assert "yandexmusic.exe" in player_processes("YandexMusic.exe")


def test_unconfirmed_rating_reports_failure(monkeypatch):
    monkeypatch.setattr("media_rating.time.sleep", lambda _: None)
    button = Button()
    button.toggle = lambda: None
    with pytest.raises(media.MediaError, match="не подтвердил"):
        apply_rating([button], "like")


def test_music_dispatch_parses_spoken_percentages(tmp_path, monkeypatch):
    from app_runtime import AssistantRuntime
    from configuration import ConfigManager
    from skills import media as skills

    app = AssistantRuntime(ConfigManager(tmp_path / "config.json").snapshot, data_dir=tmp_path)
    monkeypatch.setattr(app.manager, "availability", lambda entry: "Доступен")
    volume = MagicMock(return_value="Готово")
    monkeypatch.setattr(skills, "_volume", volume)
    try:
        assert app.execute("громче на двадцать").success
        volume.assert_called_with("relative", 20)
        assert app.execute("громкость на двадцать пять").success
        volume.assert_called_with("set", 25)
        assert not app.execute("громкость на двести").success
        assert app.manager.match("пауза")[0].func.__name__ == "pause_music"
        assert app.manager.match("продолжи")[0].func.__name__ == "play_music"
    finally:
        app.close()
