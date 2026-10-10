import logging
import os
import time
import tkinter as tk

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DISPLAY"), reason="GUI requires an X display")


@pytest.fixture
def app(tmp_path):
    from configuration import ConfigManager
    from welcome_gui import ControlCenterApp, indicator_controller

    cfg = ConfigManager(tmp_path / "config.json")
    data = cfg.snapshot()
    data["owner_name"] = "Тенно"
    data["profile"]["onboarding_completed"] = True
    cfg.save_config(data)
    ui = ControlCenterApp(config_path=cfg.filepath)
    errors = []
    ui.report_callback_exception = lambda *args: errors.append(str(args))
    ui.test_errors = errors
    yield ui
    ui.runtime.close()
    ui.integration.stop_all()
    indicator_controller.stop()
    ui.effects.close()
    logging.getLogger().removeHandler(ui._log_handler)
    ui.destroy()


def pump(app, predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.update()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "UI did not finish its background operation"


def test_all_game_pages_theme_drafts_and_settings_save(app):
    from warframe_pages import PAGE_NAMES

    app.switch_page("wf_goals")
    app.wf_var("goal_name").set("Форма")
    app.wf_var("goal_target").set("3")
    app.wf_call("add_goal", name="Форма", target="3")
    pump(app, lambda: len(app.wf_rows.get("goals", {})) == 1)
    for code, _ in PAGE_NAMES:
        app.switch_page(code)
        app.update()
        assert app.page_bodies[code].winfo_reqwidth() <= app.winfo_width()
    app.wf_vars["overlay_size"].set("44")
    app.wf_vars["poll_seconds"].set("180")
    app.wf_cycles["vallisCycle"].set(True)
    app.wf_texts["build_mods"].insert("1.0", "Vitality:10\nContinuity:5")
    app.effect_mode.set("Снег")
    app.effect_intensity.set("1")
    previous = app.effects
    app.apply_theme("Emerald Cyber")
    app.update()
    assert previous.closed and previous.job is None
    assert app.wf_var("goal_name").get() == "Форма"
    assert app.wf_texts["build_mods"].get("1.0", "end-1c") == "Vitality:10\nContinuity:5"
    assert app.wf_vars["overlay_size"].get() == "44" and app.effect_mode.get() == "Снег"
    assert app.save_all_settings()
    saved = app.config_mgr.snapshot()
    assert saved["warframe"]["overlay_size"] == 44 and saved["warframe"]["poll_seconds"] == 180
    assert saved["warframe"]["cycles"] == ["cetusCycle", "vallisCycle"]
    assert saved["visual_effects"] == {"mode": "snow", "intensity": 1, "pause_in_game": True}
    assert not app.test_errors


def test_market_selection_copies_exact_chosen_offer_without_sending(app):
    app.switch_page("wf_market")
    app.update()
    app._wf_order_item = {"name": "Vitality", "slug": "vitality"}
    app.wf_fill(
        "orders",
        [
            {
                "id": "order",
                "seller": "Test-Tenno",
                "price": 8,
                "rank": 5,
                "quantity": 2,
                "status": "ingame",
            }
        ],
        lambda r: (r["seller"], r["price"], r["status"], r["rank"]),
    )
    app.wf_trees["orders"].selection_set("order")
    app.wf_copy_order()
    assert (
        app.clipboard_get()
        == "/w Test-Tenno Hi! I want to buy: Vitality (rank 5) for 8 platinum. (warframe.market)"
    )
    assert "Перед отправкой" in app.wf_outputs["wf_market"]
    assert not app.test_errors


def test_visual_effects_stop_when_off_reduced_hidden_and_game_profile(app):
    app.switch_page("appearance")
    app.update()
    app.effect_mode.set("Кровавые потёки")
    app.update()
    assert app.effects.job is not None
    app.switch_page("security")
    app.switch_page("appearance")
    app.reduce_motion.set(True)
    app.effects.refresh()
    app.update()
    assert all(not scene.particles for scene in app.effects.scenes)
    app.reduce_motion.set(False)
    app.runtime.performance.set_mode("game")
    app.effects.refresh()
    assert all(not scene.particles for scene in app.effects.scenes)
    app.runtime.performance.set_mode("balanced")
    app.withdraw()
    app.effects.refresh()
    assert all(not scene.particles for scene in app.effects.scenes)
    app.effect_mode.set("Выключены")
    assert app.effects.job is None
    assert not app.test_errors


def test_cross_and_particles_use_bounded_canvas_objects():
    from game_overlay import draw_cross, COLORS
    from menu_effects import ParticleScene

    root = tk.Tk()
    canvas = tk.Canvas(root, width=350, height=160)
    canvas.pack()
    root.update()
    try:
        for state in COLORS:
            draw_cross(canvas, 52, state, level=1500, phase=1)
            assert len(canvas.find_all()) == 4
        canvas.delete("all")
        scene = ParticleScene(canvas)
        for mode in ("snow", "blood", "stars", "embers"):
            for _ in range(60):
                scene.draw(mode, 3)
            assert len(scene.particles) == 26 and len(canvas.find_withtag("particles")) <= 52
        scene.clear()
        assert not canvas.find_all()
    finally:
        root.destroy()


def test_table_selection_and_editing_survive_refresh(app):
    app.switch_page("wf_goals")
    service = app.runtime.warframe
    goal = service.action("add_goal", name="Цель", target=3)["row"]
    service.action("add_task", name="Задача", period="daily")
    app.wf_render(service.snapshot())
    app.wf_trees["goals"].selection_set(goal["id"])
    app.wf_load_record("goals")
    app.wf_var("goal_name").set("Изменено")
    app.wf_edit_goal()
    pump(app, lambda: service.store.snapshot()["goals"][0]["name"] == "Изменено")
    app.wf_render(service.snapshot())
    assert app.wf_trees["goals"].selection() == (goal["id"],)
    task = service.store.snapshot()["tasks"][0]
    service.action("complete_task", id=task["id"])
    app.wf_render(service.snapshot())
    app.wf_trees["tasks"].selection_set(task["id"])
    app.wf_selected_action("tasks", "complete_task", completed=False)
    pump(app, lambda: not service.store.snapshot()["tasks"][0]["done"])
    replacement = dict(goal, id="different", name="Изменено")
    app.wf_fill("goals", [replacement], lambda r: (r["name"], "0/3", "15 мин · приоритет 2"))
    assert app.wf_trees["goals"].get_children() == ("different",)
    assert not app.test_errors


def test_overlay_survives_lost_foreground_window_and_exits_with_game(tmp_path, monkeypatch):
    import game_overlay
    import game_capture
    import kernel_client
    from warframe_config import DEFAULT_WARFRAME

    calls = []
    states = []

    class Client:
        def __init__(self, path):
            pass

        def call(self, method):
            calls.append(method)
            return {
                "game": {"running": len(calls) < 3},
                "settings": dict(DEFAULT_WARFRAME, overlay_foreground_only=False),
                "voice": {"connected": True, "rms": 1000},
                "listening": True,
                "reduced": True,
            }

    monkeypatch.setattr(kernel_client, "KernelClient", Client)

    def no_foreground():
        raise ValueError("Window has closed")

    monkeypatch.setattr(game_capture, "foreground_game_rect", no_foreground)
    original = game_overlay.draw_cross

    def draw(canvas, size, state, *a, **kw):
        states.append(state)
        return original(canvas, size, state, *a, **kw)

    monkeypatch.setattr(game_overlay, "draw_cross", draw)
    assert game_overlay.run_overlay(tmp_path / "config.json") == 0
    assert "listening" in states and len(calls) >= 3


def test_effects_keep_animating_in_maximized_menu(app, monkeypatch):
    app.switch_page("appearance")
    app.update()
    app.effect_mode.set("Снег")
    app.update()
    # Xvfb cannot maximize with Windows' WM state. Exercise the actual renderer
    # with the exact state returned by Tk on a maximized Windows window.
    monkeypatch.setattr(app, "state", lambda: "zoomed")
    app.effects.refresh()
    scene = app.effects.scenes[0]
    assert scene.particles and scene.canvas.find_withtag("particles")
    before = [p["y"] for p in scene.particles]
    app.effects.last -= 0.1
    app.after_cancel(app.effects.job)
    app.effects.tick()
    assert before != [p["y"] for p in scene.particles]
    assert app.effects.job is not None
    assert "включён" in app.effect_status.get()
    app.effect_mode.set("Выключены")
    assert not scene.particles and app.effects.job is None
    assert not app.test_errors


def test_effect_status_explains_game_pause_and_resumes_immediately(app):
    app.switch_page("appearance")
    app.update()
    app.runtime.performance.set_mode("game")
    app.effect_mode.set("Тлеющие искры")
    assert "игровой профиль" in app.effect_status.get()
    assert not app.effects.scenes[0].particles
    app.effect_pause.set(False)
    assert app.effects.scenes[0].particles
    app.switch_page("security")
    app.reduce_motion.set(True)
    app.effects.refresh()
    assert "Уменьшить движение" in app.effect_status.get()
    assert not app.effects.scenes[0].particles
    app.reduce_motion.set(False)
    app.effects.refresh()
    assert app.effects.scenes[0].particles
    assert not app.test_errors


def test_native_overlay_failure_keeps_in_menu_animation_alive(app, monkeypatch):
    from unittest.mock import MagicMock
    import menu_effects

    app.update()
    app.effects._disable_overlay("test setup")
    native = MagicMock()
    native.winfo_viewable.return_value = False
    app.effects.window = native
    monkeypatch.setattr(app, "focus_displayof", lambda: app)

    def failed(window):
        raise OSError("Transparent overlay is unavailable")

    monkeypatch.setattr(menu_effects, "passthrough", failed)
    app.effect_mode.set("Снег")
    native.destroy.assert_called_once()
    assert app.effects.window is None
    assert app.effects.scenes[0].canvas.find_withtag("particles")
    assert app.effects.job is not None
    assert "Наложение по краям недоступно" in app.effect_status.get()
    assert not app.test_errors


def test_recycled_particles_stay_visible_and_at_borders():
    from menu_effects import ParticleScene

    root = tk.Tk()
    canvas = tk.Canvas(root, width=600, height=400, highlightthickness=0)
    canvas.pack()
    root.update()
    try:
        scene = ParticleScene(canvas)
        scene.random.seed(12)
        for mode in ("snow", "blood", "embers", "stars"):
            scene.draw(mode, 3, dt=0, border_only=True)
            assert all(0 <= p["y"] <= 400 for p in scene.particles)
            for _ in range(1800):  # Several complete screen traversals.
                scene.draw(mode, 3, dt=0.15, border_only=True)
            assert all(p["x"] <= 20 or p["x"] >= 580 for p in scene.particles)
            assert sum(0 <= p["y"] <= 400 for p in scene.particles) >= 20
            assert 26 <= len(canvas.find_withtag("particles")) <= 52
    finally:
        root.destroy()


def test_saved_effect_restores_on_next_menu_launch(tmp_path):
    from welcome_gui import ControlCenterApp, indicator_controller

    path = tmp_path / "config.json"
    for launch in range(2):
        ui = ControlCenterApp(config_path=path)
        try:
            # Keep onboarding/runtime stopped; reveal the menu for rendering checks.
            ui.onboarding.place_forget()
            ui.update()
            if launch == 0:
                ui.effect_mode.set("Кровавые потёки")
                ui.effect_intensity.set("3")
                ui.effect_pause.set(False)
                assert ui.save_all_settings()
            else:
                assert ui.effect_mode.get() == "Кровавые потёки"
                assert ui.effect_intensity.get() == "3"
                assert not ui.effect_pause.get()
                ui.effects.refresh()
                assert ui.effects.scenes[0].canvas.find_withtag("particles")
        finally:
            ui.runtime.close()
            ui.integration.stop_all()
            indicator_controller.stop()
            logging.getLogger().removeHandler(ui._log_handler)
            ui.destroy()
