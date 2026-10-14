"""Contextual desktop and notification commands, without a language model."""

from skill_manager import skill


@skill(
    patterns=[r"ответь (?:ему|ей|на сообщение) (?P<message>.+)"],
    examples=["ответь ему буду через десять минут"],
    description="Подготовить ответ отправителю; отправка после отдельного подтверждения",
    category="Сообщения",
)
def draft_reply(message, state):
    return state.runtime.conversations.call("draft", text=message)


@skill(
    keywords=["подтверждаю отправку", "отправь подготовленный ответ"],
    description="Отправить только что прочитанный черновик конкретному адресату",
    category="Сообщения",
)
def confirm_reply(state):
    return state.runtime.conversations.call("confirm")


@skill(keywords=["отмени ответ"], description="Удалить черновик ответа", category="Сообщения")
def cancel_reply(state):
    return state.runtime.conversations.call("cancel")


@skill(
    keywords=["напомни об этом сообщении после игры", "напомни о сообщении после игры"],
    description="Напомнить о выбранном сообщении после закрытия игры",
    category="Сообщения",
)
def message_after_game(state):
    return state.runtime.conversations.call("after_game")


@skill(
    keywords=["приглуши этот чат на час"],
    description="Приглушить уведомления и автоответы одному контакту на час",
    category="Сообщения",
)
def mute_chat(state):
    return state.runtime.conversations.call("mute")


@skill(
    patterns=[r"открой папку (?P<folder>загрузки|документы|рабочий стол)"],
    keywords=["открой загрузки"],
    description="Открыть папку и использовать её в следующих командах",
    category="Файлы",
    examples=["открой папку документы"],
)
def context_folder(state, folder="загрузки"):
    return state.runtime.desktop.set_folder(folder)


@skill(
    patterns=[
        r"найди (?P<query>(?:последний |последнюю |свежий |недавний |файл |файлы |документ |установщик |архив |скриншот ).+)"
    ],
    examples=["найди последний архив", "найди документ отчет", "найди установщик за неделю"],
    description="Поиск по именам, типам и датам в загрузках, документах и на рабочем столе",
    category="Файлы",
)
def find_local_file(query, state):
    return state.runtime.desktop.search(query)


@skill(
    patterns=[r"выбери файл (?P<number>[1-5]|один|два|три|четыре|пять)"],
    examples=["выбери файл два"],
    description="Выбрать файл из последних результатов поиска",
    category="Файлы",
)
def choose_local_file(number, state):
    return state.runtime.desktop.choose(
        int(number)
        if number.isdigit()
        else ["один", "два", "три", "четыре", "пять"].index(number) + 1
    )


@skill(
    keywords=["открой выбранный файл", "открой его"],
    description="Открыть выбранный документ; исполняемые файлы требуют ручного запуска",
    category="Файлы",
)
def open_context_file(state):
    return state.runtime.desktop.open_selected()


@skill(
    keywords=["покажи его в папке", "покажи выбранный файл"],
    description="Показать выбранный файл в Проводнике",
    category="Файлы",
)
def reveal_context_file(state):
    return state.runtime.desktop.open_selected(reveal=True)


@skill(
    keywords=["распакуй его сюда", "распакуй выбранный архив"],
    description="Распаковать выбранный ZIP в отдельную папку без перезаписи",
    category="Файлы",
)
def extract_context_file(state):
    return state.runtime.desktop.extract(state.runtime.command_stop)


@skill(
    keywords=["проверь последнее действие", "проверь результат команды"],
    description="Показать подтверждение действий плеера, Discord и запуска программ",
    category="Система",
)
def verify_action(state):
    return state.runtime.verifier.summary()


@skill(
    keywords=["режим звонка", "звук для звонка"],
    description="Приглушить выбранные приложения на время звонка",
    category="Звук",
)
def call_audio(state):
    return state.runtime.audio_focus.set_mode("call")


@skill(
    keywords=["верни обычный звук", "звонок окончен"],
    description="Восстановить громкость приложений после звонка",
    category="Звук",
)
def normal_audio(state):
    return state.runtime.audio_focus.set_mode("normal")


@skill(
    keywords=["автоматический звук", "автозвук"],
    description="Переключать звуковой профиль по обнаруженной игре",
    category="Звук",
)
def auto_audio(state):
    return state.runtime.audio_focus.set_mode("auto", bool(state.runtime.performance.game))
