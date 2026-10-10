import asyncio
import importlib.util
import json
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import zipfile

import pytest

from command_parser import parse_command
from configuration import ConfigManager
from skill_manager import SkillManager


def test_parser_uses_real_aliases_and_never_executes(tmp_path, monkeypatch):
    manager = SkillManager(lambda: {})
    entry = manager.match("сделай скриншот")[0]
    monkeypatch.setattr(entry, "func", MagicMock(side_effect=AssertionError("executed")))
    # Function mock changes its identity; use another read-only command below.
    result = parse_command(manager, "Крест, громкость на 35", {})
    assert result["wake"] and result["parameters"]
    assert result["skill"].endswith(".set_exact_volume")
    entry.func.assert_not_called()
    alias = parse_command(
        manager, "крест моя фраза", {"command_aliases": {"моя фраза": "который час"}}
    )
    assert alias["skill"].startswith("skills.basic.")
    assert not parse_command(manager, "не выключай компьютер", {})["skill"]


def test_screenshots_are_saved_as_unique_complete_pngs(tmp_path):
    from screenshots import capture_screen
    from PIL import Image

    paths = [
        capture_screen(tmp_path, grab=lambda **kw: Image.new("RGB", (30, 20), "red"))
        for _ in range(2)
    ]
    assert paths[0] != paths[1]
    for path in paths:
        with Image.open(path) as image:
            assert image.size == (30, 20) and image.format == "PNG"
    assert not list(tmp_path.glob("*.tmp"))


def test_screenshot_failures_do_not_report_saved_or_leave_files(tmp_path):
    from screenshots import capture_screen

    image = MagicMock()
    image.save.side_effect = OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        capture_screen(tmp_path, grab=lambda **kwargs: image)
    image.close.assert_called_once()
    assert not list(tmp_path.iterdir())


def test_screenshot_and_snipping_are_separate_skills():
    manager = SkillManager()
    assert manager.match("сделай скриншот")[0].func.__name__ == "take_screenshot"
    assert manager.match("ножницы")[0].func.__name__ == "snip_region"
    assert manager.match("открой скриншоты")[0].func.__name__ == "show_screenshots"


@pytest.mark.parametrize(
    "style", ["friendly", "concise", "lively", "calm", "tactical", "ironic", "gothic"]
)
def test_personalities_validate_and_respond_without_model(tmp_path, style):
    from personality import reply, CHARACTERS, REPLIES

    cfg = ConfigManager(tmp_path / "config.json")
    draft = cfg.snapshot()
    draft["profile"]["personality"] = style
    cfg.save_config(draft)
    state = SimpleNamespace(owner_name="Саша", config_getter=cfg.snapshot)
    assert CHARACTERS[style]
    for kind in REPLIES:
        text = reply(kind, state)
        assert text and "{name}" not in text


def test_quiz_has_explicit_session_context_and_clear_answers():
    from skills.interactive import quiz, quiz_answer

    state = SimpleNamespace()
    assert "Сначала" in quiz_answer(state, "луна")
    assert "мой ответ" in quiz(state)
    answer = state._quiz_answer
    assert "Верно" in quiz_answer(state, answer)
    assert state._quiz_answer is None


def test_ocr_uses_static_dimension_and_closes_resources(monkeypatch):
    from game_vision import _read_image

    engine = SimpleNamespace(
        recognize_async=AsyncMock(return_value=SimpleNamespace(text="Foundry"))
    )
    factory = SimpleNamespace(
        max_image_dimension=1024, try_create_from_user_profile_languages=lambda: engine
    )
    bitmap = MagicMock()
    decoder = SimpleNamespace(get_software_bitmap_async=AsyncMock(return_value=bitmap))
    stream, writer = MagicMock(), MagicMock()
    writer.store_async = AsyncMock()
    writer.flush_async = AsyncMock()
    modules = {
        "winsdk.windows.media.ocr": SimpleNamespace(OcrEngine=factory),
        "winsdk.windows.graphics.imaging": SimpleNamespace(
            BitmapDecoder=SimpleNamespace(create_async=AsyncMock(return_value=decoder)),
            BitmapPixelFormat=SimpleNamespace(BGRA8=1),
            BitmapAlphaMode=SimpleNamespace(PREMULTIPLIED=1),
        ),
        "winsdk.windows.storage.streams": SimpleNamespace(
            InMemoryRandomAccessStream=lambda: stream, DataWriter=lambda s: writer
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    image = MagicMock()
    assert asyncio.run(_read_image(image)) == "Foundry"
    image.thumbnail.assert_called_once_with((1024, 1024))
    stream.close.assert_called_once()
    bitmap.close.assert_called_once()


@pytest.mark.skipif(os.name != "nt", reason="Windows OCR API")
def test_windows_ocr_static_dimension_exists():
    from winsdk.windows.media.ocr import OcrEngine

    assert OcrEngine.max_image_dimension > 0


def test_vosk_extraction_rejects_escape_paths(tmp_path):
    from model_setup import unpack_vosk

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../../escape", "bad")
    with pytest.raises(ValueError, match="путь"):
        unpack_vosk(archive, tmp_path / "extract")
    assert not (tmp_path.parent / "escape").exists()


def test_auto_models_keep_existing_and_publish_only_complete_files(tmp_path, monkeypatch):
    import model_setup
    import voice_models

    monkeypatch.setattr(model_setup, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(model_setup, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(voice_models, "BASE_DIR", tmp_path)
    downloads = []

    def download(url, path, progress, cancelled):
        downloads.append(url)
        if url == model_setup.VOSK_URL:
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("vosk/am/final.mdl", "model")
                z.writestr("vosk/conf/model.conf", "config")
        else:
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("archive/model.pkl", "fixture, never executed")

    monkeypatch.setattr(model_setup, "download", download)
    models = model_setup.prepare({})
    assert all(Path(p).exists() for p in models.values())
    assert len(downloads) == 2
    assert model_setup.prepare({}) == models and len(downloads) == 2
    assert not list((tmp_path / "models").glob(".prepare-*"))


def test_compiled_process_and_data_paths_preserve_source_mode(tmp_path, monkeypatch):
    import app_paths

    monkeypatch.setattr(app_paths, "compiled", lambda: True)
    monkeypatch.delenv("KREST_CONFIG_PATH", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert app_paths.app_command("--kernel") == [sys.executable, "--kernel"]
    assert app_paths.data_dir() == tmp_path / "Krest" / "Data"
    monkeypatch.setenv("KREST_CONFIG_PATH", str(tmp_path / "custom" / "config.json"))
    assert app_paths.data_dir() == tmp_path / "custom"


def test_packaged_skills_do_not_require_source_files(tmp_path, monkeypatch):
    import app_paths

    monkeypatch.setattr(app_paths, "compiled", lambda: True)
    manager = SkillManager()
    manager.base_dir = tmp_path
    manager.load_skills()
    assert not manager.errors and len(manager.catalog()) >= 158
    from skills import BUILTIN_MODULES

    assert set(BUILTIN_MODULES) == {
        p.stem
        for p in (Path(__file__).parents[1] / "skills").glob("*.py")
        if not p.name.startswith("_")
    }


def test_build_plan_and_audit_exclude_user_sources(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "release_builder", Path(__file__).parents[1] / "packaging" / "build_windows.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    cmd = builder.compiler_command(tmp_path)
    assert "--mode=standalone" in cmd and "--include-package=skills" in cmd
    assert not any("private" in v or "config.json=" in v for v in cmd)
    (tmp_path / "Krest.exe").touch()
    builder.audit_distribution(tmp_path)
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(RuntimeError, match="Личные данные"):
        builder.audit_distribution(tmp_path)


def test_real_child_kernel_release_check(tmp_path):
    result = subprocess.run(
        [sys.executable, "main.py", "--release-smoke", "--config", str(tmp_path / "config.json")],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr


def test_every_builtin_keyword_and_example_has_its_intended_handler():
    from speech_tools import normalize_command

    manager = SkillManager()
    for entry in manager.catalog():
        for phrase in entry.keywords + entry.examples:
            found = manager.match(normalize_command(phrase))
            assert found and found[0].id == entry.id, (phrase, entry.id)


def test_voice_retry_recovers_after_missing_models_without_restarting_app(tmp_path, monkeypatch):
    from app_runtime import AssistantRuntime

    cfg = ConfigManager(tmp_path / "config.json")
    runtime = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    attempts = []

    class NoDelay:
        def is_set(self):
            return False

        def wait(self, timeout):
            return False

        def set(self):
            pass

        def clear(self):
            pass

    runtime._voice_stop = NoDelay()

    def attempt():
        attempts.append(1)
        return "Модель отсутствует" if len(attempts) == 1 else ""

    monkeypatch.setattr(runtime, "_voice_attempt", attempt)
    try:
        runtime._voice_loop()
        assert len(attempts) == 2
    finally:
        runtime.close()


@pytest.mark.skipif(not os.getenv("DISPLAY"), reason="Tk display required")
def test_pages_build_independently_and_parser_is_a_separate_window(tmp_path, monkeypatch):
    import welcome_gui
    from ui_design import RoundedCard
    import tkinter as tk

    monkeypatch.setattr(welcome_gui.ControlCenterApp, "_start_after_profile", lambda self: None)
    cfg = ConfigManager(tmp_path / "config.json")
    draft = cfg.snapshot()
    draft["owner_name"] = "Тест"
    draft["profile"]["onboarding_completed"] = True
    cfg.save_config(draft)
    app = welcome_gui.ControlCenterApp(config_path=cfg.filepath)
    try:
        app.switch_page("memory")
        assert app._built_pages == {"actions", "memory"}
        app.switch_page("wf_home")
        assert "wf_arsenal" not in app._built_pages
        assert isinstance(RoundedCard(app, app.theme), tk.Frame)
        assert app.effects.window is None
        app._open_command_parser()
        assert app._parser_window.winfo_exists()
        first = app._parser_window
        app._open_command_parser()
        assert app._parser_window is first
    finally:
        app.runtime.close()
        app.integration.stop_all()
        import logging

        logging.getLogger().removeHandler(app._log_handler)
        welcome_gui.indicator_controller.stop()
        app.destroy()


def test_atomic_storage_retries_windows_sharing_conflicts_without_deleting_old_file(
    tmp_path, monkeypatch
):
    import storage

    path = tmp_path / "config.json"
    storage.atomic_write_json(path, {"value": "old"})
    original = storage.os.replace
    attempts = []

    def replace(source, target):
        attempts.append(1)
        assert json.loads(path.read_text())["value"] == "old"
        if len(attempts) < 3:
            error = PermissionError("Sharing violation")
            error.winerror = 32
            raise error
        original(source, target)

    monkeypatch.setattr(storage.os, "replace", replace)
    monkeypatch.setattr(storage.time, "sleep", lambda delay: None)
    storage.atomic_write_json(path, {"value": "new"})
    assert len(attempts) == 3 and json.loads(path.read_text())["value"] == "new"
    assert list(tmp_path.iterdir()) == [path]


def test_atomic_reader_retries_sharing_error_but_does_not_hide_corrupt_json(monkeypatch):
    import storage

    error = PermissionError("File is being replaced")
    error.winerror = 32
    reader = MagicMock(side_effect=[error, '{"ready":true}'])
    monkeypatch.setattr(storage, "Path", lambda path: SimpleNamespace(read_text=reader))
    monkeypatch.setattr(storage.time, "sleep", lambda delay: None)
    assert storage.read_json("config.json") == {"ready": True}
    assert reader.call_count == 2
    reader.side_effect = None
    reader.return_value = "not json"
    with pytest.raises(ValueError):
        storage.read_json("config.json")
