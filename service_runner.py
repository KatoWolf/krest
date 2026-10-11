"""Explicit entry points used by compiled child processes."""

BUILTIN_SERVICES = (
    "tg_bridge.py",
    "krest_bot.py",
    "ds_session.py",
    "discord_account.py",
    "steam_bot.py",
    "discord_presence.py",
)


def run_service(name):
    from licensing import require_premium
    from configuration import ConfigManager

    require_premium(ConfigManager().snapshot())
    from notifications import NotificationQueue

    if name == "tg_bridge.py":
        from tg_bridge import start_tg_bridge

        start_tg_bridge(NotificationQueue())
    elif name == "krest_bot.py":
        from krest_bot import run_krest_service

        run_krest_service(NotificationQueue())
    elif name == "ds_session.py":
        from ds_session import start_ds_bridge

        start_ds_bridge(NotificationQueue())
    elif name == "discord_account.py":
        from discord_account import main

        main()
    elif name == "steam_bot.py":
        from steam_bot import main

        main()
    elif name == "discord_presence.py":
        from discord_presence import main

        main()
    else:
        raise ValueError("Неизвестный встроенный сервис")
    return 0


def run_external(path):
    from pathlib import Path
    import runpy
    import sys
    from licensing import require_premium
    from configuration import ConfigManager

    require_premium(ConfigManager().snapshot())
    script = Path(path).resolve(strict=True)
    if script.suffix.casefold() != ".py":
        raise ValueError("Нужен Python-скрипт .py")
    sys.argv = [str(script)]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")
    return 0
