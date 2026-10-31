"""Voice controls for session protection and explicit correction feedback."""

from skill_manager import skill


@skill(
    keywords=["музыка заикается", "музыка прерывается", "звук заикается"],
    description="Проверить нагрузку и известные сбои помощника музыки; показать факты и разрешённые исправления",
    category="Защита сессии",
)
def music_problem(state):
    return state.runtime.session_guard.call("diagnose", problem="music")


@skill(
    keywords=[
        "игра тормозит",
        "игра начала тормозить",
        "компьютер тормозит",
        "проверь причину тормозов",
    ],
    description="Разовый замер CPU, памяти и диска; причина не выдумывается при отсутствии данных",
    category="Защита сессии",
)
def game_problem(state):
    return state.runtime.session_guard.call("diagnose", problem="game")


@skill(
    keywords=["проверь почему ты меня не слышишь", "проверь слух и нагрузку"],
    description="Диагностика входящего звука и нагрузки с ограниченным восстановлением микрофона",
    category="Защита сессии",
)
def voice_problem(state):
    return state.runtime.session_guard.call("diagnose", problem="voice")


@skill(
    keywords=["освободи ресурсы для игры", "приостанови второстепенный фон"],
    description="На две минуты отложить фоновые задачи Крест; голос и напоминания остаются доступны",
    category="Защита сессии",
)
def relief(state):
    return state.runtime.session_guard.call("relief")


@skill(
    keywords=["матч начался", "я начал матч"],
    description="Отметить начало матча для настроенного диспетчера загрузок",
    category="Защита сессии",
)
def match_start(state):
    return state.runtime.session_guard.call("match", active=True)


@skill(
    keywords=["матч закончился", "я закончил матч", "верни скорость загрузок"],
    description="Закончить матч и вернуть изменённые Крест ограничения загрузчика",
    category="Защита сессии",
)
def match_end(state):
    return state.runtime.session_guard.call("match", active=False)


@skill(
    keywords=["статус защиты сессии", "что ограничивает фон"],
    description="Назвать бюджет Крест и состояние ограничений загрузчика",
    category="Защита сессии",
)
def status(state):
    data = state.runtime.session_guard.snapshot()
    return (
        data["budget"]["reason"]
        + ". "
        + data["match"]["text"]
        + ". "
        + data["downloads"]["message"]
        + ". "
        + data["steam"]["message"]
    )


@skill(
    keywords=["отмени последнее исправление речи", "отмени исправление произношения"],
    description="Отменить последнюю подтверждённую поправку произношения в этой сессии",
    category="Голос",
)
def undo_speech(state):
    return state.runtime.companion.call("learning.undo")
