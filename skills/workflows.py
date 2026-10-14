"""Explicit scenario, scheduling and file-history commands."""

from skill_manager import skill


@skill(
    keywords=[
        "кто написал",
        "кто мне написал",
        "пропущенные сообщения",
        "прочитай пропущенные сообщения",
    ],
    description="Назвать отправителей новых сообщений из подключённых мессенджеров",
    category="Сообщения",
)
def missed_messages(state):
    from collections import Counter

    rows = [r for r in state.runtime.center.list(unread=True) if r.get("kind") == "message"]
    if not rows:
        return "Новых уведомлений о сообщениях нет."
    senders = Counter()
    for row in rows:
        senders[row["text"].replace("новое сообщение от ", "")] += row["count"]
    return (
        "Пропущенные уведомления: "
        + "; ".join(f"{name}, сообщений {count}" for name, count in list(senders.items())[:8])
        + (". Остальные — в меню уведомлений." if len(senders) > 8 else ".")
    )


@skill(
    keywords=["очисти пропущенные уведомления", "отметь сообщения прочитанными"],
    description="Отметить уведомления о сообщениях в Крест прочитанными; чаты не меняются",
    category="Сообщения",
)
def clear_missed_messages(state):
    for row in state.runtime.center.list(unread=True):
        if row.get("kind") == "message":
            state.runtime.center.mark_read(row["id"])
    return "Уведомления о сообщениях в Крест отмечены прочитанными."


@skill(
    patterns=[r"(?:создай|составь) (?P<draft>(?:сценарий|режим) .+)"],
    examples=["создай сценарий Вечер из действий приглуши музыку, включи не беспокоить"],
    description="Создать черновик сценария голосом без выполнения",
    category="Сценарии",
)
def create_scenario(draft, state):
    return state.runtime.companion.call("draft.create", text="создай " + draft)


@skill(
    keywords=["сохрани сценарий"],
    description="Сохранить предварительно прочитанный черновик",
    category="Сценарии",
)
def save_scenario_draft(state):
    return state.runtime.companion.call("draft.save")


@skill(
    keywords=["отмени черновик"],
    description="Удалить несохранённый черновик сценария",
    category="Сценарии",
)
def cancel_scenario_draft(state):
    return state.runtime.companion.call("draft.cancel")


@skill(
    patterns=[
        r"(?:запомни )?(?:мой|моя|моё|мое) (?P<key>любимый плеер|любимая игра|время напоминаний) (?:это |— |: )?(?P<value>.+)"
    ],
    examples=["мой любимый плеер Яндекс Музыка"],
    description="Запомнить личные предпочтения",
    category="Память",
)
def remember_preference(key, value, state):
    return state.runtime.companion.call("preference.set", key=key, value=value)


@skill(
    keywords=["открой любимый плеер", "включи любимый плеер"],
    description="Открыть запомненный плеер из каталога программ",
    category="Память",
)
def favourite_player(state):
    from app_catalog import launch

    name = state.runtime.memory.recall("любимый плеер")
    if not name:
        return "Скажите: мой любимый плеер — и название из каталога программ."
    return launch(name, state.config_getter())


@skill(
    keywords=["запусти любимую игру", "открой любимую игру"],
    description="Запустить запомненную игру из Steam",
    category="Память",
)
def favourite_game(state):
    from skills.webo import launch_steam_game

    name = state.runtime.memory.recall("любимая игра")
    if not name:
        return "Скажите: моя любимая игра — и название игры."
    return launch_steam_game("", game_name=name)


@skill(
    patterns=[r"напоминай в обычное время (?P<task>.+)"],
    examples=["напоминай в обычное время размяться"],
    description="Ежедневное напоминание в запомненное время",
    category="Память",
)
def preferred_reminder(task, state):
    from skills.timers_alarms import daily_reminder

    clock = state.runtime.memory.recall("время напоминаний")
    if not clock:
        return "Скажите: моё время напоминаний девять утра."
    return daily_reminder(clock + " " + task, state)


@skill(
    patterns=[r"(?:запусти|включи) сценарий (?P<name>.+)"],
    examples=["запусти сценарий Игра"],
    description="Выполнить сохранённый сценарий",
    category="Сценарии",
)
def run_scenario(name, state):
    if not hasattr(state, "runtime"):
        return {"status": "error", "message": "Нужен запущенный центр управления"}
    state.runtime.scenarios.start(name)
    return "Сценарий запущен: " + name


@skill(
    keywords=["останови сценарий", "отмени сценарий"],
    description="Остановить оставшиеся шаги сценария",
    category="Сценарии",
)
def stop_scenario(state):
    state.runtime.scenarios.cancel()
    return "Останавливаю оставшиеся шаги. Начатое действие завершится."


@skill(
    patterns=[
        r"напоминай (?P<days>по будням|по выходным|по понедельникам|по вторникам|по средам|по четвергам|по пятницам|по субботам|по воскресеньям) в (?P<phrase>.+)"
    ],
    examples=["напоминай по будням в 09:00 зарядка"],
    description="Напоминать по выбранным дням недели",
    category="Timers",
)
def scheduled_reminder(days, phrase, state):
    from skills.timers_alarms import daily_reminder

    values = {"по будням": [0, 1, 2, 3, 4], "по выходным": [5, 6]}
    for day, name in enumerate(
        [
            "понедельникам",
            "вторникам",
            "средам",
            "четвергам",
            "пятницам",
            "субботам",
            "воскресеньям",
        ]
    ):
        values["по " + name] = [day]
    return daily_reminder(phrase, state, weekdays=values[days])


@skill(
    keywords=["отмени сортировку", "верни последние перемещенные файлы"],
    description="Вернуть неизменённые файлы последней сортировки",
    category="System",
)
def undo_sort(state):
    from sort_history import SortHistory

    folder = state.config_getter()["app_settings"]["downloads_path"]
    result = SortHistory(folder).undo_last(
        state.runtime.command_stop if hasattr(state, "runtime") else None
    )
    return {
        "status": "error" if result["errors"] else "success",
        "message": f"Возвращено: {result['restored']}. " + "; ".join(result["errors"][:5]),
    }


@skill(
    keywords=["верни как было", "отмени последнее действие", "верни последнее изменение"],
    description="Отменить последнее поддерживаемое изменение: громкость, таймер, будильник или ручную сортировку",
    category="System",
)
def undo_action(state):
    runtime = getattr(state, "runtime", None)
    if runtime is None:
        return {"status": "error", "message": "История отмены доступна в запущенном ядре Крест."}
    return runtime.undo.undo()


@skill(
    keywords=["предпросмотр сортировки"],
    description="Показать распределение загрузок без перемещения",
    category="System",
)
def preview_sort(state):
    from downloads import DownloadSorter
    from pathlib import Path

    result = DownloadSorter(state.config_getter().get("file_policy", {})).scan(
        state.config_getter()["app_settings"]["downloads_path"],
        automatic=False,
        preview=True,
        stop=state.runtime.command_stop if hasattr(state, "runtime") else None,
    )
    return (
        "\n".join(Path(v["source"]).name + " → " + v["category"] for v in result["preview"])
        or "Нет готовых к сортировке файлов."
    )


@skill(
    keywords=["проверь плеер", "диагностика музыки"],
    description="Показать возможности и кнопки текущего плеера без нажатий",
    category="Media",
)
def diagnose_player(state):
    from media_control import diagnostics

    return diagnostics(state.config_getter().get("media", {}).get("preferred_app", ""))


@skill(
    patterns=[r"запомни (?P<key>[^:=]+)\s*[:=]\s*(?P<value>.+)"],
    examples=["запомни любимый цвет: фиолетовый"],
    description="Сохранить личную заметку",
    category="Память",
)
def remember(key, value, state):
    state.runtime.memory.remember(key, value, "note")
    return "Запомнила: " + key.strip() + "."


@skill(
    patterns=[r"что ты помнишь (?:про|о) (?P<key>.+)"],
    examples=["что ты помнишь про любимый цвет"],
    description="Прочитать сохранённую заметку",
    category="Память",
)
def recall(key, state):
    return state.runtime.memory.recall(key) or "Такой записи ещё нет."


@skill(
    patterns=[r"забудь заметку (?P<key>.+)"],
    examples=["забудь заметку любимый цвет"],
    description="Удалить одну заметку",
    category="Память",
)
def forget(key, state):
    return "Запись удалена." if state.runtime.memory.forget(key) else "Такая запись не найдена."


@skill(
    keywords=["утренняя сводка", "итоги дня"],
    description="Предстоящие и завершённые дела",
    category="Память",
)
def daily_briefing(state):
    from assistant_memory import briefing

    return briefing(state)


@skill(
    keywords=["предложи привычки", "мои повторяющиеся команды"],
    description="Предложить повторяющиеся действия без автозапуска",
    category="Память",
)
def suggest_routines(state):
    from assistant_memory import routine_suggestions

    rows = routine_suggestions(state.runtime.jobs.list(2000))
    return (
        "\n".join(
            f"{v['command']} — {v['days']} разных дней около {v['time']}. Можно добавить в сценарий."
            for v in rows
        )
        or "Пока недостаточно повторений: нужно минимум три разных дня примерно в одно время."
    )


@skill(
    patterns=[r"(?:открой|запусти) программу (?P<name>.+)"],
    examples=["открой программу калькулятор"],
    description="Запустить программу из локального каталога",
    category="Windows",
)
def launch_catalog_app(name, state):
    from app_catalog import launch

    return launch(name, state.config_getter())


@skill(
    patterns=[r"громкость приложения (?P<name>.+) на (?P<percent>\d{1,3})(?: процентов)?"],
    examples=["громкость приложения YandexMusic.exe на 30"],
    description="Громкость отдельной программы",
    category="Media",
)
def app_volume(name, percent):
    from windows_audio import set_app_volume

    return set_app_volume(name, int(percent))
