"""Managed Steam monitor. Read-only; it never accepts or sends a trade."""

import threading
import time
from configuration import ConfigManager
from bridge_sdk import commands, data_dir, emit
from steam_service import SteamMonitor
from performance import shared_multiplier


def deliver_notification(text, cfg):
    emit("notification", text, priority="high")
    if not cfg.get("steam", {}).get("telegram_notifications", False):
        return
    import os
    import requests

    keys = cfg.get("api_keys", {})
    token = os.getenv("KREST_BOT_TOKEN") or keys.get("krest_bot_token", "")
    owner = str(keys.get("telegram_owner_id", ""))
    if not token or not owner.isdigit() or int(owner) <= 0:
        emit("error", "Событие сохранено в Крест. Для Telegram заполните Bot Token и ID владельца.")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": owner, "text": text[:4000]},
            timeout=(5, 15),
        )
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise ValueError("Telegram rejected notification")
    except (requests.RequestException, ValueError):
        emit(
            "error",
            "Событие сохранено в Крест, но Telegram не подтвердил доставку. Откройте диалог с ботом и проверьте настройки.",
        )


class SteamHealthReporter:
    """Keep a failed remote check visible until a successful remote refresh."""

    def __init__(self):
        self.error = ""

    def fail(self, message):
        self.error = message
        emit("error", message, capabilities=["refresh"])

    def report(self, data, cfg, remote):
        from integrations import redact

        errors = data.get("errors", {})
        if errors:
            reasons = "; ".join(f"{key}: {value}" for key, value in errors.items())
            self.fail("Steam: " + redact(reasons, cfg)[:1800])
            return
        if self.error and not remote:
            # Local manifests do not demonstrate restored API connectivity.
            emit("error", self.error, capabilities=["refresh"])
            return
        summary = f"Игр: {len(data.get('games', []))}"
        if remote:
            self.error = ""
            emit("ready", summary, capabilities=["refresh"])
        else:
            emit("status", summary)


def main():
    refresh = threading.Event()

    def read_control():
        for command in commands():
            if command["command"] == "refresh":
                refresh.set()

    threading.Thread(target=read_control, daemon=True).start()
    monitor = SteamMonitor(
        data_dir() / "steam_state.json",
        lambda text: deliver_notification(text, ConfigManager().snapshot()),
    )
    health = SteamHealthReporter()
    emit("status", "Монитор Steam выполняет первую проверку")
    next_remote = 0
    last_manual = -60
    while True:
        cfg = ConfigManager().snapshot()
        now = time.monotonic()
        manual = refresh.is_set() and now - last_manual >= 60
        if manual:
            last_manual = now
        remote = now >= next_remote or manual
        refresh.clear()
        try:
            data = monitor.refresh(cfg, remote=remote)
            health.report(data, cfg, remote)
        except Exception as exc:
            health.fail(f"Проверка Steam не завершена: {type(exc).__name__}")
        if remote:
            next_remote = now + int(
                cfg.get("steam", {}).get("poll_minutes", 15)
            ) * 60 * shared_multiplier(data_dir())

        refresh.wait(30 * shared_multiplier(data_dir()))


if __name__ == "__main__":
    from licensing import require_premium
    from configuration import ConfigManager

    require_premium(ConfigManager().snapshot())
    main()
