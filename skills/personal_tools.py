"""Voice access to local clipboard snippets and automatically detected game sessions."""

from skill_manager import skill


@skill(
    keywords=["сохрани буфер", "запомни буфер обмена"],
    description="Сохранить текущий текст в локальную историю буфера",
    category="Буфер обмена",
)
def save_clipboard(state):
    return state.runtime.clipboard.call("save")


@skill(
    keywords=["покажи историю буфера", "мои фрагменты"],
    description="Назвать последние пять фрагментов и дать им номера для выбора",
    category="Буфер обмена",
)
def clipboard_history(state):
    return state.runtime.clipboard.describe()


@skill(
    patterns=[r"скопируй фрагмент (?P<number>[1-5]|один|два|три|четыре|пять)"],
    examples=["скопируй фрагмент два"],
    description="Скопировать выбранный фрагмент из только что прочитанной истории",
    category="Буфер обмена",
)
def choose_clipboard(number, state):
    value = (
        int(number)
        if number.isdigit()
        else ["один", "два", "три", "четыре", "пять"].index(number) + 1
    )
    return state.runtime.clipboard.choose(value)


@skill(
    patterns=[r"(?:заметка для игры|запиши игровую заметку) (?P<note>.+)"],
    examples=["заметка для игры проверить реликвии после миссии"],
    description="Сохранить голосовую заметку в текущую игровую сессию",
    category="Игровые сессии",
)
def game_note(note, state):
    return state.runtime.game_sessions.add(note)


@skill(
    keywords=["итоги игровой сессии", "отчёт игровой сессии", "отчет игровой сессии"],
    description="Длительность, заметки и число снимков последней игровой сессии",
    category="Игровые сессии",
)
def game_report(state):
    return state.runtime.game_sessions.report()
