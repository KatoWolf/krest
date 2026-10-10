"""Import this file through Главная → Навыки → Импортировать .py."""

from skill_manager import skill


@skill(
    keywords=["проверь мой новый навык"],
    description="Проверить подключение своего навыка",
    category="Мои навыки",
    examples=["проверь мой новый навык"],
)
def check_import():
    return "Новый навык работает. Команду можно произнести или написать в меню."
