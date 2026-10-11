"""Small Vosk result helpers. No extra model, audio capture or fuzzy action dispatch."""

import re
from difflib import SequenceMatcher

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


def hypotheses(result):
    """Use word confidence, never Vosk's unbounded alternative log-likelihood."""
    rows = result.get("alternatives") or [result]
    return [{"text": row.get("text", ""), "words": row.get("result", [])} for row in rows[:3]]


def combine_hypotheses(segments):
    # A small beam preserves alternatives when Vosk splits wake word and command.
    beam = [(0, "", [])]
    for segment in segments:
        candidates = [
            (score + rank, (text + " " + row["text"]).strip(), words + row["words"])
            for score, text, words in beam
            for rank, row in enumerate(segment)
        ]
        beam = sorted(candidates, key=lambda item: item[0])[:3]
    result = []
    for _, text, words in beam:
        confidences = [float(word["conf"]) for word in words if "conf" in word]
        result.append(
            {
                "text": text,
                "confidence": sum(confidences) / len(confidences) if confidences else None,
            }
        )
    return result


def resolve_media_alternative(manager, command, alternatives, settings, has_wake):
    """Recover only unambiguous transport intents after a real wake word.

    The primary transcript stays authoritative for known commands and negation.
    Alternate transcripts never invent a wake word or select destructive skills.
    """
    aliases = settings.get("command_aliases", {})
    primary = normalize_command(command, aliases)
    if (
        not has_wake
        or manager.match(primary)
        or len(primary.split()) > 6
        or re.search(r"\b(?:не|нет|отмена)\b", primary.casefold())
        or not re.match(
            r"^(?:переключ|смени\b|поменяй\b|пропусти\b|следующ|предыдущ|пауз|продолж)",
            primary.casefold(),
        )
    ):
        return None
    cfg = settings.get("app_settings", {})
    threshold = max(0.7, cfg.get("speech_confidence", 60) / 100)
    candidates = {}
    for alternative in (alternatives or [])[:3]:
        confidence = alternative.get("confidence")
        if not isinstance(confidence, (int, float)) or not threshold <= confidence <= 1:
            continue
        wake, text = split_wake(alternative.get("text", ""), cfg.get("wake_word", "крест"))
        if not wake:
            continue
        normalized = normalize_command(text, aliases)
        if SequenceMatcher(None, primary.casefold(), normalized.casefold()).ratio() < 0.8:
            continue
        matched = manager.match(normalized)
        if (
            matched
            and re.search(r"\b(?:предыдущ|прошл)", primary.casefold())
            and matched[0].id != "skills.media.prev_track"
        ):
            continue
        if (
            matched
            and re.search(r"\bследующ", primary.casefold())
            and matched[0].id != "skills.media.next_track"
        ):
            continue
        if (
            matched
            and matched[0].id in TRANSPORT_SKILLS
            and manager.availability(matched[0]) == "Доступен"
        ):
            candidates[matched[0].id] = (text, confidence)
    return next(iter(candidates.values())) if len(candidates) == 1 else None
