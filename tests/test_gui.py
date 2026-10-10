import os
from pathlib import Path
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DISPLAY"), reason="GUI requires an X display")


def test_workflow_pages_save_steps_weekdays_and_notification_preferences(tmp_path, monkeypatch):
    import welcome_gui

    original = welcome_gui.ConfigManager
    monkeypatch.setattr(welcome_gui, "ConfigManager", lambda: original(tmp_path / "config.json"))
    app = welcome_gui.ControlCenterApp()
    errors = []
    app.report_callback_exception = lambda *args: errors.append(str(args))
    try:
        app._new_scenario()
        app.scenario_name.set("Мой режим")
        app.step_action.set("Команда")
        app.step_value.set("время")
        app._add_scenario_step()
        app.step_value.set("статус")
        app._add_scenario_step()
        app.scenario_tree.selection_set("1")
        app._move_step(-1)
        app._save_scenario()
        assert (
            original(tmp_path / "config.json").snapshot()["scenarios"]["Мой режим"][0]["value"]
            == "статус"
        )
        app.step_value.set("таймер пять минут")
        app._add_scenario_step()
        app.apply_theme("Emerald Cyber")
        assert app.scenario_steps[-1]["value"] == "таймер пять минут"
        app.reminder_label.set("Выходной")
        for i, var in enumerate(app.weekdays):
            var.set(i in (5, 6))
        app._add_daily_reminder()
        assert app.runtime.state.daily_tasks[0]["weekdays"] == [5, 6]
        app.runtime.notification_mode = "silent"
        app.notice_mode_var.set("Только важные")
        app._apply_notices()
        assert app.runtime.notification_mode is None
        assert app.config_mgr.snapshot()["notification_settings"]["mode"] == "important"
        ident, _ = app.runtime.center.add("Проверка", "Система")
        app._refresh_inbox()
        app.inbox_tree.selection_set(str(ident))
        app._notice_details()
        assert app.notice_details.get() == "Проверка"
        app._read_notice()
        assert not app.runtime.center.list(unread=True)
        for code in ("scenarios", "inbox", "maintenance", "timers"):
            app.switch_page(code)
            app._update_workflows()
            app.update()
        assert not errors
    finally:
        app.runtime.close()
        app.integration.stop_all()
        import logging

        logging.getLogger().removeHandler(app._log_handler)
        welcome_gui.indicator_controller.stop()
        app.destroy()


def test_gui_pages_settings_and_commands(tmp_path, monkeypatch):
    import welcome_gui

    monkeypatch.setattr(welcome_gui, "TRAY_SUPPORTED", False)
    original = welcome_gui.ConfigManager
    monkeypatch.setattr(
        welcome_gui, "ConfigManager", lambda: original(str(tmp_path / "config.json"))
    )
    monkeypatch.setattr(welcome_gui.messagebox, "showinfo", lambda *args: None)
    errors = []
    app = welcome_gui.ControlCenterApp()
    app.report_callback_exception = lambda *args: errors.append(str(args))
    try:
        assert app.onboarding is not None
        assert not app.owner_var.get()
        assert not app.runtime._threads
        app._complete_welcome()
        assert app.onboarding is not None
        app.owner_var.set("Тестовый пользователь")
        app._complete_welcome()
        assert app.onboarding is None and app.runtime._threads
        for code in app.pages:
            app.switch_page(code)
            app.update()
        app.command_entry.insert(0, "статус")
        app.execute_command()
        app.after(500, app.quit)
        app.mainloop()
        assert "Python" in app.command_result.cget("text")
        assert str(app.api_entries["krest_bot_token"].cget("state")) == "normal"
        app.entry_owner_name.delete(0, "end")
        app.entry_owner_name.insert(0, "Новый пользователь")
        app.save_all_settings()
        assert original(str(tmp_path / "config.json")).config["owner_name"] == "Новый пользователь"
        app.settings_vars["weather_city"].set("Кызылорда")
        app.api_vars["telegram_owner_id"].set("123456")
        app.apply_theme("Emerald Cyber")
        app.update()
        app.apply_theme("Amethyst Modern")
        assert app.settings_vars["weather_city"].get() == "Кызылорда"
        assert app.api_vars["telegram_owner_id"].get() == "123456"
        app.switch_page("skills")
        app.skill_search.set("таймер")
        assert app.skill_tree.get_children()
        assert len(app.skill_tree.get_children()) < len(app.runtime.manager.catalog())
        app.command_entry.delete(0, "end")
        app.command_entry.insert(0, "время")
        app._save_favorite()
        assert app.command_book.favorites == ["время"]
        app.command_lists["favorites"].selection_set("0")
        before = app.runtime.requests.qsize()
        app._use_saved_command("favorites")
        assert app.command_entry.get() == "время"
        assert app.runtime.requests.qsize() == before  # Selection does not execute a command.
        app.alias_name.set("проверка")
        app.alias_target.set("статус")
        app._add_alias()
        app.save_all_settings()
        assert app.config_mgr.snapshot()["command_aliases"] == {"проверка": "статус"}
        app.reminder_label.set("Зарядка")
        app.daily_time.set("09:15")
        app._add_daily_reminder()
        assert app.runtime.state.daily_tasks[0]["label"] == "Зарядка"
        app.reminder_label.set("Черновик напоминания")
        app.apply_theme("Emerald Cyber")
        assert app.reminder_label.get() == "Черновик напоминания"
        assert app.daily_time.get() == "09:15"
        assert app.alias_tree.exists("проверка")
        item = app.runtime.state.add_timer(1, "Проверка списка")
        app.runtime.state.take_due(item["end_time"])
        app._update_reminders()
        app.reminder_tree.selection_set(item["id"])
        app._handle_reminder(True)
        assert not app.runtime.state.reminders()
        assert app.runtime.state.active_timers[0]["label"] == "Проверка списка"
        app._open_service_log("ds_session.py")
        assert app.current_page == "logs" and app.log_source.get() == "ds_session.py"
        assert {"steam", "discord_bot", "discord_account", "scripts"} <= app.pages.keys()
        from storage import atomic_write_json

        atomic_write_json(
            tmp_path / "steam_state.json",
            {
                "account": "",
                "games": [{"appid": "730", "name": "Counter-Strike 2", "installed": True}],
                "inventories": {
                    "730:2": {
                        "updated": 1700000000,
                        "summary": {"total": 1, "tradable": 0, "locked": 1, "unknown": 0},
                        "items": [
                            {
                                "assetid": "1",
                                "name": "Item",
                                "amount": 1,
                                "tradable": False,
                                "unlock_at": None,
                            }
                        ],
                    }
                },
            },
        )
        app._update_steam(force=True)
        assert app.steam_games.exists("730")
        assert len(app.steam_items.get_children()) == 1
        opened = []
        import service_pages

        monkeypatch.setattr(service_pages.webbrowser, "open", opened.append)
        app.steam_games.selection_set("730")
        app._launch_steam_selection()
        assert opened == ["steam://rungameid/730"]
        app.steam_search.set("nonexistent")
        assert not app.steam_games.get_children()
        app.service_autostart["steam_bot.py"].set(True)
        app.save_all_settings()
        assert app.config_mgr.snapshot()["services"]["steam_bot.py"]["autostart"]
        assert not app.integration.processes
        assert "voice" in app.pages
        app.switch_page("voice")
        app._show_recognition(
            {
                "heard": "Крест, скока время",
                "command": "скока время",
                "confidence": 0.75,
                "status": "Не выполнено",
            }
        )
        app.correction_var.set("который час")
        before = app.runtime.requests.qsize()
        app._save_speech_correction()
        assert app.config_mgr.snapshot()["command_aliases"]["скока время"] == "время"
        assert app.runtime.requests.qsize() == before
        assert app.runtime.execute("крест скока время", "voice", confidence=0.95).success
        app.pron_word.set("Discord")
        app.pron_reading.set("диск+орд")
        app._add_pronunciation()
        app.settings_vars["speech_pause_ms"].set("900")
        app.apply_theme("Amethyst Modern")
        app.update()
        assert app.pron_tree.exists("Discord")
        assert "скока время" in app.heard_var.get()
        assert app.settings_vars["speech_pause_ms"].get() == "900"
        app.microphone_test.noise_threshold = 275
        app._apply_mic_threshold()
        assert app.save_all_settings()
        saved = original(str(tmp_path / "config.json")).snapshot()
        assert saved["app_settings"]["speech_threshold"] == 275
        assert saved["app_settings"]["speech_pause_ms"] == 900
        assert saved["pronunciations"] == {"Discord": "диск+орд"}
        # Probe failures must clear the busy flag and update the page, not leave it hanging.
        monkeypatch.setattr(app, "_background", lambda work: work())

        def fail_probe(*args, **kwargs):
            raise RuntimeError("Микрофон недоступен")

        monkeypatch.setattr(app.microphone_test, "record", fail_probe)
        with pytest.raises(RuntimeError, match="Микрофон недоступен"):
            app._microphone_action("record")
        app.after(200, app.quit)
        app.mainloop()
        assert not app.audio_test_busy
        assert "не удалась" in app.mic_test_status.get()
        app.after(1700, app.quit)
        app.mainloop()
        assert not errors, errors
        assert not app.integration.processes
    finally:
        app.integration.stop_all()
        app.runtime.close()
        import logging

        logging.getLogger().removeHandler(app._log_handler)
        welcome_gui.indicator_controller.stop()
        app.destroy()


def test_profile_premium_themes_and_music_selection(tmp_path, monkeypatch, issuer):
    import welcome_gui
    import media_control
    from unittest.mock import MagicMock

    original = welcome_gui.ConfigManager
    monkeypatch.setattr(welcome_gui, "ConfigManager", lambda: original(tmp_path / "config.json"))
    app = welcome_gui.ControlCenterApp()
    try:
        app.owner_var.set("Алексей")
        app.personality_var.set("Краткая")
        app._complete_welcome()
        assert app.config_mgr.snapshot()["profile"] == {
            "onboarding_completed": True,
            "personality": "concise",
        }
        app.apply_theme("Arctic Light")
        assert app.theme_var.get() == "Amethyst Modern"
        assert app.current_page == "license"
        app.license_code_var.set("invalid")
        app._activate_premium()
        assert not app.config_mgr.is_premium()
        assert "подписи" in app.license_feedback_var.get()
        app.settings_vars["weather_city"].set("Казань")  # Draft survives activation.
        app.license_code_var.set(issuer[2])
        app._activate_premium()
        assert app.config_mgr.is_premium()
        assert app.settings_vars["weather_city"].get() == "Казань"
        assert app.config_mgr.snapshot()["app_settings"]["weather_city"] != "Казань"
        for name in ("Arctic Light", "Ocean Blue", "Copper Night", "Rose Quartz"):
            app.apply_theme(name)
            app.update()
            assert app.theme_var.get() == name
        assert app.save_all_settings()
        monkeypatch.setattr(
            media_control,
            "query",
            lambda **kw: [
                {
                    "app_id": "YandexMusic.exe",
                    "app_name": "Яндекс Музыка",
                    "artist": "Тест",
                    "title": "Трек",
                    "is_playing": True,
                }
            ],
        )
        app.switch_page("music")
        app.after(200, app.quit)
        app.mainloop()
        app.music_tree.selection_set("0")
        app._select_music()
        assert app.config_mgr.snapshot()["media"]["preferred_app"] == "YandexMusic.exe"
        app._auto_music()
        assert app.config_mgr.snapshot()["media"]["preferred_app"] == ""
        app._deactivate_premium()
        assert not app.config_mgr.is_premium()
        assert app.theme_var.get() == "Amethyst Modern"
        assert not app.license_code_var.get()
        assert original(tmp_path / "config.json").snapshot()["profile"]["onboarding_completed"]
    finally:
        app.runtime.close()
        app.integration.stop_all()
        import logging

        logging.getLogger().removeHandler(app._log_handler)
        welcome_gui.indicator_controller.stop()
        app.destroy()
