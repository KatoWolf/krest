"""Parse an entire spoken sequence before allowing its first side effect."""

from dataclasses import dataclass, field
import re

from command_understanding import exact_command
from speech_tools import normalize_command, split_wake

MAX_STEPS = 5
SEPARATOR = re.compile(
    r"\s*(?:;|\bследующая команда\b|\b(?:а |и )?(?:затем|потом)\b|\bпосле этого\b|\bи\b)\s*", re.I
)
EXPLICIT = re.compile(r"\s*(?:;|\bследующая команда\b)\s*", re.I)

# These actions have no modal confirmation, credential prompt or irreversible
# mutation. Unknown/imported skills must declare a contract before joining this list.
SAFE = (
    {
        "skills.media." + name
        for name in (
            "next_track",
            "prev_track",
            "pause_music",
            "play_music",
            "like_track",
            "unlike_track",
            "tell_current_playing_track",
            "set_exact_volume",
            "set_volume_max",
            "volume_up",
            "volume_down",
            "mute_system",
            "unmute_system",
        )
    }
    | {
        "skills.discord_voice." + name
        for name in (
            "mute_microphone",
            "unmute_microphone",
            "microphone_status",
        )
    }
    | {
        "skills.basic.basic",
        "skills.windows.take_screenshot",
        "skills.timers_alarms.set_timer",
        "skills.timers_alarms.named_reminder",
        "skills.timers_alarms.check_active_timers",
        "skills.timers_alarms.check_active_alarms",
        "skills.workflows.launch_catalog_app",
        "skills.workflows.favourite_player",
        "skills.workflows.favourite_game",
        "skills.webo.launch_steam_game",
        "skills.webo.get_weather_forecast",
        "skills.warframe.launch",
        "skills.warframe.briefing",
        "skills.warframe.cycles",
        "skills.warframe.show_briefing",
    }
)
# These parameter payloads contain numbers rather than arbitrary prose. All
# other captured text (reminders, names, queries) is indivisible without an
# explicit spoken "следующая команда" or a semicolon in text input.
NUMERIC = {
    "skills.media.set_exact_volume",
    "skills.media.volume_up",
    "skills.media.volume_down",
    "skills.timers_alarms.set_timer",
}


@dataclass
class SequencePlan:
    steps: list = field(default_factory=list)
    error: str = ""


def _match(manager, text):
    _, text = split_wake(text, manager.settings().get("app_settings", {}).get("wake_word", "крест"))
    command = exact_command(manager, text)
    return (command, *manager.match(command)) if command else None


def parameter_error(entry, params, command):
    """Pure validation; no skill is executed to discover whether it can run."""
    if entry.id == "skills.media.set_exact_volume":
        from skills.media import _number

        n = _number(params.get("level", ""))
        if n is None or not 0 <= n <= 100:
            return "Укажите громкость от нуля до ста."
    if entry.id in {"skills.media.volume_up", "skills.media.volume_down"}:
        from skills.media import _number

        step = params.get("step")
        if step and (_number(step) is None or not 1 <= _number(step) <= 100):
            return "Укажите изменение громкости от одного до ста."
    if entry.id == "skills.timers_alarms.set_timer":
        from skills.timers_alarms import parse_timer_duration

        seconds, _ = parse_timer_duration(params.get("phrase") or command)
        if not 0 < seconds <= 365 * 86400 or not duration_fragment(params.get("phrase", "")):
            return "Укажите длительность таймера, например две минуты."
    if entry.id == "skills.timers_alarms.named_reminder":
        from skills.timers_alarms import parse_named_reminder

        try:
            parse_named_reminder(params.get("phrase", ""))
        except ValueError as exc:
            return str(exc)
    return ""


def duration_fragment(text):
    from skills.timers_alarms import NUMBERS_DICT

    return bool(text.strip()) and all(
        w in NUMBERS_DICT
        or w
        in {
            "и",
            "на",
            "полчаса",
            "пол-часа",
            "полтора",
            "полторы",
            "половину",
            "четверть",
            "минутку",
            "секундочку",
        }
        or re.fullmatch(r"\d+(?:[.,]\d+)?|час(?:а|ов)?|ч|минут[уыа]?|мин|секунд[уыа]?|сек", w)
        for w in text.casefold().strip(" .!?").split()
    )


def plan_sequence(manager, text):
    """None means one ordinary command. An invalid sequence never runs a prefix."""
    text = normalize_command(text, manager.settings().get("command_aliases", {}))
    separators = list(SEPARATOR.finditer(text))
    if not separators:
        return None
    if len(separators) > 20:
        return SequencePlan(error="Слишком длинная цепочка. Назовите до пяти команд.")

    def split(value):
        whole = _match(manager, value)
        explicit = list(EXPLICIT.finditer(value))
        if whole and not explicit:
            _, entry, params = whole
            # A conjunction inside a title or a reminder is part of its payload.
            if params and entry.id not in NUMERIC or not params:
                return [value]
        for delimiter in SEPARATOR.finditer(value):
            left, right = value[: delimiter.start()].strip(), value[delimiter.end() :].strip()
            if not left or not right:
                continue
            first = _match(manager, left)
            if not first:
                continue
            _, entry, params = first
            if (
                entry.id == "skills.timers_alarms.set_timer"
                and duration_fragment(right)
                and not EXPLICIT.fullmatch(delimiter[0])
            ):
                continue
            if params and entry.id not in NUMERIC and not EXPLICIT.fullmatch(delimiter[0]):
                continue
            tail = split(right)
            return [left, *tail]
        return [value]

    parts = split(text)
    if len(parts) < 2:
        return None
    if len(parts) > MAX_STEPS:
        return SequencePlan(error="За один раз можно выполнить до пяти команд.")
    plan = SequencePlan()
    for i, part in enumerate(parts, 1):
        found = _match(manager, part)
        if not found:
            plan.error = f"Не поняла команду {i}: «{part}». Цепочка не запущена."
            break
        command, entry, params = found
        problem = parameter_error(entry, params, command)
        if entry.id not in SAFE:
            problem = "Эту команду нужно произнести отдельно."
        elif manager.availability(entry) != "Доступен":
            problem = manager.availability(entry)
        if problem:
            plan.error = f"Команда {i}: {problem} Цепочка не запущена."
            break
        plan.steps.append(
            {"command": command, "heard": part, "skill": entry.id, "description": entry.description}
        )
    return plan


def sequence_confidence(plan, recognition, fallback):
    """A confident first action cannot hide a poorly heard second action."""
    words = recognition.get("words", [])
    aligned = " ".join(w.get("word", "") for w in words).casefold()
    if aligned != " ".join(recognition.get("text", "").casefold().split()):
        return fallback
    scores, offset = [], 0
    for step in plan.steps:
        part = step["heard"].casefold()
        at = aligned.find(part, offset)
        if at < 0:
            return fallback
        first = len(aligned[:at].split())
        segment = words[first : first + len(part.split())]
        if not segment or any("conf" not in w for w in segment):
            return fallback
        score = sum(w["conf"] for w in segment) / len(segment)
        if step["skill"].startswith("skills.discord_voice.") or step["skill"] in NUMERIC:
            score = min(score, *(w["conf"] for w in segment))
        scores.append(score)
        offset = at + len(part)
    return min([*scores, fallback] if fallback is not None else scores) if scores else fallback
