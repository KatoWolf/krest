"""Deterministic command language, bounded matching and confirmed local corrections."""

from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import re
import sqlite3
import time

from speech_tools import normalize_command
from speech_recognition import TRANSPORT_SKILLS

AUTO_SKILLS = TRANSPORT_SKILLS | {
    "skills.basic.basic",
    "skills.windows.take_screenshot",
    "skills.media.tell_current_playing_track",
    "skills.media.volume_up",
    "skills.media.volume_down",
    "skills.timers_alarms.check_active_timers",
    "skills.timers_alarms.check_active_alarms",
    "skills.timers_alarms.check_stopwatch",
    "skills.warframe.briefing",
    "skills.warframe.cycles",
    "skills.warframe.show_briefing",
}
VERBS = {
    "открыть": "открой",
    "закрыть": "закрой",
    "включить": "включи",
    "выключить": "выключи",
    "отключить": "отключи",
    "запустить": "запусти",
    "остановить": "останови",
    "поставить": "поставь",
    "установить": "установи",
    "сделать": "сделай",
    "показать": "покажи",
    "рассказать": "расскажи",
    "проверить": "проверь",
    "напомнить": "напомни",
    "запомнить": "запомни",
    "переключить": "переключи",
    "перемотать": "перемотай",
    "свернуть": "сверни",
    "развернуть": "разверни",
    "отменить": "отмени",
    "перезагрузить": "перезагрузи",
    "найти": "найди",
    "обновить": "обнови",
    "добавить": "добавь",
    "удалить": "удали",
}
PREAMBLE = re.compile(
    r"^(?:пожалуйста|будь добра|будь добр|можешь(?: ты)?(?: мне)?|"
    r"можно(?: мне)?|прошу тебя|не могла бы ты|я хочу чтобы ты|давай)[, ]+",
    re.I,
)
NEGATION = re.compile(r"\b(?:не|нет|отмена)\b", re.I)


def conversational_form(text):
    # Only modify the leading command, not free text in its argument.
    value = text.strip()
    for _ in range(3):
        value, count = PREAMBLE.subn("", value, count=1)
        if not count:
            break
    first, separator, rest = value.partition(" ")
    first = VERBS.get(first.casefold(), first)
    value = first + separator + rest
    value = re.sub(r"^(\w+) пожалуйста[, ]+", r"\1 ", value, flags=re.I)
    return normalize_command(value)


def exact_command(manager, text):
    aliases = manager.settings().get("command_aliases", {})
    normalized = normalize_command(text, aliases)
    if manager.match(normalized):
        return normalized
    spoken = conversational_form(normalized)
    return spoken if manager.match(spoken) else None


def _sound(text):
    text = text.casefold().replace("ё", "е").translate(str.maketrans("бдгжзвое", "пткссфаи"))
    return re.sub(r"(.)\1+", r"\1", text.replace("ь", "").replace("ъ", ""))


def _similarity(heard, expected):
    if heard == expected:
        return 1.0
    plain = SequenceMatcher(None, heard, expected).ratio()
    phonetic = SequenceMatcher(None, _sound(heard), _sound(expected)).ratio()
    # Phonetics support spelling distance; they never make different words exact.
    return max(plain, min(0.94, 0.65 * phonetic + 0.35 * plain))


def _index(manager):
    entries = manager.catalog()
    signature = tuple(
        (e.id, tuple(e.keywords), tuple(p.pattern for p in e.patterns)) for e in entries
    )
    previous = getattr(manager, "_language_index", None)
    if previous and previous[0] == signature:
        return previous[1]
    rows = []
    for entry in entries:
        for keyword in entry.keywords:
            if not NEGATION.search(keyword):
                rows.append((entry, keyword, False))
        for pattern in entry.patterns:
            # Literal command prefixes only. The captured argument stays verbatim.
            prefix = re.match(r"^([\w ]+) \(\?P<", pattern.pattern)
            if prefix and len(prefix[1].split()) >= 2:
                rows.append((entry, prefix[1], True))
    manager._language_index = (signature, rows)
    return rows


def suggestions(manager, text):
    heard = conversational_form(text).casefold().strip(" ,.!?")
    if NEGATION.search(heard) or not 1 <= len(heard.split()) <= 18 or len(heard) > 120:
        return []
    words = list(re.finditer(r"\S+", heard))
    ranked = {}
    for entry, phrase, prefix in _index(manager):
        tokens = phrase.split()
        size = len(tokens)
        boundary_error = not prefix and abs(size - len(words)) == 1
        if (prefix and size >= len(words)) or (
            not prefix and size != len(words) and not boundary_error
        ):
            continue
        sample = [w.group() for w in (words if boundary_error else words[:size])]
        if re.findall(r"\d+", " ".join(sample)) != re.findall(r"\d+", phrase):
            continue
        # Don't rewrite intent opposites just because most letters are shared.
        opposites = (
            {"включи", "выключи"},
            {"открой", "закрой"},
            {"следующий", "предыдущий"},
            {"громче", "тише"},
        )
        if any(
            a != b and any(a in pair and b in pair for pair in opposites)
            for a, b in zip(sample, tokens)
        ):
            continue
        if boundary_error:
            score = _similarity("".join(sample), "".join(tokens)) * 0.96
            if score < 0.94 or len(heard) < 9:
                continue
        else:
            scores = [_similarity(a, b) for a, b in zip(sample, tokens)]
            if min(scores) < 0.62 or sum(a != b for a, b in zip(sample, tokens)) > 2:
                continue
            score = sum(scores) / size
        if score < 0.84 or manager.availability(entry) != "Доступен":
            continue
        suffix = ""
        if prefix:
            # Obtain the same positions in the case-preserving normalized form.
            form = conversational_form(text).strip(" ,.!?")
            suffix = " " + form[words[size].start() :]
        command = phrase + suffix
        match = manager.match(command)
        if not match or match[0].id != entry.id:
            continue
        row = {"command": command, "skill": entry.id, "score": score, "prefix": prefix}
        key = (entry.id, suffix)
        if key not in ranked or score > ranked[key]["score"]:
            ranked[key] = row
    return sorted(ranked.values(), key=lambda row: (-row["score"], row["command"]))[:3]


@dataclass
class Understanding:
    command: str = ""
    prompt: str = ""
    learning: tuple | None = None


class VoiceUnderstanding:
    def __init__(self, manager, memory, companion=None):
        self.manager, self.memory = manager, memory
        self.companion = companion
        self.pending = None

    def resolve(self, text, addressed):
        now = time.monotonic()
        clean = text.casefold().strip(" ,.!?")
        pending, self.pending = self.pending, None
        if pending and now < pending["until"]:
            if clean in ("нет", "отмена", "не надо", "неправильно"):
                return Understanding(prompt="Хорошо, отменила.")
            choice = None
            if addressed or all(row["skill"] in AUTO_SKILLS for row in pending["choices"]):
                if (
                    clean in ("да", "верно", "правильно", "подтверждаю")
                    and len(pending["choices"]) == 1
                ):
                    choice = pending["choices"][0]
                elif clean in ("первое", "первый", "один", "1"):
                    choice = pending["choices"][0]
                elif clean in ("второе", "второй", "два", "2") and len(pending["choices"]) > 1:
                    choice = pending["choices"][1]
            if choice:
                return Understanding(choice["command"], learning=(pending["heard"], choice))
            if clean in (
                "да",
                "верно",
                "правильно",
                "подтверждаю",
                "первое",
                "второе",
                "один",
                "два",
                "1",
                "2",
            ):
                self.pending = pending
                return Understanding(text)
            if not addressed and not exact_command(self.manager, text):
                self.pending = pending
                return Understanding(text)
        exact = exact_command(self.manager, text)
        if not addressed or NEGATION.search(clean):
            return Understanding(exact or text)
        choices = []
        if self.companion:
            saved = self.companion.learning.lookup(self.companion.profile, clean)
            match = self.manager.match(saved["command"]) if saved else None
            if (
                match
                and match[0].id == saved["skill"]
                and self.manager.availability(match[0]) == "Доступен"
            ):
                choices = [dict(saved, score=1, prefix=False)]
        if exact and not choices:
            return Understanding(exact)
        key = "команда: " + clean
        if (
            not choices
            and len(key) <= 100
            and (not self.companion or self.companion.profile == self.companion.legacy_profile)
        ):
            try:
                saved = json.loads(self.memory.recall(key, "null"))
                matched = self.manager.match(saved["command"]) if isinstance(saved, dict) else None
                if (
                    matched
                    and matched[0].id == saved["skill"]
                    and self.manager.availability(matched[0]) == "Доступен"
                ):
                    choices = [dict(saved, score=1, prefix=False)]
            except (ValueError, KeyError, TypeError):
                pass
        choices = choices or suggestions(self.manager, text)
        if not choices:
            return Understanding(text)
        best = choices[0]
        gap = best["score"] - choices[1]["score"] if len(choices) > 1 else 1
        if (
            best["skill"] in AUTO_SKILLS
            and not best["prefix"]
            and best["score"] >= 0.94
            and gap >= 0.08
        ):
            return Understanding(best["command"])
        choices = choices[:2] if gap < 0.06 else choices[:1]
        self.pending = {"heard": clean, "choices": choices, "until": now + 25}
        labels = [row["command"] for row in choices]
        prompt = (
            f"Выполнить «{labels[0]}»? Скажи «Крест, да» или «нет»."
            if len(labels) == 1
            else f"Первое: «{labels[0]}». Второе: «{labels[1]}». Скажи «Крест, первое» или «Крест, второе»."
        )
        if self.companion and len(labels) == 1:
            from companion_behavior import policy

            prompt = policy(self.manager.settings(), bool(self.companion.runtime.performance.game))[
                "confirm"
            ].format(command=labels[0]) + (
                " Скажи «да» или «нет»."
                if best["skill"] in AUTO_SKILLS
                else " Скажи «Крест, да» или «нет»."
            )
        return Understanding(prompt=prompt)

    def remember(self, learning):
        if not learning:
            return
        heard, choice = learning
        if self.companion:
            self.companion.record_correction(heard, choice)
            return
        key = "команда: " + heard
        if len(key) <= 100:
            try:
                self.memory.remember(
                    key,
                    json.dumps(
                        {"command": choice["command"], "skill": choice["skill"]}, ensure_ascii=False
                    ),
                    "correction",
                )
            except (OSError, ValueError, sqlite3.Error):
                pass  # A full correction store must not undo a successful command.
