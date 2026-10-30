"""Small, explicit accessibility recipes; no recorded keystrokes or coordinates."""

import copy
import os

MAX_STEPS = 12
OPERATIONS = {"invoke", "select", "toggle", "expand", "collapse"}


def name_key(name):
    if not isinstance(name, str):
        raise ValueError("Укажите название действия")
    name = " ".join(name.split()).strip(' «»"')
    if not 1 <= len(name) <= 60 or any(not (c.isalnum() or c in " -_") for c in name):
        raise ValueError("Название: до 60 букв, цифр, пробелов или дефисов")
    return name.casefold()


def validate_step(step):
    if not isinstance(step, dict) or step.get("operation") not in OPERATIONS:
        raise ValueError("Неизвестный тип обученного действия")
    for field in (
        "exe",
        "window_class",
        "window_title",
        "name",
        "auto_id",
        "class_name",
        "control_type",
    ):
        if not isinstance(step.get(field), str) or len(step[field]) > 1024:
            raise ValueError("Некорректное описание элемента")
    if not step["exe"] or not step["control_type"] or not (step["name"] or step["auto_id"]):
        raise ValueError("У элемента нет устойчивого имени или идентификатора")
    if step["operation"] == "toggle" and step.get("value") not in (0, 1):
        raise ValueError("Состояние переключателя: включить или выключить")
    parents = step.get("parents", [])
    if not isinstance(parents, list) or len(parents) > 2:
        raise ValueError("Некорректный путь элемента")
    for parent in parents:
        if not isinstance(parent, dict) or set(parent) != {"auto_id", "control_type", "name"}:
            raise ValueError("Некорректный родитель элемента")
        if any(not isinstance(v, str) or len(v) > 1024 for v in parent.values()):
            raise ValueError("Слишком длинное описание элемента")
    return copy.deepcopy(step)


def same_app(first, second):
    return (
        os.path.normcase(os.path.realpath(first)).casefold()
        == os.path.normcase(os.path.realpath(second)).casefold()
    )


def step_label(step):
    label = step["name"] or step["auto_id"]
    verb = {
        "invoke": "Нажать",
        "select": "Выбрать",
        "expand": "Раскрыть",
        "collapse": "Свернуть",
        "toggle": "Включить" if step.get("value") else "Выключить",
    }[step["operation"]]
    return f"{verb}: {label}"
