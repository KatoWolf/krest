"""Explicit voice/text commands for the public-data and personal Warframe companion."""

from skill_manager import skill


def run(state, action, **values):
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return {"status": "error", "message": "Запустите ядро Крест для работы с Warframe."}
    return runtime.warframe.action(action, **values)["text"]


@skill(
    keywords=["сводка варфрейм", "что сегодня в варфрейме", "варфрейм сводка"],
    description="Циклы, активности и личные цели Warframe",
    category="Warframe",
)
def briefing(state):
    return run(state, "briefing")


@skill(
    keywords=["обнови варфрейм"],
    description="Обновить публичные данные мира Warframe",
    category="Warframe",
)
def refresh(state):
    return run(state, "refresh")


@skill(
    keywords=["циклы варфрейм", "когда ночь на равнинах", "время на равнинах"],
    description="Фазы открытых миров Warframe и время смены",
    category="Warframe",
)
def cycles(state):
    return run(state, "cycles")


@skill(
    keywords=["события варфрейм", "разломы варфрейм"],
    description="Разломы, вторжения, вылазка и охота на Архонта",
    category="Warframe",
)
def events(state):
    return run(state, "events")


@skill(
    keywords=["баро", "что привез баро", "когда баро"],
    description="Прибытие Баро, ассортимент и совпадения с целями",
    category="Warframe",
)
def baro(state):
    return run(state, "baro")


@skill(
    keywords=["ночная волна", "задания ночной волны"],
    description="Задачи Ночной Волны и похожие задания",
    category="Warframe",
)
def nightwave(state):
    return run(state, "nightwave")


@skill(
    keywords=["мои цели варфрейм", "цели варфрейм"],
    description="Ваши незавершённые цели Warframe",
    category="Warframe",
)
def goals(state):
    return run(state, "goals")


@skill(
    patterns=[r"варфрейм цель (?P<name>.+)"],
    examples=["варфрейм цель собрать Некроса"],
    description="Добавить цель Warframe",
    category="Warframe",
)
def add_goal(name, state):
    return run(state, "add_goal", name=name)


@skill(
    patterns=[r"варфрейм каждый день (?P<name>.+)"],
    examples=["варфрейм каждый день проверить синдикаты"],
    description="Ежедневный игровой чек-лист, сброс 00:00 UTC",
    category="Warframe",
)
def daily(name, state):
    return run(state, "add_task", name=name, period="daily")


@skill(
    patterns=[r"варфрейм каждую неделю (?P<name>.+)"],
    examples=["варфрейм каждую неделю охота на архонта"],
    description="Еженедельная задача, сброс в понедельник 00:00 UTC",
    category="Warframe",
)
def weekly(name, state):
    return run(state, "add_task", name=name, period="weekly")


@skill(
    patterns=[r"варфрейм план на (?P<duration>.+)"],
    examples=["варфрейм план на тридцать минут"],
    description="План сессии по вашему времени и целям",
    category="Warframe",
)
def plan(duration, state):
    from skills.timers_alarms import parse_timer_duration

    seconds, _ = parse_timer_duration(duration)
    return run(state, "plan", minutes=int(seconds // 60))


@skill(
    patterns=[r"варфрейм где фармить (?P<query>.+)"],
    examples=["варфрейм где фармить нейроды"],
    description="Справочник ресурсов и план изготовления",
    category="Warframe",
)
def farm(query, state):
    return run(state, "farm", query=query, category="Resources")


@skill(
    patterns=[r"варфрейм рецепт (?P<query>.+)"],
    examples=["варфрейм рецепт форма"],
    description="Рецепт компонента, недостающие ресурсы и время изготовления",
    category="Warframe",
)
def recipe(query, state):
    return run(state, "recipe", query=query, category="Misc")


@skill(
    patterns=[r"варфрейм реликвии для (?P<query>.+)"],
    examples=["варфрейм реликвии для Wisp Prime"],
    description="Реликвии, награды и шансы из справочника",
    category="Warframe",
)
def relics(query, state):
    return run(state, "relics", query=query)


@skill(
    patterns=[r"варфрейм следы для (?P<count>\d+)"],
    examples=["варфрейм следы для 4"],
    description="Следы для улучшения нетронутых реликвий до сияющих",
    category="Warframe",
)
def traces(count, state):
    return run(state, "traces", count=int(count), tier="radiant")


@skill(
    keywords=["кузница варфрейм", "что готово в кузнице"],
    description="Локальные таймеры изготовления Warframe",
    category="Warframe",
)
def foundry(state):
    return run(state, "foundry")


@skill(
    patterns=[r"варфрейм кузница (?P<name>.+?) на (?P<duration>.+)"],
    examples=["варфрейм кузница форма на двадцать три часа"],
    description="Отметить изготовление и запустить локальный таймер",
    category="Warframe",
)
def foundry_start(name, duration, state):
    from skills.timers_alarms import parse_timer_duration

    seconds, _ = parse_timer_duration(duration)
    return run(state, "foundry_start", name=name, seconds=seconds)


@skill(
    patterns=[r"варфрейм проверь билд (?P<name>.+)"],
    examples=["варфрейм проверь билд мой некрос"],
    description="Недостающие моды и расчёт усиления сохранённой сборки",
    category="Warframe",
)
def build(name, state):
    return run(state, "build_report", name=name)


@skill(
    patterns=[r"варфрейм рынок (?P<query>.+)"],
    examples=["варфрейм рынок Wisp Prime Set"],
    description="Найти предмет на Warframe Market; выбор предложений в меню",
    category="Warframe",
)
def market(query, state):
    return run(state, "market_search", query=query)


@skill(
    patterns=[r"варфрейм запомни (?P<name>.+)"],
    keywords=["варфрейм сохрани момент"],
    examples=["варфрейм запомни завтра добить нейроды"],
    description="Заметка сессии, скриншот и запрос клипа по настройкам",
    category="Warframe",
)
def bookmark(state, name="Игровой момент"):
    return run(state, "bookmark", name=name)


@skill(
    keywords=["журнал варфрейм", "на чем остановились в варфрейме"],
    description="Последние заметки и игровые сессии",
    category="Warframe",
)
def journal(state):
    return run(state, "journal")


@skill(
    keywords=["запусти варфрейм", "запусти warframe"],
    description="Запустить Warframe; профиль включится после обнаружения игры",
    category="Warframe",
)
def launch(state):
    return run(state, "launch")


@skill(
    keywords=["что видишь в варфрейме", "варфрейм экран"],
    description="Последнее локальное наблюдение экрана Warframe",
    category="Warframe",
)
def vision(state):
    return run(state, "vision")
