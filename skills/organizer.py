from skill_manager import skill
from organizer_commands import parse_create


@skill(
    patterns=[r"напомни участнику (?P<name>.+)"],
    examples=["напомни участнику Макс"],
    description="Уточнить готовность конкретного участника",
    category="Компания",
)
def remind(name, state):
    return state.runtime.organizer.call("member_remind", name=name)


@skill(
    patterns=[r"исключи из сбора (?P<name>.+)"],
    examples=["исключи из сбора Макс"],
    description="Исключить друга из текущего сбора",
    category="Компания",
)
def remove(name, state):
    return state.runtime.organizer.call("member_remove", name=name)


@skill(
    patterns=[r"(?P<request>(?:собери (?:нас|компанию|друзей|пати)|созови друзей)(?: .+)?)"],
    description="Собрать компанию через выбранный юзербот",
    category="Компания",
    examples=["собери компанию", "собери нас вечером в доту начинаем когда будет пятеро"],
)
def gather(request, state):
    return state.runtime.organizer.create(**parse_create(request))


@skill(
    keywords=["кто готов играть", "кого ждем", "сводка компании", "как идет сбор"],
    description="Готовность и ожидание состава",
    category="Компания",
)
def summary(state):
    return state.runtime.organizer.call("summary")


@skill(
    keywords=["приостанови сбор", "пауза сбора"],
    description="Приостановить приглашения",
    category="Компания",
)
def pause(state):
    return state.runtime.organizer.call("pause")


@skill(
    keywords=["продолжи сбор", "возобнови сбор"],
    description="Продолжить сбор компании",
    category="Компания",
)
def resume(state):
    return state.runtime.organizer.call("resume")


@skill(
    keywords=["отмени сбор компании", "отмени сбор", "распусти компанию"],
    description="Отменить сбор и уведомить друзей",
    category="Компания",
)
def cancel(state):
    return state.runtime.organizer.call("cancel")


@skill(
    keywords=["запускай нашу игру", "начинаем нашу игру"],
    description="Открыть игру и канал после готовности состава",
    category="Компания",
)
def launch(state):
    return state.runtime.organizer.call("launch")


@skill(
    keywords=["обнови друзей организатора"],
    description="Получить друзей из выбранного мессенджера",
    category="Компания",
)
def contacts(state):
    return state.runtime.organizer.call("contacts_load")


@skill(
    keywords=["кто в нашей компании", "наш список друзей"],
    description="Назвать выбранных друзей",
    category="Компания",
)
def roster(state):
    data = state.runtime.organizer.snapshot()
    names = [
        c["name"]
        for c in data["contacts"]
        if c["enabled"] and c["service"] == data["preferences"]["messenger"]
    ]
    return (
        "В компании: " + ", ".join(names)
        if names
        else "Сначала выберите друзей в разделе «Компания»"
    )


@skill(
    keywords=["что просили друзья", "просьбы друзей"],
    description="Прочитать переданные просьбы",
    category="Компания",
)
def requests(state):
    rows = [
        r
        for r in state.runtime.organizer.snapshot()["requests"]
        if r["status"] in {"waiting", "delivered", "reply_failed", "reply_uncertain"}
    ]
    return (
        "; ".join(r["name"] + ": " + r["text"] for r in rows[-5:]) if rows else "Новых просьб нет"
    )


@skill(
    patterns=[
        r"передай ожидающему (?P<text>.+)",
        r"скажи (?P<text>(?:через|буду|приду|зайду|готов|не смогу|позже)(?: .+)?)",
    ],
    description="Ответить на единственную свежую просьбу друга",
    category="Компания",
    examples=["передай ожидающему буду через пять минут"],
)
def reply(text, state):
    return state.runtime.organizer.call("request_reply", text=text)


@skill(
    patterns=[r"разбери ответ друга (?P<text>.+)"],
    examples=["разбери ответ друга мб позже"],
    description="Проверить понимание сленга без отправки",
    category="Компания",
)
def explain(text, state):
    result = state.runtime.organizer.call("interpret", text=text)
    return result["label"] + ". " + result["reason"]
