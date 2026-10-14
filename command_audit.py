"""Validate every built-in command contract without executing PC actions."""

import inspect
import time
from speech_tools import normalize_command
from command_understanding import conversational_form
from command_lanes import LANES, lane_for
from speech_recognition import decode_voice_command


def audit_catalog(manager):
    started = time.perf_counter()
    errors, covered, count = {}, set(), 0
    wake = manager.settings().get("app_settings", {}).get("wake_word", "крест")
    for entry in manager.catalog():
        if not entry.id.startswith("skills."):
            continue
        phrases = entry.keywords + entry.examples
        if not phrases:
            errors[entry.id] = "Нет проверяемого примера команды"
        for phrase in phrases:
            count += 1
            normalized = normalize_command(phrase)
            found = manager.match(normalized) or manager.match(conversational_form(normalized))
            if found is None or found[0].id != entry.id:
                errors[entry.id + ": " + phrase] = "Фраза не ведёт к объявленному навыку"
                continue
            addressed, body = decode_voice_command(
                manager,
                "разговор " + wake + " " + phrase,
                {"app_settings": {"wake_word": wake}},
            )
            spoken = manager.match(normalize_command(body)) or manager.match(
                conversational_form(body)
            )
            if not addressed or not spoken or spoken[0].id != entry.id:
                errors[entry.id + ": " + phrase] = "Голосовое обращение изменило команду"
            signature = inspect.signature(entry.func)
            values = {"text": phrase, "state": None, **found[1]}
            try:
                signature.bind(**{k: v for k, v in values.items() if k in signature.parameters})
            except TypeError as exc:
                errors[entry.id] = str(exc)
            if lane_for(entry) not in LANES:
                errors[entry.id] = "Нет очереди выполнения"
            covered.add(entry.id)
    return {
        "skills": len(covered),
        "phrases": count,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "errors": errors,
    }
