"""Small offline tools with explicit inputs and no external side effects."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

from skill_manager import skill


def number(value):
    """Parse one decimal, never extract a plausible number from invalid input."""
    value = str(value).strip().replace(",", ".")
    if len(value) > 32 or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        raise ValueError("Введите число цифрами, например 12,5.")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Не удалось прочитать число.") from exc
    if abs(result) > Decimal("1000000000000"):
        raise ValueError("Допустимы числа по модулю до 1 000 000 000 000.")
    return result


def formatted(value, money=False):
    with localcontext() as context:
        context.prec = 48
        if money:
            value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            return format(value, ".2f").replace(".", ",")
        if not value:
            return "0"
        result = format(value, "f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        return result.replace(".", ",")


def error(message):
    return {"success": False, "message": message}


@skill(
    patterns=[r"(?:посчитай |сколько будет )?(?P<percent>\S+)\s*(?:%|процент(?:а|ов)?) от (?P<amount>.+)"],
    description="Процент от числа, без интернета",
    category="Расчёты", examples=["15 процентов от 2400"],
)
def percentage(percent, amount):
    try:
        rate, total = number(percent), number(amount)
        with localcontext() as context:
            context.prec = 80
            result = total * rate / 100
        return f"{formatted(rate)}% от {formatted(total)} = {formatted(result)}."
    except ValueError as exc:
        return error(str(exc))


@skill(
    patterns=[r"скидка (?P<percent>\S+)\s*(?:%|процент(?:а|ов)?) на (?P<amount>.+)"],
    description="Цена после скидки и сумма экономии",
    category="Расчёты", examples=["скидка 15 процентов на 2490"],
)
def discount(percent, amount):
    try:
        rate, total = number(percent), number(amount)
        if not 0 <= rate <= 100 or total < 0:
            return error("Скидка должна быть от 0 до 100%, цена — неотрицательной.")
        saving = (total * rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"После скидки: {formatted(total - saving, True)}. Экономия: {formatted(saving, True)}."
    except ValueError as exc:
        return error(str(exc))


@skill(
    patterns=[r"раздели сч[её]т (?P<amount>\S+) на (?P<people>\S+)(?: с чаевыми (?P<tip>\S+)\s*(?:%|процент(?:а|ов)?))?"],
    description="Разделить счёт поровну, с необязательными чаевыми",
    category="Расчёты", examples=["раздели счёт 3000 на 4 с чаевыми 10 процентов"],
)
def split_bill(amount, people, tip="0"):
    try:
        total, count, rate = number(amount), number(people), number(tip)
        if total < 0 or count != count.to_integral_value() or not 1 <= count <= 10000:
            return error("Сумма должна быть неотрицательной, число людей — целым от 1 до 10000.")
        if not 0 <= rate <= 100:
            return error("Чаевые должны быть от 0 до 100%.")
        total = (total * (1 + rate / 100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cents = int(total * 100)
        base, extra = divmod(cents, int(count))
        if not extra:
            shares = f"По {formatted(Decimal(base) / 100, True)} с человека."
        else:
            shares = (f"{extra} чел. по {formatted(Decimal(base + 1) / 100, True)}, "
                      f"{int(count) - extra} чел. по {formatted(Decimal(base) / 100, True)}.")
        return f"Всего с чаевыми: {formatted(total, True)}. {shares}"
    except ValueError as exc:
        return error(str(exc))


def calendar_date(value):
    value = value.strip().casefold()
    if value in ("сегодня", "завтра", "вчера"):
        return date.today() + timedelta(days={"сегодня": 0, "завтра": 1, "вчера": -1}[value])
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", value):
            day, month, year = map(int, value.split("."))
            return date(year, month, day)
    except ValueError:
        pass
    raise ValueError("Нужна существующая дата: 25.12.2026 или 2026-12-25; также понимаю сегодня, завтра, вчера.")


@skill(
    patterns=[r"сколько дней (?:между|с) (?P<start>\S+) (?:и|до) (?P<end>\S+)"],
    description="Разница двух календарных дат, без включения начального дня",
    category="Календарь", examples=["сколько дней между сегодня и 31.12.2026"],
)
def days_between(start, end):
    try:
        first, last = calendar_date(start), calendar_date(end)
        days = (last - first).days
        direction = "назад" if days < 0 else "вперёд"
        return (f"От {first:%d.%m.%Y} до {last:%d.%m.%Y}: {abs(days)} дн. {direction}. "
                "Начальный день не включён.")
    except ValueError as exc:
        return error(str(exc))


@skill(
    patterns=[r"(?:какой )?день недели (?P<value>\S+)"],
    description="День недели для указанной календарной даты",
    category="Календарь", examples=["какой день недели 01.01.2027"],
)
def weekday(value):
    try:
        target = calendar_date(value)
        name = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")[target.weekday()]
        return f"{target:%d.%m.%Y} — {name}."
    except ValueError as exc:
        return error(str(exc))


@skill(
    patterns=[r"дата через (?P<days>\S+) (?:день|дня|дней) от (?P<start>\S+)"],
    description="Прибавить или вычесть календарные дни из даты",
    category="Календарь", examples=["дата через 30 дней от 01.01.2027"],
)
def shift_date(days, start):
    try:
        count, first = number(days), calendar_date(start)
        if count != count.to_integral_value() or abs(count) > 3650000:
            return error("Нужно целое число дней по модулю до 3 650 000.")
        result = first + timedelta(days=int(count))
        return f"{first:%d.%m.%Y}, сдвиг {int(count):+d} дн.: {result:%d.%m.%Y}."
    except OverflowError:
        return error("Результат выходит за диапазон календаря: годы от 1 до 9999.")
    except ValueError as exc:
        return error(str(exc))
