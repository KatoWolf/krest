"""Explicit, short-lived speech corrections; no guesses about destructive commands."""

import re
import time
import uuid
from command_understanding import exact_command, AUTO_SKILLS, NEGATION
from speech_dictionary import QUICK_SKILLS
from speech_tools import normalize_command

SAFE_REPAIRS = AUTO_SKILLS | (
    QUICK_SKILLS
    - {
        "skills.learned_actions.capture",
        "skills.learned_actions.save",
        "skills.learned_actions.cancel",
        "skills.learned_actions.stop",
        "skills.learned_actions.undo_step",
        "skills.smart_tools.cancel_watches",
    }
)
PATTERN = re.compile(r"^(?:нет[, ]+)?(?:я сказал[а]?|я имел[а]? в виду)\s+(.+?)[.!?]*$", re.I)


def target(text):
    found = PATTERN.fullmatch(text.strip())
    return found[1].strip() if found else ""


class SpeechRepair:
    def __init__(self, runtime, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self.last = None
        self.last_learned = None

    def capture(self, heard):
        if (
            not heard
            or target(heard)
            or heard.casefold() in {"да", "нет", "отмена", "первое", "второе"}
        ):
            return
        self.last = {
            "heard": normalize_command(heard),
            "profile": self.runtime.companion.profile,
            "at": self.clock(),
            "id": uuid.uuid4().hex,
        }
        return self.last["id"]

    def finished(self, ident, command, success):
        if ident and self.last and self.last["id"] == ident and success:
            self.last["executed"] = command

    def eligible(self, text, *, addressed=False):
        return bool(
            target(text)
            and self.last
            and self.last["profile"] == self.runtime.companion.profile
            and 0 <= self.clock() - self.last["at"] < (45 if addressed else 12)
        )

    def correct(self, text, *, addressed):
        if not self.eligible(text, addressed=addressed):
            raise ValueError(
                "Нет недавней фразы для исправления. Скажите команду целиком с обращением."
            )
        corrected = exact_command(self.runtime.manager, target(text))
        found = self.runtime.manager.match(corrected) if corrected else None
        if not found or found[0].id not in SAFE_REPAIRS:
            raise ValueError(
                "Такую команду не запоминаю как поправку слуха. Произнесите её отдельно с обращением «Крест»."
            )
        entry, params = found
        # Personal corrections are whole phrases, not replacements of names,
        # paths, numbers, negations or portions of another command.
        if params or NEGATION.search(corrected) or re.search(r"\d", corrected):
            raise ValueError(
                "Команду с параметрами произнесите отдельно; её значения не обучаются как произношение."
            )
        previous, self.last = self.last, None  # A correction consumes its evidence exactly once.
        heard = previous["heard"]
        existing = exact_command(self.runtime.manager, heard)
        performed = self.runtime.manager.match(previous.get("executed", ""))
        learn = not existing and not NEGATION.search(heard) and not re.search(r"\d", heard)
        choice = {
            "command": corrected,
            "skill": entry.id,
            "explicit": True,
            "activate": learn,
            "profile": previous["profile"],
            "already_done": bool(
                performed and performed[0].id == entry.id and performed[1] == params
            ),
        }
        return corrected, (heard, choice)

    def remember(self, heard, choice):
        companion = self.runtime.companion
        if choice.get("profile") != companion.profile or not choice.get("activate"):
            return
        old = next(
            (
                r
                for r in companion.learning.list(companion.profile)
                if r["heard"] == heard.casefold().strip(" ,.!?")
            ),
            None,
        )
        row = companion.learning.record(
            companion.profile, heard, choice["command"], choice["skill"]
        )
        if row:
            companion.learning.decide(companion.profile, row["heard"], "approved")
            companion._learning_at = 0
            self.last_learned = {
                "profile": companion.profile,
                "heard": row["heard"],
                "previous": old,
                "command": choice["command"],
                "skill": choice["skill"],
            }
            self.runtime.publish_notice(
                f"Запомнила произношение: «{row['heard']}» → «{choice['command']}». Можно отменить последнее исправление.",
                "Обучение речи",
                speak=False,
            )

    def undo(self):
        saved = self.last_learned
        if not saved or saved["profile"] != self.runtime.companion.profile:
            raise ValueError("В этой сессии нет подтверждённого исправления для отмены")
        learning = self.runtime.companion.learning
        current = learning.lookup(saved["profile"], saved["heard"])
        if not current or (current["command"], current["skill"]) != (
            saved["command"],
            saved["skill"],
        ):
            raise ValueError("Запись уже изменена в меню; отмена её не заменяет")
        previous = saved["previous"]
        if previous:
            learning.record(
                saved["profile"], saved["heard"], previous["command"], previous["skill"]
            )
            learning.decide(saved["profile"], saved["heard"], previous["status"])
        else:
            learning.decide(saved["profile"], saved["heard"], "rejected")
        self.last_learned = None
        self.runtime.companion._learning_at = 0
        return "Последнее исправление произношения отменено. Выполненное действие не повторялось и не отменялось."
