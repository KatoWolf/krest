"""Local commands usable before voice models and integrations are configured."""

from datetime import datetime
import platform


def execute(text):
    text = text.strip().lower()
    if text.startswith("крест "):
        text = text[6:].strip()
    if text in ("время", "сколько времени"):
        return datetime.now().strftime("Сейчас %H:%M")
    if text in ("дата", "какая дата"):
        return datetime.now().strftime("Сегодня %d.%m.%Y")
    if text in ("статус", "система"):
        return f"КРЕСТ работает · {platform.system()} · Python {platform.python_version()}"
    if text in ("помощь", "команды"):
        return "Доступно: время, дата, статус, помощь. Голос и боты включаются отдельно."
    return "Неизвестная команда. Введите «помощь»."
