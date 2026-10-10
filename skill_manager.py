"""Skill discovery and deterministic dispatch for text and voice commands."""

from dataclasses import dataclass, field
import importlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading

import voice
from app_state import get_state, use_state

logger = logging.getLogger(__name__)
SKILL_REGISTRY = []
_registry_lock = threading.RLock()


@dataclass
class SkillEntry:
    func: object
    keywords: list = field(default_factory=list)
    patterns: list = field(default_factory=list)
    description: str = ""
    category: str = "Общие"

    @property
    def id(self):
        return self.func.__module__ + "." + self.func.__name__

    @property
    def module(self):
        return self.func.__module__.rsplit(".", 1)[-1]

    @property
    def requires_windows(self):
        return (
            self.module == "windows"
            or self.module == "media"
            and not self.func.__name__.startswith("open_")
            or self.module == "system_mon"
            and self.func.__name__ == "clean_temp_files"
            or self.module == "webo"
            and self.func.__name__ in ("launch_steam_game", "list_installed_games")
        )


@dataclass
class CommandResult:
    handled: bool
    success: bool
    text: str
    skill_id: str = ""
    responded: bool = False


def format_result(value):
    if isinstance(value, dict):
        if value.get("status") == "error":
            return value.get("message", "Не удалось выполнить команду")
        if "cpu_usage_percent" in value:
            ram = value["ram"]
            return f"Процессор: {value['cpu_usage_percent']}%. Память: {ram['percent']}%, использовано {ram['used_gb']} из {ram['total_gb']} ГБ."
        if "has_battery" in value:
            if not value["has_battery"]:
                return "Аккумулятор не обнаружен."
            return f"Заряд: {value['percent']}%. " + (
                "Питание от сети." if value["power_plugged"] else "Работа от батареи."
            )
        if "freed_mb" in value:
            return f"Освобождено {value['freed_mb']} МБ старых временных файлов."
        if "moved_count" in value:
            return f"Перемещено файлов: {value['moved_count']}."
        if "total_seconds" in value:
            return f"Компьютер работает {value['days']} дн. {value['hours']} ч. {value['minutes']} мин."
    if isinstance(value, list) and all(
        isinstance(item, dict) and "memory_mb" in item for item in value
    ):
        return (
            "; ".join(f"{item['name']}: {item['memory_mb']} МБ" for item in value)
            or "Крупные процессы не найдены."
        )
    return json.dumps(value, ensure_ascii=False, indent=2)


def skill(keywords=None, patterns=None, description="", category="Общие"):
    def decorator(func):
        entry = SkillEntry(
            func,
            [s.casefold().strip() for s in keywords or []],
            [re.compile(p, re.I) for p in patterns or []],
            description,
            category,
        )
        with _registry_lock:
            SKILL_REGISTRY[:] = [item for item in SKILL_REGISTRY if item.id != entry.id]
            SKILL_REGISTRY.append(entry)
        return func

    return decorator


class SkillManager:
    def __init__(self, settings=None):
        self.base_dir = Path(__file__).resolve().parent
        self.settings = settings or (lambda: {})
        self.errors = {}
        self.load_skills()
        self.load_custom_plugins()

    def _load(self, folder, reload=False):
        root = self.base_dir / folder
        if not root.exists():
            return
        with _registry_lock:
            for path in sorted(root.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                name = f"{folder}.{path.stem}"
                previous = [entry for entry in SKILL_REGISTRY if entry.func.__module__ == name]
                try:
                    if reload and name in sys.modules:
                        SKILL_REGISTRY[:] = [e for e in SKILL_REGISTRY if e.func.__module__ != name]
                        importlib.reload(sys.modules[name])
                    else:
                        importlib.import_module(name)
                    self.errors.pop(name, None)
                except Exception as exc:
                    SKILL_REGISTRY[:] = [e for e in SKILL_REGISTRY if e.func.__module__ != name]
                    SKILL_REGISTRY.extend(previous)
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                    logger.exception("Не загружен %s", name)

    def load_skills(self, reload=False):
        self._load("skills", reload)

    def load_custom_plugins(self, reload=False):
        self._load("plugins", reload)

    def reload(self):
        # gid owns a running service; keep its live state when reloading pure skills.
        from skills import gid

        gid.stop_service()
        self.load_skills(reload=True)
        self.load_custom_plugins(reload=True)

    def catalog(self):
        with _registry_lock:
            return list(SKILL_REGISTRY)

    def availability(self, entry):
        if not self.settings().get("skills", {}).get(entry.module, True):
            return "Выключен в настройках"
        if entry.requires_windows and os.name != "nt":
            return "Требуется Windows"
        return "Доступен"

    def match(self, text):
        clean = text.casefold().strip(" ,.!?")
        candidates = []
        for entry in self.catalog():
            for keyword in entry.keywords:
                if clean == keyword:
                    candidates.append((10000 + len(keyword), entry, {}))
                elif re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", clean):
                    candidates.append((2000 + len(keyword), entry, {}))
            for pattern in entry.patterns:
                match = pattern.fullmatch(clean)
                if match:
                    literal = re.split(r"[([.?\\]", pattern.pattern, maxsplit=1)[0]
                    candidates.append((2002 + len(literal), entry, match.groupdict()))
        if not candidates:
            return None
        # Stable ordering is by specificity, then skill identity, independent of file traversal.
        _, entry, kwargs = sorted(candidates, key=lambda v: (-v[0], v[1].id))[0]
        return entry, kwargs

    def execute(self, text, state=None):
        match = self.match(text)
        if match is None:
            return CommandResult(False, False, "Команда не найдена. Откройте каталог навыков.")
        entry, extra = match
        available = self.availability(entry)
        if available != "Доступен":
            return CommandResult(True, False, available, entry.id)
        state = state or get_state()
        state.skill_manager = self
        try:
            parameters = inspect.signature(entry.func).parameters
            kwargs = {
                key: value
                for key, value in dict(text=text, state=state, **extra).items()
                if key in parameters and value is not None
            }
            with use_state(state), voice.capture() as messages:
                value = entry.func(**kwargs)
            output = "\n".join(messages)
            if not output and value is not None and not isinstance(value, bool):
                output = format_result(value) if isinstance(value, (dict, list)) else str(value)
            success = value is not False and not (
                isinstance(value, dict)
                and (value.get("status") == "error" or value.get("success") is False)
            )
            return CommandResult(
                True, success, output or "Команда выполнена.", entry.id, bool(messages)
            )
        except Exception as exc:
            logger.exception("Ошибка навыка %s", entry.id)
            return CommandResult(True, False, f"Ошибка команды: {exc}", entry.id)

    def dispatch(self, text, state=None):
        result = self.execute(text, state)
        if result.handled and not result.success:
            voice.speak(result.text)
        return result.handled and result.success
