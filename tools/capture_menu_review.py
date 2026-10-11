"""Capture the real Tk menu with an isolated offline profile (requires DISPLAY).

Run: DISPLAY=:99 .venv/bin/python tools/capture_menu_review.py --output docs/menu-review
No bot, model, microphone, game or network connection is started.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from PIL import ImageGrab
    from welcome_gui import ControlCenterApp, indicator_controller

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/menu-review"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    metrics = {
        "environment": "Linux / Xvfb; Python "
        + sys.version.split()[0]
        + "; offline profile; actual Tk widgets",
        "pages": {},
    }
    errors = []
    with (
        tempfile.TemporaryDirectory(prefix="krest-menu-") as temp,
        patch("app_runtime.AssistantRuntime.start"),
        patch("welcome_gui.ControlCenterApp.wf_refresh"),
        patch("welcome_gui.ControlCenterApp._refresh_music"),
    ):
        start = time.perf_counter()
        app = ControlCenterApp(config_path=Path(temp) / "config.json")
        app.onboarding.place_forget()
        app.report_callback_exception = lambda *e: errors.append(str(e))
        app.geometry("1180x820+0+0")
        app.update()
        metrics["startup_ms"] = round((time.perf_counter() - start) * 1000, 2)

        def capture(name):
            # X11 expose events are delivered after geometry idle tasks.
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                app.update()
                time.sleep(0.005)
            ImageGrab.grab(
                bbox=(
                    app.winfo_rootx(),
                    app.winfo_rooty(),
                    app.winfo_rootx() + app.winfo_width(),
                    app.winfo_rooty() + app.winfo_height(),
                )
            ).save(args.output / f"{name}.png")

        try:
            # Unlock presentation only in this temporary process, without touching licenses.
            with patch.object(app.config_mgr, "is_premium", return_value=True):
                for code in (
                    "actions",
                    "settings",
                    "appearance",
                    "bot_status",
                    "voice",
                    "skills",
                    "wf_home",
                ):
                    before = time.perf_counter()
                    app.switch_page(code)
                    app.update()
                    cold = (time.perf_counter() - before) * 1000
                    metrics["pages"][code] = {"first_ms": round(cold, 2), "repeat_ms": []}
                    capture(code)
                for code, timings in metrics["pages"].items():
                    for _ in range(5):
                        app.switch_page("settings" if code == "actions" else "actions")
                        app.update_idletasks()
                        before = time.perf_counter()
                        app.switch_page(code)
                        app.update_idletasks()
                        timings["repeat_ms"].append(round((time.perf_counter() - before) * 1000, 2))
                # Render explicit local fixtures, never claim a live account connection.
                app.switch_page("bot_status")
                original_details = app.integration.details
                for state, detail in (
                    ("error", {"running": False, "health": "error", "error": "Тестовый пример: авторизация отклонена. Проверьте настройки доступа."}),
                    ("connecting", {"running": True, "health": "starting", "operation": "restart", "pid": 123, "uptime": 0}),
                ):
                    with patch.object(
                        app.integration,
                        "details",
                        side_effect=lambda name, d=detail: {**original_details(name), **d},
                    ):
                        app._update_service_views(refresh=False)
                        capture("bots-" + state + "-fixture")
                app._update_service_views(refresh=False)
                app.switch_page("settings")
                app.settings_search.set("профиль")
                app.owner_var.set("Алекс")
                app.preview_situation.set("Поддержка")
                capture("personality")
                app.settings_search.set("микрофон")
                capture("settings-search")
                app.settings_search.set("несуществующий параметр")
                capture("settings-empty")
                app.settings_search.set("")
                app.apply_theme("Emerald Cyber")
                app.switch_page("appearance")
                capture("themes-emerald")
                app.geometry("1020x700+0+0")
                app.switch_page("actions")
                capture("compact-home")
                for code in ("settings", "bot_status", "appearance"):
                    app.switch_page(code)
                    capture("compact-" + code)
                for index in range(20):
                    app.switch_page("settings" if index % 2 else "appearance")
                    if index % 5 == 0:
                        app.apply_theme("Amethyst Modern" if index % 2 else "Emerald Cyber")
                    app.update()
                deadline = time.monotonic() + 0.35
                latencies = []
                while time.monotonic() < deadline:
                    began = time.perf_counter()
                    app.update()
                    latencies.append((time.perf_counter() - began) * 1000)
                    time.sleep(0.01)
                metrics["stress"] = {
                    "transitions": 20,
                    "theme_changes": 4,
                    "remaining_transition_job": app._transition_job,
                    "remaining_canvas_items": len(app.transition_bar.find_all()),
                    "max_update_ms": round(max(latencies), 2),
                }
                # Match startup accessibility scale. Rebuild at compact window size.
                app.tk.call("tk", "scaling", app._base_scaling * 1.5)
                app.apply_theme("Amethyst Modern")
                metrics["large_scale"] = {"scale_percent": 150, "window": "1020x700", "pages": {}}
                for code in ("settings", "bot_status", "appearance"):
                    app.switch_page(code)
                    app.update()
                    capture("large-text-" + code)
                    metrics["large_scale"]["pages"][code] = {
                        "requested_width": app.page_bodies[code].winfo_reqwidth(),
                        "viewport_width": app.canvases[code].winfo_width(),
                        "save_button_visible": bool(app.save_button.winfo_viewable()),
                    }
            metrics["callback_errors"] = errors
            (args.output / "performance.json").write_text(
                json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            if errors:
                raise RuntimeError("Tk callback errors: " + str(errors))
        finally:
            app.runtime.close()
            app.integration.stop_all()
            indicator_controller.stop()
            logging.getLogger().removeHandler(app._log_handler)
            app.destroy()
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
