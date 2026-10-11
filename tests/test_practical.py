"""Offline skill results, honest failure modes, and actual registry routing."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app_state import AssistantState
from skill_manager import SkillManager
from skills import practical


@pytest.fixture
def manager(tmp_path):
    return SkillManager(plugin_dir=tmp_path / "plugins")


@pytest.mark.parametrize("command, expected, fragment", [
    ("15 процентов от 2400", "percentage", "= 360"),
    ("посчитай 12,5% от 80", "percentage", "= 10"),
    ("сколько будет 150 процентов от -20", "percentage", "= -30"),
    ("скидка 15 процентов на 2490", "discount", "2116,50"),
    ("раздели счёт 3000 на 4 с чаевыми 10 процентов", "split_bill", "825,00"),
    ("раздели счет 100 на 3", "split_bill", "1 чел. по 33,34, 2 чел. по 33,33"),
    ("сколько дней между 28.02.2024 и 01.03.2024", "days_between", "2 дн. вперёд"),
    ("сколько дней с 2026-01-01 до 2025-12-31", "days_between", "1 дн. назад"),
    ("какой день недели 01.01.2027", "weekday", "пятница"),
    ("дата через 30 дней от 01.01.2027", "shift_date", "31.01.2027"),
])
def test_real_dispatch(manager, tmp_path, command, expected, fragment):
    result = manager.execute(command, state=AssistantState(tmp_path / "state.json"))
    assert result.success, result.text
    assert result.skill_id == "skills.practical." + expected
    assert fragment in result.text


@pytest.mark.parametrize("command", [
    "скидка 101 процентов на 20", "скидка -1% на 20", "скидка 10% на -20",
    "раздели счет 100 на 0", "раздели счет 100 на 2,5",
    "раздели счет 100 на 10001", "раздели счет 100 на 2 с чаевыми -5%",
    "раздели счет -100 на 2", "десять процентов от 20",
    "15% от NaN", "15% от 12 рублей", "15% от 1000000000001",
    "какой день недели 29.02.2023", "какой день недели 31.04.2026",
    "какой день недели 1/2/26", "сколько дней между сегодня и никогда",
    "дата через 2,5 дня от сегодня", "дата через 1 день от 9999-12-31",
])
def test_invalid_input_is_not_reported_as_success(manager, tmp_path, command):
    result = manager.execute(command, state=AssistantState(tmp_path / "state.json"))
    assert result.handled
    assert not result.success
    assert result.text and "Ошибка команды:" not in result.text


def test_bill_rounding_preserves_total():
    assert "1 чел. по 0,01, 2 чел. по 0,00" in practical.split_bill("0.01", "3")
    assert "Всего с чаевыми: 11,06" in practical.split_bill("10.05", "1", "10")
    assert "По 0,00" in practical.split_bill("0", "2")


def test_large_percentage_stays_finite_and_exact():
    assert "10000000000000000000000" in practical.percentage("1000000000000", "1000000000000")
    assert practical.formatted(Decimal("-0.00000001")) == "-0,00000001"
    assert "= 0,0000000001" in practical.percentage("0.00001", "0.001")


def test_relative_calendar_dates():
    today = date.today()
    assert practical.calendar_date("сегодня") == today
    assert practical.calendar_date("завтра") == today + timedelta(days=1)
    assert practical.calendar_date("вчера") == today - timedelta(days=1)


def test_calendar_shift_crosses_leap_day_and_year():
    assert "29.02.2024" in practical.shift_date("1", "28.02.2024")
    assert "31.12.2025" in practical.shift_date("-1", "01.01.2026")
    assert not practical.shift_date("-1", "0001-01-01")["success"]
    assert not practical.shift_date("3650001", "сегодня")["success"]


@pytest.mark.parametrize("command, name", [
    ("посчитай 12 * 8", "calc_expression_skill"),
    ("переведи 2 километра в метры", "convert_currency_or_units"),
    ("дата", "basic"), ("таймер на 10 минут", "set_timer"),
])
def test_existing_commands_keep_routes(manager, command, name):
    assert manager.match(command)[0].func.__name__ == name


def test_negation_and_narration_never_execute(manager):
    assert manager.match("не раздели счет 100 на 3") is None
    assert manager.match("он сказал скидка 15 процентов на 2490") is None


def test_compiled_manifest_includes_practical():
    from skills import BUILTIN_MODULES
    assert "practical" in BUILTIN_MODULES


@pytest.mark.parametrize("command, fragment", [
    ("переведи -10 цельсия в фаренгейт", "14 по Фаренгейту"),
    ("переведи 1,5 километра в метры", "1500"),
    ("переведи 1,5км в м", "1500"),
    ("переведи двадцать пять километров в метры", "25000"),
    ("переведи минус пять цельсия в фаренгейт", "23 по Фаренгейту"),
    ("посчитай 1,5 плюс 2,5", "4"),
])
def test_signed_fractional_and_spoken_unit_conversion(manager, tmp_path, command, fragment):
    result = manager.execute(command, state=AssistantState(tmp_path / "state.json"))
    assert result.success, result.text
    assert fragment in result.text


@pytest.mark.parametrize("value", ["", "неизвестно", "NaN", "бесконечность", "12 рублей", "2+3"])
def test_conversion_never_silently_defaults_to_one(value):
    from skills.utils import parse_number_from_text
    with pytest.raises(ValueError):
        parse_number_from_text(value)
