"""Regressions for settings search, draft previews and retired decorations."""

import logging
import os
import time

import pytest


def test_legacy_particles_are_disabled_without_changing_game_overlay(tmp_path):
    from configuration import ConfigManager

    config = ConfigManager(tmp_path / "config.json")
    data = config.snapshot()
    data["visual_effects"] = {"mode": "blood", "intensity": 3, "pause_in_game": False}
    data["warframe"]["overlay_size"] = 44
    config.save_config(data)
    saved = ConfigManager(config.filepath).snapshot()
    assert saved["visual_effects"]["mode"] == "off"
    assert saved["warframe"]["overlay_size"] == 44


@pytest.fixture
def app(tmp_path):
    if not os.getenv("DISPLAY"):
        pytest.skip("GUI requires an X display")
    from welcome_gui import ControlCenterApp, indicator_controller

    ui = ControlCenterApp(config_path=tmp_path / "config.json")
    errors = []
    ui.report_callback_exception = lambda *args: errors.append(args)
    ui.test_errors = errors
    ui.onboarding.place_forget()
    ui.update()
    yield ui
    ui.runtime.close()
    ui.integration.stop_all()
    indicator_controller.stop()
    logging.getLogger().removeHandler(ui._log_handler)
    ui.tk.call("tk", "scaling", ui._base_scaling)
    ui.destroy()


def test_settings_search_reveals_hidden_fields_and_preserves_drafts(app):
    from ui_design import Disclosure

    app.switch_page("settings")
    app.owner_var.set("Алекс")
    app.settings_vars["weather_city"].set("Казань")
    app.settings_search.set("микрофон")
    app.update()
    visible = [w for w in app._settings_cards if w.winfo_manager()]
    assert visible

    def disclosures(widget):
        for child in widget.winfo_children():
            if isinstance(child, Disclosure):
                yield child
            yield from disclosures(child)

    assert any(
        d.opened for w in visible for d in disclosures(w) if "микрофон" in d.title.casefold()
    )
    app.settings_search.set("несуществующийпараметр")
    assert not any(w.winfo_manager() for w in app._settings_cards)
    assert "Ничего" in app.settings_results.cget("text")
    app.settings_search.set("")
    assert all(w.winfo_manager() for w in app._settings_cards)
    assert app.owner_var.get() == "Алекс"
    assert app.settings_vars["weather_city"].get() == "Казань"
    assert not app.test_errors


def test_preview_is_live_and_does_not_save_or_require_valid_other_fields(app):
    from product_pages import PERSONALITIES

    app.switch_page("settings")
    app.settings_vars["voice_volume"].set("не число")
    app.owner_var.set("Маша")
    first = app.personality_preview.get()
    app.personality_var.set(
        PERSONALITIES["tactical"]
        if "tactical" in PERSONALITIES
        else list(PERSONALITIES.values())[-1]
    )
    second = app.personality_preview.get()
    assert first and second and first != second
    app.preview_situation.set("Поддержка")
    assert app.personality_preview.get() != second
    assert app.config_mgr.snapshot()["owner_name"] != "Маша"
    assert not app.test_errors


def test_rapid_navigation_theme_change_cancels_motion_and_keeps_search(app):
    app.switch_page("settings")
    app.settings_search.set("город")
    for _ in range(4):
        app.switch_page("appearance")
        app.switch_page("settings")
    app.apply_theme("Emerald Cyber")
    app.update()
    assert app.settings_search.get() == "город"
    assert not hasattr(app, "effects") and not hasattr(app, "effects_header")
    until = time.monotonic() + 0.3
    while time.monotonic() < until:
        app.update()
        time.sleep(0.01)
    assert app._transition_job is None
    assert not app.transition_bar.find_all()
    assert not app.test_errors


def test_skill_cache_preserves_selection_and_invalidates_for_settings_and_catalog(app, monkeypatch):
    app.switch_page("skills")
    ident = app.skill_tree.get_children()[0]
    app.skill_tree.selection_set(ident)
    app.switch_page("actions")
    app.switch_page("skills")
    assert app.skill_tree.selection() == (ident,)
    entry = app.catalog[ident]
    draft = app.config_mgr.snapshot()
    draft["skills"][entry.module] = False
    app.config_mgr.save_config(draft)
    app.refresh_skills()
    assert app.skill_tree.item(ident, "values")[2] == "Выключен в настройках"
    monkeypatch.setattr(entry, "description", "Новая проверочная команда")
    app.refresh_skills()
    assert app.skill_tree.item(ident, "values")[1] == "Новая проверочная команда"
    app.skill_search.set("Новая проверочная")
    assert app.skill_tree.get_children() == (ident,)
    assert not app.test_errors


def test_compact_large_text_navigation_reveals_focused_bottom_section(app):
    app.tk.call("tk", "scaling", app._base_scaling * 1.5)
    app.apply_theme("Emerald Cyber")
    app.geometry("1020x700")
    # Force a real overflow independently of the display's font backend.
    # Some minimal Tk builds expose only bitmap fonts with zero-width Cyrillic.
    for nav_button in app.group_buttons.values():
        nav_button.configure(padding=(16, 30))
    app.update()
    assert app.nav_frame.winfo_height() > app.nav_canvas.winfo_height()
    assert app.nav_scrollbar.winfo_manager() == "pack"
    button = app.group_buttons["settings"]
    app._reveal_nav_button(button)
    app.update()
    top = app.nav_canvas.canvasy(0)
    assert button.winfo_y() >= top - 1
    assert button.winfo_y() + button.winfo_height() <= top + app.nav_canvas.winfo_height() + 1
    app.switch_page("appearance")
    app.update()
    assert (
        app.page_bodies["appearance"].winfo_reqwidth() <= app.canvases["appearance"].winfo_width()
    )
    assert not app.test_errors


@pytest.mark.parametrize("scale", [100, 150])
def test_search_scrolls_to_actual_field_and_keeps_search_focus(app, scale):
    from tkinter import ttk

    app.tk.call("tk", "scaling", app._base_scaling * scale / 100)
    app.apply_theme("Emerald Cyber")
    app.geometry("1020x700")
    app.switch_page("settings")
    app.settings_vars["weather_city"].set("Томск")
    app.update()
    app.settings_search_entry.focus_force()

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    for query, key in (
        ("микрофон", "microphone"),
        ("silero", "silero_path"),
        ("город", "weather_city"),
    ):
        app.settings_search.set(query)
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            app.update()
            time.sleep(0.005)
        field = next(
            w
            for w in descendants(app.page_bodies["settings"])
            if isinstance(w, ttk.Entry)
            and str(w.cget("textvariable")) == str(app.settings_vars[key])
        )
        canvas = app.canvases["settings"]
        assert canvas.winfo_rooty() <= field.winfo_rooty()
        assert (
            field.winfo_rooty() + field.winfo_height()
            <= canvas.winfo_rooty() + canvas.winfo_height()
        )
        assert app.focus_get() is app.settings_search_entry
        assert app.settings_search_entry.winfo_viewable()
        assert app._settings_scroll_job is None
    assert app.settings_vars["weather_city"].get() == "Томск"
    for query in ("м", "мик", "микрофон", "silero", "город"):
        app.settings_search.set(query)
    old_job = app._settings_scroll_job
    app.apply_theme("Amethyst Modern")
    assert old_job not in app.tk.call("after", "info")
    deadline = time.monotonic() + 0.1
    while time.monotonic() < deadline:
        app.update()
        time.sleep(0.005)
    assert app._settings_scroll_job is None
    assert not app.test_errors


def test_large_text_headers_tabs_and_voice_security_controls_fit(app, monkeypatch):
    from tkinter import ttk
    from themes import THEMES

    app.tk.call("tk", "scaling", app._base_scaling * 1.5)
    app.geometry("1020x700")
    monkeypatch.setattr(app.config_mgr, "is_premium", lambda: True)

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    for theme in THEMES:
        app.apply_theme(theme)
        for code in ("bot_status", "voice", "security"):
            app.switch_page(code)
            app.update()
            title = app.header_title_widget
            assert title.winfo_width() >= title.winfo_reqwidth(), (theme, code, "title")
            for button in app.subnav.winfo_children():
                assert button.winfo_width() >= button.winfo_reqwidth(), (
                    theme,
                    code,
                    button.cget("text"),
                )
            canvas = app.canvases[code]
            left, right = canvas.winfo_rootx(), canvas.winfo_rootx() + canvas.winfo_width()
            for widget in descendants(app.page_bodies[code]):
                if isinstance(widget, (ttk.Button, ttk.Checkbutton)) and widget.winfo_ismapped():
                    assert left <= widget.winfo_rootx(), (theme, code, widget.cget("text"))
                    assert widget.winfo_rootx() + widget.winfo_width() <= right, (
                        theme,
                        code,
                        widget.cget("text"),
                    )
                    assert widget.winfo_width() >= widget.winfo_reqwidth(), (
                        theme,
                        code,
                        widget.cget("text"),
                    )
    assert not app.test_errors
