"""Reviewed single-file skill installation; preview parses source without executing it."""

import ast
import copy
import hashlib
import inspect
import json
import logging
from pathlib import Path
import re
import sys
import threading
import types
import uuid

from storage import atomic_write_json

MAX_SOURCE = 512 * 1024
logger = logging.getLogger(__name__)
TEMPLATE = """from skill_manager import skill


@skill(
    keywords=["моя новая команда"],
    description="Пример своего навыка",
    category="Мои навыки",
)
def my_command(text=""):
    return "Новый навык подключён и работает."
"""


def inspect_source(path):
    path = Path(path).expanduser()
    if path.suffix.casefold() != ".py" or not path.is_file():
        raise ValueError("Выберите существующий Python-файл .py")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE + 1)
    if len(raw) > MAX_SOURCE:
        raise ValueError("Навык должен быть не больше 512 КБ")
    try:
        source = raw.decode("utf-8-sig")
        tree = ast.parse(source, filename=path.name)
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError("Не удалось прочитать Python-файл: " + str(exc)) from exc
    rows, warnings = [], []
    names = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith(
            "_"
        ):
            continue
        decorators = [
            d
            for d in node.decorator_list
            if isinstance(d, ast.Call)
            and (
                isinstance(d.func, ast.Name)
                and d.func.id == "skill"
                or isinstance(d.func, ast.Attribute)
                and d.func.attr == "skill"
            )
        ]
        if isinstance(node, ast.AsyncFunctionDef):
            if decorators:
                raise ValueError("Навык " + node.name + ": async-функции пока не поддерживаются")
            warnings.append(node.name + ": async-функция пропущена")
            continue
        if node.name in names:
            raise ValueError("Повторное имя функции: " + node.name)
        names.add(node.name)
        if decorators:
            if len(decorators) != 1 or len(node.decorator_list) != 1 or decorators[0].args:
                raise ValueError(node.name + ": используйте один @skill с именованными параметрами")
            try:
                data = {kw.arg: ast.literal_eval(kw.value) for kw in decorators[0].keywords}
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    node.name + ": параметры @skill должны быть обычными строками и списками"
                ) from exc
            if set(data) - {"keywords", "patterns", "description", "category", "examples"}:
                raise ValueError(node.name + ": неизвестный параметр @skill")
        else:
            if node.decorator_list:
                warnings.append(node.name + ": функция с другим декоратором пропущена")
                continue
            data = {
                "keywords": [node.name.replace("_", " ")],
                "description": ast.get_docstring(node) or node.name,
                "category": "Мои навыки",
            }
        for key in ("keywords", "patterns", "examples"):
            values = data.setdefault(key, [])
            if (
                not isinstance(values, list)
                or len(values) > 30
                or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in values)
            ):
                raise ValueError(node.name + ": некорректный список " + key)
        for key in ("description", "category"):
            data.setdefault(key, node.name if key == "description" else "Мои навыки")
            if not isinstance(data[key], str) or len(data[key]) > 1000:
                raise ValueError(node.name + ": некорректное поле " + key)
        if not data["keywords"] and not data["patterns"]:
            raise ValueError(node.name + ": не заданы команды или шаблоны")
        try:
            captured = set().union(*(re.compile(p, re.I).groupindex for p in data["patterns"]))
        except re.error as exc:
            raise ValueError(node.name + ": ошибка шаблона: " + str(exc)) from exc
        positional = node.args.posonlyargs + node.args.args
        required = positional[: len(positional) - len(node.args.defaults)]
        required += [
            a for a, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None
        ]
        unsupported = [a.arg for a in required if a.arg not in {"text", "state"} | captured]
        if node.args.posonlyargs or unsupported:
            if decorators:
                raise ValueError(
                    node.name
                    + ": обязательные параметры нельзя заполнить из команды: "
                    + ", ".join(unsupported or [a.arg for a in node.args.posonlyargs])
                )
            warnings.append(node.name + ": пропущена, нужны аргументы " + ", ".join(unsupported))
            continue
        rows.append(dict(data, function=node.name, decorated=bool(decorators)))
    if not rows:
        raise ValueError(
            "Не найдено функций для подключения. Используйте шаблон навыка или функции без обязательных аргументов, кроме text/state."
        )
    if len(rows) > 50:
        raise ValueError("Не более 50 функций в одном файле")
    return {
        "name": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "skills": rows,
        "warnings": warnings,
    }, raw


class SkillImports:
    def __init__(self, manager, root):
        self.manager, self.root = manager, Path(root).resolve()
        self.path = self.root / "registry.json"
        self.prefix = (
            "krest_imported_" + hashlib.sha256(str(self.root).encode()).hexdigest()[:16] + "_"
        )
        self.lock = threading.RLock()
        self.records = {}
        self.error = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    not isinstance(data, dict)
                    or data.get("version") != 1
                    or not isinstance(data.get("modules"), dict)
                ):
                    raise ValueError("Неизвестный формат реестра")
                if len(data["modules"]) > 100:
                    raise ValueError("Слишком много модулей")
                for ident, record in data["modules"].items():
                    if (
                        not isinstance(record, dict)
                        or not re.fullmatch(r"[a-f0-9]{24}", ident)
                        or not re.fullmatch(r"[a-f0-9]{32}\.py", record["file"])
                    ):
                        raise ValueError("Повреждена запись модуля")
                    if (
                        not isinstance(record.get("name"), str)
                        or not isinstance(record.get("enabled"), bool)
                        or not isinstance(record.get("sha256"), str)
                        or not re.fullmatch("[a-f0-9]{64}", record["sha256"])
                        or not isinstance(record.get("skills"), list)
                        or not 1 <= len(record["skills"]) <= 50
                    ):
                        raise ValueError("Повреждены сведения о модуле")
                    for row in record["skills"]:
                        if (
                            not isinstance(row, dict)
                            or not isinstance(row.get("function"), str)
                            or not row["function"].isidentifier()
                            or not isinstance(row.get("decorated"), bool)
                            or any(
                                not isinstance(row.get(key), str)
                                for key in ("description", "category")
                            )
                            or any(
                                not isinstance(row.get(key), list)
                                or any(not isinstance(v, str) for v in row[key])
                                for key in ("keywords", "patterns", "examples")
                            )
                        ):
                            raise ValueError("Повреждены сведения о командах")
                self.records = data["modules"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.error = "Реестр импортированных навыков: " + str(exc)
                self.manager.errors["imported_skills"] = self.error

    def preview(self, path):
        return inspect_source(path)[0]

    def list(self):
        with self.lock:
            return {
                "error": self.error,
                "modules": [
                    dict(
                        id=key,
                        name=row["name"],
                        count=len(row["skills"]),
                        enabled=row["enabled"],
                        error=(
                            "Выключен в безопасном режиме"
                            if row["enabled"] and self.manager.settings().get("safe_mode", False)
                            else self.manager.errors.get(self.prefix + key, "")
                        ),
                    )
                    for key, row in sorted(self.records.items())
                ],
            }

    def _save(self, records):
        if self.error:
            raise ValueError(self.error)
        atomic_write_json(self.path, {"version": 1, "modules": records})
        self.records = records

    def _activate(self, ident, record, raw):
        from skill_manager import SKILL_REGISTRY, skill

        name = self.prefix + ident
        occupied = {"keywords": set(), "patterns": set()}
        for entry in self.manager.catalog():
            if entry.func.__module__ != name:
                occupied["keywords"].update(v.casefold().strip(" ,.!?") for v in entry.keywords)
                occupied["patterns"].update(v.pattern.casefold() for v in entry.patterns)
        for row in record["skills"]:
            for kind in ("keywords", "patterns"):
                for value in row[kind]:
                    value = (
                        value.casefold().strip(" ,.!?") if kind == "keywords" else value.casefold()
                    )
                    if value in occupied[kind]:
                        raise ValueError("Команда или шаблон уже занят: " + value)
                    occupied[kind].add(value)
        previous = list(SKILL_REGISTRY)
        old_module = sys.modules.get(name)
        SKILL_REGISTRY[:] = [e for e in SKILL_REGISTRY if e.func.__module__ != name]
        module = types.ModuleType(name)
        module.__file__ = str(self.root / record["file"])
        sys.modules[name] = module
        try:
            exec(compile(raw, module.__file__, "exec"), module.__dict__)
            expected = {row["function"] for row in record["skills"]}
            for row in record["skills"]:
                if not row["decorated"]:
                    func = getattr(module, row["function"], None)
                    if not inspect.isfunction(func) or func.__module__ != name:
                        raise ValueError("Не найдена функция: " + row["function"])
                    skill(
                        keywords=row["keywords"],
                        description=row["description"],
                        category=row["category"],
                    )(func)
            entries = [e for e in SKILL_REGISTRY if e.func.__module__ == name]
            if {e.func.__name__ for e in entries} != expected:
                raise ValueError("Зарегистрированные функции отличаются от предпросмотра")
            keywords, patterns = {}, {}
            for entry in self.manager.catalog():
                for values, seen in (
                    (entry.keywords, keywords),
                    ([p.pattern for p in entry.patterns], patterns),
                ):
                    for value in values:
                        value = value.casefold().strip(" ,.!?")
                        other = seen.get(value)
                        if other and (
                            entry.func.__module__ == name or other.func.__module__ == name
                        ):
                            raise ValueError("Команда или шаблон уже занят: " + value)
                        seen[value] = entry
            self.manager.errors.pop(name, None)
        except (Exception, SystemExit) as exc:
            SKILL_REGISTRY[:] = previous
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module
            raise ValueError("Не удалось подключить модуль: " + str(exc)) from exc

    def load_all(self):
        from skill_manager import _registry_lock, SKILL_REGISTRY

        with self.lock, _registry_lock:
            active = {self.prefix + ident for ident, row in self.records.items() if row["enabled"]}
            SKILL_REGISTRY[:] = [
                e
                for e in SKILL_REGISTRY
                if not e.func.__module__.startswith(self.prefix) or e.func.__module__ in active
            ]
            if self.manager.settings().get("safe_mode", False):
                return
            for ident, row in self.records.items():
                if not row["enabled"]:
                    continue
                try:
                    raw = (self.root / row["file"]).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                        raise ValueError(
                            "Файл изменён вне меню. Импортируйте новую версию через меню."
                        )
                    self._activate(ident, row, raw)
                except (OSError, ValueError) as exc:
                    self.manager.errors[self.prefix + ident] = str(exc)

    def install(self, path, sha256, bindings=None):
        if self.manager.settings().get("safe_mode", False):
            raise ValueError("Импорт выключен в безопасном режиме")
        preview, raw = inspect_source(path)
        if preview["sha256"] != sha256:
            raise ValueError("Файл изменился после предпросмотра. Выберите его заново.")
        bindings = bindings or {}
        if not isinstance(bindings, dict):
            raise ValueError("Некорректные фразы функций")
        for row in preview["skills"]:
            if not row["decorated"] and row["function"] in bindings:
                values = bindings[row["function"]]
                if (
                    not isinstance(values, list)
                    or not 1 <= len(values) <= 30
                    or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values)
                ):
                    raise ValueError("Введите от 1 до 30 непустых фраз для функции")
                row["keywords"] = [v.strip() for v in values]
        ident = hashlib.sha256(preview["name"].casefold().encode()).hexdigest()[:24]
        from skill_manager import SKILL_REGISTRY, _registry_lock

        with self.lock, _registry_lock:
            if len(self.records) >= 100 and ident not in self.records:
                raise ValueError("Можно установить не более 100 модулей")
            self.root.mkdir(parents=True, exist_ok=True)
            filename = uuid.uuid4().hex + ".py"
            target = self.root / filename
            target.write_bytes(raw)
            row = dict(preview, file=filename, enabled=True)
            previous = list(SKILL_REGISTRY)
            name = self.prefix + ident
            old_module = sys.modules.get(name)
            old_record = self.records.get(ident)
            try:
                self._activate(ident, row, raw)
                self._save(dict(self.records, **{ident: row}))
            except (Exception, SystemExit):
                SKILL_REGISTRY[:] = previous
                if old_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old_module
                target.unlink(missing_ok=True)
                raise
            if old_record:
                self._remove_copy(old_record["file"])
            return {"id": ident, "name": row["name"], "count": len(row["skills"])}

    def _remove_copy(self, filename):
        try:
            (self.root / filename).unlink(missing_ok=True)
        except OSError:
            logger.warning("Модуль отключён; старая копия файла пока занята: %s", filename)

    def change(self, ident, action):
        from skill_manager import SKILL_REGISTRY, _registry_lock

        with self.lock, _registry_lock:
            if ident not in self.records or action not in ("enable", "disable", "remove"):
                raise ValueError("Неизвестный модуль или действие")
            records = copy.deepcopy(self.records)
            row = records[ident]
            name = self.prefix + ident
            previous = list(SKILL_REGISTRY)
            old_module = sys.modules.get(name)
            if action == "enable":
                if self.manager.settings().get("safe_mode", False):
                    raise ValueError("Сторонние навыки выключены в безопасном режиме")
                raw = (self.root / row["file"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                    raise ValueError("Файл изменён. Импортируйте его повторно.")
                self._activate(ident, row, raw)
                row["enabled"] = True
            else:
                SKILL_REGISTRY[:] = [e for e in SKILL_REGISTRY if e.func.__module__ != name]
                sys.modules.pop(name, None)
                if action == "disable":
                    row["enabled"] = False
                else:
                    del records[ident]
            try:
                self._save(records)
            except Exception:
                SKILL_REGISTRY[:] = previous
                if old_module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old_module
                raise
            self.manager.errors.pop(name, None)
            if action == "remove":
                self._remove_copy(row["file"])
            return True
