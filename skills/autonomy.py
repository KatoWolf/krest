"""Voice controls for the AFK queue. A command never bypasses idle checks."""

from skill_manager import skill


@skill(
    keywords=["статус дежурства", "что сделала пока меня не было", "очередь дежурства"],
    description="Статус AFK и последние результаты поручений",
    category="Дежурство",
)
def duty_status(state):
    return state.runtime.autonomy.report()


@skill(
    keywords=["останови дежурство", "пауза дежурства"],
    description="Приостановить дежурство; начатый установщик завершится",
    category="Дежурство",
)
def duty_pause(state):
    return state.runtime.autonomy.call("pause")


@skill(
    keywords=["продолжи дежурство", "возобнови дежурство"],
    description="Разрешить очередь после следующего простоя",
    category="Дежурство",
)
def duty_resume(state):
    return state.runtime.autonomy.call("resume")


@skill(
    keywords=["сохрани настройки в очередь", "резервная копия при простое"],
    description="Поставить копирование настроек Крест в очередь AFK",
    category="Дежурство",
)
def duty_backup(state):
    return state.runtime.autonomy.call("enqueue", kind="profile_backup")["text"]


@skill(
    keywords=["проверь подключения при простое"],
    description="Поставить диагностику управляемых подключений в очередь AFK",
    category="Дежурство",
)
def duty_health(state):
    return state.runtime.autonomy.call("enqueue", kind="health")["text"]


@skill(
    patterns=[r"запусти правило (?P<name>.+)"],
    examples=["запусти правило архив документов"],
    description="Поставить настроенное правило дежурства в очередь по имени",
    category="Дежурство",
)
def duty_rule(name, state):
    from speech_tools import normalize_command

    rows = [
        r
        for r in state.runtime.autonomy.store.rules()
        if normalize_command(r["label"]) == normalize_command(name)
    ]
    if len(rows) != 1:
        return "Назовите уникальное имя правила со страницы «Дежурство»."
    return state.runtime.autonomy.call("rule.run", id=rows[0]["id"])["text"]
