"""Run Python sources: GUI by default, or diagnostics/one text command via CLI."""

import argparse
import json
import logging
from dataclasses import asdict

from app_state import AssistantState, state
from app_runtime import AssistantRuntime, check_wake_word
from configuration import ConfigManager


def main(argv=None):
    parser = argparse.ArgumentParser(description="КРЕСТ — локальный ассистент")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Проверить загрузку навыков без GUI и внешних запросов",
    )
    parser.add_argument("--command", help="Выполнить одну текстовую команду")
    parser.add_argument(
        "--voice-core", action="store_true", help="Запустить голосовой режим в консоли"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    if not (args.check or args.command or args.voice_core):
        from welcome_gui import open_menu_gui

        open_menu_gui()
        return 0
    settings = ConfigManager()
    runtime = AssistantRuntime(settings.snapshot, data_dir=settings.filepath.parent)
    try:
        if args.check:
            print(
                json.dumps(
                    {
                        "skills": len(runtime.manager.catalog()),
                        "errors": runtime.manager.errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1 if runtime.manager.errors else 0
        if args.command:
            result = runtime.execute(args.command)
            print(result.text)
            return 0 if result.success else 1
        runtime.start()
        runtime.start_voice()
        while not runtime.state.shutdown_requested.wait(0.5):
            if runtime._voice_thread and not runtime._voice_thread.is_alive():
                return 1
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
