"""Check prerequisites before spawning optional processes."""

import importlib.util
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def voice_report(cfg):
    app = cfg.get("app_settings", {})
    lines = []
    for module in ("torch", "vosk", "sounddevice", "numpy", "num2words"):
        lines.append(
            f"{module}: " + ("установлен" if importlib.util.find_spec(module) else "не установлен")
        )
    from voice_models import discover_models

    models = discover_models(cfg)
    for key, title in (("vosk_path", "Vosk / распознавание"), ("silero_path", "Silero / озвучка")):
        lines.append(title + ": " + (models[key] or "модель не найдена"))
    lines.append("Микрофон: " + (str(app.get("microphone")) or "системный по умолчанию"))
    lines.append("Обращение: " + app.get("wake_word", "крест"))
    lines.append(
        "Проверка файлов не гарантирует совместимость модели. Для проверки записи включите голос."
    )
    return "\n".join(lines)


def validate_module(filename, cfg):
    keys = cfg.get("api_keys", {})
    required = {
        "krest_bot.py": ("krest_bot_token", "telegram_owner_id"),
        "ds_session.py": ("discord_token",),
        "discord_account.py": ("discord_user_token",),
        "tg_bridge.py": ("telegram_api_id", "telegram_api_hash"),
    }
    for key in required.get(filename, ()):
        env_name = {
            "krest_bot_token": "KREST_BOT_TOKEN",
            "discord_token": "DISCORD_TOKEN",
        }.get(key)
        if not keys.get(key) and not (env_name and os.getenv(env_name)):
            raise ValueError(f"Заполните поле {key} на вкладке API.")
    if filename == "krest_bot.py":
        try:
            owner = int(keys["telegram_owner_id"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Telegram Owner ID должен быть целым числом.") from exc
        if owner <= 0:
            raise ValueError("Telegram Owner ID должен быть положительным.")
    packages = {
        "steam_bot.py": ("requests",),
        "discord_presence.py": ("pypresence",),
        "ds_session.py": ("discord",),
        "discord_account.py": ("aiohttp",),
        "tg_bridge.py": ("pyrogram",),
        "krest_bot.py": ("telebot", "psutil", "requests"),
        "main.py": ("torch", "vosk", "sounddevice", "num2words", "numpy"),
    }
    missing = [p for p in packages.get(filename, ()) if importlib.util.find_spec(p) is None]
    if missing:
        raise RuntimeError("Не установлены зависимости: " + ", ".join(missing))
    if (
        filename == "discord_presence.py"
        and not str(cfg.get("discord_presence", {}).get("application_id", "")).isdigit()
    ):
        raise ValueError("Введите Application ID приложения Discord на странице «Мой Discord»")
    if filename == "discord_account.py" and not cfg["discord_account"]["enabled"]:
        raise ValueError("Включите личную сессию на странице «Мой Discord»")
    if filename == "main.py":
        if not (BASE_DIR / "model").is_dir() or not (BASE_DIR / "model_silero.pt").is_file():
            raise RuntimeError(
                "Добавьте модель Vosk в model/ и Silero в model_silero.pt. См. README."
            )
    if filename == "tg_bridge.py" and not (BASE_DIR / "krest_user_session.session").exists():
        raise RuntimeError("Сначала войдите в Telegram с помощью кнопок Код / Войти.")
