import os
import sys
import time
import re
import logging
from typing import Dict, Optional, Tuple, Any

# Ensure root project directory is in sys.path for skill_manager import

try:
    from skill_manager import skill  # type: ignore # pyright: ignore
except ImportError:
    from ..skill_manager import skill  # type: ignore # pyright: ignore

import voice

logger = logging.getLogger("SkillTimers")

stopwatch_start_time: Optional[float] = None

# Словарь числительных для перевода устной речи в числа
NUMBERS_DICT: Dict[str, int] = {
    "ноль": 0,
    "нуль": 0,
    "0": 0,
    "один": 1,
    "одна": 1,
    "одну": 1,
    "одно": 1,
    "1": 1,
    "два": 2,
    "две": 2,
    "2": 2,
    "три": 3,
    "3": 3,
    "четыре": 4,
    "4": 4,
    "пять": 5,
    "5": 5,
    "шесть": 6,
    "6": 6,
    "семь": 7,
    "7": 7,
    "восемь": 8,
    "8": 8,
    "девять": 9,
    "9": 9,
    "десять": 10,
    "10": 10,
    "одиннадцать": 11,
    "11": 11,
    "двенадцать": 12,
    "12": 12,
    "тринадцать": 13,
    "13": 13,
    "четырнадцать": 14,
    "14": 14,
    "пятнадцать": 15,
    "15": 15,
    "шестнадцать": 16,
    "16": 16,
    "семнадцать": 17,
    "17": 17,
    "восемнадцать": 18,
    "18": 18,
    "девятнадцать": 19,
    "19": 19,
    "двадцать": 20,
    "20": 20,
    "тридцать": 30,
    "30": 30,
    "сорок": 40,
    "40": 40,
    "пятьдесят": 50,
    "50": 50,
    "шестьдесят": 60,
    "60": 60,
    "семьдесят": 70,
    "70": 70,
    "восемьдесят": 80,
    "80": 80,
    "девяносто": 90,
    "90": 90,
    "сто": 100,
    "100": 100,
}

SPECIAL_DURATION_PHRASES: Dict[str, int] = {
    "полчаса": 1800,
    "пол-часа": 1800,
    "половину часа": 1800,
    "полтора часа": 5400,
    "полтора": 5400,
    "четверть часа": 900,
    "минутку": 60,
    "минуту": 60,
    "секундочку": 10,
    "секунду": 1,
}


def words_to_number(text_snippet: str) -> Optional[int]:
    """
    Преобразует составное число из слов (например: "двадцать пять" -> 25, "семь" -> 7).
    """
    clean_snippet = text_snippet.lower().strip()
    if not clean_snippet:
        return None

    # Если уже цифры
    if clean_snippet.isdigit():
        return int(clean_snippet)

    tokens = clean_snippet.split()
    total_val = 0
    found_any = False

    for token in tokens:
        if token in NUMBERS_DICT:
            total_val += NUMBERS_DICT[token]
            found_any = True
        elif token.isdigit():
            total_val += int(token)
            found_any = True

    return total_val if found_any else None


def parse_timer_duration(text):
    lowered = text.casefold().strip()
    for phrase, value in [
        ("полтора часа", "1.5 часа"),
        ("полторы минуты", "1.5 минуты"),
        ("полчаса", "30 минут"),
        ("пол-часа", "30 минут"),
        ("половину часа", "30 минут"),
        ("четверть часа", "15 минут"),
        ("минутку", "1 минуту"),
        ("секундочку", "1 секунду"),
    ]:
        lowered = lowered.replace(phrase, value)
    names = "|".join(
        sorted(
            (re.escape(k) for k in NUMBERS_DICT if not k.isdigit()),
            key=len,
            reverse=True,
        )
    )
    amount = rf"(?:\d+(?:[.,]\d+)?|(?:{names})(?:\s+(?:{names}))*)"
    pattern = rf"(?<![\w.-])(?P<n>{amount})\s*(?P<u>час(?:ов|а)?|ч|минут(?:а|ы|у)?|мин|секунд(?:а|ы|у)?|сек)(?!\w)"
    seconds = 0
    labels = []
    for match in re.finditer(pattern, lowered):
        value = match["n"]
        number = (
            float(value.replace(",", "."))
            if re.fullmatch(r"\d+(?:[.,]\d+)?", value)
            else words_to_number(value)
        )
        if number is None:
            continue
        unit = match["u"]
        multiplier = 3600 if unit.startswith("ч") else 60 if unit.startswith("мин") else 1
        seconds += int(number * multiplier)
        labels.append(f"{number:g} " + {3600: "ч.", 60: "мин.", 1: "сек."}[multiplier])
    if not labels:
        stripped = re.sub(
            r"^(?:поставь |включи |запусти |засеки )?(?:таймер )?(?:на )?", "", lowered
        ).strip()
        value = (
            words_to_number(stripped)
            if all(w in NUMBERS_DICT or w.isdigit() for w in stripped.split())
            else None
        )
        if value:
            return value * 60, f"{value} мин."
    if re.search(r"-\s*\d", lowered):
        return 0, ""
    return seconds, " ".join(labels)


def parse_alarm_time(text):
    lowered = text.casefold().strip()
    match = re.search(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)", lowered)
    if match:
        hours, minutes = map(int, match.groups())
    else:
        cleaned = re.sub(
            r"\b(поставь|включи|заведи|установи|будильник|на|в|утра|ночи|дня|вечера|часов|час)\b",
            "",
            lowered,
        )
        tokens = cleaned.split()
        if not tokens or any(t not in NUMBERS_DICT and not t.isdigit() for t in tokens):
            return None
        hours, minutes = None, 0
        splits = (
            [2, 1]
            if len(tokens) >= 2
            and tokens[0] == "двадцать"
            and (words_to_number(tokens[1]) or 0) <= 3
            else [1]
        )
        for split in splits:
            h = words_to_number(" ".join(tokens[:split]))
            m = words_to_number(" ".join(tokens[split:])) if tokens[split:] else 0
            if h is not None and m is not None and 0 <= h < 24 and 0 <= m < 60:
                hours, minutes = h, m
                break
        if hours is None:
            return None
    if ("вечера" in lowered or "дня" in lowered) and hours < 12:
        hours += 12
    elif ("утра" in lowered or "ночи" in lowered) and hours == 12:
        hours = 0
    return f"{hours:02}:{minutes:02}" if 0 <= hours < 24 and 0 <= minutes < 60 else None


@skill(
    keywords=[
        "поставь таймер",
        "включи таймер",
        "таймер на",
        "запусти таймер",
        "засеки",
    ],
    patterns=[r"поставь таймер на (?P<phrase>.+)", r"таймер на (?P<phrase>.+)"],
    description="Установка таймера обратного отсчета с поддержкой устных чисел (на 7 минут, на полчаса)",
    category="Timers",
)
def set_timer(text, phrase="", state=None):
    from app_state import get_state

    state = state or get_state()
    seconds, label = parse_timer_duration(phrase or text)
    if seconds <= 0:
        voice.speak("Укажите длительность, например: таймер на семь минут.")
        return False
    state.add_timer(seconds, label)
    voice.speak(f"Таймер запущен на {label}.")


@skill(
    keywords=[
        "сколько осталось на таймере",
        "статус таймера",
        "проверь таймер",
        "список таймеров",
        "какие таймеры",
    ],
    description="Проверка оставшегося времени по всем активным таймерам",
    category="Timers",
)
def check_active_timers(text="", state=None):
    from app_state import get_state

    state = state or get_state()
    reports = []
    for item in state.snapshot()["active_timers"]:
        remaining = max(0, int(item["end_time"] - time.time()))
        reports.append(f"{item['label']}: {remaining // 60} мин. {remaining % 60} сек.")
    voice.speak("; ".join(reports) or "Активных таймеров нет.")


@skill(
    keywords=[
        "отмени таймер",
        "сбрось таймеры",
        "удали таймер",
        "останови таймеры",
        "выключи таймер",
    ],
    description="Отмена и очистка всех активных таймеров",
    category="Timers",
)
def cancel_timers(text="", state=None):
    from app_state import get_state

    state = state or get_state()
    voice.speak(f"Отменено таймеров: {state.cancel('active_timers')}.")


@skill(
    keywords=[
        "поставь будильник",
        "заведи будильник",
        "включи будильник",
        "будильник на",
    ],
    patterns=[
        r"поставь будильник на (?P<alarm_time>.+)",
        r"заведи будильник на (?P<alarm_time>.+)",
        r"будильник на (?P<alarm_time>.+)",
    ],
    description="Планировщик срабатывания будильника (цифры или устная речь: 'восемь тридцать')",
    category="Timers",
)
def set_alarm(text, alarm_time="", state=None):
    from app_state import get_state

    state = state or get_state()
    formatted = parse_alarm_time(alarm_time or text)
    if formatted is None:
        voice.speak("Укажите время от 00:00 до 23:59, например: будильник на 8:30.")
        return False
    state.add_alarm(formatted)
    voice.speak(f"Будильник установлен на ближайшие {formatted}.")


@skill(
    keywords=[
        "проверь будильники",
        "список будильников",
        "какие будильники",
        "активные будильники",
    ],
    description="Просмотр списка всех установленных будильников",
    category="Timers",
)
def check_active_alarms(text="", state=None):
    from app_state import get_state

    state = state or get_state()
    alarms = state.snapshot()["active_alarms"]
    voice.speak(
        "Будильники: " + ", ".join(item["time_str"] for item in alarms)
        if alarms
        else "Активных будильников нет."
    )


@skill(
    keywords=[
        "отмени будильник",
        "сбрось будильники",
        "удали будильник",
        "выключи будильники",
    ],
    description="Отмена всех активных будильников",
    category="Timers",
)
def cancel_alarms(text="", state=None):
    from app_state import get_state

    state = state or get_state()
    voice.speak(f"Отменено будильников: {state.cancel('active_alarms')}.")


@skill(
    keywords=[
        "запусти секундомер",
        "старт секундомер",
        "включи секундомер",
        "начни отсчет",
    ],
    description="Запуск точного секундомера",
    category="Timers",
)
def start_stopwatch(text="", state=None):
    from app_state import get_state

    state = state or get_state()
    state.stopwatch_start_time = time.monotonic()
    voice.speak("Секундомер запущен.")


@skill(
    keywords=["сколько на секундомере", "статус секундомера", "проверь секундомер"],
    description="Проверка текущего промежуточного времени секундомера без его остановки",
    category="Timers",
)
def check_stopwatch(text="", state=None):
    from app_state import get_state

    state = state or get_state()
    if state.stopwatch_start_time is None:
        voice.speak("Секундомер не запущен.")
        return
    elapsed = int(time.monotonic() - state.stopwatch_start_time)
    voice.speak(f"На секундомере {elapsed // 60} мин. {elapsed % 60} сек.")


@skill(
    keywords=[
        "останови секундомер",
        "стоп секундомер",
        "сбрось секундомер",
        "выключи секундомер",
    ],
    description="Фиксация и остановка секундомера",
    category="Timers",
)
def stop_stopwatch(text="", state=None):
    from app_state import get_state

    state = state or get_state()
    check_stopwatch(state=state)
    state.stopwatch_start_time = None
