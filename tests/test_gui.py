import os
from pathlib import Path
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DISPLAY"), reason="GUI requires an X display")


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
