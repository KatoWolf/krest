import logging
import os

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DISPLAY"), reason="GUI requires display")


@pytest.fixture
def app(tmp_path):
    import welcome_gui

    app = welcome_gui.ControlCenterApp(config_path=tmp_path / "config.json")
    errors = []
    app.report_callback_exception = lambda *args: errors.append(str(args))
    app.callback_errors = errors
    yield app
    app.runtime.close()
    app.integration.stop_all()
    logging.getLogger().removeHandler(app._log_handler)
    welcome_gui.indicator_controller.stop()
    app.destroy()


def test_home_is_voice_first_and_pages_are_lazy(app):
    app.update()
    assert app._built_pages == {"actions"}
    assert not app.manual_input.opened
    assert not app.command_entry.winfo_ismapped()
    cfg = app.config_mgr.snapshot()
    cfg["briefings"]["morning"] = "08:30"
    cfg["api_keys"]["telegram_owner_id"] = "123"
    app.config_mgr.save_config(cfg)
    app.api_vars["telegram_owner_id"].set("123")
    assert app.save_all_settings()
    assert app.config_mgr.snapshot()["briefings"]["morning"] == "08:30"
    app.apply_theme("Emerald Cyber")
    app.update()
    assert app._built_pages == {"actions"}
    app.switch_page("voice")
    app.update()
    assert app._built_pages == {"actions", "voice"}
    app._focus_command()
    assert app.manual_input.opened
    app.after(250, app.quit)
    app.mainloop()
    assert not app.callback_errors


def test_bot_pages_are_locked_before_build_and_relock_after_expiry(app, monkeypatch):
    for page in app.BOT_PAGES:
        app.switch_page(page)
        app.update()
        assert page not in app._built_pages
        assert not app.pages[page].winfo_ismapped()
        assert app._locked_pages[page].winfo_ismapped()
    monkeypatch.setattr(app.config_mgr, "is_premium", lambda: True)
    # Arbitrary opening order must not reset controls for other services.
    for page in ("steam", "scripts", "discord_account", "api"):
        app.switch_page(page)
        app.update()
        assert app.pages[page].winfo_ismapped()
    assert "ds_session.py" in app.service_labels and "steam_bot.py" in app.service_labels
    app.api_vars["discord_token"].set("local-test-value")
    monkeypatch.setattr(app.config_mgr, "is_premium", lambda: False)
    app.switch_page("discord_bot")
    app.update()
    assert not app.pages["discord_bot"].winfo_ismapped()
    assert app._locked_pages["discord_bot"].winfo_ismapped()
    assert app.api_vars["discord_token"].get() == "local-test-value"
    assert not app.callback_errors
