"""Conservative repairs for short audio commands from the existing Vosk model."""

import re

from speech_tools import addressed_speech, normalize_command

TRANSPORT_SKILLS = {
    "skills.media." + name for name in ("next_track", "prev_track", "pause_music", "play_music")
}
SHORT_AUDIO_SKILLS = TRANSPORT_SKILLS | {
    "skills.discord_voice." + name
    for name in ("mute_microphone", "unmute_microphone", "microphone_status")
}


def command_confidence(manager, text, command, addressed, average, recognition, cfg):
    """Score the addressed phrase instead of the preceding conversation.

    Only exact short controls can discount a weak wake word. Keep the intent verb;
    don't relax confidence for numbers, names or arbitrary task contents.
    """
    if not addressed or recognition.get("text") != text:
        return average
    words = recognition.get("words", [])
    wake = cfg.get("wake_word", "крест").casefold()
    # Confidence belongs to the addressed phrase, not the conversation before
    # it. Align Vosk tokens before discarding any scores; incomplete metadata
    # must keep the original confidence gate.
    aligned = " ".join(w.get("word", "") for w in words).casefold()
    aligned_wake, body = addressed_speech(aligned, wake)
    start = len(words) - len(body.split()) - 1 if aligned_wake else None
    aligned_ok = aligned == " ".join(text.casefold().split())
    found = manager.match(normalize_command(command))
    if start is None or not aligned_ok:
        return average
    if any("conf" not in w for w in words[start:]):
        return average
    if not found or found[0].id not in SHORT_AUDIO_SKILLS:
        scores = [float(w["conf"]) for w in words[start:]]
        return sum(scores) / len(scores) if scores else average
    rest = words[start + 1 :]
    if not rest or any("conf" not in w for w in rest):
        return average
    floor = cfg.get("speech_confidence", 60) / 100
    scores = [float(w["conf"]) for w in rest]
    if found[0].id.startswith("skills.discord_voice.") and scores[0] < floor:
        return min(average if average is not None else 1, scores[0])
    if float(words[start].get("conf", 0)) < 0.3 or scores[0] < floor or min(scores) < 0.45:
        return average
    result = sum(scores) / len(scores)
    return max(average or 0, result) if result >= max(floor, 0.75) else average


def decode_voice_command(manager, text, settings):
    cfg = settings.get("app_settings", {})
    wake_word = cfg.get("wake_word", "крест")
    wake, command = addressed_speech(text, wake_word)
    if wake:
        # A restarted address is common in live speech. Remove only repeated
        # wake words at the start, never words inside notes or other arguments.
        restart = re.compile(
            r"^(?:(?:ну|ээ|эм|алло|слушай)[, ]+)*" + re.escape(wake_word) + r"(?:[, ]+|$)", re.I
        )
        for _ in range(3):
            command, count = restart.subn("", command, count=1)
            if not count:
                break
    near_wake = False
    if not wake and wake_word.casefold() == "крест":
        for variant in ("креси", "крес"):
            near_wake, command = addressed_speech(text, variant)
            if near_wake:
                break
    aliases = settings.get("command_aliases", {})
    clean = normalize_command(command, aliases).casefold().strip(" ,.!?")
    # Reported small-model transcripts: corrections apply to complete music
    # phrases only, never to reminder contents or arbitrary computer actions.
    if not manager.match(clean) and re.fullmatch(
        r"(?:с ключи|сключи|прикручивать|прикручивай) (?:тр[еэ]к|песню)", clean
    ):
        command = "переключи трек"
    if wake or near_wake:
        command = isolate_control(manager, command, aliases)
    if near_wake:
        matched = manager.match(normalize_command(command, aliases))
        if not matched or matched[0].id not in SHORT_AUDIO_SKILLS:
            return False, text
        return True, command
    return wake, command


def isolate_control(manager, command, aliases):
    """Remove conversational framing only around an exact short control.

    Never truncate names, reminder text, destructive actions or a negation.
    Unknown speech continues through the ordinary clarification path.
    """
    from command_understanding import conversational_form
    from speech_dictionary import QUICK_SKILLS

    value = normalize_command(command, aliases)
    for _ in range(3):
        value = re.sub(
            r"^(?:(?:ну|сейчас|пожалуйста|давай|мне|слушай)\s+){1,5}", "", value, flags=re.I
        )
        value = conversational_form(value)
    value = re.sub(r"^(\w+) (?:сейчас |мне )", r"\1 ", value, flags=re.I)
    candidate = re.split(
        r"\s+(?:а то|потому что|спасибо|пожалуйста)\b", value, maxsplit=1, flags=re.I
    )[0]
    if re.search(r"\b(?:не|нет|отмена|подожди|стой)\b", value, re.I):
        return command
    found = manager.match(candidate.strip(" ,.!?"))
    return candidate if found and found[0].id in QUICK_SKILLS else command
