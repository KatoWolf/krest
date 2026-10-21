"""Object commands and explicitly scoped automation, discoverable in the catalog."""

from pathlib import Path
from skill_manager import skill


@skill(
    keywords=["упакуй это", "заархивируй это", "упакуй выбранные файлы"],
    description="Упаковать выделенные в Проводнике файлы в проверенный ZIP в папке archives",
    category="Файлы",
)
def pack_selection(state):
    return state.runtime.smart.objects.pack()


@skill(
    keywords=["открой последний скриншот", "покажи последний скриншот"],
    description="Открыть последний скриншот Крест без поиска в индексе",
    category="Файлы",
)
def last_screenshot(state):
    return state.runtime.smart.objects.latest_screenshot()


@skill(
    keywords=["что сейчас выбрано", "какие файлы выбраны"],
    description="Назвать выделенные в Проводнике файлы и текущую папку",
    category="Файлы",
)
def selected_objects(state):
    context = state.runtime.smart.objects.current()
    return (
        "Выбрано: "
        + (", ".join(Path(p).name for p in context["paths"][:10]) or "нет выделенных файлов")
        + ". Папка: "
        + context["folder"]
    )


@skill(
    keywords=["открой это"],
    description="Открыть выделенный в Проводнике документ или папку",
    category="Файлы",
)
def open_selection(state):
    return state.runtime.smart.objects.open()


@skill(
    keywords=["покажи это в папке"],
    description="Показать выбранный объект в Проводнике",
    category="Файлы",
)
def reveal_selection(state):
    return state.runtime.smart.objects.open(reveal=True)


@skill(
    patterns=[r"(?:все )?архивы(?: с)? (?P<topic>.+?) (?:складывай|сохраняй) сюда и распаковывай"],
    examples=["архивы с модами складывай сюда и распаковывай"],
    description="Создать правило: ZIP с указанным словом из загрузок копировать и распаковывать в выбранной папке при AFK",
    category="Автоматизация",
)
def route_archives(topic, state):
    topic = {
        "модами": "мод",
        "моды": "мод",
        "драйверами": "драйвер",
        "обновлениями": "обновлен",
    }.get(topic.strip(), topic.strip())
    if not topic or len(topic) > 60 or any(c in topic for c in "*?[]/\\:"):
        raise ValueError("Назовите слово из имени архива, например «мод»")
    folder = state.runtime.smart.objects.folder()
    source = state.runtime.settings()["app_settings"]["downloads_path"]
    pattern = "*" + topic + "*.zip"
    if topic == "мод":
        pattern += ";*mod*.zip"
    return state.runtime.smart.downloads.add(source, folder, pattern)["text"]


@skill(
    keywords=["архивы складывай сюда и распаковывай", "все архивы складывай сюда и распаковывай"],
    description="Обрабатывать ZIP из загрузок в выбранную папку только при AFK",
    category="Автоматизация",
)
def route_all_archives(state):
    return state.runtime.smart.downloads.add(
        state.runtime.settings()["app_settings"]["downloads_path"],
        state.runtime.smart.objects.folder(),
    )["text"]


@skill(
    patterns=[r"(?:сообщи|скажи) когда (?:скачается|установится) (?P<name>.+)"],
    examples=["сообщи когда скачается Warframe"],
    description="Наблюдать за установкой названной игры по локальным данным Steam",
    category="Наблюдение",
)
def watch_game(name, state):
    return state.runtime.smart.watches.steam(name)["text"]


@skill(
    keywords=["сообщи когда копирование закончится", "сообщи когда закончится копирование"],
    description="Наблюдать за единственной активной задачей копирования Крест",
    category="Наблюдение",
)
def watch_copy(state):
    return state.runtime.smart.watches.last_duty("copy")["text"]


@skill(
    keywords=[
        "после завершения копирования выключи компьютер",
        "выключи компьютер после копирования",
    ],
    description="После проверенного копирования Крест дождаться AFK и запланировать выключение с минутой на отмену",
    category="Наблюдение",
)
def shutdown_after_copy(state):
    return state.runtime.smart.watches.last_duty("copy", shutdown=True)["text"]


@skill(
    keywords=["следи за этим файлом", "сообщи когда этот файл будет готов"],
    description="Сообщить, когда выбранный файл закрыт и стабилен две минуты; это не проверка успеха внешней программы",
    category="Наблюдение",
)
def watch_file(state):
    paths = state.runtime.smart.objects.current()["paths"]
    if len(paths) != 1 or not Path(paths[0]).is_file():
        raise ValueError("Выделите один файл в Проводнике")
    return state.runtime.smart.watches.add("file", paths[0], Path(paths[0]).name)["text"]


@skill(
    keywords=["следи за загрузками сюда", "следи за этой папкой"],
    description="Наблюдать только за выбранной папкой и сообщать о новых закрытых стабильных файлах",
    category="Наблюдение",
)
def watch_folder(state):
    folder = state.runtime.smart.objects.folder()
    return state.runtime.smart.watches.add("folder", folder, "Файлы: " + Path(folder).name)["text"]


@skill(
    keywords=["отмени наблюдение", "останови наблюдение за задачами"],
    description="Отменить активные наблюдения и ожидающее выключение после задач",
    category="Наблюдение",
)
def cancel_watches(state):
    return state.runtime.smart.watches.cancel()


@skill(
    keywords=["что ты сейчас отслеживаешь", "статус наблюдения"],
    description="Прочитать последние наблюдаемые задачи",
    category="Наблюдение",
)
def watches_status(state):
    from task_watch import STATES

    rows = state.runtime.smart.watches.rows()
    return (
        "\n".join(r["label"] + ": " + STATES[r["status"]] + ". " + r["detail"] for r in rows[:5])
        or "Наблюдений пока нет"
    )


@skill(
    keywords=[
        "приглуши все кроме дискорда",
        "приглуши всё кроме дискорда",
        "приглуши все кроме дискорд",
    ],
    description="Приглушить аудиосессии до 20% исходной громкости, кроме Discord и Крест; вернуть командой «верни обычный звук»",
    category="Звук",
)
def except_discord(state):
    focus = state.runtime.audio_focus
    with focus.lock:
        read, _ = focus.port()
        names = {r["name"].casefold() for r in read()}
        discord = {"discord.exe", "discordcanary.exe", "discordptb.exe"}
        if not names & discord:
            raise ValueError("Аудиосессия Discord не найдена. Подключитесь к голосовому каналу")
        focus.manual_except = discord | {"krest.exe", "python.exe", "pythonw.exe"}
        focus.reasons["except"] = 0.2
        focus._apply()
    return "Приглушила остальное. Для возврата: «верни обычный звук»."


@skill(
    patterns=[r"запомни звук для (?P<profile>игры|звонка|наушников|обычного режима)"],
    examples=["запомни звук для игры"],
    description="Сохранить текущие устройства и громкость приложений в звуковой профиль",
    category="Звук",
)
def remember_sound(profile, state):
    key = {
        "игры": "game",
        "звонка": "call",
        "наушников": "headphones",
        "обычного режима": "normal",
    }[profile]
    return state.runtime.smart.audio.capture(key)


@skill(
    keywords=["включи звуковой диспетчер"],
    description="Автоматически выбирать сохранённые профили звука и распознавать звонки поддерживаемых приложений",
    category="Звук",
)
def enable_sound(state):
    return state.runtime.smart.call(
        "settings", section="sound_dispatcher", options={"enabled": True}
    )


@skill(
    keywords=["выключи звуковой диспетчер"],
    description="Остановить автоматическое переключение сохранённых профилей",
    category="Звук",
)
def disable_sound(state):
    return state.runtime.smart.call(
        "settings", section="sound_dispatcher", options={"enabled": False}
    )


@skill(
    keywords=["предложения автоматизации", "какие привычки ты заметила"],
    description="Назвать повторяющиеся последовательности команд; включение после просмотра шагов в меню",
    category="Автоматизация",
)
def learned_routines(state):
    from routine_learning import TRIGGERS

    rows = [r for r in state.runtime.smart.store.rows("habits", 100) if r["status"] == "suggested"]
    return (
        "\n".join(
            TRIGGERS[r["event"]]
            + (" · " + r["match"] if r["match"] else "")
            + ": "
            + " → ".join(r["steps"])
            for r in rows[:5]
        )
        or "Пока нет устойчивых повторений. Наблюдаю только успешно выполненные команды Крест."
    )
