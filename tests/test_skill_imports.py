from pathlib import Path
import sys
import time

import pytest

from configuration import ConfigManager
from skill_manager import SkillManager, SKILL_REGISTRY, _registry_lock
from skill_importer import inspect_source


@pytest.fixture
def manager(tmp_path):
    mgr = SkillManager(plugin_dir=tmp_path / "profile" / "imported_skills")
    yield mgr
    with _registry_lock:
        SKILL_REGISTRY[:] = [
            e for e in SKILL_REGISTRY if not e.func.__module__.startswith(mgr.imports.prefix)
        ]
    for name in list(sys.modules):
        if name.startswith(mgr.imports.prefix):
            del sys.modules[name]


def source(path, phrase="проверка импорта", result="Импорт работает"):
    path.write_text(
        "from skill_manager import skill\n@skill(keywords=["
        + repr(phrase)
        + '], description="Тест импорта")\ndef hello():\n    return '
        + repr(result)
        + "\n",
        encoding="utf-8",
    )
    return path


def install(manager, path, **kwargs):
    preview = manager.imports.preview(path)
    return manager.imports.install(path, preview["sha256"], **kwargs)


def test_preview_never_executes_and_plain_functions_get_ui_bindings(tmp_path, manager):
    marker = tmp_path / "executed"
    path = tmp_path / "functions.py"
    path.write_text(
        f'from pathlib import Path\nPath({str(marker)!r}).touch()\ndef greet(text=""):\n    return "Получено: " + text\n',
        encoding="utf-8",
    )
    preview = manager.imports.preview(path)
    assert not marker.exists()
    assert preview["skills"][0]["function"] == "greet"
    manager.imports.install(path, preview["sha256"], {"greet": ["привет из файла"]})
    assert marker.exists()
    assert manager.execute("привет из файла").text == "Получено: привет из файла"


def test_import_persists_independent_of_source_and_can_disable_remove(tmp_path, manager):
    path = source(tmp_path / "new_skill.py")
    info = install(manager, path)
    path.unlink()
    restarted = SkillManager(plugin_dir=manager.imports.root)
    assert restarted.execute("проверка импорта").text == "Импорт работает"
    restarted.imports.change(info["id"], "disable")
    assert restarted.match("проверка импорта") is None
    assert not SkillManager(plugin_dir=manager.imports.root).match("проверка импорта")
    restarted.imports.change(info["id"], "enable")
    assert restarted.execute("проверка импорта").success
    restarted.imports.change(info["id"], "remove")
    assert restarted.imports.list()["modules"] == []
    assert not restarted.match("проверка импорта")
    assert not list(restarted.imports.root.glob("*.py"))


def test_update_replaces_commands_and_failure_preserves_previous_version(tmp_path, manager):
    path = source(tmp_path / "update.py", result="Первая версия")
    ident = install(manager, path)["id"]
    before = manager.imports.path.read_bytes()
    source(path, result="Неверная версия")
    path.write_text(path.read_text() + '\nraise ImportError("missing dependency")\n')
    with pytest.raises(ValueError, match="missing dependency"):
        install(manager, path)
    assert manager.execute("проверка импорта").text == "Первая версия"
    assert manager.imports.path.read_bytes() == before
    assert len(list(manager.imports.root.glob("*.py"))) == 1
    source(path, phrase="новая фраза", result="Вторая версия")
    assert install(manager, path)["id"] == ident
    assert manager.match("проверка импорта") is None
    assert manager.execute("новая фраза").text == "Вторая версия"


def test_conflicting_builtin_or_imported_command_is_rejected(tmp_path, manager):
    path = source(tmp_path / "conflict.py", phrase="время")
    original = manager.match("время")[0].id
    with pytest.raises(ValueError, match="уже занят"):
        install(manager, path)
    assert manager.match("время")[0].id == original
    first = source(tmp_path / "first.py")
    install(manager, first)
    with pytest.raises(ValueError, match="уже занят"):
        install(manager, source(tmp_path / "second.py"))
    assert len(manager.imports.list()["modules"]) == 1


def test_changed_source_and_failed_registry_write_are_atomic(tmp_path, manager, monkeypatch):
    import skill_importer

    path = source(tmp_path / "atomic.py")
    preview = manager.imports.preview(path)
    source(path, result="Changed")
    with pytest.raises(ValueError, match="изменился"):
        manager.imports.install(path, preview["sha256"])
    install(manager, path)
    before = manager.imports.path.read_bytes()
    source(path, result="Not committed")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(skill_importer, "atomic_write_json", fail)
    with pytest.raises(OSError, match="disk full"):
        install(manager, path)
    assert manager.execute("проверка импорта").text == "Changed"
    assert manager.imports.path.read_bytes() == before
    with pytest.raises(OSError):
        manager.imports.change(manager.imports.list()["modules"][0]["id"], "disable")
    assert manager.execute("проверка импорта").success


def test_profiles_are_isolated_and_safe_mode_does_not_load_imports(tmp_path, manager):
    path = source(tmp_path / "profile.py")
    install(manager, path)
    isolated = SkillManager(plugin_dir=tmp_path / "other" / "imported_skills")
    assert isolated.match("проверка импорта") is None
    safe = SkillManager(lambda: {"safe_mode": True}, plugin_dir=manager.imports.root)
    assert not safe.execute("проверка импорта").success
    with pytest.raises(ValueError, match="безопасном"):
        install(safe, path)


@pytest.mark.parametrize(
    "text", ["[]", '{"version":1,"modules":{"bad":{}}}', '{"version":99,"modules":{}}']
)
def test_corrupt_import_registry_does_not_prevent_builtin_startup(tmp_path, text):
    folder = tmp_path / "imported_skills"
    folder.mkdir()
    (folder / "registry.json").write_text(text)
    manager = SkillManager(plugin_dir=folder)
    assert manager.imports.list()["error"]
    assert manager.execute("время").success


def test_unsupported_signatures_and_invalid_patterns_are_explained(tmp_path):
    path = tmp_path / "bad.py"
    path.write_text("def needs_argument(filename):\n    return filename\n")
    with pytest.raises(ValueError, match="Не найдено функций"):
        inspect_source(path)
    path.write_text('from skill_manager import skill\n@skill(patterns=["["])\ndef f(): pass\n')
    with pytest.raises(ValueError, match="ошибка шаблона"):
        inspect_source(path)


def test_kernel_import_updates_remote_catalog_and_commands(tmp_path):
    from kernel_client import KernelClient, RemoteRuntime

    cfg = ConfigManager(tmp_path / "config.json")
    data = cfg.snapshot()
    data["profile"]["onboarding_completed"] = True
    data["owner_name"] = "Тест"
    cfg.save_config(data)
    client = KernelClient(cfg.filepath).ensure_started()
    remote = RemoteRuntime(client, cfg.snapshot)
    try:
        path = source(tmp_path / "live.py")
        preview = remote.skill_import("preview", path=str(path))
        info = remote.skill_import("install", path=str(path), sha256=preview["sha256"])
        deadline = time.monotonic() + 5
        while not any("проверка импорта" in e.keywords for e in remote.manager.catalog()):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert client.call("submit", text="проверка импорта", request_key="import-test")
        while True:
            job = client.call("job_result", request_key="import-test")
            if job and job["status"] == "completed":
                break
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert job["result"] == "Импорт работает"
        remote.skill_import("disable", id=info["id"])
        assert client.call("match", text="проверка импорта") is None
    finally:
        remote.close()


@pytest.mark.skipif(not __import__("os").getenv("DISPLAY"), reason="GUI requires display")
def test_menu_imports_plain_function_with_custom_phrase(tmp_path, monkeypatch):
    import welcome_gui
    import skill_import_pages
    from tkinter import ttk

    cfg = ConfigManager(tmp_path / "config.json")
    path = tmp_path / "plain.py"
    path.write_text(
        'def hello():\n    return "Привет из импортированного файла"\n', encoding="utf-8"
    )
    monkeypatch.setattr(skill_import_pages.filedialog, "askopenfilename", lambda **kw: str(path))
    app = welcome_gui.ControlCenterApp(config_path=cfg.filepath)
    failures = []
    app.report_callback_exception = lambda *args: failures.append(args)

    def wait_until(check):
        deadline = time.monotonic() + 6
        while not check():
            assert time.monotonic() < deadline, failures
            app.update()
            time.sleep(0.01)

    def children(widget):
        for child in widget.winfo_children():
            yield child
            yield from children(child)

    try:
        app.onboarding.destroy()
        app.onboarding = None
        app.switch_page("skills")
        app._choose_skill_import()
        wait_until(lambda: getattr(app, "_skill_import_dialog", None) is not None)
        dialog = app._skill_import_dialog
        app.update()
        field = next(w for w in children(dialog) if isinstance(w, ttk.Entry))
        field.delete(0, "end")
        field.insert(0, "поздоровайся из модуля")
        button = next(
            w
            for w in children(dialog)
            if isinstance(w, ttk.Button) and w.cget("text") == "Подключить команды"
        )
        button.invoke()
        wait_until(lambda: not dialog.winfo_exists())
        wait_until(lambda: bool(app.import_tree.get_children()))
        assert (
            app.runtime.manager.execute("поздоровайся из модуля").text
            == "Привет из импортированного файла"
        )
        assert any("поздоровайся из модуля" in entry.keywords for entry in app.catalog.values())
        ident = app.import_tree.get_children()[0]
        app.import_tree.selection_set(ident)
        app._change_skill_import("disable")
        wait_until(lambda: app.runtime.manager.match("поздоровайся из модуля") is None)
        assert not failures
    finally:
        app.runtime.close()
        app.integration.stop_all()
        welcome_gui.indicator_controller.stop()
        app.destroy()
