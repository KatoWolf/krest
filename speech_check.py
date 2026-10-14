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
        check_game_audio(engine, manager)
    finally:
        engine.stream = original_stream
        engine.clear_audio_queue()
        engine._reset_recognizer()
    return rows


def check_game_audio(engine, manager):
    """Real Vosk+Silero, changing noise and a 1.2 s consumer scheduling stall.

    No microphone, game or player is controlled. Only final, addressed commands
    that pass the runtime confidence rule count as accepted.
    """
    import numpy as np
    from audio_devices import PCMConverter
    from configuration import DEFAULT_CONFIG
    from speech_recognition import command_confidence, decode_voice_command
    from speech_tools import normalize_command

    target = "skills.media.next_track"
    cfg = DEFAULT_CONFIG["app_settings"]
    checks = (
        ("Крест, переключи трек.", 0.0, 1, target),
        ("Крест, переключи трек.", 0.004, 1, target),
        ("Крест, переключи трек.", 0.0, 40, target),
        ("Крест, время.", 0.004, 1, "skills.basic.basic"),
        ("Крест, сделай скриншот.", 0.004, 1, "skills.windows.take_screenshot"),
        ("Крест, таймер на две минуты.", 0.004, 1, "skills.timers_alarms.set_timer"),
        ("Мы продолжаем играть.", 0.0, 1, ""),
        ("Переключи трек.", 0.0, 1, ""),
    )
    for phrase, noise, batch, expected in checks:
        samples = engine.synthesize(phrase)
        audio = np.concatenate([samples * 0.65, np.zeros(24000 * 2)])
        audio += np.random.default_rng(73).normal(0, noise, audio.shape)
        # Calibrate in quiet, then change the background while talking.
        raw = bytes(16000) + PCMConverter(24000).convert(
            (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        )
        engine.clear_audio_queue()
        engine._reset_recognizer()
        engine._noise.reset()
        engine._continuity.recovering = False
        accepted, transcripts = [], []
        for offset in range(0, len(raw), 960 * batch):
            chunk = raw[offset : offset + 960 * batch]
            engine.audio_callback(chunk, len(chunk) // 2, None, None)
            while engine.q.ready.is_set():
                text = engine.listen()
                if not text:
                    continue
                transcripts.append(text)
                wake, body = decode_voice_command(manager, text, DEFAULT_CONFIG)
                found = manager.match(normalize_command(body))
                data = engine.last_recognition
                confidence = command_confidence(
                    manager, text, body, wake, data["confidence"], data, cfg
                )
                if wake and found and confidence is not None and confidence >= 0.6:
                    accepted.append(found[0].id)
        print(
            "Game audio check: "
            + str(
                {
                    "phrase": phrase,
                    "noise": noise,
                    "batch_ms": batch * 30,
                    "heard": transcripts,
                    "accepted": accepted,
                }
            ),
            flush=True,
        )
        if accepted != ([expected] if expected else []):
            raise RuntimeError("Голос в игровом профиле: " + phrase + " → " + str(transcripts))
