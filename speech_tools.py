"""Small deterministic speech helpers; no models or audio devices on import."""

from array import array
import math
import re
import sys


def pcm_levels(data):
    samples = array("h")
    samples.frombytes(data[: len(data) - len(data) % 2])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0, 0
    return math.sqrt(sum(v * v for v in samples) / len(samples)), max(abs(v) for v in samples)


class Endpoint:
    def __init__(self, threshold=200, silence_ms=700, max_seconds=20):
        self.threshold = threshold
        self.pause = silence_ms / 1000
        self.max_seconds = max_seconds
        self.reset()

    def reset(self):
        self.started = False
        self.silence = 0
        self.elapsed = 0

    def feed(self, rms, seconds):
        if rms >= self.threshold:
            self.started = True
            self.silence = 0
        elif self.started:
            self.silence += seconds
        if self.started:
            self.elapsed += seconds
        return self.started and (
            self.silence + 1e-9 >= self.pause or self.elapsed >= self.max_seconds
        )


def split_wake(text, wake_word):
    match = re.match(r"\s*([\w]+)[\s,.:!?-]*(.*)", text, re.S)
    if match and match[1].casefold().replace("ё", "е") == wake_word.casefold().replace("ё", "е"):
        return True, match[2].strip()
    return False, text


def normalize_command(text, aliases=None):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^пожалуйста[, ]+|[, ]+пожалуйста[.!?]*$", "", text, flags=re.I)
    key = text.casefold().strip(" .!?,")
    if key in (aliases or {}):
        return aliases[key]
    fixed = {
        "сделай музыку потише": "тише",
        "сделай потише": "тише",
        "потише музыку": "тише",
        "сделай музыку погромче": "громче",
        "сделай погромче": "громче",
        "поставь музыку на паузу": "пауза",
        "поставь на паузу": "пауза",
        "открой контру": "запусти игру counter-strike 2",
        "запусти контру": "запусти игру counter-strike 2",
        "открой доту": "запусти игру dota 2",
        "который час": "время",
    }
    if key in fixed:
        return fixed[key]
    # Capture the task verbatim; corrections never rewrite reminder contents.
    match = re.fullmatch(r"напомни\s+(?!через\b)(.+?)\s+через\s+(.+)", text, re.I)
    if match:
        from skills.timers_alarms import parse_timer_duration

        seconds, _ = parse_timer_duration(match[2])
        if seconds > 0:
            return f"напомни через {match[2]}: {match[1]}"
    return text


def spoken_summary(text, limit=350):
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    boundary = max(prefix.rfind(". "), prefix.rfind("! "), prefix.rfind("? "))
    if boundary > 40:
        prefix = prefix[: boundary + 1]
    else:
        prefix = prefix.rsplit(" ", 1)[0].rstrip(" ,;:") + "."
    return prefix + " Полный ответ — в меню."


def verbalize_numbers(text):
    """Expand time/units before generic TTS digit expansion."""

    def clock(match):
        hours, minutes = map(int, match.groups())
        if hours > 23 or minutes > 59:
            return match[0]

        def unit(n, forms):
            return (
                forms[2]
                if 11 <= n % 100 <= 14
                else forms[0]
                if n % 10 == 1
                else forms[1]
                if 2 <= n % 10 <= 4
                else forms[2]
            )

        return f"{hours} {unit(hours, ('час', 'часа', 'часов'))} {minutes} {unit(minutes, ('минута', 'минуты', 'минут'))}"

    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", clock, text)
    for short, full in (
        ("ГБ", "гигабайт"),
        ("МБ", "мегабайт"),
        ("ГГц", "гигагерц"),
        ("FPS", "кадров в секунду"),
    ):
        text = re.sub(r"(?<=\d)\s*" + short + r"\b", " " + full, text, flags=re.I)
    return text
