"""Voice commands for known incoming files. No whole-disk indexing or inference."""

from pathlib import Path
from skill_manager import skill


@skill(
    patterns=[
        r"(?P<action>открой|покажи|где) (?P<when>последний|последняя|последнее|вчерашний|вчерашняя|вчерашнее)(?: скачанный| скачанная| скачанное)? (?P<group>архив|документ|картинка|видео|файл)"
    ],
    examples=["открой последний скачанный архив", "где вчерашний документ"],
    description="Найти файл в истории выбранных папок загрузок; при неоднозначности уточнить выбор",
    category="Файлы",
)
def find_incoming(action, when, group, state):
    service = state.runtime.smart.incoming
    row = service.call(
        "find", group=group, when="вчерашний" if when.startswith("вчера") else "последний"
    )
    if action in {"открой", "покажи"}:
        return service.call("open" if action == "открой" else "reveal", id=row["id"])
    return Path(row["current"]).name + ": " + str(Path(row["current"]).parent)


@skill(
    patterns=[r"найди в истории файлов (?P<query>.+)"],
    examples=["найди в истории файлов инструкция"],
    description="Найти известный Крест файл по части имени",
    category="Файлы",
)
def search_incoming(query, state):
    row = state.runtime.smart.incoming.call("find", query=query)
    return "Нашла: " + row["current"]


@skill(
    keywords=["верни этот файл обратно", "верни найденный файл обратно"],
    description="Вернуть найденный или выбранный в истории файл, если он не изменён и исходное имя свободно",
    category="Файлы",
)
def undo_incoming(state):
    return state.runtime.smart.incoming.call("undo")


@skill(
    keywords=["такие файлы складывай сюда", "такие файлы сохраняй сюда"],
    description="Создать правило по расширению и папке ранее выбранного образца; назначение — открытая папка Проводника",
    category="Файлы",
)
def learn_incoming(state):
    return state.runtime.smart.incoming.call(
        "learn", destination=state.runtime.smart.objects.folder()
    )


@skill(
    keywords=["приостанови обработку файлов", "пауза файлов"],
    description="Приостановить лёгкую автоматическую обработку входящих файлов",
    category="Файлы",
)
def pause_incoming(state):
    return state.runtime.smart.incoming.call("pause", paused=True)


@skill(
    keywords=["продолжи обработку файлов", "включи полезный фон"],
    description="Продолжить обработку выбранных папок при доступных ресурсах, в том числе во время игры",
    category="Файлы",
)
def resume_incoming(state):
    return state.runtime.smart.incoming.call("pause", paused=False)


@skill(
    keywords=["что происходит с файлами", "статус фоновых файлов"],
    description="Назвать состояние лёгкого обработчика входящих файлов",
    category="Файлы",
)
def incoming_status(state):
    return state.runtime.smart.incoming.status
