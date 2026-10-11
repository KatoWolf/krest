"""Exercise Tk navigation and theme changes offline; never start real services.

DISPLAY=:99 python tools/soak_menu.py --cycles 120 --output menu-soak.json
"""

import argparse
import gc
import json
import logging
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import psutil
    from welcome_gui import ControlCenterApp, indicator_controller

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.cycles <= 2000:
        parser.error("Use 10–2000 cycles")
    process = psutil.Process()
    errors, durations, samples = [], [], []
    pages = ("actions", "settings", "appearance", "skills", "bot_status")
    with (
        tempfile.TemporaryDirectory(prefix="krest-menu-soak-") as folder,
        patch("app_runtime.AssistantRuntime.start"),
        patch("welcome_gui.ControlCenterApp.wf_refresh"),
        patch("welcome_gui.ControlCenterApp._refresh_music"),
    ):
        ui = ControlCenterApp(config_path=Path(folder) / "config.json")
        ui.onboarding.place_forget()
        ui.geometry("1180x820+0+0")
        ui.report_callback_exception = lambda *e: errors.append(str(e))
        started = time.monotonic()

        def sample():
            gc.collect()  # Tk-owned cycles are collected on the GUI thread.
            return {
                "rss_mb": round(process.memory_info().rss / 1024**2, 2),
                "pending_after": len(ui.tk.call("after", "info")),
                "owner_traces": len(ui.owner_var.trace_info()),
                "personality_traces": len(ui.personality_var.trace_info()),
                "motion_widgets": len(ui.motion.styles),
            }

        try:
            with patch.object(ui.config_mgr, "is_premium", return_value=True):
                for page in pages:
                    ui.switch_page(page)
                    ui.update()
                samples.append(sample())
                for cycle in range(args.cycles):
                    for page in pages:
                        began = time.perf_counter()
                        ui.switch_page(page)
                        ui.update()
                        durations.append((time.perf_counter() - began) * 1000)
                    ui.switch_page("settings")
                    ui.settings_search.set("микрофон" if cycle % 2 else "")
                    # Leave an active hover animation before destroying the theme.
                    ui.motion._animate(ui.save_button, True)
                    ui.apply_theme("Emerald Cyber" if cycle % 2 else "Amethyst Modern")
                    ui.update()
                    if (cycle + 1) % 10 == 0:
                        samples.append(sample())
                deadline = time.monotonic() + 0.4
                while time.monotonic() < deadline:
                    ui.update()
                    time.sleep(0.01)
                final = sample()
                report = {
                    "scope": "Linux/Xvfb; offline Tk; no microphone, models, games or accounts",
                    "python": sys.version.split()[0],
                    "tk": str(ui.tk.call("info", "patchlevel")),
                    "cycles": args.cycles,
                    "page_transitions": args.cycles * (len(pages) + 1),
                    "theme_rebuilds": args.cycles,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "transition_ms_median": round(statistics.median(durations), 2),
                    "transition_ms_p95": round(sorted(durations)[int(len(durations) * .95)], 2),
                    "transition_ms_max": round(max(durations), 2),
                    "samples": samples,
                    "final": final,
                    "remaining_transition_items": len(ui.transition_bar.find_all()),
                    "remaining_transition_job": ui._transition_job,
                    "remaining_motion_jobs": len(ui.motion.jobs),
                    "callback_errors": errors,
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                if errors or ui._transition_job or ui.transition_bar.find_all() or ui.motion.jobs:
                    raise RuntimeError("UI callbacks or animation jobs did not settle")
                for key in ("owner_traces", "personality_traces"):
                    if final[key] != samples[0][key]:
                        raise RuntimeError("Accumulating Tk traces: " + key)
                print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            ui.runtime.close()
            ui.integration.stop_all()
            indicator_controller.stop()
            logging.getLogger().removeHandler(ui._log_handler)
            ui.destroy()


if __name__ == "__main__":
    main()
