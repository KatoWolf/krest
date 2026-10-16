"""Telegram remote control menu: callbacks name commands, never carry executable code."""

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

GROUPS = {
    "duty": (
        "Дежурство AFK",
        [
            ("Статус и результаты", "статус дежурства"),
            ("Приостановить", "останови дежурство"),
            ("Продолжить при AFK", "продолжи дежурство"),
            ("Копия настроек", "сохрани настройки в очередь"),
            ("Проверка подключений", "проверь подключения при простое"),
        ],
    ),
    "home": (
        "Обзор ПК",
        [
            ("Статус", "статус"),
            ("Диски", "диски"),
            ("Процессы", "процессы"),
            ("Активное окно", "окно"),
            ("Скриншот", "скриншот"),
        ],
    ),
    "music": (
        "Музыка и микрофон",
        [
            ("Пауза", "пауза"),
            ("Продолжить", "плей"),
            ("Следующий трек", "следующий"),
            ("Предыдущий трек", "предыдущий трек"),
            ("Что играет", "что играет"),
            ("Лайк", "поставь лайк"),
            ("Громче", "громче"),
            ("Тише", "тише"),
            ("Мут Discord", "мут"),
            ("Включить микрофон", "анмут"),
        ],
    ),
    "tasks": (
        "Дела",
        [
            ("План на сегодня", "план на сегодня"),
            ("Таймеры", "список таймеров"),
            ("Будильники", "список будильников"),
            ("История выполнения", "история команд"),
        ],
    ),
    "games": (
        "Игры и Steam",
        [
            ("Инвентарь CS2", "инвентарь"),
            ("Предложения обмена", "трейды"),
            ("Мир Warframe", "сводка варфрейм"),
            ("Циклы Warframe", "циклы варфрейм"),
        ],
    ),
    "pc": (
        "Управление ПК",
        [
            ("Блокировка", "заблокируй компьютер"),
            ("Выключение", "выключи пк"),
            ("Перезагрузка", "перезагрузи пк"),
            ("Отменить выключение", "отмени выключение"),
            ("Файлы пульта", "файлы"),
        ],
    ),
}


def menu(settings, group=None):
    cfg = settings.get("remote_control", {})
    allowed = {v.casefold() for v in cfg.get("allowed_commands", [])}
    keyboard = InlineKeyboardMarkup(row_width=2)
    if group not in GROUPS:
        for key, (title, _) in GROUPS.items():
            keyboard.add(InlineKeyboardButton(title, callback_data="menu:" + key))
        status = (
            "Пульт включён"
            if cfg.get("enabled")
            else "Пульт выключен — включите на странице Telegram в Крест"
        )
        text = (
            "КРЕСТ · ПУЛЬТ КОМПЬЮТЕРА\n"
            + status
            + "\n\nВыберите раздел. Команды выполняются на вашем ПК.\n"
        )
        text += "\nРазрешённые команды:\n" + (
            "\n".join(cfg.get("allowed_commands", []))
            or "Список пуст — нажмите «Включить базовый пульт» в Крест."
        )
    else:
        title, items = GROUPS[group]
        text = "КРЕСТ · " + title.upper() + "\n\n🔒 — команда не включена в доступ пульта."
        for index, (label, command) in enumerate(items):
            keyboard.add(
                InlineKeyboardButton(
                    ("" if command in allowed else "🔒 ") + label,
                    callback_data=f"run:{group}:{index}",
                )
            )
        if group == "tasks":
            text += "\n\nМожно написать: таймер на 10 минут\nнапомни через час: проверить загрузку"
        if group == "pc":
            text += (
                "\n\nФайлы пульта: uploads и screenshots.\nДля передачи: файл screenshots/имя.png"
            )
        keyboard.add(InlineKeyboardButton("← Все разделы", callback_data="menu:all"))
    return text[:3900], keyboard


def callback_command(value):
    try:
        prefix, group, index = value.split(":")
        if prefix != "run" or not index.isdigit():
            return None
        return GROUPS[group][1][int(index)][1]
    except (KeyError, ValueError, IndexError):
        return None
