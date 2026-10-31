"""Failure recovery with temporary artifacts; no models or external accounts."""

from pathlib import Path
from contextlib import redirect_stdout
import io
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_windows as builder
from build_bootstrap import prepare_environment, environment_valid
from build_support import BuildSession


class ReleaseRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="krest-build-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / "stage" / "Krest"
        self.stage.mkdir(parents=True)
        (self.stage / "Krest.exe").write_bytes(b"test-only release content")
        self.output = self.root / "dist"
        self.old = self.output / "Krest"
        self.old.mkdir(parents=True)
        (self.old / "keep.txt").write_text("previous version", encoding="utf-8")
        self.archive = self.output / ("Krest-Windows-" + builder.VERSION + ".zip")
        self.archive.write_bytes(b"previous archive")

    def assert_previous(self):
        self.assertEqual((self.old / "keep.txt").read_text(), "previous version")
        self.assertEqual(self.archive.read_bytes(), b"previous archive")
        self.assertTrue((self.stage / "Krest.exe").is_file())
        self.assertEqual(list(self.output.glob("*.partial")), [])

    def test_write_failure_preserves_previous_folder_and_zip(self):
        with patch.object(zipfile.ZipFile, "write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "Прежние файлы сохранены"):
                builder.publish_distribution(self.stage, self.output)
        self.assert_previous()

    def test_failed_crc_never_replaces_release(self):
        with patch.object(zipfile.ZipFile, "testzip", return_value="Krest/Krest.exe"):
            with self.assertRaisesRegex(RuntimeError, "Повреждён"):
                builder.publish_distribution(self.stage, self.output)
        self.assert_previous()

    def test_each_failed_move_rolls_back(self):
        original = Path.rename
        # Old folder, old ZIP, new folder, new ZIP: fail each once, then let rollback run.
        for failed in range(1, 5):
            calls = 0

            def rename(path, target):
                nonlocal calls
                calls += 1
                if calls == failed:
                    raise PermissionError("file is open")
                return original(path, target)

            with self.subTest(move=failed), patch.object(Path, "rename", rename):
                with self.assertRaisesRegex(RuntimeError, "Прежние файлы сохранены"):
                    builder.publish_distribution(self.stage, self.output)
            self.assert_previous()

    def test_success_preserves_old_folder_and_publishes_matching_archive(self):
        target, archive = builder.publish_distribution(self.stage, self.output)
        with zipfile.ZipFile(archive) as zipped:
            self.assertIsNone(zipped.testzip())
            self.assertEqual(zipped.read("Krest/Krest.exe"), (target / "Krest.exe").read_bytes())
        backups = list(self.output.glob("Krest-previous-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "keep.txt").read_text(), "previous version")
        self.assertFalse(self.stage.exists())

    def test_plan_does_not_publish_or_claim_a_finished_build(self):
        output = io.StringIO()
        with (
            patch.object(builder, "ROOT", self.root),
            patch.object(builder, "publish_distribution") as publish,
            patch.object(builder, "report_distribution") as report,
            redirect_stdout(output),
        ):
            builder.main(["--plan"])
        self.assertIn("ТОЛЬКО ПЛАН", output.getvalue())
        self.assertIn(str(self.root / "dist"), output.getvalue())
        self.assertNotIn("СБОРКА ЗАВЕРШЕНА", output.getvalue())
        publish.assert_not_called()
        report.assert_not_called()
        self.assert_previous()

    def test_report_requires_both_published_artifacts(self):
        output = io.StringIO()
        with patch.object(builder, "ROOT", self.root), redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "сборка не подтверждена"):
                builder.report_distribution(self.old, self.archive, open_folder=False)
        self.assertNotIn("СБОРКА ЗАВЕРШЕНА", output.getvalue())
        self.assertFalse((self.root / "build/last-successful-build.txt").exists())

    def test_success_records_absolute_paths_without_opening_when_disabled(self):
        target, archive = builder.publish_distribution(self.stage, self.output)
        output = io.StringIO()
        with (
            patch.object(builder, "ROOT", self.root),
            patch.object(os, "startfile", create=True) as startfile,
            redirect_stdout(output),
        ):
            builder.report_distribution(target, archive, open_folder=False)
        receipt = (self.root / "build/last-successful-build.txt").read_text(encoding="utf-8")
        self.assertIn(str(target.resolve() / "Krest.exe"), receipt)
        self.assertIn(str(archive.resolve()), receipt)
        self.assertIn("СБОРКА ЗАВЕРШЕНА", output.getvalue())
        startfile.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Explorer")
    def test_explorer_failure_does_not_turn_published_release_into_failure(self):
        target, archive = builder.publish_distribution(self.stage, self.output)
        output = io.StringIO()
        with (
            patch.object(builder, "ROOT", self.root),
            patch.dict(os.environ, {"CI": ""}),
            patch.object(os, "startfile", side_effect=OSError("Explorer unavailable")) as startfile,
            redirect_stdout(output),
        ):
            builder.report_distribution(target, archive)
        startfile.assert_called_once_with(str(target.parent.resolve()))
        self.assertTrue((target / "Krest.exe").is_file())
        self.assertTrue(archive.is_file())
        self.assertIn("Проводник не открылся", output.getvalue())

    def test_ci_never_opens_result_folder(self):
        target, archive = builder.publish_distribution(self.stage, self.output)
        with (
            patch.object(builder, "ROOT", self.root),
            patch.dict(os.environ, {"CI": "true"}),
            patch.object(os, "startfile", create=True) as startfile,
            redirect_stdout(io.StringIO()),
        ):
            builder.report_distribution(target, archive)
        startfile.assert_not_called()

    def smoke_calls(self, *, interactive):
        import json

        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "--check-output" in command:
                report = Path(command[command.index("--check-output") + 1])
                report.write_text(json.dumps({"errors": [], "skills": 284}), encoding="utf-8")

        with patch.object(builder, "run_checked", side_effect=run):
            builder.smoke_test(self.stage, **({"interactive": True} if interactive else {}))
        return calls

    def test_standard_build_keeps_runtime_and_voice_checks_without_focus_test(self):
        calls = self.smoke_calls(interactive=False)
        self.assertFalse(any("--release-interactive-check" in command for command in calls))
        for check in (
            "--check",
            "--release-smoke",
            "--release-import-check",
            "--release-model-check",
        ):
            self.assertTrue(any(check in command for command in calls), check)

    def test_ci_build_adds_the_full_interactive_check(self):
        calls = self.smoke_calls(interactive=True)
        self.assertEqual(sum("--release-interactive-check" in command for command in calls), 1)
        self.assertTrue(any("--release-model-check" in command for command in calls))

    def test_foreground_code_zero_is_checked_against_actual_window(self):
        from unittest.mock import Mock
        from release_learning_check import _focus_test_window

        class Win32Error(Exception):
            pass

        foreground = [22]

        def denied(hwnd):
            raise Win32Error(0, "SetForegroundWindow", "No error message is available")

        gui = SimpleNamespace(
            GetForegroundWindow=lambda: foreground[0],
            GetAncestor=lambda hwnd, kind: hwnd,
            SetForegroundWindow=Mock(side_effect=denied),
        )
        with patch.dict(
            sys.modules, {"win32gui": gui, "pywintypes": SimpleNamespace(error=Win32Error)}
        ):
            with self.assertRaisesRegex(RuntimeError, "не дала"):
                _focus_test_window(11)
            self.assertEqual(gui.SetForegroundWindow.call_count, 1)
            foreground[0] = 11
            _focus_test_window(11)
            self.assertEqual(gui.SetForegroundWindow.call_count, 1)

            def changed(hwnd):
                foreground[0] = hwnd
                denied(hwnd)

            foreground[0] = 22
            gui.SetForegroundWindow.side_effect = changed
            _focus_test_window(11)
            self.assertEqual(foreground[0], 11)

    @unittest.skipUnless(os.name == "nt", "Windows UI Automation")
    def test_background_native_check_survives_forbidden_foreground_changes(self):
        from release_learning_check import check_native_recipes

        with patch(
            "win32gui.SetForegroundWindow", side_effect=AssertionError("Focus forbidden")
        ) as focus:
            check_native_recipes(interactive=False)
        focus.assert_not_called()

    def test_builder_lock_does_not_truncate_active_log(self):
        with BuildSession(self.root):
            print("Первый сборщик")
            log = self.root / "build" / "build-last.log"
            before = log.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "уже выполняется"):
                with BuildSession(self.root):
                    self.fail("second builder entered")
            self.assertEqual(log.read_bytes(), before)
        with BuildSession(self.root):
            print("Второй запуск после завершения")
        self.assertIn(
            "Первый сборщик", log.with_name("build-previous.log").read_text(encoding="utf-8")
        )

    def test_failed_child_is_logged_and_exit_status_retained(self):
        # Keep an expected ::error:: out of GitHub's annotation stream.
        with redirect_stdout(io.StringIO()), BuildSession(self.root):
            with self.assertRaises(subprocess.CalledProcessError) as error:
                builder.run_checked(
                    [sys.executable, "-c", "print('builder failure'); raise SystemExit(7)"]
                )
            self.assertEqual(error.exception.returncode, 7)
        self.assertIn("builder failure", (self.root / "build/build-last.log").read_text())

    def check_child_session(self, python, folder):
        child = folder / "child.py"
        root = Path(__file__).resolve().parents[1]
        child.write_text(
            "import sys\n"
            + "sys.path[:0] = "
            + repr([str(root), str(root / "packaging")])
            + "\n"
            + "from build_support import BuildSession\n"
            + "with BuildSession("
            + repr(str(folder))
            + "):\n"
            + "    print('child joined builder')\n",
            encoding="utf-8",
        )
        with BuildSession(folder) as session:
            builder.run_checked([str(python), str(child)], env=session.environment(), timeout=30)
        log = (folder / "build/build-last.log").read_text(encoding="utf-8")
        self.assertIn("child joined builder", log)
        self.assertEqual(log.count("Крест — сборка"), 1)

    def test_child_joins_active_session_without_replacing_log(self):
        self.check_child_session(sys.executable, self.root)

    @unittest.skipUnless(os.name == "nt", "Windows build environment")
    def test_repairs_broken_environment_in_unicode_path(self):
        folder = self.root / "Крест сборка"
        broken = folder / ".build-venv"
        broken.mkdir(parents=True)
        (broken / "keep.txt").write_text("old cache")
        python = prepare_environment(folder, sys.executable, dict(os.environ))
        self.assertTrue(environment_valid(python))
        self.assertEqual(prepare_environment(folder, sys.executable, dict(os.environ)), python)
        self.check_child_session(python, folder)
        backups = list(folder.glob(".build-venv-broken-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "keep.txt").read_text(), "old cache")
        # A caller running inside that cache would keep python.exe locked during
        # repair. Exercise the actual batch selector with a venv as first choice.
        packaging = folder / "packaging"
        packaging.mkdir()
        shutil.copy2(Path(__file__).with_name("find_python.bat"), packaging)
        # .venv is the selector's first candidate, independent of machines which
        # also have Krest's per-user Python installed.
        (folder / ".build-venv").rename(folder / ".venv")
        script = folder / "check.bat"
        script.write_text(
            '@echo off\ncall "%~dp0packaging\\find_python.bat"\n'
            "if errorlevel 1 exit /b 1\n"
            '"%KREST_PYTHON%" %KREST_PY_ARGS% -c "import sys; assert sys.prefix == sys.base_prefix"\n',
            encoding="utf-8",
        )
        subprocess.run(["cmd", "/d", "/c", str(script)], check=True, timeout=60)


if __name__ == "__main__":
    unittest.main()
