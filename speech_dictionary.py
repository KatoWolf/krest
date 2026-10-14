"""A small catalog-derived vocabulary for quick, parameter-free voice controls."""

from speech_recognition import SHORT_AUDIO_SKILLS
from speech_tools import alias_body

QUICK_SKILLS = SHORT_AUDIO_SKILLS | {
    "skills.media.volume_up",
    "skills.media.volume_down",
    "skills.media.tell_current_playing_track",
    "skills.media.like_track",
    "skills.media.unlike_track",
}


def quick_phrases(manager, settings, learned=()):
    wake = settings.get("app_settings", {}).get("wake_word", "крест").casefold()
    phrases = set()
    from command_sequence import SAFE

    for entry in manager.catalog():
        if (
            entry.id in QUICK_SKILLS
            or (entry.id in SAFE or entry.id == "skills.workflows.undo_action")
            and not entry.patterns
        ):
            phrases.update(
                k.casefold() for k in [*entry.keywords, *entry.examples] if len(k.split()) <= 6
            )
    # Cover the finite forms accepted by built-in patterns, not only keywords.
    candidates = [
        verb + " " + noun
        for verb in ("переключи", "переключить", "смени", "поменяй", "пропусти")
        for noun in ("трек", "песню", "музыку", "композицию")
    ] + [
        verb + " " + noun + suffix
        for verb in ("включи", "выключи", "отключи", "заглуши")
        for noun in ("микрофон", "микро", "мик")
        for suffix in ("", " в дискорде", " в дс")
    ]
    for candidate in candidates:
        found = manager.match(candidate)
        if found and found[0].id in QUICK_SKILLS:
            phrases.add(candidate)
    for alias, target in settings.get("command_aliases", {}).items():
        found = manager.match(alias_body(target, wake))
        if found and found[0].id in QUICK_SKILLS:
            phrases.add(alias_body(alias, wake).casefold())
    for row in learned:
        if row.get("status") == "approved" and row.get("skill") in QUICK_SKILLS:
            phrases.add(alias_body(row["heard"], wake).casefold())
    # Bare phrases only accelerate endpointing; runtime still requires a wake
    # word or an explicit, live follow-up context before any action.
    phrases = {p for p in phrases if len(p) <= 100 and len(p.split()) <= 8}
    return phrases | {wake + " " + phrase for phrase in phrases}
