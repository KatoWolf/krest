"""Speech pipeline checks use synthetic PCM and fake model/audio backends."""

import importlib.util
import json
from pathlib import Path
import queue
import struct
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from speech_tools import Endpoint, normalize_command, pcm_levels, split_wake, spoken_summary


def pcm(value=1000, samples=800):
    return struct.pack("<h", value) * samples


@pytest.mark.parametrize("word", ["лес", "тест", "кресло", "арест", "крестик"])
def test_similar_words_do_not_wake(word):
    assert split_wake(word + " время", "крест") == (False, word + " время")


def test_wake_boundary_and_punctuation():
    assert split_wake(" Крест, который час?", "крест") == (True, "который час?")
    assert split_wake("скажи крест время", "крест")[0] is False


def test_normalization_preserves_reminder_contents():
    assert normalize_command("Пожалуйста, сделай музыку потише") == "тише"
    assert normalize_command("Который час?") == "время"
    assert normalize_command("крес время", {"крес время": "время"}) == "время"
    assert (
        normalize_command("напомни проверить тест через пять минут")
        == "напомни через пять минут: проверить тест"
    )
    original = "напомни через пять минут: проверить тест"
    assert normalize_command(original) == original
    canonical = "напомни через пять минут: включить таймер через десять минут"
    assert normalize_command(canonical) == canonical
    assert normalize_command("напомни про контру через обед") == "напомни про контру через обед"


def test_pcm_and_endpoint_respect_short_pause_and_reset():
    assert pcm_levels(b"") == (0, 0)
    assert pcm_levels(pcm(-32768)) == (32768, 32768)
    endpoint = Endpoint(silence_ms=700)
    assert not endpoint.feed(20, 10)  # Background alone is not an utterance.
    assert not endpoint.feed(1000, 0.2)
    assert not endpoint.feed(0, 0.4)
    assert not endpoint.feed(1000, 0.2)  # A word after a short pause continues the phrase.
    assert not endpoint.feed(0, 0.65)
    assert endpoint.feed(0, 0.05)
    endpoint.reset()
    assert not endpoint.started
    assert endpoint.feed(1000, 20)  # Bound continuous input even without a pause.


def test_short_summary_retains_complete_sentences():
    assert spoken_summary("Готово.") == "Готово."
    text = "Достаточно длинное первое предложение для проверки паузы. " + "Подробности " * 80
    short = spoken_summary(text)
    assert (
        short == "Достаточно длинное первое предложение для проверки паузы. Полный ответ — в меню."
    )


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Import the real engine with no model downloads or audio hardware."""
    torch = MagicMock()
    sd = MagicMock()
    vosk = MagicMock()
    vosk.Model.side_effect = RuntimeError("No real model in this test")
    config = SimpleNamespace(
        reload_config=lambda: {},
        MODEL_VOSK_PATH=str(tmp_path / "vosk"),
        MODEL_SILERO_PATH=str(tmp_path / "absent.pt"),
        SAMPLE_RATE=16000,
    )
    for name, value in {
        "torch": torch,
        "sounddevice": sd,
        "vosk": vosk,
        "numpy": MagicMock(),
        "num2words": SimpleNamespace(
            num2words=lambda n, **kw: {1: "один", 2: "два"}.get(n, str(n))
        ),
        "config": config,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    spec = importlib.util.spec_from_file_location(
        "test_voice_engine", Path(__file__).parents[1] / "voice_engine.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.stream = SimpleNamespace(active=True)
    module.rec = MagicMock()
    module.rec.AcceptWaveform.return_value = False
    module.rec.PartialResult.return_value = '{"partial": "крест"}'
    module.rec.FinalResult.return_value = json.dumps(
        {"text": "крест время", "result": [{"conf": 0.9}, {"conf": 0.8}]}
    )
    module.configure(lambda: {"app_settings": {"voice_effect": False, "voice_volume": 100}})
    module.rec.Reset.reset_mock()
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    module.silero_path = str(tmp_path / "model.pt")
    Path(module.silero_path).touch()
    module.TTS_CACHE_DIR = str(tmp_path)
    return module


def feed(engine, value, blocks=1):
    for _ in range(blocks):
        engine.audio_callback(pcm(value), 800, None, None)
    return engine.listen()


def test_streaming_retains_preroll_and_waits_for_configured_pause(engine):
    assert not feed(engine, 10, 6)
    assert engine.rec.AcceptWaveform.call_count == 0
    assert not feed(engine, 1000)
    assert engine.rec.AcceptWaveform.call_count == 4  # 150 ms lead-in + speech
    assert not feed(engine, 0, 8)
    assert not feed(engine, 1000)  # Resume before the end-of-phrase threshold.
    assert not feed(engine, 0, 13)
    assert feed(engine, 0) == "крест время"
    assert abs(engine.last_recognition["confidence"] - 0.85) < 1e-9
    assert engine.rec.Reset.call_count == 1
    assert engine.q.unfinished_tasks == 0


def test_vosk_early_endpoint_does_not_dispatch_a_partial_command(engine):
    engine.rec.AcceptWaveform.side_effect = [True] + [False] * 15
    engine.rec.Result.return_value = '{"text":"крест","result":[{"conf":0.8}]}'
    engine.rec.FinalResult.return_value = '{"text":"время","result":[{"conf":1.0}]}'
    assert not feed(engine, 1000)
    assert not feed(engine, 0, 13)
    assert feed(engine, 0) == "крест время"
    assert engine.last_recognition["confidence"] == 0.9


def test_overflow_discards_stale_audio_and_recovers(engine):
    for _ in range(35):
        engine.audio_callback(pcm(), 800, None, None)
    assert engine.q.qsize() == 20
    assert engine.metrics["dropped"] == 15
    assert not engine.listen()
    assert engine.q.empty() and engine.q.unfinished_tasks == 0
    engine.rec.AcceptWaveform.assert_not_called()
    assert not feed(engine, 1000)
    assert feed(engine, 0, 14) == "крест время"


def test_speech_does_not_listen_to_its_own_audio(engine):
    engine.is_speaking = True
    engine.audio_callback(pcm(), 800, None, None)
    assert engine.q.empty()
    assert engine.listen() == ""


def test_cancel_during_synthesis_never_starts_playback(engine):
    def synthesis(**kwargs):
        engine.interrupt()
        return MagicMock()

    engine.model_tts = MagicMock()
    engine.model_tts.apply_tts.side_effect = synthesis
    engine.speak("Готово")
    engine.sd.play.assert_not_called()
    engine.sd.stop.assert_called_once()
    assert not engine.is_speaking


def test_preparation_failure_restores_listening(engine, monkeypatch):
    engine.model_tts = MagicMock()
    monkeypatch.setattr(
        engine, "prepare_text", MagicMock(side_effect=ValueError("bad pronunciation"))
    )
    engine.speak("Готово")
    assert not engine.is_speaking
    engine.sd.play.assert_not_called()
    assert engine.rec.Reset.call_count == 2


def test_custom_pronunciation_and_time_without_changing_voice(engine):
    engine.configure(lambda: {"pronunciations": {"Discord": "дискордик"}})
    text = engine.prepare_text("Discord в 1:02")
    assert "дискордик" in text and "один час две минуты" in text
    engine.model_tts = MagicMock()
    engine.speak("Готово")
    assert engine.model_tts.apply_tts.call_args.kwargs["speaker"] == "xenia"
    assert engine.model_tts.apply_tts.call_args.kwargs["sample_rate"] == 24000


def test_confidence_gate_blocks_skill_but_not_text_commands(tmp_path):
    from app_runtime import AssistantRuntime
    from configuration import ConfigManager
    from skill_manager import CommandResult

    cfg = ConfigManager(tmp_path / "config.json")
    app = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    app.manager.execute = MagicMock(return_value=CommandResult(True, True, "Готово"))
    try:
        result = app.execute("крест время", "voice", confidence=0.3)
        assert not result.success
        app.manager.execute.assert_not_called()
        assert not app.execute("тест время", "voice", confidence=0.99).handled
        app.manager.execute.assert_not_called()
        assert app.execute("крест время", "voice", confidence=0.9).success
        app.manager.execute.assert_called_once()
        assert app.execute("время", "text", confidence=0.1).success
        observed = []
        while not app.events.empty():
            kind, data = app.events.get_nowait()
            if kind == "recognition":
                observed.append(data)
        assert any("Низкая уверенность" in v["status"] for v in observed)
        assert observed[-1]["command"] == "время"
    finally:
        app.close()


def test_stop_speech_cancels_generation_and_pending_replies(monkeypatch):
    import voice

    fake = MagicMock()
    audio_queue = queue.Queue()
    monkeypatch.setattr(voice, "_engine", fake)
    monkeypatch.setattr(voice, "_audio_queue", audio_queue)
    monkeypatch.setattr(voice, "_generation", 10)
    audio_queue.put((10, "Первый ответ"))
    audio_queue.put((10, "Второй ответ"))
    voice.stop_speaking()
    assert voice._generation == 11
    assert audio_queue.empty() and audio_queue.unfinished_tasks == 0
    fake.interrupt.assert_called_once()


def test_microphone_calibration_and_failed_device_recovery(monkeypatch):
    from microphone_test import MicrophoneTest

    probe = MicrophoneTest()
    probe.stop = MagicMock()
    probe.stop.is_set.return_value = False

    def stream(**kwargs):
        kwargs["callback"](pcm(100), 800, None, None)
        return MagicMock()

    sd = SimpleNamespace(RawInputStream=MagicMock(side_effect=stream))
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    probe.record("3", calibrate=True)
    assert probe.noise_threshold == 230
    assert not probe.samples and not probe.busy
    probe.record("3")
    assert probe.samples == pcm(100)
    assert sd.RawInputStream.call_args.kwargs["device"] == 3
    sd.RawInputStream.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        probe.record()
    assert not probe.busy and not probe.samples


def test_long_spoken_reply_keeps_full_text_in_menu(monkeypatch):
    import voice

    audio_queue = queue.Queue()
    output = []
    monkeypatch.setattr(voice, "_engine", MagicMock())
    monkeypatch.setattr(voice, "_audio_thread", MagicMock())
    monkeypatch.setattr(voice, "_audio_queue", audio_queue)
    monkeypatch.setattr(voice, "_state", SimpleNamespace(is_muted=False))
    monkeypatch.setattr(voice, "_settings", lambda: {})
    monkeypatch.setattr(voice, "_output", output.append)
    text = "Описание предмета. " * 50
    with voice.capture() as captured:
        voice.speak(text)
    assert output == captured == [text.strip()]
    _, spoken = audio_queue.get_nowait()
    assert len(spoken) < len(text)
    assert spoken.endswith("Полный ответ — в меню.")


@pytest.mark.parametrize(
    "key,value",
    [
        ("speech_pause_ms", 1),
        ("speech_threshold", 0),
        ("speech_confidence", 101),
        ("tts_threads", 8),
    ],
)
def test_invalid_speech_settings_do_not_replace_saved_configuration(tmp_path, key, value):
    from configuration import ConfigManager

    cfg = ConfigManager(tmp_path / "config.json")
    cfg.save_config()
    before = cfg.filepath.read_bytes()
    draft = cfg.snapshot()
    draft["app_settings"][key] = value
    with pytest.raises(ValueError):
        cfg.save_config(draft)
    assert cfg.filepath.read_bytes() == before


def test_microphone_overflow_does_not_propose_bad_threshold(monkeypatch):
    from microphone_test import MicrophoneTest

    probe = MicrophoneTest()
    probe.stop = MagicMock()
    probe.stop.is_set.return_value = False

    def stream(**kwargs):
        kwargs["callback"](pcm(100), 800, None, "overflow")
        return MagicMock()

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(RawInputStream=stream))
    with pytest.raises(RuntimeError, match="пропусками"):
        probe.record(calibrate=True)
    assert probe.noise_threshold is None
    assert not probe.busy


def test_embedded_or_negated_dangerous_keywords_never_dispatch():
    from skill_manager import SkillManager

    manager = SkillManager()
    for phrase in (
        "я не хочу чтобы ты закрой окно",
        "объясни команду очисти корзину",
        "ты вчера сказала выключись",
        "не надо закрой окно",
        "не выключись",
    ):
        assert manager.match(phrase) is None, phrase
    assert manager.match("очисти корзину") is not None
    assert manager.match("напомни через пять минут не закрывать окно") is not None


def test_removed_microphone_active_property_error_retries_without_killing_listener(
    engine, monkeypatch
):
    class Removed:
        @property
        def active(self):
            raise RuntimeError("Device removed")

    engine.stream = Removed()
    attempts = []
    monkeypatch.setattr(engine, "start_microphone", lambda: attempts.append(True) or False)
    assert engine.listen() == "" and attempts == [True]


def test_microphone_backend_refresh_is_rate_limited_and_only_after_failure(engine, monkeypatch):
    import audio_devices

    calls = []
    monkeypatch.setattr(audio_devices, "refresh_idle_backend", lambda sd: calls.append("refresh"))
    monkeypatch.setattr(
        audio_devices,
        "open_microphone",
        lambda *a: (
            SimpleNamespace(active=True, stop=lambda: None, close=lambda: None),
            {"name": "USB", "index": 1, "input_rate": 48000},
        ),
    )
    engine.last_mic_error_time = engine.time.time() - 10
    assert engine.start_microphone()
    assert engine.metrics["device_name"] == "USB" and engine.metrics["connected"]
    assert engine.start_microphone()
    assert calls == ["refresh"]


def test_voice_autostart_inspects_paths_without_opening_devices(tmp_path):
    import voice

    cfg = {
        "app_settings": {
            "vosk_path": str(tmp_path / "vosk"),
            "silero_path": str(tmp_path / "silero.pt"),
        }
    }
    assert not voice.local_models_ready(cfg)
    (tmp_path / "vosk" / "am").mkdir(parents=True)
    (tmp_path / "vosk" / "conf").mkdir()
    (tmp_path / "vosk" / "am" / "final.mdl").touch()
    (tmp_path / "vosk" / "conf" / "model.conf").touch()
    assert voice.local_models_ready(cfg)  # Speech recognition does not require TTS.
