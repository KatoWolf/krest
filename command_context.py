"""Short follow-up commands address explicit IDs, never a whole collection."""

import re
import time
from dataclasses import dataclass
from skills.timers_alarms import parse_timer_duration


@dataclass(frozen=True)
class ContextReply:
    text: str
    success: bool = False


class CommandContext:
    def __init__(self, state):
        self.state = state
        self.pending = None

    def handle(self, text):
        clean = text.casefold().strip(" .!?")
        if self.pending and time.monotonic() - self.pending["created"] > 120:
            self.pending = None
        if self.pending:
            selected = [v for v in self.pending["choices"] if v["label"].casefold() == clean]
            if clean.isdigit() and 1 <= int(clean) <= len(self.pending["choices"]):
                selected = [self.pending["choices"][int(clean) - 1]]
            if len(selected) == 1:
                pending, self.pending = self.pending, None
                return self._apply(pending["action"], selected[0], pending["seconds"])
        action, seconds = None, 0
        match = re.fullmatch(
            r"(?:добавь еще|добавь ещё|продли его на|продли таймер на) (.+)", clean
        )
        postpone = re.fullmatch(
            r"(?:отложи|перенеси) (?:это |его |её |ее )?(?:еще |ещё )?(?:на |через )(.+)", clean
        )
        if match:
            action = "extend"
            seconds = parse_timer_duration(match[1])[0]
        elif postpone:
            action = "snooze"
            seconds = parse_timer_duration(postpone[1])[0]
        elif clean in ("отмени его", "отмени её", "отмени ее", "отмени это"):
            action = "cancel"
        elif clean in ("выполнено", "дело сделано", "готово"):
            action = "done"
        else:
            self.pending = None
            return None
        self.pending = None
        if action in ("extend", "snooze") and seconds <= 0:
            return ContextReply("Укажите длительность, например: добавь ещё пять минут.")
        choices = (
            self.state.reminders()
            if action in ("snooze", "done")
            else [
                dict(v, kind=k)
                for k, items in self.state.snapshot().items()
                if k in ("active_timers", "active_alarms", "daily_tasks")
                for v in items
            ]
        )
        if action == "extend":
            choices = [v for v in choices if v["kind"] == "active_timers"]
        recent = getattr(self.state, "last_target", None)
        if recent and time.monotonic() - recent[1] < 120:
            focused = [v for v in choices if v["id"] == recent[0]]
            if focused:
                choices = focused
        if not choices:
            return ContextReply("Нет подходящей активной задачи. Назовите новую команду целиком.")
        if len(choices) > 1:
            self.pending = {
                "action": action,
                "seconds": seconds,
                "choices": choices,
                "created": time.monotonic(),
            }
            return ContextReply(
                "Уточните название или номер: "
                + "; ".join(f"{i}. {v['label']}" for i, v in enumerate(choices, 1))
            )
        return self._apply(action, choices[0], seconds)

    def _apply(self, action, item, seconds):
        try:
            if action == "extend":
                self.state.extend_timer(item["id"], seconds)
            elif action == "cancel":
                if not self.state.cancel(item["kind"], item["id"]):
                    raise ValueError("Задача уже завершилась")
            elif action == "snooze":
                self.state.snooze(item["id"], seconds)
            else:
                self.state.complete(item["id"])
            return ContextReply(
                {
                    "extend": "Таймер продлён",
                    "cancel": "Задача отменена",
                    "snooze": "Напоминание отложено",
                    "done": "Выполнение отмечено",
                }[action]
                + ": "
                + item["label"]
                + ".",
                True,
            )
        except (ValueError, OSError) as exc:
            return ContextReply(str(exc))
