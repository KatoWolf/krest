import sys
import os
import time
import random
import re
import datetime
import subprocess
import logging
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None


try:
    from skill_manager import skill, SKILL_REGISTRY  # type: ignore # pyright: ignore
except ImportError:
    from ..skill_manager import skill, SKILL_REGISTRY  # type: ignore # pyright: ignore

import voice

logger = logging.getLogger("SkillInteractive")


def show_toast(title, message):
    logger.info("%s: %s", title, message)


# Словарь числительных (включая падежные формы для естественного распознавания речи)
WORD_TO_NUM = {
    # 0
    "ноль": 0,
    "нуль": 0,
    "нуля": 0,
    # 1
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одного": 1,
    "одной": 1,
    "единица": 1,
    "единицы": 1,
    # 2
    "два": 2,
    "две": 2,
    "двух": 2,
    "двойка": 2,
    "двойки": 2,
    # 3
    "три": 3,
    "трех": 3,
    "трёх": 3,
    "тройка": 3,
    "тройки": 3,
    # 4
    "четыре": 4,
    "четырех": 4,
    "четырёх": 4,
    "четверка": 4,
    "четвёрка": 4,
    # 5
    "пять": 5,
    "пяти": 5,
    "пятерка": 5,
    "пятёрка": 5,
    # 6
    "шесть": 6,
    "шести": 6,
    "шестерка": 6,
    "шестёрка": 6,
    # 7
    "семь": 7,
    "семи": 7,
    "семерка": 7,
    "семёрка": 7,
    # 8
    "восемь": 8,
    "восьми": 8,
    "восьмерка": 8,
    "восьмёрка": 8,
    # 9
    "девять": 9,
    "девяти": 9,
    "девятка": 9,
    "девятки": 9,
    # 10
    "десять": 10,
    "десяти": 10,
    "десятка": 10,
    "десятки": 10,
    # 11-19
    "одиннадцать": 11,
    "одиннадцати": 11,
    "двенадцать": 12,
    "двенадцати": 12,
    "тринадцать": 13,
    "тринадцати": 13,
    "четырнадцать": 14,
    "четырнадцати": 14,
    "пятнадцать": 15,
    "пятнадцати": 15,
    "шестнадцать": 16,
    "шестнадцати": 16,
    "семнадцать": 17,
    "семнадцати": 17,
    "восемнадцать": 18,
    "восемнадцати": 18,
    "девятнадцать": 19,
    "девятнадцати": 19,
    # Десятки
    "двадцать": 20,
    "двадцати": 20,
    "тридцать": 30,
    "тридцати": 30,
    "сорок": 40,
    "сорока": 40,
    "пятьдесят": 50,
    "пятидесяти": 50,
    "шестьдесят": 60,
    "шестидесяти": 60,
    "семьдесят": 70,
    "семидесяти": 70,
    "восемьдесят": 80,
    "восьмидесяти": 80,
    "девяносто": 90,
    "девяноста": 90,
    # Сотни
    "сто": 100,
    "ста": 100,
    "двести": 200,
    "двухсот": 200,
    "триста": 300,
    "трехсот": 300,
    "трёхсот": 300,
    "четыреста": 400,
    "четырехсот": 400,
    "четырёхсот": 400,
    "пятьсот": 500,
    "пятисот": 500,
    "шестьсот": 600,
    "шестисот": 600,
    "семьсот": 700,
    "семисот": 700,
    "восемьсот": 800,
    "восьмисот": 800,
    "девятьсот": 900,
    "девятисот": 900,
    # Тысячи
    "тысяча": 1000,
    "тысячи": 1000,
    "тысяч": 1000,
}


def text_to_int(val: str) -> Optional[int]:
    """
    Преобразует текстовое представление числа (например: 'пять', 'двадцать три', 'ста пятидесяти', '10') в integer.
    """
    if not val:
        return None
    val = val.strip().lower()

    # Если переданы обычные цифры ("5", "10")
    if val.isdigit():
        return int(val)

    words = re.findall(r"\w+", val)
    if not words:
        return None

    total = 0
    current = 0
    found_any = False

    for w in words:
        if w.isdigit():
            current += int(w)
            found_any = True
        elif w in WORD_TO_NUM:
            n = WORD_TO_NUM[w]
            if n == 1000:
                current = (current if current != 0 else 1) * 1000
                total += current
                current = 0
            else:
                current += n
            found_any = True

    total += current
    return total if found_any else None


# ==========================================
# 1. Бросок кубика и Генератор чисел
# ==========================================


@skill(
    keywords=[
        "брось кубик",
        "кинь кубик",
        "подбрось кубик",
        "бросить кубик",
        "игральный кубик",
    ],
    description="Бросок шестигранного игрального кубика (1-6)",
    category="Развлечения",
)
def roll_dice(text: str):
    from app_state import get_state

    state = get_state()
    result = random.randint(1, 6)
    msg = f"Выпало число {result}."
    if state.is_muted:
        show_toast("Бросок кубика", msg)
    voice.speak(msg)


@skill(
    patterns=[
        r"назови число от (?P<start>.+?) до (?P<end>.+)",
        r"случайное число от (?P<start>.+?) до (?P<end>.+)",
        r"сгенерируй число от (?P<start>.+?) до (?P<end>.+)",
        r"число от (?P<start>.+?) до (?P<end>.+)",
    ],
    description="Генерация случайного числа в диапазоне (поддерживает цифры и слова, например: от пяти до двадцати)",
    category="Развлечения",
)
def random_number(text: str, start: str = "1", end: str = "100"):
    from app_state import get_state

    state = get_state()

    a = text_to_int(start)
    b = text_to_int(end)

    if a is None or b is None:
        voice.speak(
            "Не удалось определить диапазон чисел. Попробуйте сказать, например, 'назови число от пяти до тридцати' или 'от 1 до 100'."
        )
        return

    if a > b:
        a, b = b, a

    res = random.randint(a, b)
    msg = f"Случайное число: {res}."
    if state.is_muted:
        show_toast("Генератор чисел", msg)
    voice.speak(msg)


# ==========================================
# 2. Модуль принятия решений
# ==========================================


@skill(
    patterns=[
        r"что выбрать (?P<options>.+)",
        r"выбери между (?P<options>.+)",
        r"выбери (?P<options>.+)",
    ],
    description="Выбор случайного варианта из предложенных (например: что выбрать пиццу или бургер)",
    category="Развлечения",
)
def make_decision(text: str, options: str = ""):
    from app_state import get_state

    state = get_state()
    if not options:
        voice.speak("Укажите варианты для выбора, разделив их словом 'или'.")
        return

    # Разбиваем строку по слову "или" или по запятым
    raw_list = re.split(r"\s+или\s+|\s*,\s*", options, flags=re.IGNORECASE)
    choices = [item.strip(" ?.") for item in raw_list if item.strip(" ?.")]

    if len(choices) < 2:
        voice.speak("Пожалуйста, предложите хотя бы два варианта, используя слово 'или'.")
        return

    selected = random.choice(choices)
    phrases = [
        f"Я считаю, лучше выбрать {selected}.",
        f"Мой выбор — {selected}.",
        f"Однозначно {selected}!",
        f"Думаю, отличным вариантом будет {selected}.",
    ]
    ans = random.choice(phrases)

    if state.is_muted:
        show_toast("Выбор ассистента", ans)
    voice.speak(ans)


# ==========================================
# 3. Подсчет Uptime системы
# ==========================================


@skill(
    keywords=[
        "сколько работает компьютер",
        "время работы системы",
    ],
    description="Подсчет времени работы ПК с момента запуска (Uptime)",
    category="Система",
)
def get_system_uptime(text: str):
    from app_state import get_state

    state = get_state()
    if not psutil:
        voice.speak("Модуль psutil не установлен. Не могу посчитать время работы.")
        return

    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)

    hours = uptime_seconds // 3600
    minutes = (uptime_seconds % 3600) // 60

    if hours > 0:
        msg = f"Компьютер работает уже {hours} ч. и {minutes} мин."
    else:
        msg = f"Компьютер работает {minutes} мин."

    if state.is_muted:
        show_toast("Uptime системы", msg)
    voice.speak(msg)


# ==========================================
# 4. Системный ритуал «Доброе утро»
# ==========================================


@skill(
    keywords=[
        "доброе утро",
        "утренний ритуал",
        "запусти утренний сценарий",
        "утренний отчет",
    ],
    description="Приветствие, сводка системы и ежедневных дел",
    category="Сценарии",
)
def good_morning_ritual(text: str):
    from app_state import get_state

    state = get_state()

    # 1. Дата и время
    now = datetime.datetime.now()
    days = [
        "понедельник",
        "вторник",
        "среда",
        "четверг",
        "пятница",
        "суббота",
        "воскресенье",
    ]
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]

    day_name = days[now.weekday()]
    month_name = months[now.month - 1]
    time_str = now.strftime("%H:%M")

    greeting = f"Доброе утро! Сегодня {day_name}, {now.day} {month_name}. Время {time_str}."

    # 2. Информация о ресурсах системы
    sys_info = []
    if psutil:
        ram = psutil.virtual_memory().percent
        sys_info.append(f"Загрузка оперативной памяти {ram}%.")

        battery = psutil.sensors_battery()
        if battery:
            plugged = "подключен к сети" if battery.power_plugged else "работает от батареи"
            sys_info.append(f"Заряд аккумулятора {battery.percent}%, ноутбук {plugged}.")

    status_str = " ".join(sys_info)
    name = state.owner_name
    full_speech = (
        (f"{name}, " if name else "") + f"{greeting} {status_str} " + daily_briefing(state)
    )

    if state.is_muted:
        show_toast("Доброе утро!", full_speech)
    voice.speak(full_speech)


@skill(
    keywords=["привет", "здравствуй", "ты здесь", "крест на связи"],
    description="Поздороваться с Крест",
    category="Общение",
)
def greet(state):
    from personality import reply

    return reply("greeting", state)


@skill(
    keywords=["спасибо", "благодарю", "молодец"],
    description="Ответ на благодарность",
    category="Общение",
)
def thanks(state):
    from personality import reply

    return reply("thanks", state)


@skill(keywords=["кто ты", "расскажи о себе"], description="Знакомство с Крест", category="Общение")
def introduce(state):
    from personality import reply

    return reply("identity", state)


@skill(keywords=["как дела", "как ты"], description="Короткий ответ Крест", category="Общение")
def mood(state):
    from personality import reply

    return reply("mood", state)


@skill(
    keywords=["поддержи меня", "я устал", "я устала"],
    description="Несколько слов поддержки",
    category="Общение",
)
def support(state):
    from personality import reply

    return reply("support", state)


@skill(
    keywords=["расскажи шутку", "пошути"],
    description="Небольшая шутка из локального набора",
    category="Общение",
)
def joke(state):
    from personality import reply

    return reply("joke", state)


@skill(
    keywords=["как меня зовут", "мое имя", "моё имя"],
    description="Имя из вашего профиля",
    category="Общение",
)
def owner_identity(state):
    return (
        f"Вы попросили обращаться к вам: {state.owner_name}."
        if state.owner_name
        else "Имя пока не задано. Его можно указать в настройках."
    )


@skill(
    keywords=["план на сегодня", "мои ежедневные дела"],
    description="Ежедневные задачи и непрочитанные напоминания",
    category="Сценарии",
)
def daily_briefing(state):
    pending = state.reminders()
    tasks = list(state.daily_tasks)
    lines = [f"Ежедневных дел: {len(tasks)}. Непрочитанных напоминаний: {len(pending)}."]
    lines.extend(
        f"{datetime.datetime.fromtimestamp(t['end_time']).strftime('%H:%M')}: {t['label']}"
        for t in sorted(tasks, key=lambda x: x["end_time"])
    )
    return "\n".join(lines)
