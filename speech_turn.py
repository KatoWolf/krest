"""Join only a addressed, unfinished command across finalized Vosk segments."""

import time

from command_sequence import SEPARATOR, parameter_error, plan_sequence
from command_understanding import exact_command
from speech_recognition import decode_voice_command
from speech_tools import addressed_speech


class VoiceTurn:
    def __init__(self, manager, feedback):
        self.manager, self.feedback = manager, feedback
        self.pending = None
        self.turn = None
        self.partial = ""
        self.partial_at = 0.0
        self.prefixes = set()
        for entry in manager.catalog():
            for phrase in entry.keywords + entry.examples:
                words = phrase.casefold().split()
                self.prefixes.update(" ".join(words[:i]) for i in range(1, len(words)))

    def incomplete(self, body):
        body = body.casefold().strip(" ,.!?")
        if not body:
            return False  # Bare wake keeps the existing explicit listening window.
        plan = plan_sequence(self.manager, body)
        if plan is not None:
            tail = SEPARATOR.split(body)[-1].strip()
            return bool(plan.error and tail and tail != body and self.incomplete(tail))
        command = exact_command(self.manager, body)
        if command:
            entry, params = self.manager.match(command)
            if entry.id == "skills.timers_alarms.set_timer" and body in self.prefixes:
                return True
            if entry.id == "skills.timers_alarms.set_timer" and not params:
                return True
            if entry.id in {
                "skills.media.set_exact_volume",
                "skills.media.volume_up",
                "skills.media.volume_down",
            } and body.endswith(" на"):
                return True
            return bool(parameter_error(entry, params, command)) and body in self.prefixes
        if body in self.prefixes:
            return True
        for suffix in (" и", " затем", " а затем", " потом", " а потом", " следующая команда"):
            if body.endswith(suffix):
                return bool(exact_command(self.manager, body[: -len(suffix)]))
        return False

    def observe(self, partial, now=None, *, listening_until=0.0):
        now = time.monotonic() if now is None else now
        if not partial or partial == self.partial:
            return
        self.partial, self.partial_at = partial, now
        wake = self.manager.settings().get("app_settings", {}).get("wake_word", "крест")
        addressed, body = addressed_speech(partial, wake)
        if addressed or self.pending or now < listening_until:
            if self.turn is None:
                self.turn = self.feedback.begin("addressed", "Обращение услышано")
            else:
                self.feedback.update(self.turn, "recognizing", body if addressed else partial)

    def clear(self, reason=""):
        if reason and self.turn is not None:
            self.feedback.update(self.turn, "error", reason, 3)
        self.pending = None
        self.turn = None
        self.partial = ""
        self.partial_at = 0

    def push(self, text, recognition, now=None, *, listening_until=0.0):
        now = time.monotonic() if now is None else now
        settings = self.manager.settings()
        addressed, body = decode_voice_command(self.manager, text, settings)
        if self.pending and not addressed:
            previous = self.pending
            if now > self._deadline() or (not previous["addressed"] and now >= listening_until):
                return self.flush(now, force=True)
            combined = previous["text"] + " " + text
            _, combined_body = decode_voice_command(self.manager, combined, settings)
            plan = plan_sequence(self.manager, combined_body)
            valid = (
                exact_command(self.manager, combined_body)
                or self.incomplete(combined_body)
                or (plan and not plan.error)
            )
            if valid:
                recognition = merge_recognition(previous["recognition"], recognition, combined)
                text, body, addressed = combined, combined_body, previous["addressed"]
            else:
                # Unrelated speech must not turn an incomplete command into a
                # guessed action. Let the ordinary path explain the original.
                return self.flush(now, force=True)
        elif self.pending and addressed:
            self.clear()
        incomplete = (addressed or now < listening_until) and self.incomplete(body)
        if (addressed or incomplete) and self.turn is None:
            self.turn = self.feedback.begin("addressed", body)
        if incomplete:
            started = self.pending["started"] if self.pending else now
            self.pending = dict(
                text=text,
                recognition=recognition,
                started=started,
                until=min(started + 5, now + 2),
                addressed=addressed,
                listening_until=listening_until if not addressed else None,
            )
            self.feedback.update(self.turn, "recognizing", "Слушаю продолжение команды")
            self.partial = ""
            return []
        turn = self.turn
        self.clear()
        return [(text, recognition, turn)]

    def flush(self, now=None, force=False):
        now = time.monotonic() if now is None else now
        if not self.pending:
            return []
        # Extend only while recognizer words are actually changing; no indefinite
        # hold due to a stale partial or a constant noise floor.
        if not force and now < self._deadline():
            return []
        pending, turn = self.pending, self.turn
        self.clear()
        return [(pending["text"], pending["recognition"], turn)]

    def _deadline(self):
        return min(
            self.pending["started"] + 5,
            max(self.pending["until"], self.partial_at + 1),
            self.pending.get("listening_until") or float("inf"),
        )


def merge_recognition(first, second, text):
    words = [*first.get("words", []), *second.get("words", [])]
    scores = [w["conf"] for w in words if "conf" in w]
    aligned = " ".join(w.get("word", "") for w in words).casefold()
    complete = aligned == " ".join(text.casefold().split()) and len(scores) == len(words)
    averages = [r["confidence"] for r in (first, second) if r.get("confidence") is not None]
    confidence = sum(scores) / len(scores) if complete and scores else min(averages, default=None)
    return dict(
        second,
        text=text,
        words=words,
        confidence=confidence,
        decoder_ms=sum(r.get("decoder_ms", 0) or 0 for r in (first, second)),
        endpoint="joined",
    )
