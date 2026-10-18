"""Skill discovery and deterministic dispatch for text and voice commands."""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
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
    examples: list = field(default_factory=list)

    @property
    def id(self):
        return self.func.__module__ + "." + self.func.__name__

    @property
    def module(self):
        return self.func.__module__.rsplit(".", 1)[-1]

    @property
    def requires_windows(self):
        return (
            self.module in ("windows", "discord_voice")
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
        if "message" in value and ("success" in value or "status" in value):
            return str(value["message"])
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


def skill(keywords=None, patterns=None, description="", category="Общие", examples=None):
    def decorator(func):
        entry = SkillEntry(
            func,
            [s.casefold().strip() for s in keywords or []],
            [re.compile(p, re.I) for p in patterns or []],
            description,
            category,
            list(examples or []),
        )
        with _registry_lock:
            SKILL_REGISTRY[:] = [item for item in SKILL_REGISTRY if item.id != entry.id]
            SKILL_REGISTRY.append(entry)
        return func

    return decorator


class SkillManager:
    def __init__(self, settings=None, plugin_dir=None):
        self.base_dir = Path(__file__).resolve().parent
        self.settings = settings or (lambda: {})
        self.errors = {}
        self.load_skills()
        self.load_custom_plugins()
        from skill_importer import SkillImports

        root = (
            plugin_dir
            or Path(os.getenv("KREST_CONFIG_PATH", self.base_dir / "config.json")).parent
            / "imported_skills"
        )
        self.imports = SkillImports(self, root)
        self.imports.load_all()
        from extension_packages import ExtensionPackages

        self.packages = ExtensionPackages(self, Path(root).parent / "extensions")

    def _load(self, folder, reload=False):
        root = self.base_dir / folder
        from app_paths import compiled

        if folder == "skills" and compiled():
            from skills import BUILTIN_MODULES

            paths = [root / (name + ".py") for name in BUILTIN_MODULES]
        else:
            paths = sorted(root.glob("*.py"))
        with _registry_lock:
            for path in paths:
                if path.name.startswith("_") or folder == "skills" and path.stem == "gid":
                    continue
                name = f"{folder}.{path.stem}"
                previous = [entry for entry in SKILL_REGISTRY if entry.func.__module__ == name]
                try:
                    if reload and name in sys.modules and not compiled():
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
        if self.settings().get("safe_mode", False):
            return
        self._load("plugins", reload)

    def reload(self):
        self.load_skills(reload=True)
        self.load_custom_plugins(reload=True)
        self.imports.load_all()
        self.packages.load_all()

    def catalog(self):
        with _registry_lock:
            prefix = self.imports.prefix if hasattr(self, "imports") else ""
            result = [
                e
                for e in SKILL_REGISTRY
                if not e.func.__module__.startswith("krest_imported_")
                or e.func.__module__.startswith(prefix)
                and bool(prefix)
            ]
            return [
                e
                for e in result
                if not e.func.__module__.startswith("krest_package_")
                or e.func.__module__.startswith(getattr(self, "package_prefix", "__none__"))
            ]

    def availability(self, entry, settings=None):
        settings = self.settings() if settings is None else settings
        if settings.get("safe_mode", False) and entry.func.__module__.startswith(
            ("plugins.", "krest_imported_", "krest_package_")
        ):
            return "Сторонние навыки отключены в безопасном режиме"
        if not settings.get("skills", {}).get(entry.module, True):
            return "Выключен в настройках"
        if entry.requires_windows and os.name != "nt":
            return "Требуется Windows"
        return "Доступен"

    def match(self, text):
        clean = text.casefold().strip(" ,.!?")
        vocabulary = getattr(self, "name_vocabulary", None)
        if vocabulary is not None:
            clean = vocabulary.resolve(clean).casefold()
        if re.match(r"^(?:не|не надо|не нужно)\s", clean) and clean not in (
            "не нравится",
            "не могу сосредоточиться",
        ):
            return None
        entries = self.catalog()
        # Exact keywords always outrank patterns for the same text. Keep the
        # identity tie-break and live registry; no stale cache after skill import.
        exact = [entry for entry in entries if clean in entry.keywords]
        if exact:
            return min(exact, key=lambda entry: entry.id), {}
        candidates = []
        for entry in entries:
            for pattern in entry.patterns:
                match = pattern.fullmatch(clean)
                if match:
                    captured = sum(
                        match.end(name) - match.start(name)
                        for name in match.groupdict()
                        if match.start(name) >= 0
                    )
                    specificity = max(0, len(clean) - captured)
                    candidates.append((5000 + specificity, entry, match.groupdict()))
        if not candidates:
            return None
        # Stable ordering is by specificity, then skill identity, independent of file traversal.
        _, entry, kwargs = min(candidates, key=lambda v: (-v[0], v[1].id))
        return entry, kwargs

    def suggest(self, text, limit=3):
        """Offer close available keywords without ever dispatching a fuzzy match."""
        clean = text.casefold().strip()
        if len(clean) < 3:
            return []
        scores = {}
        settings = self.settings()
        for entry in self.catalog():
            if self.availability(entry, settings) != "Доступен":
                continue
            for keyword in entry.keywords:
                score = SequenceMatcher(None, clean, keyword).ratio()
                if score >= 0.6:
                    scores[keyword] = max(score, scores.get(keyword, 0))
        return sorted(scores, key=lambda k: (-scores[k], k))[:limit]

    def execute(self, text, state=None):
        from speech_tools import normalize_command
        from command_understanding import exact_command

        text = exact_command(self, text) or normalize_command(
            text, self.settings().get("command_aliases", {})
        )
        match = self.match(text)
        if match is None:
            suggestions = self.suggest(text)
            hint = (
                " Возможно, вы имели в виду: " + "; ".join(suggestions) + "." if suggestions else ""
            )
            return CommandResult(
                False, False, "Команда не найдена. Откройте каталог навыков." + hint
            )
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
            verifier = getattr(getattr(state, "runtime", None), "verifier", None)
            if verifier is not None:
                verifier.record(entry.id, value)
            output = "\n".join(messages)
            if not output and value is not None and not isinstance(value, bool):
                output = format_result(value) if isinstance(value, (dict, list)) else str(value)
            success = value is not False and not (
                isinstance(value, dict)
                and (value.get("status") == "error" or value.get("success") is False)
            )
            if not success and isinstance(value, dict):
                detail = format_result(value)
                if detail not in output:
                    output = (output + "\n" + detail).strip()
            if (
                success
                and not messages
                and output
                and not (
                    isinstance(value, dict)
                    and value.get("verification") in ("pending", "unconfirmed")
                )
            ):
                from action_replies import action_reply

                output = action_reply(entry.id, output, state)
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
