import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app_icons import ASSETS, load_image
from configuration import DEFAULT_CONFIG
from desktop_shell import DesktopShell
from local_setup import discover_settings


class Shell(DesktopShell):
    def __init__(self, icon):
        self.init_desktop_shell()
        self.closing = False
        self.onboarding = None
        self.config_mgr = SimpleNamespace(config={"app_settings": {"close_to_tray": True}})
        self._new_tray_icon = Mock(return_value=icon)
        self.withdraw = Mock()
        self.deiconify = Mock()
        self.lift = Mock()
        self.focus_force = Mock()
        self.state = Mock()
        self._report_error = Mock()
        self._real_quit = Mock()
        self.status_var = SimpleNamespace(set=Mock())
        self.after = Mock(return_value="timeout")
        self.after_cancel = Mock()
        self.pending = []

    def post_ui(self, fn):
        self.pending.append(fn)

    def _background(self, fn):
        fn()

    def flush(self):
        while self.pending:
            self.pending.pop(0)()


def test_tray_waits_for_readiness_before_hiding_and_reuses_icon():
    icon = Mock()
    app = Shell(icon)
    assert app.hide_to_tray()
    app.withdraw.assert_not_called()
    icon.run_detached.call_args.kwargs["setup"](icon)
    app.flush()
    app.withdraw.assert_called_once()
    assert app.tray_state == "ready" and icon.visible
    app.restore_window()
    app.deiconify.assert_called_once()
    assert app.hide_to_tray()
    app._new_tray_icon.assert_called_once()
    app.after_cancel.assert_called_once_with("timeout")


def test_tray_failure_keeps_window_open_instead_of_quitting():
    app = Shell(Mock())
    app._new_tray_icon.side_effect = RuntimeError("missing backend")
    app.on_close_window()
    app.withdraw.assert_not_called()
    app._real_quit.assert_not_called()
    app.deiconify.assert_called_once()
    assert app.tray_state == "failed"
    assert "missing backend" in app._report_error.call_args.args[0]


def test_tray_timeout_and_late_callback_cannot_hide_window():
    icon = Mock()
    app = Shell(icon)
    app.hide_to_tray()
    app.after.call_args.args[1]()
    assert app.tray_icon is None
    icon.run_detached.call_args.kwargs["setup"](icon)
    app.flush()
    app.withdraw.assert_not_called()
    assert icon.stop.call_count == 2


def test_starting_tray_does_not_hide_a_visible_window():
    icon = Mock()
    app = Shell(icon)
    app.ensure_tray()
    icon.run_detached.call_args.kwargs["setup"](icon)
    app.flush()
    app.withdraw.assert_not_called()
    assert app.tray_state == "ready"


def test_quitting_before_tray_ready_stops_late_icon_without_ui_callback():
    icon = Mock()
    app = Shell(icon)
    app.ensure_tray()
    app.closing = True
    icon.run_detached.call_args.kwargs["setup"](icon)
    icon.stop.assert_called_once()
    assert not app.pending


def make_vosk(root):
    (root / "am").mkdir(parents=True)
    (root / "conf").mkdir()
    (root / "am" / "final.mdl").write_bytes(b"model fixture")
    (root / "conf" / "model.conf").write_text("test")


def test_local_discovery_fills_default_paths_without_changing_opt_ins(tmp_path, monkeypatch):
    monkeypatch.setattr("skills.webo.get_steam_install_path", lambda: str(tmp_path / "Steam"))
    root = tmp_path / "project"
    model = root / "model" / "vosk-small-ru"
    make_vosk(model)
    tts = root / "silero-ru.pt"
    tts.write_bytes(b"fixture")
    home = tmp_path / "user"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["app_settings"]["downloads_path"] = ""
    updated, changes = discover_settings(config, root, home)
    assert Path(updated["app_settings"]["vosk_path"]) == model
    assert Path(updated["app_settings"]["silero_path"]) == tts
    assert Path(updated["app_settings"]["downloads_path"]) == downloads
    assert updated["steam"]["install_path"] == str(tmp_path / "Steam")
    assert not updated["app_settings"]["downloads_auto_sort"]
    assert not updated["app_settings"]["autostart"]
    assert updated["api_keys"] == config["api_keys"]
    assert config["app_settings"]["vosk_path"] == "model"
    assert changes


def test_local_discovery_preserves_manual_paths_and_ambiguous_models(tmp_path, monkeypatch):
    monkeypatch.setattr("skills.webo.get_steam_install_path", lambda: None)
    make_vosk(tmp_path / "model-a")
    make_vosk(tmp_path / "model-b")
    config = copy.deepcopy(DEFAULT_CONFIG)
    updated, changes = discover_settings(config, tmp_path)
    assert updated["app_settings"]["vosk_path"] == "model"
    config["app_settings"]["vosk_path"] = "my-own-model"
    config["app_settings"]["silero_path"] = "voice/custom.pt"
    config["steam"]["install_path"] = "custom-steam"
    updated, changes = discover_settings(config, tmp_path)
    assert updated["app_settings"]["vosk_path"] == "my-own-model"
    assert updated["app_settings"]["silero_path"] == "voice/custom.pt"
    assert updated["steam"]["install_path"] == "custom-steam"


def test_icon_contains_windows_sizes_and_loads_without_working_directory(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.chdir(tmp_path)
    assert load_image(32).size == (32, 32)
    with Image.open(ASSETS / "krest.ico") as icon:
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= icon.ico.sizes()
