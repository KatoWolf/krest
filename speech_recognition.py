"""Conservative repairs for short music commands from the existing Vosk model."""

import re

from speech_tools import normalize_command, split_wake

TRANSPORT_SKILLS = {
    "skills.media." + name for name in ("next_track", "prev_track", "pause_music", "play_music")
}


def decode_voice_command(manager, text, settings):
    cfg = settings.get("app_settings", {})
    wake_word = cfg.get("wake_word", "крест")
    wake, command = split_wake(text, wake_word)
    near_wake = False
    if not wake and wake_word.casefold() == "крест":
        match = re.fullmatch(r"\s*(?:креси|крес)[\s,.:!?-]+(.+)", text, re.I)
        if match:
            near_wake, command = True, match[1]
    aliases = settings.get("command_aliases", {})
    clean = normalize_command(command, aliases).casefold().strip(" ,.!?")
    # Reported small-model transcripts: corrections apply to complete music
    # phrases only, never to reminder contents or arbitrary computer actions.
    if not manager.match(clean) and re.fullmatch(
        r"(?:с ключи|сключи|прикручивать|прикручивай) (?:тр[еэ]к|песню)", clean
    ):
        command = "переключи трек"
    if near_wake:
        matched = manager.match(normalize_command(command, aliases))
        if not matched or matched[0].id not in TRANSPORT_SKILLS:
            return False, text
        return True, command
    return wake, command
