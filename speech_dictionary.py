"""A small catalog-derived vocabulary for quick, parameter-free voice controls."""

from speech_recognition import SHORT_AUDIO_SKILLS
from speech_tools import alias_body

QUICK_SKILLS = SHORT_AUDIO_SKILLS | {
    "skills.media.volume_up",
    "skills.media.volume_down",
    "skills.media.tell_current_playing_track",
}


def quick_phrases(manager, settings):
    wake = settings.get("app_settings", {}).get("wake_word", "крест").casefold()
    phrases = set()
    for entry in manager.catalog():
        if entry.id in QUICK_SKILLS:
            phrases.update(k.casefold() for k in entry.keywords if len(k.split()) <= 6)
    for alias, target in settings.get("command_aliases", {}).items():
        found = manager.match(alias_body(target, wake))
        if found and found[0].id in QUICK_SKILLS:
            phrases.add(alias_body(alias, wake).casefold())
    return {wake + " " + phrase for phrase in phrases}
