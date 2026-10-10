"""Validated application settings. Secrets remain in the local, ignored config file."""

import copy
import json
import os
from pathlib import Path
import shutil
import time
import threading
from storage import atomic_write_json
from scenarios import DEFAULT_SCENARIOS, validate_scenarios
from warframe_config import DEFAULT_WARFRAME, DEFAULT_EFFECTS, validate_game_settings

CONFIG_PATH = Path(os.getenv("KREST_CONFIG_PATH", Path(__file__).resolve().parent / "config.json"))
DEFAULT_CONFIG = {
    "schema_version": 2,
    "warframe": DEFAULT_WARFRAME,
    "visual_effects": DEFAULT_EFFECTS,
    "owner_name": "",
    "profile": {"onboarding_completed": False, "personality": "friendly"},
    "license": {"code": ""},
    "media": {"preferred_app": ""},
    "remote_control": {
        "enabled": False,
        "block_when_locked": True,
        "allow_uploads": False,
        "allowed_commands": [],
    },
    "applications": [],
    "application_aliases": {},
    "scenarios": DEFAULT_SCENARIOS,
    "event_rules": [],
    "file_policy": {"folders": [], "exclusions": [], "rules": []},
    "briefings": {"morning": "", "evening": ""},
    "accessibility": {"reduce_motion": False, "font_scale": 100},
    "performance": {
        "mode": "auto",
        "game_processes": "cs2.exe,dota2.exe,Warframe.x64.exe,Warframe.exe",
    },
    "notification_settings": {"mode": "all", "game_important_only": True},
    "selected_theme": "Amethyst Modern",
    "app_settings": {
        "autostart": False,
        "minimize_to_tray": True,
        "close_to_tray": True,
        "voice_enabled": True,
        "voice_auto_start": True,
        "voice_volume": 80,
        "wake_word": "крест",
        "active_timeout": 6,
        "indicator_enabled": True,
        "weather_city": "Москва",
        "microphone": "",
        "vosk_path": "model",
        "silero_path": "model_silero.pt",
        "downloads_auto_sort": False,
        "downloads_path": str(Path.home() / "Downloads"),
        "speech_pause_ms": 700,
        "speech_threshold": 200,
        "speech_auto_threshold": True,
        "speech_confidence": 60,
        "tts_threads": 2,
        "voice_effect": True,
        "short_voice_answers": True,
    },
    "api_keys": dict.fromkeys(
        (
            "steam_api_key",
            "steam_id",
            "discord_token",
            "discord_channel_id",
            "telegram_api_id",
            "telegram_api_hash",
            "telegram_phone",
            "krest_bot_token",
            "telegram_owner_id",
            "telegram_channel_id",
        ),
        "",
    ),
    "command_aliases": {},
    "pronunciations": {},
    "steam": {
        "poll_minutes": 15,
        "install_path": "",
        "inventories": "730:2",
        "telegram_notifications": False,
    },
    "discord_presence": {
        "application_id": "",
        "details": "Крест — личный ассистент",
        "state": "За компьютером",
        "follow_activity": True,
    },
    "services": {},
    "dota_guide": {
        "process_name": "dota2.exe",
        "warning_lead_time": 30,
        "gsi_port": 3000,
        "enabled": False,
        "auto_mode": False,
    },
    "skills": {
        name: True
        for name in (
            "basic",
            "workflows",
            "interactive",
            "meta",
            "media",
            "utils",
            "timers_alarms",
            "system_mon",
            "webo",
            "windows",
            "gid",
            "steam",
            "warframe",
        )
    },
}


def merge(default, values):
    result = copy.deepcopy(default)
    for key, value in values.items():
        if key == "scenarios":
            result[key] = copy.deepcopy(value)
        elif key in result and isinstance(result[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"Раздел {key} должен быть объектом JSON")
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def validate(data):
    validate_game_settings(data)
    validate_scenarios(data["scenarios"])
    remote = data["remote_control"]
    if (
        type(remote["allow_uploads"]) is not bool
        or type(remote["enabled"]) is not bool
        or type(remote["block_when_locked"]) is not bool
        or not isinstance(remote["allowed_commands"], list)
        or len(remote["allowed_commands"]) > 200
        or any(
            not isinstance(v, str) or not 1 <= len(v) <= 2000 for v in remote["allowed_commands"]
        )
    ):
        raise ValueError("Некорректный список удалённых команд")
    from app_catalog import validate_apps

    validate_apps(data)
    from file_tools import validate_policy

    validate_policy(data["file_policy"])
    from automation import validate_rules

    validate_rules(data["event_rules"], data["scenarios"])
    from datetime import datetime

    for value in data["briefings"].values():
        if value:
            datetime.strptime(value, "%H:%M")
    if type(data["accessibility"]["reduce_motion"]) is not bool or data["accessibility"][
        "font_scale"
    ] not in (90, 100, 110, 125, 150):
        raise ValueError("Некорректные настройки доступности")
    if data["performance"]["mode"] not in ("auto", "balanced", "game", "economy"):
        raise ValueError("Неизвестный профиль нагрузки")
    if (
        not isinstance(data["performance"]["game_processes"], str)
        or len(data["performance"]["game_processes"]) > 2000
    ):
        raise ValueError("Укажите список процессов игр до 2000 символов")
    if (
        data["notification_settings"]["mode"] not in ("all", "important", "silent")
        or type(data["notification_settings"]["game_important_only"]) is not bool
    ):
        raise ValueError("Некорректные настройки уведомлений")

    if len(data["pronunciations"]) > 100 or any(
        not isinstance(k, str)
        or not k.strip()
        or not isinstance(v, str)
        or not v.strip()
        or len(k) > 80
        or len(v) > 160
        for k, v in data["pronunciations"].items()
    ):
        raise ValueError(
            "Словарь произношения: до 100 слов, слово до 80, произношение до 160 символов"
        )
    steam = data["steam"]
    if not isinstance(steam["telegram_notifications"], bool):
        raise ValueError("Уведомления Steam в Telegram: нужен переключатель")
    if not str(steam["poll_minutes"]).isdigit() or not 5 <= int(steam["poll_minutes"]) <= 1440:
        raise ValueError("Steam: интервал проверки от 5 до 1440 минут")
    steam["poll_minutes"] = int(steam["poll_minutes"])
    import re

    if not isinstance(steam["inventories"], str) or not re.fullmatch(
        r"\d+:\d+(?:\s*,\s*\d+:\d+){0,9}", steam["inventories"]
    ):
        raise ValueError("Инвентари Steam: AppID:ContextID через запятую, например 730:2,570:2")
    if not isinstance(steam["install_path"], str):
        raise ValueError("Путь Steam должен быть строкой")
    presence = data["discord_presence"]
    if presence["application_id"] and not str(presence["application_id"]).isdigit():
        raise ValueError("Discord Application ID должен быть числом")
    if not isinstance(presence["follow_activity"], bool):
        raise ValueError("Автостатус Discord: нужен переключатель")
    for key in ("details", "state"):
        if not isinstance(presence[key], str) or not 2 <= len(presence[key]) <= 128:
            raise ValueError("Текст Discord Rich Presence: от 2 до 128 символов")
    for ident, service in data["services"].items():
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", ident) or not isinstance(service, dict):
            raise ValueError("Неверная запись сервиса")
        if type(service.get("auto_restart", False)) is not bool:
            raise ValueError("Автоперезапуск сервиса: нужен переключатель")
        if not isinstance(service.get("autostart", False), bool) or not isinstance(
            service.get("path", ""), str
        ):
            raise ValueError("Укажите путь скрипта и переключатель автозапуска")
        if not isinstance(service.get("token", ""), str) or not isinstance(
            service.get("title", ident), str
        ):
            raise ValueError("Название сервиса и токен должны быть строками")
    aliases = data["command_aliases"]
    if len(aliases) > 100 or any(
        not isinstance(key, str)
        or not key.strip()
        or len(key) > 100
        or not isinstance(value, str)
        or not value.strip()
        or len(value) > 2000
        for key, value in aliases.items()
    ):
        raise ValueError("Допускается до 100 своих фраз: фраза до 100, команда до 2000 символов")
    data["command_aliases"] = {k.casefold().strip(): v.strip() for k, v in aliases.items()}
    path = data["app_settings"]["downloads_path"]
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Укажите папку загрузок")
    name = data["owner_name"]
    if not isinstance(name, str) or len(name.strip()) > 60 or any(ord(c) < 32 for c in name):
        raise ValueError("Имя: до 60 символов без управляющих знаков")
    data["owner_name"] = name.strip()
    if type(data["profile"]["onboarding_completed"]) is not bool:
        raise ValueError("Некорректное состояние профиля")
    if data["profile"]["onboarding_completed"] and not name.strip():
        raise ValueError("Введите имя пользователя")
    if data["profile"]["personality"] not in ("friendly", "concise", "lively"):
        raise ValueError("Неизвестный стиль общения")
    if not isinstance(data["license"]["code"], str) or len(data["license"]["code"]) > 4096:
        raise ValueError("Некорректный код Premium")
    if (
        not isinstance(data["media"]["preferred_app"], str)
        or len(data["media"]["preferred_app"]) > 512
    ):
        raise ValueError("Некорректный идентификатор плеера")
    for section in ("app_settings", "skills", "dota_guide"):
        for key, default in DEFAULT_CONFIG[section].items():
            if isinstance(default, bool) and not isinstance(data[section].get(key), bool):
                raise ValueError(f"{key}: требуется логический переключатель")
    app = data["app_settings"]
    if not str(app["wake_word"]).strip() or len(str(app["wake_word"]).split()) != 1:
        raise ValueError("Обращение должно состоять из одного слова")
    for section, key, lo, hi in [
        ("app_settings", "voice_volume", 0, 100),
        ("app_settings", "active_timeout", 1, 60),
        ("app_settings", "speech_pause_ms", 300, 2000),
        ("app_settings", "speech_threshold", 20, 5000),
        ("app_settings", "speech_confidence", 0, 100),
        ("app_settings", "tts_threads", 1, 4),
        ("dota_guide", "gsi_port", 1024, 65535),
        ("dota_guide", "warning_lead_time", 0, 120),
    ]:
        value = data[section][key]
        if isinstance(value, bool) or not str(value).isdigit() or not lo <= int(value) <= hi:
            raise ValueError(f"{key}: требуется целое число от {lo} до {hi}")
        data[section][key] = int(value)
    for key in ("telegram_owner_id", "telegram_api_id"):
        value = data["api_keys"].get(key)
        if value and (not str(value).isdigit() or int(value) <= 0):
            raise ValueError(f"{key}: требуется положительный числовой ID")
    for key in ("telegram_channel_id", "discord_channel_id"):
        value = str(data["api_keys"].get(key, ""))
        if value and not value.lstrip("-").isdigit():
            raise ValueError(f"{key}: требуется числовой ID")
    return data


class ConfigManager:
    def __init__(self, filepath=CONFIG_PATH):
        self.filepath = Path(filepath)
        self._lock = threading.RLock()
        self.load_error = ""
        self.future_version = False
        self.config = self.load_config()
        self._disk_stamp = self._stamp()

    def _stamp(self):
        try:
            info = self.filepath.stat()
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    def load_config(self):
        if not self.filepath.exists():
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            data = json.loads(self.filepath.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("Ожидался объект JSON")
            if data.get("schema_version", 1) not in (1, 2):
                self.future_version = True
                raise ValueError("Настройки созданы более новой версией Крест")
            self.future_version = False
            data["schema_version"] = 2
            if "profile" not in data and data.get("owner_name") in (
                "Игорь",
                "КатоВульф",
                "KatoWolf",
            ):
                data["owner_name"] = ""
            from secret_store import hydrate

            self.secret_errors = []
            return validate(merge(DEFAULT_CONFIG, hydrate(data, self.secret_errors)))
        except (ValueError, OSError) as exc:
            self.load_error = str(exc)
            return copy.deepcopy(DEFAULT_CONFIG)

    def snapshot(self):
        with self._lock:
            stamp = self._stamp()
            if stamp != self._disk_stamp:
                self.load_error = ""
                candidate = self.load_config()
                if not self.load_error:
                    self.config = candidate
                self._disk_stamp = stamp
            return copy.deepcopy(self.config)

    def save_config(self, data=None):
        with self._lock:
            if self.future_version:
                raise ValueError(
                    "Эта версия Крест не может перезаписывать настройки более новой версии"
                )
            candidate = validate(merge(DEFAULT_CONFIG, data if data is not None else self.config))
            if self.load_error and self.filepath.exists():
                backup = self.filepath.with_name(self.filepath.name + f".broken-{time.time_ns()}")
                shutil.copy2(self.filepath, backup)
            from secret_store import protect_config

            atomic_write_json(self.filepath, protect_config(candidate, self.filepath.resolve()))
            if os.name != "nt":
                self.filepath.chmod(0o600)
            self.config = candidate
            self._disk_stamp = self._stamp()
            self.load_error = ""

    def is_premium(self):
        from licensing import status

        return status(self.snapshot())["premium"]
