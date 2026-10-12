"""Voice-authored scenario drafts; parse all steps before saving, never execute a draft."""

import re
from scenarios import validate_scenarios
from command_understanding import exact_command


def parse_draft(text, manager):
    match = re.fullmatch(
        r"(?:создай|составь) (?:сценарий|режим) (.{1,60}?)(?:\s*:\s*|\s+из действий\s+)(.+)",
        text.strip(),
        re.I,
    )
    if not match:
        raise ValueError(
            "Скажите: создай сценарий Вечер из действий приглуши музыку, включи не беспокоить, запусти Warframe"
        )
    name, body = match.groups()
    chunks = re.split(r"\s*[,;]\s*|\s+(?:затем|потом)\s+", body, flags=re.I)
    steps = []
    labels = []
    replacements = {
        "приглуши музыку": ("command", "тише"),
        "включи не беспокоить": ("notifications", "important"),
        "выключи не беспокоить": ("notifications", "all"),
        "включи игровой профиль": ("profile", "game"),
        "запусти warframe": ("command", "запусти варфрейм"),
        "запусти варфрейм": ("command", "запусти варфрейм"),
    }
    for raw in chunks:
        raw = raw.strip(" .")
        if raw.casefold() in replacements:
            action, value = replacements[raw.casefold()]
        else:
            wait = re.fullmatch(r"подожди (\d{1,2}) секунд(?:ы|у)?", raw, re.I)
            if wait:
                action, value = "wait", wait[1]
            else:
                value = exact_command(manager, raw)
                if not value:
                    raise ValueError("Неизвестный шаг: «" + raw + "». Черновик не сохранён.")
                action = "command"
        if action == "command":
            matched = manager.match(value)
            if not matched or manager.availability(matched[0]) != "Доступен":
                raise ValueError("Шаг сейчас недоступен: " + value)
            if matched[0].id in {
                "skills.workflows.run_scenario",
                "skills.workflows.create_scenario",
                "skills.workflows.save_scenario_draft",
                "skills.workflows.cancel_scenario_draft",
                "skills.workflows.stop_scenario",
            }:
                raise ValueError("Вложенные сценарии не поддерживаются")
        steps.append({"action": action, "value": value})
        labels.append(f"{len(steps)}. {raw}")
    validate_scenarios({name.strip(): steps})
    return {
        "name": name.strip(),
        "steps": steps,
        "text": "Сценарий «" + name.strip() + "»:\n" + "\n".join(labels),
    }
