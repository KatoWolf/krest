"""Official local Rich Presence IPC; never authenticates with a user token."""

import time
from bridge_sdk import data_dir, emit
from configuration import ConfigManager
from steam_service import read_cache


def presence_payload(cfg, status):
    settings = cfg.get("discord_presence", {})
    details = settings.get("details", "Крест — личный ассистент")
    state = settings.get("state", "За компьютером")
    if settings.get("follow_activity", True):
        details = status.get("game") or details
        state = status.get("full_message") or state
    return {
        "details": str(details)[:128] if len(str(details)) >= 2 else "Крест",
        "state": str(state)[:128] if len(str(state)) >= 2 else "За компьютером",
    }


def main():
    from pypresence import Presence

    connection = None
    signature = None
    last_error = ""
    while True:
        cfg = ConfigManager().snapshot()
        app_id = str(cfg.get("discord_presence", {}).get("application_id", ""))
        try:
            if not app_id.isdigit():
                raise ValueError("Введите Application ID в настройках Discord")
            if connection is None or signature != app_id:
                if connection:
                    connection.close()
                connection = Presence(app_id)
                connection.connect()
                signature = app_id
                emit("ready", "Rich Presence подключён к локальному Discord")
            status = read_cache(data_dir() / "current_status.json")
            if time.time() - status.get("updated_at", 0) > 30:
                status = {}
            connection.update(**presence_payload(cfg, status))
            emit("status", "Активность обновлена")
            last_error = ""
            time.sleep(20)
        except KeyboardInterrupt:
            if connection:
                connection.close()
            return
        except Exception as exc:
            message = (
                "Discord Rich Presence: откройте настольный Discord и проверьте Application ID. "
                + type(exc).__name__
            )
            if message != last_error:
                emit("error", message)
                last_error = message
            if connection:
                try:
                    connection.close()
                except Exception:
                    pass
            connection = None
            time.sleep(30)


if __name__ == "__main__":
    main()
