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
    text = re.sub(r"^\s*(?:эй|слушай)[, ]+", "", text, flags=re.I)
    match = re.match(r"\s*([\w]+)[\s,.:!?-]*(.*)", text, re.S)
    if match and match[1].casefold().replace("ё", "е") == wake_word.casefold().replace("ё", "е"):
        return True, match[2].strip()
    return False, text


def normalize_command(text, aliases=None):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^пожалуйста[, ]+|[, ]+пожалуйста[.!?]*$", "", text, flags=re.I)
    key = text.casefold().strip(" .!?,")
    if key in (aliases or {}):
        return normalize_command(aliases[key])
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
    # Only anchored templates rewrite commands; task contents stay verbatim.
    match = re.fullmatch(
        r"(?:можешь |можно )?(?:поставить|поставь|заведи|установи) (.+?) таймер", text, re.I
    )
    if match:
        return "таймер на " + match[1].removeprefix("на ")
    match = re.fullmatch(
        r"(?:звук|громкость)(?: музыки)? (?:сделай |поставь |установи )?(?:на )?(\d{1,3})(?: процентов|%)?",
        text,
        re.I,
    )
    if match:
        return "громкость на " + match[1]
    match = re.fullmatch(r"(?:через) (.+?) напомни (.+)", text, re.I)
    if match:
        from skills.timers_alarms import parse_timer_duration

        if parse_timer_duration(match[1])[0] > 0:
            return f"напомни через {match[1]}: {match[2]}"
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

    def quantity(match):
        number, suffix = match.groups()
        forms = (
            ("процент", "процента", "процентов")
            if suffix == "%"
            else ("градус", "градуса", "градусов")
        )
        if "." in number or "," in number:
            unit = forms[1]
        else:
            value = int(number)
            unit = (
                forms[2]
                if 11 <= value % 100 <= 14
                else forms[0]
                if value % 10 == 1
                else forms[1]
                if 2 <= value % 10 <= 4
                else forms[2]
            )
        return (
            number
            + " "
            + unit
            + (" Цельсия" if "c" in suffix.lower() or "с" in suffix.lower() else "")
        )

    text = re.sub(r"(\d+(?:[.,]\d+)?)\s*(%|°\s*[CСcс]?)", quantity, text)
    text = re.sub(r"(?<!\w)[−-](?=\d)", "минус ", text)
    for short, full in (
        ("ГБ", "гигабайт"),
        ("МБ", "мегабайт"),
        ("ГГц", "гигагерц"),
        ("FPS", "кадров в секунду"),
    ):
        text = re.sub(r"(?<=\d)\s*" + short + r"\b", " " + full, text, flags=re.I)
    return text


def speech_chunks(text, limit=240):
    """Keep full sentences when possible and bound long unpunctuated replies."""
    pending = ""
    for sentence in re.split(r"(?<=[.!?;])\s+", text.strip()):
        for word in sentence.split():
            if pending and len(pending) + 1 + len(word) > limit:
                yield pending
                pending = ""
            while len(word) > limit:
                yield word[:limit]
                word = word[limit:]
            pending = (pending + " " + word).strip()
        if pending:
            yield pending
            pending = ""


class AdaptiveNoiseFloor:
    """A bounded silence percentile, not speaker training. Never learns during speech."""

    def __init__(self):
        from collections import deque

        self.samples = deque(maxlen=120)
        self.threshold = 200

    def reset(self, initial=200):
        self.samples.clear()
        self.threshold = initial

    def feed(self, rms, speaking=False):
        if speaking:
            return self.threshold
        self.samples.append(max(0, rms))
        if len(self.samples) >= 12:
            floor = sorted(self.samples)[len(self.samples) // 5]
            target = max(35, min(900, floor * 2.8 + 20))
            self.threshold = int(self.threshold * 0.8 + target * 0.2)
        return self.threshold
