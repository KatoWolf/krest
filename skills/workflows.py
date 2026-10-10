"""Explicit scenario, scheduling and file-history commands."""

from skill_manager import skill


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
    result = SortHistory(folder).undo_last()
    return {
        "status": "error" if result["errors"] else "success",
        "message": f"Возвращено: {result['restored']}. " + "; ".join(result["errors"][:5]),
    }


@skill(
    keywords=["предпросмотр сортировки"],
    description="Показать распределение загрузок без перемещения",
    category="System",
)
def preview_sort(state):
    from downloads import DownloadSorter
    from pathlib import Path

    result = DownloadSorter().scan(
        state.config_getter()["app_settings"]["downloads_path"], automatic=False, preview=True
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
