from skill_manager import skill


@skill(
    patterns=[r"(?:учись действию|запомни действие) (?P<name>.+)"],
    description="Начать обучение кнопкам программы",
    category="Покажи один раз",
    examples=["учись действию подготовка дискорда"],
)
def begin(name, state):
    return state.runtime.learned.begin(name)


@skill(
    keywords=["запомни кнопку", "запомни этот шаг"],
    description="Запомнить кнопку под указателем, без нажатия",
    category="Покажи один раз",
)
def capture(state):
    return state.runtime.learned.capture()


@skill(
    keywords=["сохрани действие", "закончи обучение действию"],
    description="Сохранить обученное действие",
    category="Покажи один раз",
)
def save(state):
    return state.runtime.learned.save()


@skill(
    keywords=["отмени обучение действию", "отмени обучение кнопкам"],
    description="Отменить черновик обучения",
    category="Покажи один раз",
)
def cancel(state):
    return state.runtime.learned.call("cancel")


@skill(
    keywords=["убери последний шаг"],
    description="Убрать последний показанный элемент",
    category="Покажи один раз",
)
def undo_step(state):
    return state.runtime.learned.call("undo_step")


@skill(
    patterns=[r"выполни действие (?P<name>.+)"],
    description="Выполнить сохранённые нажатия в активной программе",
    category="Покажи один раз",
    examples=["выполни действие подготовка дискорда"],
)
def run(name, state):
    return state.runtime.learned.run(name)


@skill(
    patterns=[r"проверь действие (?P<name>.+)"],
    description="Проверить доступность первого шага без нажатий",
    category="Покажи один раз",
    examples=["проверь действие подготовка дискорда"],
)
def probe(name, state):
    return state.runtime.learned.run(name, probe=True)


@skill(
    keywords=["останови действие", "останови обученное действие"],
    description="Остановить оставшиеся обученные шаги",
    category="Покажи один раз",
)
def stop(state):
    return state.runtime.learned.call("stop")


@skill(
    keywords=["мои обученные действия", "каким действиям я тебя научил"],
    description="Назвать сохранённые обученные действия",
    category="Покажи один раз",
)
def list_saved(state):
    names = state.runtime.learned.names()
    return (
        "Могу выполнить действия: " + "; ".join(names) + "."
        if names
        else "Обученных действий пока нет. Скажи «учись действию» и название."
    )
