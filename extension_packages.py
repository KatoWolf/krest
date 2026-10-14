"""Reviewed ZIP extensions with declarative UI and bounded isolated execution.

This is process isolation, not a security sandbox. Install trusted packages only.
"""

import hashlib
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile

from storage import atomic_write_json, read_json

EVENTS = {"assistant.started", "game.started", "game.stopped", "timer.due"}


def inspect_package(path):
    path = Path(path)
    if path.stat().st_size > 8 * 1024**2:
        raise ValueError("Пакет должен быть не больше 8 МБ")
    raw = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 100 or sum(e.file_size for e in entries) > 16 * 1024**2:
            raise ValueError("До 100 файлов и 16 МБ после распаковки")
        names = set()
        for entry in entries:
            name = entry.filename
            parts = name.split("/")
            if (
                name.startswith("/")
                or "\\" in name
                or any(p in (".", "..") or ":" in p or p.rstrip(" .") != p for p in parts if p)
                or name.casefold() in names
                or (entry.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Недопустимый путь в пакете")
            names.add(name.casefold())
        if "krest-extension.json" not in names:
            raise ValueError("В ZIP нужен krest-extension.json в корне")
        manifest = json.loads(archive.read("krest-extension.json").decode("utf-8-sig"))
        if not isinstance(manifest, dict) or manifest.get("api_version") != 1:
            raise ValueError("Нужен api_version: 1")
        for key, pattern in (
            ("id", r"[a-z][a-z0-9_-]{2,60}"),
            ("entry", r"[a-zA-Z_][a-zA-Z0-9_]*\.py"),
        ):
            if not isinstance(manifest.get(key), str) or not re.fullmatch(pattern, manifest[key]):
                raise ValueError("Некорректное поле пакета: " + key)
        if manifest["entry"] not in archive.namelist():
            raise ValueError("Файл запуска отсутствует")
        if not isinstance(manifest.get("name"), str) or not 1 <= len(manifest["name"]) <= 80:
            raise ValueError("Название пакета: 1–80 символов")
        commands = manifest.get("commands")
        if not isinstance(commands, list) or not 1 <= len(commands) <= 30:
            raise ValueError("В пакете должно быть 1–30 команд")
        skills = []
        functions = set()
        for row in commands:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("function"), str)
                or not row["function"].isidentifier()
            ):
                raise ValueError("Укажите имя функции команды")
            if row["function"] in functions:
                raise ValueError("Имена функций команд должны быть уникальны")
            functions.add(row["function"])
            phrases = row.get("keywords", [])
            if (
                not isinstance(phrases, list)
                or not 1 <= len(phrases) <= 20
                or any(not isinstance(v, str) or not 1 <= len(v) <= 100 for v in phrases)
            ):
                raise ValueError("Команде нужны фразы длиной 1–100 символов")
            for key in ("undo",):
                if row.get(key) and (not isinstance(row[key], str) or not row[key].isidentifier()):
                    raise ValueError("Некорректный обработчик отмены")
            skills.append(
                dict(
                    function=row["function"],
                    keywords=phrases,
                    patterns=[],
                    examples=phrases[:2],
                    description=str(row.get("description", row["function"]))[:500],
                    category=manifest["name"],
                    decorated=True,
                )
            )
        settings = manifest.setdefault("settings", {})
        if not isinstance(settings, dict) or len(settings) > 20:
            raise ValueError("До 20 настроек расширения")
        for key, row in settings.items():
            if not isinstance(key, str) or not key.isidentifier() or not isinstance(row, dict):
                raise ValueError("Некорректное описание настройки")
            validate_setting(row, row.get("default"))
        events = manifest.setdefault("events", {})
        if (
            not isinstance(events, dict)
            or set(events) - EVENTS
            or any(not isinstance(v, str) or not v.isidentifier() for v in events.values())
        ):
            raise ValueError("Неизвестное событие или функция обработчика")
        permissions = manifest.setdefault("permissions", [])
        if not isinstance(permissions, list) or any(v != "notifications" for v in permissions):
            raise ValueError("Неизвестное разрешение пакета")
    return {
        "package": True,
        "name": manifest["name"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "skills": skills,
        "warnings": [
            "Пакет исполняет доверенный Python-код с правами пользователя. Отдельный процесс защищает от зависания, но не ограничивает доступ к файлам."
        ]
        + (["События: " + ", ".join(events)] if events else []),
        "manifest": manifest,
    }


def validate_setting(schema, value):
    kind = schema.get("type")
    if kind == "boolean" and type(value) is bool:
        return
    if (
        kind == "integer"
        and type(value) is int
        and schema.get("min", -1000000) <= value <= schema.get("max", 1000000)
    ):
        return
    if kind == "string" and isinstance(value, str) and len(value) <= 1000:
        return
    raise ValueError("Значение настройки не соответствует описанию")


class ExtensionPackages:
    def __init__(self, manager, root):
        self.manager, self.root = manager, Path(root)
        self.path = self.root / "packages.json"
        self.lock = threading.RLock()
        self.prefix = (
            "krest_package_"
            + hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:12]
            + "_"
        )
        self.manager.package_prefix = self.prefix
        self.errors = {}
        self.records = {}
        self.registry_error = ""
        try:
            data = read_json(self.path) if self.path.exists() else {}
            if not isinstance(data, dict) or len(data) > 50:
                raise ValueError("Некорректный реестр пакетов")
            for ident, row in data.items():
                if (
                    not re.fullmatch(r"[a-z][a-z0-9_-]{2,60}", ident)
                    or not isinstance(row, dict)
                    or not re.fullmatch(r"[a-f0-9]{32}", str(row.get("folder", "")))
                    or type(row.get("enabled")) is not bool
                ):
                    raise ValueError("Повреждена запись пакета")
                bundle = self.root / row["folder"] / "bundle.zip"
                preview = inspect_package(bundle)
                if preview["sha256"] != row.get("sha256") or preview["manifest"]["id"] != ident:
                    raise ValueError("Установленный пакет изменён вне меню")
                for key, schema in preview["manifest"]["settings"].items():
                    validate_setting(schema, row.get("settings", {}).get(key))
                self.records[ident] = {**row, **preview}
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
            self.registry_error = "Пакеты отключены: " + str(exc)
            self.manager.errors["extensions"] = self.registry_error
            self.records = {}
        self.failures = {}
        self._event_lock = threading.Lock()
        self.load_all()

    def list(self):
        with self.lock:
            return [
                dict(
                    id="package-" + k,
                    name=v["manifest"]["name"],
                    count=len(v["skills"]),
                    enabled=v["enabled"],
                    error=self.errors.get(k, ""),
                    package=True,
                    settings=v.get("settings", {}),
                    schema=v["manifest"]["settings"],
                    commands=v["manifest"]["commands"],
                )
                for k, v in self.records.items()
            ]

    def save(self):
        if self.registry_error:
            raise ValueError(self.registry_error)
        atomic_write_json(self.path, self.records)

    def install(self, path, sha256):
        if self.manager.settings().get("safe_mode") or self.registry_error:
            raise ValueError(self.registry_error or "Пакеты отключены в безопасном режиме")
        preview = inspect_package(path)
        if preview["sha256"] != sha256:
            raise ValueError("Пакет изменился после предпросмотра")
        ident = preview["manifest"]["id"]
        from skill_manager import _registry_lock

        with self.lock, _registry_lock:
            if len(self.records) >= 50 and ident not in self.records:
                raise ValueError("Достигнут лимит 50 пакетов")
            self.root.mkdir(parents=True, exist_ok=True)
            name = uuid.uuid4().hex
            folder = self.root / name
            folder.mkdir()
            old = self.records.get(ident)
            try:
                shutil.copyfile(path, folder / "bundle.zip")
                # Extract only the verified copy, never reopen a mutable source.
                if hashlib.sha256((folder / "bundle.zip").read_bytes()).hexdigest() != sha256:
                    raise ValueError("Пакет изменился при копировании")
                with zipfile.ZipFile(folder / "bundle.zip") as archive:
                    archive.extractall(folder / "code")
                preferences = {k: v["default"] for k, v in preview["manifest"]["settings"].items()}
                if old:
                    for key, value in old.get("settings", {}).items():
                        if key in preferences:
                            try:
                                validate_setting(preview["manifest"]["settings"][key], value)
                                preferences[key] = value
                            except ValueError:
                                pass
                self.records[ident] = dict(
                    preview,
                    folder=name,
                    enabled=True,
                    settings=preferences,
                )
                self._activate(ident)
                self.save()
            except Exception:
                if old is None:
                    self.records.pop(ident, None)
                else:
                    self.records[ident] = old
                shutil.rmtree(folder)
                self.load_all()
                raise
            if old:
                shutil.rmtree(self.root / old["folder"], ignore_errors=True)
            return {
                "id": "package-" + ident,
                "name": preview["name"],
                "count": len(preview["skills"]),
            }

    def _activate(self, ident):
        from skill_manager import SKILL_REGISTRY, skill

        row = self.records[ident]
        module = self.prefix + hashlib.sha256(ident.encode()).hexdigest()[:24]
        occupied = {
            k for e in self.manager.catalog() if e.func.__module__ != module for k in e.keywords
        }
        for command in row["skills"]:
            if any(k.casefold().strip() in occupied for k in command["keywords"]):
                raise ValueError("Фраза расширения уже занята другой командой")
            occupied.update(k.casefold().strip() for k in command["keywords"])
        SKILL_REGISTRY[:] = [e for e in SKILL_REGISTRY if e.func.__module__ != module]
        for command in row["skills"]:
            function = command["function"]

            def invoke(text="", state=None, _function=function, _id=ident):
                result = self.call(_id, _function, {"text": text})
                definition = next(
                    c
                    for c in self.records[_id]["manifest"]["commands"]
                    if c["function"] == _function
                )
                if result.get("undo") is not None and definition.get("undo") and state is not None:
                    from action_undo import record

                    token = result["undo"]
                    record(
                        "plugins",
                        lambda: self.call(_id, definition["undo"], {"undo": token}).get(
                            "text", "Отменено"
                        ),
                    )
                if (
                    state is not None
                    and "notifications" in self.records[_id]["manifest"]["permissions"]
                ):
                    for message in result.get("notifications", [])[:3]:
                        state.runtime.publish_notice(
                            str(message)[:500], row["manifest"]["name"], speak=False
                        )
                return result.get("text", "Команда расширения завершена")

            invoke.__name__ = function
            invoke.__module__ = module
            skill(
                keywords=command["keywords"],
                description=command["description"],
                category=command["category"],
                examples=command["examples"],
            )(invoke)

    def load_all(self):
        from skill_manager import SKILL_REGISTRY, _registry_lock

        with self.lock, _registry_lock:
            SKILL_REGISTRY[:] = [
                e for e in SKILL_REGISTRY if not e.func.__module__.startswith(self.prefix)
            ]
            if self.manager.settings().get("safe_mode"):
                return
            for ident, row in self.records.items():
                if row["enabled"]:
                    try:
                        self._activate(ident)
                    except Exception as exc:
                        self.errors[ident] = str(exc)

    def change(self, ident, action, values=None):
        from skill_manager import SKILL_REGISTRY, _registry_lock

        with self.lock, _registry_lock:
            if self.manager.settings().get("safe_mode") and action in ("enable", "settings"):
                raise ValueError("Пакеты отключены в безопасном режиме")
            previous, registry = copy.deepcopy(self.records), list(SKILL_REGISTRY)
            row = self.records[ident]
            try:
                if action == "settings":
                    if not isinstance(values, dict) or set(values) != set(
                        row["manifest"]["settings"]
                    ):
                        raise ValueError("Набор настроек изменился")
                    for key, value in values.items():
                        validate_setting(row["manifest"]["settings"][key], value)
                    row["settings"] = dict(values)
                elif action == "enable":
                    self._activate(ident)
                    row["enabled"] = True
                    self.failures[ident] = 0
                    self.errors.pop(ident, None)
                elif action in ("disable", "remove"):
                    row["enabled"] = False
                    module = self.prefix + hashlib.sha256(ident.encode()).hexdigest()[:24]
                    SKILL_REGISTRY[:] = [e for e in SKILL_REGISTRY if e.func.__module__ != module]
                    if action == "remove":
                        del self.records[ident]
                else:
                    raise ValueError("Неизвестная операция пакета")
                self.save()
            except Exception:
                self.records = previous
                SKILL_REGISTRY[:] = registry
                raise
            if action == "remove":
                shutil.rmtree(self.root / row["folder"], ignore_errors=True)
            return True

    def call(self, ident, function, payload):
        row = self.records[ident]
        if (
            not row["enabled"]
            or self.manager.settings().get("safe_mode")
            or self.failures.get(ident, 0) >= 3
        ):
            raise ValueError(
                "Расширение отключено или остановлено после трёх ошибок. Включите его заново после проверки."
            )
        from app_paths import app_command

        request = dict(
            payload, entry=row["manifest"]["entry"], function=function, settings=row["settings"]
        )
        folder = self.root / row["folder"]
        request["archive"] = str(folder / "bundle.zip")
        request["sha256"] = row["sha256"]
        try:
            # Output goes to a bounded temporary file; code cannot block the kernel on pipes.
            with tempfile.TemporaryDirectory(prefix="krest-extension-") as tmp:
                input_file, output_file = Path(tmp) / "request.json", Path(tmp) / "result.json"
                atomic_write_json(input_file, request)
                proc = subprocess.run(
                    app_command(
                        "--extension-worker",
                        str(input_file),
                        "--extension-output",
                        str(output_file),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if not output_file.is_file() or output_file.stat().st_size > 16000:
                    raise ValueError("Процесс расширения не вернул допустимый результат")
                result = read_json(output_file)
                if not isinstance(result, dict):
                    raise ValueError("Неверный ответ расширения")
                if (
                    proc.returncode
                    or not isinstance(result.get("text", ""), str)
                    or result.get("error")
                ):
                    raise ValueError(str(result.get("error", "Неверный ответ расширения"))[:500])
                if not isinstance(result.get("notifications", []), list):
                    raise ValueError("Уведомления расширения должны быть списком")
            self.failures[ident] = 0
            return result
        except Exception as exc:
            self.failures[ident] = self.failures.get(ident, 0) + 1
            self.errors[ident] = type(exc).__name__ + ": " + str(exc)[:250]
            raise ValueError(self.errors[ident]) from exc

    def event(self, name, payload, runtime):
        if name not in EVENTS or not self._event_lock.acquire(blocking=False):
            return

        def work():
            try:
                for ident, row in list(self.records.items()):
                    function = row["manifest"]["events"].get(name)
                    if function and row["enabled"]:
                        try:
                            result = self.call(
                                ident, function, {"event": {"name": name, **payload}}
                            )
                            if "notifications" in row["manifest"]["permissions"]:
                                for message in result.get("notifications", [])[:3]:
                                    runtime.publish_notice(
                                        str(message)[:500], row["manifest"]["name"], speak=False
                                    )
                        except ValueError:
                            pass
            finally:
                self._event_lock.release()

        threading.Thread(target=work, name="KrestExtensionEvent", daemon=True).start()


def run_worker(input_path, output_path):
    import runpy
    import sys

    request = read_json(Path(input_path))
    try:
        archive = Path(request["archive"])
        if (
            archive.stat().st_size > 8 * 1024**2
            or hashlib.sha256(archive.read_bytes()).hexdigest() != request["sha256"]
        ):
            raise ValueError("Пакет изменён после установки")
        with tempfile.TemporaryDirectory(prefix="krest-extension-code-") as tmp:
            # Re-validate before extraction, including paths and size bounds.
            inspect_package(archive)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(tmp)
            sys.path.insert(0, tmp)
            module = runpy.run_path(str(Path(tmp) / request["entry"]), run_name="krest_extension")
            function = module.get(request["function"])
            if not callable(function):
                raise ValueError("Не найдена функция расширения")
            context = {k: request[k] for k in ("settings", "text", "event", "undo") if k in request}
            value = function(context)
            result = value if isinstance(value, dict) else {"text": str(value)}
            encoded = json.dumps(result, ensure_ascii=False)
            if len(encoded.encode()) > 16000:
                raise ValueError("Ответ расширения слишком большой")
            atomic_write_json(output_path, result)
        return 0
    except BaseException as exc:
        atomic_write_json(output_path, {"error": str(exc)[:500]})
        return 1


def save_template(path):
    manifest = {
        "api_version": 1,
        "id": "hello-pack",
        "name": "Мой первый пакет",
        "entry": "entry.py",
        "commands": [
            {
                "function": "hello",
                "keywords": ["привет от расширения"],
                "description": "Ответ из настроек пакета",
            }
        ],
        "settings": {
            "greeting": {
                "type": "string",
                "default": "Расширение подключено!",
                "label": "Приветствие",
            }
        },
        "events": {},
        "permissions": [],
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("krest-extension.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(
            "entry.py",
            'def hello(context):\n    return {"text": context["settings"]["greeting"]}\n',
        )
