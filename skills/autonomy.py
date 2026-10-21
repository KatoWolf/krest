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
    keywords=["подготовь обслуживание", "займись компьютером пока меня нет"],
    description="Поставить копирование, проверку версий, подключения и настроенную сортировку в очередь AFK",
    category="Дежурство",
)
def maintenance(state):
    return state.runtime.autonomy.call("maintenance")["text"]


@skill(
    keywords=["проверь резервные копии"],
    description="Проверить сохранённые версии пробным восстановлением при AFK",
    category="Дежурство",
)
def verify_backups(state):
    return state.runtime.autonomy.call("enqueue", kind="backup_audit")["text"]


@skill(
    keywords=["проверь подключения", "диагностика подключений"],
    description="Проверить реальное состояние микрофона, управляемых ботов и плеера",
    category="Система",
)
def check_connections(state):
    return state.runtime.health.call("check")


@skill(
    keywords=["почини подключения", "восстанови подключения"],
    description="Проверить подключения и ограниченно восстановить неисправные компоненты",
    category="Система",
)
def repair_connections(state):
    return state.runtime.health.call("repair")


@skill(
    keywords=["отчет о подключениях", "отчёт о подключениях"],
    description="Прочитать последние результаты проверки и восстановления подключений",
    category="Система",
)
def connections_report(state):
    return state.runtime.health.call("report")


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
