"""Release check for the actual bundled models and streaming recognizer; no devices."""

import time


def check(engine):
    import numpy as np
    from types import SimpleNamespace
    from audio_devices import PCMConverter
    from configuration import DEFAULT_CONFIG
    from skill_manager import SkillManager
    from speech_dictionary import quick_phrases
    from command_parser import parse_command

    manager = SkillManager(lambda: DEFAULT_CONFIG)
    original_stream = engine.stream
    engine.stream = SimpleNamespace(active=True)
    engine.configure(lambda: DEFAULT_CONFIG)
    engine.set_command_dictionary(quick_phrases(manager, DEFAULT_CONFIG))
    examples = (
        ("Крест, переключи трек.", "skills.media.next_track"),
        ("Крест, пауза.", "skills.media.pause_music"),
        ("Крест, выключи микрофон.", "skills.discord_voice.mute_microphone"),
        ("Крест, включи микрофон.", "skills.discord_voice.unmute_microphone"),
        ("Ха ха ха, Крест, переключи трек.", "skills.media.next_track"),
    )
    rows = []
    try:
        for phrase, expected in examples:
            samples = engine.synthesize(phrase)
            for gain, noise in ((1.0, 0.0), (0.35, 0.001)):
                rng = np.random.default_rng(41)
                audio = samples * gain + rng.normal(0, noise, samples.shape)
                raw = PCMConverter(24000).convert(
                    (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
                )
                raw = bytes(16000) + raw + bytes(32000)
                engine.clear_audio_queue()
                engine._reset_recognizer()
                engine._noise.reset()
                engine._continuity.recovering = False
                heard = []
                started = time.monotonic()
                for offset in range(0, len(raw), 960):
                    block = raw[offset : offset + 960]
                    engine.audio_callback(block, len(block) // 2, None, None)
                    if text := engine.listen():
                        heard.append(text)
                transcript = " ".join(heard)
                parsed = parse_command(manager, transcript, DEFAULT_CONFIG)
                skill = parsed["skill"] if parsed["wake"] else ""
                row = {
                    "phrase": phrase,
                    "heard": transcript,
                    "skill": skill,
                    "expected": expected,
                    "ok": skill == expected,
                    "gain": gain,
                    "noise": noise,
                    "rtf": round((time.monotonic() - started) / (len(raw) / 32000), 3),
                }
                rows.append(row)
                print("Speech check: " + str(row), flush=True)
                if skill and skill != expected:
                    raise RuntimeError("Распознано другое действие: " + transcript)
        passed = sum(row["ok"] for row in rows)
        print(
            f"Bundled speech pipeline: {passed}/{len(rows)} synthetic phrases; no microphone or actions.",
            flush=True,
        )
        if not any(row["ok"] for row in rows):
            raise RuntimeError("Сборка не распознала ни одной контрольной команды")
    finally:
        engine.stream = original_stream
        engine.clear_audio_queue()
        engine._reset_recognizer()
    return rows
