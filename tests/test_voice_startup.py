"""Regression tests for drop-in models and Windows console startup."""

import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from voice_models import discover_models
from speech_tools import AdaptiveNoiseFloor


def model(root):
    (root / "am").mkdir(parents=True)
    (root / "conf").mkdir()
    (root / "am" / "final.mdl").touch()
    (root / "conf" / "model.conf").touch()
    return root


def test_drop_in_nested_model_repairs_old_machine_paths(tmp_path, monkeypatch):
    vosk = model(tmp_path / "model" / "vosk-model-small-ru")
    tts = tmp_path / "model" / "v4_ru.pt"
    tts.touch()
    monkeypatch.chdir(tmp_path.parent)
    found = discover_models(
        {"app_settings": {"vosk_path": "Z:/old/model", "silero_path": "Z:/old/model.pt"}}, tmp_path
    )
    assert found == {"vosk_path": str(vosk), "silero_path": str(tts)}


def test_exact_model_directory_wins_over_other_models_and_preserves_custom(tmp_path):
    primary = model(tmp_path / "model")
    other = model(tmp_path / "models" / "other")
    assert discover_models({}, tmp_path)["vosk_path"] == str(primary)
    assert discover_models({"app_settings": {"vosk_path": str(other)}}, tmp_path)[
        "vosk_path"
    ] == str(other)
    assert discover_models({}, tmp_path)["silero_path"] == ""


def test_partial_or_ambiguous_models_are_not_guessed(tmp_path):
    (tmp_path / "model").mkdir()
    assert not discover_models({}, tmp_path)["vosk_path"]
    model(tmp_path / "models" / "one")
    model(tmp_path / "models" / "two")
    assert not discover_models({}, tmp_path)["vosk_path"]


def test_models_load_with_strict_cp1251_stdout(tmp_path, monkeypatch):
    # The former ready-message contained U+2705 and aborted a successful model load.
    tts = tmp_path / "model_silero.pt"
    tts.touch()
    config = SimpleNamespace(
        reload_config=lambda: {}, MODEL_VOSK_PATH=str(tmp_path), MODEL_SILERO_PATH=str(tts)
    )
    for name, value in {
        "torch": MagicMock(),
        "vosk": MagicMock(),
        "sounddevice": MagicMock(),
        "numpy": MagicMock(),
        "num2words": MagicMock(),
        "config": config,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1251", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    spec = importlib.util.spec_from_file_location(
        "encoding_voice_engine", Path(__file__).parents[1] / "voice_engine.py"
    )
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    assert engine.rec is not None and engine.model_tts is not None


def test_silence_adaptation_is_bounded_and_does_not_learn_speech():
    floor = AdaptiveNoiseFloor()
    for _ in range(80):
        floor.feed(8)
    assert 35 <= floor.threshold <= 50
    original = floor.threshold
    for _ in range(200):
        floor.feed(14000, speaking=True)
    assert floor.threshold == original
    floor.reset()
    for _ in range(200):
        floor.feed(32000)
    assert floor.threshold <= 900
    assert len(floor.samples) == 120


def test_recognition_can_start_without_silero_or_torch(tmp_path, monkeypatch):
    import voice
    import voice_models

    root = model(tmp_path / "model")
    monkeypatch.setattr(voice_models, "BASE_DIR", tmp_path)
    settings = {"app_settings": {"vosk_path": str(root)}}
    monkeypatch.setattr(voice, "_settings", lambda: settings)
    monkeypatch.setattr(voice, "_engine", None)
    monkeypatch.setattr(voice, "_model_signature", None)
    engine = SimpleNamespace(
        rec=object(),
        model_tts=None,
        metrics={},
        stream=None,
        configure=MagicMock(),
        start_microphone=MagicMock(),
    )
    monkeypatch.setattr(
        voice.importlib.util,
        "find_spec",
        lambda name: None if name in ("torch", "num2words") else object(),
    )
    config = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setattr(voice.importlib, "reload", lambda module: module)
    monkeypatch.setattr(voice.importlib, "import_module", lambda name: engine)
    voice.enable()
    engine.start_microphone.assert_called_once()
    assert voice._engine is engine
    assert not engine.metrics["tts_ready"]
    assert config.MODEL_VOSK_PATH == str(root)
