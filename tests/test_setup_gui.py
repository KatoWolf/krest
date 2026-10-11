import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("DISPLAY"), reason="GUI requires display")


def pump(app, check):
    end = time.monotonic() + 3
    while time.monotonic() < end:
        app.update()
        if check():
            return
        time.sleep(0.01)
    assert check()


def test_installer_checks_then_waits_for_click_installs_and_verifies(tmp_path, monkeypatch):
    import setup_app

    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(setup_app, "ROOT", tmp_path)
    reports = iter(
        [
            {"missing": ["winsdk"], "broken": []},
            {"missing": ["winsdk"], "broken": []},
            {"missing": [], "broken": []},
        ]
    )
    monkeypatch.setattr(setup_app, "inspect", lambda *args: next(reports))
    commands = []
    monkeypatch.setattr(setup_app, "run_command", lambda cmd, emit, cancel: commands.append(cmd))
    app = setup_app.SetupWindow("music", python, launch=False)
    try:
        pump(app, lambda: not app.busy)
        assert not commands
        assert app.button.cget("text") == "Подключить музыку"
        app.button.invoke()
        pump(app, lambda: app.ready)
        assert all(cmd[0] == str(python) for cmd in commands)
        assert app.button.cget("text") == "Готово"
        assert "готово" in app.status.get().casefold()
    finally:
        app.destroy()


def test_ready_launcher_does_not_install_and_opens_local_environment(tmp_path, monkeypatch):
    import setup_app
    from types import SimpleNamespace

    monkeypatch.setattr(setup_app, "inspect", lambda *a: {"missing": [], "broken": []})
    python = tmp_path / "python.exe"
    python.touch()
    opened = []
    monkeypatch.setattr(
        setup_app.subprocess, "Popen", lambda args, **kw: opened.append(args) or SimpleNamespace()
    )
    monkeypatch.setattr(setup_app, "run_command", lambda *a: pytest.fail("must not install"))
    app = setup_app.SetupWindow("all", python, launch=True)
    pump(app, lambda: bool(opened))
    assert opened[0] == [str(python), str(setup_app.ROOT / "main.py")]


def test_setup_failure_releases_lock_and_allows_retry(tmp_path, monkeypatch):
    import setup_app
    from process_lock import ProcessLock

    python = tmp_path / "python.exe"
    python.touch()
    monkeypatch.setattr(setup_app, "ROOT", tmp_path)
    monkeypatch.setattr(setup_app, "inspect", lambda *a: {"missing": ["winsdk"], "broken": []})

    def failure(*args):
        raise RuntimeError("Нет сети. Попробуйте ещё раз.")

    monkeypatch.setattr(setup_app, "run_command", failure)
    app = setup_app.SetupWindow("music", python, launch=False)
    try:
        pump(app, lambda: not app.busy)
        app.button.invoke()
        pump(app, lambda: not app.busy)
        assert "Нет сети" in app.status.get()
        assert app.button.cget("text") == "Повторить подключение"
        lock = ProcessLock(tmp_path / ".setup.lock")
        assert lock.acquire()
        lock.close()
        assert app.log_panel.opened
    finally:
        app.destroy()


def test_bundled_font_is_shared_by_buttons_fields_and_dialogs():
    import tkinter as tk
    from tkinter import font, ttk
    from themes import THEMES
    from ui_design import apply_style
    from ui_fonts import UI_FONT, load_fonts
    from PIL import ImageFont

    root_path = Path(__file__).parents[1] / "assets" / "fonts"
    assert ImageFont.truetype(str(root_path / "Roboto-Regular.ttf"), 18).getname()[0] == "Roboto"
    load_fonts()
    root = tk.Tk()
    try:
        apply_style(root, THEMES["Amethyst Modern"])
        assert font.nametofont("TkDefaultFont", root=root).cget("family") == UI_FONT
        assert UI_FONT in ttk.Style(root).lookup("TButton", "font")
        assert (root_path / "LICENSE.txt").is_file()
    finally:
        root.destroy()


def test_closed_setup_releases_tk_variable_before_worker_gc(tmp_path, monkeypatch):
    """Closing mid-check must never leave Tcl finalization to the worker."""
    import gc
    import threading
    import weakref
    import setup_app

    executable = tmp_path / "python.exe"
    executable.touch()
    started, release, finished = (threading.Event() for _ in range(3))
    main_thread = threading.get_ident()
    finalized_on = []
    original_del = setup_app.tk.Variable.__del__

    def tracked_del(variable):
        finalized_on.append(threading.get_ident())
        original_del(variable)

    def inspect(*args):
        started.set()
        assert release.wait(3)
        gc.collect()
        finished.set()
        return {"missing": [], "broken": []}

    monkeypatch.setattr(setup_app.tk.Variable, "__del__", tracked_del)
    monkeypatch.setattr(setup_app, "inspect", inspect)
    app = setup_app.SetupWindow("music", executable, launch=False)
    assert started.wait(2)
    variable = weakref.ref(app.status)
    app.destroy()
    del app
    try:
        # Destruction must happen synchronously on the caller's Tk thread,
        # rather than just happen to pass when a later GC runs on that thread.
        assert variable() is None
        assert finalized_on == [main_thread]
    finally:
        release.set()
        assert finished.wait(3)
    assert finalized_on == [main_thread]
