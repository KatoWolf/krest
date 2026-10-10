import os
import sys
import time
import ctypes
import json
import re
import math
import ast
import logging
import urllib.request
import urllib.parse

from skill_manager import skill  # type: ignore # pyright: ignore

import voice

logger = logging.getLogger("SkillUtils")

# ==========================================
# 1. Низкоуровневая работа с буфером обмена (Win32 RAM API)
# ==========================================

CF_UNICODETEXT = 13


def get_clipboard_text():
    try:
        import pyperclip

        return pyperclip.paste()
    except Exception:
        return ""


def set_clipboard_text(text):
    try:
        import pyperclip

        pyperclip.copy(text)
        return True
    except Exception:
        return False


def clear_clipboard_ram():
    return set_clipboard_text("")


# ==========================================
# 2. Менеджер шаблонов с сохранением на диск (Оффлайн JSON)
# ==========================================

TEMPLATES_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates.json"))

DEFAULT_TEMPLATES = {
    "почта": "user@example.com",
    "email": "user@example.com",
    "привет": "Здравствуйте! Надеюсь, у вас отличный день.",
    "приветствие": "Добрый день! Рад нашему общению.",
    "код": "if __name__ == '__main__':\n    main()",
    "подпись": "С уважением,\nКоманда разработки",
    "телефон": "+7 (700) 000-00-00",
    "дискорд": "https://discord.gg/invite",
}


def load_templates() -> dict:
    """Загружает шаблоны из локального JSON файла или создает базовые."""
    if os.path.exists(TEMPLATES_FILE):
        try:
            with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in data.items()
                ):
                    raise ValueError("Некорректный файл шаблонов")
                return {**DEFAULT_TEMPLATES, **data}
        except Exception as e:
            logger.error(f"Ошибка загрузки шаблонов: {e}")
    return DEFAULT_TEMPLATES.copy()


def save_templates(templates: dict) -> bool:
    """Сохраняет текущие шаблоны в локальный JSON файл."""
    try:
        from storage import atomic_write_json

        atomic_write_json(TEMPLATES_FILE, templates)
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения шаблонов в файл: {e}")
        return False


# ==========================================
# 3. Оффлайн-переводчик, словарь и транслитерация
# ==========================================

CYR_TO_LAT_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "А": "A",
    "Б": "B",
    "В": "V",
    "Г": "G",
    "Д": "D",
    "Е": "E",
    "Ё": "Yo",
    "Ж": "Zh",
    "З": "Z",
    "И": "I",
    "Й": "Y",
    "К": "K",
    "Л": "L",
    "М": "M",
    "Н": "N",
    "О": "O",
    "П": "P",
    "Р": "R",
    "С": "S",
    "Т": "T",
    "У": "U",
    "Ф": "F",
    "Х": "Kh",
    "Ц": "Ts",
    "Ч": "Ch",
    "Ш": "Sh",
    "Щ": "Shch",
    "Ъ": "",
    "Ы": "Y",
    "Ь": "",
    "Э": "E",
    "Ю": "Yu",
    "Я": "Ya",
}

OFFLINE_DICTIONARY = {
    # EN -> RU
    "hello": "привет",
    "world": "мир",
    "error": "ошибка",
    "warning": "предупреждение",
    "success": "успех",
    "user": "пользователь",
    "password": "пароль",
    "system": "система",
    "download": "скачать",
    "file": "файл",
    "folder": "папка",
    "settings": "настройки",
    "cancel": "отмена",
    "save": "сохранить",
    "delete": "удалить",
    "search": "поиск",
    "help": "помощь",
    "status": "статус",
    "time": "время",
    "date": "дата",
    # RU -> EN
    "привет": "hello",
    "мир": "world",
    "ошибка": "error",
    "успех": "success",
    "пароль": "password",
    "система": "system",
    "файл": "file",
    "настройки": "settings",
}


def transliterate_text(text: str) -> str:
    """Преобразует кириллицу в латиницу офлайн."""
    res = []
    for char in text:
        res.append(CYR_TO_LAT_MAP.get(char, char))
    return "".join(res)


def offline_translate_fallback(text: str, target_lang: str) -> str:
    """Пословный и фразовый перевод на основе локального словаря без доступа в интернет."""
    words = re.findall(r"\b\w+\b", text)
    if not words:
        return text

    translated_words = []
    for word in words:
        word_lower = word.lower()
        if word_lower in OFFLINE_DICTIONARY:
            translated_words.append(OFFLINE_DICTIONARY[word_lower])
        else:
            translated_words.append(word)

    result = " ".join(translated_words)
    if result.lower() == text.lower():
        # Если совпадений в словаре нет, делаем читаемую транслитерацию
        return transliterate_text(text)
    return result


# ==========================================
# 4. Оффлайн-конвертер валют и физических величин
# ==========================================

RATES_CACHE_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "currency_rates.json")
)

DEFAULT_CURRENCY_RATES = {
    "тенге": 1.0,
    "kzt": 1.0,
    "тг": 1.0,
    "доллар": 480.0,
    "долларов": 480.0,
    "доллара": 480.0,
    "usd": 480.0,
    "$": 480.0,
    "евро": 520.0,
    "eur": 520.0,
    "€": 520.0,
    "рубль": 5.3,
    "рублей": 5.3,
    "рубля": 5.3,
    "rub": 5.3,
    "₽": 5.3,
    "юань": 67.0,
    "юаней": 67.0,
    "cny": 67.0,
    "фунт": 610.0,
    "фунтов": 610.0,
    "gbp": 610.0,
    "лира": 14.0,
    "лир": 14.0,
    "try": 14.0,
}


def update_rates_cache_online():
    from storage import atomic_write_json

    cached = {}
    try:
        with open(RATES_CACHE_FILE, encoding="utf-8") as stream:
            cached = json.load(stream)
    except (ValueError, OSError):
        pass
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("rates"), dict)
        and time.time() - cached.get("updated_at", 0) < 21600
    ):
        return cached
    try:
        request = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD", headers={"User-Agent": "Krest/0.3"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.load(response)
        rates = data.get("rates", {})
        if data.get("result") != "success" or not isinstance(rates, dict) or not rates:
            raise ValueError("Сервис курсов вернул неполные данные")
        if any(
            not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
            for v in rates.values()
        ):
            raise ValueError("Некорректный курс валюты")
        cached = {
            "rates": rates,
            "updated_at": time.time(),
            "date": data.get("time_last_update_utc", ""),
        }
        atomic_write_json(RATES_CACHE_FILE, cached)
        return cached
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("rates"), dict)
            and cached.get("updated_at")
        ):
            return dict(cached, stale=True)
        raise RuntimeError("Не удалось получить курс валют. Проверьте подключение к сети.") from exc


ALL_NUMBER_WORDS = {
    # Именительный падеж и варианты
    "ноль": 0,
    "нуль": 0,
    "0": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "единица": 1,
    "одного": 1,
    "одной": 1,
    "1": 1,
    "два": 2,
    "две": 2,
    "двойка": 2,
    "двух": 2,
    "2": 2,
    "три": 3,
    "тройка": 3,
    "трех": 3,
    "трёх": 3,
    "3": 3,
    "четыре": 4,
    "четверка": 4,
    "четырех": 4,
    "четырёх": 4,
    "4": 4,
    "пять": 5,
    "пятерка": 5,
    "пяти": 5,
    "5": 5,
    "шесть": 6,
    "шестерка": 6,
    "шести": 6,
    "6": 6,
    "семь": 7,
    "семерка": 7,
    "семи": 7,
    "7": 7,
    "восемь": 8,
    "восьмерка": 8,
    "восьми": 8,
    "8": 8,
    "девять": 9,
    "девятка": 9,
    "девяти": 9,
    "9": 9,
    "десять": 10,
    "десяти": 10,
    "10": 10,
    "одиннадцать": 11,
    "одиннадцати": 11,
    "11": 11,
    "двенадцать": 12,
    "двенадцати": 12,
    "12": 12,
    "тринадцать": 13,
    "тринадцати": 13,
    "13": 13,
    "четырнадцать": 14,
    "четырнадцати": 14,
    "14": 14,
    "пятнадцать": 15,
    "пятнадцати": 15,
    "15": 15,
    "шестнадцать": 16,
    "шестнадцати": 16,
    "16": 16,
    "семнадцать": 17,
    "семнадцати": 17,
    "17": 17,
    "восемнадцать": 18,
    "восемнадцати": 18,
    "18": 18,
    "девятнадцать": 19,
    "девятнадцати": 19,
    "19": 19,
    "двадцать": 20,
    "двадцати": 20,
    "20": 20,
    "тридцать": 30,
    "тридцати": 30,
    "30": 30,
    "сорок": 40,
    "сорока": 40,
    "40": 40,
    "пятьдесят": 50,
    "пятидесяти": 50,
    "50": 50,
    "шестьдесят": 60,
    "шестидесяти": 60,
    "60": 60,
    "семьдесят": 70,
    "семьдесяти": 70,
    "70": 70,
    "восемьдесят": 80,
    "восьмидесяти": 80,
    "80": 80,
    "девяносто": 90,
    "девяноста": 90,
    "90": 90,
    "сто": 100,
    "ста": 100,
    "100": 100,
    "двести": 200,
    "двухсот": 200,
    "200": 200,
    "триста": 300,
    "трехсот": 300,
    "300": 300,
    "четыреста": 400,
    "четырехсот": 400,
    "400": 400,
    "пятьсот": 500,
    "пятисот": 500,
    "500": 500,
    "шестьсот": 600,
    "шестисот": 600,
    "600": 600,
    "семьсот": 700,
    "семисот": 700,
    "700": 700,
    "восемьсот": 800,
    "восьмисот": 800,
    "800": 800,
    "девятьсот": 900,
    "девятисот": 900,
    "900": 900,
}

MULTIPLIER_WORDS = {
    "тысяча": 1000,
    "тысячи": 1000,
    "тысяч": 1000,
    "тысячам": 1000,
    "миллион": 1000000,
    "миллиона": 1000000,
    "миллионов": 1000000,
}

DECIMAL_WORDS = {"точка", "запятая", "целых", "целая"}


def russian_words_to_number_str(text: str) -> str:
    """
    Преобразует словесные числительные и математические термины на русском языке
    в цифры и стандартные математические операторы.
    Примеры:
      'семь плюс пять' -> '7 + 5'
      'двадцать три умножить на четыре' -> '23 * 4'
      'сто пятьдесят разделить на три точка пять' -> '150 / 3.5'
      'корень из шестнадцати' -> 'sqrt(16)'
    """
    if not text:
        return ""

    expr = text.lower()

    # Замена устных формулировок математических операций
    expr = re.sub(r"\bквадратный\s+корень\s+из\b", " sqrt ", expr)
    expr = re.sub(r"\bкорень\s+из\b", " sqrt ", expr)
    expr = re.sub(r"\bв\s+квадрате\b", " ** 2 ", expr)
    expr = re.sub(r"\bв\s+кубе\b", " ** 3 ", expr)
    expr = re.sub(
        r"\bумножить\s+на\b|\bумноженное\s+на\b|\bумноженная\s+на\b|\bумножить\b",
        " * ",
        expr,
    )
    expr = re.sub(
        r"\bразделить\s+на\b|\bподелить\s+на\b|\bделенное\s+на\b|\bразделить\b|\bподелить\b",
        " / ",
        expr,
    )
    expr = re.sub(r"\bприбавить\s+к\b|\bприбавить\b|\bплюс\b", " + ", expr)
    expr = re.sub(r"\bвычесть\s+из\b|\bвычесть\b|\bотнять\b|\bминус\b", " - ", expr)
    expr = re.sub(r"\bв\s+степени\b|\bв\s+степень\b|\bстепень\b", " ** ", expr)
    expr = re.sub(r"\bостаток\s+от\s+деления\s+на\b", " % ", expr)

    # Токенизация выражения
    tokens = re.findall(r"\d+(?:\.\d+)?|[a-zA-Aа-яА-ЯёЁ]+|[\+\-\*\/\(\)\^\%]", expr)

    result_tokens = []
    i = 0
    n = len(tokens)

    while i < n:
        token = tokens[i]
        is_num_word = token in ALL_NUMBER_WORDS or token in MULTIPLIER_WORDS or token.isdigit()

        if is_num_word:
            current_sum = 0
            temp_val = 0

            while i < n:
                tok = tokens[i]
                if tok in ALL_NUMBER_WORDS:
                    temp_val += ALL_NUMBER_WORDS[tok]
                    i += 1
                elif tok.isdigit():
                    temp_val += int(tok)
                    i += 1
                elif tok in MULTIPLIER_WORDS:
                    mult = MULTIPLIER_WORDS[tok]
                    if temp_val == 0:
                        temp_val = 1
                    current_sum += temp_val * mult
                    temp_val = 0
                    i += 1
                else:
                    break

            number_value = current_sum + temp_val

            # Проверка на наличие десятичного разделителя ("семь точка пять")
            if i < n and tokens[i] in DECIMAL_WORDS:
                i += 1  # пропускаем разделитель
                frac_str = ""
                while i < n and (tokens[i] in ALL_NUMBER_WORDS or tokens[i].isdigit()):
                    sub_tok = tokens[i]
                    if sub_tok in ALL_NUMBER_WORDS:
                        frac_str += str(ALL_NUMBER_WORDS[sub_tok])
                    elif sub_tok.isdigit():
                        frac_str += sub_tok
                    i += 1
                if frac_str:
                    result_tokens.append(f"{number_value}.{frac_str}")
                else:
                    result_tokens.append(str(number_value))
            else:
                result_tokens.append(str(number_value))
        else:
            result_tokens.append(token)
            i += 1

    return " ".join(result_tokens)


def parse_number_from_text(text_val: str) -> float:
    """Преобразует текстовое или словесное числовое значение в float."""
    clean = text_val.strip().lower()
    converted = russian_words_to_number_str(clean)
    match = re.search(r"\d+(?:\.\d+)?", converted)
    if match:
        try:
            return float(match.group(0))
        except ValueError:
            pass
    return 1.0


# ==========================================
# 5. Оффлайн-калькулятор выражений
# ==========================================


def safe_eval_math(expression: str) -> str:
    """Безопасное офлайн вычисление математических выражений с парсингом русской речи."""
    if not expression or not expression.strip() or len(expression) > 512:
        return ""

    # 1. Преобразуем числительные слова и фразы в цифровые операторы
    parsed_expr = russian_words_to_number_str(expression)

    # 2. Оставляем только допустимые математические символы, операторы и вызовы sqrt
    allowed_expr = re.sub(r"[^0-9\+\-\*\/\(\)\.\,\^\%\s\w]", "", parsed_expr)
    allowed_expr = allowed_expr.replace("^", "**")

    if not allowed_expr.strip():
        return ""

    try:
        node = ast.parse(allowed_expr, mode="eval")
        if sum(1 for _ in ast.walk(node)) > 100:
            raise ValueError("Выражение слишком сложное")

        def _eval_node(n):
            if isinstance(n, ast.Expression):
                return _eval_node(n.body)
            elif isinstance(n, ast.Constant):
                if type(n.value) not in (int, float) or abs(n.value) > 10**12:
                    raise ValueError("Число вне допустимого диапазона")
                return n.value
            elif isinstance(n, ast.BinOp):
                left = _eval_node(n.left)
                right = _eval_node(n.right)
                if isinstance(n.op, ast.Add):
                    return left + right
                elif isinstance(n.op, ast.Sub):
                    return left - right
                elif isinstance(n.op, ast.Mult):
                    return left * right
                elif isinstance(n.op, ast.Div):
                    return left / right if right != 0 else float("inf")
                elif isinstance(n.op, ast.Pow):
                    if abs(right) > 100 or abs(left) > 10**12:
                        raise ValueError("Слишком большая степень")
                    return left**right
                elif isinstance(n.op, ast.Mod):
                    return left % right
                elif isinstance(n.op, ast.FloorDiv):
                    return left // right if right != 0 else float("inf")
            elif isinstance(n, ast.UnaryOp):
                operand = _eval_node(n.operand)
                if isinstance(n.op, ast.UAdd):
                    return +operand
                elif isinstance(n.op, ast.USub):
                    return -operand
            elif isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    func_name = n.func.id.lower()
                    args = [_eval_node(arg) for arg in n.args]
                    if func_name == "sqrt" and len(args) == 1:
                        return math.sqrt(args[0])
                    elif func_name == "abs" and len(args) == 1:
                        return abs(args[0])
                    elif func_name == "round":
                        if len(args) == 1:
                            return round(args[0])
                        elif len(args) == 2:
                            return round(args[0], int(args[1]))
            raise ValueError("Неподдерживаемая операция")

        res = _eval_node(node)
        if math.isinf(res):
            return "Деление на ноль невозможно."

        if isinstance(res, float) and res.is_integer():
            res = int(res)
        elif isinstance(res, float):
            res = round(res, 4)

        return str(res)
    except Exception as e:
        logger.debug(f"Math parse error: {e}")
        return ""


# ==========================================
# 6. Голосовые навыки (Skills)
# ==========================================


@skill(
    keywords=[
        "посчитай буфер",
        "вычисли буфер",
        "реши из буфера",
        "посчитай выражение",
        "посчитай",
        "вычисли",
        "реши",
        "сколько будет",
    ],
    patterns=[r"(?:посчитай(?: выражение)?|вычисли|реши|сколько будет) (?P<expression>.+)"],
    examples=["посчитай 12 * 8"],
    description="Автономное вычисление математического выражения с поддержкой распознавания русской речи",
    category="Clipboard",
)
def calc_expression_skill(text: str):
    content = get_clipboard_text() if "буфер" in text.lower() else ""

    # Очищаем команду от префиксов ("сколько будет", "посчитай", "реши" и т.д.)
    expr_candidate = text.lower()
    for kw in [
        "посчитай выражение",
        "посчитай буфер",
        "реши из буфера",
        "вычисли буфер",
        "посчитай",
        "вычисли",
        "реши",
        "сколько будет",
        "крест",
    ]:
        expr_candidate = expr_candidate.replace(kw, "").strip()

    result = ""
    if expr_candidate:
        result = safe_eval_math(expr_candidate)

    if not result and content:
        result = safe_eval_math(content)

    if result:
        voice.speak(f"Результат вычисления: {result}.")
        if "буфер" in text.lower():
            set_clipboard_text(str(result))
    else:
        voice.speak("Не удалось распознать математическое выражение.")


@skill(
    patterns=[
        r"сколько будет (?P<amount>[\d\.\,]+|\w+)\s*(?P<from_curr>\w+)\s*в\s*(?P<to_curr>\w+)",
        r"переведи (?P<amount>[\d\.\,]+|\w+)\s*(?P<from_curr>\w+)\s*в\s*(?P<to_curr>\w+)",
    ],
    description="Автономный конвертер валют, длины, массы и объемов данных",
    category="Clipboard",
)
def convert_currency_or_units(text, amount="1", from_curr="", to_curr=""):
    value = parse_number_from_text(amount)
    if not math.isfinite(value):
        raise ValueError("Введите конечное число")

    def unit(name):
        name = name.lower().strip()
        aliases = [
            ("length", 1000, ("км", "километр")),
            ("length", 0.01, ("см", "сантиметр")),
            ("length", 0.001, ("мм", "миллиметр")),
            ("length", 1, ("м", "метр")),
            ("mass", 1000, ("кг", "килограмм")),
            ("mass", 1, ("г", "грамм")),
            ("mass", 453.59237, ("фунт",)),
            ("data", 1024**3, ("гб", "гигабайт")),
            ("data", 1024**2, ("мб", "мегабайт")),
            ("data", 1024**4, ("тб", "терабайт")),
            ("data", 1024, ("кб", "килобайт")),
            ("data", 1, ("байт",)),
        ]
        for group, factor, names in aliases:
            if any(name == alias or len(alias) > 2 and name.startswith(alias) for alias in names):
                return group, factor
        return None

    if "цельси" in from_curr.casefold() and "фаренгейт" in to_curr.casefold():
        voice.speak(f"{value:g} градусов Цельсия — это {value * 9 / 5 + 32:g} по Фаренгейту.")
        return
    if "фаренгейт" in from_curr.casefold() and "цельси" in to_curr.casefold():
        voice.speak(f"{value:g} по Фаренгейту — это {(value - 32) * 5 / 9:g} градусов Цельсия.")
        return
    left, right = unit(from_curr), unit(to_curr)
    if left and right and left[0] == right[0]:
        result = value * left[1] / right[1]
        voice.speak(f"{amount} {from_curr} — это {result:g} {to_curr}.")
        return
    currencies = {
        "USD": ("usd", "доллар", "доллара", "долларов"),
        "EUR": ("eur", "евро"),
        "RUB": ("rub", "рубль", "рубля", "рублей"),
        "KZT": ("kzt", "тенге", "тг"),
        "CNY": ("cny", "юань", "юаня", "юаней"),
        "GBP": ("gbp", "фунт", "фунтов"),
        "TRY": ("try", "лира", "лир", "лиры"),
    }
    source = next(
        (code for code, names in currencies.items() if from_curr.casefold() in names),
        None,
    )
    target = next(
        (code for code, names in currencies.items() if to_curr.casefold() in names),
        None,
    )
    if not source or not target:
        voice.speak("Неизвестные или несовместимые единицы измерения.")
        return False
    cached = update_rates_cache_online()
    rates = cached["rates"]
    if source not in rates or target not in rates:
        raise ValueError("Сервис не вернул курс выбранной валюты")
    result = round(value / rates[source] * rates[target], 2)
    date = time.strftime("%d.%m.%Y %H:%M", time.localtime(cached["updated_at"]))
    prefix = "Сохранённый курс" if cached.get("stale") else "Курс"
    voice.speak(f"{amount} {from_curr} — это {result:g} {to_curr}. {prefix} от {date}.")
