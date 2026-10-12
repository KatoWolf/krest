import sys
import os
import time
import subprocess
import logging
import re
import voice


from skill_manager import skill, SKILL_REGISTRY, SkillManager  # type: ignore # pyright: ignore

logger = logging.getLogger("SkillMeta")


def _get_state():
    from app_state import get_state

    return get_state()


def show_toast(title, message):
    logger.info("%s: %s", title, message)


@skill(
    keywords=[
        "тихий режим",
        "без звука ассистент",
        "режим мут",
        "выключи голос",
        "включи тихий режим",
        "включи mute",
    ],
    description="Перевод ассистента в тихий режим (без вербальных ответов, с всплывающими уведомлениями)",
    category="Meta",
)
def toggle_mute_mode(text: str):
    state = _get_state()
    state.is_muted = True
    voice.stop_speaking()

    voice.speak("Тихий режим включён. Ответы остаются в меню.")


@skill(
    keywords=[
        "усни",
        "режим ожидания",
        "спи",
        "крест усни",
        "пауза ассистента",
        "перейди в сон",
    ],
    description="Перевод ассистента в режим сна (реагирует только на команды активации)",
    category="Meta",
)
def go_to_sleep(text: str):
    state = _get_state()
    state.is_sleeping = True
    msg = "Ухожу в режим ожидания. Скажите 'Крест проснись' для возобновления работы."
    if getattr(state, "is_muted", False):
        show_toast("Крест Ассистент", "💤 Ассистент перешел в режим сна.")
    voice.speak(msg)


@skill(
    keywords=["проснись", "активация", "крест проснись", "вставай", "подъем"],
    description="Вывод ассистента из режима сна в активное состояние",
    category="Meta",
)
def wake_up(text: str):
    state = _get_state()
    if getattr(state, "is_sleeping", False):
        state.is_sleeping = False
        msg = "Я снова на связи и готова выполнять ваши команды!"
        if getattr(state, "is_muted", False):
            show_toast("Крест Ассистент", "🔔 Ассистент проснулся и готов к работе.")
        voice.speak(msg)
    else:
        voice.speak("Я и так нахожусь в активном режиме.")


@skill(
    keywords=[
        "статус ассистента",
        "настройки ассистента",
        "информация об ассистенте",
        "состояние ассистента",
    ],
    description="Вывод текущего состояния голосового ассистента (Mute, Sleep, Uptime, Skills)",
    category="Meta",
)
def get_assistant_status(text: str):
    state = _get_state()

    is_muted = getattr(state, "is_muted", False)
    is_sleeping = getattr(state, "is_sleeping", False)
    start_time = getattr(state, "start_time", time.time())

    mute_state = "Включен (без голоса)" if is_muted else "Выключен (с голосом)"
    sleep_state = "В режиме сна" if is_sleeping else "Активен"

    uptime = int(time.time() - start_time)
    mins = uptime // 60

    status_report = (
        f"Статус ассистента Крест: "
        f"Тихий режим: {mute_state}. "
        f"Режим сна: {sleep_state}. "
        f"Время работы: {mins} минут. "
        f"Загружено навыков: {len(SKILL_REGISTRY)}."
    )

    if is_muted:
        show_toast("Статус Ассистента", status_report)
    voice.speak(status_report)


@skill(
    keywords=[
        "выключись",
        "заверши работу",
        "закрой ассистента",
    ],
    description="Полное завершение работы голосового ассистента Крест",
    category="Meta",
)
def shutdown(text: str):
    state = _get_state()
    msg = "Отключаю все системы Крест. До связи!"
    if getattr(state, "is_muted", False):
        show_toast("Крест Ассистент", "🛑 Работа ассистента завершена.")
    voice.speak(msg)
    state.shutdown_requested.set()


@skill(
    keywords=[
        "что ты умеешь",
        "справка",
        "команды",
        "помощь",
        "список команд",
        "все команды",
    ],
    description="Вывод в консоль и озвучивание общедоступных категорий и функций",
    category="Meta",
)
def show_help(text: str):
    state = _get_state()
    categories = sorted(list(set(getattr(item, "category", "General") for item in SKILL_REGISTRY)))

    print("\n" + "=" * 65)
    print("        ГОЛОСОВОЙ АССИСТЕНТ КРЕСТ — СПИСОК НАВЫКОВ ПО КАТЕГОРИЯМ")
    print("=" * 65)

    for cat in categories:
        cat_skills = [
            item for item in SKILL_REGISTRY if getattr(item, "category", "General") == cat
        ]
        print(f"\n📂 [{cat.upper()}] ({len(cat_skills)} навыка)")
        for item in cat_skills:
            kw_str = (
                ", ".join(item.keywords[:3]) if getattr(item, "keywords", None) else "Regex-шаблон"
            )
            desc = getattr(item, "description", "Без описания")
            print(f"  • {desc} — Ключевые слова: [{kw_str}]")

    print("\n" + "=" * 65 + "\n")

    summary_msg = f"Всего доступно {len(SKILL_REGISTRY)} навыков в {len(categories)} категориях. Откройте страницу «Навыки» для списка и поиска."
    if getattr(state, "is_muted", False):
        show_toast("Справка по командам", summary_msg)
    voice.speak(summary_msg)


@skill(
    keywords=[
        "покажи категории",
        "список категорий",
        "какие есть категории",
        "покажи разделы",
    ],
    description="Озвучивание и вывод на экран всех доступных категорий навыков",
    category="Meta",
)
def list_categories(text: str):
    state = _get_state()
    categories = sorted(list(set(getattr(item, "category", "General") for item in SKILL_REGISTRY)))
    cats_str = ", ".join(categories)

    print("\n📂 ДОСТУПНЫЕ КАТЕГОРИИ НАВЫКОВ:")
    for cat in categories:
        count = sum(1 for item in SKILL_REGISTRY if getattr(item, "category", "General") == cat)
        print(f"  • {cat}: {count} навыка(ов)")

    msg = f"Доступные категории навыков: {cats_str}."
    if getattr(state, "is_muted", False):
        show_toast("Категории навыков", msg)
    voice.speak(msg)


@skill(
    patterns=[
        r"команды категории (?P<cat_name>.+)",
        r"покажи команды (?P<cat_name>.+)",
        r"покажи раздел (?P<cat_name>.+)",
    ],
    description="Просмотр команд конкретной категории (например: команды категории Windows)",
    category="Meta",
)
def show_category_help(text: str, cat_name: str = ""):
    state = _get_state()
    if not cat_name:
        voice.speak("Укажите название категории.")
        return

    target_cat = cat_name.lower().strip()
    matched_skills = []

    for item in SKILL_REGISTRY:
        cat_value = getattr(item, "category", "").lower()
        if cat_value == target_cat or target_cat in cat_value:
            matched_skills.append(item)

    if matched_skills:
        cat_title = getattr(matched_skills[0], "category", "Раздел")
        print(f"\n📂 [КАТЕГОРИЯ: {cat_title.upper()}]")
        for item in matched_skills:
            kw_str = ", ".join(item.keywords[:3]) if getattr(item, "keywords", None) else "Шаблон"
            desc = getattr(item, "description", "Без описания")
            print(f"  • {desc} ({kw_str})")
        print("")

        msg = f"В категории {cat_title} найдено {len(matched_skills)} навыков. Подробности на странице «Навыки»."
        if getattr(state, "is_muted", False):
            show_toast(f"Категория {cat_title}", msg)
        voice.speak(msg)
    else:
        voice.speak(
            f"Категория '{cat_name}' не найдена. Чтобы узнать список категорий, скажите 'Покажи категории'."
        )


@skill(
    patterns=[
        r"найди команду (?P<query>.+)",
        r"поиск команды (?P<query>.+)",
        r"найди навык (?P<query>.+)",
        r"как (?P<query>.+)",
    ],
    keywords=["найди команду", "поиск навыка", "как сделать"],
    description="Интеллектуальный поиск команды по названию, описанию или триггерам",
    category="Meta",
)
def search_command(text: str, query: str = ""):
    """Динамический поиск любой команды в базе зарегистрированных скиллов"""
    state = _get_state()
    search_term = query.lower().strip() if query else text.lower().strip()

    # Удаляем служебные слова из запроса
    for stop_word in ["найди команду", "поиск команды", "найди навык", "крест", "как"]:
        search_term = search_term.replace(stop_word, "").strip()

    if not search_term:
        voice.speak("Что именно вы хотите найти?")
        return

    results = []
    for item in SKILL_REGISTRY:
        desc = getattr(item, "description", "").lower()
        keywords = [k.lower() for k in getattr(item, "keywords", [])]
        category = getattr(item, "category", "").lower()
        func_name = getattr(item, "func_name", "").lower()

        # Проверка на совпадения
        if (
            search_term in desc
            or any(search_term in kw for kw in keywords)
            or search_term in category
            or search_term in func_name
        ):
            results.append(item)

    if results:
        top_skill = results[0]
        kw_example = (
            f"'{top_skill.keywords[0]}'"
            if getattr(top_skill, "keywords", None)
            else "соответствующая фраза"
        )
        desc = getattr(top_skill, "description", "Выполнение задачи")

        reply = f"Найдена команда: {desc}. Произнесите {kw_example}."
        if len(results) > 1:
            reply += f" Найдено еще совпадений: {len(results) - 1}. Детали выведены в консоль."

        print(f"\n🔍 [РЕЗУЛЬТАТЫ ПОИСКА ДЛЯ '{search_term}']: ")
        for res in results:
            kw_str = ", ".join(res.keywords[:3]) if getattr(res, "keywords", None) else "Regex"
            print(f"  • {res.description} -> Триггеры: [{kw_str}] (Категория: {res.category})")
        print("")

        if getattr(state, "is_muted", False):
            show_toast("Поиск команд", reply)
        voice.speak(reply)
    else:
        voice.speak(f"К сожалению, я не нашла команд по запросу '{search_term}'.")


@skill(
    keywords=[
        "сохрани команды в файл",
        "экспорт команд",
        "сохрани список команд",
        "выгрузи команды",
    ],
    description="Сохранение всех команд ассистента в файл commands_list.txt",
    category="Meta",
)
def export_commands(text: str):
    """Экспортирует всю базу знаний 'Крест' в файл commands_list.txt"""
    state = _get_state()
    file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "commands_list.txt")

    try:
        categories = sorted(
            list(set(getattr(item, "category", "General") for item in SKILL_REGISTRY))
        )
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("=========================================================\n")
            f.write("      ПОЛНЫЙ СПИСОК КОМАНД ГОЛОСОВОГО АССИСТЕНТА КРЕСТ   \n")
            f.write("=========================================================\n\n")

            for cat in categories:
                cat_skills = [
                    item for item in SKILL_REGISTRY if getattr(item, "category", "General") == cat
                ]
                f.write(f"Раздел: [{cat.upper()}]\n")
                f.write("-" * 50 + "\n")
                for item in cat_skills:
                    kw_str = (
                        ", ".join(item.keywords)
                        if getattr(item, "keywords", None)
                        else "Regex шаблон"
                    )
                    desc = getattr(item, "description", "Без описания")
                    f.write(f"• Описание: {desc}\n")
                    f.write(f"  Ключевые слова: {kw_str}\n\n")
                f.write("\n")

        msg = "Список всех команд успешно сохранен в файл commands_list.txt."
        if getattr(state, "is_muted", False):
            show_toast("Экспорт команд", msg)
        voice.speak(msg)
    except Exception as e:
        logger.error(f"Ошибка сохранения команд в файл: {e}")
        voice.speak("Не удалось сохранить список команд.")


@skill(
    keywords=[
        "перезагрузи навыки",
        "обнови навыки",
        "перезапусти модули",
        "обновить навыки",
    ],
    description="Динамическая горячая перезагрузка всех навыков без перезапуска системы",
    category="Meta",
)
def reload_skills(text: str):
    state = _get_state()
    try:
        manager = state.skill_manager
        if manager is None:
            raise RuntimeError("Менеджер навыков не запущен")
        manager.reload()
        msg = f"Все навыки успешно перезагружены! Активно навыков: {len(SKILL_REGISTRY)}."
        if getattr(state, "is_muted", False):
            show_toast("Обновление навыков", msg)
        voice.speak(msg)
    except Exception as e:
        logger.error(f"Error reloading skills: {e}")
        voice.speak("Ошибка при горячей перезагрузке навыков.")


@skill(
    keywords=["выключи тихий режим", "верни голос", "говори вслух"],
    description="Включить озвучку ответов ассистента",
    category="Meta",
)
def unmute_assistant(text=""):
    state = _get_state()
    state.is_muted = False
    voice.speak("Озвучка ответов включена.")
