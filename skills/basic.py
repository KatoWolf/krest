from skill_manager import skill
from commands import execute


@skill(
    keywords=["время", "сколько времени", "дата", "статус"],
    description="Время, дата и состояние системы",
)
def basic(text, state=None):
    import voice

    voice.speak(execute(text))
    return True
